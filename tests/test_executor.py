import logging
import os
import sqlite3
import tempfile

import pytest
from unittest.mock import MagicMock

from polypocket.executor import (
    FillResult,
    SettlementInfo,
    TradeResult,
    execute_paper_trade,
    execute_live_trade,
    reconcile_recovered_trade,
    settle_live_trade,
)
from polypocket.ledger import (
    find_trade_by_window_slug,
    get_paper_balance,
    init_db,
    log_trade as persist_trade,
    update_trade,
)
from polypocket.signal import Signal


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    return path


def test_paper_trade_up_win():
    db_path = make_db()
    signal = Signal(
        side="up",
        model_p_up=0.75,
        market_price=0.55,
        edge=0.20,
        up_edge=0.20,
        down_edge=-0.20,
    )
    result = execute_paper_trade(
        db_path=db_path,
        signal=signal,
        entry_price=0.55,
        size=10.0,
        window_slug="btc-5m-123",
        outcome="up",
    )
    assert result.success is True
    assert result.pnl > 0
    trade = find_trade_by_window_slug(db_path, "btc-5m-123")
    assert trade["side"] == "up"
    assert trade["status"] == "settled"
    assert trade["model_p_up"] == pytest.approx(0.75)
    assert trade["market_p_up"] == pytest.approx(0.55)
    balance = get_paper_balance(db_path)
    assert balance > 990.0
    os.unlink(db_path)


def test_paper_trade_up_loss():
    db_path = make_db()
    signal = Signal(
        side="up",
        model_p_up=0.75,
        market_price=0.55,
        edge=0.20,
        up_edge=0.20,
        down_edge=-0.20,
    )
    result = execute_paper_trade(
        db_path=db_path,
        signal=signal,
        entry_price=0.55,
        size=10.0,
        window_slug="btc-5m-456",
        outcome="down",
    )
    assert result.success is True
    assert result.pnl < 0
    balance = get_paper_balance(db_path)
    assert balance < 1000.0
    os.unlink(db_path)


def test_paper_trade_insufficient_balance():
    db_path = make_db()
    signal = Signal(
        side="up",
        model_p_up=0.75,
        market_price=0.55,
        edge=0.20,
        up_edge=0.20,
        down_edge=-0.20,
    )
    result = execute_paper_trade(
        db_path=db_path,
        signal=signal,
        entry_price=0.55,
        size=20000.0,
        window_slug="btc-5m-789",
        outcome="up",
    )
    assert result.success is False
    assert "balance" in result.error.lower()
    os.unlink(db_path)


def test_duplicate_paper_trade_rejection_does_not_reduce_balance():
    db_path = make_db()
    signal = Signal(
        side="up",
        model_p_up=0.75,
        market_price=0.55,
        edge=0.20,
        up_edge=0.20,
        down_edge=-0.20,
    )

    first = execute_paper_trade(
        db_path=db_path,
        signal=signal,
        entry_price=0.55,
        size=10.0,
        window_slug="btc-5m-dup",
        outcome="up",
    )
    assert first.success is True
    balance_after_first = get_paper_balance(db_path)

    second = execute_paper_trade(
        db_path=db_path,
        signal=signal,
        entry_price=0.55,
        size=10.0,
        window_slug="btc-5m-dup",
        outcome="up",
    )

    assert second.success is False
    assert second.error == "window-already-consumed"
    assert second.trade_id == first.trade_id
    assert find_trade_by_window_slug(db_path, "btc-5m-dup")["status"] == "settled"
    assert get_paper_balance(db_path) == balance_after_first
    os.unlink(db_path)


