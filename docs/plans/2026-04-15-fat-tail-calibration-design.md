# Fat-Tail Model Calibration (t-distribution)

**Date:** 2026-04-15
**Status:** Approved

## Problem

`compute_model_p_up` uses `norm.cdf(displacement / sigma_remaining)` — a normal distribution that assumes thin tails. BTC has fat tails. The model reports 97% confidence when reality is 67%. Both UP and DOWN extremes are overconfident.

Calibration data (192 trades across both DBs):

| Bucket | Predicted | Actual | Gap |
|---|---|---|---|
| 0.9-1.0 | 96.8% | 66.7% | -30.2% |
| 0.8-0.9 | 85.3% | 75.0% | -10.3% |
| 0.0-0.1 | 2.4% | 23.5% | +21.2% |
| 0.3-0.4 | 36.3% | 31.4% | -4.9% (well calibrated) |

## Solution

Replace `norm.cdf` with `t.cdf` using a single `df` (degrees of freedom) parameter. Lower df = fatter tails = probabilities pulled toward 0.5. At df=infinity, t-distribution equals normal (current behavior).

Effect at displacement/sigma_remaining = 2.0:
- norm.cdf(2.0) = 0.977 (current)
- t.cdf(2.0, df=5) = 0.949
- t.cdf(2.0, df=3) = 0.930

## Architecture

### Phase 1: Add df parameter + backtest sweep

- Add `MODEL_TAIL_DF` to `polypocket/config.py`
- Replace `norm.cdf` with `t.cdf(x, df=MODEL_TAIL_DF)` in `compute_model_p_up`
- Add a backtest/sweep script that tests df values (2-20 + infinity) against both DBs' settled trades
- Pick the df that minimizes mean absolute calibration error

### Phase 2: Paper trade validation

- Deploy the chosen df value
- After ~100 trades, run `python -m polypocket.analyze --calibration` again
- Compare calibration gaps to pre-change baseline

## Files Changed

- `polypocket/config.py` — add `MODEL_TAIL_DF`
- `polypocket/observer.py` — replace `norm.cdf` with `t.cdf`
- `tests/test_observer.py` — update tests for new distribution
- `polypocket/backtest.py` or new script — df sweep tool
- `tests/test_signal.py` — may need displacement adjustments since probabilities shift

## Decisions

- Single df for both UP and DOWN (not separate per direction) — avoids overfitting with only 192 trades
- No empirical post-hoc correction curve — try the principled fix first
- No changes to edge calculation or thresholds
- Validate via backtest sweep first, then paper trade to confirm
