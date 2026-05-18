# Logistic p_up v3 — refit on the 5,076-row post-G1 corpus

> **STATUS: HALTED 2026-05-18** — investigated end-to-end; both v0 and v0.1
> variants fail §Q4. The design's "regime drift on the 0.80+ bins" premise
> was invalidated by primary evidence: the drift was time-localized to the
> early post-cutover period and has fully mean-reverted on the slice v3 would
> evaluate against. v2 stays the active gate. Full evidence in
> `scripts/_model_v3_training.md`; training infrastructure preserved at
> `scripts/train_model_v3.py` as a starting point for any future refit.

**Date:** 2026-05-17
**Companion (deferred):** `2026-05-17-logistic-p-up-v3-implementation.md` — drafted after this design is approved.
**Depends on:** the post-G1 capture pipeline (`#16` / G1–G5, shipped 2026-04-24) that supplies this plan's corpus; the v2 training pipeline at `docs/plans/2026-04-23-logistic-p-up-model-design.md` (this plan extends rather than replaces it — same model class, calibration layer, dual-logging discipline, and acceptance shape).
**Sequenced after:** `2026-05-17-post-only-retirement-design.md`. The retirement removes the active bleeding mechanism; this plan fixes the bin-level model drift that retirement explicitly carried forward as known risk (§"What can break" row 3 of that plan).
**Closes (if promoted to live):** the 0.80–0.85 / 0.85–0.90 calibration drift that surfaced in the post-only-retirement review; opens a clean baseline for the next live FAK cohort plan.

**Correction note (2026-05-17, during impl-plan drafting):** v2 was actually trained 2026-05-12 on **n=4,461 rows**, not on n=404 as my earlier draft of this design claimed by reading the v2 *design* doc (which projected n=404 at design time, before the data shipped). The v2 *training report* at `scripts/_model_v2_training.md` is the authoritative source. v2 also ships **without isotonic calibration** — the iso step pushed held-out log-loss from 0.451 to 0.629 and was dropped. This design has been updated throughout to reflect those facts; the goal of v3 shifts from "fix tail-bin starvation" to "address regime drift on a slightly larger corpus + test the deferred book-depth features." The core recommendation — refit, two corpora variants, gate must beat v2 — is unchanged.

## Problem

The v2 logistic model (`model_p_up_v2`, trained 2026-05-12 on n=4,461 paper post-G1 rows, **no isotonic** per `scripts/_model_v2_training.md:6-8` — the isotonic step pushed held-out log-loss from 0.451 to 0.629 and was dropped) passed its own training held-out gate at the 0.80+ tail (gap +1.3pt at n=273) but has drifted in production. Two structural misses, surfaced by the 2026-05-17 retirement diagnostic and `scripts/_model_health.md`:

| slice | bin | n | mean p_pred | actual wr | gap |
|---|---|---:|---:|---:|---:|
| post-cutover paper, all-eligible | 0.80–0.85 | 125 | 0.815 | 71.2% | **−10.2pt** |
| post-cutover paper, all-eligible | 0.85–0.90 | 131 | 0.878 | 74.0% | **−13.8pt** |
| live FAK v2 cohort (n=20) | overall DOWN | 35 | 83.1% | 71.4% | **−11.7pt** |

Source: `_bmad-output/v2_failure_diagnostics_modelver.md:26-29`, `scripts/_model_health.md:80-96`. The bin-level drift is on the *all-eligible* paper population — not the post-only filled subset — meaning it's a model-calibration issue separate from the post-only fill mechanism. A FAK-only bot would see this drift on every high-confidence decision.

