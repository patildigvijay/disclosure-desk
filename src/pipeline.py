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
MEMBER_FIELDS = ["bioguide", "name", "chamber", "party", "state", "district", "leadership", "committees"]


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
    rows, conf, _ = llm.extract_pdf(pdf)
    return rows, "ai", conf, "parsed"


def parse_senate(s, f, use_llm):
    if f["kind"] == "paper":
        return [], "", None, "needs_review"  # scanned images; v1 queues these with their link
    rows = senate.parse_ptr_html(senate.fetch_ptr(s, f["source_url"]))
    return rows, "html", None, "parsed"


def run(since, chambers, max_filings=None, use_llm=True, strict=False):
    s = session()
    filings_db = {(r["chamber"], r["filing_id"]): r for r in read_csv(DATA / "filings.csv")}
    trades_db = {r["trade_id"]: r for r in read_csv(DATA / "trades.csv")}
    members = Members.load(s)
    committee_sectors = load_committee_sectors()
    sectors = Sectors(s)
    sectors.load_tickers()  # fail fast (before any state changes) if SEC is unreachable
    use_llm = use_llm and llm.available()
    failures = []

    todo = []
    if "house" in chambers:
        try:
            for year in range(since.year, date.today().year + 1):
                todo += [("house", f) for f in house.fetch_index(s, year)
                         if f["filing_date"] and f["filing_date"] >= since]
        except Exception as e:
            failures.append(f"House index: {e}")
    if "senate" in chambers:
        try:
            senate.open_session(s)
            todo += [("senate", f) for f in senate.search_ptrs(s, since)]
        except Exception as e:
            failures.append(f"Senate search: {e}")

    todo = [(c, f) for c, f in todo
            if filings_db.get((f["chamber"], f["filing_id"]), {}).get("status") not in DONE]
    todo.sort(key=lambda x: x[1]["filing_date"] or date.min)
    if max_filings:
        todo = todo[:max_filings]
    print(f"{len(todo)} new filings to process")

    for chamber, f in todo:
        rec = {**{k: f.get(k, "") for k in FILING_FIELDS}, "processed_at": datetime.now().isoformat(timespec="seconds")}
        try:
            parser = parse_house if chamber == "house" else parse_senate
            rows, method, conf, status = parser(s, f, use_llm)
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
            rec.update(status=status, n_trades=len(rows), error="")
        except Exception as e:
            rec.update(status="error", n_trades=0, error=str(e)[:300])
            failures.append(f"{f['chamber']} {f['filing_id']}: {e}")
            traceback.print_exc()
        filings_db[(f["chamber"], f["filing_id"])] = rec
        time.sleep(0.5)  # be polite to government servers

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
    for msg in failures:
        print("FAILED:", msg, file=sys.stderr)
    return 1 if (strict and failures) else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=f"{date.today().year}-01-01")
    ap.add_argument("--chambers", default="house,senate")
    ap.add_argument("--max", type=int, default=None, help="limit filings per run (testing)")
    ap.add_argument("--no-ai", action="store_true", help="never call the AI extractor")
    ap.add_argument("--strict", action="store_true", help="exit 1 if a chamber could not be fetched")
    a = ap.parse_args()
    sys.exit(run(datetime.strptime(a.since, "%Y-%m-%d").date(), a.chambers.split(","),
                 a.max, not a.no_ai, a.strict))


if __name__ == "__main__":
    main()
