"""Polymarket CLOB v2 client — L2 proxy-wallet signing."""

import logging
import math
import time

from py_clob_client_v2 import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    ClobClient,
    MarketOrderArgs,
    OrderArgs,
    OrderPayload,
    OrderType,
    PartialCreateOrderOptions,
    Side,
    SignatureTypeV2,
    TradeParams,
)
from py_clob_client_v2.exceptions import PolyApiException

from polypocket.config import FOK_SLIPPAGE_TICKS
from polypocket.executor import FillResult, PlaceResult, SettlementInfo

log = logging.getLogger(__name__)

# Signature type 1 is the proxy-wallet signing path used by Polymarket's
# email/OAuth signup flow (the wallet on file is a POLY_PROXY contract that
# owns the pUSD — balances/allowances are keyed on the proxy, orders are
# signed by the EOA but executed as the proxy). Verified empirically against
# this account: sig_type=2 (POLY_GNOSIS_SAFE) returns $0; sig_type=1 returns
# the real balance. v2 preserves this signature scheme; the only on-server
# change was the EIP-712 Exchange domain version bumping "1" → "2".
POLY_PROXY_SIG_TYPE = SignatureTypeV2.POLY_PROXY  # IntEnum value = 1

# All BTC up/down markets use 1¢ ticks. Hardcoded rather than queried per
# order to avoid a roundtrip; if a future market type ever needs a different
# tick size, plumb it through from the bot.
TICK_SIZE = "0.01"

CANCEL_RETRY_MAX = 2
CANCEL_RETRY_BACKOFF_S = 0.25


def _classify_no_match_error(exc: Exception) -> tuple[str, str] | None:
    """If `exc` is a v2-server no-match rejection, return (order_id, label).

    Polymarket's v2 CLOB raises HTTP 400 (as a `PolyApiException`) for the
    common case where a FOK/FAK order signs and reaches the matching engine
    but finds no counterparty at the limit price — instead of returning a
    normal `success: false` response body. The 400 body still carries a
    real `orderID` we want to preserve for the reconciler.

    Returns `(order_id, "fak-no-fill"|"fok-no-fill")` if the exception
    matches that pattern, else None (caller falls back to the generic
    `network:` error path).
    """
    if not isinstance(exc, PolyApiException) or exc.status_code != 400:
        return None
    body = exc.error_msg
    if not isinstance(body, dict):
        return None
    err = (body.get("error") or "").lower()
    if "no orders found to match" not in err:
        return None
    order_id = body.get("orderID") or ""
    label = "fak-no-fill" if "fak" in err else "fok-no-fill"
    return order_id, label


def _classify_post_only_cross_error(exc: Exception) -> tuple[str, str] | None:
    """If `exc` is a v2-server post-only-would-cross rejection, return
    (order_id, label). Returns None otherwise.

    Polymarket's v2 server raises HTTP 400 (PolyApiException) when a
    post-only order would have crossed the book at placement — the only
    pattern of post-only-rejection we expect at runtime. Token-matches on
    "post" + ("only" or "cross") in the error message — the exact server
    string is confirmed empirically in the Step-9 dry-run probe of the
    2026-05-15 post-only-entries implementation plan.
    """
    if not isinstance(exc, PolyApiException) or exc.status_code != 400:
        return None
    body = exc.error_msg
    if not isinstance(body, dict):
        return None
    err = (body.get("error") or "").lower()
    if "post" not in err or ("only" not in err and "cross" not in err):
        return None
    order_id = body.get("orderID") or ""
    return order_id, "post-only-would-cross"


def fok_limit_price(price: float) -> float:
    """FOK limit price: best ask + FOK_SLIPPAGE_TICKS, capped at $0.99."""
    return round(min(0.99, price + FOK_SLIPPAGE_TICKS * 0.01), 2)


