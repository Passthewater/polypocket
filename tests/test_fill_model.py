"""Tests for simulate_pair_merge_fill.

The function walks an opposing-side bid stack (best price first) buying up to
`size` shares. A bid at price `b` implies entry cost `1 - b` for a pair-merge
buy; levels whose implied entry exceeds the buffer-capped limit are skipped.
If full `size` can't be filled under the cap, the fill is rejected.
"""
import pytest

from polypocket.fillmodel import simulate_pair_merge_fill


def test_full_fill_top_bid_only():
    bids = [{"price": 0.48, "size": 5}]
    r = simulate_pair_merge_fill(size=1, opp_bids=bids, buffer_ticks=15)
    assert r.rejected is False
    assert r.filled_size == 1
    assert r.vwap == pytest.approx(0.48)
    assert r.implied_entry == pytest.approx(0.52)


def test_vwap_across_levels():
    bids = [{"price": 0.48, "size": 1}, {"price": 0.47, "size": 1}, {"price": 0.46, "size": 2}]
    r = simulate_pair_merge_fill(size=3, opp_bids=bids, buffer_ticks=15)
    assert r.rejected is False
    assert r.filled_size == 3
    assert r.vwap == pytest.approx((0.48 + 0.47 + 0.46) / 3)
    assert r.implied_entry == pytest.approx(1 - (0.48 + 0.47 + 0.46) / 3)


def test_cap_excludes_deep_levels():
    # best bid 0.48 -> best entry cost 0.52. Cap = 0.52 + 0.03 = 0.55.
    # second bid 0.40 -> entry cost 0.60 > 0.55 -> excluded.
    bids = [{"price": 0.48, "size": 1}, {"price": 0.40, "size": 5}]
    r = simulate_pair_merge_fill(size=3, opp_bids=bids, buffer_ticks=3)
    assert r.rejected is True


def test_size_exceeds_book():
    bids = [{"price": 0.48, "size": 2}]
    r = simulate_pair_merge_fill(size=10, opp_bids=bids, buffer_ticks=15)
    assert r.rejected is True


def test_empty_bids():
    r = simulate_pair_merge_fill(size=1, opp_bids=[], buffer_ticks=15)
    assert r.rejected is True


def test_none_bids():
    r = simulate_pair_merge_fill(size=1, opp_bids=None, buffer_ticks=15)
    assert r.rejected is True


def test_unsorted_input_defensive():
    # Input in ascending price order; function must re-sort desc.
    bids = [{"price": 0.46, "size": 2}, {"price": 0.47, "size": 1}, {"price": 0.48, "size": 1}]
    r = simulate_pair_merge_fill(size=3, opp_bids=bids, buffer_ticks=15)
    assert r.rejected is False
    assert r.vwap == pytest.approx((0.48 + 0.47 + 0.46) / 3)


def test_tick_edge_case_inclusive():
    # Cap = 1 - 0.48 + 0.04 = 0.56. Second bid 0.44 -> entry cost 0.56 == cap.
    # Inclusive: the level at exactly cap is eligible. Tick-integer comparison
    # is what avoids the e6c4ae7/a4de4e0 float bug class here.
    bids = [{"price": 0.48, "size": 1}, {"price": 0.44, "size": 2}]
    r = simulate_pair_merge_fill(size=2, opp_bids=bids, buffer_ticks=4)
    assert r.rejected is False
    assert r.filled_size == 2
    assert r.vwap == pytest.approx((0.48 + 0.44) / 2)


def test_tick_edge_case_float_artifact():
    # 0.1 + 0.2 style float artifacts. Cap in ticks: (1-0.48)*100 + 15 = 67.
    # A level at 0.33 has entry 0.67 -> should compute to exactly 67 ticks
    # after round(). Raw multiply (0.67 * 100) can be 66.99999...
    bids = [{"price": 0.48, "size": 1}, {"price": 0.33, "size": 1}]
    r = simulate_pair_merge_fill(size=2, opp_bids=bids, buffer_ticks=15)
    assert r.rejected is False
    assert r.filled_size == 2
