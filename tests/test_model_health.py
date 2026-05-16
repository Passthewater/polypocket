"""Tests for scripts/model_health.py (issue #22)."""
import math
import os
import tempfile

import pytest

from polypocket.ledger import init_db, log_snapshot
from scripts.model_health import (
    GAP_FLAG_THRESHOLD,
    N_FLAG_THRESHOLD,
    _pred_and_hit,
    brier_score,
    flag_breaches,
    load_decisions,
    log_loss,
    per_side_gap,
    reliability_table,
    render_report,
)


def _make_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    return path


def _log_pair(
    db_path: str,
    slug: str,
    *,
    p_up_v1: float | None = None,
    p_up_v2: float | None = None,
    outcome: str | None = "up",
) -> None:
    """Write paired decision + close snapshots for one window."""
    log_snapshot(
        db_path, slug, "decision",
        stats={
            "model_p_up": p_up_v2 if p_up_v2 is not None else p_up_v1,
            "model_p_up_v1_calibrated": p_up_v1,
            "model_p_up_v2": p_up_v2,
        },
        trade_fired=False,
    )
    if outcome is not None:
        log_snapshot(
            db_path, slug, "close",
            stats={},
            outcome=outcome,
        )


# ── _pred_and_hit ────────────────────────────────────────────────────────────

def test_pred_and_hit_up_leaning_correct():
    p_pred, hit, side = _pred_and_hit(0.70, "up")
    assert (p_pred, hit, side) == (0.70, 1, "up")


def test_pred_and_hit_up_leaning_miss():
    p_pred, hit, side = _pred_and_hit(0.70, "down")
    assert (p_pred, hit, side) == (0.70, 0, "up")


def test_pred_and_hit_down_leaning_correct():
    # p_up=0.30 means model thinks DOWN at 0.70 confidence.
    p_pred, hit, side = _pred_and_hit(0.30, "down")
    assert side == "down"
    assert hit == 1
    assert p_pred == pytest.approx(0.70)


def test_pred_and_hit_down_leaning_miss():
    p_pred, hit, side = _pred_and_hit(0.30, "up")
    assert (hit, side) == (0, "down")
    assert p_pred == pytest.approx(0.70)


def test_pred_and_hit_p_equal_half_treated_as_up():
    _, _, side = _pred_and_hit(0.50, "up")
    assert side == "up"


# ── reliability_table ────────────────────────────────────────────────────────

def test_reliability_table_bins_up_leaning():
    rows = [
        {"model_p_up_v2": 0.55, "outcome": "up"},
        {"model_p_up_v2": 0.56, "outcome": "down"},
        {"model_p_up_v2": 0.59, "outcome": "up"},
    ]
    table = reliability_table(rows, "model_p_up_v2", bin_width=0.05)
    # all three land in 0.55-0.60
    row = next(r for r in table if r["bin_lo"] == pytest.approx(0.55))
    assert row["n"] == 3
    assert row["predicted_wr"] == pytest.approx((0.55 + 0.56 + 0.59) / 3)
    assert row["actual_wr"] == pytest.approx(2 / 3)
    assert row["gap"] == pytest.approx(row["actual_wr"] - row["predicted_wr"])


def test_reliability_table_down_leaning_lands_on_reoriented_bin():
    # p_up=0.30 -> p_pred=0.70 -> belongs in 0.70-0.75, NOT 0.30-0.35.
    rows = [{"model_p_up_v2": 0.30, "outcome": "down"}]
    table = reliability_table(rows, "model_p_up_v2", bin_width=0.05)
    hit_bin = next(r for r in table if r["bin_lo"] == pytest.approx(0.70))
    assert hit_bin["n"] == 1
    low_bin_exists = any(r["bin_lo"] == pytest.approx(0.30) for r in table)
    assert not low_bin_exists  # 0.30 is below our floor of 0.50


def test_reliability_table_p_pred_exactly_one_lands_in_last_bin():
    rows = [{"model_p_up_v2": 1.0, "outcome": "up"}]
    table = reliability_table(rows, "model_p_up_v2", bin_width=0.05)
    last = table[-1]
    assert last["bin_hi"] == pytest.approx(1.00)
    assert last["n"] == 1


def test_reliability_table_skips_null_prob_and_unknown_outcome():
    rows = [
        {"model_p_up_v2": None, "outcome": "up"},
        {"model_p_up_v2": 0.60, "outcome": None},
        {"model_p_up_v2": 0.60, "outcome": "weird"},
        {"model_p_up_v2": 0.60, "outcome": "up"},
    ]
    table = reliability_table(rows, "model_p_up_v2", bin_width=0.05)
    bin_60 = next(r for r in table if r["bin_lo"] == pytest.approx(0.60))
    assert bin_60["n"] == 1


# ── brier_score / log_loss (side-invariant on raw p_up) ──────────────────────

def test_brier_score_basic():
    rows = [
        {"model_p_up_v2": 0.70, "outcome": "up"},    # (0.70-1)^2 = 0.09
        {"model_p_up_v2": 0.40, "outcome": "down"},  # (0.40-0)^2 = 0.16
    ]
    assert brier_score(rows, "model_p_up_v2") == pytest.approx((0.09 + 0.16) / 2)


def test_brier_score_empty_returns_none():
    assert brier_score([], "model_p_up_v2") is None


def test_log_loss_perfect_predictions_are_near_zero():
    rows = [
        {"model_p_up_v2": 1.0 - 1e-9, "outcome": "up"},
        {"model_p_up_v2": 1e-9, "outcome": "down"},
    ]
    ll = log_loss(rows, "model_p_up_v2")
    assert ll is not None
    assert ll < 1e-6


