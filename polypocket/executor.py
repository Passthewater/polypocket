"""Trade execution for paper mode and future live mode."""

import logging
from dataclasses import dataclass

from polypocket.config import FEE_RATE
from polypocket.ledger import (
    credit_paper_balance,
    deduct_paper_balance,
    get_paper_balance,
    log_trade,
    update_trade,
)
from polypocket.signal import Signal

log = logging.getLogger(__name__)


@dataclass
class TradeResult:
    success: bool
    trade_id: int | None = None
    pnl: float | None = None
    error: str | None = None


def execute_paper_trade(
    db_path: str,
    signal: Signal,
    entry_price: float,
    size: float,
    window_slug: str,
    outcome: str | None = None,
) -> TradeResult:
    """Execute a paper trade, optionally settling immediately."""
    cost = entry_price * size
    fees = cost * FEE_RATE

    balance = get_paper_balance(db_path)
    if balance < cost + fees:
        return TradeResult(
            success=False,
            error=f"Insufficient balance: need ${cost + fees:.2f}, have ${balance:.2f}",
        )

    pnl = None
    status = "open"
    payout = 0.0
    if outcome is not None:
        won = signal.side == outcome
        payout = size if won else 0.0
        pnl = payout - cost - fees
        status = "settled"

    trade_id = log_trade(
        db_path=db_path,
        window_slug=window_slug,
        side=signal.side,
        entry_price=entry_price,
        size=size,
        fees=fees,
        model_p_up=signal.model_p_up,
        market_p_up=signal.market_price,
        edge=signal.edge,
        outcome=outcome,
        pnl=pnl,
        status=status,
    )

    deduct_paper_balance(db_path, cost + fees)

    if outcome is not None:
        credit_paper_balance(db_path, payout)

    if pnl is not None:
        log.info(
            "Paper trade %s: %s @ $%.3f x%.1f -> %s (P&L: $%.2f)",
            window_slug,
            signal.side,
            entry_price,
            size,
            "WON" if pnl > 0 else "LOST",
            pnl,
        )

    return TradeResult(success=True, trade_id=trade_id, pnl=pnl)


def settle_paper_trade(
    db_path: str,
    trade_id: int,
    entry_price: float,
    size: float,
    side: str,
    outcome: str,
) -> float:
    """Settle an open paper trade when the window resolves."""
    fees = entry_price * size * FEE_RATE
    cost = entry_price * size
    payout = size if side == outcome else 0.0
    pnl = payout - cost - fees

    credit_paper_balance(db_path, payout)
    update_trade(db_path, trade_id, outcome=outcome, pnl=pnl, status="settled")
    return pnl
