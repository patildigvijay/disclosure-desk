"""Shared helpers: HTTP session, field normalisation, CSV storage."""
import csv
import os
import re
import time
from datetime import date, datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG = ROOT / "config"

# SEC and the Senate want a real contact in the User-Agent. Set it in the environment.
USER_AGENT = os.environ.get("SCRAPER_USER_AGENT", "congress-trades-bot (set SCRAPER_USER_AGENT)")

TRADE_FIELDS = [
    "trade_id", "chamber", "filing_id", "row", "filer_name", "state", "district",
    "bioguide", "party", "filing_date", "tx_date", "notification_date", "owner",
    "asset_name", "ticker", "asset_type", "tx_type", "amount_range", "amount_min",
    "amount_max", "comment", "sector", "member_committees", "overlap", "overlap_committees",
    "filing_lag_days", "late", "parse_method", "confidence", "issues", "status", "source_url",
]
FILING_FIELDS = [
    "chamber", "filing_id", "filer_name", "state", "district", "filing_date",
    "kind", "source_url", "status", "n_trades", "error", "processed_at",
]

# Official STOCK Act value ranges (min, max). max=None means "over".
AMOUNT_RANGES = [
    (1001, 15000), (15001, 50000), (50001, 100000), (100001, 250000),
    (250001, 500000), (500001, 1000000), (1000001, 5000000),
    (5000001, 25000000), (25000001, 50000000), (50000001, None),
    (1000001, None),  # "Spouse/DC Over $1,000,000"
]

OWNERS = {"": "Self", "SELF": "Self", "SP": "Spouse", "SPOUSE": "Spouse",
          "JT": "Joint", "JOINT": "Joint", "DC": "Dependent", "CHILD": "Dependent",
          "DEPENDENT": "Dependent"}

TX_TYPES = {"P": "Purchase", "PURCHASE": "Purchase", "S": "Sale", "SALE": "Sale",
            "SALE (FULL)": "Sale", "S (PARTIAL)": "Sale (Partial)",
            "SALE (PARTIAL)": "Sale (Partial)", "E": "Exchange", "EXCHANGE": "Exchange"}

# House asset-type codes -> plain labels.
ASSET_CODES = {"ST": "Stock", "OP": "Option", "EF": "ETF", "MF": "Mutual Fund",
               "CS": "Corporate Bond", "GS": "Government Security", "CT": "Crypto",
               "PS": "Private Stock", "HN": "Private Fund", "OT": "Other", "OI": "Other"}


def session():
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def get(s, url, tries=3, **kw):
    """GET with simple retry and backoff. Raises on final failure."""
    for i in range(tries):
        try:
            r = s.get(url, timeout=60, **kw)
            if r.status_code == 200:
                return r
            if r.status_code not in (429, 500, 502, 503, 504):
                r.raise_for_status()
        except requests.RequestException:
            if i == tries - 1:
                raise
        time.sleep(2 ** (i + 1))
    r.raise_for_status()
    return r


def parse_date(text):
    """MM/DD/YYYY (or YYYY-MM-DD) -> date, or None."""
    text = (text or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def parse_amount(text):
    """'$1,001 - $15,000' -> (1001, 15000); 'Over $50,000,000' -> (50000001, None)."""
    t = (text or "").replace("–", "-").replace("—", "-")
    nums = [float(n.replace(",", "")) for n in re.findall(r"\$\s*([\d,]+(?:\.\d+)?)", t)]
    nums = [int(n) if n.is_integer() else n for n in nums]
    if not nums:
        return None, None
    if re.search(r"over", t, re.I):
        return nums[0] + 1, None
    if len(nums) == 1:
        return nums[0], nums[0]
    return nums[0], nums[1]


def amount_ok(lo, hi):
    """An official range, or an exact amount under $1,001 (some filers report these voluntarily)."""
    return (lo, hi) in AMOUNT_RANGES or (lo is not None and lo == hi and 0 < lo < 1001)


def amount_label(lo, hi):
    if lo is None:
        return ""
    if hi is None:
        return f"Over ${lo - 1:,}"
    return f"${lo:,}" if lo == hi else f"${lo:,} - ${hi:,}"


def norm_owner(text):
    return OWNERS.get((text or "").strip().upper(), (text or "").strip().title())


def norm_tx_type(text):
    t = re.sub(r"\s+", " ", (text or "").strip()).upper()
    return TX_TYPES.get(t, t.title())


def norm_ticker(text):
    t = (text or "").strip().upper()
    return "" if t in ("", "--", "N/A", "NONE") else t


def read_csv(path):
    path = Path(path)
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, fields):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fields})
    tmp.replace(path)


def today():
    return date.today()
