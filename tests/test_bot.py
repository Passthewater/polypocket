import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from polypocket.config import FEE_RATE
from polypocket.executor import TradeResult
from polypocket.feeds.polymarket import Window
from polypocket.ledger import init_db, log_trade
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
    assert bot.stats["up_ask"] == 0.55
    assert bot.stats["down_ask"] == 0.45
    assert bot.stats["quote_status"] == "valid"
    assert bot.stats["execution_status"] == "open"
    assert execute_mock.call_count == 1


@pytest.mark.asyncio
async def test_bot_skips_one_sided_book_and_sets_quote_status(tmp_path: Path, monkeypatch):
    from polypocket.bot import Bot

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))

    bot = Bot(db_path=str(db_path))
    bot.binance.latest_price = 84350.0

    evaluate_mock = Mock(return_value=None)
    bot.signal_engine.evaluate = evaluate_mock
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
        down_ask=None,
    )

    await bot._on_book_update(window, "up")

    assert bot.stats["up_ask"] == 0.55
    assert bot.stats["down_ask"] is None
    assert bot.stats["quote_status"] == "missing-side"
    assert bot.stats["execution_status"] == "skipped"
    assert bot._open_trade is None
    evaluate_mock.assert_not_called()
    execute_mock.assert_not_called()


@pytest.mark.asyncio
async def test_bot_recovers_existing_open_trade_for_active_slug(tmp_path: Path, monkeypatch):
    from polypocket.bot import Bot

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))

    trade_id = log_trade(
        db_path=str(db_path),
        window_slug="btc-updown-5m-123",
        side="up",
        entry_price=0.55,
        size=10.0,
        fees=0.10,
        model_p_up=0.75,
        market_p_up=0.55,
        edge=0.20,
        outcome=None,
        pnl=None,
        status="open",
    )

    bot = Bot(db_path=str(db_path))
    bot.binance.latest_price = 84350.0
    bot.signal_engine.evaluate = Mock(
        return_value=Signal(
            side="up",
            model_p_up=0.75,
            market_price=0.55,
            edge=0.20,
            up_edge=0.20,
            down_edge=-0.20,
        )
    )
    execute_mock = Mock(return_value=TradeResult(success=True, trade_id=999, pnl=None))
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

    assert bot._window_traded is True
    assert bot._open_trade["trade_id"] == trade_id
    assert bot._open_trade["side"] == "up"
    assert bot.stats["execution_status"] == "recovery"
    execute_mock.assert_not_called()


@pytest.mark.asyncio
async def test_bot_preview_edge_exposes_down_side_price(tmp_path: Path):
    from polypocket.bot import Bot

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))

    bot = Bot(db_path=str(db_path))
    bot.binance.latest_price = 84000.0
    bot.signal_engine.evaluate = lambda **kwargs: None

    window = Window(
        condition_id="abc123",
        question="BTC Up or Down",
        up_token_id="tok_up",
        down_token_id="tok_down",
        end_time=time.time() + 180,
        slug="btc-updown-5m-123",
        price_to_beat=84198.0,
        up_ask=0.99,
        down_ask=0.15,
    )

    await bot._on_book_update(window, "up")

    expected_down_edge = (1 - bot.stats["model_p_up"]) - (window.down_ask * (1 + FEE_RATE))
    raw_up_edge = bot.stats["model_p_up"] - window.up_ask
    assert bot.stats["edge"] == pytest.approx(expected_down_edge)
    assert bot.stats["preview_side"] == "down"
    assert bot.stats["preview_market_price"] == window.down_ask
    assert bot.stats["edge"] != pytest.approx(raw_up_edge)


@pytest.mark.asyncio
async def test_bot_preview_edge_exposes_up_side_price(tmp_path: Path):
    from polypocket.bot import Bot

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))

    bot = Bot(db_path=str(db_path))
    bot.binance.latest_price = 84350.0
    bot.signal_engine.evaluate = lambda **kwargs: None

    window = Window(
        condition_id="abc123",
        question="BTC Up or Down",
        up_token_id="tok_up",
        down_token_id="tok_down",
        end_time=time.time() + 180,
        slug="btc-updown-5m-123",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.80,
    )

    await bot._on_book_update(window, "up")

    expected_up_edge = bot.stats["model_p_up"] - (window.up_ask * (1 + FEE_RATE))
    assert bot.stats["edge"] == pytest.approx(expected_up_edge)
    assert bot.stats["preview_side"] == "up"
    assert bot.stats["preview_market_price"] == window.up_ask
