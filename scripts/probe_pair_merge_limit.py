"""One-shot probe: post a BUY UP at the pair-merge-aware IOC limit and report.

Validates that `ioc_limit_price(side="up", ..., buffer_ticks=IOC_BUFFER_TICKS)`
actually crosses on a live BTC up/down market before we run a whole session.

Flow:
  1. Fetch current active 5-min BTC window.
  2. Pull UP and DOWN order books via /book.
  3. Compute limit_price = 1 - best_down_bid + IOC_BUFFER_TICKS*0.01.
  4. Post a ~$3 GTC BUY UP at that limit.
  5. Wait briefly, check size_matched, cancel any resting remainder.
  6. Print everything: the inputs, the submitted limit, the response,
     and the final matched vs. rested split.

Usage:
    python scripts/probe_pair_merge_limit.py [--notional 3.00] [--wait-ms 500]

Reads credentials from .env via polypocket.config.
"""

import argparse
import asyncio
import logging
import time

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderType

from polypocket.clients.polymarket import ioc_limit_price
from polypocket.config import (
    CHAIN_ID,
    CLOB_API_KEY,
    CLOB_PASSPHRASE,
    CLOB_SECRET,
    IOC_BUFFER_TICKS,
    POLYMARKET_HOST,
    POLYMARKET_PROXY_ADDRESS,
    PRIVATE_KEY,
)
from polypocket.feeds.polymarket import fetch_active_windows

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("probe")


def book_to_levels(book_side):
    """OrderBookSummary.asks/bids → list of {price, size} dicts, sorted by price."""
    return [{"price": float(lvl.price), "size": float(lvl.size)} for lvl in (book_side or [])]


