"""Observation mode for comparing model and market probabilities."""

import asyncio
import csv
import json
import logging
import os
import time
from bisect import bisect_right
from dataclasses import asdict, dataclass
from math import exp, sqrt
from pathlib import Path

from scipy.stats import norm

log = logging.getLogger(__name__)


_DEFAULT_V2_COEFS_PATH = Path(__file__).parent / "model_v2_coefs.json"
_v2_coefs: dict | None = None
_HULL_WARNED: set[str] = set()


def _reset_v2_cache_for_tests() -> None:
    """Test-only hook: clears memoized v2 coefs and hull-warn dedupe set."""
    global _v2_coefs
    _v2_coefs = None
    _HULL_WARNED.clear()


def _load_v2_coefs() -> dict:
    global _v2_coefs
    if _v2_coefs is not None:
        return _v2_coefs
    path = Path(os.environ.get("MODEL_V2_COEFS_PATH", str(_DEFAULT_V2_COEFS_PATH)))
    with open(path) as f:
        _v2_coefs = json.load(f)
    return _v2_coefs


def _isotonic_apply(x: float, xs: list[float], ys: list[float]) -> float:
    """Piecewise-linear interpolation with clipping; mirrors sklearn's
    IsotonicRegression(out_of_bounds='clip')."""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect_right(xs, x) - 1
    x0, x1 = xs[i], xs[i + 1]
    y0, y1 = ys[i], ys[i + 1]
    if x1 == x0:
        return y0
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


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

    return float(norm.cdf(displacement / sigma_remaining))


def calibrate_p_up(
    p_raw: float,
    *,
    up_factor: float,
    down_factor: float,
) -> float:
    """Apply side-dependent shrinkage toward 0.5.

    The raw norm.cdf model is overconfident at the extremes. Shrinking
    pulls the estimate toward 0.5 by `factor` (1.0 = identity, 0.0 = collapse
    to 0.5). DOWN-leaning (p_raw<0.5) and UP-leaning (p_raw>0.5) regions are
    allowed separate factors because post-filter calibration showed DOWN
    carries a larger overconfidence gap than UP.
    """
    factor = up_factor if p_raw >= 0.5 else down_factor
    return 0.5 + (p_raw - 0.5) * factor


def compute_model_p_up_v2(
    *,
    displacement: float,
    t_remaining: float,
    sigma_5min: float,
    up_ask: float,
    down_ask: float,
) -> float:
    """v2: L2 logistic + isotonic calibration. Coefs in model_v2_coefs.json.

    market_p_up is NOT a parameter -- it's derived from up_ask/down_ask inside
    this function to match the exporter's formula.
    """
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

    coefs = _load_v2_coefs()

    denom = up_ask + down_ask
    market_p_up_normalized = (up_ask / denom) if denom > 0 else 0.5
    z = displacement / sigma_remaining

    all_features = {
        "z": z,
        "t_remaining": t_remaining,
        "sigma_5min": sigma_5min,
        "market_p_up_normalized": market_p_up_normalized,
        "spread": up_ask + down_ask - 1.0,
        "z_times_market": z * market_p_up_normalized,
    }

    hull = coefs.get("feature_hull")
    if hull is not None:
        for name in coefs["features"]:
            lo, hi = hull[name]
            v = all_features[name]
            if (v < lo or v > hi) and name not in _HULL_WARNED:
                _HULL_WARNED.add(name)
                log.warning(
                    "compute_model_p_up_v2: feature %s=%.4f outside training hull "
                    "[%.4f, %.4f] (further out-of-hull values for this feature suppressed)",
                    name, v, lo, hi,
                )

    values = [all_features[name] for name in coefs["features"]]
    standardized = [
        (v - coefs["scaler_mean"][i]) / coefs["scaler_scale"][i]
        for i, v in enumerate(values)
    ]
    logit = coefs["logistic_intercept"] + sum(
        c * v for c, v in zip(coefs["logistic_coef"], standardized)
    )
    raw = 1.0 / (1.0 + exp(-logit))

    return _isotonic_apply(raw, coefs["isotonic_x"], coefs["isotonic_y"])


def compute_model_p_up_active(
    *,
    displacement: float,
    t_remaining: float,
    sigma_5min: float,
    up_ask: float,
    down_ask: float,
) -> float:
    """Dispatch to v1 (raw) or v2 based on MODEL_VERSION env var. Default v1.

    v1 contract: returns RAW norm.cdf output; the caller (signal.py) applies
    `calibrate_p_up` to get the final probability. v2 returns its own
    isotonic-calibrated output directly.
    """
    version = os.environ.get("MODEL_VERSION", "v1").strip().lower()
    if version == "v2":
        return compute_model_p_up_v2(
            displacement=displacement,
            t_remaining=t_remaining,
            sigma_5min=sigma_5min,
            up_ask=up_ask,
            down_ask=down_ask,
        )
    return compute_model_p_up(displacement, t_remaining, sigma_5min)


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
