# v2 logistic p_up training report (#15)
_seed=42; sklearn LogisticRegression L2; TimeSeriesSplit n_splits=5_

> **No-isotonic shipping config.** Earlier with-iso fit on a smaller
> corpus (n=3576) passed log-loss but failed the calibration gate due to
> isotonic overfitting on the cal slice; on the refreshed corpus (n=4461)
> the iso step pushed held-out log-loss from 0.451 (no-iso) to 0.629.
> The raw logistic is well-calibrated; the iso safety net was the bug.
> Pinned C=10 for stability (prior CV: C=10 and C=100 were tied to 1e-4).
>
> v2 ships behind MODEL_VERSION env var; v1 path stays reachable for
> rollback.

## Corpus
- N=4461  train=2676  cal=892  held=893
- train dates: 2026-04-24 14:35:00 -> 2026-05-05 23:30:59
- cal dates:   2026-05-05 23:35:43 -> 2026-05-09 05:34:23
- held dates:  2026-05-09 05:40:53 -> 2026-05-12 14:35:51
- base rate (up): train=0.513  cal=0.512  held=0.494

## CV log-loss across L2 strength (4-feature default; no-iso, C=10 pinned)
  C=  10.0: mean_logloss=0.3820  std=0.0274 <- chosen

## Reliability (held-out)

=== v2 (no-iso, C=10 pinned) ===
  0.50-0.60: n=  28  pred=0.545  actual=0.643  gap=0.098  CI=[0.464, 0.821]
  0.60-0.70: n=  35  pred=0.658  actual=0.686  gap=0.028  CI=[0.514, 0.829]
  0.70-0.80: n=  69  pred=0.762  actual=0.739  gap=0.023  CI=[0.638, 0.841]
  0.80-1.00: n= 273  pred=0.914  actual=0.901  gap=0.013  CI=[0.864, 0.934]

=== v1 (current shrinkage) ===
  0.50-0.60: n=  42  pred=0.544  actual=0.595  gap=0.051  CI=[0.429, 0.738]
  0.60-0.70: n=  46  pred=0.654  actual=0.739  gap=0.085  CI=[0.609, 0.848]
  0.70-0.80: n=  84  pred=0.757  actual=0.762  gap=0.005  CI=[0.667, 0.845]
  0.80-1.00: n= 245  pred=0.935  actual=0.894  gap=0.041  CI=[0.853, 0.931]

## Gate verdict: ship_ok = True

## Simulated EV under live gate

=== EV sim: v2 ===
  fires: 251 (81 up, 170 down)
  total PnL: $+164.905  (avg/fire $+0.6570)
  bootstrap 95% CI on total: [$+125.969, $+209.078]

=== EV sim: v1 ===
  fires: 150 (59 up, 91 down)
  total PnL: $+144.527  (avg/fire $+0.9635)
  bootstrap 95% CI on total: [$+108.714, $+182.811]

  delta total PnL (v2 - v1) 95% CI: [$-34.512, $+77.259]
  EV check: no veto (v2 not provably worse than v1).

## Ablations (held-out)
  primary (no-iso, C=10 pinned): log_loss=0.4514  C=10.0

=== v2 + spread + z*market ===
  0.50-0.60: n=  32  pred=0.547  actual=0.719  gap=0.172  CI=[0.562, 0.875]
  0.60-0.70: n=  15  pred=0.692  actual=0.600  gap=0.092  CI=[0.333, 0.867]
  0.70-0.80: n=0 (empty)
  0.80-1.00: n= 340  pred=0.912  actual=0.874  gap=0.038  CI=[0.835, 0.906]
  (a) +spread +z*market:  log_loss=0.6233  delta=+0.1718  gate_pass=False  C=10.0

=== v2 (no isotonic) ===
  0.50-0.60: n=  28  pred=0.545  actual=0.643  gap=0.098  CI=[0.464, 0.786]
  0.60-0.70: n=  35  pred=0.658  actual=0.686  gap=0.028  CI=[0.514, 0.829]
  0.70-0.80: n=  65  pred=0.761  actual=0.723  gap=0.037  CI=[0.615, 0.831]
  0.80-1.00: n= 277  pred=0.913  actual=0.903  gap=0.010  CI=[0.866, 0.939]
  (b) no isotonic:        log_loss=0.4516  delta=+0.0002
  (c) blended (+v1 feat): log_loss=0.5842  delta=+0.1328
  (d) per-fold scaler:    cv_logloss=0.3819 vs global=0.3820  delta=-0.0001

  >> shipping config: 4-core+no-iso

Wrote polypocket/model_v2_coefs.candidate.json
