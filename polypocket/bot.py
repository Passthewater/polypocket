"""Main bot orchestrator: connects feeds, evaluates signals, executes trades."""

import asyncio
import logging
import time

from polypocket.config import (
    CALIBRATION_SHRINKAGE_DOWN,
    CALIBRATION_SHRINKAGE_UP,
    DEPTH_CLAMP_BUFFER,
    EDGE_FLOOR,
    EDGE_RANGE,
    MAX_BOOK_AGE_S,
    MAX_POSITION_USDC,
    MIN_FILL_RATIO,
    MIN_POSITION_USDC,
    PAPER_DB_PATH,
    TRADING_MODE,
    VOL_FLOOR,
    VOL_RANGE,
    VOLATILITY_LOOKBACK,
    effective_ask,
)
from polypocket.clients.polymarket import fok_limit_price
from polypocket.executor import (
    LiveOrderClient,
    TradeResult,
    execute_live_trade,
    execute_paper_trade,
    reconcile_recovered_trade,
    settle_live_trade,
    settle_paper_trade,
)
from polypocket.feeds.binance import BinanceFeed
from polypocket.feeds.polymarket import (
    Window,
    fetch_active_windows,
    fetch_price_to_beat,
    fetch_resolution,
    subscribe_and_stream,
)
from polypocket.ledger import find_trade_by_window_slug, find_unsettled_trades, init_db, log_snapshot
from polypocket.observer import calibrate_p_up, compute_model_p_up, compute_realized_vol
from polypocket.quotes import QuoteSnapshot, validate_quote
from polypocket.risk import RiskManager
from polypocket.signal import SignalEngine

log = logging.getLogger(__name__)


