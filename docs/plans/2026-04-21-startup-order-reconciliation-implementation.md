# Implementation plan — Startup order-status reconciliation (issue #5)

Design: `docs/plans/2026-04-21-startup-order-reconciliation-design.md`.

Execute these steps in order. Commit after each step that ends with a `git commit` line. Run tests before each commit.

---

## Step 1: Add `get_order_status` to the `LiveOrderClient` Protocol

**File:** `polypocket/executor.py`

In the `LiveOrderClient` Protocol (around line 50-56), add a method:

```python
def get_order_status(self, order_id: str) -> dict: ...
```

Place it after `get_settlement_info`. No other changes in this step.

---

## Step 2: Implement `reconcile_recovered_trade` helper

**File:** `polypocket/executor.py`

Add this function near the top-level functions (after `_window_consumed_result` or near the other standalone helpers — pick a location matching existing code style):

```python
def reconcile_recovered_trade(
    db_path: str,
    trade: dict,
    client: LiveOrderClient | None,
) -> str:
    """Query CLOB for a recovered trade's order status and reconcile local DB.

    Called only in live mode during startup recovery. Returns the final local
    status: "open" (resume into _open_trade) or "rejected" (window consumed,
    no position to resume). On any uncertainty (no order id, no client,
    CLOB error, unknown status, resting order) returns the existing local
    status unchanged and writes nothing, preserving today's recovery
    behavior when CLOB evidence isn't available.
    """
    current_status = trade["status"]
    order_id = trade.get("external_order_id")
    if not order_id or client is None:
        return current_status

    try:
        resp = client.get_order_status(order_id)
    except Exception as exc:
        log.warning(
            "reconcile: get_order_status failed for trade %s order %s: %s",
            trade["id"], order_id, exc,
        )
        return current_status

    if not resp:
        return current_status

    clob_status = str(resp.get("status", "")).strip().lower()

    if clob_status == "matched":
        if current_status != "open":
            update_trade(db_path, trade["id"], outcome=None, pnl=None, status="open")
        return "open"

    if clob_status in {"canceled", "cancelled", "unmatched"}:
        update_trade(db_path, trade["id"], outcome=None, pnl=None, status="rejected")
        return "rejected"

    log.warning(
        "reconcile: unexpected CLOB status %r for trade %s order %s; keeping local %r",
        clob_status, trade["id"], order_id, current_status,
    )
    return current_status
```

Notes:
- `update_trade` and `log` are already imported/defined at module scope.
- Keep the signature exactly as specified — tests will call it directly.

---

## Step 3: Make `PolymarketClient.get_order_status` dry-run safe

**File:** `polypocket/clients/polymarket.py`

Modify `get_order_status` (currently lines 150-151) to return `{}` when dry-run or for `DRY-RUN` order IDs, mirroring `get_settlement_info` (lines 153-163):

```python
def get_order_status(self, order_id: str) -> dict:
    if self._dry_run or order_id == "DRY-RUN":
        return {}
    return self._client.get_order(order_id)
```

---

## Step 4: Wire reconciliation into the bot's recovery path

**File:** `polypocket/bot.py`

At the top of the file, add `reconcile_recovered_trade` to the executor import block (currently lines 23-30):

```python
from polypocket.executor import (
    LiveOrderClient,
    TradeResult,
    execute_live_trade,
    execute_paper_trade,
    reconcile_recovered_trade,
    settle_live_trade,
    settle_paper_trade,
)
```

Replace the recovery block (current lines 158-179) with:

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
    # Remove from pending list to avoid double settlement
    self._pending_settlements = [
        p for p in self._pending_settlements
        if p["trade_id"] != recovered_trade["id"]
    ]

    if final_status == "open":
        self.stats["execution_status"] = "recovery"
        self._open_trade = {
            "trade_id": recovered_trade["id"],
            "side": recovered_trade["side"],
            "entry_price": recovered_trade["entry_price"],
            "size": recovered_trade["size"],
            "mode": TRADING_MODE,
            "status": "open",
            "external_order_id": recovered_trade.get("external_order_id"),
        }
        self.stats["position"] = self._format_position(self._open_trade)
    else:
        # CLOB says the order never matched — window consumed, no position.
        self.stats["execution_status"] = "rejected-on-recovery"
        self._open_trade = None
