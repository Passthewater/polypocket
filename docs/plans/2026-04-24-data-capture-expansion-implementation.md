# Data Capture Expansion — Implementation Plan

> **For Claude:** Execute in-chat, one task at a time, linearly. No parallel subagents. Each task has file paths, concrete commands, and a verification + rollback line.

**Goal:** Land five independent data-capture improvements to `polypocket`'s paper and live SQLite ledgers. Each phase ships as its own commit/PR.

**Design doc:** `docs/plans/2026-04-24-data-capture-expansion-design.md`.

**Related:** `2026-04-23-logistic-p-up-model-design.md` (the consumer that motivates these captures).

---

## Pre-decisions (user, before Task 1)

Three open questions from the design doc need a yes/no before implementation starts:

- **Q1 (Phase 1 backfill of past windows).** Default: **no backfill** — past non-traded windows have no `open` row to attach a `close` to. Corpus grows forward from deploy day. Override only if the user wants a separate backfill script.
- **Q2 (Phase 4 FOK instrumentation).** Default: **instrument both `submit_ioc` and `submit_fok` equally**, even though the live path only calls IOC today.
- **Q3 (Phase 5 sample interval).** Default: **30 s**, module-level constant, not env-backed. Revisit if downstream features need higher resolution.

If the user is silent on these, proceed with the defaults.

---

# Phase 1 — Universal window-close snapshot (G1)

**Why first:** single highest-leverage change; breaks the selection bias that undermines every future model fit. Roughly 30 lines of code.

**Per design D2 / D2c:** the transition emitter is the **sole** writer of the close row. Settle paths stop writing close rows entirely. The close row's `outcome` column is BTC-derived only (null on tie, null on missing price). No update helper is introduced — the first design draft included one, but with BTC-outcome-as-source-of-truth it would be a no-op and is omitted.

## Task 1.1: Emit close snapshot on every window transition

**Files:**
- Modify: `polypocket/bot.py`
- Modify: `tests/test_bot.py`

### Step 1: Find the transition block

In `bot.py` `_on_book_update`, the transition block begins at the comment `# Flush previous window's skip decision snapshot before settling/resetting`. The decision-flush currently lives there. We add a close-flush next to it.

### Step 2: Add the close-snapshot emitter (unconditional)

Immediately **after** the existing `if not self._window_traded and self._best_edge_snapshot is not None:` block (inside the `if self._current_window is not None:` guard), add:

```python
# G1: write a close snapshot for every expiring window, regardless of trade.
# Written unconditionally — if the hires buffer didn't reach end_time, the
# row still lands with NULL final_price / outcome (design D2c). That NULL
# signal is more useful to consumers than a missing row, which would look
# identical to "window never existed."
prev_window = self._current_window
prev_slug = prev_window.slug
prev_ptb = prev_window.price_to_beat
final_btc = self.binance.price_at(prev_window.end_time)
btc_outcome: str | None = None
if final_btc is not None and prev_ptb is not None:
    if final_btc > prev_ptb:
        btc_outcome = "up"
    elif final_btc < prev_ptb:
        btc_outcome = "down"
    # Exact equality → btc_outcome stays None (tie — design D2b).
log_snapshot(
    self.db_path,
    window_slug=prev_slug,
    snapshot_type="close",
    stats={
        "btc_price": final_btc,
        "window_open_price": prev_ptb,
    },
    final_price=final_btc,
    outcome=btc_outcome,
)
```

`log_snapshot` already accepts `outcome` and `final_price` as keyword parameters (both default None) — verified at `polypocket/ledger.py` line 349. No ledger signature change needed for Phase 1.

### Step 3: Remove the close-row writes from settle paths

In `bot.py` `_settle_trade`, delete the `log_snapshot(self.db_path, ..., snapshot_type="close", ..., trade_fired=True, outcome=outcome)` call entirely (the entire `log_snapshot(...)` statement and its arguments). The transition emitter in Step 2 now owns the close row. Trade PnL/outcome continues to land on the `trades` table via `update_trade` — that path is untouched.

Same deletion inside `_poll_pending_settlements`.

