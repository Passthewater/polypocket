import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from polypocket.config import effective_ask
from polypocket.executor import FillResult, TradeResult
from polypocket.feeds.polymarket import Window
from polypocket.ledger import find_trade_by_window_slug, get_paper_balance, get_snapshots_for_window, init_db, log_trade
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

    monkeypatch.setattr("polypocket.bot.TRADING_MODE", "paper")
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
    assert bot.stats["preview_side"] == "up"
    assert bot.stats["preview_market_price"] == 0.55
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
    assert bot.stats["preview_side"] == "up"
    assert bot.stats["preview_market_price"] == 0.55
    assert bot._open_trade is None
    evaluate_mock.assert_not_called()
    execute_mock.assert_not_called()


@pytest.mark.asyncio
async def test_bot_clears_stale_skipped_status_when_quote_becomes_valid(tmp_path: Path, monkeypatch):
    from polypocket.bot import Bot

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))

    bot = Bot(db_path=str(db_path))
    bot.binance.latest_price = 84350.0
    bot.signal_engine.evaluate = Mock(return_value=None)

    invalid_window = Window(
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
    valid_window = Window(
        condition_id="abc123",
        question="BTC Up or Down",
        up_token_id="tok_up",
        down_token_id="tok_down",
        end_time=time.time() + 170,
        slug="btc-updown-5m-123",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
    )

    await bot._on_book_update(invalid_window, "up")
    assert bot.stats["execution_status"] == "skipped"

    await bot._on_book_update(valid_window, "up")

    assert bot.stats["quote_status"] == "valid"
    assert bot.stats["execution_status"] is None


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
async def test_bot_live_mode_open_trade_is_not_rehydrated_into_paper_settlement(
    tmp_path: Path, monkeypatch
):
    import polypocket.bot as bot_module
    from polypocket.bot import Bot

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))

    log_trade(
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

    starting_balance = get_paper_balance(str(db_path))
    monkeypatch.setattr(bot_module, "TRADING_MODE", "live")
    settle_mock = Mock(return_value=0.0)
    monkeypatch.setattr(bot_module, "settle_paper_trade", settle_mock)

    async def mock_resolution(slug):
        return "up"

    monkeypatch.setattr(bot_module, "fetch_resolution", mock_resolution)

    bot = Bot(db_path=str(db_path), live_order_client=Mock())
    bot.binance.latest_price = 84350.0

    expired_window = Window(
        condition_id="abc123",
        question="BTC Up or Down",
        up_token_id="tok_up",
        down_token_id="tok_down",
        end_time=time.time() - 1,
        slug="btc-updown-5m-123",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
    )

    await bot._on_book_update(expired_window, "up")

    trade = find_trade_by_window_slug(str(db_path), "btc-updown-5m-123")
    assert bot._window_traded is True
    assert bot._open_trade is None
    assert bot.stats["position"] is None
    assert bot.stats["execution_status"] == "recovery"
    assert trade["status"] == "settled"
    assert trade["outcome"] == "up"
    assert trade["pnl"] is None
    assert get_paper_balance(str(db_path)) == starting_balance
    settle_mock.assert_not_called()


@pytest.mark.asyncio
async def test_bot_live_mode_recovers_reserved_trade_and_prevents_reentry(tmp_path: Path, monkeypatch):
    import polypocket.bot as bot_module
    from polypocket.bot import Bot

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))

    trade_id = log_trade(
        db_path=str(db_path),
        window_slug="btc-updown-5m-456",
        side="down",
        entry_price=0.45,
        size=10.0,
        fees=0.10,
        model_p_up=0.25,
        market_p_up=0.55,
        edge=0.20,
        outcome=None,
        pnl=None,
        status="reserved",
    )

    monkeypatch.setattr(bot_module, "TRADING_MODE", "live")
    execute_mock = Mock(return_value=TradeResult(success=True, trade_id=999, pnl=None))
    monkeypatch.setattr(bot_module, "execute_live_trade", execute_mock)

    bot = Bot(db_path=str(db_path), live_order_client=Mock())
    bot.binance.latest_price = 84000.0
    bot.signal_engine.evaluate = Mock(
        return_value=Signal(
            side="down",
            model_p_up=0.25,
            market_price=0.45,
            edge=0.20,
            up_edge=-0.20,
            down_edge=0.20,
        )
    )
    bot.risk.check = lambda: (True, "")

    active_window = Window(
        condition_id="def456",
        question="BTC Up or Down",
        up_token_id="tok_up",
        down_token_id="tok_down",
        end_time=time.time() + 180,
        slug="btc-updown-5m-456",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
    )

    await bot._on_book_update(active_window, "down")

    assert bot._window_traded is True
    assert bot._open_trade["trade_id"] == trade_id
    assert bot.stats["execution_status"] == "recovery"
    execute_mock.assert_not_called()


@pytest.mark.asyncio
async def test_bot_live_recovery_reconciles_matched_to_open(tmp_path: Path, monkeypatch):
    import polypocket.bot as bot_module
    from polypocket.bot import Bot
    from polypocket.ledger import update_trade

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))

    trade_id = log_trade(
        db_path=str(db_path),
        window_slug="btc-updown-5m-rec-matched",
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
    update_trade(str(db_path), trade_id, outcome=None, pnl=None, status="reserved",
                 external_order_id="0xabc")

    monkeypatch.setattr(bot_module, "TRADING_MODE", "live")
    execute_mock = Mock(return_value=TradeResult(success=True, trade_id=999, pnl=None))
    monkeypatch.setattr(bot_module, "execute_live_trade", execute_mock)

    live_order_client = Mock()
    live_order_client.get_order_status.return_value = {"status": "MATCHED"}

    bot = Bot(db_path=str(db_path), live_order_client=live_order_client)
    bot.binance.latest_price = 84000.0
    bot.signal_engine.evaluate = Mock(return_value=None)
    bot.risk.check = lambda: (True, "")

    active_window = Window(
        condition_id="rec-matched",
        question="BTC Up or Down",
        up_token_id="tok_up",
        down_token_id="tok_down",
        end_time=time.time() + 180,
        slug="btc-updown-5m-rec-matched",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
    )

    await bot._on_book_update(active_window, "up")

    assert bot._open_trade["trade_id"] == trade_id
    assert bot._window_traded is True
    assert bot.stats["execution_status"] == "recovery"
    execute_mock.assert_not_called()
    assert find_trade_by_window_slug(str(db_path), "btc-updown-5m-rec-matched")["status"] == "open"


@pytest.mark.asyncio
async def test_bot_live_recovery_reconciles_canceled_to_rejected(tmp_path: Path, monkeypatch):
    import polypocket.bot as bot_module
    from polypocket.bot import Bot
    from polypocket.ledger import update_trade

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))

    trade_id = log_trade(
        db_path=str(db_path),
        window_slug="btc-updown-5m-rec-canceled",
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
    update_trade(str(db_path), trade_id, outcome=None, pnl=None, status="reserved",
                 external_order_id="0xabc")

    monkeypatch.setattr(bot_module, "TRADING_MODE", "live")
    execute_mock = Mock(return_value=TradeResult(success=True, trade_id=999, pnl=None))
    monkeypatch.setattr(bot_module, "execute_live_trade", execute_mock)

    live_order_client = Mock()
    live_order_client.get_order_status.return_value = {"status": "CANCELED"}

    bot = Bot(db_path=str(db_path), live_order_client=live_order_client)
    bot.binance.latest_price = 84000.0
    bot.signal_engine.evaluate = Mock(return_value=None)
    bot.risk.check = lambda: (True, "")

    active_window = Window(
        condition_id="rec-canceled",
        question="BTC Up or Down",
        up_token_id="tok_up",
        down_token_id="tok_down",
        end_time=time.time() + 180,
        slug="btc-updown-5m-rec-canceled",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
    )

    await bot._on_book_update(active_window, "up")

    assert bot._open_trade is None
    assert bot._window_traded is True
    assert bot.stats["execution_status"] == "rejected-on-recovery"
    execute_mock.assert_not_called()
    assert find_trade_by_window_slug(str(db_path), "btc-updown-5m-rec-canceled")["status"] == "rejected"


@pytest.mark.asyncio
async def test_bot_live_settle_uses_clob_settlement_info_for_real_pnl(
    tmp_path: Path, monkeypatch
):
    """When a live trade resolves, the bot must query the CLOB via the
    injected client and record real PnL + risk outcome."""
    import polypocket.bot as bot_module
    from polypocket.bot import Bot
    from polypocket.executor import SettlementInfo

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))

    trade_id = log_trade(
        db_path=str(db_path),
        window_slug="btc-updown-5m-789",
        side="up",
        entry_price=0.55,
        size=10.0,
        fees=0.0,
        model_p_up=0.75,
        market_p_up=0.55,
        edge=0.20,
        outcome=None,
        pnl=None,
        status="open",
    )
    # Flag the row with an external order id so the settle path queries CLOB.
    from polypocket.ledger import update_trade
    update_trade(str(db_path), trade_id, outcome=None, pnl=None, status="open",
                 external_order_id="clob-ord-42")

    monkeypatch.setattr(bot_module, "TRADING_MODE", "live")

    async def mock_resolution(slug):
        return "up"
    monkeypatch.setattr(bot_module, "fetch_resolution", mock_resolution)

    client = Mock()
    client.get_settlement_info = Mock(
        return_value=SettlementInfo(shares_held=9.0, cost_usdc=5.5)
    )

    bot = Bot(db_path=str(db_path), live_order_client=client)
    bot.binance.latest_price = 84350.0
    record_win = Mock()
    record_loss = Mock()
    bot.risk.record_win = record_win
    bot.risk.record_loss = record_loss

    expired_window = Window(
        condition_id="abc789",
        question="BTC Up or Down",
        up_token_id="tok_up",
        down_token_id="tok_down",
        end_time=time.time() - 1,
        slug="btc-updown-5m-789",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
    )

    await bot._on_book_update(expired_window, "up")

    trade = find_trade_by_window_slug(str(db_path), "btc-updown-5m-789")
    assert trade["status"] == "settled"
    assert trade["outcome"] == "up"
    assert trade["pnl"] == pytest.approx(3.5)
    client.get_settlement_info.assert_called_once_with("clob-ord-42")
    record_win.assert_called_once()
    record_loss.assert_not_called()


