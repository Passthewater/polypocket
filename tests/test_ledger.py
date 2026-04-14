import os
import tempfile

from polypocket.ledger import (
    credit_paper_balance,
    deduct_paper_balance,
    get_daily_pnl,
    get_paper_balance,
    get_recent_trades,
    get_session_stats,
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
