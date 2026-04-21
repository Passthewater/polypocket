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
    assert len(close) == 1
    assert close[0]["outcome"] == "up"
    assert close[0]["trade_fired"] == 1


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

    # Verify close has outcome
    close = next(s for s in snapshots if s["snapshot_type"] == "close")
    assert close["outcome"] == "up"


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

        def get_usdc_balance(self):
            return 1000.0

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

    def get_usdc_balance(self):
        return 1000.0


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
    client = _CapturingClient()
    bot = _make_live_bot(tmp_path, monkeypatch, client)

    # With edge=0.20, vol_scale=1 (sigma forced to 0.001 floor), intended =
    # MAX_POSITION_USDC / entry = ~9.09 shares at $0.55.
    # Book holds 8 shares at <= FOK limit (0.55 + 3 ticks = 0.58).
    # ratio = 8*0.9/9.09 = 0.79 > 0.5. Clamp to 8 * 0.9 = 7.2 shares.
    window = Window(
        condition_id="shallow-test",
        question="BTC Up or Down",
        up_token_id="UP", down_token_id="DOWN",
        end_time=time.time() + 180,
        slug="btc-updown-5m-shallow",
        price_to_beat=84198.0,
        up_ask=0.55, down_ask=0.45,
        up_book=[
            {"price": 0.55, "size": 8.0},
            {"price": 0.70, "size": 1000.0},  # outside limit band
        ],
        down_book=[{"price": 0.45, "size": 1000.0}],
        book_updated_at=time.monotonic(),
    )

    await bot._on_book_update(window, "up")

    assert len(client.calls) == 1
    # fillable = 8, clamped = 8 * 0.9 = 7.2. intended is ~9.09 shares
    # (MAX_POSITION_USDC/entry), so the clamp engages.
    assert client.calls[0]["size"] == pytest.approx(7.2, rel=1e-3)


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

    # Same shape as the old test_bot_live_skips_when_book_too_thin: only
    # 3 shares at <= limit. With intended ~ 18 shares, clamp would give
    # 3*0.9=2.7, which is 2.7/18 = 0.15 < MIN_FILL_RATIO (0.5) -> skip.
    window = Window(
        condition_id="thin-test",
        question="BTC Up or Down",
        up_token_id="UP", down_token_id="DOWN",
        end_time=time.time() + 180,
        slug="btc-updown-5m-thin",
        price_to_beat=84198.0,
        up_ask=0.55, down_ask=0.45,
        up_book=[
            {"price": 0.55, "size": 2.0},
            {"price": 0.56, "size": 1.0},
            {"price": 0.70, "size": 1000.0},
        ],
        down_book=[{"price": 0.45, "size": 1000.0}],
        book_updated_at=time.monotonic(),
    )

    await bot._on_book_update(window, "up")

    assert client.calls == []
    assert bot._window_skip_reason == "book-too-thin"


@pytest.mark.asyncio
async def test_bot_live_skips_when_book_empty(tmp_path: Path, monkeypatch):
    """Empty book / None -> skip book-too-thin (fillable=0)."""
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
        up_book=[],
        down_book=[{"price": 0.45, "size": 1000.0}],
        book_updated_at=time.monotonic(),
    )

    await bot._on_book_update(window, "up")

    assert client.calls == []
    assert bot._window_skip_reason == "book-too-thin"


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
        # fillable=8. clamped=7.2. 7.2*0.55=$3.96 < $5 floor.
        up_book=[{"price": 0.55, "size": 8.0}, {"price": 0.70, "size": 1000.0}],
        down_book=[{"price": 0.45, "size": 1000.0}],
        book_updated_at=time.monotonic(),
    )

    await bot._on_book_update(window, "up")

    assert client.calls == []
    assert bot._window_skip_reason == "book-too-thin"
