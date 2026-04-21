import os
import tempfile

from polypocket.ledger import init_db, log_trade, update_trade
from polypocket.risk import RiskManager


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    return path


def test_risk_allows_normal_trade():
    db_path = make_db()
    manager = RiskManager(db_path=db_path)
    ok, reason = manager.check()
    assert ok is True
    assert reason == ""
    os.unlink(db_path)


def test_risk_blocks_after_max_daily_loss():
    db_path = make_db()
    for index in range(6):
        log_trade(
            db_path,
            f"w{index}",
            "up",
            0.5,
            20,
            0.2,
            0.6,
            0.5,
            0.1,
            "down",
            -10.2,
            "settled",
        )
    manager = RiskManager(db_path=db_path)
    ok, reason = manager.check()
    assert ok is False
    assert "daily loss" in reason.lower()
    os.unlink(db_path)


def test_risk_blocks_after_consecutive_losses():
    db_path = make_db()
    manager = RiskManager(db_path=db_path)
    for _ in range(5):
        manager.record_loss()
    ok, reason = manager.check()
    assert ok is False
    assert "consecutive" in reason.lower()
    os.unlink(db_path)


def test_risk_resets_consecutive_on_win():
    db_path = make_db()
    manager = RiskManager(db_path=db_path)
    for _ in range(4):
        manager.record_loss()
    manager.record_win()
    ok, reason = manager.check()
    assert ok is True
    os.unlink(db_path)


def test_risk_blocks_mixed_paper_and_live_losses():
    """Mixed paper+live ledger trips MAX_DAILY_LOSS.

    Paper alone (-$30) and live alone (-$30) each stay under the $50 limit;
    combined (-$60) must trip. This proves RiskManager treats live rows
    identically to paper rows via get_daily_pnl.

    The MAX_CONSECUTIVE_LOSSES path is covered separately: risk.RiskManager
    uses an in-memory counter fed by record_loss(), and
    tests/test_bot.py::test_poll_pending_settlements_live_writes_real_pnl
    asserts the live settlement path calls record_loss identically to paper.
    """
    db_path = make_db()

    # Insert 3 paper-style rows (no external_order_id).
    for i in range(3):
        log_trade(
            db_path,
            f"paper-{i}",
            "up",
            0.5,
            20,
            0.2,
            0.6,
            0.5,
            0.1,
            "down",
            -10.0,
            "settled",
        )

    # Insert 3 live-style rows, then stamp each with an external_order_id.
    for i in range(3):
        trade_id = log_trade(
            db_path,
            f"live-{i}",
            "up",
            0.5,
            20,
            0.2,
            0.6,
            0.5,
            0.1,
            "down",
            -10.0,
            "settled",
        )
        update_trade(
            db_path,
            trade_id,
            outcome="down",
            pnl=-10.0,
            status="settled",
            external_order_id=f"clob-{i}",
        )

    ok, reason = RiskManager(db_path=db_path).check()
    assert ok is False
    assert "daily loss" in reason.lower()

    os.unlink(db_path)
