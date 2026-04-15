# Finetuning Data Capture Design

**Date:** 2026-04-15  
**Goal:** Store structured data in the existing SQLite DB to support future model calibration, trade selection analysis, and execution tuning.

## Decision Summary

- Log **every window**, not just traded ones
- Capture **3 snapshots per window**: open, decision, close
- Include **order book depth** (top 3 levels per side)
- Store in a **new `window_snapshots` table** in the existing `paper_trades.db`
- **No changes** to the existing `trades` table

## Schema

```sql
CREATE TABLE window_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    window_slug TEXT NOT NULL,
    snapshot_type TEXT NOT NULL,        -- 'open', 'decision', 'close'

    -- BTC state
    btc_price REAL,
    window_open_price REAL,            -- price_to_beat
    ptb_provisional INTEGER,           -- 1 if using Binance estimate, 0 if Chainlink confirmed
    displacement REAL,

    -- Model state
    sigma_5min REAL,
    model_p_up REAL,
    t_remaining REAL,

    -- Market state
    up_ask REAL,
    down_ask REAL,
    market_p_up REAL,
    edge REAL,
    preview_side TEXT,                 -- 'up', 'down', or null
    quote_status TEXT,                 -- 'valid', 'missing-side', etc.

    -- Order book depth (top 3 levels, each side)
    up_book_json TEXT,                 -- JSON: [{"price": 0.55, "size": 120}, ...]
    down_book_json TEXT,               -- JSON: [{"price": 0.45, "size": 80}, ...]

    -- Decision context (populated on 'decision' and 'close' snapshots)
    trade_fired INTEGER,               -- 1 if trade was placed this window, 0 if skipped
    skip_reason TEXT,                  -- 'no-edge', 'risk-blocked', 'timing-early', 'timing-late', 'missing-quote', 'consumed'

    -- Resolution (populated on 'close' snapshot only)
    outcome TEXT,                      -- 'up' or 'down'
    final_price REAL,                  -- Chainlink final price

    UNIQUE(window_slug, snapshot_type)
);

CREATE INDEX idx_snapshots_window ON window_snapshots(window_slug);
CREATE INDEX idx_snapshots_type ON window_snapshots(snapshot_type);
CREATE INDEX idx_snapshots_timestamp ON window_snapshots(timestamp DESC);
```

## Snapshot Capture Points

### 1. Open — new window detected

Triggered when `window_slug` changes in `bot.py`. Written after the first book update with valid prices so we have ask data. `model_p_up` may be null if volatility hasn't computed yet.

### 2. Decision — bot acts or best opportunity passes

Two sub-cases:

- **Trade fires:** snapshot taken at the moment `signal_engine.evaluate()` returns a signal and execution begins. Captures exact state that triggered the trade.
- **No trade:** snapshot taken at the moment of **peak edge** during the window. Bot tracks `_best_edge_snapshot` in memory and flushes it on window close.

### 3. Close — window resolves

Triggered when `t_remaining <= 0` and resolution is fetched. Captures final market state, outcome, final Chainlink price, whether a trade was placed, and skip reason if applicable.

## Skip Reasons

| Reason | Meaning |
|--------|---------|
| `no-edge` | Edge never exceeded threshold |
| `risk-blocked` | Risk manager blocked (daily loss or consecutive losses) |
| `timing-early` | Edge appeared before `WINDOW_ENTRY_MIN_ELAPSED` |
| `timing-late` | Edge appeared after `WINDOW_ENTRY_MIN_REMAINING` |
| `missing-quote` | One or both sides had no valid ask |
| `consumed` | Another instance already traded this window |

## Write Path

### Ledger function

```python
def log_snapshot(window_slug, snapshot_type, stats, book_depth=None,
                 trade_fired=None, skip_reason=None, outcome=None, final_price=None):
```

Uses `INSERT OR REPLACE` against the `UNIQUE(window_slug, snapshot_type)` constraint for idempotent writes.

### Bot integration

- **New window:** `log_snapshot(..., "open", stats)` after first valid book update
- **Trade execution:** `log_snapshot(..., "decision", stats, book_depth, trade_fired=True)` right before calling executor
- **During window:** track `_best_edge_snapshot` in memory (dict copy on each new peak edge, no DB writes)
- **Window close (no trade):** flush `_best_edge_snapshot` as `"decision"` snapshot with `trade_fired=False, skip_reason=...`
- **Window close (always):** `log_snapshot(..., "close", stats, ..., outcome=outcome, final_price=final_price)`

### Book depth capture

Extend polymarket feed's book parsing to expose top 3 levels per side (currently only extracts best ask). Serialized as JSON before insert.

### What doesn't change

- `trades` table schema
- Trade execution flow (snapshots are fire-and-forget, don't gate trading)
- TUI
- Risk manager

## Example Queries

### Model calibration
```sql
SELECT ROUND(model_p_up, 1) AS bucket, COUNT(*) AS n,
       AVG(CASE WHEN outcome = 'up' THEN 1.0 ELSE 0.0 END) AS actual_up_rate
FROM window_snapshots WHERE snapshot_type = 'close' AND outcome IS NOT NULL
GROUP BY bucket;
```

### Missed opportunities
```sql
SELECT window_slug, skip_reason, edge, preview_side, outcome
FROM window_snapshots
WHERE snapshot_type = 'decision' AND trade_fired = 0 AND preview_side = outcome;
```

### Edge vs win rate
```sql
SELECT ROUND(d.edge, 2) AS edge_bucket, COUNT(*) AS n,
       AVG(CASE WHEN t.pnl > 0 THEN 1.0 ELSE 0.0 END) AS win_rate
FROM window_snapshots d JOIN trades t ON t.window_slug = d.window_slug
WHERE d.snapshot_type = 'decision' AND d.trade_fired = 1
GROUP BY edge_bucket;
```

### Skip reason distribution
```sql
SELECT skip_reason, COUNT(*) AS n
FROM window_snapshots WHERE snapshot_type = 'decision' AND trade_fired = 0
GROUP BY skip_reason ORDER BY n DESC;
```

## Storage Estimate

- ~3 rows per window, ~288 windows/day = ~864 rows/day
- ~315k rows/year
- Each row ~500 bytes = ~150 MB/year
- Well within SQLite's comfortable range
