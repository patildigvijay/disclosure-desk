"""AI extraction for filings the rule-based parsers can't read (scanned paper PTRs,
or e-filed PDFs that fail the row-count check). Uses Claude via the Anthropic API.

Needs ANTHROPIC_API_KEY in the environment. Model is set by ANTHROPIC_MODEL.
"""
import base64
import io
import os

from common import (ASSET_CODES, norm_owner, norm_ticker, norm_tx_type, parse_amount,
                    parse_date)

MODEL = os.environ.get("ANTHROPIC_MODEL") or "claude-haiku-5-5"
# USD per million tokens (input, output). Haiku 5.5 rate is for prompts up to 100k tokens,
# which chunking keeps us under. Unknown models are costed at Sonnet rates (the safe side).
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.0, 10.0)}
PAGES_PER_CALL = 5       # long scans are sent in chunks so no answer gets cut off
TOKENS_PER_PAGE = 6000   # answer room per page (~40 rows); a 5-page chunk gets 32k

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
                        "owner": {"type": "string", "description": "As written: blank/Self, SP, JT, DC"},
                        "asset_name": {"type": "string"},
                        "ticker": {"type": "string", "description": "Ticker if shown, else empty"},
                        "asset_type": {"type": "string", "description": "Code in brackets e.g. ST, OP, or type text"},
                        "tx_type": {"type": "string", "description": "P, S, S (partial), E, or full words"},
                        "tx_date": {"type": "string", "description": "MM/DD/YYYY"},
                        "notification_date": {"type": "string", "description": "MM/DD/YYYY or empty"},
                        "amount_range": {"type": "string", "description": "Exactly as written, e.g. $1,001 - $15,000"},
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


def _call(client, pdf_bytes, pages):
    request = dict(
        model=MODEL,
        max_tokens=TOKENS_PER_PAGE * pages + 2000,
        tools=[TOOL],
        # Sonnet 5.5 rejects forced tool use (tool_choice "tool"/"any"): offer the tool, ask for it.
        tool_choice={"type": "auto"},
        messages=[{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                            "data": base64.b64encode(pdf_bytes).decode()}},
            {"type": "text", "text": PROMPT},
        ]}],
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


def extract_pdf(pdf_bytes, client=None):
    """Returns (rows, confidence, notes). Raises AIError if a call fails.
    Long PDFs are read in chunks of PAGES_PER_CALL pages; confidence is the lowest chunk's."""
    if client is None:
        import anthropic
        client = anthropic.Anthropic(max_retries=6)  # waits out short rate limits / hiccups
    try:
        chunks = split_pdf(pdf_bytes)
    except Exception:  # unreadable by pypdf: send it whole and let the model try
        chunks = [(pdf_bytes, PAGES_PER_CALL)]
    rows, confs, notes = [], [], []
    for chunk, pages in chunks:
        data = _call(client, chunk, pages)
        confs.append(float(data.get("confidence", 0) or 0))
        if data.get("notes"):
            notes.append(data["notes"])
        for t in data.get("transactions", []):
            lo, hi = parse_amount(t.get("amount_range"))
            code = (t.get("asset_type") or "").strip("[] ").upper()
            rows.append({
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
    return rows, min(confs) if confs else 0.0, " | ".join(notes)


def cost_usd():
    return round(USAGE["usd"], 2)


def available():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))
