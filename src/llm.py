"""AI extraction for filings the rule-based parsers can't read (scanned paper PTRs,
or e-filed PDFs that fail the row-count check). Uses Claude via the Anthropic API.

Needs ANTHROPIC_API_KEY in the environment. Model is set by ANTHROPIC_MODEL.
"""
import base64
import os

from common import (ASSET_CODES, norm_owner, norm_ticker, norm_tx_type, parse_amount,
                    parse_date)

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")

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


# Running totals for the log: calls, input tokens, output tokens (USD/M tokens: Sonnet 5.5 $2 in, $10 out)
USAGE = {"calls": 0, "in": 0, "out": 0}
PRICE_PER_M = (2.0, 10.0)


class AIError(Exception):
    """The AI extractor failed. kind:
    "filing"    - this filing's problem (too long, unreadable, no answer): counts as a filing error
    "transient" - API hiccup (5xx, overloaded, rate limit, network): try again later
    "fatal"     - API unusable (bad key, no credit, wrong model): stop calling it this run
    """

    def __init__(self, message, kind="filing"):
        super().__init__(message)
        self.kind = kind


def error_kind(e):
    status = getattr(e, "status_code", None)
    names = {c.__name__ for c in type(e).__mro__}
    text = str(e).lower()
    if status in (401, 403, 404) or (status == 400 and ("credit" in text or "billing" in text)):
        return "fatal"
    if status == 429 or (status or 0) >= 500 or "APIConnectionError" in names:
        return "transient"
    return "filing"


def extract_pdf(pdf_bytes, client=None):
    """Returns (rows, confidence, notes). Raises AIError if the call fails."""
    request = dict(
        model=MODEL,
        max_tokens=64000,  # long scanned filings can have hundreds of rows
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
        if client is None:
            import anthropic
            client = anthropic.Anthropic(max_retries=6)  # waits out short rate limits / hiccups
        # Streaming: the SDK requires it above 21,333 max_tokens
        with client.messages.stream(**request) as stream:
            msg = stream.get_final_message()
    except Exception as e:
        raise AIError(str(e), error_kind(e)) from e
    usage = getattr(msg, "usage", None)
    USAGE["calls"] += 1
    USAGE["in"] += getattr(usage, "input_tokens", 0) or 0
    USAGE["out"] += getattr(usage, "output_tokens", 0) or 0
    if getattr(msg, "stop_reason", "") == "max_tokens":
        raise AIError("answer cut off at max_tokens (filing too long)")
    data = next((b.input for b in msg.content if getattr(b, "type", "") == "tool_use"), None)
    if data is None:
        raise AIError("model did not call the record_transactions tool")
    rows = []
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
    return rows, float(data.get("confidence", 0)), data.get("notes", "")


def cost_usd():
    return round(USAGE["in"] / 1e6 * PRICE_PER_M[0] + USAGE["out"] / 1e6 * PRICE_PER_M[1], 2)


def available():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))
