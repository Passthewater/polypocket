"""Polymarket CLOB client — L2 proxy-wallet signing."""

import logging

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    MarketOrderArgs,
    OrderType,
)

from polypocket.config import FOK_SLIPPAGE_TICKS
from polypocket.executor import FillResult

log = logging.getLogger(__name__)

# POLY_PROXY=1 is the proxy-wallet signing path used by Polymarket's
# email/OAuth signup flow (the wallet on file is a POLY_PROXY contract that
# owns the USDC — balances/allowances are keyed on the proxy, orders are
# signed by the EOA but executed as the proxy). Verified empirically against
# this account: sig_type=2 (POLY_GNOSIS_SAFE) returns $0; sig_type=1 returns
# the real balance.
POLY_PROXY_SIG_TYPE = 1


def fok_limit_price(price: float) -> float:
    """FOK limit price: best ask + FOK_SLIPPAGE_TICKS, capped at $0.99."""
    return round(min(0.99, price + FOK_SLIPPAGE_TICKS * 0.01), 2)


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
        self._client = ClobClient(
            host=host,
            key=private_key,
            chain_id=chain_id,
            creds=creds,
            signature_type=POLY_PROXY_SIG_TYPE,
            funder=proxy_address,
        )
        # Per-market taker fee cache. Polymarket rejects orders with
        # `feeRateBps=0` when the market's `taker_base_fee` is non-zero
        # (BTC up/down markets report 1000). We look it up once per
        # condition_id and reuse.
        self._fee_rate_bps_cache: dict[str, int] = {}

    def _fee_rate_bps(self, condition_id: str) -> int:
        cached = self._fee_rate_bps_cache.get(condition_id)
        if cached is not None:
            return cached
        try:
            market = self._client.get_market(condition_id)
            fee = int(market.get("taker_base_fee", 0) or 0)
        except Exception as exc:
            log.warning("get_market(%s) failed, defaulting fee_rate_bps=0: %s",
                        condition_id, exc)
            fee = 0
        self._fee_rate_bps_cache[condition_id] = fee
        return fee

    def submit_fok(self, side, price, size, token_id, condition_id):
        if self._dry_run:
            log.info(
                "DRY-RUN submit_fok side=%s price=%.4f size=%.2f token=%s cond=%s",
                side, price, size, token_id, condition_id,
            )
            return FillResult(
                status="filled", order_id="DRY-RUN",
                filled_size=size, avg_price=price, error=None,
            )

        fee_rate_bps = self._fee_rate_bps(condition_id)
        # FOK must go through create_market_order: Polymarket requires
        # makerAmount accuracy of 2 decimals and takerAmount 4 decimals
        # for taker-style orders. The limit-order path (create_order +
        # OrderArgs.size) produces 4-decimal makerAmount (e.g. 27.78 *
        # 0.36 = 10.0008) and is rejected with `invalid amounts`.
        # Market-order path computes makerAmount = round_down(amount, 2)
        # directly, which the server accepts.
        limit_price = fok_limit_price(price)
        args = MarketOrderArgs(
            token_id=token_id,
            amount=round(size * price, 2),  # USDC budget at target price
            price=limit_price,              # allow sweeping up to +N ticks
            fee_rate_bps=fee_rate_bps,
        )
        try:
            signed = self._client.create_market_order(args)
            resp = self._client.post_order(signed, OrderType.FOK)
        except Exception as exc:
            log.exception("submit_fok network/signing error")
            return FillResult(
                status="error", order_id=None, filled_size=0.0,
                avg_price=None, error=f"network: {exc}",
            )

        # FOK semantics: only treat as filled when the server explicitly reports
        # success AND status=="matched". Anything else (success with status
        # "unmatched"/"delayed", missing status, success=False) is a reject.
        if not (resp.get("success") and resp.get("status") == "matched"):
            err = resp.get("errorMsg") or f"status={resp.get('status')!r}"
            return FillResult(
                status="rejected", order_id=None, filled_size=0.0,
                avg_price=None, error=err,
            )

        order_id = resp.get("orderID")
        try:
            status = self._client.get_order(order_id)
            filled = float(status.get("size_matched", size))
        except Exception as exc:
            log.warning("get_order failed after successful post: %s", exc)
            filled = size  # POST reported matched; trust it.

        return FillResult(
            status="filled", order_id=order_id, filled_size=filled,
            avg_price=price, error=None,
        )

    def get_usdc_balance(self) -> float:
        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=POLY_PROXY_SIG_TYPE,
        )
        resp = self._client.get_balance_allowance(params)
        # Polymarket returns USDC balance as a string of raw on-chain units
        # (6 decimals, matching `to_token_decimals` in py_clob_client). Convert
        # to dollars so caller-side `< MIN_POSITION_USDC` and `< size * price`
        # gates compare in the right units.
        return float(resp.get("balance", 0.0)) / 1_000_000

    def get_order_status(self, order_id: str) -> dict:
        return self._client.get_order(order_id)