def test_paper_trade_race_on_insert_returns_consumed_existing_trade(monkeypatch):
    db_path = make_db()
    signal = Signal(
        side="up",
        model_p_up=0.75,
        market_price=0.55,
        edge=0.20,
        up_edge=0.20,
        down_edge=-0.20,
    )
    balance_before = get_paper_balance(db_path)

    def losing_race(**kwargs):
        persist_trade(**kwargs)
        raise sqlite3.IntegrityError("UNIQUE constraint failed: trades.window_slug")

    monkeypatch.setattr("polypocket.executor.log_trade", losing_race)

    result = execute_paper_trade(
        db_path=db_path,
        signal=signal,
        entry_price=0.55,
        size=10.0,
        window_slug="btc-5m-race",
        outcome="up",
    )

    trade = find_trade_by_window_slug(db_path, "btc-5m-race")
    assert result == TradeResult(
        success=False,
        trade_id=trade["id"],
        pnl=None,
        error="window-already-consumed",
    )
    assert trade["status"] == "settled"
    assert get_paper_balance(db_path) == balance_before
    os.unlink(db_path)


class RecordingLiveOrderClient:
    def __init__(self, balance=1000.0):
        self.calls = []
        self._balance = balance

    def submit_fok(self, side, price, size, token_id, condition_id):
        self.calls.append({
            "side": side, "price": price, "size": size,
            "token_id": token_id, "condition_id": condition_id,
        })
        return FillResult(
            status="filled", order_id=f"ord-{len(self.calls)}",
            filled_size=size, avg_price=price, error=None,
        )

    def submit_ioc(self, side, price, size, token_id, condition_id):
        self.calls.append({
            "side": side, "price": price, "size": size,
            "token_id": token_id, "condition_id": condition_id,
        })
        return FillResult(
            status="filled", order_id=f"ord-{len(self.calls)}",
            filled_size=size, avg_price=price, error=None,
        )

    def get_usdc_balance(self):
        return self._balance


class RejectingLiveOrderClient:
    def __init__(self, balance=1000.0, error="no match"):
        self.calls = 0
        self._balance = balance
        self._error = error

    def submit_fok(self, side, price, size, token_id, condition_id):
        self.calls += 1
        return FillResult(
            status="rejected", order_id=None,
            filled_size=0.0, avg_price=None, error=self._error,
        )

    def submit_ioc(self, side, price, size, token_id, condition_id):
        self.calls += 1
        return FillResult(
            status="rejected", order_id=None,
            filled_size=0.0, avg_price=None, error=self._error,
        )

    def get_usdc_balance(self):
        return self._balance


def test_live_trade_threads_args_to_client():
    db_path = make_db()
    signal = Signal(
        side="down",
        model_p_up=0.25,
        market_price=0.45,
        edge=0.15,
        up_edge=-0.15,
        down_edge=0.15,
    )
    client = RecordingLiveOrderClient()

    result = execute_live_trade(
        db_path=db_path,
        signal=signal,
        entry_price=0.45,
        size=5.0,
        window_slug="eth-5m-999",
        token_id="TKN-DOWN",
        condition_id="0xcond",
        client=client,
    )

    assert result.success is True
    assert client.calls == [
        {
            "side": "down",
            "price": 0.45,
            "size": 5.0,
            "token_id": "TKN-DOWN",
            "condition_id": "0xcond",
        }
    ]
    trade = find_trade_by_window_slug(db_path, "eth-5m-999")
    assert result.trade_id == trade["id"]
    assert trade["status"] == "open"
    assert trade["side"] == "down"
    assert trade["model_p_up"] == pytest.approx(0.25)
    assert trade["market_p_up"] == pytest.approx(0.45)
    os.unlink(db_path)


def test_duplicate_live_trade_rejection_does_not_submit_again():
    db_path = make_db()
    signal = Signal(
        side="up",
        model_p_up=0.72,
        market_price=0.51,
        edge=0.21,
        up_edge=0.21,
        down_edge=-0.21,
    )
    client = RecordingLiveOrderClient()

    first = execute_live_trade(
        db_path=db_path,
        signal=signal,
        entry_price=0.51,
        size=7.0,
        window_slug="sol-5m-dup",
        token_id="TKN-UP",
        condition_id="0xcond",
        client=client,
    )
    assert first.success is True
    assert len(client.calls) == 1

    second = execute_live_trade(
        db_path=db_path,
        signal=signal,
        entry_price=0.51,
        size=7.0,
        window_slug="sol-5m-dup",
        token_id="TKN-UP",
        condition_id="0xcond",
        client=client,
    )

    assert second.success is False
    assert second.error == "window-already-consumed"
    assert second.trade_id == first.trade_id
    assert len(client.calls) == 1
    os.unlink(db_path)


