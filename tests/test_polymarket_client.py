from unittest.mock import MagicMock, patch

import pytest

from polypocket.clients.polymarket import PolymarketClient
from polypocket.executor import FillResult


@pytest.fixture
def mock_clob():
    with patch("polypocket.clients.polymarket.ClobClient") as cls:
        yield cls


def _make_client(mock_clob_cls, dry_run=False):
    instance = mock_clob_cls.return_value
    instance.get_balance_allowance.return_value = {"balance": "1234.5"}
    return PolymarketClient(
        host="https://clob.polymarket.com", chain_id=137,
        private_key="0x" + "1" * 64,
        api_creds={"key": "k", "secret": "s", "passphrase": "p"},
        proxy_address="0x" + "2" * 40,
        dry_run=dry_run,
    ), instance


def test_submit_fok_filled(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_order.return_value = MagicMock()
    inst.post_order.return_value = {"success": True, "orderID": "abc"}
    inst.get_order.return_value = {"status": "matched", "size_matched": "7.0"}

    fill = client.submit_fok(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", client_order_id="window-x")

    assert fill.status == "filled"
    assert fill.order_id == "abc"
    assert fill.filled_size == pytest.approx(7.0)
    inst.post_order.assert_called_once()


def test_submit_fok_rejected(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_order.return_value = MagicMock()
    inst.post_order.return_value = {"success": False, "errorMsg": "not matched"}

    fill = client.submit_fok(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", client_order_id="window-x")

    assert fill.status == "rejected"
    assert fill.error == "not matched"
    assert fill.order_id is None
    inst.get_order.assert_not_called()


def test_submit_fok_network_error(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_order.side_effect = RuntimeError("boom")

    fill = client.submit_fok(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", client_order_id="window-x")

    assert fill.status == "error"
    assert "boom" in fill.error


def test_submit_fok_dry_run_does_not_post(mock_clob):
    client, inst = _make_client(mock_clob, dry_run=True)

    fill = client.submit_fok(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", client_order_id="window-x")

    assert fill.status == "filled"
    assert fill.order_id == "DRY-RUN"
    inst.create_order.assert_not_called()
    inst.post_order.assert_not_called()


def test_get_usdc_balance_queries_proxy(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.get_balance_allowance.return_value = {"balance": "42.7"}

    bal = client.get_usdc_balance()

    assert bal == pytest.approx(42.7)
    call = inst.get_balance_allowance.call_args
    assert call is not None
