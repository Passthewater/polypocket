import os
import tempfile
from unittest.mock import Mock

from polypocket.ledger import init_db, log_trade, set_live_starting_balance, update_trade
from polypocket.risk import RiskManager, check_wallet_divergence, compute_expected_usdc_balance


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


# ---------------------------------------------------------------------------
# Wallet-balance watchdog tests (Step 6)
# ---------------------------------------------------------------------------

def _make_live_db() -> str:
    """Create a temp db and return its path."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    return path


def test_check_wallet_divergence_passes_when_balance_matches_ledger(monkeypatch):
    """Expected $50, actual $50 — should pass."""
    import polypocket.risk as risk_module

    db_path = _make_live_db()
    monkeypatch.setattr(risk_module, "TRADING_MODE", "live")
    set_live_starting_balance(db_path, 50.0)

    # No trades in ledger so expected = 50.0.
    client = Mock()
    client.get_usdc_balance.return_value = 50.0

    ok, reason = check_wallet_divergence(client, db_path)
    assert ok is True
    assert reason is None
    client.get_usdc_balance.assert_called_once()
    os.unlink(db_path)


def test_check_wallet_divergence_halts_when_actual_below_expected_by_threshold(monkeypatch):
    """Expected $50, actual $44 — divergence $6 > threshold $5 → halt."""
    import polypocket.risk as risk_module

    db_path = _make_live_db()
    monkeypatch.setattr(risk_module, "TRADING_MODE", "live")
    # Force threshold to $5 (the default) explicitly.
    monkeypatch.setattr(risk_module, "WALLET_LEDGER_DIVERGENCE_HALT_USDC", 5.0)
    set_live_starting_balance(db_path, 50.0)

    client = Mock()
    client.get_usdc_balance.return_value = 44.0

    ok, reason = check_wallet_divergence(client, db_path)
    assert ok is False
    assert reason is not None
    assert "wallet divergence" in reason
    assert "50.00" in reason  # expected
    assert "44.00" in reason  # actual
    os.unlink(db_path)


def test_check_wallet_divergence_tolerates_small_drift(monkeypatch):
    """Expected $50, actual $48.50 — divergence $1.50 < threshold $5 → pass."""
    import polypocket.risk as risk_module

    db_path = _make_live_db()
    monkeypatch.setattr(risk_module, "TRADING_MODE", "live")
    monkeypatch.setattr(risk_module, "WALLET_LEDGER_DIVERGENCE_HALT_USDC", 5.0)
    set_live_starting_balance(db_path, 50.0)

    client = Mock()
    client.get_usdc_balance.return_value = 48.50

    ok, reason = check_wallet_divergence(client, db_path)
    assert ok is True
    assert reason is None
    os.unlink(db_path)


def test_check_wallet_divergence_skipped_in_paper_mode(monkeypatch):
    """TRADING_MODE=paper → no-op: client is never called."""
    import polypocket.risk as risk_module

    db_path = _make_live_db()
    monkeypatch.setattr(risk_module, "TRADING_MODE", "paper")

    client = Mock()
    ok, reason = check_wallet_divergence(client, db_path)

    assert ok is True
    assert reason is None
    client.get_usdc_balance.assert_not_called()
    os.unlink(db_path)


def test_compute_expected_usdc_balance_does_not_subtract_placed_rows():
    """Regression: placed rows must NOT reduce expected balance.

    Polymarket CLOB does NOT escrow pUSD on resting maker orders — the proxy
    account is debited only at fill time (status transitions to 'open').
    Including 'placed' in the cost SUM would create two bugs:
      1. Expected drops artificially after placement → false halt at first tick.
      2. When a silent fill lands, actual drops but expected was already low →
         the divergence signal cancels out and the watchdog never fires.

    This test seeds a $100 starting balance with one placed row
    (entry_price=0.5, size=10 → $5 cost if erroneously included).
    The correct expected balance is $100.0, not $95.0.
    """
    import pytest
    db_path = _make_live_db()
    set_live_starting_balance(db_path, 100.0)

    log_trade(
        db_path,
        "placed-window",
        "up",
        0.5,   # entry_price
        10.0,  # size
        0.0,   # fees
        0.75,  # model_p_up
        0.55,  # market_p_up
        0.20,  # edge
        None,  # outcome
        None,  # pnl
        "placed",
    )

    result = compute_expected_usdc_balance(db_path)
    assert result == pytest.approx(100.0), (
        f"Expected $100.0 (placed rows excluded) but got ${result:.2f}. "
        "Placed rows must not reduce expected balance — Polymarket does not "
        "escrow pUSD on resting maker orders."
    )
    os.unlink(db_path)
