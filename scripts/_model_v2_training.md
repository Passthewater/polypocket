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
- N=3576  train=2145  cal=715  held=716
- train dates: 2026-04-24 14:35:00 -> 2026-05-04 00:45:01
- cal dates:   2026-05-04 00:55:15 -> 2026-05-06 15:52:36
- held dates:  2026-05-06 16:01:13 -> 2026-05-09 06:11:47
- base rate (up): train=0.514  cal=0.522  held=0.501

## CV log-loss across L2 strength (4-feature default + isotonic)
  C=  0.01: mean_logloss=0.5067  std=0.0220
  C=   0.1: mean_logloss=0.4118  std=0.0246
  C=   1.0: mean_logloss=0.3806  std=0.0272
  C=  10.0: mean_logloss=0.3793  std=0.0283 <- chosen
  C= 100.0: mean_logloss=0.3795  std=0.0285

## Reliability (held-out)

=== v2 (4-core + iso) ===
  0.50-0.60: n=  48  pred=0.530  actual=0.438  gap=0.093  CI=[0.312, 0.583]
  0.60-0.70: n=   7  pred=0.611  actual=0.571  gap=0.039  CI=[0.143, 0.857]
  0.70-0.80: n=  91  pred=0.739  actual=0.747  gap=0.008  CI=[0.659, 0.835]
  0.80-1.00: n= 230  pred=0.914  actual=0.926  gap=0.012  CI=[0.891, 0.957]

=== v1 (current shrinkage) ===
  0.50-0.60: n=  35  pred=0.553  actual=0.629  gap=0.076  CI=[0.457, 0.800]
  0.60-0.70: n=  56  pred=0.654  actual=0.714  gap=0.061  CI=[0.589, 0.821]
  0.70-0.80: n=  91  pred=0.762  actual=0.769  gap=0.007  CI=[0.670, 0.857]
  0.80-1.00: n= 203  pred=0.912  actual=0.911  gap=0.000  CI=[0.872, 0.951]

## Gate verdict: ship_ok = False
  - FAIL: paper-post-G1 0.50-0.60: gap 0.093 > 0.05
  - FAIL: paper-post-G1 0.80+: v2 gap 0.012 > v1 gap 0.000

## Simulated EV under live gate

=== EV sim: v2 ===
  fires: 148 (46 up, 102 down)
  total PnL: $+126.758  (avg/fire $+0.8565)
  bootstrap 95% CI on total: [$+93.723, $+169.558]

=== EV sim: v1 ===
  fires: 208 (102 up, 106 down)
  total PnL: $+199.584  (avg/fire $+0.9595)
  bootstrap 95% CI on total: [$+153.971, $+252.260]

  delta total PnL (v2 - v1) 95% CI: [$-135.251, $-10.121]
  >> EV VETO: v2 worse than v1 (CI entirely negative).

## Ablations (held-out)
  primary (4-core + iso): log_loss=0.3636  C=10.0

=== v2 + spread + z*market ===
  0.50-0.60: n=   6  pred=0.510  actual=0.167  gap=0.343  CI=[0.000, 0.500]
  0.60-0.70: n=   4  pred=0.614  actual=0.250  gap=0.364  CI=[0.000, 0.750]
  0.70-0.80: n= 102  pred=0.750  actual=0.765  gap=0.015  CI=[0.676, 0.843]
  0.80-1.00: n= 222  pred=0.926  actual=0.928  gap=0.002  CI=[0.892, 0.959]
  (a) +spread +z*market:  log_loss=0.3636  delta=-0.0000  gate_pass=False  C=1.0

=== v2 (no isotonic) ===
  0.50-0.60: n=  44  pred=0.551  actual=0.432  gap=0.119  CI=[0.273, 0.568]
  0.60-0.70: n=  36  pred=0.652  actual=0.444  gap=0.207  CI=[0.278, 0.611]
  0.70-0.80: n=  91  pred=0.753  actual=0.780  gap=0.027  CI=[0.692, 0.857]
  0.80-1.00: n= 221  pred=0.913  actual=0.932  gap=0.019  CI=[0.896, 0.964]
  (b) no isotonic:        log_loss=0.3639  delta=+0.0002
  (c) blended (+v1 feat): log_loss=0.3673  delta=+0.0037
  (d) per-fold scaler:    cv_logloss=0.3794 vs global=0.3793  delta=+0.0001

  >> shipping config: 4-core+iso (engineered did not lift by >=0.005 on held-out, or gate failed)

Wrote polypocket/model_v2_coefs.json
