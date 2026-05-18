# v3 logistic p_up training report

_seed=42; sklearn LogisticRegression L2; TimeSeriesSplit n_splits=5_

## Status: HALTED 2026-05-18 — design premise invalidated

The v3 design (`docs/plans/2026-05-17-logistic-p-up-v3-design.md`) targeted a
"regime drift on the 0.80+ bins" diagnosed at -10.2pt (0.80-0.85) and -13.8pt
(0.85-0.90) in `_bmad-output/v2_failure_diagnostics_modelver.md` (2026-05-17,
n=125/131 on the post-cutover `trade_fired=1 + bids` slice).

Time-localizing the same diagnostic filter:

| slice | n | 0.80-0.85 v2 gap |
|---|---:|---:|
| FULL post-cutover (2026-04-24 -> 2026-05-18) | 689 | **-10.8pt** (reproduces diagnostic) |
| EARLY (2026-04-24 -> 2026-05-11) | 261 | **-20.9pt** |
| LATE (2026-05-11 -> 2026-05-18 = v3 holdout window) | 428 | **-0.3pt** |

The drift was concentrated in the early post-cutover period when v2 had limited
tail-bin support; it has self-healed on the slice v3 would be evaluated against.
v2 is currently calibrated; v3 has nothing to fix at the 0.80+ tail.

**Both variants below FAIL §Q4 acceptance.** No coefs JSON committed. v2 remains
the active gate (`MODEL_VERSION=v2`).

