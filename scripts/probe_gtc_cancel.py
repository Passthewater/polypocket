"""One-shot probe: confirm cancel(order_id) applies to remaining, not original.

Run against live CLOB. Posts a deliberately small GTC at a price on a live
market, waits briefly, cancels, reads /order before and after cancel, prints
size / size_matched / status. If size_matched > 0 stays unchanged across
the cancel, the assumption holds (cancel applies to remainder only).

Usage (full-match probe — price = best ask, order fills immediately, cancel
is a no-op that should return an error gracefully):
    python scripts/probe_gtc_cancel.py --token <TOKEN_ID> --condition <COND_ID> \\
        --price 0.50 --size 2.0

Usage (partial/rest probe — price 1 tick below best ask, order rests, cancel
should succeed and preserve any pre-match):
    python scripts/probe_gtc_cancel.py --token <TOKEN_ID> --condition <COND_ID> \\
        --price 0.49 --size 2.0

Reads credentials from .env via polypocket.config (PRIVATE_KEY, PROXY_ADDRESS,
CLOB_API_KEY, CLOB_SECRET, CLOB_PASSPHRASE).
"""

import argparse
import logging
import time

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds, MarketOrderArgs, OrderType,
)

from polypocket.config import (
    CHAIN_ID,
    CLOB_API_KEY,
    CLOB_PASSPHRASE,
    CLOB_SECRET,
    POLYMARKET_HOST,
    POLYMARKET_PROXY_ADDRESS,
    PRIVATE_KEY,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("probe")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--token", required=True, help="outcome token id")
    p.add_argument("--condition", required=True, help="condition id")
    p.add_argument("--price", type=float, required=True, help="limit price")
    p.add_argument("--size", type=float, required=True, help="share size")
    p.add_argument("--wait-ms", type=int, default=200)
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

    market = client.get_market(args.condition)
    fee_rate_bps = int(market.get("taker_base_fee", 0) or 0)
    log.info("market fee_rate_bps=%d", fee_rate_bps)

    log.info("posting GTC size=%.2f @ $%.2f token=%s", args.size, args.price, args.token)
    order_args = MarketOrderArgs(
        token_id=args.token,
        amount=round(args.size * args.price, 2),
        price=args.price,
        fee_rate_bps=fee_rate_bps,
    )
    signed = client.create_market_order(order_args)
    resp = client.post_order(signed, OrderType.GTC)
    log.info("post_order resp: %s", resp)

    order_id = resp.get("orderID")
    if not order_id:
        log.error("no order id in response; aborting")
        return

    log.info("sleeping %d ms before cancel", args.wait_ms)
    time.sleep(args.wait_ms / 1000.0)

    before = client.get_order(order_id)
    log.info("pre-cancel /order: size=%s size_matched=%s status=%s",
             before.get("size"), before.get("size_matched"), before.get("status"))

    try:
        cancel_resp = client.cancel(order_id)
        log.info("cancel resp: %s", cancel_resp)
    except Exception as exc:
        log.warning("cancel raised: %s", exc)

    after = client.get_order(order_id)
    log.info("post-cancel /order: size=%s size_matched=%s status=%s",
             after.get("size"), after.get("size_matched"), after.get("status"))


if __name__ == "__main__":
    main()
