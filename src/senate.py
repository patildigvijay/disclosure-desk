"""Senate: Electronic Financial Disclosure (eFD) search + PTR pages.

Source: https://efdsearch.senate.gov
Flow: accept the site's prohibited-use agreement -> search PTRs (report type 11)
-> open each e-filed PTR page (HTML table). Paper PTRs are scanned images and
are queued for review with their link.

NOTE: the site blocks most non-US connections. Run this from a US server
(GitHub Actions runners are in the US).
"""
import re

from bs4 import BeautifulSoup

from common import (get, norm_owner, norm_ticker, norm_tx_type, parse_amount,
                    parse_date)

BASE = "https://efdsearch.senate.gov"
HOME = f"{BASE}/search/home/"
SEARCH = f"{BASE}/search/"
DATA = f"{BASE}/search/report/data/"
PTR_TYPE = 11

HEADER_MAP = {
    "transaction date": "tx_date", "owner": "owner", "ticker": "ticker",
    "asset name": "asset_name", "asset type": "asset_type", "type": "tx_type",
    "amount": "amount", "comment": "comment",
}


def open_session(s):
    """Accept the eFD agreement so search requests are allowed."""
    r = get(s, HOME)
    token = BeautifulSoup(r.text, "html.parser").find("input", {"name": "csrfmiddlewaretoken"})
    if token is None:
        raise RuntimeError("Senate eFD: agreement form not found (blocked or layout changed)")
    s.post(HOME, data={"prohibition_agreement": "1", "csrfmiddlewaretoken": token["value"]},
           headers={"Referer": HOME}, timeout=60).raise_for_status()


def search_ptrs(s, start_date, page_size=100):
    """All PTRs submitted since start_date (date). Returns filing dicts."""
    out, start = [], 0
    while True:
        payload = {
            "start": str(start), "length": str(page_size),
            "report_types": f"[{PTR_TYPE}]", "filer_types": "[]",
            "submitted_start_date": start_date.strftime("%m/%d/%Y 00:00:00"),
            "submitted_end_date": "", "candidate_state": "", "senator_state": "",
            "office_id": "", "first_name": "", "last_name": "",
            "csrfmiddlewaretoken": s.cookies.get("csrftoken") or s.cookies.get("csrf", ""),
        }
        r = s.post(DATA, data=payload, headers={"Referer": SEARCH}, timeout=60)
        r.raise_for_status()
        body = r.json()
        rows = body.get("data", [])
        out += [parse_search_row(row) for row in rows]
        start += page_size
        if not rows or start >= int(body.get("recordsTotal", 0)):
            return [f for f in out if f]


def parse_search_row(row):
    """[first, last, office, '<a href=/search/view/ptr/UUID/>...</a>', 'MM/DD/YYYY']"""
    first, last, office, link, filed = (row + [""] * 5)[:5]
    m = re.search(r'href="([^"]+)"', link)
    if not m:
        return None
    path = m.group(1)
    kind = "paper" if "/paper/" in path else "efile"
    filing_id = path.rstrip("/").split("/")[-1]
    first = re.sub(r"\s+", " ", first).strip()
    last = re.sub(r"\s+", " ", last).strip()
    return {
        "chamber": "Senate",
        "filing_id": filing_id,
        "filer_name": f"{first} {last}".strip(),
        "first_name": first,
        "last_name": last,
        "state": "", "district": "",
        "filing_date": parse_date(filed),
        "kind": kind,
        "source_url": BASE + path,
        "office": office,
    }


def fetch_ptr(s, url):
    """GET a PTR page; if the session expired (redirect to the agreement page), renew once."""
    r = get(s, url)
    if r.url.rstrip("/") == HOME.rstrip("/"):
        open_session(s)
        r = get(s, url)
    return r.text


def parse_ptr_html(html):
    """Parse the transactions table of an e-filed Senate PTR page."""
    soup = BeautifulSoup(html, "html.parser")
    table = None
    for t in soup.find_all("table"):
        heads = [th.get_text(" ", strip=True).lower() for th in t.find_all("th")]
        if "transaction date" in heads and "amount" in heads:
            table, keys = t, [HEADER_MAP.get(h) for h in heads]
            break
    if table is None:
        raise ValueError("Senate PTR: transactions table not found")
    out = []
    for tr in table.find("tbody").find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(cells) != len(keys):
            continue
        r = {k: v for k, v in zip(keys, cells) if k}
        lo, hi = parse_amount(r.get("amount"))
        comment = r.get("comment", "")
        out.append({
            "owner": norm_owner(r.get("owner")),
            "asset_name": re.sub(r"\s+", " ", r.get("asset_name", "")),
            "ticker": norm_ticker(r.get("ticker")),
            "asset_type": r.get("asset_type", ""),
            "tx_type": norm_tx_type(r.get("tx_type")),
            "tx_date": parse_date(r.get("tx_date")),
            "notification_date": None,
            "amount_min": lo,
            "amount_max": hi,
            "comment": "" if comment in ("--", "") else comment,
        })
    return out
