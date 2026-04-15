"""Quote validation helpers for Polymarket books."""

from dataclasses import dataclass

from polypocket.config import BOOK_MAX_TOTAL_ASK


@dataclass
class QuoteSnapshot:
    up_ask: float | None = None
    down_ask: float | None = None


@dataclass
class QuoteValidation:
    valid: bool
    reason: str | None = None


def validate_quote(snapshot: QuoteSnapshot) -> QuoteValidation:
    if snapshot.up_ask is None or snapshot.down_ask is None:
        return QuoteValidation(valid=False, reason="missing-side")

    if not 0.0 <= snapshot.up_ask <= 1.0 or not 0.0 <= snapshot.down_ask <= 1.0:
        return QuoteValidation(valid=False, reason="ask-out-of-range")

    if snapshot.up_ask + snapshot.down_ask > BOOK_MAX_TOTAL_ASK:
        return QuoteValidation(valid=False, reason="overround")

    return QuoteValidation(valid=True)
