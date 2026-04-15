import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from polypocket.executor import TradeResult
from polypocket.feeds.polymarket import Window
from polypocket.ledger import init_db
from polypocket.signal import Signal


@pytest.mark.asyncio
async def test_bot_updates_stats_with_price_to_beat(tmp_path: Path):
    from polypocket.bot import Bot

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))

    bot = Bot(db_path=str(db_path))
    bot.binance.latest_price = 84250.0
    bot.signal_engine.evaluate = lambda **kwargs: None

    window = Window(
        condition_id="abc123",
        question="BTC Up or Down",
        up_token_id="tok_up",
        down_token_id="tok_down",
        end_time=time.time() + 180,
        slug="btc-updown-5m-123",
        price_to_beat=84198.0,
        up_ask=0.57,
        down_ask=0.43,
    )

    await bot._on_book_update(window, "up")

    assert bot.stats["window_open_price"] == 84198.0
    assert bot.stats["window_slug"] == "btc-updown-5m-123"


@pytest.mark.asyncio
async def test_bot_executes_once_per_window(tmp_path: Path, monkeypatch):
    from polypocket.bot import Bot

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))

    bot = Bot(db_path=str(db_path))
    bot.binance.latest_price = 84350.0
    bot.signal_engine.evaluate = lambda **kwargs: Signal(
        side="up",
        model_p_up=0.75,
        market_price=0.55,
        edge=0.20,
        up_edge=0.20,
        down_edge=-0.20,
    )
    bot.risk.check = lambda: (True, "")

    execute_mock = Mock(return_value=TradeResult(success=True, trade_id=1, pnl=None))
    monkeypatch.setattr("polypocket.bot.execute_paper_trade", execute_mock)

    window = Window(
        condition_id="abc123",
        question="BTC Up or Down",
        up_token_id="tok_up",
        down_token_id="tok_down",
        end_time=time.time() + 180,
        slug="btc-updown-5m-123",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
    )

    await bot._on_book_update(window, "up")
    await bot._on_book_update(window, "up")

    assert bot._window_traded is True
    assert bot._open_trade["trade_id"] == 1
    assert bot.stats["position"] is not None
    assert execute_mock.call_count == 1
