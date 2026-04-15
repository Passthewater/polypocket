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
    market_price: float
    edge: float
    up_edge: float
    down_edge: float


class SignalEngine:
    """Evaluates whether an exploitable edge exists in the current window."""

    def evaluate(
        self,
        displacement: float,
        t_elapsed: float,
        t_remaining: float,
        sigma_5min: float,
        *,
        up_ask: float | None,
        down_ask: float | None,
    ) -> Signal | None:
        if t_elapsed < WINDOW_ENTRY_MIN_ELAPSED:
            return None
        if t_remaining < WINDOW_ENTRY_MIN_REMAINING:
            return None
        if up_ask is None or down_ask is None:
            return None
        if not (0 < up_ask <= 1) or not (0 < down_ask <= 1):
            return None
        if sigma_5min <= 0:
            return None

        model_p_up = compute_model_p_up(displacement, t_remaining, sigma_5min)
        up_edge = model_p_up - (up_ask * (1 + FEE_RATE))
        down_edge = (1 - model_p_up) - (down_ask * (1 + FEE_RATE))

        if up_edge >= MIN_EDGE_THRESHOLD and up_edge >= down_edge:
            return Signal(
                side="up",
                model_p_up=model_p_up,
                market_price=up_ask,
                edge=up_edge,
                up_edge=up_edge,
                down_edge=down_edge,
            )
        if down_edge >= MIN_EDGE_THRESHOLD:
            return Signal(
                side="down",
                model_p_up=model_p_up,
                market_price=down_ask,
                edge=down_edge,
                up_edge=up_edge,
                down_edge=down_edge,
            )
        return None
