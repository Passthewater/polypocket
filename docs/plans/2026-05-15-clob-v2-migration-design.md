# CLOB v2 SDK migration — design

## Problem

Polymarket completed their CLOB v2 server migration on 2026-04-28. The
EIP-712 Exchange domain version bumped from `"1"` to `"2"`; legacy V1 SDKs
sign with the old version and every order is rejected by the server with
HTTP 400 `{"error": "order_version_mismatch"}`. We're pinned to
`py-clob-client==0.19.0`, so since the cutover every order this bot has
ever signed has been server-rejected. Confirmed empirically on the
2026-05-15 live session: 4 gate-passing decisions, 4 `order_version_mismatch`
rejects, 0 fills.

Resolution path per Polymarket's [migration changelog entry][changelog]
and the [v2 SDK reference][sdks] is to install `py-clob-client-v2` and
remove the legacy package. The new SDK changes the import surface, the
order constructors, the order-type enum, the cancel API, and removes
client-side fee handling.

In parallel, the v1 → v2 contract swap means our previous on-chain
allowances against the V1 Exchange contracts are dead; the user has
already manually migrated USDC.e → pUSD and re-approved allowances against
the V2 CTF Exchange (`0xE111180000d2663C0091e4f400237545B87B996B`) and
Neg Risk CTF Exchange (`0xe2222d279d744050d28e00520010520000310F59`).
That work is out of scope here; this plan is the SDK-level code work that
follows.

[changelog]: https://docs.polymarket.com/changelog
[sdks]: https://docs.polymarket.com/api-reference/clients-sdks.md

## Goal

Get `TRADING_MODE=live` back to a working state: orders sign with the v2
domain, the bot's `submit_fok` / `submit_ioc` paths still satisfy the
`LiveOrderClient` protocol in `executor.py`, settlement accounting in
`get_settlement_info` ties out, and the existing reconciler / stranded-fill
sweep keeps working.

## V1 → V2 API deltas (verified against official docs + v2 source)

| Concern | v1 (`py_clob_client` 0.19.0) | v2 (`py_clob_client_v2` 1.0.1) |
|---|---|---|
| Package | `py-clob-client` | `py-clob-client-v2` |
| Imports | `py_clob_client.client.ClobClient` | `py_clob_client_v2.ClobClient` |
| Imports — types | `py_clob_client.clob_types.{ApiCreds,AssetType,BalanceAllowanceParams,MarketOrderArgs,OrderType,TradeParams}` | `py_clob_client_v2.{ApiCreds,AssetType,BalanceAllowanceParams,MarketOrderArgs,OrderType,TradeParams,PartialCreateOrderOptions,OrderPayload,Side,SignatureTypeV2}` |
| `ClobClient.__init__` | `host, key, chain_id, creds, signature_type, funder` | same kwargs; pass `SignatureTypeV2.POLY_PROXY` (= 1) for clarity instead of the literal `1` |
| Order placement | `client.create_market_order(args)` → `client.post_order(signed, OrderType.FOK)` | single call: `client.create_and_post_market_order(args, options=PartialCreateOrderOptions(tick_size="0.01"), order_type=OrderType.FOK)`; built-in `_retry_on_version_update` |
| `MarketOrderArgs` fields | `token_id, amount, price, fee_rate_bps` (no `side`, no `order_type`) | `token_id, amount, side, price, order_type, user_usdc_balance, builder_code, metadata` — `side` and `order_type` **required** (with defaults); `fee_rate_bps` **gone** (server computes per [fees docs][fees]) |
| `Side` | string `"BUY"/"SELL"` literals | `Side` IntEnum (`BUY=0, SELL=1`) |
| `OrderType` enum | `GTC, FOK, IOC` | `GTC, FOK, FAK, GTD` — **no IOC**; FAK is the new "fill what you can, kill rest" |
| `tick_size` | implicit | per-call via `PartialCreateOrderOptions(tick_size="0.01")` |
| Cancel | `client.cancel(order_id=str)` | `client.cancel_order(payload=OrderPayload(orderID=str))` (verified in v2 `client.py`; docs example still shows the legacy `client.cancel(order_id=...)` form — may be a docs lag, we use the `OrderPayload` form from source) |
| `BalanceAllowanceParams` | `asset_type, signature_type` | same (now Optional defaults) |
| `TradeParams(id=tid)` | same | same |
| `get_market(condition_id)` | returns `taker_base_fee` etc. | still present; we no longer need it for fees |
| Order owner | `creds.api_key` | `creds.api_key` (unchanged) |
| Response shape | `success, status, orderID, errorMsg` | `success, orderID, status (live|matched|delayed), makingAmount, takingAmount, transactionsHashes, tradeIDs, errorMsg` per [POST /order][post-order] — superset of v1, our existing predicate `resp.get("success") and resp.get("status") == "matched"` still works unchanged |

