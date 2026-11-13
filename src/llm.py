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


class AIError(Exception):
    """The AI extractor failed (API error, or no usable answer)."""


def extract_pdf(pdf_bytes, client=None):
    """Returns (rows, confidence, notes). Raises AIError if the call fails."""
    try:
        if client is None:
            import anthropic
            client = anthropic.Anthropic(max_retries=6)  # waits out rate limits
        # Sonnet 5.5 rejects forced tool use (tool_choice "tool"/"any"), so the tool is
        # offered with "auto" and the prompt asks for it. Medium effort: this is transcription.
        msg = client.messages.create(
            model=MODEL,
            max_tokens=20000,
            tools=[TOOL],
            tool_choice={"type": "auto"},
            extra_body={"output_config": {"effort": "medium"}},
            messages=[{"role": "user", "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.b64encode(pdf_bytes).decode()}},
                {"type": "text", "text": PROMPT},
            ]}],
        )
    except Exception as e:
        raise AIError(str(e)) from e
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


def available():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))
