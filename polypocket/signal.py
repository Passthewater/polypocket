"""Signal engine: evaluate edge and produce trading signals."""

from dataclasses import dataclass

from polypocket.config import (
    FEE_RATE,
    MIN_EDGE_THRESHOLD,
    WINDOW_ENTRY_MIN_ELAPSED,
    WINDOW_ENTRY_MIN_REMAINING,
)
from polypocket.observer import compute_model_p_up


@dataclass
class Signal:
    side: str
    model_p_up: float
    market_p_up: float
    edge: float


class SignalEngine:
    """Evaluates whether an exploitable edge exists in the current window."""

    def evaluate(
        self,
        displacement: float,
        t_elapsed: float,
        t_remaining: float,
        sigma_5min: float,
        market_p_up: float | None,
    ) -> Signal | None:
        if t_elapsed < WINDOW_ENTRY_MIN_ELAPSED:
            return None
        if t_remaining < WINDOW_ENTRY_MIN_REMAINING:
            return None
        if market_p_up is None:
            return None
        if sigma_5min <= 0:
            return None

        model_p_up = compute_model_p_up(displacement, t_remaining, sigma_5min)
        up_edge = model_p_up - market_p_up
        min_required = MIN_EDGE_THRESHOLD + FEE_RATE

        if up_edge >= min_required:
            return Signal(
                side="up",
                model_p_up=model_p_up,
                market_p_up=market_p_up,
                edge=up_edge,
            )
        if -up_edge >= min_required:
            return Signal(
                side="down",
                model_p_up=model_p_up,
                market_p_up=market_p_up,
                edge=-up_edge,
            )
        return None
