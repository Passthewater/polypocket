"""Main bot orchestrator: connects feeds, evaluates signals, executes trades."""

import asyncio
import logging
import time

from polypocket.config import PAPER_DB_PATH, POSITION_SIZE_USDC, TRADING_MODE, VOLATILITY_LOOKBACK
from polypocket.executor import TradeResult, execute_paper_trade, settle_paper_trade
from polypocket.feeds.binance import BinanceFeed
from polypocket.feeds.polymarket import Window, fetch_active_windows, subscribe_and_stream
from polypocket.ledger import init_db
from polypocket.observer import compute_model_p_up, compute_realized_vol
from polypocket.risk import RiskManager
from polypocket.signal import SignalEngine

log = logging.getLogger(__name__)


class Bot:
    def __init__(self, db_path: str = PAPER_DB_PATH):
        self.db_path = db_path
        self.binance = BinanceFeed()
        self.signal_engine = SignalEngine()
        self.risk = RiskManager(db_path=db_path)
        self.stop = asyncio.Event()

        self._current_window_id: str | None = None
        self._current_window: Window | None = None
        self._window_traded = False
        self._open_trade: dict | None = None

        self.stats = {
            "btc_price": None,
            "window_open_price": None,
            "displacement": None,
            "model_p_up": None,
            "market_p_up": None,
            "edge": None,
            "sigma_5min": None,
            "t_remaining": None,
            "window_slug": None,
            "position": None,
        }

        self.on_trade = None
        self.on_stats_update = None

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

            # If priceToBeat missing (Chainlink delay), use Binance price as anchor
            if window.price_to_beat is None:
                window.price_to_beat = self.binance.latest_price
                log.info(
                    "New window: %s priceToBeat=PENDING, using Binance anchor=%.2f",
                    window.slug,
                    window.price_to_beat,
                )
            else:
                log.info(
                    "New window: %s priceToBeat=%.6f (Binance=%.2f)",
                    window.slug,
                    window.price_to_beat,
                    self.binance.latest_price,
                )

        displacement = (self.binance.latest_price - window.price_to_beat) / window.price_to_beat
        sigma = compute_realized_vol(self.binance.get_5min_returns(), VOLATILITY_LOOKBACK)
        if sigma <= 0:
            sigma = 0.001

        model_p_up = compute_model_p_up(displacement, max(t_remaining, 0), sigma)
        self.stats.update(
            {
                "btc_price": self.binance.latest_price,
                "window_open_price": window.price_to_beat,
                "displacement": displacement,
                "model_p_up": model_p_up,
                "market_p_up": window.up_ask,
                "edge": (model_p_up - window.up_ask) if window.up_ask is not None else None,
                "sigma_5min": sigma,
                "t_remaining": t_remaining,
                "window_slug": window.slug,
            }
        )
        if self.on_stats_update:
            self.on_stats_update(self.stats)

        if t_remaining <= 0:
            if self._open_trade:
                outcome = "up" if self.binance.latest_price >= window.price_to_beat else "down"
                await self._settle_trade(outcome)
            return

        if self._window_traded:
            return

        signal = self.signal_engine.evaluate(
            displacement=displacement,
            t_elapsed=t_elapsed,
            t_remaining=t_remaining,
            sigma_5min=sigma,
            market_p_up=window.up_ask,
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
            signal.market_p_up * 100,
            signal.side,
            entry_price,
            size,
        )

        result = execute_paper_trade(
            db_path=self.db_path,
            signal=signal,
            entry_price=entry_price,
            size=size,
            window_slug=window.slug,
        )
        if result.success:
            self._window_traded = True
            self._open_trade = {
                "trade_id": result.trade_id,
                "side": signal.side,
                "entry_price": entry_price,
                "size": size,
            }
            self.stats["position"] = f"{size:.1f} {signal.side.upper()} @ ${entry_price:.3f}"
            if self.on_trade:
                self.on_trade(result, signal, window.slug)

    async def _settle_trade(self, outcome: str) -> None:
        if not self._open_trade:
            return

        trade = self._open_trade
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
        outcome = "up" if self.binance.latest_price >= prev_window.price_to_beat else "down"
        await self._settle_trade(outcome)

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
