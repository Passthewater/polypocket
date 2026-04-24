# Data Capture Expansion — Design

**Date:** 2026-04-24
**Status:** draft
**Related:** `2026-04-23-logistic-p-up-model-design.md` (motivating consumer)

## Motivation

The logistic-p-up model plan admits one structural weakness: the labeled corpus is 404 rows because those are only the windows v1's gate fired on. Everything downstream — train/test splits, calibration curves, holdout evaluation — inherits that selection bias. Features are also lossy summaries (`displacement`, `sigma_5min`) with no raw path behind them, and the gate config at decision time isn't persisted so regime-conditional analysis is impossible.

This plan closes five capture gaps in the live and paper ledgers. Each is cheap at the decision rate (≤ one row per 5-min window, plus occasional mid-window writes). The five together multiply the training corpus ~5–10× and unlock feature classes the current schema can't express.

## Goals

**G1. Label every window, not just the ones we traded.** A `close` snapshot must be emitted at each 5-min boundary with `final_price` and a BTC-derived `outcome` regardless of whether the gate fired.

**G2. Persist the gate config with every decision.** One JSON blob of the active thresholds and sizing constants so future regime analysis is trivial.

**G3. Persist the raw BTC path per decision.** ~1 Hz tick series covering `[window_start, decision_time]` so downstream feature engineering (momentum, drawdown-from-high, microstructure) is unconstrained by the summaries chosen today.

**G4. Normalize order lifecycle telemetry.** Submit, ack, fill, reject events in their own table with per-event timestamps and the book state at each event — foundation for a realistic execution/slippage model.

**G5. Capture mid-window book trajectory.** Every ~30 s write a book snapshot so book-depth features see the path, not just the decision-time point.

Non-goals (deferred):
- Cross-check of BTC-derived outcome vs. Polymarket resolution. G1 uses `price_to_beat` as the baseline for local label derivation; Polymarket-reported resolution remains a separate backfill concern if ever needed.
- Fills-level reconciliation across orders and on-chain matches beyond what `executor.reconcile_recovered_trade` already does. G4 captures the event stream, not a new reconciliation loop.

## Key Decisions

**D1. Derive the outcome label from BTC, not from Polymarket resolution.**
`outcome = "up" if final_price > window.price_to_beat else "down"`. Baseline is `eventMetadata.priceToBeat` which is fixed at window open, so the label is a deterministic function of the BTC close price the bot already records. No `/events` race, no `fetch_resolution` dependency in the close-emission path. This is what makes G1 genuinely cheap. (Polymarket-reported resolution can be reconciled in a later backfill script if the user wants an audit; it is not required for training.)

**D2. Single-writer rule: settle paths stop touching the close row.**
Currently `_settle_trade` and `_poll_pending_settlements` write `snapshot_type='close'` only for traded windows. A transition-time emitter that also writes `'close'` creates a two-writer race on the `UNIQUE(window_slug, snapshot_type)` row.

Chosen rule:
- The **window-transition emitter in `bot.py` is the sole writer** of the close row.
- The close row's `outcome` column is **BTC-derived only** (see D2b). Polymarket-reported resolution lives on `trades.outcome` as today; no column is added to `window_snapshots` for it in this plan.
- `_settle_trade` and `_poll_pending_settlements` **remove** their `log_snapshot(snapshot_type='close', ...)` calls entirely. Trade PnL/outcome continues to land on `trades` via `update_trade`; that path is unchanged.

The transition emitter writes the close row **unconditionally** at each window transition (see D2c) so no later UPSERT is needed.

**D2b. Tie and missing-data handling for the BTC-derived outcome.**
- `outcome = "up"` if `final_price > prev_ptb`
- `outcome = "down"` if `final_price < prev_ptb`
- `outcome = None` on exact equality (a tie — extremely rare at float precision but possible on frozen-price edge cases) or when either price is missing. Downstream consumers filter on `outcome IS NOT NULL` when labeling; tie windows are unlabeled rather than arbitrarily assigned.

Polymarket's published resolution on exact ties is not contractually specified in our knowledge; rather than guess, we leave it null and document the join pattern.

