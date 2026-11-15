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
import os
import re
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
from enrich import Members, Sectors, enrich, load_committee_sectors, set_sector
from validate import check

DONE = {"parsed", "review"}  # statuses that are not retried
MAX_ATTEMPTS = 3   # scheduled runs stop retrying a filing that errored this many times
AI_BREAKER = 3     # API hiccups in a row (after the SDK's own retries) that pause the AI extractor
AI_PAUSE_MIN = 10  # ... for this many minutes
AI_MAX_PAUSES = 3  # ... and after this many pauses it is off for the rest of the run
AI_MONTHLY_BUDGET = 3.0  # USD of AI per calendar month (env AI_MONTHLY_BUDGET overrides); spend kept in data/ai_spend.json
AI_BUDGETS = {"2026-11": 10.0}  # one-off months: Nov 2026 catch-up (double-read re-check + Senate paper), approved 15 Nov
AI_VERSION = "2"  # bump when AI reading changes: filings read by an older version are read again
FUND_WORDS = re.compile(r"\b(etf|fund|trust|index|bond|notes?|treasury|option|call|put)\b", re.I)
MEMBER_FIELDS = ["bioguide", "name", "chamber", "party", "state", "district", "leadership", "committees"]
# What the website loads on every visit: shown trades only, only the columns its pages use.
# The full record stays in trades.csv (all trades, all columns, incl. held-back ones).
SITE_TRADE_FIELDS = ["trade_id", "chamber", "filer_name", "state", "district", "bioguide", "party", "filing_date",
                     "tx_date", "owner", "asset_name", "ticker", "tx_type", "amount_min", "amount_max", "sector",
                     "overlap", "filing_lag_days", "late", "source_url", "status"]


def parsed_with(method):
    if method == "text":
        return f"text-{house.PARSER_VERSION}"
    if method == "ai":
        return f"ai-{AI_VERSION}"
    return method


def is_old_read(parser):
    """Read by an older rules parser or an older AI reading ("" = before versioning, "ai" = single read)."""
    if parser in ("", "ai"):
        return True
    kind = parser.split("-")[0]
    return kind in ("text", "ai") and parser != parsed_with(kind)


