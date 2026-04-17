"""Signal engine: evaluate edge and produce trading signals."""

from dataclasses import dataclass

from polypocket.config import (
    CALIBRATION_SHRINKAGE_DOWN,
    CALIBRATION_SHRINKAGE_UP,
    MAX_ENTRY_PRICE,
    MIN_EDGE_THRESHOLD,
    MIN_EDGE_THRESHOLD_DOWN,
    MIN_MODEL_CONFIDENCE,
    MIN_MODEL_CONFIDENCE_UP,
    WINDOW_ENTRY_MIN_ELAPSED,
    WINDOW_ENTRY_MIN_REMAINING,
    effective_ask,
)
from polypocket.observer import calibrate_p_up, compute_model_p_up


@dataclass
class Signal:
    side: str
    model_p_up: float
    market_price: float
    edge: float
    up_edge: float
    down_edge: float
    model_p_up_raw: float | None = None


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

        model_p_up_raw = compute_model_p_up(displacement, t_remaining, sigma_5min)
        model_p_up = calibrate_p_up(
            model_p_up_raw,
            up_factor=CALIBRATION_SHRINKAGE_UP,
            down_factor=CALIBRATION_SHRINKAGE_DOWN,
        )
        up_edge = model_p_up - effective_ask(up_ask)
        down_edge = (1 - model_p_up) - effective_ask(down_ask)

        up_aligned = model_p_up >= MIN_MODEL_CONFIDENCE_UP
        down_aligned = model_p_up <= (1 - MIN_MODEL_CONFIDENCE)

        up_price_ok = up_ask < MAX_ENTRY_PRICE
        down_price_ok = down_ask < MAX_ENTRY_PRICE

        if (
            up_aligned
            and up_price_ok
            and up_edge >= MIN_EDGE_THRESHOLD
            and up_edge >= down_edge
        ):
            return Signal(
                side="up",
                model_p_up=model_p_up,
                model_p_up_raw=model_p_up_raw,
                market_price=up_ask,
                edge=up_edge,
                up_edge=up_edge,
                down_edge=down_edge,
            )
        if down_aligned and down_price_ok and down_edge >= MIN_EDGE_THRESHOLD_DOWN:
            return Signal(
                side="down",
                model_p_up=model_p_up,
                model_p_up_raw=model_p_up_raw,
                market_price=down_ask,
                edge=down_edge,
                up_edge=up_edge,
                down_edge=down_edge,
            )
        return None
