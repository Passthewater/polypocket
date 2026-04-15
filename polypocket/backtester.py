"""Backtester: replay historical BTC price data through the signal model.

This is a proxy backtest: it uses Binance candle opens as a stand-in for
Polymarket's `priceToBeat` because historical event metadata is not readily
available in bulk.

WARNING: without historical Polymarket ask prices, the backtest assumes a
fixed midpoint market price (default 0.50) and converts it into synthetic
up/down asks. Live trading uses real order book asks which already partially
reflect the move. Backtest results will overstate edge vs. live - use paper
trading for realistic performance estimates.
"""

import logging
import time
from dataclasses import dataclass

import aiohttp

from polypocket.config import FEE_RATE
from polypocket.signal import SignalEngine

log = logging.getLogger(__name__)

BINANCE_KLINE_URL = "https://api.binance.com/api/v3/klines"


@dataclass
class WindowResult:
    open_price: float
    close_price: float
    outcome: str
    signal_fired: bool
    signal_side: str | None
    signal_time_s: float | None
    model_p_up: float | None
    edge: float | None
    pnl: float | None


def simulate_window(
    candles: list[dict],
    sigma_5min: float,
    market_p_up: float = 0.50,
) -> WindowResult:
    """Simulate one 5-minute window using 1-minute candle data."""
    if len(candles) < 5:
        return WindowResult(
            open_price=0,
            close_price=0,
            outcome="up",
            signal_fired=False,
            signal_side=None,
            signal_time_s=None,
            model_p_up=None,
            edge=None,
            pnl=None,
        )

    open_price = candles[0]["open"]
    close_price = candles[-1]["close"]
    outcome = "up" if close_price >= open_price else "down"

    engine = SignalEngine()
    signal_result = None

    for index, candle in enumerate(candles):
        t_elapsed = (index + 1) * 60.0
        t_remaining = 300.0 - t_elapsed
        current_price = candle["close"]
        displacement = (current_price - open_price) / open_price

        signal = engine.evaluate(
            displacement=displacement,
            t_elapsed=t_elapsed,
            t_remaining=t_remaining,
            sigma_5min=sigma_5min,
            up_ask=market_p_up,
            down_ask=1.0 - market_p_up,
        )

        if signal is not None and signal_result is None:
            entry_price = market_p_up if signal.side == "up" else (1.0 - market_p_up)
            payout = 1.0 if signal.side == outcome else 0.0
            fees = entry_price * FEE_RATE
            pnl = payout - entry_price - fees
            signal_result = WindowResult(
                open_price=open_price,
                close_price=close_price,
                outcome=outcome,
                signal_fired=True,
                signal_side=signal.side,
                signal_time_s=t_elapsed,
                model_p_up=signal.model_p_up,
                edge=signal.edge,
                pnl=pnl,
            )

    if signal_result:
        return signal_result

    return WindowResult(
        open_price=open_price,
        close_price=close_price,
        outcome=outcome,
        signal_fired=False,
        signal_side=None,
        signal_time_s=None,
        model_p_up=None,
        edge=None,
        pnl=None,
    )


async def fetch_historical_klines(
    symbol: str = "BTCUSDT",
    interval: str = "1m",
    days: int = 7,
) -> list[dict]:
    """Fetch historical 1-minute candles from Binance REST API."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (days * 24 * 60 * 60 * 1000)
    all_candles: list[dict] = []

    async with aiohttp.ClientSession() as session:
        cursor = start_ms
        while cursor < end_ms:
            params = {
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1000,
            }
            async with session.get(BINANCE_KLINE_URL, params=params) as response:
                data = await response.json()
                if not data:
                    break
                for candle in data:
                    all_candles.append(
                        {
                            "ts": candle[0],
                            "open": float(candle[1]),
                            "high": float(candle[2]),
                            "low": float(candle[3]),
                            "close": float(candle[4]),
                            "volume": float(candle[5]),
                        }
                    )
                cursor = data[-1][0] + 60_000

    log.info("Fetched %d candles (%d days)", len(all_candles), days)
    return all_candles


def run_backtest(
    candles: list[dict],
    sigma_override: float | None = None,
    market_p_up: float = 0.50,
) -> dict:
    """Run backtest over all 5-minute windows in the candle data.

    Args:
        market_p_up: assumed market probability for all windows. Default 0.50
            overstates edge vs. live where real asks partially price in the move.
    """
    five_min_returns: list[float] = []
    results: list[WindowResult] = []

    for index in range(0, len(candles) - 4, 5):
        window_candles = candles[index : index + 5]
        if len(window_candles) < 5:
            break

        if sigma_override is not None:
            sigma = sigma_override
        elif len(five_min_returns) >= 10:
            recent = five_min_returns[-50:]
            mean = sum(recent) / len(recent)
            variance = sum((value - mean) ** 2 for value in recent) / (len(recent) - 1)
            sigma = variance ** 0.5
        else:
            sigma = 0.001

        results.append(simulate_window(window_candles, sigma_5min=sigma, market_p_up=market_p_up))

        window_return = (
            (window_candles[-1]["close"] - window_candles[0]["open"]) / window_candles[0]["open"]
        )
        five_min_returns.append(window_return)

    traded = [result for result in results if result.signal_fired]
    wins = [result for result in traded if result.pnl is not None and result.pnl > 0]
    losses = [result for result in traded if result.pnl is not None and result.pnl <= 0]
    total_pnl = sum(result.pnl for result in traded if result.pnl is not None)

    return {
        "total_windows": len(results),
        "signals_fired": len(traded),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(traded) if traded else 0,
        "total_pnl": total_pnl,
        "avg_pnl_per_trade": total_pnl / len(traded) if traded else 0,
        "profit_factor": (
            sum(result.pnl for result in wins if result.pnl is not None)
            / abs(sum(result.pnl for result in losses if result.pnl is not None))
            if losses and any(result.pnl is not None for result in losses)
            else float("inf")
        ),
        "max_consecutive_losses": _max_streak(
            traded, lambda result: result.pnl is not None and result.pnl <= 0
        ),
    }


def _max_streak(items, predicate) -> int:
    streak = 0
    max_streak = 0
    for item in items:
        if predicate(item):
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return max_streak


async def run_backtest_cli(days: int = 7) -> None:
    """CLI entry point for backtesting."""
    log.info("Fetching %d days of BTC 1-min candles from Binance...", days)
    candles = await fetch_historical_klines(days=days)
    log.info("Running backtest over %d candles...", len(candles))

    for assumed_p in (0.50, 0.52, 0.55):
        summary = run_backtest(candles, market_p_up=assumed_p)
        print(f"\n=== BACKTEST RESULTS (assumed market_p_up={assumed_p:.2f}) ===")
        print(f"Period: {days} days ({summary['total_windows']} windows)")
        print(f"Signals fired: {summary['signals_fired']}")
        print(f"Wins: {summary['wins']}  Losses: {summary['losses']}")
        print(f"Win rate: {summary['win_rate']:.1%}")
        print(f"Total P&L: ${summary['total_pnl']:+,.2f} (per $1 position)")
        print(f"Avg P&L per trade: ${summary['avg_pnl_per_trade']:+,.4f}")
        print(f"Profit factor: {summary['profit_factor']:.2f}")
        print(f"Max consecutive losses: {summary['max_consecutive_losses']}")
    print()
