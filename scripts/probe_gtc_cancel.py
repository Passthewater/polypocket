"""One-shot probe: confirm cancel(order_id) applies to remaining, not original.

Run against live CLOB. Posts a deliberately small GTC at a favorable price
on a known quiet market, waits briefly, cancels, reads /order, prints
size / size_matched / status. If size_matched > 0 and cancel did not
wipe it, the assumption holds.

Usage:
    python scripts/probe_gtc_cancel.py --token <TOKEN_ID> --condition <COND_ID> \\
        --price 0.50 --size 2.0

Requires POLYMARKET_* env vars (same as main bot).
"""

import argparse
import logging
import os
import time

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds, MarketOrderArgs, OrderType,
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

    client = ClobClient(
        host=os.environ["POLYMARKET_HOST"],
        key=os.environ["POLYMARKET_PRIVATE_KEY"],
        chain_id=int(os.environ.get("POLYMARKET_CHAIN_ID", "137")),
        creds=ApiCreds(
            api_key=os.environ["POLYMARKET_API_KEY"],
            api_secret=os.environ["POLYMARKET_API_SECRET"],
            api_passphrase=os.environ["POLYMARKET_API_PASSPHRASE"],
        ),
        signature_type=1,
        funder=os.environ["POLYMARKET_PROXY_ADDRESS"],
    )

    market = client.get_market(args.condition)
    fee_rate_bps = int(market.get("taker_base_fee", 0) or 0)

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

    cancel_resp = client.cancel(order_id)
    log.info("cancel resp: %s", cancel_resp)

    after = client.get_order(order_id)
    log.info("post-cancel /order: size=%s size_matched=%s status=%s",
             after.get("size"), after.get("size_matched"), after.get("status"))


if __name__ == "__main__":
    main()