Rationale (design D2): with the close row's `outcome` derived from BTC at transition time, there is no remaining reason for settle to write to `window_snapshots`. Trade PnL and PM-resolved outcome remain on the `trades` row where they already live.

### Step 4: Tests

Add to `tests/test_bot.py`. The existing test module already stubs `BinanceFeed` — reuse the stub and extend it with enough hires-buffer state to answer `price_at(end_time)`.

Required cases:

1. **Happy path, non-traded window.** Two successive windows; first ends with no trade. Assert one `close` row exists for the first window with `final_price` populated and `outcome in {"up", "down"}`.
2. **Happy path, traded window.** First window fires a trade; second arrives. Assert one `close` row exists for the first window from the transition emitter (not from settle). Assert settle did not write a second `close` row (check by counting rows with `snapshot_type='close' AND window_slug=...` = 1).
3. **Hires buffer empty (`price_at` returns None).** Drive a transition when the stub's hires buffer has no samples covering `prev_window.end_time`. Assert the `close` row exists with `final_price IS NULL` and `outcome IS NULL`.
4. **Tie case.** Stub the buffer so `price_at(end_time)` returns exactly `prev_window.price_to_beat`. Assert the row exists with `final_price` populated but `outcome IS NULL`.
5. **Settle no longer writes close.** After a successful paper trade settles, query `SELECT COUNT(*) FROM window_snapshots WHERE snapshot_type='close' AND window_slug=?` and assert the only row is the transition-emitted one (count=1, not 2).

Run: `pytest tests/test_bot.py tests/test_ledger.py -x`. Expected: all pass.

### Step 5: Commit

```bash
git add polypocket/bot.py tests/test_bot.py
git commit -m "feat(bot): emit close snapshot for every window, BTC-derived label"
```

**Rollback:** `git revert HEAD`. Orphan close rows in deployed DBs are harmless.

---

# Phase 2 — Gate config snapshot per decision (G2)

## Task 2.1: Helper — `snapshot_gate_config()`

**Files:**
- Modify: `polypocket/config.py`
- Modify: `tests/test_config.py` (create if missing)

### Step 1: Append helper to `config.py`

At the end of the file:

```python
def snapshot_gate_config() -> dict:
    """Return a plain-dict snapshot of TUI-mutable gate/sizing constants.

    Read at call time so TUI keybind mutations are reflected. Serialize with
    json.dumps at the call site. When adding a new tunable constant above,
    append its name here in the same commit.
    """
    return {
        "MIN_EDGE_THRESHOLD": MIN_EDGE_THRESHOLD,
        "MIN_EDGE_THRESHOLD_DOWN": MIN_EDGE_THRESHOLD_DOWN,
        "MAX_ENTRY_PRICE": MAX_ENTRY_PRICE,
        "MAX_EDGE_THRESHOLD_UP": MAX_EDGE_THRESHOLD_UP,
        "MIN_MODEL_CONFIDENCE": MIN_MODEL_CONFIDENCE,
        "MIN_MODEL_CONFIDENCE_UP": MIN_MODEL_CONFIDENCE_UP,
        "CALIBRATION_SHRINKAGE_UP": CALIBRATION_SHRINKAGE_UP,
        "CALIBRATION_SHRINKAGE_DOWN": CALIBRATION_SHRINKAGE_DOWN,
        "SIGNAL_CUSHION_TICKS": SIGNAL_CUSHION_TICKS,
        "IOC_BUFFER_TICKS": IOC_BUFFER_TICKS,
        "FOK_SLIPPAGE_TICKS": FOK_SLIPPAGE_TICKS,
        "DEPTH_CLAMP_BUFFER": DEPTH_CLAMP_BUFFER,
        "MIN_FILL_RATIO": MIN_FILL_RATIO,
        "MAX_BOOK_AGE_S": MAX_BOOK_AGE_S,
        "WINDOW_ENTRY_MIN_ELAPSED": WINDOW_ENTRY_MIN_ELAPSED,
        "WINDOW_ENTRY_MIN_REMAINING": WINDOW_ENTRY_MIN_REMAINING,
        "VOLATILITY_LOOKBACK": VOLATILITY_LOOKBACK,
        "MIN_POSITION_USDC": MIN_POSITION_USDC,
        "MAX_POSITION_USDC": MAX_POSITION_USDC,
        "EDGE_FLOOR": EDGE_FLOOR,
        "EDGE_RANGE": EDGE_RANGE,
        "VOL_FLOOR": VOL_FLOOR,
        "VOL_RANGE": VOL_RANGE,
        "FEE_RATE": FEE_RATE,
        "TRADING_MODE": TRADING_MODE,
    }
```

