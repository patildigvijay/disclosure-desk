"""Run the full update: find new filings -> parse -> enrich -> validate -> save CSVs.

Usage:
  python src/pipeline.py                     # everything new since 1 Jan this year
  python src/pipeline.py --since 2026-10-01 --chambers house --max 20
Outputs (data/):
  trades.csv   one row per transaction (ok + review)
  filings.csv  one row per filing and its processing status
  review.csv   trades/filings that need a human look
"""
import argparse
import json
import sys
import time
import traceback
from datetime import date, datetime

import house
import llm
import portfolio
import prices
import senate
from common import (DATA, FILING_FIELDS, TRADE_FIELDS, amount_label, amount_ok, get, read_csv,
                    session, write_csv)
from enrich import Members, Sectors, enrich, load_committee_sectors
from validate import check

DONE = {"parsed", "review"}  # statuses that are not retried
MAX_ATTEMPTS = 3   # scheduled runs stop retrying a filing that errored this many times
AI_BREAKER = 3     # API hiccups in a row (after the SDK's own retries) that pause the AI extractor
AI_PAUSE_MIN = 10  # ... for this many minutes
AI_MAX_PAUSES = 3  # ... and after this many pauses it is off for the rest of the run
MEMBER_FIELDS = ["bioguide", "name", "chamber", "party", "state", "district", "leadership", "committees"]


def parsed_with(method):
    return f"text-{house.PARSER_VERSION}" if method == "text" else method


def to_retry(rec, retry_errors=False):
    """A filing seen before that should be processed again."""
    status, parser = rec.get("status"), rec.get("parser") or ""
    if status in DONE:
        # House filings read by an older version of the rules parser. Blank = before
        # versioning (all of those were rules-parsed).
        return rec.get("chamber") == "House" and (parser == "" or
                                                  (parser.startswith("text-") and parser != parsed_with("text")))
    if status == "error":
        return retry_errors or int(rec.get("attempts") or 0) < MAX_ATTEMPTS
    return status == "needs_ai"


def parse_house(s, f, use_llm):
    """Returns (rows, method, confidence, status)."""
    if f["kind"] == "paper" and not use_llm:
        return [], "", None, "needs_ai"
    pdf = get(s, f["source_url"]).content
    if f["kind"] == "efile":
        try:
            rows, expected = house.parse_pdf(pdf)
            clean = all(r["tx_date"] and amount_ok(r["amount_min"], r["amount_max"]) for r in rows)
            if rows and len(rows) == expected and clean:
                return rows, "text", None, "parsed"
        except Exception:
            pass  # fall through to AI
    if not use_llm:
        return [], "", None, "needs_ai"
    rows, conf, _ = llm.extract_pdf(pdf)  # raises llm.AIError
    return rows, "ai", conf, "parsed"


def parse_senate(s, f, use_llm):
    if f["kind"] == "paper":
        return [], "", None, "needs_review"  # scanned images; v1 queues these with their link
    rows = senate.parse_ptr_html(senate.fetch_ptr(s, f["source_url"]))
    return rows, "html", None, "parsed"