@pytest.mark.asyncio
async def test_poll_pending_settlements_live_writes_real_pnl(
    tmp_path: Path, monkeypatch
):
    """A live trade parked in _pending_settlements should reconcile via the
    CLOB client and write real PnL."""
    import polypocket.bot as bot_module
    from polypocket.bot import Bot
    from polypocket.executor import SettlementInfo

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))

    trade_id = log_trade(
        db_path=str(db_path),
        window_slug="btc-updown-5m-pnd",
        side="down", entry_price=0.45, size=10.0, fees=0.0,
        model_p_up=0.25, market_p_up=0.45, edge=0.15,
        outcome=None, pnl=None, status="open",
    )
    from polypocket.ledger import update_trade
    update_trade(str(db_path), trade_id, outcome=None, pnl=None, status="open",
                 external_order_id="clob-pnd-1")

    monkeypatch.setattr(bot_module, "TRADING_MODE", "live")

    async def mock_resolution(slug):
        return "up"  # we're "down" → loss
    monkeypatch.setattr(bot_module, "fetch_resolution", mock_resolution)

    client = Mock()
    client.get_settlement_info = Mock(
        return_value=SettlementInfo(shares_held=9.0, cost_usdc=4.5)
    )

    bot = Bot(db_path=str(db_path), live_order_client=client)
    record_loss = Mock()
    bot.risk.record_loss = record_loss
    bot._pending_settlements.append({
        "trade_id": trade_id,
        "side": "down",
        "entry_price": 0.45,
        "size": 10.0,
        "mode": "live",
        "status": "open",
        "window_slug": "btc-updown-5m-pnd",
        "external_order_id": "clob-pnd-1",
    })

    await bot._poll_pending_settlements()

    trade = find_trade_by_window_slug(str(db_path), "btc-updown-5m-pnd")
    assert trade["status"] == "settled"
    assert trade["outcome"] == "up"
    assert trade["pnl"] == pytest.approx(-4.5)
    client.get_settlement_info.assert_called_once_with("clob-pnd-1")
    record_loss.assert_called_once()
    assert bot._pending_settlements == []


@pytest.mark.asyncio
async def test_live_settle_unreconciled_counts_as_loss(
    tmp_path: Path, monkeypatch
):
    """If the CLOB can't return settlement info (no order_id, or lookup
    error), the bot must still advance the consecutive-loss counter —
    conservative fallback so reconciliation failures can't mask a losing
    streak."""
    import polypocket.bot as bot_module
    from polypocket.bot import Bot

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))

    trade_id = log_trade(
        db_path=str(db_path),
        window_slug="btc-updown-5m-unr",
        side="up", entry_price=0.55, size=10.0, fees=0.0,
        model_p_up=0.75, market_p_up=0.55, edge=0.20,
        outcome=None, pnl=None, status="open",
    )
    # No external_order_id → settle_live_trade returns None.

    monkeypatch.setattr(bot_module, "TRADING_MODE", "live")

    async def mock_resolution(slug):
        return "up"
    monkeypatch.setattr(bot_module, "fetch_resolution", mock_resolution)

    bot = Bot(db_path=str(db_path), live_order_client=Mock())
    record_win = Mock()
    record_loss = Mock()
    bot.risk.record_win = record_win
    bot.risk.record_loss = record_loss
    bot._pending_settlements.append({
        "trade_id": trade_id,
        "side": "up",
        "entry_price": 0.55,
        "size": 10.0,
        "mode": "live",
        "status": "open",
        "window_slug": "btc-updown-5m-unr",
        "external_order_id": None,
    })

    await bot._poll_pending_settlements()

    record_loss.assert_called_once()
    record_win.assert_not_called()
    assert bot._pending_settlements == []


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

    expected_down_edge = (1 - bot.stats["model_p_up_calibrated"]) - effective_ask(window.down_ask)
    raw_up_edge = bot.stats["model_p_up"] - window.up_ask
    assert bot.stats["edge"] == pytest.approx(expected_down_edge)
    assert bot.stats["preview_side"] == "down"
    assert bot.stats["preview_market_price"] == window.down_ask
    assert bot.stats["up_ask"] == window.up_ask
    assert bot.stats["down_ask"] == window.down_ask
    assert bot.stats["quote_status"] == "overround"
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

    expected_up_edge = bot.stats["model_p_up"] - effective_ask(window.up_ask)
    assert bot.stats["edge"] == pytest.approx(expected_up_edge)
    assert bot.stats["preview_side"] == "up"
    assert bot.stats["preview_market_price"] == window.up_ask
    assert bot.stats["up_ask"] == window.up_ask
    assert bot.stats["down_ask"] == window.down_ask
    assert bot.stats["quote_status"] == "overround"


@pytest.mark.asyncio
async def test_bot_emits_open_snapshot_on_new_window(tmp_path: Path):
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
        slug="btc-updown-5m-snap-open",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
    )

    await bot._on_book_update(window, "up")

    snapshots = get_snapshots_for_window(str(db_path), "btc-updown-5m-snap-open")
    assert len(snapshots) == 1
    assert snapshots[0]["snapshot_type"] == "open"
    assert snapshots[0]["btc_price"] == 84250.0
    assert snapshots[0]["window_open_price"] == 84198.0


@pytest.mark.asyncio
async def test_bot_emits_decision_snapshot_on_trade(tmp_path: Path, monkeypatch):
    from polypocket.bot import Bot

    monkeypatch.setattr("polypocket.bot.TRADING_MODE", "paper")
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
        slug="btc-updown-5m-snap-decision",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
    )

    await bot._on_book_update(window, "up")

    snapshots = get_snapshots_for_window(str(db_path), "btc-updown-5m-snap-decision")
    decision = [s for s in snapshots if s["snapshot_type"] == "decision"]
    assert len(decision) == 1
    assert decision[0]["trade_fired"] == 1
    assert decision[0]["skip_reason"] is None
    assert decision[0]["btc_price"] == 84350.0


