"""Trade execution for paper mode and future live mode."""

import logging
import sqlite3
from dataclasses import dataclass
from typing import Protocol

from polypocket.config import FEE_RATE
from polypocket.ledger import (
    credit_paper_balance,
    deduct_paper_balance,
    get_paper_balance,
    find_trade_by_window_slug,
    log_trade,
    update_trade,
    update_trade_status,
)
from polypocket.signal import Signal

log = logging.getLogger(__name__)


@dataclass
class TradeResult:
    success: bool
    trade_id: int | None = None
    pnl: float | None = None
    error: str | None = None


class LiveOrderClient(Protocol):
    def submit_fok(self, side: str, price: float, size: float, client_order_id: str) -> None:
        ...


def _window_client_order_id(window_slug: str) -> str:
    return f"window-{window_slug}"


def _window_consumed_result(db_path: str, window_slug: str) -> TradeResult:
    existing_trade = find_trade_by_window_slug(db_path, window_slug)
    return TradeResult(
        success=False,
        trade_id=None if existing_trade is None else existing_trade["id"],
        error="window-already-consumed",
    )


def execute_paper_trade(
    db_path: str,
    signal: Signal,
    entry_price: float,
    size: float,
    window_slug: str,
    outcome: str | None = None,
) -> TradeResult:
    """Execute a paper trade, optionally settling immediately."""
    existing_trade = find_trade_by_window_slug(db_path, window_slug)
    if existing_trade is not None:
        return _window_consumed_result(db_path, window_slug)

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

    try:
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
    except sqlite3.IntegrityError:
        consumed = _window_consumed_result(db_path, window_slug)
        if consumed.trade_id is not None:
            return consumed
        raise

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


def execute_live_trade(
    db_path: str,
    signal: Signal,
    entry_price: float,
    size: float,
    window_slug: str,
    client: LiveOrderClient,
) -> TradeResult:
    existing_trade = find_trade_by_window_slug(db_path, window_slug)
    if existing_trade is not None:
        return _window_consumed_result(db_path, window_slug)

    client_order_id = _window_client_order_id(window_slug)
    fees = entry_price * size * FEE_RATE
    try:
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
            outcome=None,
            pnl=None,
            status="reserved",
        )
    except sqlite3.IntegrityError:
        consumed = _window_consumed_result(db_path, window_slug)
        if consumed.trade_id is not None:
            return consumed
        raise
    client.submit_fok(
        side=signal.side,
        price=entry_price,
        size=size,
        client_order_id=client_order_id,
    )
    update_trade_status(db_path, trade_id, "open")
    return TradeResult(success=True, trade_id=trade_id, pnl=None)


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


def settle_live_trade(db_path: str, trade_id: int, outcome: str) -> None:
    """Mark a live trade resolved locally without touching paper balances."""
    update_trade(db_path, trade_id, outcome=outcome, pnl=None, status="settled")
