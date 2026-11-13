"""House of Representatives: index of filings + Periodic Transaction Report (PTR) PDFs.

Source: https://disclosures-clerk.house.gov
- Yearly index:  /public_disc/financial-pdfs/{YEAR}FD.zip  (XML list of every filing)
- PTR PDF:       /public_disc/ptr-pdfs/{YEAR}/{DocID}.pdf
E-filed PTRs (DocID 2xxxxxxx) have a text layer and are parsed by rules.
Paper PTRs (scanned images) have no text layer and go to the AI extractor.
"""
import io
import re
import zipfile
import xml.etree.ElementTree as ET

import pdfplumber

from common import (ASSET_CODES, get, norm_owner, norm_ticker, norm_tx_type,
                    parse_amount, parse_date)

BASE = "https://disclosures-clerk.house.gov"
DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
CODE_RE = re.compile(r"\[([A-Z]{2})\]")
TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,9})\)\s*\[[A-Z]{2}\]")
# Detail lines under each asset: Filing Status, Subholding Of, Description, Comments, Location.
# The PDF font only exposes the label initials, e.g. "F S : New", "S O : IRA", "D : ...";
# the other letters come through as NUL characters, which are stripped first.
DETAIL_RE = re.compile(r"^(F\s*S|S\s*O|D|C|L)\s*:\s*")  # L = location
TABLE_END_RE = re.compile(r"^(\* For the complete list|I P O|Initial Public Offering|C S$|Certification)")


def index_url(year):
    return f"{BASE}/public_disc/financial-pdfs/{year}FD.zip"


def pdf_url(year, doc_id):
    return f"{BASE}/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"


def parse_index(zip_bytes):
    """Yearly FD.zip -> list of PTR filings (FilingType 'P')."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".xml"))
        root = ET.fromstring(z.read(name))
    out = []
    for m in root.iter("Member"):
        f = {c.tag: (c.text or "").strip() for c in m}
        if f.get("FilingType") != "P":
            continue
        sd = f.get("StateDst", "")
        name = " ".join(x for x in (f.get("First"), f.get("Last"), f.get("Suffix")) if x)
        out.append({
            "chamber": "House",
            "filing_id": f["DocID"],
            "filer_name": name,
            "last_name": f.get("Last", ""),
            "first_name": f.get("First", ""),
            "state": sd[:2],
            "district": sd[2:],
            "filing_date": parse_date(f.get("FilingDate")),
            "kind": "efile" if f["DocID"].startswith("2") and len(f["DocID"]) == 8 else "paper",
            "source_url": pdf_url(f.get("Year"), f["DocID"]),
        })
    return out


def fetch_index(s, year):
    return parse_index(get(s, index_url(year)).content)


def _lines(words, tol=3):
    """Group words into visual lines by their top coordinate."""
    lines = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if lines and abs(lines[-1][0]["top"] - w["top"]) <= tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(l, key=lambda w: w["x0"]) for l in lines]


def _columns(lines):
    """Find the table header and return column start x positions, or None."""
    for i, line in enumerate(lines):
        texts = [w["text"] for w in line]
        if "Owner" in texts and "Asset" in texts and "Amount" in texts:
            x = {w["text"]: w["x0"] for w in line}
            dates = [w["x0"] for w in line if w["text"] == "Date"]
            return i, {
                "owner": x["Owner"] - 2,
                "asset": x["Asset"] - 2,
                "type": x.get("Transaction", x["Asset"] + 200) - 2,
                "date": (dates[0] if dates else x.get("Transaction", 0) + 60) - 2,
                "notif": x.get("Notification", x["Amount"] - 60) - 2,
                "amount": x["Amount"] - 2,
                "gains": x.get("Cap.", 10_000) - 2,
            }
    return None


def _col(x, cols):
    order = ["owner", "asset", "type", "date", "notif", "amount", "gains"]
    name = "id"
    for c in order:
        if x >= cols[c]:
            name = c
    return name


def text_layer(pdf_bytes):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


def parse_pdf(pdf_bytes):
    """Rule-based parse of an e-filed PTR. Returns (rows, expected_count).

    expected_count = number of asset-type codes like [ST] in the table, an independent
    check that no row was dropped or invented.
    """
    rows, cols, current, ended = [], None, None, False
    table_text = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            words = page.extract_words(x_tolerance=1.5, y_tolerance=2)
            for w in words:
                w["text"] = w["text"].replace("\x00", "")
            lines = _lines([w for w in words if w["text"]])
            start = 0
            found = _columns(lines)
            if found:
                hdr, cols = found
                start = hdr + 1
                # Skip second header line ("Type Date Gains > $200?")
                while start < len(lines) and any(w["text"] in ("Type", "Gains", "$200?") for w in lines[start]):
                    start += 1
            if cols is None:
                continue
            for line in lines[start:]:
                text = " ".join(w["text"] for w in line)
                if TABLE_END_RE.match(text):
                    ended = True
                    break
                table_text.append(text)
                cells = {}
                for w in line:
                    cells.setdefault(_col(w["x0"], cols), []).append(w["text"])
                date_cell = " ".join(cells.get("date", []))
                if DATE_RE.match(date_cell):
                    current = {k: [] for k in ("owner", "asset", "type", "date", "notif", "amount")}
                    current["details"] = []
                    rows.append(current)
                elif current is not None and (current["details"] or DETAIL_RE.match(text)):
                    # Detail lines span the full width, across the date and amount columns:
                    # keep them whole. Unlabelled lines after one are its continuation.
                    current["details"].append(text)
                    continue
                if current is None:
                    continue
                for k in ("owner", "asset", "type", "date", "notif", "amount"):
                    current[k] += cells.get(k, [])
            if ended:
                break

    out = []
    for r in rows:
        asset = " ".join(r["asset"])
        details = {}
        for d in r["details"]:
            m = DETAIL_RE.match(d)
            if m:
                details[re.sub(r"\s", "", m.group(1))] = d[m.end():].strip()
            elif details:
                last = list(details)[-1]
                details[last] = f"{details[last]} {d}"
        code = CODE_RE.search(asset)
        tick = TICKER_RE.search(asset)
        name = CODE_RE.sub("", asset)
        if tick:
            name = name.replace(f"({tick.group(1)})", "")
        lo, hi = parse_amount(" ".join(r["amount"]))
        comment = "; ".join(f"{k}: {v}" for k, v in details.items() if k in ("SO", "D", "C") and v)
        out.append({
            "owner": norm_owner(" ".join(r["owner"])),
            "asset_name": re.sub(r"\s+", " ", name).strip(" -"),
            "ticker": norm_ticker(tick.group(1) if tick else ""),
            "asset_type": ASSET_CODES.get(code.group(1), code.group(1)) if code else "",
            "tx_type": norm_tx_type(" ".join(r["type"])),
            "tx_date": parse_date(" ".join(r["date"])),
            "notification_date": parse_date(" ".join(r["notif"])),
            "amount_min": lo,
            "amount_max": hi,
            "comment": comment,
        })
    expected = len(CODE_RE.findall(" ".join(table_text)))
    return out, expected
