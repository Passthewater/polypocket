# Backtest Simulator Design

**Date**: 2026-04-15
**Status**: Approved

## Problem

Analyzing paper trading data to find optimal signal filters requires manual SQL queries. We need a repeatable CLI tool to test filter combinations against historical trades.

## Design

### Interface

CLI tool: `python -m polypocket.backtest`

Arguments (all optional):
- `--db PATH` — database path (default: `paper_trades.db`)
- `--min-edge FLOAT` — minimum edge threshold (default: `0.03`)
- `--min-alignment FLOAT` — model confidence filter; requires `model_p_up > X` for UP and `< (1-X)` for DOWN (default: `0.50`, no filter)
- `--min-disp-sigma FLOAT` — minimum `|displacement| / sigma` ratio (default: `0.0`, no filter)
- `--min-elapsed FLOAT` — minimum seconds elapsed in window (default: `60`)
- `--min-remaining FLOAT` — minimum seconds remaining (default: `30`)

### Output

1. **Baseline vs Filtered summary** — trades, wins, winrate, total PnL, avg PnL, trades filtered out
2. **Breakdowns for filtered subset**:
   - By side (up/down)
   - By edge bucket
   - By model confidence bucket
   - By displacement/sigma bucket

### Implementation

Single file `polypocket/backtest.py` with `__main__` support. Reads `trades` + `window_snapshots` tables. Pure SQL + Python formatting. No new dependencies.

## Related

Signal engine optimization: add model confidence guard (model_p_up > 0.60 for UP, < 0.40 for DOWN) based on 108-trade analysis showing 69.1% winrate vs 54.6% baseline.
