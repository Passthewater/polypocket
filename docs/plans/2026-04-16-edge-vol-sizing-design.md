# Edge x Vol Dynamic Sizing + Norm Revert

## Problem

The t(df=5) distribution experiment showed no improvement over norm across three sessions.
Meanwhile, flat $10 position sizing loses money in low-vol regimes where the model's edge
estimates are unreliable (reports ~20% edge but trades net negative).

Back-test across all three paper trading databases:

| Session         | Flat $10 | Edge x Vol $5-20 |
|-----------------|----------|------------------|
| bak (high vol)  | +$60     | +$98             |
| df2.bak (mixed) | +$69     | +$168            |
| current (low v) | -$20     | +$3              |

## Changes

### 1. Revert to norm.cdf

`observer.py`: replace `t_dist.cdf(z, df=MODEL_TAIL_DF)` with `norm.cdf(z)`.
Remove `MODEL_TAIL_DF` from config and all imports.

### 2. Dynamic position sizing

Replace flat `POSITION_SIZE_USDC / entry_price` in `bot.py` with:

```
edge_scale = clamp((edge - EDGE_FLOOR) / EDGE_RANGE, 0, 1)
vol_scale  = clamp((sigma - VOL_FLOOR) / VOL_RANGE, 0, 1)
size_usdc  = MIN_POSITION_USDC + (edge_scale * vol_scale) * (MAX_POSITION_USDC - MIN_POSITION_USDC)
```

### 3. New config constants

```
MIN_POSITION_USDC = 5.0
MAX_POSITION_USDC = 20.0
VOL_FLOOR = 0.0005
VOL_RANGE = 0.0005
EDGE_FLOOR = 0.03
EDGE_RANGE = 0.17
```

### 4. Update dependents

- `analyze.py` missed-opportunity calc uses dynamic sizing
- `tui.py` displays size range
- Tests updated for new config shape

### 5. Not changed

- MIN_MODEL_CONFIDENCE stays symmetric 0.60
- No vol-gate (bot trades in low vol at $5 floor)
- MIN_EDGE_THRESHOLD stays 0.03

### Known limitation

The model's edge estimate is unreliable in low vol. The vol factor protects us (pulls
size down), but at moderate vol (~0.0008-0.001) with overestimated edge, the sizing
may still scale up on marginal trades. This is the next thing to investigate if PnL
stays flat.