[fees]: https://docs.polymarket.com/trading/fees.md
[post-order]: https://docs.polymarket.com/api-reference/trade/post-a-new-order.md

Material consequences:

- **`submit_ioc` simplifies dramatically.** v1 lacked native IOC, so we
  posted a `GTC` and hand-rolled a true-IOC by checking `size_matched`
  and cancelling the remainder, with a pessimistic-estimate fallback for
  the indexing-race case. v2's `FAK` does this natively; we make one
  call and read settlement from `/trades`. The `get_order`/`cancel_order`/
  degraded-estimate paths in our current `submit_ioc` go away.
- **`_fee_rate_bps_cache` becomes dead code.** v2 manages fees server-side.
  Delete `_fee_rate_bps()` and its cache field.
- **`_tick_safe_size` workaround**: keep for now. The v1 bug was a
  float-based `round_down` in `py_clob_client.order_utils`; v2 has a
  rewritten order builder. Probably no longer needed, but defense-in-depth
  is cheap and the workaround is independent of SDK version semantics.
  Mark as a follow-up candidate to remove.
- **Pair-merge limit math (`ioc_limit_price`, `fok_limit_price`) stays put.**
  This is our edge-gate cushion logic against pair-merge clearing prices,
  not SDK behavior. No SDK migration touches it.

## Things still uncertain — to verify during execution (not blockers)

1. `cancel_order` API: docs show `client.cancel(order_id=str)`, v2 source
   shows `client.cancel_order(OrderPayload)`. Both probably exist
   (alias). We use the `OrderPayload` form from the source-of-truth; if
   it's actually wrong we get an `AttributeError` at import or call
   time, trivial to fix.
2. `get_balance_allowance` response numeric scale: v1 returned `balance`
   as a raw 6-decimal string (`/1_000_000` → USDC). pUSD is a
   USDC-backed ERC-20 on Polygon; almost certainly also 6 decimals, but
   we'll log the raw response on first live probe and confirm before
   trusting the divisor.
3. `get_order` response still includes `size_matched` and
   `associate_trades` (required by `get_settlement_info`). The /order
   endpoint shape isn't documented to have changed, but we'll verify on
   the probe.
4. `MarketOrderArgsV2.amount` interpretation: docs say `BUY orders: $$$
   Amount to buy` (matches v1 USDC-budget semantics, what we pass as
   `round(size * price, 2)`). Verify against a real fill that
   `info.shares_held × info.cost_usdc / info.shares_held` ties out to
   our expected `size`.

## Out of scope

- Migrating to POLY_1271 (deposit wallets). The auth docs explicitly say
  existing POLY_PROXY users *"can keep using their current funder
  address and signature type"*. We stay on `signature_type=1`.
- Builder code attribution (`builder_code` field on `MarketOrderArgsV2`).
  We're not a builder.
- `user_usdc_balance` fee adjustment on `MarketOrderArgsV2`. Optional;
  used to slightly improve fill quality when buying with most-of-balance.
  Skip — we're sizing small ($5–$20) against >$100 balances, the
  adjustment is negligible.
