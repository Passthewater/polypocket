import os
import tempfile

from polypocket.ledger import init_db, log_trade
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