**D2c. Write the close row unconditionally.**
If `binance.price_at(window.end_time)` returns None (hires buffer didn't cover end_time — bot restart mid-window, feed reconnect across the boundary, deep stall), the row is still written with `final_price = NULL` and `outcome = NULL`. This preserves the "every window gets a close row" invariant that Phase 1 exists to establish; a null `final_price` is a legitimate signal to the consumer that the bot couldn't observe the close, which is more useful than a missing row that looks identical to "window never existed."

**D3. No new env-backed config constants.**
Every addition below is a column add, helper function, or new table. `tests/conftest.py`'s `_key` tuple does not need to change.

**D4. Reuse `window_snapshots` for G1–G3; add a new table for G4 (order events) and G5 (book samples).**
G1–G3 are per-window scalars/blobs that fit `window_snapshots`. G4 is per-order-event (N per window, typed) and G5 is per-sample (many per window, timestamped) — neither fits the `(window_slug, snapshot_type)` unique shape.

**D5. Write identically to paper and live DBs.**
All capture paths run in `bot.py` / `executor.py` before the mode split. Paper and live ledgers get the same columns and the same per-window rows. Only the `trades` table rows differ by mode.

## Schema Changes

### G1, G2, G3 — add columns to `window_snapshots`

Idempotent `ALTER TABLE ... ADD COLUMN` in `ledger.init_db` (same pattern already used for `up_bids_json`/`down_bids_json`):

| Column | Type | Meaning |
|---|---|---|
| `final_price` | REAL | already exists; now populated for every window (G1) |
| `outcome` | TEXT | already exists; now populated for every window via BTC-derived label (G1) |
| `gate_config_json` | TEXT | JSON blob of active config at decision time (G2) |
| `btc_path_json` | TEXT | JSON array of `[ts, price]` pairs covering window start → decision time (G3) |

No new primary-key or index changes. The existing indexes on `window_slug`, `snapshot_type`, and `timestamp` remain sufficient.

### G4 — new table `order_events`

```sql
CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL,
    window_slug TEXT NOT NULL,                 -- denormalized for fast analysis
    event_type TEXT NOT NULL,                  -- see enumeration below
    event_ts_wall REAL NOT NULL,               -- time.time() (wall clock, seconds)
    external_order_id TEXT,
    payload_json TEXT NOT NULL,                -- event-type-specific fields (see below)
    FOREIGN KEY (trade_id) REFERENCES trades(id)
)
CREATE INDEX idx_order_events_trade ON order_events(trade_id);
CREATE INDEX idx_order_events_window ON order_events(window_slug);
```

**Clock basis.** `event_ts_wall` is wall-clock seconds (`time.time()`) — chosen so it's comparable across bot restarts and correlatable with `trades.timestamp`. Payload fields derived from `time.monotonic()` (e.g., `book_age_s_monotonic`) carry the `_monotonic` suffix to prevent downstream consumers from diffing the two. Never mix in the same comparison.

**Foreign-key enforcement.** SQLite does not enforce `FOREIGN KEY` clauses unless `PRAGMA foreign_keys=ON` is set per connection. `ledger.init_db` does not currently set this pragma and this plan does not change that — the FK clause is declared for schema-documentation value and to keep the option open for future enforcement, but orphan rows are an accepted possibility if `trades` rows are ever deleted. In practice `trades` is append-only today, so the risk is theoretical.

**`event_type` enumeration:**
- `submit` — pre-client-call, one per live trade (and, if Q2 accepts default, per FOK path if it's ever used)
- `ack` — immediately after client-call return, one per live trade
- `fill` — one per filled live trade; also one per paper trade (see "Write sites" below)
- `reject` — one per rejected live trade
- `reconcile_matched` — recovery promoted `reserved` → `open` based on CLOB `status=matched`
- `reconcile_canceled` — recovery demoted `reserved` → `rejected` based on CLOB `status=canceled/cancelled/unmatched`
- `reconcile_unknown` — recovery hit an unexpected CLOB status; local status unchanged, event is the audit trail
- `stranded_fill_promote` — stranded-fill sweep promoted `rejected` → `open` based on `/trades` match

**`payload_json` per event type** (field suffixes denote clock basis; all sizes/prices are floats):
- `submit`: `{side, intended_size, entry_price, limit_price, book_age_s_monotonic}` — the book itself is **not** re-serialized here; it was captured in the concurrent `decision` snapshot and (if Phase 5 shipped) in `window_book_samples`. Joins go via `window_slug` + nearest `event_ts_wall`.
- `ack`: `{status, error}` — `external_order_id` is its own column on the event row
- `fill`: `{filled_size, avg_price}`
- `reject`: `{error, clob_status}`
- `reconcile_matched`: `{from_status, clob_status}`
- `reconcile_canceled`: `{from_status, clob_status}`
- `reconcile_unknown`: `{from_status, clob_status_raw}` — raw string for post-mortem
- `stranded_fill_promote`: `{from_status, shares_held, cost_usdc, avg_price}`

**Write sites:**
- `executor.execute_live_trade`: `submit` before client call, `ack` immediately after, then `fill` or `reject` based on `fill.status`.
- `executor.execute_paper_trade`: one `fill` event per successfully-logged trade so paper and live ledgers carry a uniform lifecycle record. The `insufficient-balance` early return happens before `log_trade` — no `trade_id` exists, no event is written. This asymmetry is accepted: a balance-refusal is a *pre-trade* condition, not an order lifecycle event.
- `executor.reconcile_recovered_trade`: one event per branch reached — `reconcile_matched`, `reconcile_canceled`, `reconcile_unknown`, or `stranded_fill_promote` — so every recovery path leaves an audit trail.

### G5 — new table `window_book_samples`

```sql
CREATE TABLE IF NOT EXISTS window_book_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_slug TEXT NOT NULL,
    sampled_at REAL NOT NULL,                  -- time.time()
    up_bids_json TEXT, up_asks_json TEXT,
    down_bids_json TEXT, down_asks_json TEXT,
    btc_price REAL
)
CREATE INDEX idx_book_samples_window ON window_book_samples(window_slug);
CREATE INDEX idx_book_samples_ts ON window_book_samples(sampled_at);
```

Written by `bot._on_book_update` when `now - last_sample_ts >= 30.0`, only for the window currently in its live slot.

## Storage Budget

- G1: 1 extra row per non-traded window. At ~90 % skip rate that's ~260 additional `close` rows/day × ~200 B = 50 KB/day.
- G2: `gate_config_json` ~400 B × (288 decision + 288 close) rows/day = 230 KB/day.
- G3: `btc_path_json` ~6 KB × (288 decision + 288 close) rows/day = 3.5 MB/day. Close rows carry the full-window path (300 points); decision rows carry the partial path up to decision time. Both populated.
- G4: ~3 events × ~30 filled windows/day + ~1 event × ~260 non-filled windows (paper `fill` only fires on executed paper trades, so paper rate is similar to live) × ~300 B = ~100 KB/day.
- G5: ~9 samples × 288 windows × ~2 KB = 5 MB/day (sampling begins at +30 s after window open, so ~9 samples per 5-min window rather than 10).

Total ~9 MB/day per ledger; ~3.3 GB/year — still comfortable for SQLite. No pruning needed before the model-v2 retrain horizon.

## Scope Per Phase

Each phase is independently shippable and independently testable. Recommended commit boundary = one phase.

**Phase 1 (G1) — universal close snapshot.** Smallest, highest-leverage, ships today. Roughly 30 lines in `bot.py`, one helper in `ledger.py`, two or three tests.

**Phase 2 (G2) — gate config snapshot.** One column add, one helper in `config.py`, one line at each `log_snapshot` call site.

**Phase 3 (G3) — BTC path per decision.** One column add, one method on `BinanceFeed`, two call-site changes.

**Phase 4 (G4) — order lifecycle telemetry.** Largest phase: new table, new `ledger.log_order_event` helper, instrumentation across `execute_live_trade`, `execute_paper_trade`, and all four branches of `reconcile_recovered_trade`. The `LiveOrderClient` Protocol itself is **not** modified — timestamps are measured at the call site rather than surfaced through the client, so the Protocol contract is unchanged.

Project rule (`project-context.md` §framework-rules): *"anything touching the CLOB has a focused test in `test_polymarket_client.py`."* The instrumentation lives in `executor.py`, but the executor's wiring to the client changes shape. Phase 4 adds one smoke test to `test_polymarket_client.py` confirming the Protocol's method signatures are still structurally satisfied by `PolymarketClient`, on top of the new coverage in `test_executor.py`. This honors the rule's spirit even though the client module itself is not edited.

Kept in this plan rather than split into its own design+impl pair: Phase 4 shares context with Phase 5 (both introduce new tables under the same ledger init) and with Phase 3 (`event_ts_wall` joins to `window_book_samples.sampled_at`). Splitting would force cross-plan references for the schema of those neighboring tables, which is worse than one slightly-longer plan.

**Phase 5 (G5) — mid-window book samples.** New table, one counter in `bot._on_book_update`, one write site. Smaller than Phase 4.

## Gate Config Enumeration (for G2)

`polypocket.config.snapshot_gate_config()` returns a dict of the following module-level constants at call time (which means the TUI's runtime mutations are reflected):

- Signal gates: `MIN_EDGE_THRESHOLD`, `MIN_EDGE_THRESHOLD_DOWN`, `MAX_ENTRY_PRICE`, `MAX_EDGE_THRESHOLD_UP`, `MIN_MODEL_CONFIDENCE`, `MIN_MODEL_CONFIDENCE_UP`
- Calibration: `CALIBRATION_SHRINKAGE_UP`, `CALIBRATION_SHRINKAGE_DOWN`
- Execution: `SIGNAL_CUSHION_TICKS`, `IOC_BUFFER_TICKS`, `FOK_SLIPPAGE_TICKS`, `DEPTH_CLAMP_BUFFER`, `MIN_FILL_RATIO`, `MAX_BOOK_AGE_S`
- Timing: `WINDOW_ENTRY_MIN_ELAPSED`, `WINDOW_ENTRY_MIN_REMAINING`, `VOLATILITY_LOOKBACK`
- Sizing: `MIN_POSITION_USDC`, `MAX_POSITION_USDC`, `EDGE_FLOOR`, `EDGE_RANGE`, `VOL_FLOOR`, `VOL_RANGE`
- Meta: `FEE_RATE`, `TRADING_MODE`

Adding a new TUI-mutable constant later means one edit: append to the helper.

## Open Questions

**Q1. Should Phase 1 also backfill missing `close` rows for past windows from the existing `window_snapshots` + `BinanceFeed` snapshots?**
Recommendation: no backfill in this plan — past non-traded windows were never recorded (no `open` row either), so there's no place to attach a `close` row. The corpus starts growing from the day this ships; the existing 404 rows are what they are. Confirm or override.

**Q2. Phase 4: should we also record `submit_fok` events, or only `submit_ioc`?**
The live path is IOC-only today (`execute_live_trade` calls `submit_ioc`). `submit_fok` exists on the Protocol but isn't called from the bot path. Recommendation: instrument both in case FOK comes back, but the expected event stream in production is IOC-only. Confirm.

**Q3. Phase 5: is 30 s the right sampling interval, or should it be 15 s / 60 s?**
At 30 s we get 10 samples per window — enough to see book trajectory, cheap on storage. Recommendation: ship with `BOOK_SAMPLE_INTERVAL_S = 30.0` as a module-level constant (not env-backed — no conftest update needed), revisit if downstream features need higher resolution.

## Success Criteria

- **Phase 1 — row coverage:** 24 h after deploy, `COUNT(DISTINCT window_slug) WHERE snapshot_type='close'` equals 100 % of `COUNT(DISTINCT window_slug) WHERE snapshot_type='open'` (the transition emitter always writes, regardless of price availability).
- **Phase 1 — label coverage:** of those close rows, ≥ 95 % have a non-null `final_price` and non-null `outcome`. The ≤ 5 % gap is attributable to known failure modes:
  - Bot restart mid-window (hires buffer has no pre-boundary samples)
  - Binance WS reconnect across the window boundary (sample gap > 30 s)
  - BTC price exactly equal to `price_to_beat` at the end sample (tie → null outcome by design — expected to be rare)
  - A window with no `open` row because thin-book / single-sided quotes prevented the first valid tick; these are excluded from the denominator
- **Phase 2:** every `decision` and every `close` row written after deploy has a non-null `gate_config_json`.
- **Phase 3:** every `decision` row written after deploy has a non-null `btc_path_json` with ≥ 50 points (decisions fire no earlier than `WINDOW_ENTRY_MIN_ELAPSED = 60 s`, so 60 points is typical; 50 allows for 1 Hz buffer jitter and is a safer alarm threshold). Startup exception: the first decision per bot-process lifetime may have fewer points if the hires buffer hasn't warmed to 60 s yet — monitor by filtering `WHERE timestamp > <bot start + 60 s>`.
- **Phase 4:** for every live trade with `status ∈ {open, settled, rejected}`, at least one `submit` and one `ack` event exist in `order_events`; every `open`/`settled` trade has a `fill` event; every `rejected` trade has a `reject` event. Paper trades have exactly one `fill` event (the `insufficient-balance` pre-trade path is exempt and has no trade_id).
- **Phase 5:** each live-slot window of ≥ 120 s elapsed has ≥ 3 rows in `window_book_samples` (first sample at +30 s, next at +60 s, next at +90 s — three samples by t=120 s).

## Rollback

Each phase is column-additive (or new-table-additive) and gated behind idempotent `ALTER TABLE ... IF NOT EXISTS` patterns in `ledger.init_db`. Reverting a phase = revert the code commit; the columns / tables stay (harmless) and the next deploy stops populating them. No data migration needed.
