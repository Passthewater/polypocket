#!/usr/bin/env python3
"""Diagnose Polymarket CLOB 'invalid signature' order rejects.

Background: /balance-allowance returning a real balance does NOT prove the
local PROXY_ADDRESS is correct — the server looks up the funder server-side
from (authenticated EOA, signature_type). But order signing includes the
PROXY_ADDRESS as `maker` in the EIP-712 payload, and the server validates
that the signer EOA owns that exact proxy. If they disagree, the API
returns 400 `invalid signature`.

Per Polymarket contributors (py-clob-client#277): "invalid signature
means the backend can't verify your EOA as an authorized signer for that
proxy address."

This script:
  1. Prints EOA (from PRIVATE_KEY) and the env PROXY_ADDRESS
  2. Probes /balance-allowance with sig_type 0, 1, 2 — compare against
     the balance shown on polymarket.com
  3. Sends a tiny BUY FOK far below market so it'll reject, and prints
     the full server error (signature errors vs insufficient-balance
     errors look different)
  4. Tells you exactly what to check next

Run:
    python scripts/diagnose_live_auth.py <token_id> <condition_id>

Where <token_id>/<condition_id> are from any active Polymarket market
(e.g., from polypocket.feeds.polymarket logs). If you omit them, only
steps 1-2 run.
"""

import os
import sys

from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds, AssetType, BalanceAllowanceParams, OrderArgs, OrderType,
)
from py_clob_client.order_builder.constants import BUY


def main() -> None:
    load_dotenv()
    pk = os.getenv("PRIVATE_KEY", "").strip()
    proxy = os.getenv("PROXY_ADDRESS", "").strip()
    host = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
    chain_id = int(os.getenv("CHAIN_ID", "137"))
    api_key = os.getenv("CLOB_API_KEY", "").strip()
    api_secret = os.getenv("CLOB_SECRET", "").strip()
    api_passphrase = os.getenv("CLOB_PASSPHRASE", "").strip()

    missing = [n for n, v in [("PRIVATE_KEY", pk), ("PROXY_ADDRESS", proxy),
                              ("CLOB_API_KEY", api_key), ("CLOB_SECRET", api_secret),
                              ("CLOB_PASSPHRASE", api_passphrase)] if not v]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)

    # === Step 1: addresses =================================================
    probe = ClobClient(host=host, key=pk, chain_id=chain_id, creds=creds)
    eoa = probe.get_address()
    print("=" * 70)
    print("Step 1 — Addresses")
    print("=" * 70)
    print(f"  EOA (from PRIVATE_KEY):  {eoa}")
    print(f"  PROXY_ADDRESS (env):     {proxy}")
    print()
    print("  >>> Open https://polymarket.com/settings in a browser.")
    print("  >>> Confirm the wallet address shown there equals PROXY_ADDRESS above.")
    print("  >>> If they differ, update .env: PROXY_ADDRESS=<value from settings>")
    print()

    # === Step 2: balance probe with each sig_type ===========================
    print("=" * 70)
    print("Step 2 — /balance-allowance per sig_type (server-side funder lookup)")
    print("=" * 70)
    for sig_type, label in [(0, "EOA"), (1, "POLY_PROXY"), (2, "POLY_GNOSIS_SAFE")]:
        try:
            client = ClobClient(host=host, key=pk, chain_id=chain_id, creds=creds,
                                signature_type=sig_type, funder=proxy)
            resp = client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL,
                                       signature_type=sig_type)
            )
            raw_bal = resp.get("balance", "0") or "0"
            raw_allow = resp.get("allowance", "0") or "0"
            print(f"  sig_type={sig_type} ({label:16}) "
                  f"balance=${int(raw_bal)/1_000_000:.6f}  "
                  f"allowance=${int(raw_allow)/1_000_000:.2f}")
        except Exception as exc:
            print(f"  sig_type={sig_type} ({label:16}) ERROR: {exc}")
    print()
    print("  >>> The sig_type whose balance matches polymarket.com is the right one.")
    print("  >>> If allowance is 0 for the matching sig_type, update allowance via")
    print("      ClobClient.update_balance_allowance(...) — orders need USDC")
    print("      approved to the CTF Exchange contract.")
    print()

    if len(sys.argv) < 3:
        print("(Skipping step 3: pass <token_id> <condition_id> to run the order probe)")
        return

    token_id, condition_id = sys.argv[1], sys.argv[2]

    # === Step 3: tiny order probe ==========================================
    print("=" * 70)
    print("Step 3 — Order-submit probe (sig_type=1 first, then 0 and 2)")
    print("=" * 70)
    for sig_type, label in [(1, "POLY_PROXY"), (0, "EOA"), (2, "POLY_GNOSIS_SAFE")]:
        client = ClobClient(host=host, key=pk, chain_id=chain_id, creds=creds,
                            signature_type=sig_type, funder=proxy)
        try:
            market = client.get_market(condition_id)
            fee = int(market.get("taker_base_fee", 0) or 0)
        except Exception:
            fee = 0
        # Place at $0.01 (way below any UP/DOWN ask) — will reject unfilled
        # but the REJECT reason tells us what the server thinks.
        args = OrderArgs(token_id=token_id, price=0.01, size=5.0,
                         side=BUY, fee_rate_bps=fee)
        try:
            signed = client.create_order(args)
            resp = client.post_order(signed, OrderType.FOK)
            print(f"  sig_type={sig_type} ({label:16}) server resp: {resp}")
        except Exception as exc:
            print(f"  sig_type={sig_type} ({label:16}) RAISED: {type(exc).__name__}: {exc}")
    print()
    print("  Interpreting the results:")
    print("   - 'not enough balance / allowance' -> signatures valid, fund/approve issue")
    print("   - 'insufficient balance'           -> signatures valid, wrong funder")
    print("   - 'invalid signature' on ALL three -> EOA doesn't own PROXY_ADDRESS")
    print("                                        (wrong PRIVATE_KEY or wrong PROXY_ADDRESS)")
    print("   - one sig_type gives a different error than others -> that's the right one")


if __name__ == "__main__":
    main()