### Step 2: Test

Create `tests/test_config.py` (or append if present):

```python
def test_snapshot_gate_config_contains_all_named_constants():
    from polypocket.config import snapshot_gate_config
    snap = snapshot_gate_config()
    for key in (
        "MIN_EDGE_THRESHOLD", "MAX_ENTRY_PRICE", "MAX_EDGE_THRESHOLD_UP",
        "IOC_BUFFER_TICKS", "FOK_SLIPPAGE_TICKS", "CALIBRATION_SHRINKAGE_UP",
        "WINDOW_ENTRY_MIN_ELAPSED", "TRADING_MODE",
    ):
        assert key in snap
```

Run: `pytest tests/test_config.py -x`. Expected: pass.

---

## Task 2.2: Add `gate_config_json` column + wire into `log_snapshot`

**Files:**
- Modify: `polypocket/ledger.py`
- Modify: `polypocket/bot.py`
- Modify: `tests/test_ledger.py`

### Step 1: Idempotent ALTER in `init_db`

In `ledger.init_db`, next to the existing idempotent column adds for `up_bids_json` / `down_bids_json`, append:

```python
if "gate_config_json" not in snap_cols:
    conn.execute("ALTER TABLE window_snapshots ADD COLUMN gate_config_json TEXT")
```

### Step 2: Extend `log_snapshot` signature + INSERT

Add parameter `gate_config: dict | None = None` to `log_snapshot`. Inside the function:

```python
gate_config_json = None if gate_config is None else json.dumps(gate_config, sort_keys=True)
```

Add `gate_config_json` to the INSERT's column list and values. Keep the new parameter keyword-only if the codebase style prefers — there are no positional callers today (all callers use kwargs per `bot.py`).

### Step 3: Pass from call sites (all four snapshot types)

In `bot.py`, add `from polypocket.config import snapshot_gate_config` to the imports block. Then add `gate_config=snapshot_gate_config(),` to **every** `log_snapshot(...)` call site:

- `snapshot_type="open"` — in the open-emitter block inside `_on_book_update`
- `snapshot_type="decision"` — both sites: the trade-fire site (around the `log_snapshot(..., trade_fired=True, ...)` call) and the skip-flush site inside the transition block
- `snapshot_type="close"` — the Phase 1 transition emitter (added in Phase 1 Task 1.1 Step 2)

Per design success criteria, every `decision` and every `close` row must have `gate_config_json` populated. Open rows carry it too for consistency; it's cheap.

**Cross-phase note:** Phase 1 landed first, so its close emitter was added without `gate_config=`. This task adds the keyword argument to that site. After this commit, new close rows carry the JSON; close rows written between the Phase 1 deploy and this deploy will have `gate_config_json IS NULL` (acceptable — they still have the schema column, just unpopulated).

### Step 4: Test

Add to `tests/test_ledger.py`:

```python
def test_log_snapshot_persists_gate_config_json(tmp_path):
    import json
    db = str(tmp_path / "t.db")
    init_db(db)
    log_snapshot(db, "slug-x", "decision",
                 stats={"btc_price": 100.0},
                 gate_config={"MIN_EDGE_THRESHOLD": 0.10, "MAX_ENTRY_PRICE": 0.70})
    rows = get_snapshots_for_window(db, "slug-x")
    assert rows[0]["gate_config_json"] is not None
    loaded = json.loads(rows[0]["gate_config_json"])
    assert loaded["MIN_EDGE_THRESHOLD"] == 0.10
```

Run: `pytest tests/test_ledger.py tests/test_bot.py tests/test_config.py -x`. Expected: all pass.