def _tick_safe_size(target_size: int, limit_price: float, search: int = 6) -> int | None:
    """Pick an integer size where amount = round(size*limit, 2) survives
    float-based round_down at the order builder. Some (size, limit) combos
    produce amount values like 4.56 whose float rep is 4.5599999...; if the
    builder floors that into the previous cent, the reconstructed taker
    trips the server's 0.01 tick check. Searches ±search around target and
    returns the first safe size, or None.

    Retained from the v1 SDK era as defense-in-depth. v2's order builder
    is rewritten and likely doesn't need this; revisit removal once we
    have a few clean v2 fills to A/B against.

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


def post_only_rest_price(
    side: str,
    up_bids: list[dict] | None,
    down_bids: list[dict] | None,
    offset_ticks: int,
) -> float | None:
    """Maker rest price for a BUY UP/DOWN on a binary book.

    Sits `offset_ticks` below the pair-merge clearing `pmc = 1 - best_opp_bid`.
    Returns None when the opposite book has no bid (no pair-merge counterparty
    exists; caller should skip with 'no-pair-merge-counterparty'). Floored at
    $0.01 — a rest at $0.00 is meaningless. Capped at $0.99 to mirror the FAK
    limit convention.

    Conservatively: a non-positive offset_ticks would request a rest at-or-
    above the cross, which post-only would reject server-side. Caller's
    responsibility to keep offset_ticks >= 1; this function trusts the input.
    """
    opp_bids = down_bids if side == "up" else up_bids
    if not opp_bids:
        return None
    best_opp = max(float(b["price"]) for b in opp_bids)
    rest = (1.0 - best_opp) - offset_ticks * 0.01
    return round(max(0.01, min(0.99, rest)), 2)


class PolymarketClient:
    """Concrete LiveOrderClient for Polymarket's CLOB v2 using L2 proxy signing."""

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
            chain_id=chain_id,
            key=private_key,
            creds=creds,
            signature_type=POLY_PROXY_SIG_TYPE,
            funder=proxy_address,
        )

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

        limit_price = fok_limit_price(price)
        args = MarketOrderArgs(
            token_id=token_id,
            amount=round(size * price, 2),  # USDC budget at target price
            side=Side.BUY,
            price=limit_price,
            order_type=OrderType.FOK,
        )
        try:
            resp = self._client.create_and_post_market_order(
                order_args=args,
                options=PartialCreateOrderOptions(tick_size=TICK_SIZE),
                order_type=OrderType.FOK,
            )
        except Exception as exc:
            no_match = _classify_no_match_error(exc)
            if no_match is not None:
                order_id, label = no_match
                return FillResult(
                    status="rejected", order_id=order_id or None,
                    filled_size=0.0, avg_price=None, error=label,
                )
            log.exception("submit_fok network/signing error")
            return FillResult(
                status="error", order_id=None, filled_size=0.0,
                avg_price=None, error=f"network: {exc}",
            )

        # v2 response shape (verified against
        # https://docs.polymarket.com/api-reference/trade/post-a-new-order.md):
        # success, orderID, status (live|matched|delayed), makingAmount,
        # takingAmount, transactionsHashes, tradeIDs, errorMsg.
        # FOK: only treat as filled when explicit success + matched.
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
        """Post FAK at caller-supplied limit price; partial fills accepted,
        remainder server-cancelled.

        v2's native FAK ("Fill And Kill") replaces the v1-era GTC+manual-cancel
        workaround. limit_price is computed upstream (pair-merge-aware via
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

        # Tick-safe quantization — retained from v1 as defense-in-depth.
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
            side=Side.BUY,
            price=limit_price,
            order_type=OrderType.FAK,
        )

        try:
            resp = self._client.create_and_post_market_order(
                order_args=args,
                options=PartialCreateOrderOptions(tick_size=TICK_SIZE),
                order_type=OrderType.FAK,
            )
        except Exception as exc:
            no_match = _classify_no_match_error(exc)
            if no_match is not None:
                order_id, label = no_match
                return FillResult(
                    status="rejected", order_id=order_id or None,
                    filled_size=0.0, avg_price=None, error=label,
                )
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

        # FAK: read the real fill cost from /trades. v2 handles the
        # cancel-remainder server-side, so there's no GTC-then-cancel
        # indexing race window for us to estimate around.
        try:
            info = self.get_settlement_info(order_id)
        except Exception as exc:
            log.warning("submit_ioc: get_settlement_info failed for %s: %s", order_id, exc)
            return FillResult(
                status="rejected", order_id=order_id, filled_size=0.0,
                avg_price=None, error=f"settlement-lookup: {exc}",
            )

        if info.shares_held <= 0:
            return FillResult(
                status="rejected", order_id=order_id, filled_size=0.0,
                avg_price=None, error="fak-no-fill",
            )

        avg_price = info.cost_usdc / info.shares_held
        return FillResult(
            status="filled", order_id=order_id,
            filled_size=info.shares_held, avg_price=avg_price, error=None,
        )

    def submit_post_only(
        self, side, size, price, token_id, condition_id, expiration,
    ):
        """Post a GTC limit order with post_only=True.

        `size` is in shares (NOT USDC) — this differs from submit_ioc and
        submit_fok whose `amount` field is the USDC budget. `expiration` is
        a Unix-seconds timestamp; the server kills the resting order at
        that time if it hasn't filled.

        Returns PlaceResult:
        - status="placed" on accepted (resting in book).
        - status="rejected" on server-side rejection (e.g. would-cross).
        - status="error" on network/signing failure.
        """
        if self._dry_run:
            log.info(
                "DRY-RUN submit_post_only side=%s size=%.2f price=%.4f exp=%d "
                "token=%s cond=%s",
                side, size, price, expiration, token_id, condition_id,
            )
            return PlaceResult(
                status="placed", order_id="DRY-RUN", error=None,
                placed_size=float(size),
            )

        # Tick-safe quantization — same defense-in-depth as submit_ioc.
        target_size_int = max(1, int(round(size)))
        size_int = _tick_safe_size(target_size_int, price)
        if size_int is None:
            log.error(
                "submit_post_only: no tick-safe size near %d for price=%.4f",
                target_size_int, price,
            )
            return PlaceResult(
                status="rejected", order_id=None, error="tick-size-unfixable",
            )

        # OrderArgsV2.side is annotated `str` but the SDK accepts the
        # IntEnum directly. str(Side.BUY) would produce "Side.BUY" — wrong.
        # Mirrors the FAK path's MarketOrderArgs(side=Side.BUY).
        args = OrderArgs(
            token_id=token_id,
            price=price,
            size=float(size_int),
            side=Side.BUY,
            expiration=int(expiration),
        )
        # Empirical (Step-9 live probe 2026-05-16): the server rejects GTC
        # with a non-zero expiration ("invalid expiration value (...), it
        # should be equal to '0' as the order is not a GTD order"). Use GTD
        # when we want a server-side expiration safety net, GTC only when
        # expiration is 0 (no safety net — bot must drive cancel).
        order_type = OrderType.GTD if int(expiration) > 0 else OrderType.GTC
        try:
            resp = self._client.create_and_post_order(
                order_args=args,
                options=PartialCreateOrderOptions(tick_size=TICK_SIZE),
                order_type=order_type,
                post_only=True,
            )
        except Exception as exc:
            cross = _classify_post_only_cross_error(exc)
            if cross is not None:
                order_id, label = cross
                return PlaceResult(
                    status="rejected", order_id=order_id or None, error=label,
                )
            log.exception("submit_post_only network/signing error")
            return PlaceResult(
                status="error", order_id=None, error=f"network: {exc}",
            )

        # v2 success response (live resting order): {"success": True,
        #   "orderID": "...", "status": "live", "makingAmount": ...,
        #   "takingAmount": ..., "transactionsHashes": [], "tradeIDs": []}.
        if not resp.get("success"):
            err = resp.get("errorMsg") or f"status={resp.get('status')!r}"
            # Server-level post-only-cross can also arrive as success=False
            # with errorMsg text (not always as a 400 PolyApiException).
            lower = err.lower() if isinstance(err, str) else ""
            if "post" in lower and ("only" in lower or "cross" in lower):
                return PlaceResult(
                    status="rejected", order_id=resp.get("orderID") or None,
                    error="post-only-would-cross",
                )
            return PlaceResult(
                status="rejected", order_id=resp.get("orderID") or None, error=err,
            )

        order_id = resp.get("orderID")
        if not order_id:
            return PlaceResult(
                status="rejected", order_id=None, error="no-order-id",
            )

        return PlaceResult(
            status="placed", order_id=order_id, error=None,
            placed_size=float(size_int),
        )

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a resting order. Retries on transient errors.

        Used by the startup reconciler / stranded-fill sweep. Native FAK
        means routine fill paths no longer call cancel directly — partial
        fills are server-side cancelled inside `create_and_post_market_order`.

        Returns True on success, False if all retries fail.
        """
        if self._dry_run:
            return True

        last_exc: Exception | None = None
        for attempt in range(CANCEL_RETRY_MAX + 1):
            try:
                self._client.cancel_order(OrderPayload(orderID=order_id))
                return True
            except Exception as exc:
                last_exc = exc
                if attempt < CANCEL_RETRY_MAX:
                    time.sleep(CANCEL_RETRY_BACKOFF_S * (attempt + 1))
        log.error("cancel_order failed after %d attempts for order %s: %s",
                  CANCEL_RETRY_MAX + 1, order_id, last_exc)
        return False

    def get_usdc_balance(self) -> float:
        """Fetch the collateral (pUSD) balance for the proxy wallet.

        Polymarket returns the balance as a string of raw on-chain units.
        pUSD is a USDC-backed ERC-20 with 6 decimals on Polygon, so the
        /1_000_000 divisor from the USDC.e era still applies. Debug log
        captures the raw response so the divisor can be re-confirmed if
        anything ever drifts.
        """
        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=int(POLY_PROXY_SIG_TYPE),
        )
        resp = self._client.get_balance_allowance(params)
        log.debug("get_balance_allowance raw response: %s", resp)
        return float(resp.get("balance", 0.0)) / 1_000_000

    def get_order_status(self, order_id: str) -> dict:
        if self._dry_run or order_id == "DRY-RUN":
            return {}
        return self._client.get_order(order_id)

    def get_order_book(self, token_id: str) -> dict:
        """Fetch the live order book for a token. Best-effort diagnostic call.

        Returns `{}` on dry-run or any SDK/network error so a fetch failure
        never breaks the trade flow — this is called post-submit purely for
        the adverse-selection diagnostic.
        """
        if self._dry_run:
            return {}
        try:
            return self._client.get_order_book(token_id) or {}
        except Exception as exc:
            log.warning("get_order_book failed for %s: %s", token_id, exc)
            return {}

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
        # /order occasionally returns a null body for an order ID that's still
        # propagating (observed within ~500 ms of a post on v1; behavior on v2
        # may differ but defensive treatment costs nothing).
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