class Bot:
    def __init__(
        self,
        db_path: str = PAPER_DB_PATH,
        live_order_client: LiveOrderClient | None = None,
    ):
        self.db_path = db_path
        self.live_order_client = live_order_client
        self.binance = BinanceFeed()
        self.signal_engine = SignalEngine()
        self.risk = RiskManager(db_path=db_path)
        self.stop = asyncio.Event()

        self._current_window_id: str | None = None
        self._current_window: Window | None = None
        self._window_traded = False
        self._open_trade: dict | None = None
        self._ptb_last_fetch: float = 0.0
        self._ptb_provisional: bool = False
        self._resolution_last_fetch: float = 0.0
        self._open_snapshot_emitted = False
        self._best_edge_abs: float = 0.0
        self._best_edge_snapshot: dict | None = None
        self._window_skip_reason: str | None = None
        # Trades from past windows awaiting resolution
        self._pending_settlements: list[dict] = []

        self.stats = {
            "btc_price": None,
            "window_open_price": None,
            "displacement": None,
            "model_p_up": None,
            "model_p_up_calibrated": None,
            "market_p_up": None,
            "edge": None,
            "preview_side": None,
            "preview_market_price": None,
            "sigma_5min": None,
            "t_remaining": None,
            "up_ask": None,
            "down_ask": None,
            "quote_status": None,
            "execution_status": None,
            "window_slug": None,
            "position": None,
        }

        self.on_trade = None
        self.on_stats_update = None

    def _format_position(self, trade: dict) -> str:
        position = f'{trade["size"]:.1f} {trade["side"].upper()} @ ${trade["entry_price"]:.3f}'
        if trade.get("mode") == "live" and trade.get("status") == "reserved":
            return f"{position} (reserved)"
        return position

    async def _on_book_update(self, window: Window, side: str) -> None:
        del side
        if self.binance.latest_price is None:
            return

        # Resolve pending trades from previous windows in the background
        await self._poll_pending_settlements()

        now = time.time()

        # Transition detection. The feed pre-subscribes to BOTH the current
        # and next slots, so we see alternating book events for two windows.
        # To avoid oscillating state, only advance off the current window
        # when either (a) we have no current window yet, (b) the incoming
        # event is for a window that is currently live, or (c) the tracked
        # current window has already expired. Mere receipt of a next-slot
        # book event while the current window is still live is ignored.
        is_first_window = self._current_window_id is None
        incoming_is_live = window.start_time <= now < window.end_time
        current_expired = (
            self._current_window is not None
            and self._current_window.end_time <= now
        )
        if self._current_window_id != window.condition_id and (
            is_first_window or incoming_is_live or current_expired
        ):
            # Flush previous window's skip decision snapshot before settling/resetting
            if self._current_window is not None:
                prev_slug = self._current_window.slug
                if not self._window_traded and self._best_edge_snapshot is not None:
                    log_snapshot(
                        self.db_path,
                        window_slug=prev_slug,
                        snapshot_type="decision",
                        stats=self._best_edge_snapshot,
                        trade_fired=False,
                        skip_reason=self._window_skip_reason or "no-edge",
                    )

            if self._open_trade and self._current_window is not None:
                await self._settle_previous_window(self._current_window)

            self._current_window_id = window.condition_id
            self._current_window = window
            self._window_traded = False
            self._open_trade = None
            self._open_snapshot_emitted = False
            self._best_edge_abs = 0.0
            self._best_edge_snapshot = None
            self._window_skip_reason = None
            self.stats["position"] = None
            self.stats["execution_status"] = None

            recovered_trade = find_trade_by_window_slug(self.db_path, window.slug)
            recoverable_statuses = {"open"}
            if TRADING_MODE == "live":
                recoverable_statuses.add("reserved")
            if recovered_trade is not None and recovered_trade["status"] in recoverable_statuses:
                final_status = recovered_trade["status"]
                if TRADING_MODE == "live" and recovered_trade.get("external_order_id"):
                    final_status = reconcile_recovered_trade(
                        self.db_path, recovered_trade, self.live_order_client,
                    )

                self._window_traded = True
                # Remove from pending list to avoid double settlement
                self._pending_settlements = [
                    p for p in self._pending_settlements
                    if p["trade_id"] != recovered_trade["id"]
                ]

                if final_status == "rejected":
                    # CLOB says the order never matched — window consumed, no position.
                    self.stats["execution_status"] = "rejected-on-recovery"
                    self._open_trade = None
                else:
                    self.stats["execution_status"] = "recovery"
                    self._open_trade = {
                        "trade_id": recovered_trade["id"],
                        "side": recovered_trade["side"],
                        "entry_price": recovered_trade["entry_price"],
                        "size": recovered_trade["size"],
                        "mode": TRADING_MODE,
                        "status": "open",
                        "external_order_id": recovered_trade.get("external_order_id"),
                    }
                    self.stats["position"] = self._format_position(self._open_trade)

            if window.price_to_beat is not None:
                self._ptb_provisional = False
                log.info(
                    "New window: %s priceToBeat=%.6f (Binance=%.2f)",
                    window.slug,
                    window.price_to_beat,
                    self.binance.latest_price,
                )
            else:
                # Use Binance price at window start as provisional anchor
                # until Chainlink reports.  price_at() looks up the high-res
                # buffer; falls back to latest_price if the buffer doesn't
                # reach back far enough (bot just started).
                hist_price = self.binance.price_at(window.start_time)
                window.price_to_beat = hist_price if hist_price is not None else self.binance.latest_price
                self._ptb_provisional = True
                log.info(
                    "New window: %s priceToBeat=PENDING, provisional Binance=%.2f (%s)",
                    window.slug,
                    window.price_to_beat,
                    "historical" if hist_price is not None else "latest",
                )
            self._ptb_last_fetch = 0.0

        # If the current window has expired and we're still holding an open
        # trade, settle it now — regardless of whether this book event is
        # for the live slot. Pre-subscribed next-slot events frequently
        # arrive before any post-expiry event for the old slot.
        if (
            self._open_trade is not None
            and self._current_window is not None
            and self._current_window.end_time <= now
        ):
            outcome = await fetch_resolution(self._current_window.slug)
            if outcome is not None:
                log.info(
                    "Official resolution for %s: %s",
                    self._current_window.slug,
                    outcome.upper(),
                )
                await self._settle_trade(outcome)
            else:
                self._pending_settlements.append({
                    **self._open_trade,
                    "window_slug": self._current_window.slug,
                })
                self._open_trade = None
                self.stats["position"] = None
                self.stats["execution_status"] = None
                log.info(
                    "Parked trade for expired window %s, awaiting resolution",
                    self._current_window.slug,
                )
                if self.on_stats_update:
                    self.on_stats_update(self.stats)

        # Live-only: stats updates, signal evaluation, and mid-window
        # snapshot emission apply only when the incoming event is for the
        # window currently in its live slot. Pre-subscribed next-slot events
        # carry warm asks on the Window object but shouldn't overwrite the
        # live window's stats or trigger signal evaluation.
        if not (window.start_time <= now < window.end_time):
            return

        t_remaining = window.end_time - now
        t_elapsed = now - window.start_time

        # Keep trying to resolve the official Chainlink priceToBeat
        if self._ptb_provisional:
            now_mono = time.time()
            if now_mono - self._ptb_last_fetch >= 3.0:
                self._ptb_last_fetch = now_mono
                ptb = await fetch_price_to_beat(window.slug)
                if ptb is not None:
                    window.price_to_beat = ptb
                    self._ptb_provisional = False
                    log.info(
                        "Resolved official priceToBeat for %s: %.6f",
                        window.slug,
                        ptb,
                    )

        displacement = (self.binance.latest_price - window.price_to_beat) / window.price_to_beat
        sigma = compute_realized_vol(self.binance.get_5min_returns(), VOLATILITY_LOOKBACK)
        if sigma <= 0:
            sigma = 0.001

        model_p_up = compute_model_p_up(displacement, max(t_remaining, 0), sigma)
        model_p_up_cal = calibrate_p_up(
            model_p_up,
            up_factor=CALIBRATION_SHRINKAGE_UP,
            down_factor=CALIBRATION_SHRINKAGE_DOWN,
        )
        up_edge = None if window.up_ask is None else model_p_up_cal - effective_ask(window.up_ask)
        down_edge = None if window.down_ask is None else (1 - model_p_up_cal) - effective_ask(window.down_ask)
        preview_edge = None
        preview_side = None
        preview_market_price = None
        if up_edge is not None or down_edge is not None:
            if up_edge is not None and (down_edge is None or up_edge >= down_edge):
                preview_edge = up_edge
                preview_side = "up"
                preview_market_price = window.up_ask
            elif down_edge is not None:
                preview_edge = down_edge
                preview_side = "down"
                preview_market_price = window.down_ask
        quote_validation = validate_quote(
            QuoteSnapshot(up_ask=window.up_ask, down_ask=window.down_ask)
        )
        quote_status = quote_validation.reason if not quote_validation.valid else "valid"
        self.stats.update(
            {
                "btc_price": self.binance.latest_price,
                "window_open_price": window.price_to_beat,
                "ptb_provisional": self._ptb_provisional,
                "displacement": displacement,
                "model_p_up": model_p_up,
                "model_p_up_calibrated": model_p_up_cal,
                "market_p_up": window.up_ask,
                "edge": preview_edge,
                "preview_side": preview_side,
                "preview_market_price": preview_market_price,
                "sigma_5min": sigma,
                "t_remaining": t_remaining,
                "up_ask": window.up_ask,
                "down_ask": window.down_ask,
                "quote_status": quote_status,
                "window_slug": window.slug,
            }
        )
        if self.on_stats_update:
            self.on_stats_update(self.stats)

        if not self._open_snapshot_emitted and self.stats["up_ask"] is not None and self.stats["down_ask"] is not None:
            self._open_snapshot_emitted = True
            book_depth = None
            if window.up_book or window.down_book:
                book_depth = {"up": window.up_book, "down": window.down_book}
            log_snapshot(
                self.db_path,
                window_slug=window.slug,
                snapshot_type="open",
                stats=self.stats,
                book_depth=book_depth,
            )

        current_edge_abs = abs(self.stats.get("edge") or 0.0)
        if current_edge_abs > self._best_edge_abs:
            self._best_edge_abs = current_edge_abs
            self._best_edge_snapshot = dict(self.stats)

        if self._window_traded:
            return

        self.stats["execution_status"] = None
        if not quote_validation.valid:
            self.stats["execution_status"] = "skipped"
            if self.on_stats_update:
                self.on_stats_update(self.stats)
            return

        signal = self.signal_engine.evaluate(
            displacement=displacement,
            t_elapsed=t_elapsed,
            t_remaining=t_remaining,
            sigma_5min=sigma,
            up_ask=window.up_ask,
            down_ask=window.down_ask,
        )
        if signal is None:
            if not self._window_traded and self._window_skip_reason is None:
                self._window_skip_reason = "no-edge"
            return

        if TRADING_MODE != "paper":
            ok, reason = self.risk.check()
            if not ok:
                log.warning("Risk blocked: %s", reason)
                if self._window_skip_reason is None:
                    self._window_skip_reason = "risk-blocked"
                return

        entry_price = window.up_ask if signal.side == "up" else window.down_ask
        if entry_price is None:
            return

        edge_scale = min(max((signal.edge - EDGE_FLOOR) / EDGE_RANGE, 0.0), 1.0)
        vol_scale = min(max((sigma - VOL_FLOOR) / VOL_RANGE, 0.0), 1.0)
        size_usdc = MIN_POSITION_USDC + (edge_scale * vol_scale) * (MAX_POSITION_USDC - MIN_POSITION_USDC)

        # Live-only preflight: clamp size_usdc to available balance (minus a
        # 2% buffer for fees). Only skip when we can't even afford the floor.
        if TRADING_MODE != "paper" and self.live_order_client is not None:
            balance = self.live_order_client.get_usdc_balance()
            max_affordable = balance * 0.98
            if max_affordable < MIN_POSITION_USDC:
                self._window_skip_reason = "insufficient-balance"
                self.stats["execution_status"] = "no-balance"
                log.error(
                    "Insufficient USDC ($%.2f available, need ≥$%.2f) — skipping window %s",
                    balance, MIN_POSITION_USDC, window.slug,
                )
                return
            if max_affordable < size_usdc:
                log.info(
                    "Downsizing trade: balance=$%.2f, clamping $%.2f → $%.2f",
                    balance, size_usdc, max_affordable,
                )
                size_usdc = max_affordable

        size = size_usdc / entry_price

        # Staleness gate: refuse to trade on a book that hasn't ticked recently.
        # Covers WS-reconnect gaps and silent stalls — without a fixed grace
        # window that could expire mid-reconnect.
        if TRADING_MODE != "paper":
            age_ok = (
                window.book_updated_at is not None
                and time.monotonic() - window.book_updated_at <= MAX_BOOK_AGE_S
            )
            if not age_ok:
                self._window_skip_reason = "book-stale"
                log.warning(
                    "Skipping signal: book age exceeds %.1fs (updated_at=%s)",
                    MAX_BOOK_AGE_S, window.book_updated_at,
                )
                return

            # Depth clamp: ask for at most DEPTH_CLAMP_BUFFER of the
            # cumulative ask size at <= FOK limit. Under IOC semantics the
            # realized fill can be anywhere between 0 and target_size; skip
            # only if even a MIN_FILL_RATIO slice of visible depth cannot
            # clear MIN_POSITION_USDC (guarantees any non-skipped trade's
            # worst-acceptable partial is above the dust floor).
            intended_size_pre_clamp = size  # preserve for diagnostic log
            book = window.up_book if signal.side == "up" else window.down_book
            limit = fok_limit_price(entry_price)
            fillable = sum(
                lvl["size"] for lvl in (book or []) if lvl["price"] <= limit + 1e-9
            )

            floor_usdc = MIN_POSITION_USDC
            if fillable * entry_price * MIN_FILL_RATIO < floor_usdc:
                self._window_skip_reason = "book-too-thin"
                log.warning(
                    "Skipping signal: book too thin — fillable=%.2f @ <=$%.2f, "
                    "min_slice_value=$%.2f < floor=$%.2f",
                    fillable, limit, fillable * entry_price * MIN_FILL_RATIO, floor_usdc,
                )
                return

            target_size = min(size, fillable * DEPTH_CLAMP_BUFFER)
            if target_size < size:
                log.info(
                    "Downsizing trade to depth: intended=%.2f target=%.2f "
                    "fillable=%.2f limit=$%.2f",
                    size, target_size, fillable, limit,
                )
                size = target_size
                size_usdc = target_size * entry_price

        raw_str = (
            f"raw={signal.model_p_up_raw * 100:.1f}%" if signal.model_p_up_raw is not None else "raw=n/a"
        )
        log.info(
            "SIGNAL: %s edge=%.1f%% (model=%.1f%% %s mkt=%.1f%%) -> %s @ $%.3f x%.1f ($%.1f)",
            signal.side.upper(),
            signal.edge * 100,
            signal.model_p_up * 100,
            raw_str,
            signal.market_price * 100,
            signal.side,
            entry_price,
            size,
            size_usdc,
        )

        book_depth = None
        if window.up_book or window.down_book:
            book_depth = {"up": window.up_book, "down": window.down_book}
        log_snapshot(
            self.db_path,
            window_slug=window.slug,
            snapshot_type="decision",
            stats=self.stats,
            book_depth=book_depth,
            trade_fired=True,
        )

        if TRADING_MODE == "paper":
            result = execute_paper_trade(
                db_path=self.db_path,
                signal=signal,
                entry_price=entry_price,
                size=size,
                window_slug=window.slug,
            )
        else:
            if self.live_order_client is None:
                raise RuntimeError("live_order_client is required for live trading mode")
            token_id = window.up_token_id if signal.side == "up" else window.down_token_id
            result = execute_live_trade(
                db_path=self.db_path,
                signal=signal,
                entry_price=entry_price,
                size=size,
                window_slug=window.slug,
                token_id=token_id,
                condition_id=window.condition_id,
                client=self.live_order_client,
            )

        if not result.success and result.error == "window-already-consumed":
            self._window_traded = True
            self.stats["execution_status"] = "consumed"
            if self.on_stats_update:
                self.on_stats_update(self.stats)
            return

        if not result.success and result.error == "insufficient-balance":
            log.error("Insufficient USDC — skipping window %s", window.slug)
            self._window_traded = True
            self.stats["execution_status"] = "no-balance"
            if self.on_stats_update:
                self.on_stats_update(self.stats)
            return

        if not result.success:
            # Reject / error path — trade row already flipped to 'rejected' by executor.
            log.warning("Live trade not opened: %s", result.error)
            self._window_traded = True
            self.stats["execution_status"] = f"rejected: {result.error}"
            if self.on_stats_update:
                self.on_stats_update(self.stats)
            return

        if result.success:
            self._window_traded = True
            external_order_id = None
            if TRADING_MODE != "paper":
                recorded = find_trade_by_window_slug(self.db_path, window.slug)
                if recorded is not None:
                    external_order_id = recorded.get("external_order_id")
            self._open_trade = {
                "trade_id": result.trade_id,
                "side": signal.side,
                "entry_price": entry_price,
                "size": size,
                "mode": TRADING_MODE,
                "status": "open",
                "external_order_id": external_order_id,
            }
            self.stats["position"] = self._format_position(self._open_trade)
            self.stats["execution_status"] = "open"
            if self.on_trade:
                self.on_trade(result, signal, window.slug)
            if self.on_stats_update:
                self.on_stats_update(self.stats)

    async def _settle_trade(self, outcome: str) -> None:
        if not self._open_trade:
            return

        trade = self._open_trade
        if trade.get("mode") == "live":
            pnl = settle_live_trade(
                self.db_path,
                trade["trade_id"],
                trade["side"],
                outcome,
                trade.get("external_order_id"),
                self.live_order_client,
            )
            if pnl is None or pnl <= 0:
                self.risk.record_loss()
            else:
                self.risk.record_win()
            log.info(
                "LIVE RESOLVED: %s -> trade %s pnl=%s",
                outcome.upper(),
                trade["trade_id"],
                f"${pnl:.2f}" if pnl is not None else "unreconciled",
            )
        else:
            pnl = settle_paper_trade(
                self.db_path,
                trade["trade_id"],
                trade["entry_price"],
                trade["size"],
                trade["side"],
                outcome,
            )
            if pnl > 0:
                self.risk.record_win()
            else:
                self.risk.record_loss()
            log.info("SETTLED: %s -> P&L $%.2f", outcome.upper(), pnl)
        log_snapshot(
            self.db_path,
            window_slug=self._current_window.slug if self._current_window else "unknown",
            snapshot_type="close",
            stats=self.stats,
            trade_fired=True,
            outcome=outcome,
        )
        self._open_trade = None
        self.stats["position"] = None
        if self.on_trade:
            self.on_trade(TradeResult(success=True, trade_id=trade["trade_id"], pnl=pnl), None, None)

    async def _settle_previous_window(self, prev_window: Window) -> None:
        """Move unresolved trade to pending list so the bot can advance."""
        if not self._open_trade:
            return
        # Try immediate resolution first
        outcome = await fetch_resolution(prev_window.slug)
        if outcome is not None:
            log.info(
                "Official resolution for previous window %s: %s",
                prev_window.slug,
                outcome.upper(),
            )
            await self._settle_trade(outcome)
        else:
            # Park trade in pending — it will be settled by background polling
            self._pending_settlements.append({
                **self._open_trade,
                "window_slug": prev_window.slug,
            })
            self._open_trade = None
            self.stats["position"] = None
            log.info(
                "Parked trade for %s in pending settlements, moving to next window",
                prev_window.slug,
            )

    async def _poll_pending_settlements(self) -> None:
        """Try to resolve all pending trades from past windows."""
        if not self._pending_settlements:
            return
        still_pending = []
        for trade in self._pending_settlements:
            outcome = await fetch_resolution(trade["window_slug"])
            if outcome is not None:
                log.info(
                    "Resolved pending trade %s: %s",
                    trade["window_slug"],
                    outcome.upper(),
                )
                if trade.get("mode") == "live":
                    pnl = settle_live_trade(
                        self.db_path,
                        trade["trade_id"],
                        trade["side"],
                        outcome,
                        trade.get("external_order_id"),
                        self.live_order_client,
                    )
                    if pnl is None or pnl <= 0:
                        self.risk.record_loss()
                    else:
                        self.risk.record_win()
                    if pnl is not None:
                        log.info("SETTLED pending (live): %s -> P&L $%.2f", outcome.upper(), pnl)
                    else:
                        log.info("SETTLED pending (live, unreconciled): %s", outcome.upper())
                else:
                    pnl = settle_paper_trade(
                        self.db_path,
                        trade["trade_id"],
                        trade["entry_price"],
                        trade["size"],
                        trade["side"],
                        outcome,
                    )
                    if pnl > 0:
                        self.risk.record_win()
                    else:
                        self.risk.record_loss()
                    log.info("SETTLED pending: %s -> P&L $%.2f", outcome.upper(), pnl)
                log_snapshot(
                    self.db_path,
                    window_slug=trade["window_slug"],
                    snapshot_type="close",
                    stats=self.stats,
                    trade_fired=True,
                    outcome=outcome,
                )
                if self.on_trade:
                    self.on_trade(
                        TradeResult(success=True, trade_id=trade["trade_id"], pnl=pnl),
                        None,
                        None,
                    )
            else:
                still_pending.append(trade)
        self._pending_settlements = still_pending

    async def run(self) -> None:
        init_db(self.db_path)
        log.info("Polypocket bot starting (mode=%s)", TRADING_MODE)

        # Recover unsettled trades from previous runs
        unsettled = find_unsettled_trades(self.db_path)
        for row in unsettled:
            self._pending_settlements.append({
                "trade_id": row["id"],
                "side": row["side"],
                "entry_price": row["entry_price"],
                "size": row["size"],
                "mode": TRADING_MODE,
                "status": row["status"],
                "window_slug": row["window_slug"],
                "external_order_id": row.get("external_order_id"),
            })
        if unsettled:
            log.info("Recovered %d unsettled trade(s) from database", len(unsettled))

        async def poll_and_stream():
            while not self.stop.is_set():
                try:
                    windows = await fetch_active_windows()
                except Exception as exc:
                    log.error("Failed to fetch windows: %s", exc)
                    windows = []
                if windows:
                    log.info("Tracking %d active windows", len(windows))
                    await subscribe_and_stream(windows, self._on_book_update, self.stop)
                else:
                    log.warning("No active 5-min BTC windows found, retrying in 10s...")
                await asyncio.sleep(10)

        try:
            await asyncio.gather(
                self.binance.run(self.stop),
                poll_and_stream(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            self.stop.set()
            log.info("Bot stopped.")
