#!/usr/bin/env python3
"""Derive L2 CLOB API credentials from PRIVATE_KEY.

Usage:
    python scripts/derive_clob_creds.py

Reads PRIVATE_KEY from .env, calls Polymarket to create-or-derive L2 API creds
(idempotent — safe to re-run; returns the same creds for the same EOA), prints
them in `.env` format.
"""

import os
import sys

from dotenv import load_dotenv
from py_clob_client.client import ClobClient


def main() -> None:
    load_dotenv()
    private_key = os.getenv("PRIVATE_KEY", "").strip()
    if not private_key:
        print("ERROR: PRIVATE_KEY is empty in .env", file=sys.stderr)
        sys.exit(1)

    host = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
    chain_id = int(os.getenv("CHAIN_ID", "137"))

    client = ClobClient(host=host, key=private_key, chain_id=chain_id)
    creds = client.create_or_derive_api_creds()

    print()
    print("# Paste these into your .env:")
    print(f"CLOB_API_KEY={creds.api_key}")
    print(f"CLOB_SECRET={creds.api_secret}")
    print(f"CLOB_PASSPHRASE={creds.api_passphrase}")
    print()


if __name__ == "__main__":
    main()
