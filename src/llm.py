"""AI extraction for filings the rule-based parsers can't read (scanned paper PTRs,
or e-filed PDFs that fail the row-count check). Uses Claude via the Anthropic API.

Needs ANTHROPIC_API_KEY in the environment. Model is set by ANTHROPIC_MODEL.
"""
import base64
import io
import json
import os
import re
from collections import Counter

from common import (ASSET_CODES, norm_owner, norm_ticker, norm_tx_type, parse_amount,
                    parse_date)

MODEL = os.environ.get("ANTHROPIC_MODEL") or "claude-haiku-5-5"
# USD per million tokens (input, output). Haiku 5.5 rate is for prompts up to 100k tokens,
# which chunking keeps us under. Unknown models are costed at Sonnet rates (the safe side).
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.0, 10.0)}
PAGES_PER_CALL = 5       # long scans are sent in chunks so no answer gets cut off
TOKENS_PER_PAGE = 6000   # answer room per page (~40 rows); a 5-page chunk gets 32k
READS = 2                # each chunk is read twice; a row goes live only if both reads agree

TOOL = {
    "name": "record_transactions",
    "description": "Record every transaction row in this congressional Periodic Transaction Report.",
    "input_schema": {
        "type": "object",
        "properties": {
            "transactions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string", "description": "SP, JT, DC, or empty for the filer"},
                        "asset_name": {"type": "string"},
                        "ticker": {"type": "string", "description": "Ticker if shown, else empty"},
                        "asset_type": {"type": "string", "description": "Code in brackets e.g. ST, OP, or type text"},
                        "tx_type": {"type": "string", "description": "One of: P, S, S (partial), E"},
                        "tx_date": {"type": "string", "description": "MM/DD/YYYY with 4-digit year"},
                        "notification_date": {"type": "string", "description": "MM/DD/YYYY or empty"},
                        "amount_range": {"type": "string",
                                         "description": "The official range, e.g. $1,001 - $15,000, or the exact amount if one is written"},
                        "comment": {"type": "string"},
                    },
                    "required": ["asset_name", "tx_type", "tx_date", "amount_range"],
                },
            },
            "confidence": {"type": "number", "description": "0-1: how sure you are every row is complete and exact"},
            "notes": {"type": "string", "description": "Anything illegible or ambiguous"},
        },
        "required": ["transactions", "confidence"],
    },
}

PROMPT = (
    "This is a US congressional Periodic Transaction Report (STOCK Act). "
    "Extract EVERY transaction row exactly as written. Do not guess: if a field is "
    "illegible, leave it empty and say so in notes. Ignore the certification and IPO sections. "
    "For a checked amount box on a paper form, return that box's range. "
    "Normalise as you copy: dates as MM/DD/YYYY with a 4-digit year; tx_type as P, S, "
    "S (partial) or E (a checkmark in a column means that column's type); owner as SP, JT, DC "
    "or empty for the filer; amounts as the official range written out, e.g. $1,001 - $15,000. "
    "Return the result by calling the record_transactions tool exactly once, with all rows."
)


# Running totals for this run: calls, input tokens, output tokens, dollars
USAGE = {"calls": 0, "in": 0, "out": 0, "usd": 0.0}


def price(model=None):
    model = model or MODEL
    return next((p for k, p in PRICES.items() if model.startswith(k)), PRICES["claude-sonnet-5-5"])


class AIError(Exception):
    """The AI extractor failed. kind:
    "filing"    - this filing's problem (unreadable, no answer): counts as a filing error
    "too_long"  - answer cut off even in chunks: goes to manual review, never retried (paid)
    "transient" - API hiccup (5xx, overloaded, rate limit, network): try again later
    "no_credit" - API credit used up: AI off for this run, filings wait (not a run failure)
    "fatal"     - API unusable (bad key, wrong model): AI off, run fails so you get an email
    """

    def __init__(self, message, kind="filing"):
        super().__init__(message)
        self.kind = kind


def error_kind(e):
    status = getattr(e, "status_code", None)
    names = {c.__name__ for c in type(e).__mro__}
    text = str(e).lower()
    if status == 400 and ("credit" in text or "billing" in text):
        return "no_credit"
    if status in (401, 403, 404):
        return "fatal"
    if status == 429 or (status or 0) >= 500 or "APIConnectionError" in names:
        return "transient"
    return "filing"


def split_pdf(pdf_bytes, per_call=PAGES_PER_CALL):
    """[(chunk_bytes, n_pages)]: the PDF cut into chunks of at most per_call pages."""
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(io.BytesIO(pdf_bytes))
    n = len(reader.pages)
    if n <= per_call:
        return [(pdf_bytes, max(n, 1))]
    chunks = []
    for i in range(0, n, per_call):
        w = PdfWriter()
        for page in reader.pages[i:i + per_call]:
            w.add_page(page)
        buf = io.BytesIO()
        w.write(buf)
        chunks.append((buf.getvalue(), min(per_call, n - i)))
    return chunks


def pdf_block(pdf_bytes):
    return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                           "data": base64.b64encode(pdf_bytes).decode()}}


def image_block(image_bytes, media_type):
    return {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                        "data": base64.b64encode(image_bytes).decode()}}


