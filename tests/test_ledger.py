import os
import sqlite3
import tempfile

import pytest

from polypocket.ledger import (
    credit_paper_balance,
    deduct_paper_balance,
    get_daily_pnl,
    get_open_trade_by_window_slug,
    get_paper_balance,
    get_recent_trades,
    get_session_stats,
    find_duplicate_window_slugs,
    find_trade_by_window_slug,
    init_db,
    log_trade,
)


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    return path


def test_init_creates_tables():
    db_path = make_db()
    init_db(db_path)
    os.unlink(db_path)


def test_log_and_retrieve_trade():
    db_path = make_db()
    log_trade(
        db_path,
        window_slug="btc-5m-123",
        side="up",
        entry_price=0.575,
        size=10.0,
        fees=0.115,
        model_p_up=0.72,
        market_p_up=0.575,
        edge=0.145,
        outcome=None,
        pnl=None,
        status="open",
    )
    trades = get_recent_trades(db_path, limit=10)
    assert len(trades) == 1
    assert trades[0]["side"] == "up"
    assert trades[0]["entry_price"] == 0.575
    os.unlink(db_path)


def test_find_trade_by_window_slug_returns_existing_trade_row():
    db_path = make_db()
    log_trade(
        db_path,
        "btc-5m-123",
        "up",
        0.575,
        10.0,
        0.115,
        0.72,
        0.575,
        0.145,
        None,
        None,
        "open",
    )

    trade = find_trade_by_window_slug(db_path, "btc-5m-123")

    assert trade is not None
    assert trade["window_slug"] == "btc-5m-123"
    assert trade["status"] == "open"
    os.unlink(db_path)


def test_get_open_trade_by_window_slug_returns_none_for_settled_trade():
    db_path = make_db()
    log_trade(db_path, "w1", "up", 0.55, 10, 0.11, 0.7, 0.55, 0.15, "up", 3.4, "settled")

    trade = get_open_trade_by_window_slug(db_path, "w1")

    assert trade is None
    os.unlink(db_path)


def test_find_duplicate_window_slugs_reports_legacy_duplicates():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            window_slug TEXT NOT NULL,
            side TEXT NOT NULL,
            entry_price REAL NOT NULL,
            size REAL NOT NULL,
            fees REAL NOT NULL,
            model_p_up REAL,
            market_p_up REAL,
            edge REAL,
            outcome TEXT,
            pnl REAL,
            status TEXT NOT NULL DEFAULT 'open'
        );

        INSERT INTO trades (window_slug, side, entry_price, size, fees, status)
        VALUES ('dup-slug', 'up', 1.0, 1.0, 0.01, 'open');

        INSERT INTO trades (window_slug, side, entry_price, size, fees, status)
        VALUES ('dup-slug', 'down', 1.1, 1.0, 0.01, 'settled');

        INSERT INTO trades (window_slug, side, entry_price, size, fees, status)
        VALUES ('unique-slug', 'up', 1.2, 1.0, 0.01, 'open');
        """
    )
    conn.close()

    duplicates = find_duplicate_window_slugs(db_path)

    assert duplicates == ["dup-slug"]
    os.unlink(db_path)


def test_init_db_auto_cleans_legacy_duplicate_window_slugs():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            window_slug TEXT NOT NULL,
            side TEXT NOT NULL,
            entry_price REAL NOT NULL,
            size REAL NOT NULL,
            fees REAL NOT NULL,
            model_p_up REAL,
            market_p_up REAL,
            edge REAL,
            outcome TEXT,
            pnl REAL,
            status TEXT NOT NULL DEFAULT 'open'
        );

        INSERT INTO trades (window_slug, side, entry_price, size, fees, status)
        VALUES ('dup-slug', 'up', 1.0, 1.0, 0.01, 'open');

        INSERT INTO trades (window_slug, side, entry_price, size, fees, status)
        VALUES ('dup-slug', 'down', 1.1, 1.0, 0.01, 'settled');
        """
    )
    conn.close()

    init_db(db_path)

    check = sqlite3.connect(db_path)
    rows = check.execute("SELECT * FROM trades WHERE window_slug = 'dup-slug'").fetchall()
    check.close()
    assert len(rows) == 1
    os.unlink(db_path)

def test_init_db_enforces_unique_window_slug():
    db_path = make_db()
    log_trade(db_path, "unique-slug", "up", 0.55, 10, 0.11, 0.7, 0.55, 0.15, None, None, "open")

    try:
        with pytest.raises(sqlite3.IntegrityError):
            log_trade(db_path, "unique-slug", "down", 0.60, 10, 0.12, 0.6, 0.60, 0.0, None, None, "open")
    finally:
        os.unlink(db_path)


def test_daily_pnl():
    db_path = make_db()
    log_trade(db_path, "w1", "up", 0.55, 10, 0.11, 0.7, 0.55, 0.15, "up", 3.4, "settled")
    log_trade(db_path, "w2", "down", 0.50, 10, 0.10, 0.3, 0.50, 0.2, "up", -5.1, "settled")
    pnl = get_daily_pnl(db_path)
    assert abs(pnl - (-1.7)) < 0.01
    os.unlink(db_path)


def test_session_stats():
    db_path = make_db()
    log_trade(db_path, "w1", "up", 0.55, 10, 0.11, 0.7, 0.55, 0.15, "up", 3.0, "settled")
    log_trade(db_path, "w2", "down", 0.50, 10, 0.10, 0.3, 0.50, 0.2, "up", -5.0, "settled")
    log_trade(db_path, "w3", "up", 0.60, 10, 0.12, 0.8, 0.60, 0.2, "up", 2.8, "settled")
    stats = get_session_stats(db_path)
    assert stats["wins"] == 2
    assert stats["losses"] == 1
    assert stats["total"] == 3
    os.unlink(db_path)


def test_paper_balance():
    db_path = make_db()
    balance = get_paper_balance(db_path)
    assert balance == 1000.0
    deduct_paper_balance(db_path, 50.0)
    assert get_paper_balance(db_path) == 950.0
    credit_paper_balance(db_path, 60.0)
    assert get_paper_balance(db_path) == 1010.0
    os.unlink(db_path)


def test_init_creates_window_snapshots_table():
    db_path = make_db()
    conn = sqlite3.connect(db_path)
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='window_snapshots'"
    ).fetchall()
    conn.close()
    assert len(tables) == 1
    os.unlink(db_path)
