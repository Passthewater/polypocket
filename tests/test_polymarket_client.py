from unittest.mock import MagicMock, patch

import pytest

from polypocket.clients.polymarket import (
    PolymarketClient, _tick_safe_size, fok_limit_price, ioc_limit_price,
)


@pytest.fixture
def mock_clob():
    with patch("polypocket.clients.polymarket.ClobClient") as cls:
        yield cls


def _make_client(mock_clob_cls, dry_run=False):
    instance = mock_clob_cls.return_value
    # Polymarket returns USDC balance in raw 6-decimal on-chain units.
    # 1_234_500_000 raw = $1234.50.
    instance.get_balance_allowance.return_value = {"balance": "1234500000"}
    # Default: BTC up/down markets report taker_base_fee=1000.
    instance.get_market.return_value = {"taker_base_fee": 1000}
    return PolymarketClient(
        host="https://clob.polymarket.com", chain_id=137,
        private_key="0x" + "1" * 64,
        api_creds={"key": "k", "secret": "s", "passphrase": "p"},
        proxy_address="0x" + "2" * 40,
        dry_run=dry_run,
    ), instance


def test_submit_fok_filled(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_market_order.return_value = MagicMock()
    inst.post_order.return_value = {"success": True, "status": "matched", "orderID": "abc"}
    inst.get_order.return_value = {"status": "matched", "size_matched": "7.0"}

    fill = client.submit_fok(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND")

    assert fill.status == "filled"
    assert fill.order_id == "abc"
    assert fill.filled_size == pytest.approx(7.0)
    inst.post_order.assert_called_once()


def test_submit_fok_passes_market_fee_rate(mock_clob):
    """MarketOrderArgs.fee_rate_bps must come from the market's taker_base_fee."""
    client, inst = _make_client(mock_clob)
    inst.get_market.return_value = {"taker_base_fee": 1000}
    inst.create_market_order.return_value = MagicMock()
    inst.post_order.return_value = {"success": True, "status": "matched", "orderID": "abc"}
    inst.get_order.return_value = {"status": "matched", "size_matched": "7.0"}

    client.submit_fok(side="up", price=0.51, size=7.0,
                      token_id="TKN-UP", condition_id="0xCOND")

    inst.create_market_order.assert_called_once()
    args = inst.create_market_order.call_args.args[0]
    assert args.fee_rate_bps == 1000
    assert args.token_id == "TKN-UP"
    # amount = USDC budget at the target price (2-dp precision rule)
    assert args.amount == pytest.approx(round(7.0 * 0.51, 2))
    # price = limit (max) price, with FOK_SLIPPAGE_TICKS buffer so taker
    # can sweep thin levels instead of killing at the quoted ask
    from polypocket.config import FOK_SLIPPAGE_TICKS
    assert args.price == pytest.approx(round(0.51 + FOK_SLIPPAGE_TICKS * 0.01, 2))


def test_submit_fok_limit_price_capped_at_99c(mock_clob):
    """Limit price must never exceed $0.99 — Polymarket rejects price==1.0."""
    client, inst = _make_client(mock_clob)
    inst.create_market_order.return_value = MagicMock()
    inst.post_order.return_value = {"success": True, "status": "matched", "orderID": "x"}
    inst.get_order.return_value = {"status": "matched", "size_matched": "1.0"}

    client.submit_fok(side="up", price=0.98, size=1.0,
                      token_id="TKN-UP", condition_id="0xCOND")

    args = inst.create_market_order.call_args.args[0]
    assert args.price <= 0.99


def test_submit_fok_caches_market_fee(mock_clob):
    """get_market must be called only once per condition_id across submissions."""
    client, inst = _make_client(mock_clob)
    inst.create_market_order.return_value = MagicMock()
    inst.post_order.return_value = {"success": True, "status": "matched", "orderID": "x"}
    inst.get_order.return_value = {"status": "matched", "size_matched": "1.0"}

    for _ in range(3):
        client.submit_fok(side="up", price=0.51, size=1.0,
                          token_id="TKN-UP", condition_id="0xCOND")

    assert inst.get_market.call_count == 1


def test_submit_fok_market_lookup_failure_uses_zero_fee(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.get_market.side_effect = RuntimeError("market lookup down")
    inst.create_market_order.return_value = MagicMock()
    inst.post_order.return_value = {"success": True, "status": "matched", "orderID": "x"}
    inst.get_order.return_value = {"status": "matched", "size_matched": "1.0"}

    client.submit_fok(side="up", price=0.51, size=1.0,
                      token_id="TKN-UP", condition_id="0xCOND")

    args = inst.create_market_order.call_args.args[0]
    assert args.fee_rate_bps == 0


def test_submit_fok_success_but_unmatched_is_rejected(mock_clob):
    """FOK: `success=True, status='unmatched'` must NOT be recorded as filled."""
    client, inst = _make_client(mock_clob)
    inst.create_market_order.return_value = MagicMock()
    inst.post_order.return_value = {"success": True, "status": "unmatched"}

    fill = client.submit_fok(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND")

    assert fill.status == "rejected"
    assert fill.order_id is None
    assert "unmatched" in fill.error
    inst.get_order.assert_not_called()


def test_submit_fok_rejected(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_market_order.return_value = MagicMock()
    inst.post_order.return_value = {"success": False, "errorMsg": "not matched"}

    fill = client.submit_fok(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND")

    assert fill.status == "rejected"
    assert fill.error == "not matched"
    assert fill.order_id is None
    inst.get_order.assert_not_called()


def test_submit_fok_network_error(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_market_order.side_effect = RuntimeError("boom")

    fill = client.submit_fok(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND")

    assert fill.status == "error"
    assert "boom" in fill.error


def test_submit_fok_dry_run_does_not_post(mock_clob):
    client, inst = _make_client(mock_clob, dry_run=True)

    fill = client.submit_fok(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND")

    assert fill.status == "filled"
    assert fill.order_id == "DRY-RUN"
    inst.create_market_order.assert_not_called()
    inst.post_order.assert_not_called()
    inst.get_market.assert_not_called()


def test_get_usdc_balance_converts_raw_units_to_dollars(mock_clob):
    """Polymarket /balance-allowance returns 6-decimal raw units; client must divide."""
    client, inst = _make_client(mock_clob)
    inst.get_balance_allowance.return_value = {"balance": "42700000"}  # $42.70 raw

    bal = client.get_usdc_balance()

    assert bal == pytest.approx(42.70)
    call = inst.get_balance_allowance.call_args
    assert call is not None


def test_get_usdc_balance_handles_empty_wallet(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.get_balance_allowance.return_value = {"balance": "0"}

    assert client.get_usdc_balance() == pytest.approx(0.0)


def test_fok_limit_price_adds_slippage_ticks():
    from polypocket.config import FOK_SLIPPAGE_TICKS
    assert fok_limit_price(0.40) == pytest.approx(round(0.40 + FOK_SLIPPAGE_TICKS * 0.01, 2))
    assert fok_limit_price(0.51) == pytest.approx(round(0.51 + FOK_SLIPPAGE_TICKS * 0.01, 2))


def test_fok_limit_price_capped_at_99c():
    """Polymarket rejects price >= 1.0; helper must cap."""
    assert fok_limit_price(0.98) <= 0.99
    assert fok_limit_price(0.99) == 0.99
    assert fok_limit_price(1.00) == 0.99


def test_ioc_limit_price_buy_up_uses_down_best_bid():
    """BUY UP crosses via pair-merge: limit = 1 - best_down_bid + buffer."""
    down_bids = [
        {"price": 0.69, "size": 100.0},
        {"price": 0.68, "size": 200.0},
    ]
    limit = ioc_limit_price(
        side="up", up_bids=[], down_bids=down_bids, buffer_ticks=5,
    )
    # 1 - 0.69 + 0.05 = 0.36
    assert limit == pytest.approx(0.36)


def test_ioc_limit_price_buy_down_uses_up_best_bid():
    up_bids = [
        {"price": 0.55, "size": 100.0},
    ]
    limit = ioc_limit_price(
        side="down", up_bids=up_bids, down_bids=[], buffer_ticks=3,
    )
    # 1 - 0.55 + 0.03 = 0.48
    assert limit == pytest.approx(0.48)


def test_ioc_limit_price_none_when_no_opposite_bid():
    """No counterparty on opposite side → no pair-merge possible."""
    assert ioc_limit_price(side="up", up_bids=[], down_bids=[], buffer_ticks=5) is None
    assert ioc_limit_price(side="up", up_bids=[], down_bids=None, buffer_ticks=5) is None


def test_ioc_limit_price_capped_at_99c():
    """Tiny opposite bid (e.g., 0.01) → uncapped limit would be 1.04; cap at 0.99."""
    down_bids = [{"price": 0.01, "size": 50.0}]
    limit = ioc_limit_price(
        side="up", up_bids=[], down_bids=down_bids, buffer_ticks=5,
    )
    assert limit == 0.99


def test_ioc_limit_price_uses_highest_opposite_bid():
    """Multiple bids: uses the max (best) regardless of input order."""
    down_bids = [
        {"price": 0.50, "size": 100.0},
        {"price": 0.72, "size": 10.0},   # best
        {"price": 0.60, "size": 50.0},
    ]
    limit = ioc_limit_price(
        side="up", up_bids=[], down_bids=down_bids, buffer_ticks=2,
    )
    # 1 - 0.72 + 0.02 = 0.30
    assert limit == pytest.approx(0.30)


def test_get_settlement_info_sums_trades(mock_clob):
    """shares_held applies fee per fill; cost sums fill size × fill price."""
    client, inst = _make_client(mock_clob)
    inst.get_order.return_value = {
        "status": "MATCHED",
        "associate_trades": ["t1"],
    }
    inst.get_trades.return_value = [{
        "id": "t1",
        "taker_order_id": "abc-order",
        "size": "10.0",
        "price": "0.55",
        "fee_rate_bps": "1000",
    }]

    info = client.get_settlement_info("abc-order")

    inst.get_order.assert_called_once_with("abc-order")
    assert info.shares_held == pytest.approx(10.0 * 0.9)
    assert info.cost_usdc == pytest.approx(10.0 * 0.55)


def test_get_settlement_info_uses_taker_fill_price_not_order_limit(mock_clob):
    """Regression for live trade #17: /order.price was 0.48 (limit-ish) but
    the taker's real fill was $0.41/share via pair-match. Must use /trades."""
    client, inst = _make_client(mock_clob)
    inst.get_order.return_value = {
        "status": "MATCHED",
        "size_matched": "24.390242",
        "price": "0.48",  # NOT the real fill rate — must be ignored
        "associate_trades": ["4aa180d7"],
    }
    inst.get_trades.return_value = [{
        "id": "4aa180d7",
        "taker_order_id": "0xOID",
        "size": "24.390242",
        "price": "0.41",
        "fee_rate_bps": "1000",
    }]

    info = client.get_settlement_info("0xOID")

    assert info.cost_usdc == pytest.approx(24.390242 * 0.41)
    assert info.shares_held == pytest.approx(24.390242 * 0.9)


def test_get_settlement_info_multiple_trades(mock_clob):
    """Partial fills across multiple trades sum to total cost and shares."""
    client, inst = _make_client(mock_clob)
    inst.get_order.return_value = {
        "associate_trades": ["t1", "t2"],
    }

    def _trades_side_effect(params):
        if params.id == "t1":
            return [{
                "id": "t1", "taker_order_id": "0xOID",
                "size": "5.0", "price": "0.40", "fee_rate_bps": "1000",
            }]
        if params.id == "t2":
            return [{
                "id": "t2", "taker_order_id": "0xOID",
                "size": "3.0", "price": "0.42", "fee_rate_bps": "1000",
            }]
        return []
    inst.get_trades.side_effect = _trades_side_effect

    info = client.get_settlement_info("0xOID")

    assert info.cost_usdc == pytest.approx(5.0 * 0.40 + 3.0 * 0.42)
    assert info.shares_held == pytest.approx((5.0 + 3.0) * 0.9)


def test_get_settlement_info_ignores_fills_for_other_orders(mock_clob):
    """A /trades response may include fills whose taker_order_id is not ours."""
    client, inst = _make_client(mock_clob)
    inst.get_order.return_value = {"associate_trades": ["t1"]}
    inst.get_trades.return_value = [
        {
            "id": "t1", "taker_order_id": "0xOTHER",
            "size": "100.0", "price": "0.99", "fee_rate_bps": "1000",
        },
        {
            "id": "t1", "taker_order_id": "0xOID",
            "size": "2.0", "price": "0.50", "fee_rate_bps": "0",
        },
    ]

    info = client.get_settlement_info("0xOID")

    assert info.cost_usdc == pytest.approx(1.0)
    assert info.shares_held == pytest.approx(2.0)


def test_get_settlement_info_no_matches_returns_zeros(mock_clob):
    """Unmatched/canceled orders have no associate_trades."""
    client, inst = _make_client(mock_clob)
    inst.get_order.return_value = {"status": "CANCELED", "associate_trades": []}

    info = client.get_settlement_info("0xOID")

    assert info.shares_held == 0.0
    assert info.cost_usdc == 0.0
    inst.get_trades.assert_not_called()


def test_get_settlement_info_dry_run_returns_zeros(mock_clob):
    client, inst = _make_client(mock_clob, dry_run=True)
    info = client.get_settlement_info("DRY-RUN")
    assert info.shares_held == 0.0
    assert info.cost_usdc == 0.0
    inst.get_order.assert_not_called()


def test_cancel_order_success(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.cancel.return_value = {"canceled": ["abc"]}
    ok = client.cancel_order("abc")
    assert ok is True
    inst.cancel.assert_called_once_with(order_id="abc")


def test_cancel_order_dry_run(mock_clob):
    client, _ = _make_client(mock_clob, dry_run=True)
    assert client.cancel_order("DRY-RUN") is True
    assert client.cancel_order("anything") is True


def test_cancel_order_retries_then_succeeds(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.cancel.side_effect = [Exception("transient"), {"canceled": ["abc"]}]
    ok = client.cancel_order("abc")
    assert ok is True
    assert inst.cancel.call_count == 2


def test_cancel_order_gives_up_after_retries(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.cancel.side_effect = Exception("persistent")
    ok = client.cancel_order("abc")
    assert ok is False
    assert inst.cancel.call_count == 3  # 1 + 2 retries (CANCEL_RETRY_MAX=2)


def test_submit_ioc_full_match(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_market_order.return_value = MagicMock()
    inst.post_order.return_value = {
        "success": True, "status": "matched", "orderID": "abc",
    }
    # get_order returns fully-matched (size_matched == size)
    inst.get_order.return_value = {
        "size_matched": "7.0",
        "associate_trades": ["t1"],
    }
    inst.get_trades.return_value = [
        {"taker_order_id": "abc", "size": "7.0", "price": "0.51", "fee_rate_bps": 1000},
    ]

    fill = client.submit_ioc(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND",
                             limit_price=0.57)

    assert fill.status == "filled"
    assert fill.order_id == "abc"
    # shares_held = 7.0 * (1 - 0.10) = 6.3
    assert fill.filled_size == pytest.approx(6.3, abs=0.001)
    inst.cancel.assert_not_called()


def test_submit_ioc_partial_match(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_market_order.return_value = MagicMock()
    # Server says "matched" but get_order shows a smaller size_matched than
    # we asked for — realistic response when only part of the book crossed.
    inst.post_order.return_value = {
        "success": True, "status": "matched", "orderID": "abc",
    }
    inst.get_order.return_value = {
        "size_matched": "3.0",
        "associate_trades": ["t1"],
    }
    inst.get_trades.return_value = [
        {"taker_order_id": "abc", "size": "3.0", "price": "0.51", "fee_rate_bps": 1000},
    ]
    inst.cancel.return_value = {"canceled": ["abc"]}

    fill = client.submit_ioc(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND",
                             limit_price=0.57)

    assert fill.status == "filled"
    assert fill.order_id == "abc"
    assert fill.filled_size == pytest.approx(2.7, abs=0.001)  # 3.0 * 0.9
    # avg_price = cost_usdc / shares_held = (3.0 * 0.51) / (3.0 * 0.9) ≈ 0.5667
    assert fill.avg_price == pytest.approx(0.5667, abs=0.001)
    inst.cancel.assert_called_once_with(order_id="abc")


def test_submit_ioc_no_match_returns_rejected(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_market_order.return_value = MagicMock()
    inst.post_order.return_value = {
        "success": True, "status": "unmatched", "orderID": "abc",
    }
    inst.get_order.return_value = {"size_matched": "0", "associate_trades": []}
    inst.cancel.return_value = {"canceled": ["abc"]}

    fill = client.submit_ioc(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND",
                             limit_price=0.57)

    assert fill.status == "rejected"
    assert fill.error == "gtc-no-fill"
    assert fill.filled_size == 0.0
    inst.cancel.assert_called_once()


def test_submit_ioc_settlement_failure_preserves_order_id(mock_clob):
    """get_settlement_info failure must still return order_id so reconciler can recover."""
    client, inst = _make_client(mock_clob)
    inst.create_market_order.return_value = MagicMock()
    inst.post_order.return_value = {
        "success": True, "status": "matched", "orderID": "abc",
    }
    inst.get_order.return_value = {"size_matched": "7.0", "associate_trades": []}
    # get_settlement_info internally calls get_order again then get_trades;
    # make the second get_order call raise to simulate a lookup failure.
    inst.get_order.side_effect = [
        {"size_matched": "7.0"},   # first call: check fully_matched
        RuntimeError("CLOB 500"),  # second call: inside get_settlement_info
    ]

    fill = client.submit_ioc(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND",
                             limit_price=0.57)

    assert fill.status == "error"
    assert fill.order_id == "abc"
    assert "settlement-lookup" in fill.error


def test_submit_ioc_post_raises_returns_error(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_market_order.side_effect = Exception("network down")

    fill = client.submit_ioc(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND",
                             limit_price=0.57)

    assert fill.status == "error"
    assert "network" in fill.error
    inst.cancel.assert_not_called()


def test_submit_ioc_success_false_is_rejected(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_market_order.return_value = MagicMock()
    inst.post_order.return_value = {
        "success": False, "errorMsg": "fee mismatch",
    }

    fill = client.submit_ioc(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND",
                             limit_price=0.57)

    assert fill.status == "rejected"
    assert "fee mismatch" in fill.error
    inst.cancel.assert_not_called()


def test_submit_ioc_cancel_fails_still_returns_fill(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_market_order.return_value = MagicMock()
    inst.post_order.return_value = {
        "success": True, "status": "matched", "orderID": "abc",
    }
    inst.get_order.return_value = {
        "size_matched": "3.0", "associate_trades": ["t1"],
    }
    inst.get_trades.return_value = [
        {"taker_order_id": "abc", "size": "3.0", "price": "0.51", "fee_rate_bps": 1000},
    ]
    inst.cancel.side_effect = Exception("persistent")

    fill = client.submit_ioc(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND",
                             limit_price=0.57)

    # Cancel failure is logged but doesn't flip success — we have a real fill.
    assert fill.status == "filled"
    assert fill.filled_size == pytest.approx(2.7, abs=0.001)


def test_submit_ioc_dry_run(mock_clob):
    client, _ = _make_client(mock_clob, dry_run=True)
    fill = client.submit_ioc(side="up", price=0.51, size=7.0,
                             token_id="TKN", condition_id="COND",
                             limit_price=0.57)
    assert fill.status == "filled"
    assert fill.filled_size == 7.0


def test_submit_ioc_uses_explicit_limit_price(mock_clob):
    """submit_ioc must post at the caller-supplied limit_price, not fok_limit_price(price)."""
    client, inst = _make_client(mock_clob)
    inst.create_market_order.return_value = MagicMock()
    inst.post_order.return_value = {
        "success": True, "status": "matched", "orderID": "abc",
    }
    inst.get_order.return_value = {"size_matched": "7.0", "associate_trades": []}

    # price=0.51, limit_price=0.42 (below same-side ask — pair-merge scenario)
    client.submit_ioc(side="up", price=0.51, size=7.0,
                      token_id="TKN-UP", condition_id="0xCOND",
                      limit_price=0.42)

    args = inst.create_market_order.call_args.args[0]
    assert args.price == pytest.approx(0.42)
    # amount is now anchored to limit_price (not price) since the server
    # reconstructs taker = amount/limit, and that ratio must land on tick grid.
    # size_int = 7 is tick-safe for limit=0.42 (7*0.42=2.94, scaled 294.0 clean).
    assert args.amount == pytest.approx(round(7 * 0.42, 2))


def test_tick_safe_size_picks_target_when_clean():
    """target size already tick-safe → returns target unchanged."""
    # 9 * 0.58 = 5.22 → scaled 522.0 (clean)
    assert _tick_safe_size(9, 0.58) == 9


def test_tick_safe_size_shifts_when_target_drifts():
    """target=7 at limit=0.58: 7*0.58=4.06 scaled to 405.9999 (drift).
    Search finds 8 (also drifts) then 9 (clean)."""
    result = _tick_safe_size(7, 0.58)
    # Must return a value where size * 0.58 passes the floor-vs-round check
    import math
    scaled = result * 0.58 * 100
    assert math.floor(scaled) == round(scaled)


def test_tick_safe_size_handles_known_failing_trade_40():
    """Regression: trade 40 (2026-04-22) failed at size=7.49 limit=0.58."""
    # 7.49 rounds to 7; 7 drifts; search must yield a safe neighbor.
    result = _tick_safe_size(7, 0.58)
    assert result is not None
    assert result >= 1


def test_submit_ioc_quantizes_to_tick_safe_size(mock_clob):
    """submit_ioc must submit an amount whose size*limit survives py_clob_client's round_down."""
    import math
    client, inst = _make_client(mock_clob)
    inst.create_market_order.return_value = MagicMock()
    inst.post_order.return_value = {
        "success": True, "status": "matched", "orderID": "abc",
    }
    inst.get_order.return_value = {"size_matched": "9.0", "associate_trades": []}

    # Real failing case: size=7.49, limit=0.58 → raw ratio lands off-grid.
    client.submit_ioc(side="up", price=0.51, size=7.49,
                      token_id="TKN-UP", condition_id="0xCOND",
                      limit_price=0.58)

    args = inst.create_market_order.call_args.args[0]
    scaled = args.amount * 100
    # Core invariant: floor-scaled amount equals round-scaled (no drift).
    assert math.floor(scaled) == round(scaled), (
        f"submit_ioc picked drift-prone amount {args.amount} at limit {args.price}"
    )
    # And amount is exactly size_int * limit for some integer size_int >= 1.
    ratio = args.amount / args.price
    assert abs(ratio - round(ratio)) < 1e-6
    assert round(ratio) >= 1


def test_submit_ioc_full_match_with_rounding_tolerance(mock_clob):
    """size_matched=6.995 vs size=7.0: within 0.01 cents so treated as fully matched.

    Server-side fee rounding can produce a floor-rounded size_matched that's
    off by < 0.01 shares; must skip cancel (server rejects cancel of filled).
    """
    client, inst = _make_client(mock_clob)
    inst.create_market_order.return_value = MagicMock()
    inst.post_order.return_value = {
        "success": True, "status": "matched", "orderID": "abc",
    }
    # size_matched is 0.005 short of 7.0 — within the 0.01 tolerance
    inst.get_order.return_value = {
        "size_matched": "6.995",
        "associate_trades": ["t1"],
    }
    inst.get_trades.return_value = [
        {"taker_order_id": "abc", "size": "6.995", "price": "0.51", "fee_rate_bps": 1000},
    ]

    fill = client.submit_ioc(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND",
                             limit_price=0.57)

    assert fill.status == "filled"
    inst.cancel.assert_not_called()