async def pick_window():
    """Return the currently-live window (first one whose interval brackets now)."""
    windows = await fetch_active_windows()
    now = time.time()
    for w in windows:
        if w.start_time <= now < w.end_time:
            return w
    if windows:
        return windows[0]
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--notional", type=float, default=3.00,
                   help="USDC notional of the probe order (default 3.00)")
    p.add_argument("--wait-ms", type=int, default=500,
                   help="Milliseconds to wait before checking match status")
    args = p.parse_args()

    for name, val in [
        ("PRIVATE_KEY", PRIVATE_KEY),
        ("PROXY_ADDRESS", POLYMARKET_PROXY_ADDRESS),
        ("CLOB_API_KEY", CLOB_API_KEY),
        ("CLOB_SECRET", CLOB_SECRET),
        ("CLOB_PASSPHRASE", CLOB_PASSPHRASE),
    ]:
        if not val:
            raise SystemExit(f"{name} not set in .env")

    window = asyncio.run(pick_window())
    if window is None:
        raise SystemExit("No active BTC 5-min window found")

    log.info("window: slug=%s end=%s", window.slug, window.end_time)
    log.info("tokens: up=%s down=%s", window.up_token_id, window.down_token_id)

    client = ClobClient(
        host=POLYMARKET_HOST,
        key=PRIVATE_KEY,
        chain_id=CHAIN_ID,
        creds=ApiCreds(
            api_key=CLOB_API_KEY,
            api_secret=CLOB_SECRET,
            api_passphrase=CLOB_PASSPHRASE,
        ),
        signature_type=1,
        funder=POLYMARKET_PROXY_ADDRESS,
    )

    up_book_raw = client.get_order_book(window.up_token_id)
    down_book_raw = client.get_order_book(window.down_token_id)
    up_asks = sorted(book_to_levels(up_book_raw.asks), key=lambda x: x["price"])
    up_bids = sorted(book_to_levels(up_book_raw.bids), key=lambda x: -x["price"])
    down_asks = sorted(book_to_levels(down_book_raw.asks), key=lambda x: x["price"])
    down_bids = sorted(book_to_levels(down_book_raw.bids), key=lambda x: -x["price"])

    log.info("UP asks (top 3):   %s", up_asks[:3])
    log.info("UP bids (top 3):   %s", up_bids[:3])
    log.info("DOWN asks (top 3): %s", down_asks[:3])
    log.info("DOWN bids (top 3): %s", down_bids[:3])

    up_best_ask = up_asks[0]["price"] if up_asks else None
    down_best_bid = down_bids[0]["price"] if down_bids else None
    log.info("UP best ask=$%.4f  DOWN best bid=$%.4f", up_best_ask or -1, down_best_bid or -1)

    limit = ioc_limit_price(
        side="up", up_bids=up_bids, down_bids=down_bids,
        buffer_ticks=IOC_BUFFER_TICKS,
    )
    if limit is None:
        raise SystemExit("No DOWN-side bids — no pair-merge counterparty")

    log.info("IOC_BUFFER_TICKS=%d  -> limit_price=$%.4f  (= 1 - %.4f + %d*0.01)",
             IOC_BUFFER_TICKS, limit, down_best_bid, IOC_BUFFER_TICKS)
    log.info("  vs. UP best ask $%.4f (delta %+.4f)",
             up_best_ask, limit - (up_best_ask or 0))

    market = client.get_market(window.condition_id)
    fee_rate_bps = int(market.get("taker_base_fee", 0) or 0)
    # py_clob_client reconstructs taker_amount = amount / limit, rounded to 4dp
    # precision, and the server tick-checks the resulting maker/taker ratio
    # against 0.01. Fractional sizes like 7.69 produce ratios like 0.39000039
    # that are off-grid. Workaround: pick an integer size so amount = size*limit
    # is exact at 2dp and the server's reconstructed ratio is clean.
    size = max(1, round(args.notional / limit))
    amount = round(size * limit, 2)

    log.info("posting GTC size=%d limit=$%.4f amount=$%.2f fee=%dbps",
             size, limit, amount, fee_rate_bps)

    order_args = MarketOrderArgs(
        token_id=window.up_token_id,
        amount=amount,
        price=limit,
        fee_rate_bps=fee_rate_bps,
    )
    signed = client.create_market_order(order_args)
    resp = client.post_order(signed, OrderType.GTC)
    log.info("post_order resp: %s", resp)

    order_id = resp.get("orderID")
    if not order_id:
        log.error("no order id; aborting")
        return

    log.info("sleeping %d ms before status check", args.wait_ms)
    time.sleep(args.wait_ms / 1000.0)

    status = client.get_order(order_id)
    size_matched = float(status.get("size_matched", 0) or 0)
    log.info("/order status: size=%s size_matched=%s status=%s",
             status.get("size"), size_matched, status.get("status"))

    if size_matched >= size - 0.01:
        log.info("FULL MATCH — no cancel needed")
    else:
        log.info("partial/rest: %s of %s matched; cancelling remainder",
                 size_matched, size)
        try:
            cancel_resp = client.cancel(order_id)
            log.info("cancel resp: %s", cancel_resp)
        except Exception as exc:
            log.warning("cancel raised: %s", exc)

    # Final read
    after = client.get_order(order_id)
    log.info("final /order: size=%s size_matched=%s status=%s",
             after.get("size"), after.get("size_matched"), after.get("status"))

    # VERDICT
    final_matched = float(after.get("size_matched", 0) or 0)
    if final_matched >= size - 0.01:
        log.info("VERDICT: CROSSED — pair-merge math is correct at buffer=%d ticks",
                 IOC_BUFFER_TICKS)
    elif final_matched > 0:
        log.info("VERDICT: PARTIAL — %s/%s shares filled via pair-merge. "
                 "Formula works; buffer may need tuning.",
                 final_matched, size)
    else:
        log.warning("VERDICT: NO FILL — order did not cross. Formula or buffer wrong. "
                    "Inspect UP/DOWN book snapshots above.")


if __name__ == "__main__":
    main()