def test_live_trade_race_on_insert_returns_consumed_existing_trade(monkeypatch):
    db_path = make_db()
    signal = Signal(
        side="up",
        model_p_up=0.72,
        market_price=0.51,
        edge=0.21,
        up_edge=0.21,
        down_edge=-0.21,
    )
    client = RecordingLiveOrderClient()

    def losing_race(**kwargs):
        persist_trade(**kwargs)
        raise sqlite3.IntegrityError("UNIQUE constraint failed: trades.window_slug")

    monkeypatch.setattr("polypocket.executor.log_trade", losing_race)

    result = execute_live_trade(
        db_path=db_path,
        signal=signal,
        entry_price=0.51,
        size=7.0,
        window_slug="sol-5m-race",
        token_id="TKN-UP",
        condition_id="0xcond",
        client=client,
    )

    trade = find_trade_by_window_slug(db_path, "sol-5m-race")
    assert result == TradeResult(
        success=False,
        trade_id=trade["id"],
        pnl=None,
        error="window-already-consumed",
    )
    assert trade["status"] == "reserved"
    assert client.calls == []
    os.unlink(db_path)


class InsufficientBalanceClient:
    def submit_fok(self, **kwargs):
        raise AssertionError("submit_fok must not be called when balance check fails")

    def submit_ioc(self, **kwargs):
        raise AssertionError("submit_ioc must not be called when balance check fails")

    def get_usdc_balance(self):
        return 0.50


def test_live_trade_insufficient_balance_writes_no_row():
    db_path = make_db()
    signal = Signal(side="up", model_p_up=0.72, market_price=0.51,
                    edge=0.21, up_edge=0.21, down_edge=-0.21)
    result = execute_live_trade(
        db_path=db_path, signal=signal, entry_price=0.51, size=7.0,
        window_slug="btc-5m-nb", token_id="TKN-UP", condition_id="0xcond",
        client=InsufficientBalanceClient(),
    )
    assert result.success is False
    assert result.error == "insufficient-balance"
    assert find_trade_by_window_slug(db_path, "btc-5m-nb") is None
    os.unlink(db_path)


def test_live_trade_filled_writes_external_order_id():
    db_path = make_db()
    signal = Signal(side="up", model_p_up=0.72, market_price=0.51,
                    edge=0.21, up_edge=0.21, down_edge=-0.21)
    client = RecordingLiveOrderClient()
    result = execute_live_trade(
        db_path=db_path, signal=signal, entry_price=0.51, size=7.0,
        window_slug="btc-5m-fill", token_id="TKN-UP", condition_id="0xcond", client=client,
    )
    assert result.success is True
    trade = find_trade_by_window_slug(db_path, "btc-5m-fill")
    assert trade["status"] == "open"
    assert trade["external_order_id"] == "ord-1"
    os.unlink(db_path)


def test_live_trade_rejected_marks_trade_rejected_with_error():
    db_path = make_db()
    signal = Signal(side="down", model_p_up=0.32, market_price=0.44,
                    edge=0.12, up_edge=-0.12, down_edge=0.12)
    client = RejectingLiveOrderClient(error="no match")
    result = execute_live_trade(
        db_path=db_path, signal=signal, entry_price=0.44, size=4.0,
        window_slug="btc-5m-rej", token_id="TKN-DOWN", condition_id="0xcond", client=client,
    )
    assert result.success is False
    assert result.error == "no match"
    trade = find_trade_by_window_slug(db_path, "btc-5m-rej")
    assert trade["status"] == "rejected"
    assert trade["error"] == "no match"
    assert trade["external_order_id"] is None
    os.unlink(db_path)


