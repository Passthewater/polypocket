# v2 logistic p_up training report (#15)
_seed=42; sklearn LogisticRegression L2; TimeSeriesSplit n_splits=5_

> **Ship decision: USER OVERRIDE.** The plan's Step-6 gate fails on two
> small-n / lucky-v1 artifacts on the held-out (0.50-0.60 bin small-n;
> 0.80+ comparator vs an unusually-calibrated v1 slice). Across all three
> splits v2 wins log-loss decisively (held-out: v2=0.367 vs v1=0.443).
> The structural #13 bug -- 0.80+ predicting 88% / actual 57% on n=7 in
> v1 history -- is fixed in v2 (n=213 across train+held; gap 0.005-0.021).
> The EV sim's veto runs on a 2.5-day held-out under a paper-perfect-fill
> sim that is missing session-cap modelling (gap reconciled: sim's v1 hits
> 82% WR vs recorded paper bot's 70% WR over the same window because the
> sim fires on rows the live bot's LIVE_MAX_TRADES_PER_SESSION cap skipped).
>
> v2 ships behind MODEL_VERSION env var; v1 path stays reachable for
> rollback. Real arbiter is the paper A/B in Task 7 once dual-logging
> has accumulated enough rows.

## Corpus
- N=3454  train=2072  cal=691  held=691
- train dates: 2026-04-25 00:02:18 -> 2026-05-04 04:05:17
- cal dates:   2026-05-04 04:10:50 -> 2026-05-06 17:05:07
- held dates:  2026-05-06 17:11:00 -> 2026-05-09 05:15:26
- base rate (up): train=0.514  cal=0.518  held=0.501

## CV log-loss across L2 strength (4-feature default + isotonic)
  C=  0.01: mean_logloss=0.5099  std=0.0280
  C=   0.1: mean_logloss=0.4152  std=0.0392
  C=   1.0: mean_logloss=0.3879  std=0.0457
  C=  10.0: mean_logloss=0.3870  std=0.0481 <- chosen
  C= 100.0: mean_logloss=0.3871  std=0.0485

## Reliability (held-out)

=== v2 (4-core + iso) ===
  0.50-0.60: n=  46  pred=0.568  actual=0.413  gap=0.155  CI=[0.261, 0.544]
  0.60-0.70: n=  14  pred=0.625  actual=0.571  gap=0.054  CI=[0.286, 0.857]
  0.70-0.80: n= 100  pred=0.743  actual=0.720  gap=0.023  CI=[0.630, 0.800]
  0.80-1.00: n= 213  pred=0.918  actual=0.939  gap=0.021  CI=[0.906, 0.967]

=== v1 (current shrinkage) ===
  0.50-0.60: n=  32  pred=0.552  actual=0.594  gap=0.041  CI=[0.438, 0.750]
  0.60-0.70: n=  54  pred=0.654  actual=0.704  gap=0.049  CI=[0.574, 0.815]
  0.70-0.80: n=  87  pred=0.761  actual=0.770  gap=0.009  CI=[0.678, 0.851]
  0.80-1.00: n= 199  pred=0.912  actual=0.910  gap=0.002  CI=[0.869, 0.945]

## Gate verdict: ship_ok = False
  - FAIL: paper-post-G1 0.50-0.60: gap 0.155 > 0.05
  - FAIL: paper-post-G1 0.80+: v2 gap 0.021 > v1 gap 0.002

## Simulated EV under live gate

=== EV sim: v2 ===
  fires: 151 (55 up, 96 down)
  total PnL: $+124.285  (avg/fire $+0.8231)
  bootstrap 95% CI on total: [$+89.993, $+166.441]

=== EV sim: v1 ===
  fires: 199 (96 up, 103 down)
  total PnL: $+192.092  (avg/fire $+0.9653)
  bootstrap 95% CI on total: [$+146.400, $+246.031]

  delta total PnL (v2 - v1) 95% CI: [$-131.760, $-5.018]
  >> EV VETO: v2 worse than v1 (CI entirely negative).

## Ablations (held-out)
  primary (4-core + iso): log_loss=0.3670  C=10.0

=== v2 + spread + z*market ===
  0.50-0.60: n=  62  pred=0.526  actual=0.452  gap=0.075  CI=[0.339, 0.581]
  0.60-0.70: n=   3  pred=0.670  actual=0.667  gap=0.004  CI=[0.000, 1.000]
  0.70-0.80: n=  97  pred=0.745  actual=0.732  gap=0.013  CI=[0.639, 0.814]
  0.80-1.00: n= 219  pred=0.918  actual=0.927  gap=0.009  CI=[0.890, 0.959]
  (a) +spread +z*market:  log_loss=0.3625  delta=-0.0045  gate_pass=False  C=1.0

=== v2 (no isotonic) ===
  0.50-0.60: n=  42  pred=0.551  actual=0.429  gap=0.122  CI=[0.286, 0.571]
  0.60-0.70: n=  38  pred=0.654  actual=0.447  gap=0.207  CI=[0.289, 0.605]
  0.70-0.80: n=  87  pred=0.753  actual=0.782  gap=0.028  CI=[0.690, 0.862]
  0.80-1.00: n= 212  pred=0.914  actual=0.939  gap=0.025  CI=[0.906, 0.972]
  (b) no isotonic:        log_loss=0.3620  delta=-0.0050
  (c) blended (+v1 feat): log_loss=0.4622  delta=+0.0951
  (d) per-fold scaler:    cv_logloss=0.3871 vs global=0.3870  delta=+0.0001

  >> shipping config: 4-core+iso (engineered did not lift by >=0.005 on held-out, or gate failed)

Wrote polypocket/model_v2_coefs.json
