# CLOB v2 SDK migration — implementation

Companion to `2026-05-15-clob-v2-migration-design.md`. Linear, in-chat
execution — each step is a single concrete action. No subagent dispatch.

## Pre-flight

User has confirmed:
- Bot is stopped.
- USDC.e → pUSD balance manually migrated on funder `0xc84aD1…4C13`.
- Allowances set (assumed; will be verified empirically on Step 9).

Worktree status going in:

```
M polypocket/config.py            (MAX_DAILY_LOSS 50 → 15, unrelated to this work)
M scripts/_pnl_attribution.md     (unrelated)
M tests/test_config.py            (unrelated)
?? docs/project-context.md        (unrelated)
?? scripts/_probe_allowances.py   (unrelated)
```

These pre-existing edits stay; the v2 migration will touch additional
files. Branch off main to keep this isolated.

## Step 1 — Branch + dep swap

```powershell
git checkout -b feat/clob-v2-migration
```

In `pyproject.toml`, replace the single line:

```
    "py-clob-client==0.19.0",
```

with:

```
    "py-clob-client-v2==1.0.1",
```

Then:

```powershell
pip install -e .
```

Confirm:

```powershell
python -c "import py_clob_client_v2; print(py_clob_client_v2.__file__)"
python -c "from py_clob_client_v2 import ClobClient, SignatureTypeV2, OrderType, OrderPayload, MarketOrderArgs, PartialCreateOrderOptions, Side; print('imports ok')"
```

If either line errors, stop and resolve before proceeding.

Sanity probe — what does v2 actually expose for cancellation:

```powershell
python -c "from py_clob_client_v2 import ClobClient; c = [a for a in dir(ClobClient) if 'cancel' in a.lower()]; print(c)"
```

Expect to see `cancel_order` for sure; note whether `cancel` is also
present as an alias. The implementation in Step 3 uses `cancel_order`;
flip if needed.

## Step 2 — Backup `live_trades.db`

```powershell
Copy-Item live_trades.db "live_trades.pre-v2-sdk-20260515.bak.db"
```

(The existing `live_trades.pre-v2-flip-20260515.bak.db` is the model-v2
flip backup, distinct from this SDK swap.)

## Step 3 — Rewrite `polypocket/clients/polymarket.py`

Single complete rewrite. Skeleton:

### Import block

```python
import logging
import math
import time

from py_clob_client_v2 import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    ClobClient,
    MarketOrderArgs,
    OrderPayload,
    OrderType,
    PartialCreateOrderOptions,
    Side,
    SignatureTypeV2,
    TradeParams,
)

from polypocket.config import FOK_SLIPPAGE_TICKS
from polypocket.executor import FillResult, SettlementInfo

log = logging.getLogger(__name__)

POLY_PROXY_SIG_TYPE = SignatureTypeV2.POLY_PROXY  # = 1

CANCEL_RETRY_MAX = 2
CANCEL_RETRY_BACKOFF_S = 0.25
TICK_SIZE = "0.01"  # BTC up/down markets — all our markets are 1¢ tick
```

Notes:

- `SignatureTypeV2.POLY_PROXY` is an `IntEnum` value of 1; passes through
  the kwarg cleanly.
- We hardcode `TICK_SIZE = "0.01"` rather than dynamically querying
  `get_market(condition_id).minimum_tick_size`. Every BTC up/down market
  we trade is 1¢. Documented in the comment.

### `__init__`

```python
class PolymarketClient:
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
```

`_fee_rate_bps_cache` field — **deleted**. v2 handles fees server-side.

### `_fee_rate_bps` method — DELETED

Including the `get_market(condition_id)` lookup. No replacement.

### `_tick_safe_size` — UNCHANGED

Keep the workaround in place. Marked in design as a follow-up to revisit
once we have one or two live fills with the v2 SDK and can A/B remove it.

### `fok_limit_price`, `ioc_limit_price` — UNCHANGED