@pytest.mark.asyncio
async def test_bot_emits_close_snapshot_on_settlement(tmp_path: Path, monkeypatch):
    import polypocket.bot as bot_module
    from polypocket.bot import Bot

    monkeypatch.setattr(bot_module, "TRADING_MODE", "paper")
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
    monkeypatch.setattr("polypocket.bot.settle_paper_trade", lambda *args, **kwargs: 4.5)

    active_window = Window(
        condition_id="abc123",
        question="BTC Up or Down",
        up_token_id="tok_up",
        down_token_id="tok_down",
        end_time=time.time() + 180,
        slug="btc-updown-5m-snap-close",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
    )
    await bot._on_book_update(active_window, "up")

    async def mock_resolution(slug):
        return "up"

    monkeypatch.setattr(bot_module, "fetch_resolution", mock_resolution)

    # Simulate time passing so the active window has expired by the time
    # the next-slot book event arrives. In production this is the signal
    # that triggers the transition + settlement of the previous window.
    monkeypatch.setattr(bot_module.time, "time", lambda: active_window.end_time + 1)

    # Seed hires so the transition emitter can compute a BTC-derived outcome.
    # 84250 > ptb 84198 → outcome "up".
    bot.binance._hires.append((active_window.end_time, 84250.0))

    next_window = Window(
        condition_id="def456",
        question="BTC Up or Down",
        up_token_id="tok_up2",
        down_token_id="tok_down2",
        end_time=active_window.end_time + 300,
        slug="btc-updown-5m-snap-close-next",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
    )
    await bot._on_book_update(next_window, "up")

    snapshots = get_snapshots_for_window(str(db_path), "btc-updown-5m-snap-close")
    close = [s for s in snapshots if s["snapshot_type"] == "close"]
    # Per design D2: transition emitter is the sole writer of the close row,
    # outcome is BTC-derived (not PM-resolved), trade_fired is not set.
    assert len(close) == 1
    assert close[0]["outcome"] == "up"
    assert close[0]["final_price"] == 84250.0
    assert close[0]["trade_fired"] is None


@pytest.mark.asyncio
async def test_bot_emits_decision_snapshot_on_skip(tmp_path: Path, monkeypatch):
    import polypocket.bot as bot_module
    from polypocket.bot import Bot

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))

    bot = Bot(db_path=str(db_path))
    bot.binance.latest_price = 84250.0
    bot.signal_engine.evaluate = lambda **kwargs: None

    active_window = Window(
        condition_id="abc123",
        question="BTC Up or Down",
        up_token_id="tok_up",
        down_token_id="tok_down",
        end_time=time.time() + 180,
        slug="btc-updown-5m-snap-skip",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
    )
    await bot._on_book_update(active_window, "up")

    # Advance simulated time past the active window's expiry so the
    # next-slot event triggers transition + decision-snapshot flush.
    monkeypatch.setattr(bot_module.time, "time", lambda: active_window.end_time + 1)

    next_window = Window(
        condition_id="def456",
        question="BTC Up or Down",
        up_token_id="tok_up2",
        down_token_id="tok_down2",
        end_time=active_window.end_time + 300,
        slug="btc-updown-5m-snap-skip-next",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
    )
    await bot._on_book_update(next_window, "up")

    snapshots = get_snapshots_for_window(str(db_path), "btc-updown-5m-snap-skip")
    decision = [s for s in snapshots if s["snapshot_type"] == "decision"]
    assert len(decision) == 1
    assert decision[0]["trade_fired"] == 0
    assert decision[0]["skip_reason"] is not None


@pytest.mark.asyncio
async def test_full_window_lifecycle_produces_three_snapshots(tmp_path: Path, monkeypatch):
    import polypocket.bot as bot_module
    from polypocket.bot import Bot

    monkeypatch.setattr(bot_module, "TRADING_MODE", "paper")
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
    monkeypatch.setattr("polypocket.bot.settle_paper_trade", lambda *args, **kwargs: 4.5)

    async def mock_resolution(slug):
        return "up"

    monkeypatch.setattr(bot_module, "fetch_resolution", mock_resolution)

    # Window 1: active, trade fires
    w1 = Window(
        condition_id="w1",
        question="BTC Up or Down",
        up_token_id="tok_up",
        down_token_id="tok_down",
        end_time=time.time() + 180,
        slug="btc-updown-5m-lifecycle",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
    )
    await bot._on_book_update(w1, "up")

    # Advance simulated time past w1.end_time so the next-slot event is
    # seen as a post-expiry transition (matches production timing).
    monkeypatch.setattr(bot_module.time, "time", lambda: w1.end_time + 1)

    # Seed hires so the G1 transition emitter can BTC-derive an "up" outcome.
    bot.binance._hires.append((w1.end_time, 84250.0))

    # Window 2: triggers settlement of window 1
    w2 = Window(
        condition_id="w2",
        question="BTC Up or Down",
        up_token_id="tok_up2",
        down_token_id="tok_down2",
        end_time=w1.end_time + 300,
        slug="btc-updown-5m-lifecycle-next",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
    )
    await bot._on_book_update(w2, "up")

    snapshots = get_snapshots_for_window(str(db_path), "btc-updown-5m-lifecycle")
    types = {s["snapshot_type"] for s in snapshots}
    assert types == {"open", "decision", "close"}
    assert len(snapshots) == 3

    # Verify decision was a trade
    decision = next(s for s in snapshots if s["snapshot_type"] == "decision")
    assert decision["trade_fired"] == 1

    # G1: close outcome is BTC-derived (84250 > ptb 84198 → "up").
    close = next(s for s in snapshots if s["snapshot_type"] == "close")
    assert close["outcome"] == "up"
    assert close["final_price"] == 84250.0


@pytest.mark.asyncio
async def test_live_mode_threads_up_token_id(tmp_path: Path, monkeypatch):
    """Signal.side='up' → execute_live_trade called with window.up_token_id."""
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setattr("polypocket.bot.TRADING_MODE", "live")

    from polypocket.bot import Bot

    class CapturingClient:
        def __init__(self):
            self.calls = []

        def submit_fok(self, side, price, size, token_id, condition_id):
            self.calls.append({"side": side, "token_id": token_id, "condition_id": condition_id})
            return FillResult(
                status="filled", order_id="ord-test",
                filled_size=size, avg_price=price, error=None,
            )

        def submit_ioc(self, side, price, size, token_id, condition_id, limit_price):
            self.calls.append({"side": side, "token_id": token_id, "condition_id": condition_id})
            return FillResult(
                status="filled", order_id="ord-test",
                filled_size=size, avg_price=price, error=None,
            )

        def get_usdc_balance(self):
            return 1000.0

        def get_order_book(self, token_id):
            return {}

    db_path = tmp_path / "live.db"
    init_db(str(db_path))
    client = CapturingClient()
    bot = Bot(db_path=str(db_path), live_order_client=client)
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

    window = Window(
        condition_id="live-test",
        question="BTC Up or Down",
        up_token_id="UP-TOKEN-ID",
        down_token_id="DOWN-TOKEN-ID",
        end_time=time.time() + 180,
        slug="btc-updown-5m-live",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
        up_book=[{"price": 0.55, "size": 1000.0}],
        down_book=[{"price": 0.45, "size": 1000.0}],
        up_bids=[{"price": 0.55, "size": 1000.0}],
        down_bids=[{"price": 0.45, "size": 1000.0}],
        book_updated_at=time.monotonic(),
    )

    await bot._on_book_update(window, "up")

    assert len(client.calls) == 1
    assert client.calls[0]["side"] == "up"
    assert client.calls[0]["token_id"] == "UP-TOKEN-ID"


def _make_live_bot(tmp_path: Path, monkeypatch, client):
    from polypocket.bot import Bot

    monkeypatch.setattr("polypocket.bot.TRADING_MODE", "live")
    db_path = tmp_path / "live.db"
    init_db(str(db_path))
    bot = Bot(db_path=str(db_path), live_order_client=client)
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
    return bot


class _CapturingClient:
    def __init__(self):
        self.calls = []

    def submit_fok(self, side, price, size, token_id, condition_id):
        self.calls.append({"side": side, "size": size})
        return FillResult(status="filled", order_id="x",
                          filled_size=size, avg_price=price, error=None)

    def submit_ioc(self, side, price, size, token_id, condition_id, limit_price):
        self.calls.append({"side": side, "size": size, "limit_price": limit_price})
        return FillResult(status="filled", order_id="x",
                          filled_size=size, avg_price=price, error=None)

    def get_usdc_balance(self):
        return 1000.0

    def get_order_book(self, token_id):
        return {}


