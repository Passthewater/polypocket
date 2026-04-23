"""Pair-merge fill simulation for offline replay.

Live BUYs on binary markets clear via pair-merge: a BUY UP matches against a
DOWN-side bid such that (up_fill + down_bid) = 1. So for our size S and the
opposing-side bid stack, we walk from the best bid downward, filling S shares
at their VWAP. The implied entry price we pay is (1 - VWAP).

The live IOC has a buffer cap: limit_price = 1 - best_opp_bid + buffer*0.01.
Bids below that threshold (entry cost > cap) are not matchable. If our size
can't be filled under the cap, live would reject/partial and we exclude the
trade from PnL.

All price comparisons are done in tick-integer space (round(x * 100)) to avoid
the float artifact bug class from e6c4ae7/a4de4e0.
"""
from dataclasses import dataclass


@dataclass
class FillResult:
    filled_size: float
    vwap: float
    implied_entry: float
    rejected: bool


def simulate_pair_merge_fill(
    size: float,
    opp_bids: list[dict] | None,
    buffer_ticks: int,
) -> FillResult:
    if not opp_bids or size <= 0:
        return FillResult(0.0, 0.0, 0.0, True)

    bids = sorted(
        ({"price": float(b["price"]), "size": float(b["size"])} for b in opp_bids),
        key=lambda b: -b["price"],
    )
    best = bids[0]["price"]
    cap_ticks = round((1.0 - best) * 100) + buffer_ticks

    remaining = size
    cost = 0.0
    filled = 0.0
    for b in bids:
        entry_ticks = round((1.0 - b["price"]) * 100)
        if entry_ticks > cap_ticks:
            break
        take = min(remaining, b["size"])
        cost += b["price"] * take
        filled += take
        remaining -= take
        if remaining <= 0:
            break

    if filled + 1e-9 < size:
        return FillResult(filled, 0.0, 0.0, True)

    vwap = cost / filled
    return FillResult(filled, vwap, 1.0 - vwap, False)