Module-level pure functions, unrelated to SDK semantics.

### `submit_fok`

Old: 2 calls (`create_market_order` → `post_order(signed, OrderType.FOK)`),
passes `fee_rate_bps`.

New: single `create_and_post_market_order` call, native FOK semantics,
no fee_rate_bps:

```python
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
        log.exception("submit_fok network/signing error")
        return FillResult(
            status="error", order_id=None, filled_size=0.0,
            avg_price=None, error=f"network: {exc}",
        )

    # Response shape per docs: success, orderID, status (live|matched|delayed),
    # makingAmount, takingAmount, transactionsHashes, tradeIDs, errorMsg.
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
        filled = size

    return FillResult(
        status="filled", order_id=order_id, filled_size=filled,
        avg_price=price, error=None,
    )
```

Changes from v1:

- Single combined call (built-in version-update retry).
- `side=Side.BUY` is now required on `MarketOrderArgs`.
- `order_type` is set on the args AND passed to `create_and_post_market_order`
  (v2 source uses the kwarg for routing).
- `fee_rate_bps` removed.
- Tick-size now per-call.

### `submit_ioc`

The v1 implementation hand-rolled true-IOC by posting GTC, checking
`size_matched`, cancelling the remainder, and degraded-estimating fills
when `/trades` lagged. v2's native `FAK` does this server-side.

```python
def submit_ioc(self, side, price, size, token_id, condition_id, limit_price):
    """Post FAK at caller-supplied limit price; partial fills accepted, remainder server-cancelled."""
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

    # FAK: any partial match shows up in /trades; we read the real fill
    # cost there (the v1 degraded-estimate path is gone because there's
    # no GTC-then-cancel race window with native FAK).
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
```