@pytest.mark.asyncio
async def test_bot_live_skips_when_book_stale(tmp_path: Path, monkeypatch):
    """Staleness gate: book_updated_at older than MAX_BOOK_AGE_S -> skip."""
    client = _CapturingClient()
    bot = _make_live_bot(tmp_path, monkeypatch, client)

    window = Window(
        condition_id="stale-test",
        question="BTC Up or Down",
        up_token_id="UP", down_token_id="DOWN",
        end_time=time.time() + 180,
        slug="btc-updown-5m-stale",
        price_to_beat=84198.0,
        up_ask=0.55, down_ask=0.45,
        up_book=[{"price": 0.55, "size": 1000.0}],
        down_book=[{"price": 0.45, "size": 1000.0}],
        up_bids=[{"price": 0.55, "size": 1000.0}],
        down_bids=[{"price": 0.45, "size": 1000.0}],
        book_updated_at=time.monotonic() - 10.0,  # 10s old
    )

    await bot._on_book_update(window, "up")

    assert client.calls == []
    assert bot._window_skip_reason == "book-stale"


@pytest.mark.asyncio
async def test_bot_live_skips_when_book_age_none(tmp_path: Path, monkeypatch):
    """No book event ever received -> skip (fail-closed)."""
    client = _CapturingClient()
    bot = _make_live_bot(tmp_path, monkeypatch, client)

    window = Window(
        condition_id="none-test",
        question="BTC Up or Down",
        up_token_id="UP", down_token_id="DOWN",
        end_time=time.time() + 180,
        slug="btc-updown-5m-none",
        price_to_beat=84198.0,
        up_ask=0.55, down_ask=0.45,
        up_book=[{"price": 0.55, "size": 1000.0}],
        up_bids=[{"price": 0.55, "size": 1000.0}],
        down_bids=[{"price": 0.45, "size": 1000.0}],
        book_updated_at=None,
    )

    await bot._on_book_update(window, "up")

    assert client.calls == []
    assert bot._window_skip_reason == "book-stale"


@pytest.mark.asyncio
async def test_bot_live_submits_when_book_deep_and_fresh(tmp_path: Path, monkeypatch):
    """Sanity: gates are no-ops on a healthy book."""
    client = _CapturingClient()
    bot = _make_live_bot(tmp_path, monkeypatch, client)

    window = Window(
        condition_id="ok-test",
        question="BTC Up or Down",
        up_token_id="UP", down_token_id="DOWN",
        end_time=time.time() + 180,
        slug="btc-updown-5m-ok",
        price_to_beat=84198.0,
        up_ask=0.55, down_ask=0.45,
        up_book=[{"price": 0.55, "size": 1000.0}],
        down_book=[{"price": 0.45, "size": 1000.0}],
        up_bids=[{"price": 0.55, "size": 1000.0}],
        down_bids=[{"price": 0.45, "size": 1000.0}],
        book_updated_at=time.monotonic(),
    )

    await bot._on_book_update(window, "up")

    assert len(client.calls) == 1


class _ThinWalletClient(_CapturingClient):
    def __init__(self, balance: float):
        super().__init__()
        self._balance = balance

    def get_usdc_balance(self):
        return self._balance


@pytest.mark.asyncio
async def test_bot_live_downsizes_when_balance_below_max(tmp_path: Path, monkeypatch):
    """Balance $8 with max position $20: trade should submit at ~$8*0.98, not skip."""
    from polypocket.config import MAX_POSITION_USDC, MIN_POSITION_USDC
    client = _ThinWalletClient(balance=8.0)
    bot = _make_live_bot(tmp_path, monkeypatch, client)

    window = Window(
        condition_id="downsize-test",
        question="BTC Up or Down",
        up_token_id="UP", down_token_id="DOWN",
        end_time=time.time() + 180,
        slug="btc-updown-5m-downsize",
        price_to_beat=84198.0,
        up_ask=0.55, down_ask=0.45,
        up_book=[{"price": 0.55, "size": 1000.0}],
        down_book=[{"price": 0.45, "size": 1000.0}],
        up_bids=[{"price": 0.55, "size": 1000.0}],
        down_bids=[{"price": 0.45, "size": 1000.0}],
        book_updated_at=time.monotonic(),
    )

    await bot._on_book_update(window, "up")

    assert len(client.calls) == 1
    # At balance=8.0 and entry=0.55, clamped size = 8.0*0.98 / 0.55 ≈ 14.25 shares
    submitted = client.calls[0]["size"]
    expected = (8.0 * 0.98) / 0.55
    assert submitted == pytest.approx(expected, rel=0.01)
    # Must not exceed the unclamped MAX_POSITION_USDC-derived size
    assert submitted <= MAX_POSITION_USDC / 0.55


@pytest.mark.asyncio
async def test_bot_live_skips_when_balance_below_min_position(tmp_path: Path, monkeypatch):
    """Balance $2 with MIN_POSITION_USDC=$5: skip, don't submit."""
    from polypocket.config import MIN_POSITION_USDC
    # Ensure the wallet is below the floor even after the 2% buffer is applied.
    client = _ThinWalletClient(balance=MIN_POSITION_USDC * 0.5)
    bot = _make_live_bot(tmp_path, monkeypatch, client)

    window = Window(
        condition_id="skip-test",
        question="BTC Up or Down",
        up_token_id="UP", down_token_id="DOWN",
        end_time=time.time() + 180,
        slug="btc-updown-5m-skip",
        price_to_beat=84198.0,
        up_ask=0.55, down_ask=0.45,
        up_book=[{"price": 0.55, "size": 1000.0}],
        down_book=[{"price": 0.45, "size": 1000.0}],
        up_bids=[{"price": 0.55, "size": 1000.0}],
        down_bids=[{"price": 0.45, "size": 1000.0}],
        book_updated_at=time.monotonic(),
    )

    await bot._on_book_update(window, "up")

    assert client.calls == []
    assert bot._window_skip_reason == "insufficient-balance"
    assert bot.stats["execution_status"] == "no-balance"


@pytest.mark.asyncio
async def test_bot_live_clamps_size_when_book_shallow(tmp_path: Path, monkeypatch):
    """Book holds less than intended size but >= MIN_FILL_RATIO * intended:
    trade fires at clamped size (fillable * DEPTH_CLAMP_BUFFER)."""
    # Pin MAX_POSITION_USDC=6 so intended size is 6/0.55 ≈ 10.9 shares,
    # and MIN_POSITION_USDC=3 so floor gate passes when fillable=11
    # (11 * 0.55 * 0.5 = 3.025 >= 3.0). Clamp engages because
    # 11 * 0.9 = 9.9 < 10.9.
    monkeypatch.setattr("polypocket.bot.MAX_POSITION_USDC", 6.0)
    monkeypatch.setattr("polypocket.bot.MIN_POSITION_USDC", 3.0)
    client = _CapturingClient()
    bot = _make_live_bot(tmp_path, monkeypatch, client)

    # With edge=0.20, vol_scale=1 (sigma forced to 0.001 floor), intended =
    # MAX_POSITION_USDC / entry = ~10.9 shares at $0.55.
    # DOWN-bids hold 11 shares at 0.45 (best). limit = 1 - 0.45 + 0.08 = 0.63.
    # threshold = 0.37 -> all 11 shares pair-merge-eligible, fillable = 11.
    # Floor gate: 11 * 0.63 * 0.5 = 3.465 >= 3.0 -> passes.
    # Clamp: min(10.9, 11*0.9=9.9) = 9.9 < 10.9 -> clamp engages.
    window = Window(
        condition_id="shallow-test",
        question="BTC Up or Down",
        up_token_id="UP", down_token_id="DOWN",
        end_time=time.time() + 180,
        slug="btc-updown-5m-shallow",
        price_to_beat=84198.0,
        up_ask=0.55, down_ask=0.45,
        up_book=[{"price": 0.55, "size": 1000.0}],
        down_book=[{"price": 0.45, "size": 1000.0}],
        up_bids=[{"price": 0.55, "size": 1000.0}],
        down_bids=[{"price": 0.45, "size": 11.0}],
        book_updated_at=time.monotonic(),
    )

    await bot._on_book_update(window, "up")

    assert len(client.calls) == 1
    # fillable = 11, clamped = 11 * 0.9 = 9.9. intended is ~10.9 shares
    # (MAX_POSITION_USDC/entry), so the clamp engages.
    assert client.calls[0]["size"] == pytest.approx(9.9, rel=1e-3)


