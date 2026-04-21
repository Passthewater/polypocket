# Startup order-status reconciliation against CLOB (issue #5)

Follow-up B from #3 / `2026-04-21-live-trading-mvp-design.md`.

## Problem

On startup recovery of `reserved` / `open` trades (`bot.py:158-179`), the bot
blindly restores the local DB row to `_open_trade` without asking the CLOB
what actually happened. If the process crashed between `submit_fok` and the
follow-up `update_trade`, the DB can say `reserved` while the CLOB says:

- `MATCHED` — we own the position; local should be `open`.
- `CANCELED` / `UNMATCHED` — no position; local should be `rejected`.

Blind recovery can either miss a filled position or resume into a phantom
open trade.

## Design

### 1. Protocol update

Add `get_order_status(order_id: str) -> dict` to the `LiveOrderClient`
Protocol in `executor.py`. `PolymarketClient.get_order_status` already
exists (`clients/polymarket.py:150`).

### 2. Helper: `reconcile_recovered_trade`

In `executor.py`:

```python
def reconcile_recovered_trade(
    db_path: str,
    trade: dict,
    client: LiveOrderClient | None,
) -> str:
    """Query CLOB for the recovered trade's order status and reconcile local DB.

    Returns the final local status string: "open" (resume) or "rejected" (skip).
    Called only in live mode during startup recovery in bot.py.
    """
```

Branches:

| Condition | DB write | Return |
|---|---|---|
| No `external_order_id` or no `client` | none | `trade["status"]` |
| `get_order_status` raises | none (log warn) | `trade["status"]` |
| CLOB status `matched` | `status="open"` if not already | `"open"` |
| CLOB status `canceled`/`cancelled`/`unmatched` | `status="rejected"` | `"rejected"` |
| CLOB status `live` (resting — impossible for FOK) | none (log warn) | `trade["status"]` |
| Any other/unknown | none (log warn) | `trade["status"]` |

Rationale for preserving current behavior on errors: a transient CLOB outage
at startup shouldn't destroy our ability to resume an already-known-good
`open` trade. If the helper can't prove the state is wrong, leave it alone.

### 3. Bot wiring

Replace `bot.py:158-179`:

```python
recovered_trade = find_trade_by_window_slug(self.db_path, window.slug)
recoverable_statuses = {"open"}
if TRADING_MODE == "live":
    recoverable_statuses.add("reserved")

if recovered_trade is not None and recovered_trade["status"] in recoverable_statuses:
    final_status = recovered_trade["status"]
    if TRADING_MODE == "live" and recovered_trade.get("external_order_id"):
        final_status = reconcile_recovered_trade(
            self.db_path, recovered_trade, self.live_order_client,
        )

    self._window_traded = True
    self._pending_settlements = [
        p for p in self._pending_settlements
        if p["trade_id"] != recovered_trade["id"]
    ]

    if final_status == "open":
        self.stats["execution_status"] = "recovery"
        self._open_trade = {...}  # as today
        self.stats["position"] = self._format_position(self._open_trade)
    else:
        # rejected on CLOB recon — window consumed, no position to resume
        self.stats["execution_status"] = "rejected-on-recovery"
        self._open_trade = None
```

### 4. PolymarketClient safety

`get_order_status` should return `{}` when called with `order_id="DRY-RUN"`
or when `self._dry_run` is true (mirror the pattern in
`get_settlement_info`). The helper treats `{}` as "unknown status" and
keeps current state.

### 5. Tests

`tests/test_executor.py` — one test per branch (issue acceptance):

- `matched` + local `reserved` → DB row becomes `open`, helper returns `"open"`.
- `canceled` + local `reserved` → DB row becomes `rejected`, helper returns `"rejected"`.
- `unmatched` + local `reserved` → DB row becomes `rejected`, helper returns `"rejected"`.
- Missing `external_order_id` → no CLOB call (assert via Mock), returns current status.
- `get_order_status` raises → no DB write, returns current status.

`tests/test_bot.py` — two tests extending the live-recovery coverage:

- CLOB returns `MATCHED` → `_open_trade` populated, `execution_status="recovery"`, `execute_live_trade` not called.
- CLOB returns `CANCELED` → `_open_trade is None`, `_window_traded is True`, `execution_status="rejected-on-recovery"`, `execute_live_trade` not called.

## Acceptance (from issue)

- [x] Recover path queries CLOB when `external_order_id` is present.
- [x] Reconciles `filled` → local `open`; `cancelled`/`unmatched` → local `rejected`.
- [x] Unit test covering each reconciliation branch.