def test_log_loss_clips_zero_and_one():
    # Without clipping, p=0 against outcome='up' explodes to inf.
    rows = [{"model_p_up_v2": 0.0, "outcome": "up"}]
    ll = log_loss(rows, "model_p_up_v2", eps=1e-9)
    assert ll is not None
    assert math.isfinite(ll)


# ── per_side_gap ─────────────────────────────────────────────────────────────

def test_per_side_gap_splits_up_and_down():
    rows = [
        {"model_p_up_v2": 0.70, "outcome": "up"},     # up-leaning hit
        {"model_p_up_v2": 0.70, "outcome": "down"},   # up-leaning miss
        {"model_p_up_v2": 0.30, "outcome": "down"},   # down-leaning hit
    ]
    side = per_side_gap(rows, "model_p_up_v2")
    assert side["up"]["n"] == 2
    assert side["up"]["predicted_wr"] == pytest.approx(0.70)
    assert side["up"]["actual_wr"] == pytest.approx(0.50)
    assert side["up"]["gap"] == pytest.approx(-0.20)
    assert side["down"]["n"] == 1
    assert side["down"]["predicted_wr"] == pytest.approx(0.70)
    assert side["down"]["actual_wr"] == pytest.approx(1.0)


# ── flag_breaches ────────────────────────────────────────────────────────────

def test_flag_breaches_requires_both_n_and_gap():
    table = [
        {"bin_lo": 0.55, "bin_hi": 0.60, "n": 100, "gap": 0.04,  # n high, gap small
         "predicted_wr": 0.57, "actual_wr": 0.61},
        {"bin_lo": 0.60, "bin_hi": 0.65, "n": 10, "gap": 0.10,   # gap big, n small
         "predicted_wr": 0.62, "actual_wr": 0.72},
        {"bin_lo": 0.65, "bin_hi": 0.70, "n": 50, "gap": -0.08,  # both -> FLAG
         "predicted_wr": 0.67, "actual_wr": 0.59},
        {"bin_lo": 0.70, "bin_hi": 0.75, "n": 0, "gap": None,    # empty
         "predicted_wr": None, "actual_wr": None},
    ]
    flagged = flag_breaches(table)
    assert len(flagged) == 1
    assert flagged[0]["bin_lo"] == pytest.approx(0.65)


def test_flag_breaches_thresholds_match_constants():
    assert GAP_FLAG_THRESHOLD == pytest.approx(0.05)
    assert N_FLAG_THRESHOLD == 20


# ── load_decisions (against a real SQLite file) ──────────────────────────────

def test_load_decisions_filters_null_probability_and_missing_close():
    db_path = _make_db()
    try:
        _log_pair(db_path, "btc-5m-1", p_up_v2=0.60, outcome="up")
        _log_pair(db_path, "btc-5m-2", p_up_v2=None, p_up_v1=None, outcome="up")
        # decision exists but no close snapshot
        log_snapshot(db_path, "btc-5m-3", "decision",
                     stats={"model_p_up_v2": 0.70, "model_p_up": 0.70})
        rows = load_decisions(db_path)
        assert len(rows) == 1
        assert rows[0]["window_slug"] == "btc-5m-1"
    finally:
        os.unlink(db_path)


def test_load_decisions_returns_empty_for_missing_db():
    assert load_decisions("/no/such/path.db") == []


def test_load_decisions_keeps_v1_only_rows():
    db_path = _make_db()
    try:
        _log_pair(db_path, "btc-5m-1", p_up_v1=0.55, p_up_v2=None, outcome="up")
        rows = load_decisions(db_path)
        assert len(rows) == 1
        assert rows[0]["model_p_up_v1_calibrated"] == pytest.approx(0.55)
        assert rows[0]["model_p_up_v2"] is None
    finally:
        os.unlink(db_path)


def test_load_decisions_max_decisions_keeps_most_recent():
    db_path = _make_db()
    try:
        for i in range(5):
            _log_pair(db_path, f"btc-5m-{i}", p_up_v2=0.55 + 0.01 * i, outcome="up")
        rows = load_decisions(db_path, max_decisions=2)
        assert len(rows) == 2
        # ORDER BY timestamp ASC then id ASC -> last two inserted
        assert rows[0]["window_slug"] == "btc-5m-3"
        assert rows[1]["window_slug"] == "btc-5m-4"
    finally:
        os.unlink(db_path)


# ── end-to-end render ────────────────────────────────────────────────────────

def test_render_report_handles_empty_dbs():
    out = render_report(
        paper_rows_db="/no/such/paper.db",
        live_rows_db="/no/such/live.db",
        window_days=None,
        max_decisions=None,
        bin_width=0.05,
    )
    assert "Paper" in out
    assert "Live" in out
    assert "no decisions" in out


def test_render_report_includes_v2_section_with_data():
    db_path = _make_db()
    try:
        # 5 rows clustered in 0.55-0.60, half hit -> gap ~ -0.075
        for i, outcome in enumerate(["up", "down", "up", "down", "up"]):
            _log_pair(db_path, f"btc-5m-{i}", p_up_v2=0.575, outcome=outcome)
        out = render_report(
            paper_rows_db=db_path,
            live_rows_db="/no/such/live.db",
            window_days=None,
            max_decisions=None,
            bin_width=0.05,
        )
        assert "v2" in out
        assert "0.55-0.60" in out
        assert "5 decisions" in out  # window header
    finally:
        os.unlink(db_path)
