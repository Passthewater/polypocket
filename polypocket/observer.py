"""Observation mode for comparing model and market probabilities."""

import asyncio
import csv
import logging
import time
from dataclasses import asdict, dataclass
from math import sqrt

from scipy.stats import t as t_dist

from polypocket.config import MODEL_TAIL_DF

log = logging.getLogger(__name__)


@dataclass
class ObservationRecord:
    timestamp: float
    window_slug: str
    btc_price: float
    window_open_price: float
    displacement: float
    t_remaining: float
    sigma_5min: float
    model_p_up: float
    market_p_up: float | None
    edge: float | None


def compute_model_p_up(
    displacement: float,
    t_remaining: float,
    sigma_5min: float,
) -> float:
    """Compute the probability BTC finishes above the window open."""
    if t_remaining <= 0:
        if displacement > 0:
            return 1.0
        if displacement < 0:
            return 0.0
        return 0.5

    sigma_remaining = sigma_5min * sqrt(t_remaining / 300.0)
    if sigma_remaining <= 0:
        if displacement > 0:
            return 1.0
        if displacement < 0:
            return 0.0
        return 0.5

    return float(t_dist.cdf(displacement / sigma_remaining, df=MODEL_TAIL_DF))


def compute_realized_vol(returns: list[float], lookback: int = 50) -> float:
    """Compute realized volatility from recent 5-minute returns."""
    if len(returns) < 2:
        return 0.0

    recent = returns[-lookback:]
    mean_return = sum(recent) / len(recent)
    variance = sum((value - mean_return) ** 2 for value in recent) / (len(recent) - 1)
    return variance ** 0.5


class Observer:
    """Collects observation records and persists them to CSV."""

    def __init__(self, output_path: str = "observations.csv"):
        self.output_path = output_path
        self.records: list[ObservationRecord] = []

    def log_observation(self, record: ObservationRecord) -> None:
        self.records.append(record)
        log.info(
            "window=%s disp=%.4f%% t_rem=%.0fs model=%.1f%% mkt=%s edge=%s",
            record.window_slug,
            record.displacement * 100,
            record.t_remaining,
            record.model_p_up * 100,
            f"{record.market_p_up * 100:.1f}%" if record.market_p_up is not None else "N/A",
            f"{record.edge * 100:.1f}%" if record.edge is not None else "N/A",
        )

    def save_csv(self) -> None:
        if not self.records:
            return

        fieldnames = list(asdict(self.records[0]).keys())
        with open(self.output_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for record in self.records:
                writer.writerow(asdict(record))
        log.info("Saved %d observations to %s", len(self.records), self.output_path)


def build_observation_record(
    *,
    timestamp: float,
    window_slug: str,
    btc_price: float,
    price_to_beat: float,
    t_remaining: float,
    sigma_5min: float,
    market_p_up: float | None,
) -> ObservationRecord:
    """Build an observation record anchored to Polymarket's official open."""
    displacement = (btc_price - price_to_beat) / price_to_beat
    model_p_up = compute_model_p_up(displacement, t_remaining, sigma_5min)
    edge = model_p_up - market_p_up if market_p_up is not None else None
    return ObservationRecord(
        timestamp=timestamp,
        window_slug=window_slug,
        btc_price=btc_price,
        window_open_price=price_to_beat,
        displacement=displacement,
        t_remaining=t_remaining,
        sigma_5min=sigma_5min,
        model_p_up=model_p_up,
        market_p_up=market_p_up,
        edge=edge,
    )


async def run_observer(duration_minutes: int = 60) -> None:
    """Run observation mode for a fixed duration."""
    from polypocket.config import VOLATILITY_LOOKBACK
    from polypocket.feeds.binance import BinanceFeed
    from polypocket.feeds.polymarket import fetch_active_windows, subscribe_and_stream

    observer = Observer()
    binance = BinanceFeed()
    stop = asyncio.Event()

    current_window = None

    async def on_book_update(window, side):
        del side
        nonlocal current_window

        if binance.latest_price is None:
            return

        now = time.time()
        t_remaining = window.end_time - now
        if t_remaining < 0:
            return

        if current_window is None or current_window.condition_id != window.condition_id:
            current_window = window
            if window.price_to_beat is None:
                window.price_to_beat = binance.latest_price
            log.info(
                "New window: %s, priceToBeat: %.6f (Binance: %.2f)",
                window.slug,
                window.price_to_beat,
                binance.latest_price,
            )

        sigma = compute_realized_vol(
            binance.get_5min_returns(),
            VOLATILITY_LOOKBACK,
        )
        if sigma <= 0:
            sigma = 0.001

        observer.log_observation(
            build_observation_record(
                timestamp=now,
                window_slug=window.slug,
                btc_price=binance.latest_price,
                price_to_beat=window.price_to_beat,
                t_remaining=t_remaining,
                sigma_5min=sigma,
                market_p_up=window.up_ask,
            )
        )

    async def poll_windows():
        while not stop.is_set():
            windows = await fetch_active_windows()
            log.info("Found %d active windows", len(windows))
            if windows:
                await subscribe_and_stream(windows, on_book_update, stop)
            await asyncio.sleep(30)

    log.info("Starting observation mode for %d minutes", duration_minutes)

    tasks = [
        asyncio.create_task(binance.run(stop)),
        asyncio.create_task(poll_windows()),
    ]

    try:
        await asyncio.sleep(duration_minutes * 60)
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        observer.save_csv()
        log.info("Observation complete. %d records saved.", len(observer.records))
