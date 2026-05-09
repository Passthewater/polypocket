# Logistic p_up model — data-trained replacement for the Brownian head

**Date:** 2026-04-23, **updated 2026-04-25** after the data-capture expansion (#16 / G1–G5) shipped. Sections marked **(updated 2026-04-25)** supersede their original text.
**Closes (if promoted to live):** #15; partially #13 (0.80+ miscalibration and `MAX_EDGE_THRESHOLD_UP` retirement)
**Depends on:** #13 analysis (the break-even-minus-fees finding that motivates replacing v1, not just tuning it); #16 (data capture, now shipped — supplies the post-G1 corpus this plan trains on)

## Problem

`compute_model_p_up` in `observer.py:29` is a `norm.cdf(displacement / sigma_remaining)` of a Brownian assumption that doesn't survive contact with BTC microstructure. The current fix — `calibrate_p_up` with two hand-tuned shrinkage factors (`CALIBRATION_SHRINKAGE_UP=1.00`, `CALIBRATION_SHRINKAGE_DOWN=0.50`) — is a linear two-knob fit to an asymmetric, nonlinear miscalibration. Issue #13 showed the symptom: the UP 0.80+ bin is overconfident (predicted 0.88, realized 0.57 on n=7) and even the dominant UP 0.70–0.75 bin is correctly calibrated but lands at break-even minus fees because the model's confidence equals the book-implied price. Issue #15 proposes a direct fit.

This plan executes #15.

## Goal

Ship a logistic-regression `compute_model_p_up_v2` with an isotonic calibration layer, trained on the full labeled corpus, promoted behind a `MODEL_VERSION` env var. Gate the promotion on a pre-committed reliability criterion measured on **two independent held-outs** (paper-only and live-only) to untangle the source/time/config confound in the training corpus. After a dual-logged paper A/B meeting both a raw-N and a tail-bin-N threshold, flip live.

## Empirical grounding (updated 2026-04-25)

The original measurement (2026-04-23, N=404 paper+live with selection bias on `trade_fired=1`) is **superseded**. Issue #16 / G1–G5 landed on 2026-04-24 and changes the corpus reality:

- **G1** — `snapshot_type='close'` rows are now emitted on **every** window with a BTC-derived label. Selection bias on `trade_fired=1` is gone. Verified at 27h soak: 326/326 close-row coverage; 99.4% with `outcome` populated.
- **G2** — every decision and close row carries `gate_config_json`, pinning the gate config that produced each row. Verified at 27h: 0 missing on 325 decision + 326 close.
- **G3** — every close row carries `btc_path_json` (≥50 points, 1Hz BTC path).
- **G4** — order lifecycle normalized into `order_events`. Pre-G4 live history is not retroactively diagnosable.
- **G5** — mid-window book samples land every 30s into a separate `window_book_samples` table.

The bot has been in **paper mode** since the capture work shipped. As of 2026-04-25 (~30h soak), paper has produced ~325 decisions / 326 closes. Linear extrapolation: ~2,000 labeled rows in 7 days, ~4,000 in 14 days.

**Training corpus for v2: paper-only, post-G1** (decision timestamps ≥ commit `5f76bea`'s merge time). Pre-G1 paper rows are excluded — different label semantics (`trade_fired=1`-only closes). Pre-G4 live rows are excluded — no order_events, and the bot has not been flipped back to live since G4 shipped. The paper-vs-live source confound from the original design dissolves: paper post-G1 is the entire training distribution under one stable post-G2 gate config.

| corpus | rows (estimated at retrain time) | notes |
|---|---|---|
| paper post-G1, ~7-day soak | ~2,000 | minimum-viable retrain horizon |
| paper post-G1, ~14-day soak | ~4,000 | preferred retrain horizon |

All labeled rows have `displacement`, `sigma_5min`, `t_remaining`, `up_ask`, `down_ask`, and `gate_config_json` non-null — the v0 core feature set is fully populated. Book-depth features remain deferred to v0.1: G5 stores them in a separate table, the join hasn't been written, and the v0 fit is the immediate goal.

**Selection bias note:** because outcomes are now BTC-derived (not fill-derived), the v2 corpus measures `p(BTC up | features at decision time)` over the *full* window distribution, not just the v1-gated subspace. This is the correct calibration target for an in-deployment model, regardless of which gate downstream consumes it.

**Paper-fill realism caveat:** v0 trains on environmental BTC outcomes, which are fill-independent — the calibration model is unaffected by the perfect-fill paper assumption. The paper-fill realism gap (#16 §"Paper-mode realism upgrade") only biases the *EV simulation* in §"Acceptance gate"; that's why EV is confirmatory, not gating.

## Open question resolutions

Issue #15 listed three open questions. Two are resolved by the empirical check above; one is a pre-decision for the user.

1. **Is `up_bids_json`/`down_bids_json` reliably populated on decision rows?** No. 0/564 paper, 78/373 live. **Resolution: v0 excludes book-depth features.** Revisit in v0.1 once forward collection produces ≥150 labeled rows with bids (~2–3 weeks at current live rate).
2. **Paper vs. live labels — trust both?** ~~Use both for the shipping fit, with paper-only and live-only held-outs as dual gates.~~ **Superseded 2026-04-25.** With G1–G5 shipped, the corpus is paper-only post-G1 under one stable gate config; the source/time/config confound that motivated dual gates dissolves. Resolution: train and gate on paper post-G1; treat the live cutover as the empirical paper→live transfer test (post-deployment monitoring, not a pre-ship gate).
3. **Blend vs. replace — include v1's output as a feature?** Cheap to test. **Resolution: test in the notebook as ablation.** Important subtlety: the ledger's `model_p_up` column was computed at logging time with whatever shrinkage was live *that day*, and shrinkage has been retuned since. Using that raw column as a feature contaminates training with a moving target. If the ablation is run, recompute v1 from current shrinkage constants on raw `displacement`/`σ`/`t_remaining` — do not read `d.model_p_up` from the ledger. Commit to replacement for the shipped model unless the correctly-computed blend measurably beats it on both held-outs.

## Feature set for v0

Must-have (all directly on the decision row):

- `displacement / sigma_remaining` — the v1 z-score. Computed as `displacement / (sigma_5min * sqrt(t_remaining / 300))`, guarding against `t_remaining <= 0` (drop those rows; they're end-of-window and the gate skips them anyway).
- `t_remaining` (seconds).
- `sigma_5min` (raw volatility level, regime proxy).
- `market_p_up_normalized = up_ask / (up_ask + down_ask)` — the market's own estimate, normalized to a probability. **Not** the ledger's `market_p_up` column: a spot check on 10 decision rows shows the ledger persists `market_p_up` as the raw `up_ask` value, not the normalized probability. Using the raw column at training and the formula at inference (or vice versa) produces train/serve skew. Canon: compute the normalized value in both the exporter and `compute_model_p_up_v2`, never read `d.market_p_up` directly.

Engineered, worth testing (ablation, not default):

- `spread = up_ask + down_ask - 1` — book tightness. Stored as log-spread if distribution is long-tailed.
- `z_times_market = (displacement / sigma_remaining) * market_p_up_normalized` — interaction between model and market agreement. Added to the ablation list, not the default feature set — include in shipped model only if ablation shows a log-loss lift on held-out.

Excluded (would leak): fill price, slippage, realized PnL, anything post-decision.

Excluded (too small an N): book-imbalance, top-of-book depth. Deferred to v0.1.

Feature count for the default model is **4 on N=404** (≈100 rows/feature with L2), with engineered features gated on ablation evidence.

## Train / calibrate / eval split (updated 2026-04-25)

Single chronological 60/20/20 split on the paper post-G1 corpus. With ~2,000–4,000 rows under one stable gate config, the original dual-split machinery (paper-only / live-only as independent gates) is no longer needed — the source/time/config confound that motivated it has dissolved.

### Design

Single split, evaluated chronologically:

- **Train (60%)** — fits the L2 logistic.
- **Calibrate (20%)** — fits the isotonic regression on top of the logistic's raw output.
- **Held-out (20%)** — gates the ship decision via §"Acceptance gate".

At ~2,000 rows: train ~1,200 / calibrate ~400 / held-out ~400. At ~4,000 rows: ~2,400 / ~800 / ~800. Both meaningfully above the original combined N=404. The 0.80+ tail bin (the bin #15 was written about) will populate with 60–200 rows on held-out, depending on how aggressively v2 lands probabilities in the tail — enough for tight per-bin reliability claims.

Ship decision: paper post-G1 held-out passes §"Acceptance gate" → ship. Otherwise → do not ship; investigate. There is no separate live held-out at training time — the live transfer test is the post-cutover monitoring window in §"Live cutover".

### CV inside the training slice

Cross-validation for hyperparameters (L2 strength, ablations) runs inside the training slice via **time-series 5-fold**: each fold's training set is rows older than the fold's held-out, which is a contiguous 20% chronological chunk. No shuffling. Report mean log-loss per fold.

**Scaler fit scope:** StandardScaler is fit on the full training slice (not per-fold inside CV). Per-fold scaling is purer; at n≥1,200 the difference is epsilon. Document the choice in the training report.

## Acceptance gate (updated 2026-04-25)

Reliability is the gating criterion; simulated EV is confirmatory. Single held-out slice (paper post-G1) — see §"Train / calibrate / eval split".

### Criterion 1 — Reliability (gating)

Coarse 4-bin reliability table on the held-out, buckets chosen to mirror the live gate regime:

| bin | gating rule |
|---|---|
| 0.50–0.60 | \|predicted − actual\| ≤ 5pts when n ≥ 30; else CI check |
| 0.60–0.70 | \|predicted − actual\| ≤ 5pts when n ≥ 30; else CI check |
| 0.70–0.80 | \|predicted − actual\| ≤ 5pts when n ≥ 30; else CI check |
| 0.80+ | when n ≥ 20: \|predicted − actual\| ≤ 5pts AND v2 point-estimate gap ≤ v1 point-estimate gap. When 5 ≤ n < 20: actual WR ∈ 95% bootstrap CI of predicted AND v2 gap ≤ v1 gap. When n < 5: **0.80+ bin is empty or near-empty — do not ship.** A model whose tail bin doesn't populate at all has no evidence it fixes the bin #15 was written about. |

At the 0.80+ bin the v2 point estimate must **not** be worse than v1's, and the bin must contain real samples. Bootstrap-CI coverage alone is not enough: a model that softened to "probably in range, no evidence better than v1" is not an improvement over v1.

Per-bin n thresholds (≥30 for the lower bins, ≥20 for 0.80+) match the held-out sizes expected at retrain time (~400 rows at the 7-day horizon, ~800 at 14 days). At ~400 held-out rows split across 4 bins, ≥30 in the populated middle bins is realistic; the 0.80+ bin is the one to watch.

### Criterion 2 — Simulated held-out EV (confirmatory, not gating)

Run the existing gate logic over the held-out rows with v1 vs. v2 probabilities and compute simulated PnL. Report:

- v1 vs. v2 total simulated PnL
- number of fires per version
- bootstrap 95% CI on the delta

This is **reported** alongside the reliability table but does **not** gate the ship. Two reasons it stays confirmatory: (a) the EV simulation uses paper `entry_price` under the perfect-fill paper assumption, which #16 §"Paper-mode realism upgrade" identifies as biased — absolute magnitudes are unreliable, only relative v1-vs-v2 deltas have signal; (b) PnL bootstrap CIs at held-out N often span zero, so adopting EV as a gate would silently let reliability drive the decision anyway.

**Pre-committed veto**: if v2's simulated PnL is *worse* than v1's by a statistically meaningful margin (95% CI of delta entirely negative), that is a ship veto regardless of reliability. A calibration improvement that makes money worse on the same fill assumption is not a shipping event.

## Shipping

### Persistence

- `polypocket/model_v2_coefs.json` — fitted coefficients, intercept, feature names, and the isotonic calibration as a list of (raw_prob, calibrated_prob) breakpoints. Small file (<5KB), committed to the repo. Includes a training-metadata block: corpus commit SHA of the exporter, row counts, split dates, held-out metrics.
- `polypocket/observer.py` — add `compute_model_p_up_v2(features: dict) -> float`. v1's `compute_model_p_up` stays as-is. A top-level `compute_model_p_up_active` dispatcher reads `MODEL_VERSION` env var (default "v1") and routes to the appropriate implementation.

### Integration

- `polypocket/signal.py:77` — replace the direct `compute_model_p_up(...)` / `calibrate_p_up(...)` pair with `compute_model_p_up_active(...)`. When `MODEL_VERSION=v2`, the shrinkage/calibration step is skipped (v2 is already calibrated). When `MODEL_VERSION=v1`, current behavior.
- `polypocket/ledger.py` — add `model_p_up_v2 REAL` column to `window_snapshots`. Dual-logged on every decision regardless of which version drives the trade (see Paper A/B below).
- `polypocket/config.py` — add `MODEL_VERSION = os.getenv("MODEL_VERSION", "v1")`. No change to default runtime behavior until paper A/B passes.

### No-dependency surface

v2 must compute p_up from values already on the decision row at decision time. The features list above confirms this: nothing requires data not already plumbed into `signal.py::evaluate`. A CI test pins the feature set and asserts `compute_model_p_up_v2` receives only those fields.

## Paper A/B (updated 2026-04-25)

Dual-log design: regardless of which `MODEL_VERSION` drives gating, three probability columns are written to every decision row — `model_p_up` (whichever version fires), `model_p_up_v1_calibrated` (v1 calibrated, always), and `model_p_up_v2` (v2, always). The two version-specific columns are unconditional dual-logging; the version-independent `model_p_up` keeps backward compatibility with existing readers but is **not** trustworthy as a v1 source after cutover. The comparison script reads `model_p_up_v1_calibrated` and `model_p_up_v2` directly to avoid that ambiguity.

### Sample size target

**n ≥ 200 fresh decisions with matching close outcomes, AND the 0.80+ bin under v2 has n ≥ 20**, whichever arrives later. The raw-N floor exists so each non-tail bin has ≥30 samples for the gate's small-n threshold; the tail-bin floor is the one that directly addresses #15's motivating concern. At the post-G1 paper rate (~250–300 decisions/day) both thresholds typically land in 1–2 days of wall clock.

**Tail-bin escape hatch:** if the 0.80+ bin under v2 has not reached n=20 within 7 wall-clock days because v1's gate is starving v2's tail (v1 only lets through rows where v1 itself fires, which by construction excludes most of v2's tail when v2 disagrees with v1), flip the paper bot to `MODEL_VERSION=v2` for an additional 2-day collection window. This lets v2's gate populate v2's own tail while keeping risk in paper. After that window expires, evaluate with whatever tail-bin n is available; if still <10, do not promote — the calibration claim at the tail can't be supported.

### Promotion criteria

Re-run Criterion 1 (reliability) on the fresh slice, treating the paper A/B data as a fresh held-out. v2 must pass the same 4-bin gate. Additionally, v2's reliability gaps on the A/B slice must not be ≥5pts worse than its gaps on the original training held-out — that's the regime-drift guard. (5pts is the bin-tolerance gap itself; 3pts on a held-out at n≈400 with bootstrap CIs of ±2–3pts is below the noise floor.) Criterion 2 (simulated EV) remains confirmatory-only with the same strict-regression veto. If regime drift is detected or the veto trips, pause promotion and diagnose.

### Comparison script

New `scripts/compare_model_versions.py`: queries `window_snapshots` for decisions where both `model_p_up` and `model_p_up_v2` are non-null and a matching close row has an outcome. Emits a markdown report (`scripts/_model_v2_paper_ab.md`) with:

- Per-bin reliability for both versions
- Simulated gate-fire PnL for both versions at current live config
- Bootstrap CI on the difference
- Go / no-go verdict per the promotion criteria

## Live cutover

1. With paper A/B passing, set `MODEL_VERSION=v2` on the live launch env.
2. Watch live for the first 20 fills. If the 0.80+ bin regresses, `MODEL_VERSION=v1` flips back in one line — both paths are still wired.
3. After 2 weeks of live-v2 with no regression, commit the cleanup:
   - Retire `CALIBRATION_SHRINKAGE_UP`, `CALIBRATION_SHRINKAGE_DOWN` from `config.py`.
   - Retire `MAX_EDGE_THRESHOLD_UP` (added to paper over the miscalibration — no longer needed if v2 is calibrated at the tails).
   - Remove `calibrate_p_up` from `observer.py`.
   - Change `MODEL_VERSION` default to `"v2"`; leave the env-var escape hatch intact.
   - Remove v1's `compute_model_p_up` only after a further 2 weeks with no rollback usage.

## Out of scope

- GBDT, boosted trees, neural models. N=404 can't support it; even CV estimates of tree hyperparameters would be noise at this scale.
- Regime-conditional models (separate fits by volatility regime).
- Online learning / weekly re-fit automation. Future work if v2 shows drift.
- Backfilling outcomes for non-traded windows via external BTC prices. Would 3–5× the corpus but is a separate workstream.
- Book-depth features. Deferred to v0.1.

## Artifact

`scripts/_model_v2_training.md` — committed training report:

- Corpus summary (N, split sizes, date ranges, base rate per split)
- Feature list and L2 strength chosen
- Full CV log-loss per fold
- Fitted coefficients + isotonic breakpoints
- Reliability table (held-out)
- Simulated EV comparison (held-out v2 vs v1)
- Ablations: blended-v1-feature, paper-only fit, live-only fit

## Definition of done (updated 2026-04-25)

Paper post-G1 held-out Criterion 1 passed (no Criterion 2 veto) on ≥7 days of soak, `polypocket/model_v2_coefs.json` committed, dual-logging shipped (`model_p_up_v1_calibrated` + `model_p_up_v2` columns), paper A/B report generated on ≥200 fresh decisions with 0.80+ bin n ≥ 20 (escape hatch invoked if needed), promotion criteria passed, live cut over with explicit human-approval gate, 2-week watch window elapsed with no regression, v1 cleanup committed.

## Top risks (updated 2026-04-25)

1. **Paper→live transfer.** v2 is trained and gated on paper post-G1. Outcomes are environmental (BTC-derived) so the calibration model itself is fill-independent — but live execution introduces fill latency, slippage, and reject behavior that paper does not simulate. The model's `p_up` will be valid at the live decision point; what changes is the EV at fire time. Mitigation: the live cutover (§"Live cutover") includes a post-deployment monitoring window with explicit rollback rules. This is the only paper→live test the plan can offer until #16's "paper-mode realism upgrade" lands.
2. **Tail-bin starvation by the v1 gate during paper A/B.** v2's 0.80+ bin populates only on rows where v1 also let the bot through to a decision. If v2 systematically disagrees with v1 in the tail (the failure mode #15 was written about), the tail bin stays sparse no matter how long the A/B runs. The escape hatch in §"Paper A/B" addresses this: flip the paper bot to `MODEL_VERSION=v2` for a 2-day window so v2's gate populates v2's tail. Residual risk: if v2 promotes on the basis of an escape-hatch-generated tail bin, the paper A/B has not actually compared v2 to v1 *under v1's gate decisions* — it has compared v2 under its own gate. Document explicitly when reporting.
3. **Selection-bias inversion.** The pre-G1 corpus had selection bias *toward* gated rows; the post-G1 corpus has selection bias *toward* non-gated rows (which dominate by ~10:1). v2 will see far more 0.50–0.65 confidence rows than 0.70+ rows during training. L2 logistic on imbalanced confidence regions is fine in principle, but the 0.80+ bin's training support is by construction thin. The held-out gate's per-bin n threshold (≥20 at 0.80+) is calibrated to this; the per-feature `feature_hull` warning (§"Persistence") provides a runtime trip-wire if live decisions stray outside the trained-support hull.
