"""Accuracy checks on every parsed trade.

Hard issues send the trade to the review queue. Warnings are kept as facts.
"""
from common import amount_ok

HARD = {"no_asset", "no_tx_date", "tx_after_filing", "notified_before_tx",
        "amount_not_official", "bad_tx_type", "low_confidence"}
TX_TYPES = {"Purchase", "Sale", "Sale (Partial)", "Exchange"}
LATE_DAYS = 45
MIN_CONFIDENCE = 0.9


def check(trade, filing_date, sectors=None, confidence=None):
    """Adds issues, status, filing_lag_days and late to the trade (in place)."""
    issues = []
    tx, notif = trade.get("tx_date"), trade.get("notification_date")
    if not trade.get("asset_name"):
        issues.append("no_asset")
    if tx is None:
        issues.append("no_tx_date")
    elif filing_date and tx > filing_date:
        issues.append("tx_after_filing")
    if tx and notif and notif < tx:
        issues.append("notified_before_tx")
    if not amount_ok(trade.get("amount_min"), trade.get("amount_max")):
        issues.append("amount_not_official")
    if trade.get("tx_type") not in TX_TYPES:
        issues.append("bad_tx_type")
    if confidence is not None and confidence < MIN_CONFIDENCE:
        issues.append("low_confidence")
    if sectors and trade.get("ticker") and not sectors.known(trade["ticker"]):
        issues.append("ticker_not_in_sec_list")  # warning: funds, foreign and delisted tickers
    lag = (filing_date - tx).days if (tx and filing_date) else None
    trade.update(
        issues="|".join(issues),
        status="review" if HARD & set(issues) else "ok",
        filing_lag_days=lag,
        late=lag is not None and lag > LATE_DAYS,
    )
    return trade