Net deletion: ~50 lines (the `get_order` → `cancel_order` → "fully_matched"
check → degraded-fallback estimate block in v1's `submit_ioc`).

### `cancel_order`

```python
def cancel_order(self, order_id: str) -> bool:
    """Cancel a resting order. Retries on transient errors.

    Used by the startup reconciler / stranded-fill sweep. Native FAK
    means routine fill paths no longer call cancel directly.
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
```

If Step 1's probe shows the v2 client only exposes `cancel(order_id=str)`
(legacy alias), swap to `self._client.cancel(order_id=order_id)`. The
choice is one line.

### `get_usdc_balance`, `get_order_status`, `get_settlement_info`

Unchanged. Same signatures, same response-field reads. The internal
HTTP call delegates to v2's pass-through `_get` for these endpoints.

One log line worth adding to `get_usdc_balance` on first call after
the migration so we capture the raw response in case the divisor needs
adjustment:

```python
def get_usdc_balance(self) -> float:
    params = BalanceAllowanceParams(
        asset_type=AssetType.COLLATERAL,
        signature_type=int(POLY_PROXY_SIG_TYPE),
    )
    resp = self._client.get_balance_allowance(params)
    log.debug("get_balance_allowance raw response: %s", resp)
    return float(resp.get("balance", 0.0)) / 1_000_000
```

(`int(POLY_PROXY_SIG_TYPE)` because `BalanceAllowanceParams.signature_type`
is typed as `int`, not the IntEnum.)

## Step 4 — Rewrite `tests/test_polymarket_client.py`

Mock-surface changes. The fixture currently patches
`polypocket.clients.polymarket.ClobClient`; same patch path works,
but the methods we mock on the instance change. Concretely:

| v1 mock | v2 mock |
|---|---|
| `inst.create_market_order.return_value = MagicMock()` | _delete_ |
| `inst.post_order.return_value = {"success": True, "status": "matched", "orderID": "abc"}` | `inst.create_and_post_market_order.return_value = {"success": True, "status": "matched", "orderID": "abc"}` |
| `inst.cancel.return_value = None` | `inst.cancel_order.return_value = None` |
| `inst.get_market.return_value = {"taker_base_fee": 1000}` | _delete_ |

Tests to update / delete:

- `test_submit_fok_passes_market_fee_rate` — **delete** (no fee_rate_bps in v2).
- `test_submit_fok_caches_market_fee` — **delete** (no fee cache in v2).
- `test_submit_fok_market_lookup_failure_uses_zero_fee` — **delete** (no get_market call in v2).
- All remaining `test_submit_fok_*` tests — re-point `post_order`
  assertion to `create_and_post_market_order`.
- All `test_submit_ioc_*` tests — re-point to
  `create_and_post_market_order` and drop the
  `get_order` → `cancel` → degraded-fallback assertions. Add fresh
  tests for the FAK-native paths: full fill via `/trades`, partial fill
  via `/trades`, server-rejected (resp.success=False), settlement
  lookup raises, FAK matched nothing.
- All `test_cancel_order_*` tests — re-point `inst.cancel` →
  `inst.cancel_order`, and adjust the call-arg check to expect a
  positional `OrderPayload(orderID=...)` not a kwarg `order_id=...`.

`test_get_settlement_info_*`: unchanged — same `get_order` /
`get_trades` shapes.

The non-CLOB tests (`test_fok_limit_price_*`,
`test_ioc_limit_price_*`) are pure-function tests; **no changes**.

Run after edits:

```powershell
pytest tests/test_polymarket_client.py -v
```

Then the full suite:

```powershell
pytest tests/ -v
```

## Step 5 — Migrate scripts

`scripts/diagnose_live_auth.py`:

- Imports: `from py_clob_client.client import ClobClient` →
  `from py_clob_client_v2 import ClobClient`; same for
  `clob_types`-imported names.
- The `[(0, "EOA"), (1, "POLY_PROXY"), (2, "POLY_GNOSIS_SAFE")]` loop
  body uses signature_type integers; keep the literals but reference
  `SignatureTypeV2` for clarity in the labels.

`scripts/derive_clob_creds.py`:

- Same import swap. If it calls `client.create_or_derive_api_key()`, the
  v2 surface is identical (verified in v2 source `client.py:508`).

`scripts/check_stranded_fills.py`:

- Reads via our `PolymarketClient` wrapper. Likely zero changes after
  the wrapper rewrite. Confirm by `grep -n "py_clob_client" scripts/check_stranded_fills.py`
  and adjust only if direct SDK imports exist.

Probe scripts (`probe_gtc_cancel.py`, `probe_pair_merge_limit.py`):
defer. Not on the live path; archive-only.

## Step 6 — Paper-mode smoke

```powershell
$env:TRADING_MODE = "paper"
python -m polypocket
```

(Or whatever the standard bot start command is — there's an alias in
the project; the `__main__.py` exposes the same entry point.)

Watch for:

1. Import-time success (no `ModuleNotFoundError` on `py_clob_client_v2`).
2. One full decision/close cycle: a fresh row in
   `window_snapshots` with `snapshot_type='decision'` and the matching
   `close` row 5 min later.
3. No new `ERROR` lines in stderr.

Cancel the bot after 1–2 windows.

Paper mode doesn't construct `PolymarketClient` (TRADING_MODE=paper
short-circuits at `__main__.py:50`), so this is purely import-time
correctness — but that alone has caught regressions before.

## Step 7 — Single-shot live probe