class SettlingLiveOrderClient:
    """Live client stub that also exposes get_settlement_info."""
    def __init__(self, settlements: dict[str, SettlementInfo], balance: float = 1000.0):
        self.calls = []
        self._balance = balance
        self._settlements = settlements
        self.settlement_lookups: list[str] = []

    def submit_fok(self, side, price, size, token_id, condition_id):
        self.calls.append({"side": side, "price": price, "size": size,
                           "token_id": token_id, "condition_id": condition_id})
        return FillResult(status="filled", order_id=f"ord-{len(self.calls)}",
                          filled_size=size, avg_price=price, error=None)

    def submit_ioc(self, side, price, size, token_id, condition_id):
        self.calls.append({"side": side, "price": price, "size": size,
                           "token_id": token_id, "condition_id": condition_id})
        return FillResult(status="filled", order_id=f"ord-{len(self.calls)}",
                          filled_size=size, avg_price=price, error=None)

    def get_usdc_balance(self):
        return self._balance

    def get_settlement_info(self, order_id):
        self.settlement_lookups.append(order_id)
        return self._settlements[order_id]


def _seed_open_live_trade(db_path, window_slug, side, order_id):
    trade_id = persist_trade(
        db_path=db_path,
        window_slug=window_slug,
        side=side,
        entry_price=0.55,
        size=10.0,
        fees=0.0,
        model_p_up=0.7,
        market_p_up=0.55,
        edge=0.15,
        outcome=None,
        pnl=None,
        status="open",
    )
    update_trade(db_path, trade_id, outcome=None, pnl=None, status="open",
                 external_order_id=order_id)
    return trade_id


def test_settle_live_trade_win_writes_real_pnl():
    db_path = make_db()
    trade_id = _seed_open_live_trade(db_path, "btc-5m-livewin", "up", "ord-1")
    client = SettlingLiveOrderClient(
        {"ord-1": SettlementInfo(shares_held=9.0, cost_usdc=5.5)}
    )

    pnl = settle_live_trade(
        db_path=db_path, trade_id=trade_id, side="up", outcome="up",
        order_id="ord-1", client=client,
    )

    assert pnl == pytest.approx(9.0 - 5.5)
    assert client.settlement_lookups == ["ord-1"]
    trade = find_trade_by_window_slug(db_path, "btc-5m-livewin")
    assert trade["status"] == "settled"
    assert trade["outcome"] == "up"
    assert trade["pnl"] == pytest.approx(3.5)
    os.unlink(db_path)


def test_settle_live_trade_loss_writes_negative_pnl():
    db_path = make_db()
    trade_id = _seed_open_live_trade(db_path, "btc-5m-liveloss", "up", "ord-7")
    client = SettlingLiveOrderClient(
        {"ord-7": SettlementInfo(shares_held=9.0, cost_usdc=5.5)}
    )

    pnl = settle_live_trade(
        db_path=db_path, trade_id=trade_id, side="up", outcome="down",
        order_id="ord-7", client=client,
    )

    assert pnl == pytest.approx(-5.5)
    trade = find_trade_by_window_slug(db_path, "btc-5m-liveloss")
    assert trade["status"] == "settled"
    assert trade["outcome"] == "down"
    assert trade["pnl"] == pytest.approx(-5.5)
    os.unlink(db_path)