```

Keep the existing indentation from the surrounding `_on_book_update` / window-transition block. Preserve the trailing blank line before the `if window.price_to_beat is not None:` check.

---

## Step 5: Unit tests for the helper

**File:** `tests/test_executor.py`

Add a new test class or a group of functions (`test_reconcile_*`). Five tests, one per branch from the design doc. Use an in-memory or tmp_path sqlite DB via `init_db` + `log_trade`, then call `reconcile_recovered_trade` with a `Mock()` client, and assert:

1. `test_reconcile_matched_marks_trade_open`
   - Seed a `reserved` trade with `external_order_id="0xabc"`.
   - `client.get_order_status.return_value = {"status": "MATCHED"}`.
   - Assert return value is `"open"`.
   - Assert DB row status is now `"open"`.

2. `test_reconcile_canceled_marks_trade_rejected`
   - Seed a `reserved` trade.
   - `client.get_order_status.return_value = {"status": "CANCELED"}`.
   - Assert return is `"rejected"`, DB row is `"rejected"`.

3. `test_reconcile_unmatched_marks_trade_rejected`
   - Same shape; CLOB status `"UNMATCHED"` → `"rejected"`.

4. `test_reconcile_without_external_order_id_is_noop`
   - Seed a `reserved` trade with `external_order_id=None`.
   - Assert return equals the input status (`"reserved"`).
   - Assert `client.get_order_status.assert_not_called()`.
   - Assert DB row unchanged.

5. `test_reconcile_clob_error_preserves_local_status`
   - `client.get_order_status.side_effect = Exception("boom")`.
   - Assert return equals input status.
   - Assert DB row unchanged.

Use existing fixtures and helpers from `tests/test_executor.py` where possible. Look at how `log_trade` is called elsewhere in that file and mirror it.

---

## Step 6: Bot integration tests

**File:** `tests/test_bot.py`

Extend the existing live-recovery coverage. Locate `test_bot_live_mode_recovers_reserved_trade_and_prevents_reentry` (starts ~line 298) and use it as a template.

Add two new tests:

**A. `test_bot_live_recovery_reconciles_matched_to_open`**
- Seed a `reserved` trade with `external_order_id="0xabc"`.
- Provide a `live_order_client = Mock()` where `get_order_status.return_value = {"status": "MATCHED"}`.
- Run the same `_on_book_update` setup as the existing test.
- Assert: `bot._open_trade["trade_id"] == trade_id`, `bot._window_traded is True`, `bot.stats["execution_status"] == "recovery"`, `execute_live_trade` not called.
- Assert DB row status is now `"open"`.

**B. `test_bot_live_recovery_reconciles_canceled_to_rejected`**
- Same setup but `get_order_status.return_value = {"status": "CANCELED"}`.
- Assert: `bot._open_trade is None`, `bot._window_traded is True`, `bot.stats["execution_status"] == "rejected-on-recovery"`, `execute_live_trade` not called.
- Assert DB row status is now `"rejected"`.

The existing test (no `external_order_id`) should keep working unchanged — verify by running the full file.

---

## Step 7: Verify

```bash
python -m pytest tests/test_executor.py tests/test_bot.py -v
python -m pytest tests/ -q
```

Expected: all tests pass. No new warnings from the reconciliation helper.

---

## Step 8: Commit

```bash
git add polypocket/executor.py polypocket/bot.py polypocket/clients/polymarket.py tests/test_executor.py tests/test_bot.py
git commit -m "feat(live): reconcile recovered trades against CLOB on startup (#5)"
```

---

## Out of scope

- Integration test matrix (follow-up D / issue #7).
- Any change to `execute_live_trade` or the happy-path submission flow.
- Any change to `settle_live_trade`.
