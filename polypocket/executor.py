"""Trade execution for paper mode and future live mode."""

import logging
import sqlite3
from dataclasses import dataclass
from typing import Literal, Protocol

from polypocket.config import fee_shares
from polypocket.ledger import (
    credit_paper_balance,
    deduct_paper_balance,
    get_paper_balance,
    find_trade_by_window_slug,
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


@dataclass(frozen=True)
class FillResult:
    status: Literal["filled", "rejected", "error"]
    order_id: str | None
    filled_size: float
    avg_price: float | None
    error: str | None


@dataclass(frozen=True)
class SettlementInfo:
    """Post-fill, post-resolution accounting pulled from the CLOB.

    `shares_held` is the outcome-token balance actually owned (post-fee).
    `cost_usdc` is the USDC that actually left the account for the fill.
    """
    shares_held: float
    cost_usdc: float


class LiveOrderClient(Protocol):
    def submit_fok(
        self, side: str, price: float, size: float,
        token_id: str, condition_id: str,
    ) -> FillResult: ...
    def submit_ioc(
        self, side: str, price: float, size: float,
        token_id: str, condition_id: str,
    ) -> FillResult: ...
    def cancel_order(self, order_id: str) -> bool: ...
    def get_usdc_balance(self) -> float: ...
    def get_settlement_info(self, order_id: str) -> SettlementInfo: ...
    def get_order_status(self, order_id: str) -> dict: ...


def reconcile_recovered_trade(
    db_path: str,
    trade: dict,
    client: LiveOrderClient | None,
) -> str:
    """Query CLOB for a recovered trade's order status and reconcile local DB.

    Called only in live mode during startup recovery. Returns the final local
    status: "open" (resume into _open_trade) or "rejected" (window consumed,
    no position to resume). On any uncertainty (no order id, no client,
    CLOB error, unknown status, resting order) returns the existing local
    status unchanged and writes nothing, preserving today's recovery
    behavior when CLOB evidence isn't available.
    """
    current_status = trade["status"]
    order_id = trade.get("external_order_id")
    if not order_id or client is None:
        return current_status

    try:
        resp = client.get_order_status(order_id)
    except Exception as exc:
        log.warning(
            "reconcile: get_order_status failed for trade %s order %s: %s",
            trade["id"], order_id, exc,
        )
        return current_status

    if not resp:
        return current_status

    clob_status = str(resp.get("status", "")).strip().lower()

    if clob_status == "matched":
        if current_status != "open":
            update_trade(db_path, trade["id"], outcome=None, pnl=None, status="open")
        return "open"

    if clob_status in {"canceled", "cancelled", "unmatched"}:
        update_trade(db_path, trade["id"], outcome=None, pnl=None, status="rejected")
        return "rejected"

    log.warning(
        "reconcile: unexpected CLOB status %r for trade %s order %s; keeping local %r",
        clob_status, trade["id"], order_id, current_status,
    )
    return current_status


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
    fee_sh = fee_shares(size, entry_price)

    balance = get_paper_balance(db_path)
    if balance < cost:
        return TradeResult(
            success=False,
            error=f"Insufficient balance: need ${cost:.2f}, have ${balance:.2f}",
        )

    pnl = None
    status = "open"
    payout = 0.0
    if outcome is not None:
        won = signal.side == outcome
        payout = (size - fee_sh) if won else 0.0
        pnl = payout - cost
        status = "settled"

    try:
        trade_id = log_trade(
            db_path=db_path,
            window_slug=window_slug,
            side=signal.side,
            entry_price=entry_price,
            size=size,
            fees=fee_sh,
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

    deduct_paper_balance(db_path, cost)

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
    token_id: str,
    condition_id: str,
    client: LiveOrderClient,
) -> TradeResult:
    existing_trade = find_trade_by_window_slug(db_path, window_slug)
    if existing_trade is not None:
        return _window_consumed_result(db_path, window_slug)

    usdc_needed = entry_price * size
    if client.get_usdc_balance() < usdc_needed:
        return TradeResult(success=False, error="insufficient-balance")

    fee_sh = fee_shares(size, entry_price)
    try:
        trade_id = log_trade(
            db_path=db_path,
            window_slug=window_slug,
            side=signal.side,
            entry_price=entry_price,
            size=size,
            fees=fee_sh,
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

    fill = client.submit_ioc(
        side=signal.side,
        price=entry_price,
        size=size,
        token_id=token_id,
        condition_id=condition_id,
    )

    if fill.status == "filled":
        update_trade(
            db_path, trade_id, outcome=None, pnl=None, status="open",
            external_order_id=fill.order_id,
            size=fill.filled_size,
            entry_price=fill.avg_price,
        )
        log.info(
            "Live fill: %s %s requested=%.2f filled=%.4f vwap=$%.4f token=%s order=%s",
            window_slug, signal.side, size, fill.filled_size,
            fill.avg_price, token_id, fill.order_id,
        )
        return TradeResult(success=True, trade_id=trade_id, pnl=None)

    # rejected or error
    update_trade(
        db_path, trade_id, outcome=None, pnl=None, status="rejected",
        error=fill.error,
    )
    log.warning(
        "Live reject/error: %s %s @%.4f x%.2f: %s",
        window_slug, signal.side, entry_price, size, fill.error,
    )
    return TradeResult(success=False, trade_id=trade_id, error=fill.error)


def settle_paper_trade(
    db_path: str,
    trade_id: int,
    entry_price: float,
    size: float,
    side: str,
    outcome: str,
) -> float:
    """Settle an open paper trade when the window resolves."""
    cost = entry_price * size
    fee_sh = fee_shares(size, entry_price)
    won = side == outcome
    payout = (size - fee_sh) if won else 0.0
    pnl = payout - cost

    credit_paper_balance(db_path, payout)
    update_trade(db_path, trade_id, outcome=outcome, pnl=pnl, status="settled")
    return pnl


def settle_live_trade(
    db_path: str,
    trade_id: int,
    side: str,
    outcome: str,
    order_id: str | None,
    client: LiveOrderClient | None,
) -> float | None:
    """Reconcile a resolved live trade against the CLOB and write real PnL.

    Returns the computed PnL, or None if reconciliation couldn't run (legacy
    rows without an external_order_id, or CLOB lookup errors) — in which
    case the row is still marked settled with pnl=None so the bot can move on.
    """
    if order_id is None or client is None:
        log.warning(
            "settle_live_trade: no order_id/client for trade %s — marking settled with pnl=None",
            trade_id,
        )
        update_trade(db_path, trade_id, outcome=outcome, pnl=None, status="settled")
        return None

    try:
        info = client.get_settlement_info(order_id)
    except Exception as exc:
        log.exception("settle_live_trade: CLOB lookup failed for order %s: %s", order_id, exc)
        update_trade(db_path, trade_id, outcome=outcome, pnl=None, status="settled")
        return None

    payout = info.shares_held if side == outcome else 0.0
    pnl = payout - info.cost_usdc
    update_trade(db_path, trade_id, outcome=outcome, pnl=pnl, status="settled")
    log.info(
        "LIVE SETTLED trade %s: %s %s x%.4f cost=$%.4f payout=$%.4f pnl=$%.4f",
        trade_id, side, outcome, info.shares_held, info.cost_usdc, payout, pnl,
    )
    return pnl
