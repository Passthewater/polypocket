import os
import sqlite3
import tempfile

import pytest

from polypocket.executor import FillResult, TradeResult, execute_paper_trade, execute_live_trade
from polypocket.ledger import (
    find_trade_by_window_slug,
    get_paper_balance,
    init_db,
    log_trade as persist_trade,
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

    def submit_fok(self, side, price, size, token_id, client_order_id):
        self.calls.append({
            "side": side, "price": price, "size": size,
            "token_id": token_id, "client_order_id": client_order_id,
        })
        return FillResult(
            status="filled", order_id=f"ord-{client_order_id}",
            filled_size=size, avg_price=price, error=None,
        )

    def get_usdc_balance(self):
        return self._balance


class RejectingLiveOrderClient:
    def __init__(self, balance=1000.0, error="no match"):
        self.calls = 0
        self._balance = balance
        self._error = error

    def submit_fok(self, side, price, size, token_id, client_order_id):
        self.calls += 1
        return FillResult(
            status="rejected", order_id=None,
            filled_size=0.0, avg_price=None, error=self._error,
        )

    def get_usdc_balance(self):
        return self._balance


def test_live_trade_uses_deterministic_client_order_id():
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
        client=client,
    )

    assert result.success is True
    assert client.calls == [
        {
            "side": "down",
            "price": 0.45,
            "size": 5.0,
            "token_id": "TKN-DOWN",
            "client_order_id": "window-eth-5m-999",
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
        client=client,
    )
    assert first.success is True
    assert client.calls == [
        {
            "side": "up",
            "price": 0.51,
            "size": 7.0,
            "token_id": "TKN-UP",
            "client_order_id": "window-sol-5m-dup",
        }
    ]

    second = execute_live_trade(
        db_path=db_path,
        signal=signal,
        entry_price=0.51,
        size=7.0,
        window_slug="sol-5m-dup",
        token_id="TKN-UP",
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

    def get_usdc_balance(self):
        return 0.50


def test_live_trade_insufficient_balance_writes_no_row():
    db_path = make_db()
    signal = Signal(side="up", model_p_up=0.72, market_price=0.51,
                    edge=0.21, up_edge=0.21, down_edge=-0.21)
    result = execute_live_trade(
        db_path=db_path, signal=signal, entry_price=0.51, size=7.0,
        window_slug="btc-5m-nb", token_id="TKN-UP",
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
        window_slug="btc-5m-fill", token_id="TKN-UP", client=client,
    )
    assert result.success is True
    trade = find_trade_by_window_slug(db_path, "btc-5m-fill")
    assert trade["status"] == "open"
    assert trade["external_order_id"] == "ord-window-btc-5m-fill"
    os.unlink(db_path)


def test_live_trade_rejected_marks_trade_rejected_with_error():
    db_path = make_db()
    signal = Signal(side="down", model_p_up=0.32, market_price=0.44,
                    edge=0.12, up_edge=-0.12, down_edge=0.12)
    client = RejectingLiveOrderClient(error="no match")
    result = execute_live_trade(
        db_path=db_path, signal=signal, entry_price=0.44, size=4.0,
        window_slug="btc-5m-rej", token_id="TKN-DOWN", client=client,
    )
    assert result.success is False
    assert result.error == "no match"
    trade = find_trade_by_window_slug(db_path, "btc-5m-rej")
    assert trade["status"] == "rejected"
    assert trade["error"] == "no match"
    assert trade["external_order_id"] is None
    os.unlink(db_path)


def test_live_trade_client_error_marks_trade_rejected():
    class ErroringClient:
        def submit_fok(self, **kwargs):
            return FillResult(status="error", order_id=None, filled_size=0.0,
                              avg_price=None, error="network: timeout")
        def get_usdc_balance(self):
            return 1000.0

    db_path = make_db()
    signal = Signal(side="up", model_p_up=0.72, market_price=0.51,
                    edge=0.21, up_edge=0.21, down_edge=-0.21)
    result = execute_live_trade(
        db_path=db_path, signal=signal, entry_price=0.51, size=7.0,
        window_slug="btc-5m-err", token_id="TKN-UP", client=ErroringClient(),
    )
    assert result.success is False
    assert "network" in result.error
    trade = find_trade_by_window_slug(db_path, "btc-5m-err")
    assert trade["status"] == "rejected"
    os.unlink(db_path)
