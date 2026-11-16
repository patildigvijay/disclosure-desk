"""Daily closing prices for performance figures.

Sources, first one configured wins:
- Marketstack (paid, lists commercial use): MARKETSTACK_API_KEY.
- Alpaca market data, free plan (official API; all-exchange daily bars since 2016 except the
  latest 15 minutes): ALPACA_KEY_ID + ALPACA_SECRET. Free plan = testing / private review.
- Yahoo Finance via the free yfinance library: otherwise. Unofficial, throttled from cloud
  servers, personal use only.
Add a source that permits public display before the site goes public.
Set PRICE_SOURCE=none to turn performance figures off.

Prices are fetched at most once a day and cached in data/cache/prices/ (git-ignored, so raw
prices are never published in the repo). Marketstack budget (Basic = 10,000 requests/month):
100 tickers per request, last few days daily, full refresh weekly to re-base adjusted prices.
"""
import csv
import json
import os
import re
import time
from collections import defaultdict
from datetime import date, timedelta

from common import DATA, get
from portfolio import BENCHMARK, Prices

API = "https://api.marketstack.com/v2/eod"
ALPACA_BARS = "https://data.alpaca.markets/v2/stocks/bars"
ALPACA_SYMBOL = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")  # AAPL, BRK.B; skips misread tickers like "-- WBD"
CACHE = DATA / "cache" / "prices"
STATE = CACHE / "_state.json"
BATCH = 100
FULL_EVERY_DAYS = 7
LABELS = {"marketstack": "Marketstack", "alpaca": "Alpaca market data, free plan (testing only)",
          "yfinance": "Yahoo Finance via yfinance (testing only)"}


def source():
    if os.environ.get("MARKETSTACK_API_KEY"):
        return "marketstack"
    if os.environ.get("ALPACA_KEY_ID") and os.environ.get("ALPACA_SECRET"):
        return "alpaca"
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


def _alpaca(s, symbols, frm, to):
    """{symbol: {date: close}}, split- and dividend-adjusted, 100 symbols a request, all pages.
    Up to yesterday: the free plan refuses the latest 15 minutes. A batch Alpaca rejects
    (unknown symbol) is split in two until the bad symbol is found and skipped."""
    headers = {"APCA-API-KEY-ID": os.environ["ALPACA_KEY_ID"], "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET"]}
    out = defaultdict(dict)

    def fetch(batch):
        token, tries = None, 0
        while True:
            params = {"symbols": ",".join(batch), "timeframe": "1Day", "start": frm.isoformat(),
                      "end": (to - timedelta(days=1)).isoformat(), "adjustment": "all", "feed": "sip",
                      "limit": 10000}
            if token:
                params["page_token"] = token
            r = s.get(ALPACA_BARS, params=params, headers=headers, timeout=60)
            if (r.status_code == 429 or r.status_code >= 500) and tries < 5:
                tries += 1
                time.sleep(5 * tries)
                continue
            if r.status_code in (400, 404, 422):
                if len(batch) > 1:
                    fetch(batch[:len(batch) // 2])
                    fetch(batch[len(batch) // 2:])
                return
            if r.status_code in (401, 403):  # bad/regenerated key or plan limit: say which, keep cached prices
                raise RuntimeError(f"Alpaca refused ({r.status_code}): {r.text[:200]}")
            r.raise_for_status()
            body = r.json()
            for sym, bars in (body.get("bars") or {}).items():
                for b in bars:
                    out[sym.upper()][date.fromisoformat(b["t"][:10])] = float(b["c"])
            token = body.get("next_page_token")
            if not token:
                return
            time.sleep(0.35)  # free plan: 200 requests a minute

    symbols = [x for x in symbols if ALPACA_SYMBOL.match(x)]
    for i in range(0, len(symbols), BATCH):
        fetch(symbols[i:i + BATCH])
    return out


def _yfinance(symbols, frm, to):
    """{symbol: {date: adjusted close}}. Yahoo writes BRK.B as BRK-B."""
    import time
    import yfinance as yf
    out = {}
    # Yahoo throttles bursts from cloud servers: small batches, one at a time, a pause between,
    # and the S&P 500 benchmark first on its own (every performance figure needs it).
    order = sorted(symbols, key=lambda x: x != BENCHMARK)
    batches = [order[:1]] + [order[i:i + 50] for i in range(1, len(order), 50)]
    for n, batch in enumerate(batches):
        ymap = {sym.replace(".", "-"): sym for sym in batch}
        for attempt in range(3 if n == 0 else 1):
            df = yf.download(list(ymap), start=frm.isoformat(), end=(to + timedelta(days=1)).isoformat(),
                             auto_adjust=True, progress=False, threads=False)
            if df is not None and not df.empty:
                break
            time.sleep(20)
        time.sleep(2)
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
                elif src == "alpaca":
                    got = _alpaca(s, tks, frm, today)
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