### Step 5: Commit

```bash
git add polypocket/config.py polypocket/ledger.py polypocket/bot.py tests/
git commit -m "feat(ledger): persist gate_config_json per decision snapshot"
```

**Rollback:** `git revert HEAD`. Column remains in existing DBs (harmless null values).

---

# Phase 3 — Raw BTC path per decision (G3)

## Task 3.1: `BinanceFeed.get_path`

**Files:**
- Modify: `polypocket/feeds/binance.py`
- Modify: `tests/test_binance.py` (create if missing)

### Step 1: Add the method

Append to `BinanceFeed`:

```python
def get_path(self, start_ts: float, end_ts: float) -> list[tuple[float, float]]:
    """Return 1-Hz `(ts, price)` samples from the hires buffer in [start, end]."""
    return [(ts, p) for ts, p in self._hires if start_ts <= ts <= end_ts]
```

### Step 2: Tests (happy path + empty buffer)

```python
def test_get_path_returns_samples_in_range():
    feed = BinanceFeed()
    for i in range(10):
        feed._on_trade({"price": 100.0 + i, "timestamp": (1000 + i) * 1000})
    path = feed.get_path(1002.0, 1005.0)
    assert len(path) == 4
    assert path[0][1] == 102.0


def test_get_path_returns_empty_when_buffer_cold():
    """Startup case: no ticks received yet — get_path must return []."""
    feed = BinanceFeed()
    assert feed.get_path(1000.0, 1060.0) == []


def test_get_path_returns_empty_when_range_disjoint():
    """Range entirely outside buffer window — return [] not a partial match."""
    feed = BinanceFeed()
    for i in range(5):
        feed._on_trade({"price": 100.0 + i, "timestamp": (1000 + i) * 1000})
    assert feed.get_path(2000.0, 2060.0) == []
```

Note: `_on_trade` gates buffer appends on `HIRES_INTERVAL_S = 1.0`, so the test's per-second timestamps land. Confirm by reading `_on_trade`.

Run: `pytest tests/test_binance.py -x`. Expected: pass.

---

## Task 3.2: Persist `btc_path_json`

**Files:**
- Modify: `polypocket/ledger.py`
- Modify: `polypocket/bot.py`
- Modify: `tests/test_ledger.py`

### Step 1: ALTER + `log_snapshot` column

Same pattern as Task 2.2 Step 1–2, for column `btc_path_json`. New `log_snapshot` parameter `btc_path: list[tuple[float, float]] | None = None`, serialized with `json.dumps`.

### Step 2: Pass from call sites

In `bot.py` decision call sites (both the trade-fire site and the skip-flush site):

```python
btc_path=self.binance.get_path(window.start_time, time.time()),
```

Also pass in the Phase 1 close emitter — the same site updated in Phase 2. Use the full window range:

```python
btc_path=self.binance.get_path(prev_window.start_time, prev_window.end_time),
```

Pass `btc_path=None` (or simply omit the kwarg — default is None) on the `open` site. At window open the path has at most one sample; not worth capturing.

**Cross-phase note:** close rows written between Phase 1 and Phase 3 deploy will have `btc_path_json IS NULL`. Decision rows written between Phase 2 and Phase 3 deploy have `gate_config_json` populated but `btc_path_json IS NULL`. Both are expected and acceptable for corpus filtering (use `WHERE btc_path_json IS NOT NULL` when the feature is required).

### Step 3: Test — serialization round-trip

```python
def test_log_snapshot_persists_btc_path_json(tmp_path):
    import json
    db = str(tmp_path / "t.db")
    init_db(db)
    log_snapshot(db, "slug-x", "decision",
                 stats={"btc_price": 100.0},
                 btc_path=[(1000.0, 100.0), (1001.0, 100.5)])
    rows = get_snapshots_for_window(db, "slug-x")
    path = json.loads(rows[0]["btc_path_json"])
    assert path == [[1000.0, 100.0], [1001.0, 100.5]]
```

### Step 4: Commit

