"""Polymarket CLOB client — L2 proxy-wallet signing."""

import logging

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    OrderArgs,
    OrderType,
)
from py_clob_client.order_builder.constants import BUY

from polypocket.executor import FillResult

log = logging.getLogger(__name__)


class PolymarketClient:
    """Concrete LiveOrderClient for Polymarket's CLOB using L2 proxy signing."""

    def __init__(
        self,
        host: str,
        chain_id: int,
        private_key: str,
        api_creds: dict,
        proxy_address: str,
        dry_run: bool = False,
    ):
        self._dry_run = dry_run
        creds = ApiCreds(
            api_key=api_creds["key"],
            api_secret=api_creds["secret"],
            api_passphrase=api_creds["passphrase"],
        )
        # signature_type=2 = POLY_GNOSIS_SAFE (proxy wallet path for email/OAuth signup).
        self._client = ClobClient(
            host=host,
            key=private_key,
            chain_id=chain_id,
            creds=creds,
            signature_type=2,
            funder=proxy_address,
        )

    def submit_fok(self, side, price, size, token_id, client_order_id):
        if self._dry_run:
            log.info(
                "DRY-RUN submit_fok side=%s price=%.4f size=%.2f token=%s cid=%s",
                side, price, size, token_id, client_order_id,
            )
            return FillResult(
                status="filled", order_id="DRY-RUN",
                filled_size=size, avg_price=price, error=None,
            )

        args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=BUY,
        )
        try:
            signed = self._client.create_order(args)
            resp = self._client.post_order(signed, OrderType.FOK)
        except Exception as exc:
            log.exception("submit_fok network/signing error")
            return FillResult(
                status="error", order_id=None, filled_size=0.0,
                avg_price=None, error=f"network: {exc}",
            )

        if not resp.get("success"):
            return FillResult(
                status="rejected", order_id=None, filled_size=0.0,
                avg_price=None, error=resp.get("errorMsg", "rejected"),
            )

        order_id = resp.get("orderID")
        try:
            status = self._client.get_order(order_id)
            filled = float(status.get("size_matched", size))
        except Exception as exc:
            log.warning("get_order failed after successful post: %s", exc)
            filled = size  # POST reported success; trust it.

        return FillResult(
            status="filled", order_id=order_id, filled_size=filled,
            avg_price=price, error=None,
        )

    def get_usdc_balance(self) -> float:
        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=2,
        )
        resp = self._client.get_balance_allowance(params)
        return float(resp.get("balance", 0.0))

    def get_order_status(self, order_id: str) -> dict:
        return self._client.get_order(order_id)
