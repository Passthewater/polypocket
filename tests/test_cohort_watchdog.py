"""Tests for the safety-rail evaluator used by cohort_watchdog.

`evaluate_rails` is a pure function: takes a snapshot of cohort state
(fills, rejects, cumulative pnl, wall-clock elapsed) and returns a
verdict dict with `trip: bool`, `reason: str | None`, and the metric
values. The polling loop is separately smoke-tested.
"""
import pytest

from scripts.cohort_watchdog import evaluate_rails, Rails


BASE = Rails(
    max_fills=25,
    max_loss=20.0,
    max_wall_clock_days=7,
    reject_breaker_after=10,
    reject_breaker_pct=0.5,
)


def test_no_trip_early():
    v = evaluate_rails(BASE, n_fills=5, n_rejects=1, cum_pnl=-1.2, elapsed_days=0.5)
    assert v["trip"] is False
    assert v["reason"] is None


def test_fill_cap_trips():
    v = evaluate_rails(BASE, n_fills=25, n_rejects=2, cum_pnl=-3.0, elapsed_days=1.0)
    assert v["trip"] is True
    assert "fill" in v["reason"].lower()


def test_loss_cap_trips():
    v = evaluate_rails(BASE, n_fills=10, n_rejects=1, cum_pnl=-20.01, elapsed_days=1.0)
    assert v["trip"] is True
    assert "loss" in v["reason"].lower()


def test_wall_clock_trips():
    v = evaluate_rails(BASE, n_fills=8, n_rejects=0, cum_pnl=-2.0, elapsed_days=7.5)
    assert v["trip"] is True
    assert "wall" in v["reason"].lower() or "time" in v["reason"].lower()


def test_reject_breaker_trips_at_exactly_10_attempts():
    # 6 rejects + 4 fills = 10 attempts, 60% reject -> trip
    v = evaluate_rails(BASE, n_fills=4, n_rejects=6, cum_pnl=-0.4, elapsed_days=0.2)
    assert v["trip"] is True
    assert "reject" in v["reason"].lower()


def test_reject_breaker_trips_while_armed_under_10():
    # Breaker is "armed during first 10 attempts" -- fires the moment
    # reject-rate crosses threshold, NOT only when attempts==10 exactly.
    # Polling at 60s can skip past attempts=10 (rejects fire fast); if the
    # breaker waits for an exact equality, it misses. So: 7 attempts, 4
    # rejects = 57% -> must trip.
    v = evaluate_rails(BASE, n_fills=3, n_rejects=4, cum_pnl=-0.6, elapsed_days=0.1)
    assert v["trip"] is True
    assert "reject" in v["reason"].lower()


def test_reject_breaker_dormant_past_armed_window():
    # attempts=11 past the 10-attempt armed window -> breaker dormant even
    # at 54% rejects. Fill cap (25) is still not hit. No trip.
    v = evaluate_rails(BASE, n_fills=5, n_rejects=6, cum_pnl=-2.0, elapsed_days=1.0)
    assert v["trip"] is False


def test_reject_breaker_requires_min_attempts():
    # 1 attempt, 1 reject = 100% but single-observation noise. Don't trip
    # on n=1. Minimum armed attempts is 2.
    v = evaluate_rails(BASE, n_fills=0, n_rejects=1, cum_pnl=0.0, elapsed_days=0.05)
    assert v["trip"] is False
