from unittest.mock import MagicMock, patch

import pytest

from polypocket.clients.polymarket import PolymarketClient, fok_limit_price
from polypocket.executor import FillResult


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