def run(since, chambers, max_filings=None, use_llm=True, strict=False, retry_errors=False,
        budget_min=150):
    started = time.monotonic()
    s = session()
    filings_db = {(r["chamber"], r["filing_id"]): r for r in read_csv(DATA / "filings.csv")}
    trades_db = {r["trade_id"]: r for r in read_csv(DATA / "trades.csv")}
    members = Members.load(s)
    committee_sectors = load_committee_sectors()
    sectors = Sectors(s)
    sectors.load_tickers()  # fail fast (before any state changes) if SEC is unreachable
    use_llm = use_llm and llm.available()
    failures, errors = [], []  # source failures; per-filing errors

    # Look back far enough to reach older filings that still need work (errors, stale parses)
    start = {"House": since, "Senate": since}
    for r in filings_db.values():
        if r.get("filing_date") and r.get("chamber") in start and to_retry(r, retry_errors) \
                and (r["status"] != "needs_ai" or use_llm):
            start[r["chamber"]] = min(start[r["chamber"]], date.fromisoformat(str(r["filing_date"])))

    todo = []
    if "house" in chambers:
        try:
            for year in range(start["House"].year, date.today().year + 1):
                todo += [("house", f) for f in house.fetch_index(s, year)
                         if f["filing_date"] and f["filing_date"] >= start["House"]]
        except Exception as e:
            failures.append(f"House index: {e}")
    if "senate" in chambers:
        try:
            senate.open_session(s)
            todo += [("senate", f) for f in senate.search_ptrs(s, start["Senate"])]
        except Exception as e:
            failures.append(f"Senate search: {e}")

    def wanted(f):
        rec = filings_db.get((f["chamber"], f["filing_id"]))
        return rec is None or to_retry(rec, retry_errors)

    todo = [(c, f) for c, f in todo if wanted(f)]
    todo.sort(key=lambda x: x[1]["filing_date"] or date.min, reverse=True)  # newest first
    if max_filings:
        todo = todo[:max_filings]
    print(f"{len(todo)} filings to process (new, retried or re-read)")

    ai_hiccups, ai_pauses, ai_paused_until = 0, 0, 0.0
    processed = 0
    for chamber, f in todo:
        if time.monotonic() - started > budget_min * 60:
            print(f"Time budget of {budget_min} min reached: {len(todo) - processed} filings left for the next run")
            break
        processed += 1
        prev = filings_db.get((f["chamber"], f["filing_id"]), {})
        rec = {**{k: f.get(k, "") for k in FILING_FIELDS}, "processed_at": datetime.now().isoformat(timespec="seconds")}
        try:
            parser = parse_house if chamber == "house" else parse_senate
            rows, method, conf, status = parser(s, f, use_llm and time.monotonic() >= ai_paused_until)
            if method == "ai":
                ai_hiccups = 0
            if status == "parsed":  # replace any trades from an earlier read of this filing
                prefix = f"{f['chamber'][0]}-{f['filing_id']}-"
                for k in [k for k in trades_db if k.startswith(prefix)]:
                    del trades_db[k]
            for i, t in enumerate(rows, 1):
                t.update(trade_id=f"{f['chamber'][0]}-{f['filing_id']}-{i}", chamber=f["chamber"],
                         filing_id=f["filing_id"], row=i, filer_name=f["filer_name"],
                         district=f.get("district", ""), filing_date=f["filing_date"],
                         source_url=f["source_url"], parse_method=method, confidence=conf,
                         amount_range=amount_label(t["amount_min"], t["amount_max"]))
                enrich(t, f, members, committee_sectors, sectors)
                check(t, f["filing_date"], sectors, conf)
                trades_db[t["trade_id"]] = t
            if any(t["status"] == "review" for t in rows):
                status = "review"
            rec.update(status=status, n_trades=len(rows), error="", parser=parsed_with(method), attempts=0)
        except llm.AIError as e:
            if e.kind == "filing":
                rec.update(status="error", n_trades=0, error=str(e)[:300],
                           attempts=int(prev.get("attempts") or 0) + 1)
                errors.append(f"{f['chamber']} {f['filing_id']}: {e}")
            else:
                # Not this filing's fault: it waits for a later run, no attempt counted
                rec.update(status="needs_ai", n_trades=0, error=str(e)[:300], attempts=prev.get("attempts", 0))
                print(f"AI API problem on {f['chamber']} {f['filing_id']}: {e}", file=sys.stderr)
                if e.kind == "fatal":  # bad key, no credit, wrong model
                    use_llm = False
                    failures.append(f"AI extractor switched off: {e}")
                else:
                    ai_hiccups += 1
                    if ai_hiccups >= AI_BREAKER:
                        ai_hiccups, ai_pauses = 0, ai_pauses + 1
                        if ai_pauses >= AI_MAX_PAUSES:
                            use_llm = False
                            failures.append(f"AI extractor switched off after {ai_pauses} pauses: {e}")
                        else:
                            ai_paused_until = time.monotonic() + AI_PAUSE_MIN * 60
                            print(f"AI paused {AI_PAUSE_MIN} min after {AI_BREAKER} API errors in a row")
        except Exception as e:
            rec.update(status="error", n_trades=0, error=str(e)[:300],
                       attempts=int(prev.get("attempts") or 0) + 1)
            errors.append(f"{f['chamber']} {f['filing_id']}: {e}")
            traceback.print_exc()
        filings_db[(f["chamber"], f["filing_id"])] = rec
        time.sleep(0.5)  # be polite to government servers

    # Trades with no member match (e.g. filed before the member data had former members)
    for t in trades_db.values():
        if not t.get("bioguide") and t.get("filer_name"):
            name = t["filer_name"]  # matcher uses its last word as surname, all words as first names
            enrich(t, {"chamber": t["chamber"], "last_name": name, "first_name": name,
                       "state": t.get("state", ""), "district": t.get("district", "")},
                   members, committee_sectors, sectors)

    sectors.save()
    trades = sorted(trades_db.values(), key=lambda t: (str(t["filing_date"]), t["trade_id"]), reverse=True)
    filings = sorted(filings_db.values(), key=lambda r: str(r["filing_date"]), reverse=True)
    write_csv(DATA / "trades.csv", trades, TRADE_FIELDS)
    write_csv(DATA / "filings.csv", filings, FILING_FIELDS)
    member_rows = list(members.rows())
    write_csv(DATA / "members.csv", member_rows, MEMBER_FIELDS)

    # Estimated holdings and (if a price key is set) performance, from all ok trades
    ok = [t for t in trades if t.get("status") == "ok"]
    stock_dates = [str(t["tx_date"]) for t in ok if portfolio.is_stock(t) and t.get("tx_date")]
    start = date.fromisoformat(min(stock_dates)) if stock_dates else date.today()
    px = prices.load(s, {t["ticker"] for t in ok if portfolio.is_stock(t)}, start)
    hold = portfolio.holdings(ok, px)
    perf = portfolio.performance(ok, px, date.today())
    port = portfolio.portfolios(ok, hold, perf, {r["bioguide"]: r for r in member_rows})
    write_csv(DATA / "holdings.csv", hold, portfolio.HOLDING_FIELDS)
    write_csv(DATA / "portfolios.csv", port, portfolio.PORTFOLIO_FIELDS)
    # Site-wide facts: which price source, and the plain S&P 500 12-month return for context
    (DATA / "meta.json").write_text(json.dumps({
        "price_source": getattr(px, "source", ""), "spy_12m": portfolio.benchmark_12m(px, date.today()),
        "generated": datetime.now().isoformat(timespec="seconds")}))
    print(f"Holdings: {len(hold)} positions, {len(port)} members | "
          + (f"performance for {len(perf)} members ({px.source})" if px else "prices off: valued at cost, no performance"))
    review = [t for t in trades if t.get("status") == "review"]
    review += [{**r, "issues": r["status"], "trade_id": ""} for r in filings
               if r["status"] in ("needs_ai", "needs_review", "error")]
    write_csv(DATA / "review.csv", review, TRADE_FIELDS)

    summary = {}
    for r in filings_db.values():
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    print("Filings by status:", summary)
    print("Trades:", len(trades), "| overlap flags:", sum(str(t.get("overlap")) == "True" for t in trades),
          "| in review:", len([t for t in trades if t.get("status") == "review"]))
    # A few filing errors (a server hiccup) are retried next run; many mean something broke.
    too_many = len(errors) > max(5, 0.05 * processed)
    print(f"Filing errors: {len(errors)} of {processed} processed" + (" (too many)" if too_many else ""))
    u = llm.USAGE
    print(f"AI: {u['calls']} calls, {u['in']:,} input + {u['out']:,} output tokens, about ${llm.cost_usd()}")
    for msg in errors[:20]:
        print("ERROR:", msg, file=sys.stderr)
    for msg in failures:
        print("FAILED:", msg, file=sys.stderr)
    return 1 if (strict and (failures or too_many)) else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=f"{date.today().year}-01-01")
    ap.add_argument("--chambers", default="house,senate")
    ap.add_argument("--max", type=int, default=None, help="limit filings per run (testing)")
    ap.add_argument("--no-ai", action="store_true", help="never call the AI extractor")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 if a source could not be fetched, the AI was switched off, or >5%% of filings errored")
    ap.add_argument("--retry-errors", action="store_true",
                    help=f"retry errored filings even after {MAX_ATTEMPTS} attempts")
    ap.add_argument("--budget", type=int, default=150,
                    help="stop starting new filings after this many minutes; the rest wait for the next run")
    a = ap.parse_args()
    sys.exit(run(datetime.strptime(a.since, "%Y-%m-%d").date(), a.chambers.split(","),
                 a.max, not a.no_ai, a.strict, a.retry_errors, a.budget))


if __name__ == "__main__":
    main()
