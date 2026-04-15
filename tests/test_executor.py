import os
import sqlite3
import tempfile

import pytest

from polypocket.executor import TradeResult, execute_paper_trade, execute_live_trade
from polypocket.ledger import get_paper_balance, find_trade_by_window_slug, init_db
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


class RecordingLiveOrderClient:
    def __init__(self):
        self.calls = []

    def submit_fok(self, side, price, size, client_order_id):
        self.calls.append(
            {
                "side": side,
                "price": price,
                "size": size,
                "client_order_id": client_order_id,
            }
        )
        return "live-order-123"


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
        client=client,
    )

    assert result.success is True
    assert client.calls == [
        {
            "side": "down",
            "price": 0.45,
            "size": 5.0,
            "client_order_id": "window-eth-5m-999",
        }
    ]
    assert result.trade_id == find_trade_by_window_slug(db_path, "eth-5m-999")["id"]
    os.unlink(db_path)
