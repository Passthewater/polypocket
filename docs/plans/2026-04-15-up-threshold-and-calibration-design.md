# UP Confidence Threshold + Model Calibration

**Date:** 2026-04-15
**Status:** Approved

## Context

The 60% model confidence guard improved win rate from 54.6% (old DB, 108 trades) to 64.6% (new DB, 82 trades) and avg PnL/trade from $0.56 to $1.52. However, the UP side remains the weaker signal:

- DOWN: 67.4% win rate, $88.78 PnL (43 trades)
- UP: 61.5% win rate, $36.06 PnL (39 trades)

Additionally, trades with 30%+ calculated edge only win 50% of the time, suggesting the model's probability estimates are miscalibrated at the extremes.

## Phase 1: Raise UP Confidence Threshold to 65%

### Change

Add a separate `MIN_MODEL_CONFIDENCE_UP = 0.65` config value. The existing `MIN_MODEL_CONFIDENCE` (0.60) continues to apply to DOWN trades via the `(1 - MIN_MODEL_CONFIDENCE)` check.

In `signal.py`, change the `up_aligned` check to use the new threshold:

```python
up_aligned = model_p_up >= MIN_MODEL_CONFIDENCE_UP
```

DOWN remains unchanged:

```python
down_aligned = model_p_up <= (1 - MIN_MODEL_CONFIDENCE)
```

### Expected Impact

Recent strong UP wins (trades 77-81) all had model_p_up > 0.80 — well above 65%. The filter targets marginal UP calls (0.60-0.65) which had worse outcomes. Trade volume drops slightly; quality improves.

### Files Changed

- `polypocket/config.py` — add `MIN_MODEL_CONFIDENCE_UP`
- `polypocket/signal.py` — use new threshold for UP alignment
- `tests/` — update any tests referencing the UP confidence check

## Phase 2: Model Calibration Analysis

### Goal

Quantify where `model_p_up` predictions diverge from actual outcomes so we can decide whether to apply a correction curve or retrain.

### Approach

Add a `calibration` subcommand or function (in `analyze.py` or a new script) that:

1. Bins all settled trades by `model_p_up` into decile buckets (0.0-0.1, 0.1-0.2, ..., 0.9-1.0)
2. For each bucket, computes: trade count, actual "up" outcome rate, predicted mean model_p_up
3. Outputs a calibration table showing predicted vs actual
4. Flags buckets where the gap exceeds a threshold (e.g., >10pp)
5. Optionally merges data from both the current DB and backup DB for larger sample size

### Output Format

```
Calibration Report (N=82 trades)
Bucket       | Trades | Predicted P(up) | Actual P(up) | Gap
0.00 - 0.10  |     5  |          0.04   |       0.20   | +0.16 ⚠
0.60 - 0.70  |    12  |          0.64   |       0.58   | -0.06
0.90 - 1.00  |     8  |          0.95   |       0.50   | -0.45 ⚠
```

### Files Changed

- `polypocket/analyze.py` — add calibration report function
- Possibly CLI entry point update if adding a subcommand

## What This Does NOT Include

- Time-of-day filtering (insufficient data to justify)
- Model retraining (calibration analysis informs whether that's needed)
- Position sizing changes
