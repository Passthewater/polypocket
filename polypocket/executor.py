"""Trade execution for paper mode and future live mode."""

import logging
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from typing import Literal, Protocol

from polypocket.config import fee_shares, MIN_POSITION_USDC
from polypocket.ledger import (
    credit_paper_balance,
    deduct_paper_balance,
    get_paper_balance,
    find_trade_by_window_slug,
    log_order_event,
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
class PlaceResult:
    """Outcome of a post-only place request. Distinct from FillResult, which
    represents terminal fills; a PlaceResult represents the order's
    acceptance into the book (or its rejection at placement).

    `placed_size` is the size actually placed on the server (after the SDK
    wrapper's tick-safe integer quantization). Differs from the caller's
    intended_size by up to ±6 — the executor must update the trade row to
    this value so the local ledger matches what's resting on the book.
    None on non-placed outcomes.
    """
    status: Literal["placed", "rejected", "error"]
    order_id: str | None
    error: str | None
    placed_size: float | None = None


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
        token_id: str, condition_id: str, limit_price: float,
    ) -> FillResult: ...
    def submit_post_only(
        self, side: str, size: float, price: float,
        token_id: str, condition_id: str, expiration: int,
    ) -> PlaceResult: ...
    def cancel_order(self, order_id: str) -> bool: ...
    def get_usdc_balance(self) -> float: ...
    def get_settlement_info(self, order_id: str) -> SettlementInfo: ...
    def get_order_status(self, order_id: str) -> dict: ...
    def get_order_book(self, token_id: str) -> dict: ...


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

    Two recovery paths:
    - reserved/open trades: query /order status; promote or demote based on
      matched / canceled / unmatched.
    - rejected trades with a lingering external_order_id: stranded-fill
      sweep. If /trades shows shares actually matched (despite the local
      rejected status — usually from a crash between post and settle, or a
      settlement-lookup race), promote the trade to open with corrected
      size / entry_price so the window can settle normally.
    """
    current_status = trade["status"]
    order_id = trade.get("external_order_id")
    if not order_id or client is None:
        return current_status

    if current_status == "rejected":
        # Stranded-fill sweep: if anything actually matched on-chain, the
        # local 'rejected' row is wrong and we need to resume the position.
        try:
            info = client.get_settlement_info(order_id)
        except Exception as exc:
            log.warning(
                "reconcile: get_settlement_info failed for rejected trade %s "
                "order %s: %s",
                trade["id"], order_id, exc,
            )
            return current_status
        if info is None or info.shares_held <= 0:
            return current_status  # truly rejected — nothing stranded
        avg_price = (
            info.cost_usdc / info.shares_held
            if info.shares_held > 0
            else trade["entry_price"]
        )
        log.error(
            "reconcile: STRANDED FILL — trade %s order %s has %.4f shares "
            "@ $%.4f on-chain; promoting rejected → open",
            trade["id"], order_id, info.shares_held, avg_price,
        )
        update_trade(
            db_path, trade["id"], outcome=None, pnl=None, status="open",
            size=info.shares_held, entry_price=avg_price,
        )
        # update_trade's error column is COALESCE-preserved; clear the stale
        # 'gtc-no-fill' / 'settlement-lookup' text so the promoted row isn't
        # confusing on post-mortem.
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "UPDATE trades SET error = NULL WHERE id = ?", (trade["id"],),
            )
            conn.commit()
        log_order_event(
            db_path, trade["id"], trade["window_slug"], "stranded_fill_promote",
            time.time(),
            payload={
                "from_status": "rejected",
                "shares_held": info.shares_held,
                "cost_usdc": info.cost_usdc,
                "avg_price": avg_price,
            },
            external_order_id=order_id,
        )
        return "open"

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
        log_order_event(
            db_path, trade["id"], trade["window_slug"], "reconcile_matched",
            time.time(),
            payload={"from_status": current_status, "clob_status": clob_status},
            external_order_id=order_id,
        )
        return "open"

    if clob_status in {"canceled", "cancelled", "unmatched"}:
        update_trade(db_path, trade["id"], outcome=None, pnl=None, status="rejected")
        log_order_event(
            db_path, trade["id"], trade["window_slug"], "reconcile_canceled",
            time.time(),
            payload={"from_status": current_status, "clob_status": clob_status},
            external_order_id=order_id,
        )
        return "rejected"

    if clob_status == "live":
        # A post-only resting order from a previous run is still on the
        # book. Window context is gone; cancel-and-reconcile inline so the
        # order doesn't fill into the new window.
        log_order_event(
            db_path, trade["id"], trade["window_slug"], "reconcile_live",
            time.time(),
            payload={"from_status": current_status, "clob_status": clob_status},
            external_order_id=order_id,
        )
        return cancel_post_only_order(db_path, trade, client, trigger="crash-recovery")

    log.warning(
        "reconcile: unexpected CLOB status %r for trade %s order %s; keeping local %r",
        clob_status, trade["id"], order_id, current_status,
    )
    log_order_event(
        db_path, trade["id"], trade["window_slug"], "reconcile_unknown",
        time.time(),
        # str() guards against non-serializable raw values (e.g., Mock in tests)
        # while still preserving the post-mortem signal of what CLOB returned.
        payload={"from_status": current_status, "clob_status_raw": str(resp.get("status"))},
        external_order_id=order_id,
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
            signal_reference_price=signal.signal_reference_price,
            signal_reference_source="live",
        )
    except sqlite3.IntegrityError:
        consumed = _window_consumed_result(db_path, window_slug)
        if consumed.trade_id is not None:
            return consumed
        raise

    deduct_paper_balance(db_path, cost)

    if outcome is not None:
        credit_paper_balance(db_path, payout)

    # G4: paper path writes exactly one `fill` event per successfully-logged
    # trade. No submit/ack — there is no external client call to timestamp.
    # Records the entry even when the trade immediately settles (outcome set);
    # settlement lives on the trades row.
    log_order_event(
        db_path, trade_id, window_slug, "fill", time.time(),
        payload={"filled_size": size, "avg_price": entry_price},
    )

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


def _book_top_n(book: dict, n: int = 3) -> dict:
    """Compress a /book response to top-N levels on each side, plus timestamp.

    The v2 /book endpoint returns dicts of stringified prices/sizes already
    sorted best-first per side. We keep just the head — full depth would
    bloat order_events payloads and we only need the top for the adverse-
    selection diagnostic.
    """
    if not isinstance(book, dict):
        return {}
    return {
        "bids": (book.get("bids") or [])[:n],
        "asks": (book.get("asks") or [])[:n],
        "timestamp": book.get("timestamp"),
        "hash": book.get("hash"),
    }


def execute_live_trade(
    db_path: str,
    signal: Signal,
    entry_price: float,
    size: float,
    window_slug: str,
    token_id: str,
    condition_id: str,
    client: LiveOrderClient,
    limit_price: float,
    submit_book_age_s_monotonic: float | None = None,
    opposite_token_id: str | None = None,
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
            signal_reference_price=signal.signal_reference_price,
            signal_reference_source="live",
        )
    except sqlite3.IntegrityError:
        consumed = _window_consumed_result(db_path, window_slug)
        if consumed.trade_id is not None:
            return consumed
        raise

    # G4: order lifecycle telemetry. `submit` is written before the client
    # call; `ack` immediately after; then one of `fill` or `reject` based on
    # fill.status. The book itself is NOT re-serialized here — it's already
    # captured in the concurrent `decision` snapshot; only book age travels.
    log_order_event(
        db_path, trade_id, window_slug, "submit", time.time(),
        payload={
            "side": signal.side,
            "intended_size": size,
            "entry_price": entry_price,
            "limit_price": limit_price,
            "book_age_s_monotonic": submit_book_age_s_monotonic,
        },
    )
    fill = client.submit_ioc(
        side=signal.side,
        price=entry_price,
        size=size,
        token_id=token_id,
        condition_id=condition_id,
        limit_price=limit_price,
    )
    # Ack-time book snapshot: best-effort fresh REST read of both tokens'
    # books so analyses can measure book drift across the submit→ack window
    # (the asyncio loop is blocked during submit_ioc, so the bot's locally
    # cached book can't update). Any fetch failure is swallowed — this is a
    # diagnostic, never a trade blocker.
    book_at_ack: dict | None = None
    book_fetched_at_wall: float | None = None
    try:
        side_book = client.get_order_book(token_id)
        opp_book = (
            client.get_order_book(opposite_token_id)
            if opposite_token_id else {}
        )
        book_fetched_at_wall = time.time()
        if signal.side == "up":
            book_at_ack = {
                "up_book": _book_top_n(side_book),
                "down_book": _book_top_n(opp_book),
            }
        else:
            book_at_ack = {
                "up_book": _book_top_n(opp_book),
                "down_book": _book_top_n(side_book),
            }
    except Exception as exc:
        log.warning("ack-time book snapshot failed: %s", exc)
    ack_payload: dict = {"status": fill.status, "error": fill.error}
    if book_at_ack is not None:
        ack_payload["book_at_ack"] = book_at_ack
        ack_payload["book_fetched_at_wall"] = book_fetched_at_wall
    log_order_event(
        db_path, trade_id, window_slug, "ack", time.time(),
        payload=ack_payload,
        external_order_id=fill.order_id,
    )

    if fill.status == "filled":
        update_trade(
            db_path, trade_id, outcome=None, pnl=None, status="open",
            external_order_id=fill.order_id,
            size=fill.filled_size,
            entry_price=fill.avg_price,
        )
        log_order_event(
            db_path, trade_id, window_slug, "fill", time.time(),
            payload={"filled_size": fill.filled_size, "avg_price": fill.avg_price},
            external_order_id=fill.order_id,
        )
        notional = fill.filled_size * (fill.avg_price or 0.0)
        if notional < MIN_POSITION_USDC * 0.25:
            log.warning(
                "dust-fill %s: filled=%.4f @ $%.4f = $%.4f < floor=$%.4f",
                window_slug, fill.filled_size, fill.avg_price or 0.0,
                notional, MIN_POSITION_USDC * 0.25,
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
        external_order_id=fill.order_id,
        error=fill.error,
    )
    log_order_event(
        db_path, trade_id, window_slug, "reject", time.time(),
        payload={"error": fill.error, "clob_status": fill.status},
        external_order_id=fill.order_id,
    )
    log.warning(
        "Live reject/error: %s %s @%.4f x%.2f: %s",
        window_slug, signal.side, entry_price, size, fill.error,
    )
    return TradeResult(success=False, trade_id=trade_id, error=fill.error)


def execute_live_trade_post_only(
    db_path: str,
    signal: Signal,
    intended_size: float,
    window_slug: str,
    token_id: str,
    condition_id: str,
    client: LiveOrderClient,
    *,
    up_bids: list[dict] | None,
    down_bids: list[dict] | None,
    offset_ticks: int,
    expiration: int,
    submit_book_age_s_monotonic: float | None = None,
) -> TradeResult:
    """Place a post-only resting maker order at pmc - offset_ticks.

    Computes rest_price inside this function from the bids passed by the
    caller — the place-time recompute step. (A future enhancement may
    refresh the bids via client.get_order_book here for even-fresher pmc;
    out of v1 scope.) Returns None / 'no-pair-merge-counterparty' if no
    opposite-side bid is available.

    On successful placement, the trade row sits at status='placed' with
    size=intended_size and entry_price=rest_price (intended values). The
    realized fill is reconciled when cancel_post_only_order runs at
    window-close / crash-recovery.
    """
    from polypocket.clients.polymarket import post_only_rest_price

    existing_trade = find_trade_by_window_slug(db_path, window_slug)
    if existing_trade is not None:
        return _window_consumed_result(db_path, window_slug)

    rest_price = post_only_rest_price(signal.side, up_bids, down_bids, offset_ticks)
    if rest_price is None:
        return TradeResult(success=False, error="no-pair-merge-counterparty")

    usdc_needed = rest_price * intended_size
    if client.get_usdc_balance() < usdc_needed:
        return TradeResult(success=False, error="insufficient-balance")

    fee_sh = fee_shares(intended_size, rest_price)
    try:
        trade_id = log_trade(
            db_path=db_path,
            window_slug=window_slug,
            side=signal.side,
            entry_price=rest_price,
            size=intended_size,
            fees=fee_sh,
            model_p_up=signal.model_p_up,
            market_p_up=signal.market_price,
            edge=signal.edge,
            outcome=None,
            pnl=None,
            status="placed",
            signal_reference_price=signal.signal_reference_price,
            signal_reference_source="live",
            entry_mode="post_only",
            rest_price=rest_price,
        )
    except sqlite3.IntegrityError:
        consumed = _window_consumed_result(db_path, window_slug)
        if consumed.trade_id is not None:
            return consumed
        raise

    log_order_event(
        db_path, trade_id, window_slug, "place", time.time(),
        payload={
            "side": signal.side,
            "intended_size": intended_size,
            "rest_price": rest_price,
            "offset_ticks": offset_ticks,
            "expiration": expiration,
            "signal_reference_price": signal.signal_reference_price,
            "book_age_s_monotonic": submit_book_age_s_monotonic,
        },
    )
    place = client.submit_post_only(
        side=signal.side, size=intended_size, price=rest_price,
        token_id=token_id, condition_id=condition_id, expiration=expiration,
    )
    log_order_event(
        db_path, trade_id, window_slug, "ack", time.time(),
        payload={"status": place.status, "error": place.error},
        external_order_id=place.order_id,
    )

    if place.status == "rejected":
        update_trade(
            db_path, trade_id, outcome=None, pnl=None, status="rejected",
            external_order_id=place.order_id, error=place.error,
        )
        log_order_event(
            db_path, trade_id, window_slug, "reject", time.time(),
            payload={"error": place.error},
            external_order_id=place.order_id,
        )
        log.warning(
            "post-only reject: %s %s @%.4f x%.2f: %s",
            window_slug, signal.side, rest_price, intended_size, place.error,
        )
        return TradeResult(success=False, trade_id=trade_id, error=place.error)

    if place.status == "error":
        update_trade(
            db_path, trade_id, outcome=None, pnl=None, status="rejected",
            external_order_id=place.order_id, error=place.error,
        )
        return TradeResult(success=False, trade_id=trade_id, error=place.error)

    # status == "placed". The SDK wrapper may have quantized size to a
    # tick-safe integer that differs from intended_size by ±6 — overwrite
    # the trade row's size with the actual placed size so the local ledger
    # matches what's resting on the book. Falls back to intended_size if
    # the wrapper didn't surface a placed_size (e.g. test doubles).
    effective_size = place.placed_size if place.placed_size is not None else intended_size
    update_trade(
        db_path, trade_id, outcome=None, pnl=None, status="placed",
        external_order_id=place.order_id,
        size=effective_size,
    )
    log.info(
        "post-only PLACED: %s %s rest=$%.4f x%.2f exp=%d token=%s order=%s",
        window_slug, signal.side, rest_price, effective_size,
        expiration, token_id, place.order_id,
    )
    return TradeResult(success=True, trade_id=trade_id, pnl=None)


def cancel_post_only_order(
    db_path: str,
    trade: dict,
    client: LiveOrderClient,
    trigger: str,
) -> str:
    """Cancel a resting post-only order and reconcile via /trades.

    `get_settlement_info` is called post-cancel regardless of cancel
    success — even on a cancel race where partial fills landed in the
    submit→cancel window, the CLOB state is authoritative.

    Returns final local status: 'open' (partial-or-full fill, settles
    later at window close) or 'rejected' (no fill, post-only-no-fill).
    On settlement-lookup failure, preserves the prior status so a future
    reconciler pass can retry.
    """
    trade_id = trade["id"]
    window_slug = trade["window_slug"]
    order_id = trade.get("external_order_id")
    if not order_id:
        log.warning("cancel_post_only_order: no order_id on trade %s", trade_id)
        return trade.get("status", "placed")

    log_order_event(
        db_path, trade_id, window_slug, "cancel", time.time(),
        payload={"trigger": trigger, "phase": "request"},
        external_order_id=order_id,
    )
    cancel_ok = False
    try:
        cancel_ok = client.cancel_order(order_id)
    except Exception as exc:
        log.warning(
            "cancel_post_only_order: cancel_order raised for %s: %s", order_id, exc,
        )

    try:
        info = client.get_settlement_info(order_id)
    except Exception as exc:
        log.exception(
            "cancel_post_only_order: get_settlement_info failed for %s: %s",
            order_id, exc,
        )
        log_order_event(
            db_path, trade_id, window_slug, "cancel", time.time(),
            payload={
                "trigger": trigger, "phase": "ack",
                "cancel_success": cancel_ok,
                "settlement_lookup_error": str(exc),
            },
            external_order_id=order_id,
        )
        return trade.get("status", "placed")

    log_order_event(
        db_path, trade_id, window_slug, "cancel", time.time(),
        payload={
            "trigger": trigger, "phase": "ack",
            "cancel_success": cancel_ok,
            "shares_held": info.shares_held,
            "cost_usdc": info.cost_usdc,
        },
        external_order_id=order_id,
    )

    if info.shares_held > 0:
        avg_price = info.cost_usdc / info.shares_held
        update_trade(
            db_path, trade_id, outcome=None, pnl=None, status="open",
            size=info.shares_held, entry_price=avg_price,
        )
        # update_trade's error column is COALESCE-preserved; clear stale
        # error text from earlier transitions so the promoted-to-open row
        # reads cleanly on post-mortem. Mirrors reconcile_recovered_trade's
        # stranded-fill branch.
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "UPDATE trades SET error = NULL WHERE id = ?", (trade_id,),
            )
            conn.commit()
        notional = info.shares_held * avg_price
        if notional < MIN_POSITION_USDC * 0.25:
            log.warning(
                "post-only dust-fill %s: %.4f @ $%.4f = $%.4f < floor=$%.4f",
                window_slug, info.shares_held, avg_price, notional,
                MIN_POSITION_USDC * 0.25,
            )
        log_order_event(
            db_path, trade_id, window_slug, "fill", time.time(),
            payload={
                "filled_size": info.shares_held, "avg_price": avg_price,
                "via": "cancel-reconcile",
            },
            external_order_id=order_id,
        )
        log.info(
            "post-only FILLED via cancel-reconcile: %s %s %.4f @ $%.4f",
            window_slug, trade["side"], info.shares_held, avg_price,
        )
        return "open"

    update_trade(
        db_path, trade_id, outcome=None, pnl=None, status="rejected",
        error="post-only-no-fill",
    )
    log_order_event(
        db_path, trade_id, window_slug, "reject", time.time(),
        payload={"error": "post-only-no-fill"},
        external_order_id=order_id,
    )
    log.info(
        "post-only NO-FILL: %s order=%s trigger=%s",
        window_slug, order_id, trigger,
    )
    return "rejected"


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