Write a throwaway script (don't commit) that does one minimum-size FOK
against a real BTC up/down market on the production CLOB:

```python
# scripts/_probe_v2_fok.py  (gitignored)
import os
from polypocket.clients.polymarket import PolymarketClient, fok_limit_price
from polypocket import config

client = PolymarketClient(
    host=config.POLYMARKET_HOST,
    chain_id=config.CHAIN_ID,
    private_key=os.environ["PRIVATE_KEY"],
    api_creds={
        "key": os.environ["CLOB_API_KEY"],
        "secret": os.environ["CLOB_SECRET"],
        "passphrase": os.environ["CLOB_PASSPHRASE"],
    },
    proxy_address=os.environ["PROXY_ADDRESS"],
    dry_run=False,
)

print("balance:", client.get_usdc_balance())  # should be >0 in pUSD

# Pick an active BTC up/down market token_id + condition_id manually
# (grab from /markets or the web UI). Choose a market deep enough
# that a 5-share order at the best ask is trivially fillable.
TOKEN_ID = "..."       # fill in
CONDITION_ID = "..."   # fill in
SIZE = 5               # ~$2–$4 depending on price
PRICE = 0.50           # placeholder; replace with current best ask + small cushion

fill = client.submit_fok(
    side="up", price=PRICE, size=SIZE,
    token_id=TOKEN_ID, condition_id=CONDITION_ID,
)
print(fill)

if fill.status == "filled":
    info = client.get_settlement_info(fill.order_id)
    print("settlement:", info)
```

Acceptance for this probe:

- `client.get_usdc_balance()` returns a sensible $>0 number — confirms
  pUSD balance is visible to v2 SDK at the right decimal scale.
- `fill.status == "filled"` (or `"rejected"` with `"fak-no-fill"` if
  the book moved — either way, the *server-level* rejection
  `order_version_mismatch` MUST NOT appear).
- `get_settlement_info` returns `shares_held > 0` and
  `cost_usdc` within a cent of `SIZE * fill.avg_price`.

If `fill.error` contains `insufficient allowance` or
`balance/allowance error`, the manual pUSD allowance step was
incomplete — pause and fix on-chain before proceeding.

If anything else fails: STOP. Do not flip the bot to live with a
partial migration.

## Step 8 — Flip to live

```powershell
$env:TRADING_MODE = "live"
python -m polypocket
```

Watch the first 3–5 decision windows by tailing the log and querying
`window_snapshots` + `trades` between windows:

```powershell
# After ~15 min of live mode:
sqlite3 live_trades.db "SELECT id, timestamp, side, status, error FROM trades WHERE timestamp > datetime('now', '-1 hour') ORDER BY id"
```

Acceptance: any `trade_fired=1` decision results in a row with
`status='open'` (filled) or `status='rejected'` with an error that is
NOT `order_version_mismatch` or `insufficient allowance`.

If 2 consecutive trade attempts in live mode fail with the same error
class, stop the bot and investigate before letting it keep churning.

## Step 9 — Commit + PR

Conventional-style commits:

```
chore: pin py-clob-client-v2 in pyproject.toml
feat(clients): migrate Polymarket client to py-clob-client-v2 SDK
test(clients): retarget mocks at py-clob-client-v2 surface
chore(scripts): swap py_clob_client imports for v2 in diagnostic scripts
docs(plans): clob v2 migration design + implementation
```

PR title: `feat: migrate to py-clob-client-v2 after CLOB v2 cutover`.
Reference the existing live-rejection observation in the description.

## Step 10 — Update memory after stable live runs

Once Step 8 produces at least one settled live trade with a real PnL:

- Update `polymarket_clob_v2_migration.md` from "migration blocker"
  framing to a historical fact (migration completed at <commit-sha>).
- Update `polymarket_account_type.md` to note the funder's allowances
  are now against the V2 Exchange contracts (the v1 addresses in the
  current note are stale).
- Consider writing a new reference memory `polymarket_v2_contracts.md`
  with the V2 exchange addresses + pUSD token address, since these
  are stable facts likely to be referenced again.

## Effort estimate

| Step | Time |
|---|---|
| 1 — branch + dep swap | 5 min |
| 2 — DB backup | 1 min |
| 3 — rewrite polymarket.py | 60–90 min |
| 4 — rewrite tests | 60–90 min |
| 5 — migrate scripts | 15 min |
| 6 — paper smoke | 10 min |
| 7 — live probe | 20 min |
| 8 — live flip + watch | 30 min |
| 9 — commit + PR | 15 min |

Total: ~3.5–4.5 hours of focused work. Most of it is the test-mock
retrofit in Step 4.