@pytest.mark.asyncio
async def test_bot_live_submits_intended_when_book_deep(tmp_path: Path, monkeypatch):
    """Book holds far more than intended -> clamp is a no-op."""
    client = _CapturingClient()
    bot = _make_live_bot(tmp_path, monkeypatch, client)

    window = Window(
        condition_id="deep-test",
        question="BTC Up or Down",
        up_token_id="UP", down_token_id="DOWN",
        end_time=time.time() + 180,
        slug="btc-updown-5m-deep",
        price_to_beat=84198.0,
        up_ask=0.55, down_ask=0.45,
        up_book=[{"price": 0.55, "size": 1000.0}],
        down_book=[{"price": 0.45, "size": 1000.0}],
        up_bids=[{"price": 0.55, "size": 1000.0}],
        down_bids=[{"price": 0.45, "size": 1000.0}],
        book_updated_at=time.monotonic(),
    )

    await bot._on_book_update(window, "up")

    assert len(client.calls) == 1
    # intended size is some edge/vol-derived value; just check the clamp
    # did NOT reduce it below something the old flow would have accepted.
    assert client.calls[0]["size"] > 1.0  # not dust
    # And NOT clamped to book depth * 0.9 = 900 (which would mean clamp
    # fired incorrectly).
    assert client.calls[0]["size"] < 100.0


@pytest.mark.asyncio
async def test_bot_live_skips_when_depth_below_min_fill_ratio(
    tmp_path: Path, monkeypatch
):
    """Book holds < MIN_FILL_RATIO * intended -> skip book-too-thin."""
    client = _CapturingClient()
    bot = _make_live_bot(tmp_path, monkeypatch, client)

    # Only 3 shares of DOWN-bid depth pair-merge-eligible. limit=0.63,
    # fillable=3. Floor gate: 3 * 0.63 * 0.5 = 0.945 < $5 -> skip.
    window = Window(
        condition_id="thin-test",
        question="BTC Up or Down",
        up_token_id="UP", down_token_id="DOWN",
        end_time=time.time() + 180,
        slug="btc-updown-5m-thin",
        price_to_beat=84198.0,
        up_ask=0.55, down_ask=0.45,
        up_book=[{"price": 0.55, "size": 1000.0}],
        down_book=[{"price": 0.45, "size": 1000.0}],
        up_bids=[{"price": 0.55, "size": 1000.0}],
        down_bids=[{"price": 0.45, "size": 3.0}],
        book_updated_at=time.monotonic(),
    )

    await bot._on_book_update(window, "up")

    assert client.calls == []
    assert bot._window_skip_reason == "book-too-thin"


@pytest.mark.asyncio
async def test_bot_live_skips_when_opposite_bids_empty(tmp_path: Path, monkeypatch):
    """No opp-side bids -> no pair-merge counterparty -> skip (not a thin-book reject)."""
    client = _CapturingClient()
    bot = _make_live_bot(tmp_path, monkeypatch, client)

    window = Window(
        condition_id="empty-test",
        question="BTC Up or Down",
        up_token_id="UP", down_token_id="DOWN",
        end_time=time.time() + 180,
        slug="btc-updown-5m-empty",
        price_to_beat=84198.0,
        up_ask=0.55, down_ask=0.45,
        up_book=[{"price": 0.55, "size": 1000.0}],
        down_book=[{"price": 0.45, "size": 1000.0}],
        up_bids=[{"price": 0.55, "size": 1000.0}],
        down_bids=[],
        book_updated_at=time.monotonic(),
    )

    await bot._on_book_update(window, "up")

    assert client.calls == []
    assert bot._window_skip_reason == "no-pair-merge-counterparty"


@pytest.mark.asyncio
async def test_bot_live_skips_when_clamped_size_below_min_position_usdc(
    tmp_path: Path, monkeypatch
):
    """Clamp passes ratio but clamped_size * price < MIN_POSITION_USDC -> skip.

    With MIN_POSITION_USDC=5 (default), a clamped size of 8 shares at $0.55 =
    $4.40 is below the floor; trade must skip rather than submit a dust
    order. Use a small intended size so ratio passes but floor blocks.
    """
    client = _CapturingClient()
    bot = _make_live_bot(tmp_path, monkeypatch, client)
    # Downsize intent artificially by forcing a tiny available balance so
    # the balance clamp pushes intended size close to the floor, then the
    # depth clamp shaves it below.
    monkeypatch.setattr(
        "polypocket.bot.MIN_POSITION_USDC", 5.0, raising=False
    )

    window = Window(
        condition_id="floor-test",
        question="BTC Up or Down",
        up_token_id="UP", down_token_id="DOWN",
        end_time=time.time() + 180,
        slug="btc-updown-5m-floor",
        price_to_beat=84198.0,
        up_ask=0.55, down_ask=0.45,
        # fillable=8 on DOWN-bid side. limit=0.63. 8*0.63*0.5=$2.52 < $5 floor.
        up_book=[{"price": 0.55, "size": 1000.0}],
        down_book=[{"price": 0.45, "size": 1000.0}],
        up_bids=[{"price": 0.55, "size": 1000.0}],
        down_bids=[{"price": 0.45, "size": 8.0}],
        book_updated_at=time.monotonic(),
    )

    await bot._on_book_update(window, "up")

    assert client.calls == []
    assert bot._window_skip_reason == "book-too-thin"


@pytest.mark.asyncio
async def test_bot_floor_gate_engages_when_fillable_below_min_position(
    tmp_path: Path, monkeypatch
):
    """Pre-trade gate skips when fillable * limit_price * MIN_FILL_RATIO < MIN_POSITION_USDC.

    MIN_POSITION_USDC=5, best down bid=0.50 -> limit = 1 - 0.50 + buffer.
    fillable=10 at the limit's threshold, slice value
    = 10 * limit * 0.5; at IOC_BUFFER_TICKS=15 this is 3.25 < 5 -> skip.
    """
    monkeypatch.setattr("polypocket.bot.MIN_POSITION_USDC", 5.0, raising=False)
    client = _CapturingClient()
    bot = _make_live_bot(tmp_path, monkeypatch, client)

    window = Window(
        condition_id="gate-engages-test",
        question="BTC Up or Down",
        up_token_id="UP", down_token_id="DOWN",
        end_time=time.time() + 180,
        slug="btc-updown-5m-gate-engages",
        price_to_beat=84198.0,
        up_ask=0.50, down_ask=0.50,
        up_book=[{"price": 0.50, "size": 1000.0}],
        down_book=[{"price": 0.50, "size": 1000.0}],
        up_bids=[{"price": 0.50, "size": 1000.0}],
        down_bids=[{"price": 0.50, "size": 10.0}],
        book_updated_at=time.monotonic(),
    )

    await bot._on_book_update(window, "up")

    assert client.calls == []
    assert bot._window_skip_reason == "book-too-thin"


