"""Tests for the slip-distribution + verdict functions used by the analyzer."""
import pytest

from scripts.analyze_buffer_cohort import (
    compute_slip_ticks,
    slip_distribution,
    classify_verdict,
)


def test_compute_slip_ticks_positive():
    # entry 0.56, best_opp_bid 0.48 -> implied clearing 0.52 -> slip 0.04 -> 4 ticks
    assert compute_slip_ticks(entry=0.56, best_opp_bid=0.48) == 4


def test_compute_slip_ticks_zero():
    # entry 0.52, best_opp_bid 0.48 -> slip 0
    assert compute_slip_ticks(entry=0.52, best_opp_bid=0.48) == 0


def test_compute_slip_ticks_float_artifact():
    # 0.1 + 0.2 = 0.30000000000000004; raw multiply can produce 4.999... -> 4
    # Tick-integer rounding must produce 5.
    # entry = 1 - 0.65 + 5*0.01 = 0.40; best_opp_bid = 0.65 -> slip should be 5
    assert compute_slip_ticks(entry=0.40, best_opp_bid=0.65) == 5


def test_slip_distribution_basic():
    # Pinned against `statistics.median` (mean of the two middle values for
    # even n) and nearest-rank quartiles from a simple sorted-list indexing.
    # The implementation in scripts/analyze_buffer_cohort.py MUST match these
    # expected values; adjust the implementation, not the test.
    slips = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    d = slip_distribution(slips)
    assert d["n"] == 10
    assert d["median"] == 5.5   # statistics.median on even n
    assert d["mean"] == pytest.approx(5.5)
    assert d["min"] == 1
    assert d["max"] == 10
    # Nearest-rank percentile with simple indexing:
    #   p25 index = n // 4 - 1 = 1 (value 2);
    #   p75 index = 3 * n // 4 = 7 (value 8).
    # If you prefer a different percentile convention, update BOTH the
    # implementation AND these numbers -- don't add "or X" escape hatches.
    assert d["p25"] == 2
    assert d["p75"] == 8


def test_slip_distribution_empty():
    d = slip_distribution([])
    assert d["n"] == 0
    assert d["median"] is None


def test_classify_ship():
    assert classify_verdict(median_slip=6) == "SHIP"
    assert classify_verdict(median_slip=5) == "SHIP"
    assert classify_verdict(median_slip=0) == "SHIP"


def test_classify_escalate():
    assert classify_verdict(median_slip=8) == "ESCALATE"
    assert classify_verdict(median_slip=11) == "ESCALATE"


def test_classify_ambiguous():
    assert classify_verdict(median_slip=7) == "AMBIGUOUS"


def test_classify_none():
    assert classify_verdict(median_slip=None) == "AMBIGUOUS"