def test_settle_live_trade_without_order_id_falls_back_to_unreconciled():
    db_path = make_db()
    trade_id = _seed_open_live_trade(db_path, "btc-5m-legacy", "up", "will-be-cleared")
    update_trade(db_path, trade_id, outcome=None, pnl=None, status="open")
    # simulate a legacy row: clear the order id via a direct UPDATE
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE trades SET external_order_id = NULL WHERE id = ?", (trade_id,))
    conn.commit()
    conn.close()

    client = SettlingLiveOrderClient({})
    pnl = settle_live_trade(
        db_path=db_path, trade_id=trade_id, side="up", outcome="up",
        order_id=None, client=client,
    )
    assert pnl is None
    assert client.settlement_lookups == []
    trade = find_trade_by_window_slug(db_path, "btc-5m-legacy")
    assert trade["status"] == "settled"
    assert trade["outcome"] == "up"
    assert trade["pnl"] is None
    os.unlink(db_path)


def test_settle_live_trade_clob_error_falls_back_to_unreconciled():
    db_path = make_db()
    trade_id = _seed_open_live_trade(db_path, "btc-5m-clobfail", "down", "ord-x")

    class ErroringClient:
        def get_settlement_info(self, order_id):
            raise RuntimeError("CLOB 500")

    pnl = settle_live_trade(
        db_path=db_path, trade_id=trade_id, side="down", outcome="down",
        order_id="ord-x", client=ErroringClient(),
    )
    assert pnl is None
    trade = find_trade_by_window_slug(db_path, "btc-5m-clobfail")
    assert trade["status"] == "settled"
    assert trade["pnl"] is None
    os.unlink(db_path)


def test_live_trade_client_error_marks_trade_rejected():
    class ErroringClient:
        def submit_fok(self, **kwargs):
            return FillResult(status="error", order_id=None, filled_size=0.0,
                              avg_price=None, error="network: timeout")
        def submit_ioc(self, **kwargs):
            return FillResult(status="error", order_id=None, filled_size=0.0,
                              avg_price=None, error="network: timeout")
        def get_usdc_balance(self):
            return 1000.0

    db_path = make_db()
    signal = Signal(side="up", model_p_up=0.72, market_price=0.51,
                    edge=0.21, up_edge=0.21, down_edge=-0.21)
    result = execute_live_trade(
        db_path=db_path, signal=signal, entry_price=0.51, size=7.0,
        window_slug="btc-5m-err", token_id="TKN-UP", condition_id="0xcond", client=ErroringClient(),
    )
    assert result.success is False
    assert "network" in result.error
    trade = find_trade_by_window_slug(db_path, "btc-5m-err")
    assert trade["status"] == "rejected"
    os.unlink(db_path)


def _seed_reserved_trade(db_path, window_slug, order_id=None):
    """Seed a reserved live trade, optionally with an external_order_id."""
    trade_id = persist_trade(
        db_path=db_path,
        window_slug=window_slug,
        side="up",
        entry_price=0.55,
        size=10.0,
        fees=0.0,
        model_p_up=0.7,
        market_p_up=0.55,
        edge=0.15,
        outcome=None,
        pnl=None,
        status="reserved",
    )
    if order_id is not None:
        update_trade(db_path, trade_id, outcome=None, pnl=None, status="reserved",
                     external_order_id=order_id)
    return trade_id


def test_reconcile_matched_marks_trade_open():
    from unittest.mock import Mock
    db_path = make_db()
    trade_id = _seed_reserved_trade(db_path, "btc-5m-rec-matched", order_id="0xabc")
    trade = find_trade_by_window_slug(db_path, "btc-5m-rec-matched")

    client = Mock()
    client.get_order_status.return_value = {"status": "MATCHED"}

    result = reconcile_recovered_trade(db_path, trade, client)

    assert result == "open"
    assert find_trade_by_window_slug(db_path, "btc-5m-rec-matched")["status"] == "open"
    os.unlink(db_path)


def test_reconcile_canceled_marks_trade_rejected():
    from unittest.mock import Mock
    db_path = make_db()
    trade_id = _seed_reserved_trade(db_path, "btc-5m-rec-canceled", order_id="0xabc")
    trade = find_trade_by_window_slug(db_path, "btc-5m-rec-canceled")

    client = Mock()
    client.get_order_status.return_value = {"status": "CANCELED"}

    result = reconcile_recovered_trade(db_path, trade, client)

    assert result == "rejected"
    assert find_trade_by_window_slug(db_path, "btc-5m-rec-canceled")["status"] == "rejected"
    os.unlink(db_path)