```bash
git add polypocket/feeds/binance.py polypocket/ledger.py polypocket/bot.py tests/
git commit -m "feat(ledger): persist raw 1Hz BTC path per decision/close snapshot"
```

**Rollback:** `git revert HEAD`.

---

# Phase 4 — Order lifecycle telemetry (G4)

**Scope:** new `order_events` table, new `ledger.log_order_event` helper, instrumentation in `execute_live_trade` / `execute_paper_trade` / all four branches of `reconcile_recovered_trade`, plus a client-protocol smoke test in `test_polymarket_client.py`.

**Clock basis:** event rows use `event_ts_wall` (wall clock seconds, `time.time()`). Monotonic-derived payload fields carry the `_monotonic` suffix — never mix in the same comparison.

## Task 4.1: Table + `log_order_event` helper

**Files:**
- Modify: `polypocket/ledger.py`
- Modify: `tests/test_ledger.py`

### Step 1: Table DDL in `init_db`

```python
conn.execute(
    """
    CREATE TABLE IF NOT EXISTS order_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id INTEGER NOT NULL,
        window_slug TEXT NOT NULL,
        event_type TEXT NOT NULL,
        event_ts_wall REAL NOT NULL,
        external_order_id TEXT,
        payload_json TEXT NOT NULL,
        FOREIGN KEY (trade_id) REFERENCES trades(id)
    )
    """
)
conn.execute("CREATE INDEX IF NOT EXISTS idx_order_events_trade ON order_events(trade_id)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_order_events_window ON order_events(window_slug)")
```

**FK note:** SQLite does not enforce `FOREIGN KEY` without `PRAGMA foreign_keys=ON`. We declare the constraint for schema-documentation value but do not change pragma state. `trades` is append-only today, so orphan rows are a theoretical risk only.

### Step 2: Helper

```python
def log_order_event(
    db_path: str,
    trade_id: int,
    window_slug: str,
    event_type: str,
    event_ts_wall: float,
    payload: dict,
    external_order_id: str | None = None,
) -> None:
    import json
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO order_events (
                trade_id, window_slug, event_type, event_ts_wall,
                external_order_id, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (trade_id, window_slug, event_type, event_ts_wall,
             external_order_id, json.dumps(payload)),
        )
        conn.commit()
```

### Step 3: Tests

- `test_log_order_event_roundtrip` — insert a `submit`, read back via `SELECT`, assert fields match and `payload_json` deserializes correctly.
- `test_log_order_event_preserves_none_external_order_id` — the `submit` event carries `external_order_id IS NULL`; assert the column is null not empty-string.

---

## Task 4.2: Instrument `execute_live_trade`

**Files:**
- Modify: `polypocket/executor.py`
- Modify: `polypocket/bot.py` (live trade call site threads book-age)
- Modify: `tests/test_executor.py`

### Step 1: Thread book-age context into the executor

