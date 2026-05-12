"""Tests for the one-shot signal_reference_price backfill.

Provenance per the design:
  'exact'        : decision snapshot has the side-relevant non-null bids JSON
  'approximate'  : decision snapshot exists but lacks the side-relevant bids JSON
  'missing'      : no decision snapshot for the window
"""
import json
import sqlite3

import pytest

from polypocket.ledger import init_db, log_trade, log_snapshot
from scripts.backfill_signal_reference import backfill


def _seed_trade_and_decision(db, slug, side, has_opp_bids):
    """has_opp_bids: True populates the bids the given side's gate needs."""
    log_trade(
        db_path=db, window_slug=slug, side=side, entry_price=0.60, size=10.0,
        fees=0.024, model_p_up=0.70, market_p_up=0.58, edge=0.12,
        outcome="up", pnl=3.976, status="settled",
    )
    stats = {"btc_price": 65000, "window_open_price": 64900,
             "displacement": 0.0015, "sigma_5min": 0.002, "model_p_up": 0.70,
             "t_remaining": 200, "up_ask": 0.58, "down_ask": 0.42,
             "market_p_up": 0.58, "edge": 0.12, "preview_side": side,
             "quote_status": "ok"}
    book = None
    if has_opp_bids:
        # For side='up', the opp side is 'down', so we need down_bids.
        # For side='down', we need up_bids.
        book = {"up": [], "down": [],
                "up_bids": [{"price": 0.42}] if side == "down" else None,
                "down_bids": [{"price": 0.42}] if side == "up" else None}
    log_snapshot(db, slug, "decision", stats, book_depth=book)


def test_backfill_tags_exact_when_side_relevant_bids_present(tmp_path):
    from polypocket.config import SIGNAL_CUSHION_TICKS

    db = str(tmp_path / "t.db")
    init_db(db)
    _seed_trade_and_decision(db, "w1", "up", has_opp_bids=True)
    counts = backfill(db)
    assert counts["exact"] == 1
    with sqlite3.connect(db) as c:
        row = c.execute(
            "SELECT signal_reference_price, signal_reference_source "
            "FROM trades WHERE window_slug='w1'"
        ).fetchone()
    assert row[1] == "exact"
    expected = (1.0 - 0.42) + SIGNAL_CUSHION_TICKS * 0.01
    assert row[0] == pytest.approx(expected, abs=1e-9)


def test_backfill_tags_approximate_when_opp_bids_missing(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    _seed_trade_and_decision(db, "w2", "up", has_opp_bids=False)
    counts = backfill(db)
    assert counts["approximate"] == 1
    with sqlite3.connect(db) as c:
        row = c.execute(
            "SELECT signal_reference_price, signal_reference_source "
            "FROM trades WHERE window_slug='w2'"
        ).fetchone()
    assert row[1] == "approximate"
    assert row[0] == pytest.approx(0.58, abs=1e-9)  # falls back to up_ask


def test_backfill_tags_missing_when_no_decision_snapshot(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    log_trade(db_path=db, window_slug="w3", side="up", entry_price=0.6, size=10.0,
              fees=0.024, model_p_up=0.7, market_p_up=0.58, edge=0.12,
              outcome="up", pnl=3.976, status="settled")
    counts = backfill(db)
    assert counts["missing"] == 1
    with sqlite3.connect(db) as c:
        row = c.execute(
            "SELECT signal_reference_price, signal_reference_source "
            "FROM trades WHERE window_slug='w3'"
        ).fetchone()
    assert row[1] == "missing"
    assert row[0] is None


def test_backfill_is_idempotent(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    _seed_trade_and_decision(db, "w4", "up", has_opp_bids=True)
    backfill(db)
    with sqlite3.connect(db) as c:
        first = c.execute("SELECT signal_reference_price, signal_reference_source "
                          "FROM trades WHERE window_slug='w4'").fetchone()
    counts2 = backfill(db)
    assert counts2["skipped"] == 1
    with sqlite3.connect(db) as c:
        second = c.execute("SELECT signal_reference_price, signal_reference_source "
                           "FROM trades WHERE window_slug='w4'").fetchone()
    assert first == second


def test_backfill_does_not_overwrite_live_rows(tmp_path):
    """Rows already tagged 'live' (from a real trade post-Task 4) are left alone."""
    db = str(tmp_path / "t.db")
    init_db(db)
    log_trade(db_path=db, window_slug="w5", side="up", entry_price=0.6, size=10.0,
              fees=0.024, model_p_up=0.7, market_p_up=0.58, edge=0.12,
              outcome="up", pnl=3.976, status="settled",
              signal_reference_price=0.59, signal_reference_source="live")
    backfill(db)
    with sqlite3.connect(db) as c:
        row = c.execute("SELECT signal_reference_price, signal_reference_source "
                        "FROM trades WHERE window_slug='w5'").fetchone()
    assert row == (0.59, "live")