**This is regime drift, not tail-bin starvation.** v2's training held-out (`scripts/_model_v2_training.md:24-31`) had n=273 in the 0.80–1.00 band with gap +1.3pt — the tail was supported, the calibration was real. The drift opened up *after* v2 became the active gate: production decisions between 2026-05-09 (`model_p_up_v2` first populated) and 2026-05-15 show the 0.85–0.90 bin sliding from −9pt (30-day model_health window) to −13.8pt (post-cutover diagnostic slice). v3's task is to refit with the recent regime included in training, plus test whether the book-depth features v2 deferred for corpus-coverage reasons (1,667 bids-populated rows now, vs. v2's training-time floor of 150) add a feature that anticipates regime shifts.

### Corpus inventory (verified 2026-05-17 against `paper_trades.db`)

Joining `window_snapshots` decision rows to close rows on `window_slug`, post-G1 (≥ 2026-04-24 — one stable gate config, post-`5f76bea`):

| corpus slice | rows | feature set | notes |
|---|---:|---|---|
| v0 (4-feature) | **5,076** | displacement, sigma_5min, t_remaining, market_p_up_normalized | v2 trained on 4,461 rows; v3 adds the 615 rows captured 2026-05-12 → 2026-05-15 (the recent regime where v2's drift opened up). Both fired (n=1,667) and skipped (n=4,069) decisions included — BTC-derived outcomes are fill-independent per v2 §"Empirical grounding". |
| v0.1 (book-depth features) | **1,667** | v0 + bids JSON parses | 11× the v2 design's "revisit v0.1 at 150" floor. Only the gated subset captures bids JSON; this is a deliberate restriction. v2 had ~78 bids-populated live rows at its training time, far short of any reliable v0.1 fit. |
| v2 column populated (dual-log slice) | 1,499 | n/a | Since 2026-05-09 — used to compare v3-vs-v2 directly without re-running v2 from scratch. |

Date range: 2026-04-24 → 2026-05-15, 21 days, fully covered across all UTC hours (53–100 fired rows/hour). At 60/20/20 chronological split on the v0 corpus: train ~3,045 / calibrate ~1,015 / **held-out ~1,015**. The held-out 0.80+ bin should populate with 150–300 rows depending on v3's distribution at the tail — plenty for tight bin-level claims; the held-out tail-bin support is *not* the bottleneck for v3 (it wasn't for v2 either — v2 had n=273 at 0.80–1.00 in its own held-out and still drifted in production). The bottleneck v3 has to address is **regime change between training and deployment**, which is the same risk v2 acknowledged but couldn't fix.

### What this plan does NOT claim to fix

- **Post-only adverse selection (−25.4pt filled-cohort gap).** Structural to the fill mechanism. No decision-time feature can predict whether the book will move adversely post-decision — confirmed by the regime-split exhaustive search in `_bmad-output/v2_failure_diagnostics_extra.md`. v3 may shift which decisions land in the filled cohort but does not change the conditional-distribution gap.
- **Live FAK execution-seam (racing/thinning at ack).** Tracked separately via the `book_at_ack` diagnostic (`polypocket/executor.py:412-435`) and the deferred Phase 3 live cohort plan. v3 may *partially* narrow the live −11.7pt DOWN gap, but the execution-seam contribution is unmeasurable until the next live cohort with `book_at_ack` populated.

## Goal

Ship `compute_model_p_up_v3` as a third L2 logistic model — **no isotonic by default** (v2's training revealed isotonic overfit on this corpus and pushed log-loss from 0.451 to 0.629; v3 carries that lesson forward, with isotonic re-tested only as an ablation now that the corpus is larger) — behind `MODEL_VERSION=v3`. Dual-logged in `model_p_up_v3` column alongside v1 and v2. Promoted to gate after passing reliability on a held-out slice and a dual-logged paper A/B against the live v2 model. Live cutover deferred to a follow-up plan after the post-only-retirement plan's FAK Phase-3 lands. Out of scope: model class changes (L2 logistic stays), regime-conditional models, online learning, backfilling outcomes for pre-G1 windows.

## Key design questions

### 1. Feature set — stay at v0 or expand to v0.1?

**Recommendation: ship v3 at v0.1 with the book-depth additions ablated, default-on if they lift held-out log-loss by ≥0.005 nats.**

The v2 design deferred book-depth because the bids-JSON corpus was 0/564 paper, 78/373 live at training time. It's now 1,667 paper rows under post-G1 capture — meaningfully above the 150-row floor v2 specified for revisiting. The candidates:

- **`book_imbalance = (up_top_size − down_top_size) / (up_top_size + down_top_size)`** — top-of-book size on the side-relevant pair. Hypothesis: book imbalance at decision predicts directional bias of the next 5 minutes, additive to the displacement z-score.
- **`spread = up_ask + down_ask − 1`** — book tightness; v2 design listed this as an ablation candidate. Should now run on the bigger corpus.
- **`pre_decision_pmc_sigma`** — std of pmc over `window_book_samples` rows in the same window with `sampled_at < decision_ts`. This is the "pre-decision book volatility" feature the post-only retirement review's Axis 2 named as the strongest unexplored axis. Captures a regime distinction the v2 features can't see.
- **`z_times_market`** — model-market interaction; carry-over from v2's ablation list.

**Ablation discipline:** each new feature gates on a held-out log-loss lift ≥0.005 nats *and* a reliability-gap improvement at the 0.80+ bins. No-lift features stay out — v3 must be defensible as "the smaller fix that works," not "the kitchen-sink rewrite."

### 2. Training corpus — full 5,076 (v0) or restricted-to-bids 1,667 (v0.1)?

**Recommendation: two corpora, two model variants, ship the better one.**

The v0 fit (5,076 rows, 4 features) is the conservative baseline — same feature set as v2 with 12× the data. Almost certainly closes the 0.80+ drift just by having tail support, regardless of feature engineering.

The v0.1 fit (1,667 rows, 4 + up to 4 new features) tests whether book-depth features add value beyond more data. Trained on 33% the row count but with richer feature support.

Both go through identical train/calibrate/eval gates; the held-out log-loss + reliability comparison chooses the shipped variant. If v0.1 ties v0 on the gates, ship v0 (simpler, larger support). If v0.1 beats v0 on the 0.80+ bins specifically, ship v0.1 (the bin we're trying to fix is the gate). This is decided by the report, not pre-committed.

### 3. Train/calibrate/eval split

**Single chronological 60/20/20 on each corpus**, per v2 design's structure. v0: train ~3,045 / calibrate ~1,015 / held-out ~1,015. v0.1: train ~1,000 / calibrate ~333 / held-out ~333. Time-series 5-fold CV inside the training slice for L2 strength + feature-ablation selection. No shuffling, no leakage.

**Held-out date range:** the last ~4 days of paper data (2026-05-11 to 2026-05-15 approximately). This is the most recent regime; if v3 is going to be promoted to gate the next live FAK cohort, the held-out should look like what live will see.

### 4. Acceptance gate — what's tighter than v2's?

v2's gate was ±5pt per bin with size-conditional CI fallback at the tail. v3's gate inherits that structure with three additions:

- **Tail-bin must beat v2 point-estimate.** At 0.80–0.85: v3 gap point estimate must be **better than v2's −10.2pt by ≥5pt** (i.e., v3 ≥ −5pt). At 0.85–0.90: better than v2's −13.8pt by ≥8pt (i.e., v3 ≥ −6pt). This is the bin we're refitting to fix — half-measures fail.
- **DOWN-side overall gap ∈ [−5pt, +5pt].** Tighter than the retirement plan's ±7pt blocker because v3's whole point is calibration tightness. v2's current DOWN gap is −4.6pt; v3 should hold or improve.
- **Log-loss improvement on held-out.** v3 held-out log-loss must beat v2's evaluated on the same held-out rows by ≥0.005 nats. (v2's current Brier on the 30-day paper window is 0.1269 per `scripts/_model_health.md`; held-out log-loss is the comparable continuous metric.) Smaller deltas are inside CI noise at n≈1,000.

**Pre-committed veto:** if v3's simulated PnL on the held-out is *worse* than v2's by a 95% bootstrap CI entirely below zero (same as v2's veto rule), ship is blocked regardless of calibration metrics.

### 5. DOWN-side asymmetry — feature or training-side rebalance?

**Recommendation: side-aware training, not a side-conditional feature.**

The v2 model produces `p_up` independent of which side the bot will trade; the side is decided downstream. Adding `preview_side` as a feature would leak gate-decision logic into the model. The cleaner fix is to confirm v3 is trained on **both up-leaning and down-leaning rows** with their actual outcomes — which the v0 corpus naturally is (1,727 down-side decisions, 3,349 up-side, ~62%/58% win rates respectively). Class balance is reasonable; no rebalancing weights needed.

If after training v3 still shows ≥5pt DOWN-vs-UP gap divergence, that's a *model* issue not fixable by features alone — at that point we revisit either (a) a side-conditional intercept term (lightest fix) or (b) two separate logistics (heavier). Don't pre-commit to either; let the held-out report drive it.

### 6. What about regime-conditional models (sigma quintile, displacement quintile)?

Out of scope, same as v2 design. With 5,076 rows split 5 ways you'd have ~1,000/regime which is enough in principle, but the cross-validation surface explodes and the v2 design's "kitchen-sink risk" argument still holds. A single L2 logistic with appropriate features is the v3 swing; regime-conditional is v4 territory if v3 still misses.

### 7. Dual-logging columns and back-compat

v3 ships a new `model_p_up_v3 REAL` column on `window_snapshots`. The three coexisting columns become:

- `model_p_up` — the version that actually fired the trade (back-compat, mutates over time as `MODEL_VERSION` flips).
- `model_p_up_v1_calibrated` — v1, always.
- `model_p_up_v2` — v2, always.
- `model_p_up_v3` — v3, always (NEW).

Reader scripts (`scripts/_model_health.py`, `_bmad-output/v2_failure_diagnostics_*.py`) read the version-specific columns directly, never `model_p_up`. Same discipline as v2.

### 8. Promotion sequence — paper A/B then live?

Same as v2 design's Phase A/B: dual-log v3 unconditionally; gate stays on v2 until the A/B report shows v3 passes the same reliability gate on ≥200 fresh decisions with the 0.80+ bin n≥30 (higher floor than v2's n=20 because we have more data). Then flip `MODEL_VERSION=v3` in paper.

**Live promotion is NOT part of this plan.** Live promotion happens only after:
1. v3 passes paper A/B.
2. The post-only-retirement plan's Phase 3 (deferred live FAK cohort) lands as a separate plan and runs successfully.
3. A combined v3+FAK live cohort plan is written.

This three-step deferral is deliberate. Conflating "new model" with "new execution mode" on live capital is the kind of confounded experiment the v2 paper-vs-live transfer risk warned against.

## What can break

| Failure mode | Severity | Mitigation |
|---|---|---|
| **v3 closes the 0.80–0.85 / 0.85–0.90 bins but opens a new miss elsewhere.** Often happens with isotonic calibration on small bins — moving probability mass around. | Medium | Acceptance gate (§Q4) is bin-level for all n≥30 bins, not just the tail. If a middle bin regresses, gate fails. |
| **v0.1 features overfit on n=1,667 with up to 8 features.** Pre-decision book-vol could be especially leaky if the join is wrong. | Medium | Time-series 5-fold CV inside training slice catches in-fold overfitting; held-out is fresh data and exposes out-of-distribution. Each new feature gates on log-loss lift ≥0.005 nats — small lift = stays out. |
| **Held-out gate passes but v3 drifts in production the same way v2 did.** v2 had a clean held-out gate (+1.3pt at 0.80–1.00, n=273) and still drifted to −9 to −14pt within weeks of deployment. v3 cannot rule this out from a single training pass on the same kind of corpus. | Medium | The paper A/B (Phase 3) is the primary check — v3 must hold its calibration on a *fresh* slice generated under its own gate decisions, not just on the chronologically-newest training split. Add a post-promotion monitoring step to the runbook: `scripts/model_health.py` produces v3 reliability tables weekly, and any 0.80+ bin gap exceeding −5pt for two consecutive weeks triggers a refit conversation. |
| **Tail-bin n≥30 still not met under v3's distribution.** If v3 produces fewer ≥0.85 rows than v2 (e.g., shrinks confidence toward the mode), the tail bin we're trying to gate stays sparse. | Low | v2's training held-out had n=273 at 0.80–1.00; v3 trains on a slightly larger corpus with the same target distribution. Tail-bin starvation is unlikely. Escape hatch carried over from v2's design as defense-in-depth: if A/B tail bin hasn't reached n=30 in 7 days, flip paper bot to `MODEL_VERSION=v3` for 2 additional days so v3's gate populates v3's tail. |
| **Held-out is contaminated by the same regime we're fitting.** 21 days isn't a lot of regime variation; a 4-day held-out at the tail of that window is even less. v3 could be over-fit to the May 2026 microstructure regime. | Medium | Documented as deployment risk in the runbook. Live promotion (separate plan) gates on a post-cutover monitoring window with explicit rollback. Not solvable inside this plan; the corpus is what it is. |
| **DOWN-side gap persists at ≥5pt even after refit.** Suggests the asymmetry lives in the signal-data interaction (BTC microstructure responds differently to up vs. down displacement) rather than in model fit. | Medium | Q5's two-fallback path: side-conditional intercept or two-separate-logistics. Both are gated on held-out evidence, not pre-committed. If neither helps, the next plan is feature engineering — log-spread asymmetry, time-of-day-conditional DOWN behavior. |
| **Refit closes paper FAK calibration but live FAK still shows the −11.7pt gap.** Most of the live gap was execution-seam, not model. | Low (expected) | Out of scope. The book_at_ack diagnostic + Phase 3 live cohort plan owns this. v3 closes only the model contribution. |
| **The v2 column stops being maintained after v3 ships.** A later reader script reads `model_p_up_v2` and gets stale or null data. | Low | Dual-logging discipline (§Q7): all three version-specific columns are populated unconditionally on every decision regardless of `MODEL_VERSION`. v2 column stays accurate until explicit retirement (separate cleanup, not this plan). |
| **Training pipeline regression vs v2's** (notebook drift, sklearn version drift, scaler differences). | Low | v3 uses the same training script structure as v2 (`scripts/train_model_v2.py` → `scripts/train_model_v3.py`). Single artifact: `polypocket/model_v3_coefs.json` with same schema as `model_v2_coefs.json`. Reproducibility test: re-running training from the corpus + seed produces identical coefficients. |
| **Model exporter has train/serve skew** — the bot's runtime `compute_model_p_up_v3` reads features differently than the training script does. | Medium | Same CI test discipline as v2: a unit test pins the feature dict that `compute_model_p_up_v3` receives and asserts it matches the training feature list. `market_p_up_normalized` re-derived from `up_ask` / `down_ask` at both training and serving, never read from `d.market_p_up` directly. |

## Files touched (preview)

| File | Change |
|---|---|
| `polypocket/model_v3_coefs.json` (NEW) | Fitted coefficients, intercept, isotonic breakpoints, training metadata block. ~5KB committed. |
| `polypocket/observer.py` | Add `compute_model_p_up_v3(features: dict) -> float`. Existing `compute_model_p_up`, `compute_model_p_up_v2`, and `compute_model_p_up_active` dispatcher stay. Dispatcher gains `v3` route. |
| `polypocket/config.py` | `MODEL_VERSION` default stays at `v2` until paper A/B passes; no change in this PR. After A/B promotion: change default to `v3` in a follow-up commit. |
| `polypocket/ledger.py` | Idempotent ALTER on `window_snapshots`: add `model_p_up_v3 REAL`. Extend the decision-snapshot exporter to populate it. |
| `polypocket/signal.py` | No behavioral change in this PR. The dispatcher in `observer.py` is the only edit point. |
| `scripts/train_model_v3.py` (NEW) | Training script. No `scripts/train_model_v2.py` exists in tree (v2 was trained from a one-shot notebook); v3's script is a new, committed reproducible artifact built around `scripts/export_training_corpus.py` (the existing parquet exporter, used as-is). v0 + v0.1 corpus paths, ablation harness, single-artifact output. |
| `scripts/_model_v3_training.md` (NEW, committed) | Training report: corpus summary, feature lists per variant, CV log-loss per fold, ablation results, held-out reliability tables, simulated EV comparison vs v2, ship recommendation. |
| `scripts/compare_model_versions.py` | Extend to read v3 column. Replace its hardcoded v1-vs-v2 logic with `--versions v2,v3` (or similar). |
| `scripts/_model_v3_paper_ab.md` (NEW, generated after dual-log A/B window) | Paper A/B report: v3 vs v2 on fresh decisions, per-bin reliability, promotion verdict. |
| `tests/test_observer.py` | New tests: `compute_model_p_up_v3` exists, accepts the documented feature dict, produces float in [0,1], and matches the coefficients JSON. Train/serve skew guard. |
| `docs/runbooks/model-versions.md` (NEW or extend if exists) | Document v3, the corpora variants, the rollback procedure (`MODEL_VERSION=v2` flips back instantly), and the known limitations (no fix for post-only adverse selection, no fix for live execution-seam). |

Explicitly NOT touched: any `signal.py` gate logic, `executor.py`, `risk.py`, `bot.py`, `feat/post-only-v2-design` branch contents, `live_trades.db` (read-only access for any live-vs-paper comparison reports).

## Validation plan

### Phase 1 (mandatory before ship): training-time held-out gate

1. **Corpus extraction.** Run `scripts/train_model_v3.py --corpus v0` and `--corpus v0.1` against `paper_trades.db`. Verify row counts match the expected 5,076 / 1,667. Date range printed in the report.
2. **Train both variants** with time-series 5-fold CV. Report per-fold log-loss, chosen L2 strength, chosen feature subset (for v0.1 — which book-depth features made the cut).
3. **Held-out reliability table** for each variant. Apply the §Q4 acceptance gate: ±5pt per n≥30 bin, tail-bin point estimate beats v2 by required margin (≥5pt at 0.80–0.85, ≥8pt at 0.85–0.90), DOWN gap ∈ [−5pt, +5pt], log-loss beats v2 by ≥0.005 nats.
4. **Pick a winner** based on Q4 criteria. If both pass, prefer v0 (simpler, larger support). If only v0.1 passes the 0.80+ tail-bin requirement, ship v0.1. If neither passes, halt; surface to user.
5. **Veto check.** Simulated PnL bootstrap CI on held-out — v3 must not be entirely below v2.

**Acceptance:** the chosen variant has a green held-out gate; `polypocket/model_v3_coefs.json` is committed with the report.

### Phase 2 (mandatory before paper-A/B promotion): integration

1. Wire `compute_model_p_up_v3` into the `observer.py` dispatcher.
2. Add `model_p_up_v3` column to `window_snapshots` (idempotent ALTER).
3. Extend the decision-snapshot exporter to populate the column on every decision, regardless of `MODEL_VERSION`.
4. `pytest` green, including the new train/serve skew guard test.
5. Smoke run in paper for 1 hour. Verify the v3 column is populating; verify `MODEL_VERSION=v2` still drives the gate; verify v3 numbers in the column are sensible (not all 0.5, not all 1.0, range and distribution match training).

**Acceptance:** dual-logging is live in paper; no regression in v2 gate behavior.

### Phase 3 (mandatory before live promotion plan unblocks): paper A/B

1. Wait for ≥200 fresh decisions with both `model_p_up_v2` and `model_p_up_v3` populated. At ~250–300 decisions/day in paper, ~1 wall-clock day.
2. Verify the 0.80+ v3 bin has n≥30. If not, escape-hatch: flip paper to `MODEL_VERSION=v3` for 2 more days to populate v3's own tail.
3. Run `scripts/compare_model_versions.py --versions v2,v3 --output scripts/_model_v3_paper_ab.md`.
4. Apply the same acceptance gate as Phase 1 on the A/B slice. Additionally: v3's gaps must not be ≥5pt worse than its Phase-1 held-out gaps (regime-drift guard).

**Acceptance:** A/B gate passes; v3 is promotable to paper gate. Flip `MODEL_VERSION=v3` default in `polypocket/config.py` in a follow-up commit (this is a small, reversible config edit, not a code change).

### Phase 4 (DEFERRED — separate plan): live cutover

Not part of this plan. The live-cutover plan depends on (a) v3 paper A/B passing, (b) post-only-retirement plan's Phase 3 (live FAK cohort) being written and approved, and (c) explicit user GO. Probably a combined "v3 + FAK live probe + small cohort" plan.

## Go / no-go criterion for the human

**GO if all hold:**

1. You agree the 0.80+ bin drift on the all-eligible paper cohort is a real model issue, separate from the post-only adverse-selection seam and from the live FAK execution seam.
2. You're comfortable with the "no model class change" constraint — staying on L2 logistic + isotonic, just refit with more data and tested feature additions.
3. The acceptance gate (§Q4) is the right tightness — specifically that v3 must *beat* v2 on the bins we're trying to fix, not just stay within ±5pt of perfect.
4. The deferred live cutover (Phase 4 separate plan) is the right operating discipline; you don't want to bundle "new model" with "live FAK promotion" on the same PR.

**NO-GO triggers — revise this design:**

- You want regime-conditional models (revise §Q6 to argue for separate volatility-regime fits). Heavier scope, but defensible at 5,076 rows.
- You want to try GBDT or similar (revise: v2 design's "out of scope" rule was on n=404; at n=5,076 it's plausible to revisit. Cost: more hyperparameters, more CV variance, harder to reason about feature importance).
- You want to refit while the post-only retirement plan is still in flight rather than waiting for it to merge (revise: probably fine since the corpora are independent, but it loses the "clean baseline" framing the sequencing was built on).
- You want to bundle live promotion into this plan rather than deferring to a separate plan (revise Phase 4: argue for combined v3-and-FAK-live scope; the v2 design's paper→live transfer caveat is the counter-argument).
- You want a different acceptance gate at the 0.80+ tail (revise §Q4: the margin numbers — ≥5pt at 0.80–0.85, ≥8pt at 0.85–0.90 — are proposals derived from v2's current gap; tighter or looser is a user-tolerance call).

**Decision required:** GO / NO-GO. If GO, the companion implementation plan covers: (a) `scripts/train_model_v3.py` build + corpus extraction + training run, (b) `model_v3_coefs.json` commit, (c) `observer.py` dispatcher + ledger ALTER + tests, (d) paper A/B report + verdict, (e) `MODEL_VERSION` default flip (follow-up commit after A/B), (f) memory updates. If NO-GO, your reason determines what changes — feature set, model class, scope, or gate tightness.