@pytest.mark.asyncio
async def test_bot_floor_gate_passes_at_boundary(
    tmp_path: Path, monkeypatch
):
    """Above the floor boundary, the gate allows the trade through.

    MIN_POSITION_USDC=5, down_bid=0.50, limit=0.58.
    fillable=20: 20 * 0.58 * 0.5 = 5.8 >= 5 -> passes gate.
    """
    monkeypatch.setattr("polypocket.bot.MIN_POSITION_USDC", 5.0, raising=False)
    client = _CapturingClient()
    bot = _make_live_bot(tmp_path, monkeypatch, client)

    window = Window(
        condition_id="gate-boundary-test",
        question="BTC Up or Down",
        up_token_id="UP", down_token_id="DOWN",
        end_time=time.time() + 180,
        slug="btc-updown-5m-gate-boundary",
        price_to_beat=84198.0,
        up_ask=0.50, down_ask=0.50,
        up_book=[{"price": 0.50, "size": 1000.0}],
        down_book=[{"price": 0.50, "size": 1000.0}],
        up_bids=[{"price": 0.50, "size": 1000.0}],
        down_bids=[{"price": 0.50, "size": 20.0}],
        book_updated_at=time.monotonic(),
    )

    await bot._on_book_update(window, "up")

    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_bot_emits_ioc_diagnostic_log_line(tmp_path: Path, monkeypatch, caplog):
    """Per-trade IOC_DIAG INFO line records intended vs. actual fill fields."""
    import logging
    caplog.set_level(logging.INFO, logger="polypocket.bot")

    client = _CapturingClient()
    bot = _make_live_bot(tmp_path, monkeypatch, client)

    window = Window(
        condition_id="diag-test",
        question="BTC Up or Down",
        up_token_id="UP", down_token_id="DOWN",
        end_time=time.time() + 180,
        slug="btc-updown-5m-diag",
        price_to_beat=84198.0,
        up_ask=0.55, down_ask=0.45,
        up_book=[{"price": 0.55, "size": 1000.0}],
        down_book=[{"price": 0.45, "size": 1000.0}],
        up_bids=[{"price": 0.55, "size": 1000.0}],
        down_bids=[{"price": 0.45, "size": 1000.0}],
        book_updated_at=time.monotonic(),
    )

    await bot._on_book_update(window, "up")

    records = [r for r in caplog.records if "IOC_DIAG" in r.getMessage()]
    assert len(records) == 1
    msg = records[0].getMessage()
    assert "intended=" in msg
    assert "target=" in msg
    assert "fillable=" in msg
    assert "limit=" in msg


@pytest.mark.asyncio
async def test_bot_passes_pair_merge_limit_price_to_submit(tmp_path: Path, monkeypatch):
    """Submitted limit_price must be computed from the opposite book via
    ioc_limit_price, not the same-side ask."""
    from polypocket.config import IOC_BUFFER_TICKS
    client = _CapturingClient()
    bot = _make_live_bot(tmp_path, monkeypatch, client)

    window = Window(
        condition_id="pairmerge-test",
        question="BTC Up or Down",
        up_token_id="UP", down_token_id="DOWN",
        end_time=time.time() + 180,
        slug="btc-updown-5m-pairmerge",
        price_to_beat=84198.0,
        up_ask=0.55, down_ask=0.40,
        up_book=[{"price": 0.55, "size": 1000.0}],
        down_book=[{"price": 0.40, "size": 1000.0}],
        up_bids=[{"price": 0.55, "size": 1000.0}],
        down_bids=[{"price": 0.60, "size": 1000.0}],  # DOWN-bid at 0.60
        book_updated_at=time.monotonic(),
    )

    await bot._on_book_update(window, "up")

    assert len(client.calls) == 1
    # limit_price = 1 - 0.60 + IOC_BUFFER_TICKS*0.01
    expected = round(min(0.99, (1.0 - 0.60) + IOC_BUFFER_TICKS * 0.01), 2)
    assert client.calls[0]["limit_price"] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Phase 1 (G1): universal close-snapshot emitter
# ---------------------------------------------------------------------------


def _seed_hires(bot, ts: float, price: float) -> None:
    """Seed the binance hires buffer so price_at(ts) returns `price`."""
    bot.binance._hires.append((ts, price))
    bot.binance._hires_last_ts = ts
    bot.binance.latest_price = price


def _make_window(condition_id: str, slug: str, end_time: float, ptb: float) -> Window:
    return Window(
        condition_id=condition_id,
        question="BTC Up or Down",
        up_token_id="UP",
        down_token_id="DOWN",
        end_time=end_time,
        slug=slug,
        price_to_beat=ptb,
        up_ask=0.55,
        down_ask=0.45,
    )


@pytest.mark.asyncio
async def test_phase1_close_emitted_for_non_traded_window(tmp_path: Path):
    from polypocket.bot import Bot

    db_path = tmp_path / "p1.db"
    init_db(str(db_path))
    bot = Bot(db_path=str(db_path))
    bot.signal_engine.evaluate = lambda **kwargs: None  # no trade

    now = time.time()
    window_a = _make_window("A", "win-a", end_time=now - 1, ptb=100.0)
    window_b = _make_window("B", "win-b", end_time=now + 300, ptb=101.0)

    _seed_hires(bot, ts=window_a.end_time, price=105.0)
    await bot._on_book_update(window_a, "up")
    await bot._on_book_update(window_b, "up")

    rows = [r for r in get_snapshots_for_window(str(db_path), "win-a") if r["snapshot_type"] == "close"]
    assert len(rows) == 1
    assert rows[0]["final_price"] == 105.0
    assert rows[0]["outcome"] == "up"


@pytest.mark.asyncio
async def test_phase1_close_emitted_for_traded_window_only_once(tmp_path: Path, monkeypatch):
    import polypocket.bot as bot_module
    from polypocket.bot import Bot

    monkeypatch.setattr(bot_module, "TRADING_MODE", "paper")
    db_path = tmp_path / "p1.db"
    init_db(str(db_path))
    bot = Bot(db_path=str(db_path))
    bot.signal_engine.evaluate = lambda **kwargs: Signal(
        side="up", model_p_up=0.75, market_price=0.55, edge=0.20,
        up_edge=0.20, down_edge=-0.20,
    )
    bot.risk.check = lambda: (True, "")
    monkeypatch.setattr(
        "polypocket.bot.execute_paper_trade",
        Mock(return_value=TradeResult(success=True, trade_id=1, pnl=None)),
    )

    async def _no_resolution(_slug):  # park the trade in pending rather than settling
        return None

    monkeypatch.setattr(bot_module, "fetch_resolution", _no_resolution)

    now = time.time()
    window_a = _make_window("A", "win-a", end_time=now + 120, ptb=100.0)
    window_b = _make_window("B", "win-b", end_time=now + 420, ptb=101.0)

    _seed_hires(bot, ts=window_a.end_time, price=95.0)
    await bot._on_book_update(window_a, "up")  # fires trade

    # Advance time past window A's end so window B's incoming-live check passes
    # and the transition block fires.
    monkeypatch.setattr(bot_module.time, "time", lambda: window_a.end_time + 1)
    await bot._on_book_update(window_b, "up")

    rows = [r for r in get_snapshots_for_window(str(db_path), "win-a") if r["snapshot_type"] == "close"]
    assert len(rows) == 1  # exactly one close row, not two
    assert rows[0]["final_price"] == 95.0
    assert rows[0]["outcome"] == "down"


@pytest.mark.asyncio
async def test_phase1_close_written_with_nulls_when_hires_empty(tmp_path: Path):
    from polypocket.bot import Bot

    db_path = tmp_path / "p1.db"
    init_db(str(db_path))
    bot = Bot(db_path=str(db_path))
    bot.signal_engine.evaluate = lambda **kwargs: None
    bot.binance.latest_price = 100.0  # needed to pass the latest_price guard

    now = time.time()
    window_a = _make_window("A", "win-a", end_time=now - 1, ptb=100.0)
    window_b = _make_window("B", "win-b", end_time=now + 300, ptb=101.0)

    # no _seed_hires — price_at returns None
    await bot._on_book_update(window_a, "up")
    await bot._on_book_update(window_b, "up")

    rows = [r for r in get_snapshots_for_window(str(db_path), "win-a") if r["snapshot_type"] == "close"]
    assert len(rows) == 1
    assert rows[0]["final_price"] is None
    assert rows[0]["outcome"] is None


@pytest.mark.asyncio
async def test_phase1_tie_yields_null_outcome(tmp_path: Path):
    from polypocket.bot import Bot

    db_path = tmp_path / "p1.db"
    init_db(str(db_path))
    bot = Bot(db_path=str(db_path))
    bot.signal_engine.evaluate = lambda **kwargs: None

    now = time.time()
    window_a = _make_window("A", "win-a", end_time=now - 1, ptb=100.0)
    window_b = _make_window("B", "win-b", end_time=now + 300, ptb=101.0)

    _seed_hires(bot, ts=window_a.end_time, price=100.0)  # exact tie
    await bot._on_book_update(window_a, "up")
    await bot._on_book_update(window_b, "up")

    rows = [r for r in get_snapshots_for_window(str(db_path), "win-a") if r["snapshot_type"] == "close"]
    assert len(rows) == 1
    assert rows[0]["final_price"] == 100.0
    assert rows[0]["outcome"] is None


