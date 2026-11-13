"""Estimated holdings and performance, built from disclosed trades.

Congress members disclose trades and dollar ranges, not share counts. So:
- Position size: midpoint of each disclosed range. Buys add, partial sales reduce,
  a full sale closes the position. Stocks only (options and funds are left out).
- With daily prices, each trade becomes estimated shares, valued at the latest close.
  Without prices, positions are valued at estimated cost.
- Performance (needs prices): every purchase in the last 12 months, held until today or
  until a full sale, weighted by its midpoint, compared with the S&P 500 (SPY) over the
  same days. "Copy" return starts on the filing date instead: what a follower could get.
"""
import bisect
import re
import unicodedata
from collections import defaultdict
from datetime import date, timedelta

BENCHMARK = "SPY"
MIN_BUYS = 3  # fewer purchases than this in 12 months: no performance figure

HOLDING_FIELDS = ["member_key", "bioguide", "name", "ticker", "asset_name", "sector", "est_value",
                  "weight", "first_tx", "last_tx", "last_action", "held_by", "overlap", "overlap_committees",
                  "valued_at"]
PORTFOLIO_FIELDS = ["member_key", "bioguide", "name", "chamber", "party", "state", "district",
                    "holdings", "est_value", "top10_weight", "trades", "last_filed", "buys_12m",
                    "return_12m", "spy_12m", "excess_12m", "copy_return_12m", "copy_spy_12m"]


class Prices:
    """Daily closes per ticker: {ticker: [(date, close), ...]} sorted by date."""

    def __init__(self, series):
        self.series = {k.upper(): sorted(v) for k, v in series.items() if v}

    def close_on(self, ticker, d):
        """Last close on or before d (covers weekends and holidays), or None."""
        s = self.series.get(ticker.upper())
        if not s or d is None:
            return None
        i = bisect.bisect_right(s, (d, float("inf"))) - 1
        return s[i][1] if i >= 0 else None

    def latest(self, ticker):
        s = self.series.get(ticker.upper())
        return s[-1] if s else None


def member_key(t):
    """Same key the website builds: bioguide ID, or a slug of the filer's name."""
    if t.get("bioguide"):
        return t["bioguide"]
    s = unicodedata.normalize("NFKD", (t.get("filer_name") or "").lower())
    return "x-" + re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def _date(v):
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _mid(t):
    lo = float(t.get("amount_min") or 0)
    hi = t.get("amount_max")
    return (lo + float(hi)) / 2 if hi not in (None, "") else lo


def is_stock(t):
    a = (t.get("asset_type") or "").lower()
    return a.startswith("stock") and "option" not in a and bool(t.get("ticker"))


def _stock_trades(trades):
    out = [dict(t, _tx=_date(t.get("tx_date")), _filed=_date(t.get("filing_date")), _mid=_mid(t))
           for t in trades if t.get("status") == "ok" and is_stock(t)]
    out = [t for t in out if t["_tx"]]
    out.sort(key=lambda t: (t["_tx"], str(t.get("trade_id"))))
    return out


def holdings(trades, prices=None):
    """Current estimated positions per member. Returns HOLDING_FIELDS rows.

    Positions are tracked per owner (Self, Spouse, Joint, Dependent), so a full sale in one
    account does not close the same stock held in another, then combined under the member.
    """
    groups = defaultdict(list)
    for t in _stock_trades(trades):
        groups[(member_key(t), t["ticker"].upper())].append(t)
    rows = []
    for (key, tk), ts in groups.items():
        last = prices.latest(tk) if prices else None
        priced = bool(last) and all(prices.close_on(tk, t["_tx"]) for t in ts)
        qty, action = defaultdict(float), ""
        for t in ts:
            q = t["_mid"] / prices.close_on(tk, t["_tx"]) if priced else t["_mid"]
            who = t.get("owner") or "Self"
            if t["tx_type"] == "Purchase":
                action = "Add" if sum(qty.values()) > 0 else "Buy"
                qty[who] += q
            elif t["tx_type"] in ("Sale (Partial)", "Sale"):
                qty[who] = 0.0 if t["tx_type"] == "Sale" else max(0.0, qty[who] - q)
                action = "Reduce" if sum(qty.values()) > 0 else "Sell"
        total = sum(qty.values())
        if total <= 0:
            continue
        flagged = [t for t in ts if str(t.get("overlap")) == "True"]
        rows.append({
            "member_key": key, "bioguide": ts[-1].get("bioguide", ""), "name": ts[-1].get("filer_name", ""),
            "ticker": tk, "asset_name": ts[-1].get("asset_name", ""), "sector": ts[-1].get("sector", ""),
            "est_value": round(total * last[1] if priced else total), "first_tx": ts[0]["_tx"].isoformat(),
            "last_tx": ts[-1]["_tx"].isoformat(), "last_action": action,
            "held_by": ";".join(f"{o}={round(100 * v / total)}" for o, v in sorted(qty.items(), key=lambda x: -x[1]) if v > 0),
            "overlap": bool(flagged), "overlap_committees": flagged[-1].get("overlap_committees", "") if flagged else "",
            "valued_at": last[0].isoformat() if priced else "cost",
        })
    totals = defaultdict(float)
    for r in rows:
        totals[r["member_key"]] += r["est_value"]
    for r in rows:
        r["weight"] = round(100 * r["est_value"] / totals[r["member_key"]], 2) if totals[r["member_key"]] else 0
    return sorted(rows, key=lambda r: (r["member_key"], -r["weight"]))


