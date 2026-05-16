from unittest.mock import MagicMock, patch

import pytest

from polypocket.clients.polymarket import (
    PolymarketClient, _tick_safe_size, fok_limit_price, ioc_limit_price,
    post_only_rest_price,
)


@pytest.fixture
def mock_clob():
    with patch("polypocket.clients.polymarket.ClobClient") as cls:
        yield cls


def _make_client(mock_clob_cls, dry_run=False):
    instance = mock_clob_cls.return_value
    # Polymarket returns collateral balance in raw 6-decimal on-chain units.
    # pUSD (the v2-era collateral) is a USDC-backed ERC-20 with the same scale.
    # 1_234_500_000 raw = $1234.50.
    instance.get_balance_allowance.return_value = {"balance": "1234500000"}
    return PolymarketClient(
        host="https://clob.polymarket.com", chain_id=137,
        private_key="0x" + "1" * 64,
        api_creds={"key": "k", "secret": "s", "passphrase": "p"},
        proxy_address="0x" + "2" * 40,
        dry_run=dry_run,
    ), instance


def test_submit_fok_filled(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_and_post_market_order.return_value = {
        "success": True, "status": "matched", "orderID": "abc",
    }
    inst.get_order.return_value = {"status": "matched", "size_matched": "7.0"}

    fill = client.submit_fok(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND")

    assert fill.status == "filled"
    assert fill.order_id == "abc"
    assert fill.filled_size == pytest.approx(7.0)
    inst.create_and_post_market_order.assert_called_once()


def test_submit_fok_passes_v2_market_order_args(mock_clob):
    """v2 MarketOrderArgs: side=Side.BUY, order_type=FOK, no fee_rate_bps."""
    from py_clob_client_v2 import OrderType, Side
    client, inst = _make_client(mock_clob)
    inst.create_and_post_market_order.return_value = {
        "success": True, "status": "matched", "orderID": "abc",
    }
    inst.get_order.return_value = {"status": "matched", "size_matched": "7.0"}

    client.submit_fok(side="up", price=0.51, size=7.0,
                      token_id="TKN-UP", condition_id="0xCOND")

    kwargs = inst.create_and_post_market_order.call_args.kwargs
    args = kwargs["order_args"]
    assert args.token_id == "TKN-UP"
    assert args.side == Side.BUY
    assert args.order_type == OrderType.FOK
    # amount = USDC budget at the target price (2-dp precision rule)
    assert args.amount == pytest.approx(round(7.0 * 0.51, 2))
    # price = limit (max) price, with FOK_SLIPPAGE_TICKS buffer so taker
    # can sweep thin levels instead of killing at the quoted ask
    from polypocket.config import FOK_SLIPPAGE_TICKS
    assert args.price == pytest.approx(round(0.51 + FOK_SLIPPAGE_TICKS * 0.01, 2))
    # tick size passed via PartialCreateOrderOptions
    options = kwargs["options"]
    assert options.tick_size == "0.01"
    # order_type also passed as top-level kwarg
    assert kwargs["order_type"] == OrderType.FOK
    # v2 removes fee_rate_bps from MarketOrderArgs — no such attribute or zero default
    assert getattr(args, "fee_rate_bps", 0) == 0


def test_submit_fok_limit_price_capped_at_99c(mock_clob):
    """Limit price must never exceed $0.99 — Polymarket rejects price==1.0."""
    client, inst = _make_client(mock_clob)
    inst.create_and_post_market_order.return_value = {
        "success": True, "status": "matched", "orderID": "x",
    }
    inst.get_order.return_value = {"status": "matched", "size_matched": "1.0"}

    client.submit_fok(side="up", price=0.98, size=1.0,
                      token_id="TKN-UP", condition_id="0xCOND")

    args = inst.create_and_post_market_order.call_args.kwargs["order_args"]
    assert args.price <= 0.99


def test_submit_fok_success_but_unmatched_is_rejected(mock_clob):
    """FOK: `success=True, status='unmatched'` must NOT be recorded as filled."""
    client, inst = _make_client(mock_clob)
    inst.create_and_post_market_order.return_value = {
        "success": True, "status": "unmatched",
    }

    fill = client.submit_fok(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND")

    assert fill.status == "rejected"
    assert fill.order_id is None
    assert "unmatched" in fill.error
    inst.get_order.assert_not_called()


def test_submit_fok_rejected(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_and_post_market_order.return_value = {
        "success": False, "errorMsg": "not matched",
    }

    fill = client.submit_fok(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND")

    assert fill.status == "rejected"
    assert fill.error == "not matched"
    assert fill.order_id is None
    inst.get_order.assert_not_called()


def test_submit_fok_network_error(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_and_post_market_order.side_effect = RuntimeError("boom")

    fill = client.submit_fok(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND")

    assert fill.status == "error"
    assert "boom" in fill.error


def test_submit_fok_no_match_400_classified_as_rejected(mock_clob):
    """v2 server raises PolyApiException(status=400) when FOK finds no match,
    even though the body carries a real orderID. Must surface as a clean
    `fok-no-fill` rejection with the orderID preserved (not as `status=error`).
    """
    from py_clob_client_v2.exceptions import PolyApiException
    client, inst = _make_client(mock_clob)
    exc = PolyApiException.__new__(PolyApiException)
    exc.status_code = 400
    exc.error_msg = {
        "error": "no orders found to match with FOK order. FOK requires immediate full fill.",
        "orderID": "0xABCDEF",
    }
    inst.create_and_post_market_order.side_effect = exc

    fill = client.submit_fok(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND")

    assert fill.status == "rejected"
    assert fill.error == "fok-no-fill"
    assert fill.order_id == "0xABCDEF"  # preserved for the reconciler
    assert fill.filled_size == 0.0


def test_submit_fok_other_400_still_errors(mock_clob):
    """A 400 that's not a no-match (e.g., insufficient balance) must still
    fall through to the generic network error path — we don't want to mask
    real failures as no-fills."""
    from py_clob_client_v2.exceptions import PolyApiException
    client, inst = _make_client(mock_clob)
    exc = PolyApiException.__new__(PolyApiException)
    exc.status_code = 400
    exc.error_msg = {"error": "not enough balance / allowance"}
    inst.create_and_post_market_order.side_effect = exc

    fill = client.submit_fok(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND")

    assert fill.status == "error"
    assert "network:" in fill.error
    assert "balance" in fill.error


def test_submit_fok_dry_run_does_not_post(mock_clob):
    client, inst = _make_client(mock_clob, dry_run=True)

    fill = client.submit_fok(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND")

    assert fill.status == "filled"
    assert fill.order_id == "DRY-RUN"
    inst.create_and_post_market_order.assert_not_called()


def test_get_usdc_balance_converts_raw_units_to_dollars(mock_clob):
    """Polymarket /balance-allowance returns 6-decimal raw units; client must divide.

    pUSD inherits the same 6-decimal scale as USDC.e, so the conversion is
    unchanged from the v1 era.
    """
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


def test_get_order_book_returns_sdk_payload(mock_clob):
    client, inst = _make_client(mock_clob)
    payload = {
        "bids": [{"price": "0.56", "size": "100"}],
        "asks": [{"price": "0.58", "size": "200"}],
        "timestamp": "1778900000",
    }
    inst.get_order_book.return_value = payload

    book = client.get_order_book("TKN-UP")

    assert book == payload
    inst.get_order_book.assert_called_once_with("TKN-UP")


def test_get_order_book_swallows_sdk_error(mock_clob):
    """Ack-time diagnostic must never break the trade flow on network failures."""
    client, inst = _make_client(mock_clob)
    inst.get_order_book.side_effect = RuntimeError("503 service unavailable")

    book = client.get_order_book("TKN-UP")

    assert book == {}


def test_get_order_book_dry_run_returns_empty(mock_clob):
    client, _ = _make_client(mock_clob, dry_run=True)
    assert client.get_order_book("TKN-UP") == {}


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


def test_post_only_rest_price_up_uses_down_bid():
    """Rest BUY UP at (1 - best_down_bid) - offset_ticks."""
    down_bids = [{"price": 0.45, "size": 100.0}]
    rest = post_only_rest_price(
        side="up", up_bids=[], down_bids=down_bids, offset_ticks=2,
    )
    # 1 - 0.45 - 0.02 = 0.53
    assert rest == pytest.approx(0.53)


def test_post_only_rest_price_down_uses_up_bid():
    """Symmetric: BUY DOWN's pair-merge clearing uses the UP-side best bid."""
    up_bids = [{"price": 0.60, "size": 100.0}]
    rest = post_only_rest_price(
        side="down", up_bids=up_bids, down_bids=[], offset_ticks=3,
    )
    # 1 - 0.60 - 0.03 = 0.37
    assert rest == pytest.approx(0.37)


def test_post_only_rest_price_no_opp_bid_returns_none():
    """No counterparty on opposite side → no pair-merge possible at any offset."""
    assert post_only_rest_price(side="up", up_bids=[], down_bids=[], offset_ticks=2) is None
    assert post_only_rest_price(side="up", up_bids=[], down_bids=None, offset_ticks=2) is None


def test_post_only_rest_price_floors_at_one_cent():
    """Extreme opp_bid pushing rest below 0.01 must clamp to 0.01."""
    # 1 - 0.99 - 0.05 = -0.04 → clamp to 0.01.
    down_bids = [{"price": 0.99, "size": 10.0}]
    rest = post_only_rest_price(
        side="up", up_bids=[], down_bids=down_bids, offset_ticks=5,
    )
    assert rest == 0.01


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


def test_get_settlement_info_handles_null_order_body(mock_clob):
    """Defensive None-guard: /order may return null body during indexing lag."""
    client, inst = _make_client(mock_clob)
    inst.get_order.return_value = None

    info = client.get_settlement_info("0xOID")

    assert info.shares_held == 0.0
    assert info.cost_usdc == 0.0
    inst.get_trades.assert_not_called()


def test_cancel_order_success(mock_clob):
    from py_clob_client_v2 import OrderPayload
    client, inst = _make_client(mock_clob)
    inst.cancel_order.return_value = {"canceled": ["abc"]}
    ok = client.cancel_order("abc")
    assert ok is True
    inst.cancel_order.assert_called_once()
    # v2 cancel_order takes OrderPayload(orderID=...) positionally
    payload = inst.cancel_order.call_args.args[0]
    assert isinstance(payload, OrderPayload)
    assert payload.orderID == "abc"


def test_cancel_order_dry_run(mock_clob):
    client, _ = _make_client(mock_clob, dry_run=True)
    assert client.cancel_order("DRY-RUN") is True
    assert client.cancel_order("anything") is True


def test_cancel_order_retries_then_succeeds(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.cancel_order.side_effect = [Exception("transient"), {"canceled": ["abc"]}]
    ok = client.cancel_order("abc")
    assert ok is True
    assert inst.cancel_order.call_count == 2


def test_cancel_order_gives_up_after_retries(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.cancel_order.side_effect = Exception("persistent")
    ok = client.cancel_order("abc")
    assert ok is False
    assert inst.cancel_order.call_count == 3  # 1 + 2 retries (CANCEL_RETRY_MAX=2)


def test_submit_ioc_full_match(mock_clob):
    """FAK fully fills: server-side cancel-remainder is a no-op; /trades has the fill."""
    client, inst = _make_client(mock_clob)
    inst.create_and_post_market_order.return_value = {
        "success": True, "status": "matched", "orderID": "abc",
    }
    inst.get_order.return_value = {
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
    # avg_price = cost / shares = (7.0 * 0.51) / 6.3 ≈ 0.5667
    assert fill.avg_price == pytest.approx(0.5667, abs=0.001)
    # v2 FAK cancels remainder server-side; we never call cancel_order from submit_ioc
    inst.cancel_order.assert_not_called()


def test_submit_ioc_partial_match(mock_clob):
    """FAK partial fill: only 3 of 7 shares matched; /trades reflects the partial."""
    client, inst = _make_client(mock_clob)
    inst.create_and_post_market_order.return_value = {
        "success": True, "status": "matched", "orderID": "abc",
    }
    inst.get_order.return_value = {
        "associate_trades": ["t1"],
    }
    inst.get_trades.return_value = [
        {"taker_order_id": "abc", "size": "3.0", "price": "0.51", "fee_rate_bps": 1000},
    ]

    fill = client.submit_ioc(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND",
                             limit_price=0.57)

    assert fill.status == "filled"
    assert fill.order_id == "abc"
    assert fill.filled_size == pytest.approx(2.7, abs=0.001)  # 3.0 * 0.9
    assert fill.avg_price == pytest.approx(0.5667, abs=0.001)
    inst.cancel_order.assert_not_called()


def test_submit_ioc_no_match_returns_rejected(mock_clob):
    """FAK with zero matches: server returns success but /trades is empty → fak-no-fill."""
    client, inst = _make_client(mock_clob)
    inst.create_and_post_market_order.return_value = {
        "success": True, "status": "matched", "orderID": "abc",
    }
    inst.get_order.return_value = {"associate_trades": []}

    fill = client.submit_ioc(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND",
                             limit_price=0.57)

    assert fill.status == "rejected"
    assert fill.error == "fak-no-fill"
    assert fill.filled_size == 0.0
    assert fill.order_id == "abc"  # persisted for the reconciler
    inst.cancel_order.assert_not_called()


def test_submit_ioc_success_false_is_rejected(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_and_post_market_order.return_value = {
        "success": False, "errorMsg": "fee mismatch",
    }

    fill = client.submit_ioc(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND",
                             limit_price=0.57)

    assert fill.status == "rejected"
    assert "fee mismatch" in fill.error
    inst.cancel_order.assert_not_called()


def test_submit_ioc_post_raises_returns_error(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_and_post_market_order.side_effect = Exception("network down")

    fill = client.submit_ioc(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND",
                             limit_price=0.57)

    assert fill.status == "error"
    assert "network" in fill.error
    inst.cancel_order.assert_not_called()


def test_submit_ioc_no_match_400_classified_as_rejected(mock_clob):
    """v2 server raises PolyApiException(status=400) when FAK finds no match.
    Real-world example from 2026-05-15 live session — must surface as
    `fak-no-fill` with orderID preserved (not as `status=error`).
    """
    from py_clob_client_v2.exceptions import PolyApiException
    client, inst = _make_client(mock_clob)
    exc = PolyApiException.__new__(PolyApiException)
    exc.status_code = 400
    exc.error_msg = {
        "error": ("no orders found to match with FAK order. FAK orders "
                  "are partially filled or killed if no match is found."),
        "orderID": "0xea9b4792d52161bc",
    }
    inst.create_and_post_market_order.side_effect = exc

    fill = client.submit_ioc(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND",
                             limit_price=0.57)

    assert fill.status == "rejected"
    assert fill.error == "fak-no-fill"
    assert fill.order_id == "0xea9b4792d52161bc"
    assert fill.filled_size == 0.0
    inst.cancel_order.assert_not_called()


def test_submit_ioc_no_match_with_empty_order_id(mock_clob):
    """Defensive: if the 400 body somehow omits orderID, treat order_id=None
    (not the empty string) so executor doesn't write a bogus link."""
    from py_clob_client_v2.exceptions import PolyApiException
    client, inst = _make_client(mock_clob)
    exc = PolyApiException.__new__(PolyApiException)
    exc.status_code = 400
    exc.error_msg = {
        "error": "no orders found to match with FAK order.",
    }
    inst.create_and_post_market_order.side_effect = exc

    fill = client.submit_ioc(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND",
                             limit_price=0.57)

    assert fill.status == "rejected"
    assert fill.error == "fak-no-fill"
    assert fill.order_id is None


def test_submit_ioc_settlement_lookup_failure_is_rejected(mock_clob):
    """v2: if get_settlement_info raises, treat as rejected with a clear error.

    v1's degraded-fallback estimate path is gone — without `/trades` data we
    can't compute real cost, and v2's native FAK means there's no GTC-then-cancel
    race window where we'd need to guess.
    """
    client, inst = _make_client(mock_clob)
    inst.create_and_post_market_order.return_value = {
        "success": True, "status": "matched", "orderID": "abc",
    }
    inst.get_order.side_effect = RuntimeError("CLOB 500")

    fill = client.submit_ioc(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND",
                             limit_price=0.57)

    assert fill.status == "rejected"
    assert fill.order_id == "abc"
    assert "settlement-lookup" in fill.error


def test_submit_ioc_dry_run(mock_clob):
    client, _ = _make_client(mock_clob, dry_run=True)
    fill = client.submit_ioc(side="up", price=0.51, size=7.0,
                             token_id="TKN", condition_id="COND",
                             limit_price=0.57)
    assert fill.status == "filled"
    assert fill.filled_size == 7.0


def test_submit_ioc_uses_explicit_limit_price(mock_clob):
    """submit_ioc must post at the caller-supplied limit_price, not fok_limit_price(price)."""
    from py_clob_client_v2 import OrderType, Side
    client, inst = _make_client(mock_clob)
    inst.create_and_post_market_order.return_value = {
        "success": True, "status": "matched", "orderID": "abc",
    }
    inst.get_order.return_value = {"associate_trades": []}

    # price=0.51, limit_price=0.42 (below same-side ask — pair-merge scenario)
    client.submit_ioc(side="up", price=0.51, size=7.0,
                      token_id="TKN-UP", condition_id="0xCOND",
                      limit_price=0.42)

    kwargs = inst.create_and_post_market_order.call_args.kwargs
    args = kwargs["order_args"]
    assert args.price == pytest.approx(0.42)
    assert args.side == Side.BUY
    assert args.order_type == OrderType.FAK
    # amount is anchored to limit_price since the server reconstructs taker
    # = amount/limit, and that ratio must land on the tick grid.
    # size_int = 7 is tick-safe for limit=0.42 (7*0.42=2.94, scaled 294.0 clean).
    assert args.amount == pytest.approx(round(7 * 0.42, 2))
    assert kwargs["order_type"] == OrderType.FAK


def test_tick_safe_size_picks_target_when_clean():
    """target size already tick-safe → returns target unchanged."""
    # 9 * 0.58 = 5.22 → scaled 522.0 (clean)
    assert _tick_safe_size(9, 0.58) == 9


def test_tick_safe_size_shifts_when_target_drifts():
    """target=7 at limit=0.58 drifts; search must land on a safe neighbor.
    Must use the same check path as production: round(size*limit, 2)*100."""
    import math
    result = _tick_safe_size(7, 0.58)
    amount = round(result * 0.58, 2)
    scaled = amount * 100
    assert math.floor(scaled) == round(scaled)


def test_tick_safe_size_handles_known_failing_trade_40():
    """Regression: trade 40 (2026-04-22) failed at size=7.49 limit=0.58."""
    # 7.49 rounds to 7; 7 drifts; search must yield a safe neighbor.
    result = _tick_safe_size(7, 0.58)
    assert result is not None
    assert result >= 1


def test_tick_safe_size_handles_known_failing_trade_45():
    """Regression: trade 45 at size=11.99 limit=0.38 slipped through an
    earlier implementation because `(12 * 0.38) * 100` = 456.00...06 looked
    clean but `round(12 * 0.38, 2) * 100` = 455.99...94 doesn't. The check
    must go through round()."""
    import math
    result = _tick_safe_size(12, 0.38)
    assert result is not None
    amount = round(result * 0.38, 2)
    scaled = amount * 100
    assert math.floor(scaled) == round(scaled), (
        f"size={result} amount={amount} scaled={scaled} still drifts"
    )


def test_submit_ioc_quantizes_to_tick_safe_size(mock_clob):
    """submit_ioc must submit an amount whose size*limit survives any round_down step."""
    import math
    client, inst = _make_client(mock_clob)
    inst.create_and_post_market_order.return_value = {
        "success": True, "status": "matched", "orderID": "abc",
    }
    inst.get_order.return_value = {"associate_trades": []}

    # Real failing case: size=7.49, limit=0.58 → raw ratio lands off-grid.
    client.submit_ioc(side="up", price=0.51, size=7.49,
                      token_id="TKN-UP", condition_id="0xCOND",
                      limit_price=0.58)

    args = inst.create_and_post_market_order.call_args.kwargs["order_args"]
    scaled = args.amount * 100
    # Core invariant: floor-scaled amount equals round-scaled (no drift).
    assert math.floor(scaled) == round(scaled), (
        f"submit_ioc picked drift-prone amount {args.amount} at limit {args.price}"
    )
    # And amount is exactly size_int * limit for some integer size_int >= 1.
    ratio = args.amount / args.price
    assert abs(ratio - round(ratio)) < 1e-6
    assert round(ratio) >= 1


def test_polymarket_client_satisfies_live_order_client_protocol():
    """Protocol drift guard. If a future change renames a method on
    PolymarketClient without updating the executor's Protocol or call sites,
    this test catches it at import time."""
    for method in (
        "submit_fok", "submit_ioc", "submit_post_only", "cancel_order",
        "get_usdc_balance", "get_settlement_info", "get_order_status",
    ):
        assert hasattr(PolymarketClient, method), f"PolymarketClient missing {method}"


def test_submit_post_only_placed(mock_clob):
    """Happy path: SDK returns success+orderID, wrapper returns PlaceResult(placed)."""
    client, inst = _make_client(mock_clob)
    inst.create_and_post_order.return_value = {
        "success": True, "orderID": "po-abc", "status": "live",
    }
    place = client.submit_post_only(
        side="up", size=10.0, price=0.54,
        token_id="TKN-UP", condition_id="0xCOND",
        expiration=int(1_700_000_000),
    )
    assert place.status == "placed"
    assert place.order_id == "po-abc"
    assert place.error is None
    inst.create_and_post_order.assert_called_once()


def test_submit_post_only_passes_v2_order_args(mock_clob):
    """Call-arg shape: OrderArgs with side=Side.BUY (enum, NOT str), expiration,
    plus order_type=GTC and post_only=True at the call site."""
    from py_clob_client_v2 import OrderType, Side
    client, inst = _make_client(mock_clob)
    inst.create_and_post_order.return_value = {
        "success": True, "orderID": "po-1", "status": "live",
    }
    client.submit_post_only(
        side="up", size=10.0, price=0.54,
        token_id="TKN-UP", condition_id="0xCOND",
        expiration=1_700_000_000,
    )
    kwargs = inst.create_and_post_order.call_args.kwargs
    args = kwargs["order_args"]
    assert args.token_id == "TKN-UP"
    assert args.price == pytest.approx(0.54)
    assert args.size == pytest.approx(10.0)
    # Guards against str(Side.BUY) regression: must be the enum value, not "Side.BUY".
    assert args.side == Side.BUY
    assert args.side != "Side.BUY"
    assert args.expiration == 1_700_000_000
    assert kwargs["options"].tick_size == "0.01"
    assert kwargs["order_type"] == OrderType.GTC
    assert kwargs["post_only"] is True


def test_submit_post_only_would_cross_via_400(mock_clob):
    """v2 server raises PolyApiException(400) when post-only would cross."""
    from py_clob_client_v2.exceptions import PolyApiException
    client, inst = _make_client(mock_clob)
    exc = PolyApiException.__new__(PolyApiException)
    exc.status_code = 400
    exc.error_msg = {
        "error": "post_only order would cross the book",
        "orderID": "po-xc",
    }
    inst.create_and_post_order.side_effect = exc

    place = client.submit_post_only(
        side="up", size=10.0, price=0.54,
        token_id="TKN-UP", condition_id="0xCOND", expiration=1_700_000_000,
    )
    assert place.status == "rejected"
    assert place.error == "post-only-would-cross"
    assert place.order_id == "po-xc"


def test_submit_post_only_would_cross_via_success_false(mock_clob):
    """Defensive: server may also surface cross-rejection as success=False
    with errorMsg text instead of a 400 — wrapper detects either shape."""
    client, inst = _make_client(mock_clob)
    inst.create_and_post_order.return_value = {
        "success": False, "orderID": "po-xc2",
        "errorMsg": "post_only_would_cross",
    }
    place = client.submit_post_only(
        side="up", size=10.0, price=0.54,
        token_id="TKN-UP", condition_id="0xCOND", expiration=1_700_000_000,
    )
    assert place.status == "rejected"
    assert place.error == "post-only-would-cross"
    assert place.order_id == "po-xc2"


def test_submit_post_only_network_error(mock_clob):
    """Non-400, non-classifiable exception → status=error with 'network:' prefix."""
    client, inst = _make_client(mock_clob)
    inst.create_and_post_order.side_effect = RuntimeError("conn reset")
    place = client.submit_post_only(
        side="up", size=10.0, price=0.54,
        token_id="TKN-UP", condition_id="0xCOND", expiration=1_700_000_000,
    )
    assert place.status == "error"
    assert place.error.startswith("network:")


def test_submit_post_only_tick_safe_unfixable(mock_clob):
    """Patched _tick_safe_size returning None → rejected with tick-size-unfixable."""
    client, inst = _make_client(mock_clob)
    with patch("polypocket.clients.polymarket._tick_safe_size", return_value=None):
        place = client.submit_post_only(
            side="up", size=10.0, price=0.54,
            token_id="TKN-UP", condition_id="0xCOND", expiration=1_700_000_000,
        )
    assert place.status == "rejected"
    assert place.error == "tick-size-unfixable"
    inst.create_and_post_order.assert_not_called()


def test_submit_post_only_dry_run(mock_clob):
    """Dry-run returns synthetic placed result without calling the SDK."""
    client, inst = _make_client(mock_clob, dry_run=True)
    place = client.submit_post_only(
        side="up", size=10.0, price=0.54,
        token_id="TKN-UP", condition_id="0xCOND", expiration=1_700_000_000,
    )
    assert place.status == "placed"
    assert place.order_id == "DRY-RUN"
    inst.create_and_post_order.assert_not_called()