def test_reconcile_unmatched_marks_trade_rejected():
    from unittest.mock import Mock
    db_path = make_db()
    trade_id = _seed_reserved_trade(db_path, "btc-5m-rec-unmatched", order_id="0xabc")
    trade = find_trade_by_window_slug(db_path, "btc-5m-rec-unmatched")

    client = Mock()
    client.get_order_status.return_value = {"status": "UNMATCHED"}

    result = reconcile_recovered_trade(db_path, trade, client)

    assert result == "rejected"
    assert find_trade_by_window_slug(db_path, "btc-5m-rec-unmatched")["status"] == "rejected"
    os.unlink(db_path)


def test_reconcile_without_external_order_id_is_noop():
    from unittest.mock import Mock
    db_path = make_db()
    trade_id = _seed_reserved_trade(db_path, "btc-5m-rec-noid", order_id=None)
    trade = find_trade_by_window_slug(db_path, "btc-5m-rec-noid")
    assert trade["external_order_id"] is None

    client = Mock()

    result = reconcile_recovered_trade(db_path, trade, client)

    assert result == "reserved"
    client.get_order_status.assert_not_called()
    assert find_trade_by_window_slug(db_path, "btc-5m-rec-noid")["status"] == "reserved"
    os.unlink(db_path)


def test_reconcile_clob_error_preserves_local_status():
    from unittest.mock import Mock
    db_path = make_db()
    trade_id = _seed_reserved_trade(db_path, "btc-5m-rec-err", order_id="0xabc")
    trade = find_trade_by_window_slug(db_path, "btc-5m-rec-err")

    client = Mock()
    client.get_order_status.side_effect = Exception("boom")

    result = reconcile_recovered_trade(db_path, trade, client)

    assert result == "reserved"
    assert find_trade_by_window_slug(db_path, "btc-5m-rec-err")["status"] == "reserved"
    os.unlink(db_path)


def _sample_signal():
    return Signal(
        side="up",
        model_p_up=0.72,
        market_price=0.51,
        edge=0.21,
        up_edge=0.21,
        down_edge=-0.21,
    )


def test_execute_live_trade_uses_submit_ioc():
    """Verify executor calls submit_ioc, not submit_fok."""
    db_path = make_db()
    signal = _sample_signal()
    client = MagicMock()
    client.get_usdc_balance.return_value = 100.0
    client.submit_ioc.return_value = FillResult(
        status="filled", order_id="abc",
        filled_size=7.0, avg_price=0.51, error=None,
    )

    result = execute_live_trade(
        db_path=db_path, signal=signal, entry_price=0.51,
        size=7.0, window_slug="w1", token_id="T", condition_id="C",
        client=client,
    )

    assert result.success
    client.submit_ioc.assert_called_once()
    client.submit_fok.assert_not_called()
    os.unlink(db_path)


def test_execute_live_trade_partial_fill_persists_actual_size():
    """Partial fill: ledger row reflects filled_size, not requested size."""
    db_path = make_db()
    signal = _sample_signal()
    client = MagicMock()
    client.get_usdc_balance.return_value = 100.0
    client.submit_ioc.return_value = FillResult(
        status="filled", order_id="abc",
        filled_size=3.5, avg_price=0.52, error=None,
    )

    result = execute_live_trade(
        db_path=db_path, signal=signal, entry_price=0.51,
        size=7.0, window_slug="w2", token_id="T", condition_id="C",
        client=client,
    )

    assert result.success
    row = find_trade_by_window_slug(db_path, "w2")
    assert row["size"] == pytest.approx(3.5)
    assert row["entry_price"] == pytest.approx(0.52)
    os.unlink(db_path)