def _call(client, blocks, pages):
    request = dict(
        model=MODEL,
        max_tokens=TOKENS_PER_PAGE * pages + 2000,
        tools=[TOOL],
        # Sonnet 5.5 rejects forced tool use (tool_choice "tool"/"any"): offer the tool, ask for it.
        tool_choice={"type": "auto"},
        messages=[{"role": "user", "content": blocks + [{"type": "text", "text": PROMPT}]}],
    )
    if MODEL.startswith("claude-sonnet-5-5"):
        # No up-front thinking: this is transcription, and thinking is billed as output.
        request["thinking"] = {"type": "between_tools"}
    try:
        # Streaming: the SDK requires it above 21,333 max_tokens
        with client.messages.stream(**request) as stream:
            msg = stream.get_final_message()
    except Exception as e:
        raise AIError(str(e), error_kind(e)) from e
    usage = getattr(msg, "usage", None)
    tin = getattr(usage, "input_tokens", 0) or 0
    tout = getattr(usage, "output_tokens", 0) or 0
    pin, pout = price()
    USAGE["calls"] += 1
    USAGE["in"] += tin
    USAGE["out"] += tout
    USAGE["usd"] += tin / 1e6 * pin + tout / 1e6 * pout
    if getattr(msg, "stop_reason", "") == "max_tokens":
        raise AIError(f"answer cut off at {request['max_tokens']} tokens for {pages} pages", "too_long")
    data = next((b.input for b in msg.content if getattr(b, "type", "") == "tool_use"), None)
    if data is None:
        raise AIError("model did not call the record_transactions tool")
    return data


def _rows(data):
    out = []
    txs = data.get("transactions") or []
    if isinstance(txs, str):  # the model sometimes sends the list as a JSON string
        try:
            txs = json.loads(txs)
        except ValueError:
            txs = []
    for t in txs if isinstance(txs, list) else []:
        if not isinstance(t, dict):  # a row sent as plain text can't be checked: skip it
            continue
        lo, hi = parse_amount(t.get("amount_range"))
        code = (t.get("asset_type") or "").strip("[] ").upper()
        out.append({
            "owner": norm_owner(t.get("owner")),
            "asset_name": (t.get("asset_name") or "").strip(),
            "ticker": norm_ticker(t.get("ticker")),
            "asset_type": ASSET_CODES.get(code, t.get("asset_type", "")),
            "tx_type": norm_tx_type(t.get("tx_type")),
            "tx_date": parse_date(t.get("tx_date")),
            "notification_date": parse_date(t.get("notification_date")),
            "amount_min": lo,
            "amount_max": hi,
            "comment": (t.get("comment") or "").strip(),
        })
    return out


def row_key(r):
    """What two reads must agree on: owner, security, type, date, amount."""
    name = re.sub(r"[^a-z0-9]", "", r["asset_name"].lower())[:12]
    return (r["owner"], r["ticker"] or name, r["tx_type"], r["tx_date"], r["amount_min"], r["amount_max"])


def mark_agreement(first, second):
    """Sets double_read on each row of the first read: "agree" if the second read has the same row."""
    left = Counter(row_key(r) for r in second)
    for r in first:
        k = row_key(r)
        r["double_read"] = "agree" if left[k] > 0 else "differ"
        left[k] -= 1 if left[k] > 0 else 0
    return first


def extract(chunks, client=None, reads=READS):
    """chunks: [(content_blocks, n_pages)]. Returns (rows, confidence, notes).
    Each chunk is read `reads` times; rows come from the first read, marked against the second.
    Confidence is the lowest of the first reads. Raises AIError if a call fails."""
    if client is None:
        import anthropic
        client = anthropic.Anthropic(max_retries=6)  # waits out short rate limits / hiccups
    rows, confs, notes = [], [], []
    for blocks, pages in chunks:
        answers = [_call(client, blocks, pages) for _ in range(reads)]
        first = _rows(answers[0])
        if reads > 1:
            mark_agreement(first, _rows(answers[1]))
        rows += first
        confs.append(float(answers[0].get("confidence", 0) or 0))
        if answers[0].get("notes"):
            notes.append(answers[0]["notes"])
    return rows, min(confs) if confs else 0.0, " | ".join(notes)


def extract_pdf(pdf_bytes, client=None, reads=READS):
    """A PDF, read in chunks of PAGES_PER_CALL pages."""
    try:
        parts = split_pdf(pdf_bytes)
    except Exception:  # unreadable by pypdf: send it whole and let the model try
        parts = [(pdf_bytes, PAGES_PER_CALL)]
    return extract([([pdf_block(b)], n) for b, n in parts], client, reads)


def extract_images(images, client=None, reads=READS):
    """Page images [(bytes, media_type)] (Senate paper filings), PAGES_PER_CALL per call."""
    chunks = [([image_block(b, m) for b, m in images[i:i + PAGES_PER_CALL]], len(images[i:i + PAGES_PER_CALL]))
              for i in range(0, len(images), PAGES_PER_CALL)]
    return extract(chunks, client, reads)


def cost_usd():
    return round(USAGE["usd"], 2)


def available():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))
