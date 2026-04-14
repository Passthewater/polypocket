"""Observation mode for comparing model and market probabilities."""

import csv
import logging
from dataclasses import asdict, dataclass
from math import sqrt

from scipy.stats import norm

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

    return float(norm.cdf(displacement / sigma_remaining))


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
