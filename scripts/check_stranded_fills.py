"""One-shot: query Polymarket for two orders that hit the settlement-lookup
race (trades #68 and #70 in live_trades.db). If either matched on-chain, the
local DB is missing a position; we reconcile here.

The new reconciler in `reconcile_recovered_trade` only runs for the
currently-active window on startup, so these historical windows would never
be swept — this script is the one-time backfill.
"""

import logging
import sqlite3

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, TradeParams

from polypocket.clients.polymarket import PolymarketClient, POLY_PROXY_SIG_TYPE
from polypocket.config import (
    CHAIN_ID,
    CLOB_API_KEY,
    CLOB_PASSPHRASE,
    CLOB_SECRET,
    LIVE_DB_PATH,
    POLYMARKET_HOST,
    POLYMARKET_PROXY_ADDRESS,
    PRIVATE_KEY,
)
from polypocket.executor import reconcile_recovered_trade
from polypocket.ledger import find_trade_by_window_slug

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("check")

# Known suspect order IDs from the investigation.
SUSPECTS = [
    {"trade_id": 68, "order_id": "0x29045df5ea3480fe8957aad0cb48543fa836df7501bd93a986398ce2273f870e",
     "window_slug": "btc-updown-5m-1776901500"},
    {"trade_id": 70, "order_id": "0x16a138b8981f740e5a15180bae826517c61d4685f47e813c9eea466139940752",
     "window_slug": "btc-updown-5m-1776903300"},
]


def main():
    client = PolymarketClient(
        host=POLYMARKET_HOST,
        chain_id=CHAIN_ID,
        private_key=PRIVATE_KEY,
        api_creds={
            "key": CLOB_API_KEY,
            "secret": CLOB_SECRET,
            "passphrase": CLOB_PASSPHRASE,
        },
        proxy_address=POLYMARKET_PROXY_ADDRESS,
        dry_run=False,
    )
    # Drill one level deeper for raw inspection.
    raw_clob = ClobClient(
        host=POLYMARKET_HOST,
        key=PRIVATE_KEY,
        chain_id=CHAIN_ID,
        creds=ApiCreds(
            api_key=CLOB_API_KEY,
            api_secret=CLOB_SECRET,
            api_passphrase=CLOB_PASSPHRASE,
        ),
        signature_type=POLY_PROXY_SIG_TYPE,
        funder=POLYMARKET_PROXY_ADDRESS,
    )

    for sus in SUSPECTS:
        print()
        print("=" * 70)
        print(f"Inspecting trade #{sus['trade_id']} order {sus['order_id']}")
        print("=" * 70)

        # 1. Raw /order
        try:
            order = raw_clob.get_order(sus["order_id"])
            print(f"/order: {order}")
        except Exception as exc:
            print(f"/order raised: {exc}")
            order = None

        # 2. Raw /trades (taker_order_id filter not supported on get_trades;
        #    we walk associate_trades from the /order response).
        trade_ids = (order or {}).get("associate_trades") or []
        fills = []
        for tid in trade_ids:
            try:
                batch = raw_clob.get_trades(TradeParams(id=tid))
                fills.extend(batch)
            except Exception as exc:
                print(f"/trades({tid}) raised: {exc}")
        print(f"associate_trades: {trade_ids}")
        print(f"fills (taker_order_id=={sus['order_id']}): "
              f"{[f for f in fills if f.get('taker_order_id') == sus['order_id']]}")

        # 3. Via our wrapper — this is what the reconciler sees.
        info = client.get_settlement_info(sus["order_id"])
        print(f"SettlementInfo: shares_held={info.shares_held:.6f} "
              f"cost_usdc={info.cost_usdc:.6f}")

        # 4. Reconcile: if stranded, promote the DB row. Else surface status.
        trade_row = find_trade_by_window_slug(LIVE_DB_PATH, sus["window_slug"])
        if trade_row is None:
            print(f"DB: no trade found for window {sus['window_slug']} — skipping.")
            continue
        print(f"DB pre: status={trade_row['status']} "
              f"size={trade_row['size']} entry_price={trade_row['entry_price']} "
              f"error={trade_row['error']!r}")
        final = reconcile_recovered_trade(LIVE_DB_PATH, trade_row, client)
        post = find_trade_by_window_slug(LIVE_DB_PATH, sus["window_slug"])
        print(f"DB post: status={post['status']} "
              f"size={post['size']} entry_price={post['entry_price']} "
              f"error={post['error']!r}  (reconciler returned {final!r})")


if __name__ == "__main__":
    main()