@pytest.mark.asyncio
async def test_phase1_settle_does_not_write_close(tmp_path: Path, monkeypatch):
    """After a paper trade settles, the only close row is the transition one."""
    import polypocket.bot as bot_module
    from polypocket.bot import Bot

    monkeypatch.setattr(bot_module, "TRADING_MODE", "paper")
    db_path = tmp_path / "p1.db"
    init_db(str(db_path))
    bot = Bot(db_path=str(db_path))
    bot.signal_engine.evaluate = lambda **kwargs: Signal(
        side="up", model_p_up=0.75, market_price=0.55, edge=0.20,
        up_edge=0.20, down_edge=-0.20,
    )
    bot.risk.check = lambda: (True, "")
    monkeypatch.setattr(
        "polypocket.bot.execute_paper_trade",
        Mock(return_value=TradeResult(success=True, trade_id=1, pnl=None)),
    )
    monkeypatch.setattr("polypocket.bot.settle_paper_trade", lambda *a, **kw: 4.5)

    async def _resolution_up(_slug):
        return "up"

    monkeypatch.setattr(bot_module, "fetch_resolution", _resolution_up)

    now = time.time()
    window_a = _make_window("A", "win-a", end_time=now + 120, ptb=100.0)
    window_b = _make_window("B", "win-b", end_time=now + 420, ptb=101.0)

    _seed_hires(bot, ts=window_a.end_time, price=105.0)
    await bot._on_book_update(window_a, "up")  # fires trade

    # Advance time past window A's end — next call hits the expired-window
    # settle branch (which must NOT write close) and then the transition
    # into window B (which DOES write the single close row).
    monkeypatch.setattr(bot_module.time, "time", lambda: window_a.end_time + 1)
    await bot._on_book_update(window_b, "up")

    rows = [r for r in get_snapshots_for_window(str(db_path), "win-a") if r["snapshot_type"] == "close"]
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Phase 5 (G5): mid-window book samples
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase5_first_sample_deferred_past_window_open(tmp_path: Path):
    """Opening a new window must NOT emit a book sample — the `open` snapshot
    already covers t=0. First sample lands only once +30s has elapsed."""
    from polypocket.bot import Bot
    from polypocket.ledger import get_book_samples

    db_path = tmp_path / "p5.db"
    init_db(str(db_path))
    bot = Bot(db_path=str(db_path))
    bot.binance.latest_price = 84250.0
    bot.signal_engine.evaluate = lambda **kwargs: None

    now = time.time()
    window = _make_window("A", "win-a", end_time=now + 300, ptb=84198.0)

    await bot._on_book_update(window, "up")
    assert get_book_samples(str(db_path), "win-a") == []


@pytest.mark.asyncio
async def test_phase5_sample_emitted_after_30s(tmp_path: Path, monkeypatch):
    """Drive two updates ≥30s apart → two samples. Two updates <30s apart → one."""
    import polypocket.bot as bot_module
    from polypocket.bot import Bot
    from polypocket.ledger import get_book_samples

    db_path = tmp_path / "p5.db"
    init_db(str(db_path))
    bot = Bot(db_path=str(db_path))
    bot.binance.latest_price = 84250.0
    bot.signal_engine.evaluate = lambda **kwargs: None

    t0 = 10_000.0
    window = _make_window("A", "win-a", end_time=t0 + 300, ptb=84198.0)

    monkeypatch.setattr(bot_module.time, "time", lambda: t0)
    await bot._on_book_update(window, "up")  # transition sets _last_book_sample_ts=t0

    monkeypatch.setattr(bot_module.time, "time", lambda: t0 + 30.0)
    await bot._on_book_update(window, "up")  # 30s elapsed → sample
    monkeypatch.setattr(bot_module.time, "time", lambda: t0 + 45.0)
    await bot._on_book_update(window, "up")  # 15s since last → no sample
    monkeypatch.setattr(bot_module.time, "time", lambda: t0 + 60.0)
    await bot._on_book_update(window, "up")  # 30s since last → sample

    samples = get_book_samples(str(db_path), "win-a")
    assert [s["sampled_at"] for s in samples] == [t0 + 30.0, t0 + 60.0]


@pytest.mark.asyncio
async def test_phase5_new_window_resets_cadence(tmp_path: Path, monkeypatch):
    """A transition into a new window must reset the sample clock — first
    sample of the new window is 30s from the transition, not from the last
    sample of the previous window."""
    import polypocket.bot as bot_module
    from polypocket.bot import Bot
    from polypocket.ledger import get_book_samples

    db_path = tmp_path / "p5.db"
    init_db(str(db_path))
    bot = Bot(db_path=str(db_path))
    bot.binance.latest_price = 84250.0
    bot.signal_engine.evaluate = lambda **kwargs: None

    t0 = 10_000.0
    window_a = _make_window("A", "win-a", end_time=t0 + 300, ptb=100.0)
    window_b = _make_window("B", "win-b", end_time=t0 + 580, ptb=101.0)

    monkeypatch.setattr(bot_module.time, "time", lambda: t0)
    await bot._on_book_update(window_a, "up")
    monkeypatch.setattr(bot_module.time, "time", lambda: t0 + 270.0)
    await bot._on_book_update(window_a, "up")  # sample @ t0+270 for A
    assert len(get_book_samples(str(db_path), "win-a")) == 1

    # Transition to B at t0+290 (past A's end=t0+300? no — 290 < 300 so A not
    # expired; but condition_id mismatch + incoming_is_live(B) is the trigger.
    # B.start_time = end - 300 = t0+280, so B is live at t0+290).
    monkeypatch.setattr(bot_module.time, "time", lambda: t0 + 290.0)
    bot.binance._hires.append((window_a.end_time, 100.0))
    await bot._on_book_update(window_b, "up")  # transition; no sample for B
    assert get_book_samples(str(db_path), "win-b") == []

    # 10s into B (t0+300): under cadence
    monkeypatch.setattr(bot_module.time, "time", lambda: t0 + 300.0)
    await bot._on_book_update(window_b, "up")
    assert get_book_samples(str(db_path), "win-b") == []

    # 30s past transition (t0+320): first sample for B
    monkeypatch.setattr(bot_module.time, "time", lambda: t0 + 320.0)
    await bot._on_book_update(window_b, "up")
    assert len(get_book_samples(str(db_path), "win-b")) == 1


# ---------------------------------------------------------------------------
# Post-only / maker-side entries (2026-05-15)
# ---------------------------------------------------------------------------


def _post_only_window(slug="btc-updown-5m-po", end_offset=180.0):
    return Window(
        condition_id="po-cond",
        question="BTC Up or Down",
        up_token_id="tok_up",
        down_token_id="tok_down",
        end_time=time.time() + end_offset,
        slug=slug,
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
        up_bids=[{"price": 0.50, "size": 100.0}],
        down_bids=[{"price": 0.45, "size": 100.0}],
    )


