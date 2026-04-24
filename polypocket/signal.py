"""Signal engine: evaluate edge and produce trading signals."""

from dataclasses import dataclass

from polypocket.config import (
    CALIBRATION_SHRINKAGE_DOWN,
    CALIBRATION_SHRINKAGE_UP,
    MAX_EDGE_THRESHOLD_UP,
    MAX_ENTRY_PRICE,
    MIN_EDGE_THRESHOLD,
    MIN_EDGE_THRESHOLD_DOWN,
    MIN_MODEL_CONFIDENCE,
    MIN_MODEL_CONFIDENCE_UP,
    SIGNAL_CUSHION_TICKS,
    WINDOW_ENTRY_MIN_ELAPSED,
    WINDOW_ENTRY_MIN_REMAINING,
    effective_ask,
)


def _effective_entry(
    ask: float,
    opp_bids: list[dict] | None,
) -> float:
    """Edge-gate reference price: live pair-merge clearing when bids known, else ask.

    BUY UP on a binary market matches via pair-merge against a DOWN bid (the
    two orders sum-to-1), so the real taker entry is `1 - best_down_bid`. Using
    snapshot `up_ask` here overstates edge whenever the DOWN bid sits below
    (1 - up_ask) — which is the normal case; see 2026-04-23 replay. Fall back
    to `ask` only when the caller has no book (tests/backtests).
    """
    if not opp_bids:
        return ask
    best_opp = max(float(b["price"]) for b in opp_bids)
    return min(0.99, (1.0 - best_opp) + SIGNAL_CUSHION_TICKS * 0.01)
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
        up_bids: list[dict] | None = None,
        down_bids: list[dict] | None = None,
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
        # Live-executable entry: pair-merge clearing price when bids known,
        # snapshot ask otherwise. A BUY UP clears at (1 - best_down_bid), which
        # is typically higher than up_ask — using up_ask here inflates edge.
        up_entry = _effective_entry(up_ask, down_bids)
        down_entry = _effective_entry(down_ask, up_bids)
        up_edge = model_p_up - effective_ask(up_entry)
        down_edge = (1 - model_p_up) - effective_ask(down_entry)

        up_aligned = model_p_up >= MIN_MODEL_CONFIDENCE_UP
        down_aligned = model_p_up <= (1 - MIN_MODEL_CONFIDENCE)

        # Price gate uses live-executable entry, not snapshot ask. A BUY UP's
        # real entry is (1 - best_down_bid); gating on up_ask lets live fills
        # land above MAX_ENTRY_PRICE whenever the opposite bid sits far below
        # opp_ask — the same mechanism that drove +$0.075 mean slippage.
        up_price_ok = up_entry < MAX_ENTRY_PRICE
        down_price_ok = down_entry < MAX_ENTRY_PRICE

        if (
            up_aligned
            and up_price_ok
            and up_edge >= MIN_EDGE_THRESHOLD
            and up_edge < MAX_EDGE_THRESHOLD_UP
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