Per design D-G4: the book itself is **not** re-serialized into event payloads (it's already in `decision` snapshots and, post-Phase-5, in `window_book_samples`). Only `book_age_s_monotonic` travels with the submit event so consumers can judge book freshness at submit time.

Add parameter `submit_book_age_s_monotonic: float | None = None` to `execute_live_trade`. Caller in `bot.py` populates:

```python
submit_book_age_s_monotonic=(
    time.monotonic() - window.book_updated_at
    if window.book_updated_at is not None else None
),
```

### Step 2: Write `submit`, `ack`, `fill`/`reject` events

Inside `execute_live_trade`, after `log_trade` returns `trade_id`:

```python
submit_ts_wall = time.time()
log_order_event(
    db_path, trade_id, window_slug, "submit", submit_ts_wall,
    payload={
        "side": signal.side,
        "intended_size": size,
        "entry_price": entry_price,
        "limit_price": limit_price,
        "book_age_s_monotonic": submit_book_age_s_monotonic,
    },
)
fill = client.submit_ioc(...)
ack_ts_wall = time.time()
log_order_event(
    db_path, trade_id, window_slug, "ack", ack_ts_wall,
    payload={"status": fill.status, "error": fill.error},
    external_order_id=fill.order_id,
)
```

In the **filled** branch (after the `update_trade` that promotes status to `open`), write a `fill` event:

```python
log_order_event(
    db_path, trade_id, window_slug, "fill", time.time(),
    payload={"filled_size": fill.filled_size, "avg_price": fill.avg_price},
    external_order_id=fill.order_id,
)
```

In the **rejected / error** branch (after the `update_trade` that sets status to `rejected`), write a `reject` event:

```python
log_order_event(
    db_path, trade_id, window_slug, "reject", time.time(),
    payload={"error": fill.error, "clob_status": fill.status},
    external_order_id=fill.order_id,
)
```

### Step 3: Instrument `execute_paper_trade`

Paper path writes exactly one `fill` event per trade that successfully logs. Placement: **after** `log_trade` returns `trade_id` and **after** `deduct_paper_balance`, in all successful branches (both "outcome passed → immediate settle" and "outcome None → open"). For the immediate-settle case, the `fill` event records the entry, not the settlement (settlement lives on the `trades` row).

```python
log_order_event(
    db_path, trade_id, window_slug, "fill", time.time(),
    payload={"filled_size": size, "avg_price": entry_price},
)
```

The `insufficient-balance` branch returns before `log_trade`, so no `trade_id` exists and no event is written. This asymmetry is documented in the design: a balance-refusal is a *pre-trade* condition, not a lifecycle event.

Paper path does not write `submit`/`ack` events — there is no external client call to timestamp either side of. This intentional asymmetry between paper and live is noted in the payload schema; joining paper+live downstream filters `WHERE event_type='fill'` for a uniform lifecycle view.

### Step 4: Instrument all four branches of `reconcile_recovered_trade`

In `executor.reconcile_recovered_trade`:

- **Stranded-fill promote** (existing `if info.shares_held > 0` branch, after the promoting `update_trade`):
  ```python
  log_order_event(
      db_path, trade["id"], trade["window_slug"], "stranded_fill_promote",
      time.time(),
      payload={
          "from_status": "rejected",
          "shares_held": info.shares_held,
          "cost_usdc": info.cost_usdc,
          "avg_price": avg_price,
      },
      external_order_id=order_id,
  )
  ```
  Note: `trade["window_slug"]` — the dict returned from the DB already carries it.

- **Matched** (the `if clob_status == "matched":` branch, after any `update_trade`):
  ```python
  log_order_event(
      db_path, trade["id"], trade["window_slug"], "reconcile_matched",
      time.time(),
      payload={"from_status": current_status, "clob_status": clob_status},
      external_order_id=order_id,
  )
  ```

- **Canceled / cancelled / unmatched** (after the demoting `update_trade`):
  ```python
  log_order_event(
      db_path, trade["id"], trade["window_slug"], "reconcile_canceled",
      time.time(),
      payload={"from_status": current_status, "clob_status": clob_status},
      external_order_id=order_id,
  )
  ```

- **Unexpected status** (the final `log.warning` branch — local status unchanged):
  ```python
  log_order_event(
      db_path, trade["id"], trade["window_slug"], "reconcile_unknown",
      time.time(),
      payload={"from_status": current_status, "clob_status_raw": resp.get("status")},
      external_order_id=order_id,
  )
  ```

The early-return branches that skip reconciliation (no `order_id`, no `client`, CLOB call raised an exception) do **not** write events — they leave local state untouched and have no lifecycle transition to record. Documented as accepted gaps in the event stream.

### Step 5: Tests

Add to `tests/test_executor.py`:
- `test_execute_live_trade_writes_submit_ack_fill_events` — filled path → exactly 3 events in order `submit`, `ack`, `fill`; `event_ts_wall` strictly monotonic.
- `test_execute_live_trade_writes_submit_ack_reject_events` — rejected path → exactly 3 events, last one `reject` with `clob_status` in payload.
- `test_execute_live_trade_submit_payload_book_age_is_none_when_no_timestamp` — None-path coverage: caller passes `submit_book_age_s_monotonic=None`; assert the submit event's payload has `book_age_s_monotonic: null`.
- `test_execute_paper_trade_writes_fill_event_open_branch` — outcome=None path.
- `test_execute_paper_trade_writes_fill_event_immediate_settle_branch` — outcome="up" path; payload still reflects entry not settlement.
- `test_execute_paper_trade_insufficient_balance_writes_no_events` — early return; assert `SELECT COUNT(*) FROM order_events = 0`.
- `test_reconcile_writes_stranded_fill_promote_event` — existing stranded-fill stub extended.
- `test_reconcile_writes_matched_event` — stub `get_order_status` returning `{"status": "matched"}`.
- `test_reconcile_writes_canceled_event` — stub returning `{"status": "canceled"}`.
- `test_reconcile_writes_unknown_event_preserves_local_status` — stub returning `{"status": "weird-new-state"}`; assert event written AND local status unchanged.

Test the `LiveOrderClient` Protocol structurally (existing pattern in `test_executor.py`). No change to the Protocol itself.

### Step 6: Smoke test in `test_polymarket_client.py` (protocol drift guard)

Project rule requires a focused test in `test_polymarket_client.py` for any CLOB-adjacent change. Phase 4's changes sit in `executor.py`, but the executor's call signatures to `PolymarketClient` are the hot seam. Add:

```python
def test_polymarket_client_satisfies_live_order_client_protocol():
    """Guard against Protocol drift. If Phase 4 (or any future change)
    renames an argument on PolymarketClient without updating the Protocol
    or the executor's call sites, this test catches it at import time."""
    from polypocket.executor import LiveOrderClient
    from polypocket.clients.polymarket import PolymarketClient
    # isinstance against a Protocol checks structural conformance at runtime
    # (runtime_checkable not required — we check method presence by name).
    for method in ("submit_fok", "submit_ioc", "cancel_order",
                   "get_usdc_balance", "get_settlement_info", "get_order_status"):
        assert hasattr(PolymarketClient, method), f"PolymarketClient missing {method}"
```

This is cheap and self-contained; it doesn't hit the network or need the client's constructor.

Run: `pytest tests/test_executor.py tests/test_polymarket_client.py tests/test_ledger.py tests/test_bot.py -x`. Expected: all pass.

### Step 7: Commit

```bash
git add polypocket/executor.py polypocket/ledger.py polypocket/bot.py tests/
git commit -m "feat(executor): normalize order lifecycle into order_events table"
```

**Rollback:** `git revert HEAD`. Table remains (empty for reverted rows, harmless).

---

# Phase 5 — Mid-window book samples (G5)

## Task 5.1: Table + helper

**Files:**
- Modify: `polypocket/ledger.py`
- Modify: `tests/test_ledger.py`

### Step 1: Table DDL in `init_db`

```python
conn.execute(
    """
    CREATE TABLE IF NOT EXISTS window_book_samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        window_slug TEXT NOT NULL,
        sampled_at REAL NOT NULL,
        up_bids_json TEXT, up_asks_json TEXT,
        down_bids_json TEXT, down_asks_json TEXT,
        btc_price REAL
    )
    """
)
conn.execute("CREATE INDEX IF NOT EXISTS idx_book_samples_window ON window_book_samples(window_slug)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_book_samples_ts ON window_book_samples(sampled_at)")
```

### Step 2: Helper

```python
def log_book_sample(
    db_path: str,
    window_slug: str,
    sampled_at: float,
    up_bids, up_asks, down_bids, down_asks,
    btc_price: float | None,
) -> None:
    import json
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO window_book_samples (
                window_slug, sampled_at,
                up_bids_json, up_asks_json, down_bids_json, down_asks_json,
                btc_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (window_slug, sampled_at,
             json.dumps(up_bids), json.dumps(up_asks),
             json.dumps(down_bids), json.dumps(down_asks),
             btc_price),
        )
        conn.commit()
```

### Step 3: Test

`test_log_book_sample_roundtrip`: insert, SELECT, assert.

---

## Task 5.2: Sample every 30 s in `_on_book_update`

**Files:**
- Modify: `polypocket/bot.py`
- Modify: `tests/test_bot.py`

### Step 1: Add constant + state

In `bot.py` module-level:

```python
BOOK_SAMPLE_INTERVAL_S = 30.0
```

In `Bot.__init__`:

```python
self._last_book_sample_ts: float = 0.0
```

In the window-transition block, **set `_last_book_sample_ts = now`** (not 0.0). This defers the first mid-window sample to `window_open + 30 s`, which avoids duplicating the book state already captured in the `open` snapshot at `window_open + 0 s`. Result: 9 samples per 5-min window (at t=30, 60, 90, ..., 270 s), not 10 with the first one redundant.

### Step 2: Sampling call

Inside `_on_book_update`, only for the live-slot window (i.e., after the `if not (window.start_time <= now < window.end_time): return` gate), before the signal evaluation:

```python
if now - self._last_book_sample_ts >= BOOK_SAMPLE_INTERVAL_S:
    self._last_book_sample_ts = now
    log_book_sample(
        self.db_path,
        window_slug=window.slug,
        sampled_at=now,
        up_bids=window.up_bids, up_asks=window.up_book,
        down_bids=window.down_bids, down_asks=window.down_book,
        btc_price=self.binance.latest_price,
    )
```

### Step 3: Tests

- **Interval respected.** Drive `_on_book_update` twice with timestamps ≥ 30 s apart for the same window; assert two rows in `window_book_samples`. Drive twice within < 30 s; assert only one row.
- **No sample at t=0.** Drive the first book update of a new window (simulating `_last_book_sample_ts = now` set by the transition block). Assert no `window_book_samples` row is written on that tick (the open snapshot covers it).
- **Fresh window resets cadence.** Drive a sample late in window N (e.g., t=270 s), then transition to window N+1 and drive a book update 10 s into N+1. Assert no row for N+1 yet (cadence restarted from transition, not from absolute time).

Run: `pytest tests/test_bot.py tests/test_ledger.py -x`. Expected: pass.

### Step 4: Commit

```bash
git add polypocket/bot.py polypocket/ledger.py tests/
git commit -m "feat(bot): capture mid-window book samples every 30s"
```

**Rollback:** `git revert HEAD`. Table remains (harmless).

---

# Post-deployment verification (all phases)

After the first 4-hour soak on the paper ledger:

```bash
sqlite3 paper_trades.db <<'EOF'
-- Phase 1 row coverage: every window with an open should have a close
SELECT COUNT(DISTINCT window_slug) AS opens FROM window_snapshots WHERE snapshot_type='open';
SELECT COUNT(DISTINCT window_slug) AS closes FROM window_snapshots WHERE snapshot_type='close';
-- Phase 1 label coverage: close rows should mostly have final_price + outcome
SELECT
  SUM(CASE WHEN final_price IS NOT NULL THEN 1 ELSE 0 END) AS with_price,
  SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) AS with_outcome,
  COUNT(*) AS total
FROM window_snapshots WHERE snapshot_type='close';
-- Phase 2: gate config coverage on decision AND close rows
SELECT snapshot_type, SUM(CASE WHEN gate_config_json IS NULL THEN 1 ELSE 0 END) AS missing
FROM window_snapshots WHERE snapshot_type IN ('decision','close') GROUP BY snapshot_type;
-- Phase 3: path coverage on decision rows (≥50 points expected)
SELECT COUNT(*) AS missing FROM window_snapshots
WHERE snapshot_type='decision' AND btc_path_json IS NULL;
-- Phase 4: lifecycle events per trade (paper: fill only; live: submit,ack,fill|reject)
SELECT trade_id, GROUP_CONCAT(event_type, ',') FROM order_events GROUP BY trade_id LIMIT 10;
-- Phase 5: book samples per window (≥3 expected for live-slot windows ≥120s)
SELECT window_slug, COUNT(*) FROM window_book_samples GROUP BY window_slug LIMIT 10;
EOF
```

Success thresholds per design doc's "Success Criteria" section.

---

# Deferred / out-of-scope

- Backfill of past non-traded windows (design doc Q1).
- Polymarket-reported resolution captured alongside BTC-derived outcome (would need a separate `pm_outcome` column + a periodic poll).
- Pruning / archival of old `window_book_samples` rows (not needed before the v2 retrain horizon).