@pytest.mark.asyncio
async def test_bot_dispatches_to_post_only_when_entry_mode_set(tmp_path: Path, monkeypatch):
    """ENTRY_MODE=post_only routes the live branch to execute_live_trade_post_only,
    NOT to execute_live_trade."""
    import polypocket.bot as bot_module
    from polypocket.bot import Bot

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))

    monkeypatch.setattr(bot_module, "TRADING_MODE", "live")
    monkeypatch.setattr(bot_module, "ENTRY_MODE", "post_only")

    fak_mock = Mock(return_value=TradeResult(success=False, error="should-not-fire"))
    post_only_mock = Mock(return_value=TradeResult(success=True, trade_id=1, pnl=None))
    monkeypatch.setattr(bot_module, "execute_live_trade", fak_mock)
    monkeypatch.setattr(bot_module, "execute_live_trade_post_only", post_only_mock)

    client = Mock()
    client.get_usdc_balance.return_value = 100.0

    bot = Bot(db_path=str(db_path), live_order_client=client)
    bot.binance.latest_price = 84350.0
    bot.signal_engine.evaluate = Mock(return_value=Signal(
        side="up", model_p_up=0.75, market_price=0.55, edge=0.20,
        up_edge=0.20, down_edge=-0.20, signal_reference_price=0.55,
    ))
    bot.risk.check = lambda: (True, "")

    window = _post_only_window()
    window.book_updated_at = time.monotonic()

    # Have the mocked dispatch write the placed row, mirroring what the
    # real execute_live_trade_post_only would do — otherwise the bot's
    # post-dispatch find_trade_by_window_slug returns None and the
    # _open_trade fall-through uses the dispatch-call's intended_size.
    def _fake_post_only_dispatch(**kwargs):
        log_trade(
            db_path=str(db_path),
            window_slug=kwargs["window_slug"],
            side="up", entry_price=0.53, size=kwargs["intended_size"], fees=0.0,
            model_p_up=0.75, market_p_up=0.55, edge=0.20,
            outcome=None, pnl=None, status="placed",
            entry_mode="post_only", rest_price=0.53,
        )
        return TradeResult(success=True, trade_id=1, pnl=None)

    post_only_mock.side_effect = _fake_post_only_dispatch

    await bot._on_book_update(window, "up")

    post_only_mock.assert_called_once()
    fak_mock.assert_not_called()
    # Dispatch passed offset_ticks + expiration. Expiration is the max of
    # (window.end_time - POST_ONLY_EXPIRY_SAFETY_BUFFER_S) and the server's
    # required minimum (now + POLYMARKET_MIN_EXPIRATION_BUFFER_S). With
    # the default buffers (60 and 65) and the helper's end_offset=180,
    # both candidates are <= now + 120 so the floor wins.
    kwargs = post_only_mock.call_args.kwargs
    assert kwargs["offset_ticks"] == 2  # default POST_ONLY_REST_OFFSET_TICKS
    expected_target = int(window.end_time - 60.0)
    expected_floor = int(time.time()) + 65
    expected_min = max(expected_target, expected_floor)
    # Allow a 1s slack — wall clock may have ticked between bot call and assertion.
    assert abs(kwargs["expiration"] - expected_min) <= 1, (
        f"expected ~{expected_min}, got {kwargs['expiration']}"
    )


@pytest.mark.asyncio
async def test_bot_cancels_post_only_at_t_remaining_threshold(tmp_path: Path, monkeypatch):
    """When a resting post-only is open and t_remaining <= threshold,
    the bot tick fires cancel_post_only_order with trigger='window-close'."""
    import polypocket.bot as bot_module
    from polypocket.bot import Bot

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))

    monkeypatch.setattr(bot_module, "TRADING_MODE", "live")

    # Pre-populate a placed trade in the DB.
    trade_id = log_trade(
        db_path=str(db_path),
        window_slug="btc-updown-5m-po-cancel",
        side="up", entry_price=0.53, size=10.0, fees=0.0,
        model_p_up=0.75, market_p_up=0.55, edge=0.20,
        outcome=None, pnl=None, status="placed",
        entry_mode="post_only", rest_price=0.53,
    )
    from polypocket.ledger import update_trade
    update_trade(str(db_path), trade_id, outcome=None, pnl=None, status="placed",
                 external_order_id="po-1")

    cancel_mock = Mock(return_value="rejected")
    monkeypatch.setattr(bot_module, "cancel_post_only_order", cancel_mock)

    bot = Bot(db_path=str(db_path), live_order_client=Mock())
    bot.binance.latest_price = 84000.0
    bot.signal_engine.evaluate = Mock(return_value=None)
    bot.risk.check = lambda: (True, "")
    bot._open_trade = {
        "trade_id": trade_id, "side": "up", "entry_price": 0.53, "size": 10.0,
        "mode": "live", "status": "placed", "external_order_id": "po-1",
    }
    bot._current_window = _post_only_window(slug="btc-updown-5m-po-cancel", end_offset=10.0)
    bot._current_window_id = bot._current_window.condition_id
    bot._window_traded = True  # block signal eval re-fire on this same tick

    # t_remaining = 10s on the window — under the 30s threshold.
    await bot._on_book_update(bot._current_window, "up")

    cancel_mock.assert_called_once()
    _, _, _, kwargs_or_trigger = cancel_mock.call_args.args + (None,) * (4 - len(cancel_mock.call_args.args))
    assert cancel_mock.call_args.kwargs.get("trigger") == "window-close"
    # Returned 'rejected' → open_trade dropped, status set.
    assert bot._open_trade is None
    assert bot.stats["execution_status"] == "post-only-no-fill"


@pytest.mark.asyncio
async def test_bot_post_only_cancel_promotes_partial_fill(tmp_path: Path, monkeypatch):
    """cancel_post_only_order returns 'open' (partial fill detected) → bot
    adopts the partial fill as the open position."""
    import polypocket.bot as bot_module
    from polypocket.bot import Bot
    from polypocket.ledger import update_trade

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))

    monkeypatch.setattr(bot_module, "TRADING_MODE", "live")

    trade_id = log_trade(
        db_path=str(db_path),
        window_slug="btc-updown-5m-po-partial",
        side="up", entry_price=0.53, size=10.0, fees=0.0,
        model_p_up=0.75, market_p_up=0.55, edge=0.20,
        outcome=None, pnl=None, status="placed",
        entry_mode="post_only", rest_price=0.53,
    )
    update_trade(str(db_path), trade_id, outcome=None, pnl=None, status="placed",
                 external_order_id="po-2")

    # Simulate cancel-reconcile promoting to open with size=4 partial.
    def fake_cancel(db_path_arg, trade_row, client, trigger):
        update_trade(db_path_arg, trade_row["id"], outcome=None, pnl=None,
                     status="open", size=4.0, entry_price=0.53)
        return "open"

    monkeypatch.setattr(bot_module, "cancel_post_only_order", fake_cancel)

    bot = Bot(db_path=str(db_path), live_order_client=Mock())
    bot.binance.latest_price = 84000.0
    bot.signal_engine.evaluate = Mock(return_value=None)
    bot.risk.check = lambda: (True, "")
    bot._open_trade = {
        "trade_id": trade_id, "side": "up", "entry_price": 0.53, "size": 10.0,
        "mode": "live", "status": "placed", "external_order_id": "po-2",
    }
    bot._current_window = _post_only_window(slug="btc-updown-5m-po-partial", end_offset=10.0)
    bot._current_window_id = bot._current_window.condition_id
    bot._window_traded = True

    await bot._on_book_update(bot._current_window, "up")

    assert bot._open_trade is not None
    assert bot._open_trade["status"] == "open"
    assert bot._open_trade["size"] == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_paper_path_unchanged_with_post_only_config_set(tmp_path: Path, monkeypatch):
    """Bit-identity guarantee: ENTRY_MODE=post_only in paper mode must NOT
    enter any post-only code path. Paper continues to call execute_paper_trade
    with no submit_post_only call on any client mock."""
    import polypocket.bot as bot_module
    from polypocket.bot import Bot

    monkeypatch.setattr(bot_module, "TRADING_MODE", "paper")
    monkeypatch.setattr(bot_module, "ENTRY_MODE", "post_only")  # set but irrelevant

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))

    paper_mock = Mock(return_value=TradeResult(success=True, trade_id=1, pnl=None))
    post_only_mock = Mock(return_value=TradeResult(success=False, error="should-not-fire"))
    monkeypatch.setattr(bot_module, "execute_paper_trade", paper_mock)
    monkeypatch.setattr(bot_module, "execute_live_trade_post_only", post_only_mock)

    bot = Bot(db_path=str(db_path))
    bot.binance.latest_price = 84350.0
    bot.signal_engine.evaluate = lambda **kwargs: Signal(
        side="up", model_p_up=0.75, market_price=0.55, edge=0.20,
        up_edge=0.20, down_edge=-0.20, signal_reference_price=0.55,
    )
    bot.risk.check = lambda: (True, "")

    window = _post_only_window(slug="btc-updown-5m-paper-po")
    await bot._on_book_update(window, "up")

    paper_mock.assert_called_once()
    post_only_mock.assert_not_called()


def test_recoverable_statuses_includes_placed_in_live(monkeypatch):
    """Structural check: live-mode recovery sweeps over 'placed' so a
    resting post-only order from a previous run gets cancel-and-reconciled.
    Verified indirectly via the source — string match on the placed-add line."""
    import inspect, polypocket.bot as bot_module
    src = inspect.getsource(bot_module._on_book_update if False else bot_module.Bot._on_book_update)
    assert 'recoverable_statuses.add("placed")' in src