Next-action options (per design's NO-GO list):
1. Monitor v2 health; refit only when fresh drift appears (recommended).
2. Pivot to the bins that ARE drifting per current `_model_health.md`:
   0.60-0.65 (+8.8pt) and 0.65-0.70 (+10.4pt) are under-confident — different
   problem from v3's target. Would need a new design.
3. Model class change (GBDT etc.) — tracked as a separate GitHub issue.

Detailed results below; the training infrastructure (`scripts/train_model_v3.py`)
is preserved on this branch as a starting point for any future refit.

## Gate verdict: ship_ok = False (no variant passed §Q4)

## Variant: `v0` (FAIL)

- Features: ['z', 't_remaining', 'sigma_5min', 'market_p_up_normalized']
- N train=3165 cal=1055 heldout=1055
- train dates: 2026-04-24 14:35:00 ->2026-05-07 18:55:30
- heldout dates: 2026-05-11 13:41:33 ->2026-05-18 16:05:51
- base rate (up): train=0.513  held=0.511
- chosen L2 C = 100.0; CV means: {0.1: 0.406420629561249, 1.0: 0.38119302728824167, 10.0: 0.378605253067855, 100.0: 0.3783823814561}

### Held-out metrics
- v3 log-loss = 0.3356; v3 Brier = 0.1055
- v2-on-v3-holdout log-loss = 0.3362
- v2-on-v2-holdout log-loss (reference, _model_v2_training.md) = 0.4514
- delta (v3 - v2) on v3 holdout = -0.0006 nats (required ≤ -0.005 for gate)

### v3 reliability (held-out)
| bin | n | mean_pred | actual | gap_pt | 95% CI |
|---|---:|---:|---:|---:|---:|
| 0.50-0.60 | 32 | 0.541 | 0.406 | -13.5 | [0.250, 0.594] |
| 0.60-0.70 | 35 | 0.652 | 0.743 | +9.0 | [0.600, 0.886] |
| 0.70-0.80 | 110 | 0.770 | 0.836 | +6.6 | [0.764, 0.909] |
| 0.80-0.85 | 68 | 0.823 | 0.765 | -5.9 | [0.662, 0.868] |
| 0.85-0.90 | 42 | 0.877 | 0.905 | +2.8 | [0.810, 0.976] |
| 0.90-1.00 | 250 | 0.985 | 0.988 | +0.3 | [0.972, 1.000] |

### v2 reliability on the same held-out rows
| bin | n | mean_pred | actual | gap_pt | 95% CI |
|---|---:|---:|---:|---:|---:|
| 0.50-0.60 | 37 | 0.548 | 0.405 | -14.3 | [0.270, 0.568] |
| 0.60-0.70 | 30 | 0.654 | 0.800 | +14.6 | [0.633, 0.933] |
| 0.70-0.80 | 121 | 0.768 | 0.818 | +5.0 | [0.744, 0.884] |
| 0.80-0.85 | 59 | 0.822 | 0.780 | -4.2 | [0.678, 0.881] |
| 0.85-0.90 | 42 | 0.877 | 0.881 | +0.4 | [0.786, 0.976] |
| 0.90-1.00 | 249 | 0.984 | 0.992 | +0.8 | [0.980, 1.000] |

### Side decomposition (gate-aligned)
- UP-bias v3: n=452 mean_pred=0.908 actual=0.920 gap=+1.2pt | v2: n=454 gap=+1.6pt
- DOWN-bias v3: n=478 mean_pred=0.808 actual=0.872 gap=+6.5pt | v2: n=474 gap=+7.2pt

### Simulated EV under live gate (held-out)
- v3 total PnL: $+3737.14
- v2 total PnL: $+3682.50
- delta (v3 - v2) 95% CI: [$-32.63, $+168.55]
- EV veto: False

### Isotonic ablation
- no-iso log-loss: 0.3356
- with-iso log-loss: 0.3377
- delta (iso - no-iso) = +0.0020 nats (ship iso only if ≤ -0.01 AND iso bin gate passes)

### Gate failures
- bin 0.50-0.60 gap -13.5pt outside +/-5.0pt (n=32)
- bin 0.60-0.70 gap +9.0pt outside +/-5.0pt (n=35)
- bin 0.70-0.80 gap +6.6pt outside +/-5.0pt (n=110)
- bin 0.80-0.85 gap -5.9pt outside +/-5.0pt (n=68)
- bin 0.80-0.85 gap -5.9pt < required -5.0pt
- DOWN overall gap +6.5pt outside +/-5.0pt
- held-out log-loss 0.3356 not better than v2 (0.3362) by >=0.005 nats

## Variant: `v0.1` (FAIL)

- Features: ['z', 't_remaining', 'sigma_5min', 'market_p_up_normalized', 'book_imbalance', 'spread', 'pre_decision_pmc_sigma', 'z_times_market']
- N train=1046 cal=349 heldout=349
- train dates: 2026-04-24 14:36:13 ->2026-05-09 01:39:20
- heldout dates: 2026-05-12 06:46:25 ->2026-05-18 15:28:13
- base rate (up): train=0.528  held=0.404
- chosen L2 C = 0.1; CV means: {0.1: 0.5238433750694294, 1.0: 0.5385733086454599, 10.0: 0.5493880985731998, 100.0: 0.5508726273340889}

### Held-out metrics
- v3 log-loss = 0.4776; v3 Brier = 0.1527
- v2-on-v3-holdout log-loss = 0.4631
- v2-on-v2-holdout log-loss (reference, _model_v2_training.md) = 0.4514
- delta (v3 - v2) on v3 holdout = +0.0145 nats (required ≤ -0.005 for gate)

### v3 reliability (held-out)
| bin | n | mean_pred | actual | gap_pt | 95% CI |
|---|---:|---:|---:|---:|---:|
| 0.50-0.60 | 3 | 0.509 | 0.333 | -17.6 | [0.000, 1.000] |
| 0.60-0.70 | 21 | 0.664 | 0.905 | +24.1 | [0.762, 1.000] |
| 0.70-0.80 | 52 | 0.753 | 0.808 | +5.5 | [0.692, 0.904] |
| 0.80-0.85 | 22 | 0.821 | 0.909 | +8.8 | [0.773, 1.000] |
| 0.85-0.90 | 12 | 0.866 | 0.917 | +5.1 | [0.750, 1.000] |
| 0.90-1.00 | 3 | 0.921 | 1.000 | +7.9 | [1.000, 1.000] |

### v2 reliability on the same held-out rows
| bin | n | mean_pred | actual | gap_pt | 95% CI |
|---|---:|---:|---:|---:|---:|
| 0.50-0.60 | 0 | — | — | — | — |
| 0.60-0.70 | 0 | — | — | — | — |
| 0.70-0.80 | 69 | 0.775 | 0.870 | +9.4 | [0.783, 0.942] |
| 0.80-0.85 | 30 | 0.821 | 0.833 | +1.2 | [0.700, 0.967] |
| 0.85-0.90 | 9 | 0.868 | 0.889 | +2.1 | [0.667, 1.000] |
| 0.90-1.00 | 2 | 0.933 | 1.000 | +6.7 | [1.000, 1.000] |

### Side decomposition (gate-aligned)
- UP-bias v3: n=65 mean_pred=0.814 actual=0.877 gap=+6.3pt | v2: n=110 gap=+6.5pt
- DOWN-bias v3: n=203 mean_pred=0.735 actual=0.808 gap=+7.3pt | v2: n=236 gap=+5.4pt

### Simulated EV under live gate (held-out)
- v3 total PnL: $+1190.52
- v2 total PnL: $+1615.82
- delta (v3 - v2) 95% CI: [$-588.26, $-242.81]
- EV veto: True

### Isotonic ablation
- no-iso log-loss: 0.4776
- with-iso log-loss: 0.4759
- delta (iso - no-iso) = -0.0017 nats (ship iso only if ≤ -0.01 AND iso bin gate passes)

### Per-feature ablation (drop-one, retrain at chosen C)
| dropped | held-out log-loss | Δ vs full |
|---|---:|---:|
| book_imbalance | 0.4783 | +0.0007 |
| spread | 0.4777 | +0.0001 |
| pre_decision_pmc_sigma | 0.4759 | -0.0017 |
| z_times_market | 0.4799 | +0.0023 |

### Gate failures
- bin 0.70-0.80 gap +5.5pt outside +/-5.0pt (n=52)
- DOWN overall gap +7.3pt outside +/-5.0pt
- held-out log-loss 0.4776 not better than v2 (0.4631) by >=0.005 nats
- PnL veto: 95% CI on (v3 - v2) total PnL = [-588.26, -242.81] entirely below zero

