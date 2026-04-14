"""Real-time BTC/USDT price feed via ccxt pro WebSocket."""

import asyncio
import logging

import ccxt.pro as ccxtpro

log = logging.getLogger(__name__)

SNAPSHOT_INTERVAL_S = 300


class BinanceFeed:
    """Streams BTC/USDT trades and stores periodic snapshots."""

    def __init__(self):
        self.latest_price: float | None = None
        self.latest_ts: float | None = None
        self.prices: list[dict[str, float]] = []
        self._last_snapshot_ts = 0.0

    def _on_trade(self, trade: dict) -> None:
        price = float(trade["price"])
        ts = float(trade["timestamp"]) / 1000.0
        self.latest_price = price
        self.latest_ts = ts

        if ts - self._last_snapshot_ts >= SNAPSHOT_INTERVAL_S:
            self.prices.append({"price": price, "ts": ts})
            self._last_snapshot_ts = ts
            if len(self.prices) > 200:
                self.prices = self.prices[-200:]

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
        try:
            while stop_event is None or not stop_event.is_set():
                try:
                    trades = await exchange.watch_trades("BTC/USDT")
                    for trade in trades:
                        self._on_trade(trade)
                except Exception as exc:
                    log.error("Binance feed error: %s", exc)
                    await asyncio.sleep(1)
        finally:
            await exchange.close()
