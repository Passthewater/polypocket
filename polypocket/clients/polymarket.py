"""Polymarket CLOB client — L2 proxy-wallet signing."""

import logging
import math
import time

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    MarketOrderArgs,
    OrderType,
    TradeParams,
)

from polypocket.config import FOK_SLIPPAGE_TICKS
from polypocket.executor import FillResult, SettlementInfo

log = logging.getLogger(__name__)

# POLY_PROXY=1 is the proxy-wallet signing path used by Polymarket's
# email/OAuth signup flow (the wallet on file is a POLY_PROXY contract that
# owns the USDC — balances/allowances are keyed on the proxy, orders are
# signed by the EOA but executed as the proxy). Verified empirically against
# this account: sig_type=2 (POLY_GNOSIS_SAFE) returns $0; sig_type=1 returns
# the real balance.
POLY_PROXY_SIG_TYPE = 1

CANCEL_RETRY_MAX = 2
CANCEL_RETRY_BACKOFF_S = 0.25


def fok_limit_price(price: float) -> float:
    """FOK limit price: best ask + FOK_SLIPPAGE_TICKS, capped at $0.99."""
    return round(min(0.99, price + FOK_SLIPPAGE_TICKS * 0.01), 2)


def _tick_safe_size(target_size: int, limit_price: float, search: int = 6) -> int | None:
    """Pick an integer size where amount = round(size*limit, 2) survives
    py_clob_client's float-based round_down. Some (size, limit) combos produce
    amount values like 4.56 whose float rep is 4.5599999...; py_clob_client's
    round_down then floors into the previous cent, corrupting the reconstructed
    taker and tripping the server's 0.01 tick check. Searches ±search around
    target and returns the first safe size, or None.

    Critical: the check must go through `round(..., 2)` first, since
    `(s * limit) * 100` and `round(s * limit, 2) * 100` often have different
    float representations (e.g., 12 * 0.38 = 4.5600000000000005 positive-drift,
    but round(that, 2) * 100 = 455.9999...94 negative-drift).
    """
    candidates = [target_size]
    for d in range(1, search + 1):
        candidates.extend([target_size + d, target_size - d])
    for s in candidates:
        if s < 1:
            continue
        amount = round(s * limit_price, 2)
        scaled = amount * 100
        if math.floor(scaled) == round(scaled):
            return s
    return None


