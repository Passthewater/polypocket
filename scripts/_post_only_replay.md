# Post-only paper replay — 2026-05-16 04:10:22 UTC

- DB: `paper_trades.db`
- offset_ticks: `2`
- cancel_buffer_s: `30.0`

## Fill statistics

- Total decisions with bids JSON: **1671**
- Eligible (pmc computable): **1669** (skipped 2 for missing opp bid)
- Would-have-filled: **483**
- Fill rate: **28.9%**
- Median post-decision sample-index at fill: **1** (~30s after decision time)

Note: book samples are at 30-second cadence; reported fill rate is a LOWER BOUND. Live data with sub-second granularity may show a higher fill rate.

## Calibration (would-have-filled cohort only)

| bin | n | mean p_pred | hit rate | gap |
|---|---:|---:|---:|---:|
| 0.50-0.55 | 2 | 0.543 | 0.000 | -0.543 |
| 0.55-0.60 | 1 | 0.567 | 0.000 | -0.567 |
| 0.60-0.65 | 9 | 0.636 | 0.556 | -0.080 |
| 0.65-0.70 | 18 | 0.676 | 0.500 | -0.176 |
| 0.70-0.75 | 62 | 0.720 | 0.371 | -0.349 |
| 0.75-0.80 | 172 | 0.770 | 0.442 | -0.328 |
| 0.80-0.85 | 73 | 0.821 | 0.397 | -0.424 |
| 0.85-0.90 | 61 | 0.872 | 0.443 | -0.429 |
| 0.90-0.95 | 38 | 0.926 | 0.421 | -0.505 |
| 0.95-1.00 | 34 | 0.978 | 0.324 | -0.655 |

## Acceptance check

- Fill rate **28.9%** is in the plausible 15-80% band.