def performance(trades, prices, today):
    """{member_key: {...}} 12-month estimated returns in %, or {} without prices."""
    if not prices or not prices.latest(BENCHMARK):
        return {}
    since = today - timedelta(days=365)
    st = _stock_trades(trades)
    full_sales = defaultdict(list)
    for t in st:
        if t["tx_type"] == "Sale":
            full_sales[(member_key(t), t["ticker"].upper(), t.get("owner") or "Self")].append(t["_tx"])
    acc = defaultdict(lambda: defaultdict(float))
    for t in st:
        if t["tx_type"] != "Purchase" or t["_tx"] < since:
            continue
        key, tk = member_key(t), t["ticker"].upper()
        end = next((d for d in full_sales[(key, tk, t.get("owner") or "Self")] if d >= t["_tx"]), today)
        p0, p1 = prices.close_on(tk, t["_tx"]), prices.close_on(tk, end)
        s0, s1 = prices.close_on(BENCHMARK, t["_tx"]), prices.close_on(BENCHMARK, end)
        if not (p0 and p1 and s0 and s1):
            continue
        w, a = t["_mid"], acc[key]
        a["w"] += w
        a["n"] += 1
        a["r"] += w * (p1 / p0 - 1)
        a["s"] += w * (s1 / s0 - 1)
        c0, cs0 = prices.close_on(tk, t["_filed"]), prices.close_on(BENCHMARK, t["_filed"])
        if c0 and cs0 and (t["_filed"] or today) <= end:
            a["cw"] += w
            a["cr"] += w * (p1 / c0 - 1)
            a["cs"] += w * (s1 / cs0 - 1)
    out = {}
    for key, a in acc.items():
        if a["n"] < MIN_BUYS:
            continue
        r, s = 100 * a["r"] / a["w"], 100 * a["s"] / a["w"]
        out[key] = {"buys_12m": int(a["n"]), "return_12m": round(r, 1), "spy_12m": round(s, 1),
                    "excess_12m": round(r - s, 1),
                    "copy_return_12m": round(100 * a["cr"] / a["cw"], 1) if a["cw"] else "",
                    "copy_spy_12m": round(100 * a["cs"] / a["cw"], 1) if a["cw"] else ""}
    return out


def benchmark_12m(prices, today):
    """Plain S&P 500 (SPY) return over the last 12 months, in %, for context."""
    if not prices:
        return None
    a, b = prices.close_on(BENCHMARK, today - timedelta(days=365)), prices.close_on(BENCHMARK, today)
    return round(100 * (b / a - 1), 2) if a and b else None


def portfolios(trades, holding_rows, perf, members_by_bioguide):
    """One summary row per member who has traded. Returns PORTFOLIO_FIELDS rows."""
    by_member = defaultdict(list)
    for t in trades:
        if t.get("status") == "ok":
            by_member[member_key(t)].append(t)
    held = defaultdict(list)
    for h in holding_rows:
        held[h["member_key"]].append(h)
    rows = []
    for key, ts in by_member.items():
        m = members_by_bioguide.get(ts[0].get("bioguide"), {})
        hs = sorted(held[key], key=lambda h: -h["weight"])
        rows.append({
            "member_key": key, "bioguide": ts[0].get("bioguide", ""),
            "name": m.get("name") or ts[0].get("filer_name", ""), "chamber": ts[0].get("chamber", ""),
            "party": m.get("party") or ts[0].get("party", ""), "state": m.get("state") or ts[0].get("state", ""),
            "district": m.get("district", ts[0].get("district", "")),
            "holdings": len(hs), "est_value": round(sum(h["est_value"] for h in hs)),
            "top10_weight": round(sum(h["weight"] for h in hs[:10]), 1), "trades": len(ts),
            "last_filed": max(str(t.get("filing_date")) for t in ts), **perf.get(key, {}),
        })
    return rows