def ioc_limit_price(
    side: str,
    up_bids: list[dict] | None,
    down_bids: list[dict] | None,
    buffer_ticks: int,
) -> float | None:
    """Pair-merge-aware taker limit for binary (UP/DOWN) markets.

    A BUY UP crosses via pair-merge against a DOWN-side BUY: the two orders
    sum-to-1 (plus fees), so the effective clearing price for the UP taker
    is `1 - best_down_bid`. We add `buffer_ticks` of slippage headroom
    against DOWN-book churn during the signing window, then cap at $0.99.

    Returns None when the opposite book has no bid — no counterparty exists
    for a pair-merge match; caller should skip with
    'no-pair-merge-counterparty'.
    """
    opp_bids = down_bids if side == "up" else up_bids
    if not opp_bids:
        return None
    best_opp = max(float(b["price"]) for b in opp_bids)
    return round(min(0.99, (1.0 - best_opp) + buffer_ticks * 0.01), 2)


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

    def submit_ioc(self, side, price, size, token_id, condition_id, limit_price):
        """Post GTC at caller-supplied limit price, immediately cancel remainder.

        True-IOC semantic layered on GTC since py_clob_client doesn't expose
        IOC natively. Any match at <= limit_price fills; remainder is
        cancelled. limit_price is computed upstream (pair-merge-aware via
        `ioc_limit_price`) since only the bot sees both books. Returned
        filled_size is shares_held from per-fill /trades data (post-fee).
        """
        if self._dry_run:
            log.info(
                "DRY-RUN submit_ioc side=%s price=%.4f size=%.2f limit=%.4f token=%s cond=%s",
                side, price, size, limit_price, token_id, condition_id,
            )
            return FillResult(
                status="filled", order_id="DRY-RUN",
                filled_size=size, avg_price=price, error=None,
            )

        fee_rate_bps = self._fee_rate_bps(condition_id)
        # Tick-safe amount: quantize size to integer and pick a neighbor whose
        # amount = size * limit_price survives py_clob_client's float-based
        # round_down. Otherwise the server tick-checks the reconstructed
        # taker and rejects with `breaks minimum tick size rule: 0.01`.
        target_size_int = max(1, int(round(size)))
        size_int = _tick_safe_size(target_size_int, limit_price)
        if size_int is None:
            log.error(
                "submit_ioc: no tick-safe size near %d for limit=%.4f",
                target_size_int, limit_price,
            )
            return FillResult(
                status="rejected", order_id=None, filled_size=0.0,
                avg_price=None, error="tick-size-unfixable",
            )
        amount = round(size_int * limit_price, 2)
        args = MarketOrderArgs(
            token_id=token_id,
            amount=amount,
            price=limit_price,
            fee_rate_bps=fee_rate_bps,
        )

        try:
            signed = self._client.create_market_order(args)
            resp = self._client.post_order(signed, OrderType.GTC)
        except Exception as exc:
            log.exception("submit_ioc network/signing error")
            return FillResult(
                status="error", order_id=None, filled_size=0.0,
                avg_price=None, error=f"network: {exc}",
            )

        if not resp.get("success"):
            err = resp.get("errorMsg") or f"status={resp.get('status')!r}"
            return FillResult(
                status="rejected", order_id=None, filled_size=0.0,
                avg_price=None, error=err,
            )

        order_id = resp.get("orderID")
        if not order_id:
            return FillResult(
                status="rejected", order_id=None, filled_size=0.0,
                avg_price=None, error="no-order-id",
            )

        # Check how much matched before deciding whether to cancel remainder.
        # Skip cancel if order fully matched (server errors on cancel-of-filled).
        size_matched = 0.0
        try:
            order_status = self._client.get_order(order_id)
            if order_status:
                size_matched = float(order_status.get("size_matched", 0) or 0)
        except Exception as exc:
            log.warning("submit_ioc: get_order check failed for %s: %s", order_id, exc)

        # Compare against the integer size we actually submitted, not the
        # caller's pre-quantization float size.
        fully_matched = size_matched >= size_int - 0.01
        if not fully_matched:
            self.cancel_order(order_id)

        # Fast path: nothing matched, so skip the /trades settlement lookup
        # entirely. Avoids hitting the /order null-body race when the server
        # hasn't indexed the cancel yet — which previously propagated as a
        # spurious `settlement-lookup: NoneType` error and lost the
        # external_order_id linkage for the reconciler.
        if size_matched <= 0:
            return FillResult(
                status="rejected", order_id=order_id, filled_size=0.0,
                avg_price=None, error="gtc-no-fill",
            )

        # Derive real fill from per-fill /trades data (post-fee shares).
        try:
            info = self.get_settlement_info(order_id)
        except Exception as exc:
            log.warning("submit_ioc: get_settlement_info failed for %s: %s", order_id, exc)
            info = SettlementInfo(shares_held=0.0, cost_usdc=0.0)

        # Degraded path: first get_order saw a real match but /trades is
        # empty (null body, indexing lag, or partial /trades propagation).
        # Fall back to a pessimistic estimate from the known size_matched +
        # market fee — better than marking a real fill as rejected, which
        # would strand the position.
        if info.shares_held <= 0:
            fee_bps = self._fee_rate_bps(condition_id)
            est_shares = size_matched * (1.0 - fee_bps / 10_000.0)
            log.error(
                "submit_ioc: settlement data unavailable for %s with "
                "size_matched=%.4f; using pessimistic estimate "
                "shares=%.4f cost≈$%.4f (fill may have landed better)",
                order_id, size_matched, est_shares, size_matched * limit_price,
            )
            return FillResult(
                status="filled", order_id=order_id,
                filled_size=est_shares, avg_price=limit_price, error=None,
            )

        avg_price = info.cost_usdc / info.shares_held if info.shares_held > 0 else price
        return FillResult(
            status="filled", order_id=order_id,
            filled_size=info.shares_held, avg_price=avg_price, error=None,
        )

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a resting order. Retries on transient errors.

        Returns True on success, False if all retries fail. Errors are logged
        but not raised — the caller records whatever matched via /trades and
        the startup reconciler catches orphans.
        """
        if self._dry_run:
            return True

        last_exc: Exception | None = None
        for attempt in range(CANCEL_RETRY_MAX + 1):
            try:
                self._client.cancel(order_id=order_id)
                return True
            except Exception as exc:
                last_exc = exc
                if attempt < CANCEL_RETRY_MAX:
                    time.sleep(CANCEL_RETRY_BACKOFF_S * (attempt + 1))
        log.error("cancel_order failed after %d attempts for order %s: %s",
                  CANCEL_RETRY_MAX + 1, order_id, last_exc)
        return False

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
        if self._dry_run or order_id == "DRY-RUN":
            return {}
        return self._client.get_order(order_id)

    def get_settlement_info(self, order_id: str) -> SettlementInfo:
        """Look up the CLOB record of a filled order and return real fill accounting.

        Reads per-fill data from the /trades endpoint rather than /order, because
        Polymarket's pair-matching means a BUY Up can fill against a BUY Down
        maker — the taker's true per-share price is (1 - maker_price), which
        does NOT appear as a field on the /order response. /order.price on a
        filled market BUY reflects the order's limit rounding, not the fill
        rate, so `size_matched × order.price` overstates cost when matched
        via the pair-merge path (observed on live trade: order.price=0.48 but
        the real taker fill was 0.41).

        shares_held = sum(trade.size × (1 - trade.fee_rate_bps/10000))
        cost_usdc   = sum(trade.size × trade.price)
        """
        if self._dry_run or order_id == "DRY-RUN":
            return SettlementInfo(shares_held=0.0, cost_usdc=0.0)

        order = self._client.get_order(order_id)
        # Polymarket's /order endpoint occasionally returns a null body for an
        # order ID that's still propagating (observed within ~500 ms of a post
        # or between post+cancel). Treat as "no data yet" instead of crashing
        # — the caller decides whether to trust the prior size_matched or not.
        if not order:
            return SettlementInfo(shares_held=0.0, cost_usdc=0.0)
        trade_ids = order.get("associate_trades") or []

        shares_held = 0.0
        cost_usdc = 0.0
        for tid in trade_ids:
            fills = self._client.get_trades(TradeParams(id=tid))
            for fill in fills:
                if fill.get("taker_order_id") != order_id:
                    continue
                size = float(fill.get("size", 0.0) or 0.0)
                price = float(fill.get("price", 0.0) or 0.0)
                fee_bps = float(fill.get("fee_rate_bps", 0) or 0)
                shares_held += size * (1.0 - fee_bps / 10_000.0)
                cost_usdc += size * price
        return SettlementInfo(shares_held=shares_held, cost_usdc=cost_usdc)
