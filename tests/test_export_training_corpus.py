"""Tests for the decision->close join in the training corpus exporter.

Pure-function tests: the exporter's public helper takes an in-memory
sqlite3.Connection and returns a list of Row dataclasses. The CLI/parquet
path is smoke-tested in Task 2 Step 4 against the real ledger, not here.
"""
import sqlite3

from scripts.export_training_corpus import join_decision_close


def _make_db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute(
        """CREATE TABLE window_snapshots (
            id INTEGER PRIMARY KEY, window_slug TEXT, snapshot_type TEXT,
            timestamp TEXT, btc_price REAL, window_open_price REAL,
            displacement REAL, sigma_5min REAL, t_remaining REAL,
            model_p_up REAL, market_p_up REAL, up_ask REAL, down_ask REAL,
            up_bids_json TEXT, down_bids_json TEXT, trade_fired INTEGER,
            outcome TEXT, final_price REAL
        )"""
    )
    return c


def _insert_decision(c, slug, ts, *, displacement=0.001, sigma=0.0008,
                     t_rem=200, up_ask=0.58, down_ask=0.42, fired=1,
                     model_p_up=0.7):
    c.execute(
        """INSERT INTO window_snapshots (window_slug, snapshot_type, timestamp,
            displacement, sigma_5min, t_remaining, model_p_up, up_ask, down_ask,
            trade_fired) VALUES (?, 'decision', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (slug, ts, displacement, sigma, t_rem, model_p_up, up_ask, down_ask, fired),
    )


def _insert_close(c, slug, ts, *, outcome="up", final_price=65001.0, fired=1):
    c.execute(
        """INSERT INTO window_snapshots (window_slug, snapshot_type, timestamp,
            outcome, final_price, trade_fired) VALUES (?, 'close', ?, ?, ?, ?)""",
        (slug, ts, outcome, final_price, fired),
    )


def test_join_returns_labeled_decisions():
    c = _make_db()
    _insert_decision(c, "w1", "2026-04-25 00:00:00")
    _insert_close(c, "w1", "2026-04-25 00:05:00", outcome="up")

    rows = join_decision_close(c, source="paper")

    assert len(rows) == 1
    assert rows[0].window_slug == "w1"
    assert rows[0].outcome == "up"
    assert rows[0].outcome_int == 1
    assert rows[0].source == "paper"
    assert rows[0].displacement == 0.001
    # market_p_up_normalized = up_ask / (up_ask + down_ask) = 0.58 / 1.0
    assert abs(rows[0].market_p_up_normalized - 0.58) < 1e-9


def test_join_respects_since_timestamp_cutoff():
    c = _make_db()
    _insert_decision(c, "pre", "2026-04-20 00:00:00")
    _insert_close(c, "pre", "2026-04-20 00:05:00", outcome="up")
    _insert_decision(c, "post", "2026-04-25 00:00:00", fired=0)
    _insert_close(c, "post", "2026-04-25 00:05:00", outcome="down", fired=0)

    rows = join_decision_close(c, source="paper", since_timestamp="2026-04-24T00:00:00")

    assert [r.window_slug for r in rows] == ["post"]
    assert rows[0].outcome_int == 0


def test_join_drops_unlabeled_decision():
    c = _make_db()
    _insert_decision(c, "w_unlabeled", "2026-04-25 00:00:00")
    # no close row

    rows = join_decision_close(c, source="paper")

    assert rows == []


def test_join_drops_decision_with_missing_core_feature():
    c = _make_db()
    _insert_decision(c, "w_nullsigma", "2026-04-25 00:00:00", sigma=None)
    _insert_close(c, "w_nullsigma", "2026-04-25 00:05:00", outcome="up")

    rows = join_decision_close(c, source="paper")

    assert rows == []


def test_join_drops_t_remaining_leq_zero():
    c = _make_db()
    _insert_decision(c, "w_zero_t", "2026-04-25 00:00:00", t_rem=0)
    _insert_close(c, "w_zero_t", "2026-04-25 00:05:00", outcome="up")

    rows = join_decision_close(c, source="paper")

    assert rows == []


def test_join_drops_nonsensical_book():
    """If up_ask + down_ask <= 0 we can't normalize a market probability."""
    c = _make_db()
    _insert_decision(c, "w_zero_book", "2026-04-25 00:00:00", up_ask=0.0, down_ask=0.0)
    _insert_close(c, "w_zero_book", "2026-04-25 00:05:00", outcome="up")

    rows = join_decision_close(c, source="paper")

    assert rows == []


def test_join_drops_outcome_outside_up_down():
    c = _make_db()
    _insert_decision(c, "w_weird", "2026-04-25 00:00:00")
    _insert_close(c, "w_weird", "2026-04-25 00:05:00", outcome="void")

    rows = join_decision_close(c, source="paper")

    assert rows == []
