"""Daily closing prices for performance figures.

Two sources:
- Marketstack (paid, lists commercial use): used when MARKETSTACK_API_KEY is set.
- Yahoo Finance via the free yfinance library: used otherwise. Yahoo's terms are personal
  use only, so this is for testing; add the Marketstack key before the site goes public.
Set PRICE_SOURCE=none to turn performance figures off.

Prices are fetched at most once a day and cached in data/cache/prices/ (git-ignored, so raw
prices are never published in the repo). Marketstack budget (Basic = 10,000 requests/month):
100 tickers per request, last few days daily, full refresh weekly to re-base adjusted prices.
"""
import csv
import json
import os
from collections import defaultdict
from datetime import date, timedelta

from common import DATA, get
from portfolio import BENCHMARK, Prices

API = "https://api.marketstack.com/v2/eod"
CACHE = DATA / "cache" / "prices"
STATE = CACHE / "_state.json"
BATCH = 100
FULL_EVERY_DAYS = 7
LABELS = {"marketstack": "Marketstack", "yfinance": "Yahoo Finance via yfinance (testing only)"}


def source():
    if os.environ.get("MARKETSTACK_API_KEY"):
        return "marketstack"
    return os.environ.get("PRICE_SOURCE", "yfinance").lower()


def _read(path):
    if not path.exists():
        return {}
    with open(path, newline="") as f:
        return {date.fromisoformat(r["date"]): float(r["close"]) for r in csv.DictReader(f)}


def _write(path, series):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "close"])
        w.writerows((d.isoformat(), c) for d, c in sorted(series.items()))


def _marketstack(s, key, symbols, frm, to):
    """{symbol: {date: adj_close}} for up to BATCH symbols, all pages."""
    out, offset = defaultdict(dict), 0
    while True:
        r = get(s, API, params={"access_key": key, "symbols": ",".join(symbols), "date_from": frm.isoformat(),
                                "date_to": to.isoformat(), "limit": 1000, "offset": offset})
        body = r.json()
        for p in body.get("data", []):
            close = p.get("adj_close") or p.get("close")
            if close:
                out[p["symbol"].upper()][date.fromisoformat(p["date"][:10])] = float(close)
        page = body.get("pagination", {})
        offset += int(page.get("count", 0))
        if not page.get("count") or offset >= int(page.get("total", 0)):
            return out


def _yfinance(symbols, frm, to):
    """{symbol: {date: adjusted close}}. Yahoo writes BRK.B as BRK-B."""
    import yfinance as yf
    out = {}
    for i in range(0, len(symbols), 200):
        ymap = {sym.replace(".", "-"): sym for sym in symbols[i:i + 200]}
        df = yf.download(list(ymap), start=frm.isoformat(), end=(to + timedelta(days=1)).isoformat(),
                         auto_adjust=True, progress=False, threads=True)
        if df is None or df.empty:
            continue
        close = df["Close"]
        if getattr(close, "ndim", 2) == 1:  # one ticker comes back as a Series
            close = close.to_frame(name=next(iter(ymap)))
        for ysym in close.columns:
            col = close[ysym].dropna()
            if len(col):
                out[ymap.get(ysym, ysym)] = {d.date(): float(v) for d, v in col.items()}
    return out


def load(s, tickers, start, today=None):
    """Prices for tickers + SPY since start, refreshed at most once a day. None if turned off."""
    src = source()
    if src not in LABELS:
        return None
    today = today or date.today()
    wanted = sorted({t.upper() for t in tickers} | {BENCHMARK})
    cache = {tk: _read(CACHE / f"{tk}.csv") for tk in wanted}
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    fresh = state.get("last_fetch") == today.isoformat() and state.get("source") == src

    if not fresh:
        last_full = date.fromisoformat(state.get("last_full", "2000-01-01")) if state.get("source") == src else date(2000, 1, 1)
        full = src == "yfinance" or (today - last_full).days >= FULL_EVERY_DAYS
        groups = defaultdict(list)  # fetch start date -> tickers
        for tk in wanted:
            groups[start if full or not cache[tk] else max(cache[tk]) - timedelta(days=5)].append(tk)
        for frm, tks in groups.items():
            try:
                if src == "yfinance":
                    got = _yfinance(tks, frm, today)
                else:
                    got = {}
                    for i in range(0, len(tks), BATCH):
                        got.update(_marketstack(s, os.environ["MARKETSTACK_API_KEY"], tks[i:i + BATCH], frm, today))
            except Exception as e:  # source down: keep cached prices
                print(f"prices ({src}): {e}")
                continue
            for tk, series in got.items():
                cache[tk] = series if full else {**cache.get(tk, {}), **series}
                _write(CACHE / f"{tk}.csv", cache[tk])
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"source": src, "last_fetch": today.isoformat(),
                                     "last_full": today.isoformat() if full else last_full.isoformat()}))
    px = Prices({tk: list(v.items()) for tk, v in cache.items()})
    px.source = LABELS[src]
    return px