def to_retry(rec, retry_errors=False):
    """A filing seen before that should be processed again."""
    status, parser = rec.get("status"), rec.get("parser") or ""
    if status in DONE:
        # House filings read by an older rules parser or older AI reading; Senate AI reads too
        return (rec.get("chamber") == "House" and is_old_read(parser)) or \
               (rec.get("chamber") == "Senate" and parser.startswith("ai") and is_old_read(parser))
    if status == "needs_review":
        # Senate paper filings waited here before they had an AI reader; cut-off answers don't come back
        return rec.get("chamber") == "Senate" and rec.get("kind") == "paper" and not rec.get("error")
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
    if f["kind"] == "paper":  # scanned page images, read by AI
        if not use_llm:
            return [], "", None, "needs_review"
        urls = senate.paper_image_urls(senate.fetch_ptr(s, f["source_url"]), f["source_url"])
        if not urls:
            raise RuntimeError("no page images found on the Senate paper filing page")
        images = []
        for u in urls:
            r = get(s, u)
            images.append((r.content, senate.media_type(u, r.headers.get("Content-Type", ""))))
        rows, conf, _ = llm.extract_images(images)
        return rows, "ai", conf, "parsed"
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
    month = date.today().strftime("%Y-%m")
    spend_path = DATA / "ai_spend.json"
    spend = json.loads(spend_path.read_text()) if spend_path.exists() else {}
    spent_before = float(spend.get(month, 0))
    budget = AI_BUDGETS.get(month) or float(os.environ.get("AI_MONTHLY_BUDGET") or AI_MONTHLY_BUDGET)
    budget_hit = False
    failures, errors = [], []  # source failures; per-filing errors

    # Look back far enough to reach older filings that still need work (errors, stale parses)
    start = {"House": since, "Senate": since}
    for r in filings_db.values():
        if r.get("filing_date") and r.get("chamber") in start and to_retry(r, retry_errors) \
                and (r["status"] not in ("needs_ai", "needs_review") or use_llm):
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
            if os.environ.get("GITHUB_ENV"):  # the workflow then starts the next run straight away
                with open(os.environ["GITHUB_ENV"], "a") as env:
                    env.write("MORE_WORK=1\n")
            break
        processed += 1
        prev = filings_db.get((f["chamber"], f["filing_id"]), {})
        rec = {**{k: f.get(k, "") for k in FILING_FIELDS}, "processed_at": datetime.now().isoformat(timespec="seconds")}
        try:
            parser = parse_house if chamber == "house" else parse_senate
            ai_ok = use_llm and time.monotonic() >= ai_paused_until
            if ai_ok and spent_before + llm.USAGE["usd"] >= budget:
                ai_ok = False  # scanned filings wait (needs_ai) until next month's budget
                if not budget_hit:
                    budget_hit = True
                    print(f"AI budget of ${budget:.2f} for {month} reached: scanned filings wait")
            rows, method, conf, status = parser(s, f, ai_ok)
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
                if method == "ai" and not t["asset_type"] and t["ticker"] \
                        and not FUND_WORDS.search(t["asset_name"]) and sectors.known(t["ticker"]):
                    t["asset_type"] = "Stock"  # paper forms have no type codes; an SEC-listed company ticker is a stock
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
            elif e.kind == "too_long":  # paid for and still cut off: a person looks at it, no retry
                rec.update(status="needs_review", n_trades=0, error=str(e)[:300])
                print(f"AI answer too long on {f['chamber']} {f['filing_id']}: {e}", file=sys.stderr)
            else:
                # Not this filing's fault: it waits for a later run, no attempt counted
                rec.update(status="needs_ai", n_trades=0, error=str(e)[:300], attempts=prev.get("attempts", 0))
                print(f"AI API problem on {f['chamber']} {f['filing_id']}: {e}", file=sys.stderr)
                if e.kind == "no_credit":  # top up in the Claude Console; not a run failure
                    use_llm = False
                    print("AI credit used up: scanned filings wait until credit is added", file=sys.stderr)
                elif e.kind == "fatal":  # bad key or wrong model
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

    # Trades with a company name but no ticker (common in scanned filings): exact SEC name match
    named = 0
    for t in trades_db.values():
        a = t.get("asset_type") or ""
        if t.get("ticker") or not t.get("asset_name") or FUND_WORDS.search(t["asset_name"]) \
                or not (a == "" or (a.startswith("Stock") and "Option" not in a)):
            continue
        tk = sectors.ticker_for_name(t["asset_name"])
        if tk:
            t.update(ticker=tk, asset_type=a or "Stock")
            set_sector(t, members.committees.get(t.get("bioguide"), []), committee_sectors, sectors)
            named += 1
    print(f"Tickers found from company names: {named}")

    sectors.save()
    trades = sorted(trades_db.values(), key=lambda t: (str(t["filing_date"]), t["trade_id"]), reverse=True)
    filings = sorted(filings_db.values(), key=lambda r: str(r["filing_date"]), reverse=True)
    write_csv(DATA / "trades.csv", trades, TRADE_FIELDS)
    write_csv(DATA / "site_trades.csv", [t for t in trades if t.get("status") == "ok"], SITE_TRADE_FIELDS)
    write_csv(DATA / "filings.csv", filings, FILING_FIELDS)
    member_rows = list(members.rows())
    write_csv(DATA / "members.csv", member_rows, MEMBER_FIELDS)

    # Estimated holdings and (if a price key is set) performance, from all ok trades
    ok = [t for t in trades if t.get("status") == "ok"]
    stock_dates = [str(t["tx_date"]) for t in ok if portfolio.is_stock(t) and t.get("tx_date")]
    # Prices from 2021 at the earliest: an older trade date is almost always a misread (data starts 2022)
    start = max(date.fromisoformat(min(stock_dates)), date(2021, 1, 1)) if stock_dates else date.today()
    px = prices.load(s, {t["ticker"] for t in ok if portfolio.is_stock(t)}, start)
    hold = portfolio.holdings(ok, px)
    perf = portfolio.performance(ok, px, date.today())
    port = portfolio.portfolios(ok, hold, perf, {r["bioguide"]: r for r in member_rows})
    write_csv(DATA / "holdings.csv", hold, portfolio.HOLDING_FIELDS)
    write_csv(DATA / "portfolios.csv", port, portfolio.PORTFOLIO_FIELDS)
    # Site-wide facts: which price source, and the plain S&P 500 12-month return for context
    (DATA / "meta.json").write_text(json.dumps({
        "price_source": getattr(px, "source", ""), "spy_12m": portfolio.benchmark_12m(px, date.today()),
        "generated": datetime.now().isoformat(timespec="seconds"),
        "trades_shown": len(ok), "trades_held": len(trades) - len(ok), "filings": len(filings),
        "first_filing": min((str(r["filing_date"]) for r in filings if r.get("filing_date")), default="")}))
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
    spend[month] = round(spent_before + u["usd"], 4)
    spend_path.write_text(json.dumps(spend, indent=1))
    print(f"AI: {u['calls']} calls, {u['in']:,} input + {u['out']:,} output tokens, about ${llm.cost_usd()} "
          f"({llm.MODEL}); {month} so far ${spend[month]:.2f} of ${budget:.2f} budget")
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