- Sell-side support. We never `Side.SELL`; we only buy outcome tokens
  at decision time and let them settle to 0 or 1.
- Migrating the one-off probe scripts (`probe_gtc_cancel.py`,
  `probe_pair_merge_limit.py`). Used historically for diagnosis; not on
  the live path.

## Files changed

| File | Type of change |
|---|---|
| `pyproject.toml` | swap `py-clob-client==0.19.0` → `py-clob-client-v2==1.0.1` |
| `polypocket/clients/polymarket.py` | full rewrite of import block, client init signature args, `submit_fok`, `submit_ioc`, `cancel_order`, drop `_fee_rate_bps_cache` + `_fee_rate_bps`, keep `fok_limit_price`/`ioc_limit_price`/`_tick_safe_size`/`get_settlement_info`/`get_usdc_balance`/`get_order_status` largely unchanged |
| `tests/test_polymarket_client.py` | re-point mocks: `inst.create_market_order` + `inst.post_order` → `inst.create_and_post_market_order`; `inst.cancel` → `inst.cancel_order`; drop `fee_rate_bps` assertions; update response shapes where needed |
| `scripts/diagnose_live_auth.py` | swap imports + use v2 `SignatureTypeV2` enum |
| `scripts/derive_clob_creds.py` | swap imports + `create_or_derive_api_key` path |
| `scripts/check_stranded_fills.py` | swap imports (only) — uses our `PolymarketClient` wrapper, not the SDK directly, so likely no further change |

Out of touch: `polypocket/bot.py`, `polypocket/__main__.py`, `polypocket/executor.py`. They depend only on our `PolymarketClient` wrapper and the `LiveOrderClient` protocol. The protocol signatures don't change; the implementation does.

## Risk register

| Risk | Mitigation |
|---|---|
| v2 `cancel_order(OrderPayload)` form is wrong → `AttributeError` | Smoke-call from a REPL before code-rewrite; flip to `client.cancel(order_id=...)` if the source-of-truth form fails |
| `get_balance_allowance` numeric scale changes under pUSD → balance gate reads wrong | Log raw response on first live probe; only trust `/1_000_000` divisor after confirming |
| `MarketOrderArgsV2.amount` semantics differ from v1 → over- or under-spend | First live probe is min-size single FOK; compare cost_usdc from `/trades` against the `amount` we sent; halt if mismatch > 1¢ |
| FAK partial fill returns no `tradeIDs` in response → settlement lookup races | Existing settlement-lookup flow is post-fill (queries `/trades` by `associate_trades`), unchanged; the v1 degraded-estimate path was only for the GTC-then-cancel race, which no longer exists with native FAK |
| v2 SDK's built-in `_retry_on_version_update` interferes with our error handling | The retry only fires on `order_version_mismatch`; if we ever see one post-migration it's a real bug — retry semantics are fine |
| Allowance still wrong despite manual migration → orders reject with `insufficient allowance` rather than `order_version_mismatch` | First live probe is single trade; reject reason will be clear; user's manual migration is asserted by them but trust-but-verify |

## Acceptance bar

1. `pytest tests/` green after the rewrite.
2. Bot starts in `TRADING_MODE=paper` without import-time crash; one
   complete decision/close cycle logs into `window_snapshots`.
3. One hand-crafted FOK probe (min-size, against the real production
   CLOB, sig_type=1, our funder) returns `success: true,
   status: "matched"`; `get_settlement_info` returns
   `shares_held > 0` and `cost_usdc` within 1¢ of the submitted
   USDC-budget × VWAP.
4. `TRADING_MODE=live` for 3–5 windows: any gate-passing trade fills (no
   `order_version_mismatch`, no `insufficient allowance`, no
   `network: PolyApiException` errors with status 400). Settlement on
   resolution writes non-null `pnl` per `settle_live_trade`.

Only after #4 do we leave the bot running unattended.
