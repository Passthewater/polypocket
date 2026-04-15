"""Real-time BTC/USDT price feed via ccxt pro WebSocket."""

import asyncio
import logging
from collections import deque

import ccxt.pro as ccxtpro

log = logging.getLogger(__name__)

SNAPSHOT_INTERVAL_S = 300
# High-res buffer: one sample per second, last 10 minutes
HIRES_INTERVAL_S = 1.0
HIRES_MAX_AGE_S = 600.0


class BinanceFeed:
    """Streams BTC/USDT trades and stores periodic snapshots."""

    def __init__(self):
        self.latest_price: float | None = None
        self.latest_ts: float | None = None
        self.prices: list[dict[str, float]] = []
        self._last_snapshot_ts = 0.0
        # High-resolution rolling buffer for price_at() lookups
        self._hires: deque[tuple[float, float]] = deque()  # (ts, price)
        self._hires_last_ts = 0.0

    def _on_trade(self, trade: dict) -> None:
        price = float(trade["price"])
        ts = float(trade["timestamp"]) / 1000.0
        self.latest_price = price
        self.latest_ts = ts

        # High-res buffer: sample every ~1s, evict entries older than 10min
        if ts - self._hires_last_ts >= HIRES_INTERVAL_S:
            self._hires.append((ts, price))
            self._hires_last_ts = ts
            while self._hires and ts - self._hires[0][0] > HIRES_MAX_AGE_S:
                self._hires.popleft()

        if ts - self._last_snapshot_ts >= SNAPSHOT_INTERVAL_S:
            self.prices.append({"price": price, "ts": ts})
            self._last_snapshot_ts = ts
            if len(self.prices) > 200:
                self.prices = self.prices[-200:]

    def price_at(self, target_ts: float) -> float | None:
        """Return the price closest to *target_ts* from the high-res buffer.

        Returns None if the buffer is empty or the closest sample is more
        than 30 seconds from the target.
        """
        if not self._hires:
            return None
        best_ts, best_price = min(self._hires, key=lambda entry: abs(entry[0] - target_ts))
        if abs(best_ts - target_ts) > 30.0:
            return None
        return best_price

    def get_5min_returns(self) -> list[float]:
        if len(self.prices) < 2:
            return []

        returns = []
        for index in range(1, len(self.prices)):
            previous_price = self.prices[index - 1]["price"]
            current_price = self.prices[index]["price"]
            returns.append((current_price - previous_price) / previous_price)
        return returns

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        exchange = ccxtpro.binance()
        log.info("Connecting to Binance BTC/USDT feed...")
        try:
            while stop_event is None or not stop_event.is_set():
                try:
                    trades = await exchange.watch_trades("BTC/USDT")
                    if self.latest_price is None and trades:
                        log.info("Binance feed connected, first price: $%.2f", float(trades[0]["price"]))
                    for trade in trades:
                        self._on_trade(trade)
                except Exception as exc:
                    log.error("Binance feed error: %s", exc)
                    await asyncio.sleep(1)
        finally:
            await exchange.close()
