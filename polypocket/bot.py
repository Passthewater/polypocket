"""Main bot orchestrator: connects feeds, evaluates signals, executes trades."""

import asyncio
import logging
import time

from polypocket.config import FEE_RATE, PAPER_DB_PATH, POSITION_SIZE_USDC, TRADING_MODE, VOLATILITY_LOOKBACK
from polypocket.executor import (
    LiveOrderClient,
    TradeResult,
    execute_live_trade,
    execute_paper_trade,
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
from polypocket.ledger import find_trade_by_window_slug, init_db
from polypocket.observer import compute_model_p_up, compute_realized_vol
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

        self.stats = {
            "btc_price": None,
            "window_open_price": None,
            "displacement": None,
            "model_p_up": None,
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

        now = time.time()
        t_remaining = window.end_time - now
        t_elapsed = now - window.start_time

        if self._current_window_id != window.condition_id:
            if self._open_trade and self._current_window is not None:
                await self._settle_previous_window(self._current_window)

            self._current_window_id = window.condition_id
            self._current_window = window
            self._window_traded = False
            self._open_trade = None
            self.stats["position"] = None
            self.stats["execution_status"] = None

            recovered_trade = find_trade_by_window_slug(self.db_path, window.slug)
            recoverable_statuses = {"open"}
            if TRADING_MODE == "live":
                recoverable_statuses.add("reserved")
            if recovered_trade is not None and recovered_trade["status"] in recoverable_statuses:
                self._window_traded = True
                self.stats["execution_status"] = "recovery"
                self._open_trade = {
                    "trade_id": recovered_trade["id"],
                    "side": recovered_trade["side"],
                    "entry_price": recovered_trade["entry_price"],
                    "size": recovered_trade["size"],
                    "mode": TRADING_MODE,
                    "status": recovered_trade["status"],
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
                # Use Binance as provisional anchor until Chainlink reports.
                # Error is typically <0.05% — negligible for model probabilities.
                window.price_to_beat = self.binance.latest_price
                self._ptb_provisional = True
                log.info(
                    "New window: %s priceToBeat=PENDING, provisional Binance=%.2f",
                    window.slug,
                    window.price_to_beat,
                )
            self._ptb_last_fetch = 0.0

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
        up_edge = None if window.up_ask is None else model_p_up - (window.up_ask * (1 + FEE_RATE))
        down_edge = None if window.down_ask is None else (1 - model_p_up) - (window.down_ask * (1 + FEE_RATE))
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

        if t_remaining <= 0:
            if self._open_trade:
                now_mono = time.time()
                if now_mono - self._resolution_last_fetch >= 5.0:
                    self._resolution_last_fetch = now_mono
                    outcome = await fetch_resolution(window.slug)
                    if outcome is not None:
                        log.info(
                            "Official resolution for %s: %s",
                            window.slug,
                            outcome.upper(),
                        )
                        await self._settle_trade(outcome)
                    else:
                        self.stats["execution_status"] = "awaiting-resolution"
                        if self.on_stats_update:
                            self.on_stats_update(self.stats)
            return

        if self._window_traded:
            return

        self.stats["execution_status"] = None

        # Never trade on a provisional priceToBeat — the displacement is
        # meaningless until the real Chainlink anchor arrives.
        if self._ptb_provisional:
            self.stats["execution_status"] = "awaiting-ptb"
            if self.on_stats_update:
                self.on_stats_update(self.stats)
            return

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
            return

        ok, reason = self.risk.check()
        if not ok:
            log.warning("Risk blocked: %s", reason)
            return

        entry_price = window.up_ask if signal.side == "up" else window.down_ask
        if entry_price is None:
            return

        size = POSITION_SIZE_USDC / entry_price
        log.info(
            "SIGNAL: %s edge=%.1f%% (model=%.1f%% mkt=%.1f%%) -> %s @ $%.3f x%.1f",
            signal.side.upper(),
            signal.edge * 100,
            signal.model_p_up * 100,
            signal.market_price * 100,
            signal.side,
            entry_price,
            size,
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
            result = execute_live_trade(
                db_path=self.db_path,
                signal=signal,
                entry_price=entry_price,
                size=size,
                window_slug=window.slug,
                client=self.live_order_client,
            )
        if not result.success and result.error == "window-already-consumed":
            self._window_traded = True
            self.stats["execution_status"] = "consumed"
            if self.on_stats_update:
                self.on_stats_update(self.stats)
            return
        if result.success:
            self._window_traded = True
            self._open_trade = {
                "trade_id": result.trade_id,
                "side": signal.side,
                "entry_price": entry_price,
                "size": size,
                "mode": TRADING_MODE,
                "status": "open",
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
            settle_live_trade(self.db_path, trade["trade_id"], outcome)
            pnl = None
            log.info(
                "LIVE RESOLVED: %s -> trade %s marked settled pending reconciliation",
                outcome.upper(),
                trade["trade_id"],
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
        self._open_trade = None
        self.stats["position"] = None
        if self.on_trade:
            self.on_trade(TradeResult(success=True, trade_id=trade["trade_id"], pnl=pnl), None, None)

    async def _settle_previous_window(self, prev_window: Window) -> None:
        if not self._open_trade:
            return
        outcome = await fetch_resolution(prev_window.slug)
        if outcome is not None:
            log.info(
                "Official resolution for previous window %s: %s",
                prev_window.slug,
                outcome.upper(),
            )
            await self._settle_trade(outcome)
        else:
            # Resolution not yet available — keep the trade open,
            # it will be settled on the next book update tick.
            log.info(
                "Waiting for official resolution of %s before settling",
                prev_window.slug,
            )

    async def run(self) -> None:
        init_db(self.db_path)
        log.info("Polypocket bot starting (mode=%s)", TRADING_MODE)

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
