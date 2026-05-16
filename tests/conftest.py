"""Test-suite fixtures.

Isolates tests from the developer's local `.env`. `polypocket.config` calls
`load_dotenv(override=True)` at import so the live bot treats `.env` as
authoritative — in tests we instead want the module-level defaults. Stub
`dotenv.load_dotenv` to a no-op before any test imports `polypocket.config`.
"""

import os

import dotenv

dotenv.load_dotenv = lambda *args, **kwargs: False

for _key in (
    "MIN_POSITION_USDC",
    "MAX_POSITION_USDC",
    "TRADING_MODE",
    "FOK_SLIPPAGE_TICKS",
    "DEPTH_CLAMP_BUFFER",
    "MIN_FILL_RATIO",
    "MAX_BOOK_AGE_S",
    "SIGNAL_CUSHION_TICKS",
    "IOC_BUFFER_TICKS",
    "ENTRY_MODE",
    "POST_ONLY_REST_OFFSET_TICKS",
    "POST_ONLY_CANCEL_AT_T_REMAINING_S",
    "POST_ONLY_EXPIRY_SAFETY_BUFFER_S",
):
    os.environ.pop(_key, None)
