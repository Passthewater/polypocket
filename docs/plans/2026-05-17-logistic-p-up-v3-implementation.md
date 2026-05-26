# Logistic p_up v3 — implementation plan

> **STATUS: HALTED 2026-05-18 at Step 4.** Steps 1–4 executed; both variants
> failed §Q4. Steps 5–12 not started. v2 remains the active gate; no
> production code changed. See `scripts/_model_v3_training.md` for the full
> negative-result report.

**Companion:** `2026-05-17-logistic-p-up-v3-design.md`. Read that first; this doc is the linear step-by-step build.

**Branch:** a new branch off `main` after the post-only-retirement PR has merged. Suggested name: `feat/logistic-p-up-v3`. Sequencing rule from the design: do not start until retirement is merged so the v3 work has a clean v2 baseline + the wallet watchdog in place.

**Test discipline:** the production-code surface is small — one new function in `observer.py`, an idempotent ledger ALTER, and one new test. The training script + report live in `scripts/` and don't run in CI. Run `pytest` after Step 7. Each step's "acceptance" is the concrete check that must pass before the next step starts.

**Total scope estimate:** ~1.5–2 days focused, plus 1–2 wall-clock days waiting on Phase 3 paper A/B after PR merges. Promotion (the `MODEL_VERSION` default flip) is a small follow-up commit.

**Dependencies installed:**
- `pip install -e ".[ml]"` — provides sklearn ≥1.3, pandas ≥2.0, pyarrow ≥14. The `ml` extra already exists in `pyproject.toml`; no new dependencies are added by this plan.

---

## Step 1 — sanity-check the existing corpus exporter, dump v0 + v0.1 parquets

**Files:** read-only check on `scripts/export_training_corpus.py`; produce two parquet files under a temp/working directory (not committed — these are derived, easily regenerated).

`scripts/export_training_corpus.py` already exists from v2 (verified 2026-05-17). It joins `decision` → `close` rows on `window_slug`, filters to `outcome IN ('up','down')` + non-null core features + `t_remaining > 0`, normalizes `market_p_up = up_ask / (up_ask + down_ask)`, and writes parquet. It also persists `up_bids_json` / `down_bids_json` per-row, which is what v0.1 needs.

1. Read `scripts/export_training_corpus.py` end-to-end. Confirm:
   - `DEFAULT_SINCE = "2026-04-24 00:00:00"` (the G1 cutoff).
   - `CORE_FIELDS` covers the v0 features (displacement, sigma_5min, t_remaining, up_ask, down_ask).
   - Bids JSON is persisted unconditionally (used by v0.1).
   - `outcome_int = 1 if up else 0` is the training label.
2. Run the exporter:
   - `python scripts/export_training_corpus.py --out _bmad-output/v3_corpus.parquet`
3. Inspect with a one-liner:
   - `python -c "import pandas as pd; df = pd.read_parquet('_bmad-output/v3_corpus.parquet'); print(df.shape, df['outcome_int'].mean(), df['decision_timestamp'].min(), df['decision_timestamp'].max(), df['up_bids_json'].notna().sum())"`
   - Expected: shape `(5076, 14)`, base rate ≈ 0.51, date span 2026-04-24 → 2026-05-15, bids-populated count ≈ 1667.

**Acceptance for Step 1:**
- `_bmad-output/v3_corpus.parquet` exists; row count matches the design's §"Corpus inventory" (5,076 v0 rows; 1,667 with bids).
- If the count is off by more than ±5%, halt and diagnose before training. A capture-pipeline regression between 2026-05-12 (when v2's corpus was extracted) and now would invalidate the corpus comparability.
- Note `_bmad-output/v3_corpus.parquet` should be **gitignored** if it isn't already (large derived file). Confirm: `git check-ignore -v _bmad-output/v3_corpus.parquet`. If not ignored, append `_bmad-output/*.parquet` to `.gitignore`.

---

## Step 2 — build `scripts/train_model_v3.py`

**Files:** `scripts/train_model_v3.py` (new), `polypocket/model_v2_coefs.json` (read-only — used as the comparison baseline).

The script is the **first committed reproducible training pipeline** for this project — v2 was trained from a one-shot notebook and the only artifact left behind is `_model_v2_training.md` plus the coefficients JSON. v3's training script needs to be re-runnable on the same corpus and produce identical coefficients (modulo random-seed determinism).

Structure (mirrors `_model_v2_training.md`'s sections so the v3 report drops into the same shape):

1. **Inputs:**
   - `--corpus` (default `_bmad-output/v3_corpus.parquet`).
   - `--variant` (choices: `v0`, `v0.1`, `both`; default `both`).
   - `--seed` (default `42`, matches v2).
   - `--out-coefs` (default `polypocket/model_v3_coefs.candidate.json` — *.candidate* until Step 4 chooses the winner and renames).
   - `--out-report` (default `scripts/_model_v3_training.md`).
   - `--baseline-coefs` (default `polypocket/model_v2_coefs.json` — for the v2-vs-v3 held-out comparison).
2. **Data loading and split:**
   - Load parquet, sort by `decision_timestamp`, drop rows with `t_remaining <= 0`.
   - Chronological 60/20/20: train = first 60%, calibrate = next 20% (unused for shipped no-iso variant; reserved for iso ablation), held-out = last 20%.
   - For v0.1: additional filter — `up_bids_json IS NOT NULL AND down_bids_json IS NOT NULL`, then apply the same 60/20/20 split.
   - Print row counts and date spans per split.
3. **Feature engineering:**
   - **v0 features:**
     - `z = displacement / (sigma_5min * sqrt(t_remaining / 300))` — guard against `t_remaining <= 0` (already filtered).
     - `t_remaining` (seconds, normalized internally by `StandardScaler`).
     - `sigma_5min`.
     - `market_p_up_normalized` (already in parquet as a column).
   - **v0.1 additions** (each gated on ablation lift):
     - `book_imbalance` = parse `up_bids_json[0]['size']` and `down_bids_json[0]['size']`, compute `(up_size - down_size) / (up_size + down_size)`. Default to 0 on parse failure (shouldn't happen post-filter).
     - `spread = up_ask + down_ask - 1`.
     - `pre_decision_pmc_sigma` — requires a second SQL query against `paper_trades.db` joining `window_book_samples` rows with `sampled_at < decision_ts`. Compute pmc from each sample's bids, take std. **Implementation note:** this is the only feature requiring a DB round-trip; cache the result back into the parquet at first run to avoid re-querying. If the per-window sample count is < 3, fall back to `sigma_5min` (proxy).
     - `z_times_market = z * market_p_up_normalized`.
4. **Standardization:** `StandardScaler` fit on the training slice (full-train scope, not per-fold — v2 design's documented choice; ~ε difference at n>1k per v2's own ablation `_model_v2_training.md:69-72`).
5. **CV inside training slice:** `TimeSeriesSplit(n_splits=5)`. Grid search L2 strength `C ∈ {0.1, 1.0, 10.0, 100.0}`. Pin `C=10.0` to match v2 if multiple are tied within 1e-4 log-loss (v2's documented heuristic, `_model_v2_training.md:9`). Report per-fold log-loss in the output.
6. **Held-out evaluation:** for each variant, compute on the held-out slice:
   - Log-loss, Brier.
   - Reliability table at the v3 design's bin widths: 0.50–0.60, 0.60–0.70, 0.70–0.80, 0.80–0.85, 0.85–0.90, 0.90–1.00. (Finer bins at the tail than v2's 0.80–1.00 because the v3 gate distinguishes 0.80–0.85 from 0.85–0.90 — that's the point of the refit.)
   - DOWN-side vs UP-side overall gap. (Side computed as `1 - p_pred` for outcome=down rows when matching the gate's downstream side decision; preserve v2's convention.)
   - Bootstrap 95% CI on each bin's actual win rate (1000 resamples).
7. **Apply the §Q4 acceptance gate** per the design doc:
   - All n≥30 bins: gap ∈ [−5pt, +5pt].
   - 0.80–0.85 bin: gap ≥ −5pt (i.e., better than v2's −10.2pt by ≥5pt).
   - 0.85–0.90 bin: gap ≥ −6pt (i.e., better than v2's −13.8pt by ≥8pt — adjusted because v2's number is on production, not v2's held-out).
   - DOWN-side overall gap ∈ [−5pt, +5pt].
   - Held-out log-loss < `v2_holdout_logloss − 0.005`.
   - PnL veto: bootstrap 95% CI on (v3_PnL − v2_PnL) on held-out is not entirely below zero. **Implementation note:** to compute this without a live gate, simulate fires using the gate config persisted in `gate_config_json` on each row — there's already a path to this in `polypocket/signal.py::evaluate_gate_config_*` (use it directly to avoid forking the gate logic).
8. **Ablations** (report-only, not gate-blocking):
   - v0 vs v0.1 head-to-head log-loss.
   - Each v0.1 feature individually (drop one, retrain, report delta).
   - With-isotonic vs no-isotonic — verify v2's "iso hurt" finding still holds on the larger corpus. If iso *helps* by ≥0.01 nats and doesn't fail the bin gate, ship v3 with isotonic and override the no-iso default. Note this in the report.
9. **Winner selection** (mechanical, no human-in-the-loop at this point):
   - If both v0 and v0.1 pass §Q4, ship **v0** (simpler, larger support).
   - If only one passes, ship the one that passes.
   - If neither passes, write the report anyway, exit with code 1, and surface the failure to the user via Step 5.
10. **Output:** the winning variant's coefficients to `--out-coefs` and the full report to `--out-report`. Report includes a `## Gate verdict` block at the top with `ship_ok: True|False` and the chosen variant name.

**Acceptance for Step 2:**
- Script exists and is end-to-end executable.
- A dry run on the parquet from Step 1 produces both files. Report is human-readable markdown; coefs JSON has the same top-level keys as `polypocket/model_v2_coefs.json` (the existing v2 file is the reference schema).
- Re-running the script with the same `--seed` produces a bitwise-identical coefs JSON. (Determinism check: run twice, `diff` the two outputs.)

---

## Step 3 — train and inspect the v0 variant

1. Run: `python scripts/train_model_v3.py --variant v0 --out-coefs polypocket/model_v3_coefs.v0.candidate.json --out-report scripts/_model_v3_training.v0.md`
2. Read the report. Verify the §"Gate verdict" block exists and reports clearly.
3. Cross-check the v2-on-v3-holdout numbers against `_model_v2_training.md` for sanity — v2's training held-out was 2026-05-09 → 2026-05-12, but v3's held-out is 2026-05-12 → 2026-05-15. Different rows, but the same regime; v2's evaluation on v3's held-out tests whether v2 generalizes to the recent slice. Expected: v2 evaluated on v3's held-out shows a worse log-loss than v2 on its own held-out. If not, the design's "regime drift" diagnosis is suspect and we should surface to user before continuing.

**Acceptance for Step 3:**
- v0 report written; gate verdict is one of `ship_ok: True` or `ship_ok: False` with a one-line summary of which criterion failed.
- v2-on-v3-holdout numbers sanity-check against the regime-drift hypothesis.
- No silent training failures (NaN coefs, all-zero gradients, etc.).

---

## Step 4 — train v0.1 and inspect ablations

1. Run: `python scripts/train_model_v3.py --variant v0.1 --out-coefs polypocket/model_v3_coefs.v01.candidate.json --out-report scripts/_model_v3_training.v01.md`
2. Read the report. Specifically check:
   - The per-feature ablation table — which book-depth features actually moved log-loss.
   - The v0.1 §"Gate verdict" — does v0.1 pass §Q4?
3. Compare v0 vs v0.1 directly. Note in the report which one is shipped.
4. **If both pass:** keep v0 (simpler). Delete the `v01.candidate.json`. Rename `model_v3_coefs.v0.candidate.json` → `polypocket/model_v3_coefs.json`. The shipped report is `scripts/_model_v3_training.md` (union of v0 + v0.1 sections).
5. **If only v0.1 passes:** keep v0.1. Rename `model_v3_coefs.v01.candidate.json` → `polypocket/model_v3_coefs.json`. Document the choice in the report.
6. **If both pass and v0.1 beats v0 on the 0.80–0.85 / 0.85–0.90 bins specifically by ≥2pt:** ship v0.1 (the bins we're refitting to fix are the gate).
7. **If neither passes:** halt this plan. The retirement still stands; the next plan is either a model class change (out of scope of v3) or a refit-with-regime-conditional-models (out of scope of v3). Surface to user.

**Acceptance for Step 4:**
- Both reports exist; the merged Step-5 report (after winner selection) is the committed artifact.
- Exactly one `polypocket/model_v3_coefs.json` (no `.candidate.` suffix, no v0/v0.1 marker — the file is the shipping artifact, the variant is recorded in the JSON's `metadata.variant` field and in the training report).
- The other variant's candidate file is deleted from the working tree.

---

## Step 5 — commit the training artifacts

**Files:** `scripts/train_model_v3.py`, `scripts/_model_v3_training.md`, `polypocket/model_v3_coefs.json`.

1. `git add scripts/train_model_v3.py scripts/_model_v3_training.md polypocket/model_v3_coefs.json`.
2. Commit message: `feat(model): train v3 logistic on n=5,076 post-G1 corpus`. Body references the design doc and names the chosen variant (v0 or v0.1) + the headline numbers (overall held-out log-loss, 0.80–0.85 bin gap, 0.85–0.90 bin gap, DOWN gap).
3. `git diff HEAD~1 --stat` to confirm exactly three new files.

**Acceptance for Step 5:**
- One commit on `feat/logistic-p-up-v3` with the three files.
- No production code changed yet — this commit is purely training artifacts.

---

## Step 6 — ledger ALTER for `model_p_up_v3` column

**Files:** `polypocket/ledger.py` (one block), `tests/test_ledger.py` (one new test).

1. Locate the existing schema migration block for `model_p_up_v2` in `polypocket/ledger.py`. Mirror its pattern for `model_p_up_v3 REAL`. Use the same `ALTER TABLE … ADD COLUMN … IF NOT EXISTS`-equivalent idempotent SQL (sqlite doesn't have `IF NOT EXISTS` for ADD COLUMN; the existing code uses a try/except wrapper — copy it).
2. Add a test in `tests/test_ledger.py` mirroring the existing v2 column test: open a fresh DB, run init, assert `PRAGMA table_info(window_snapshots)` includes `model_p_up_v3`.
3. Run the migration manually against both `paper_trades.db` and (if present) `live_trades.db`:
   - `python -c "from polypocket.ledger import init_db; init_db('paper_trades.db'); init_db('live_trades.db')"`
   - Verify with: `python -c "import sqlite3; c = sqlite3.connect('paper_trades.db'); print([r[1] for r in c.execute('PRAGMA table_info(window_snapshots)').fetchall() if 'model_p_up' in r[1]])"` — expected output includes `model_p_up_v3`.

**Acceptance for Step 6:**
- `model_p_up_v3` column present on `window_snapshots` in both DBs.
- New test passes.
- Re-running `init_db` is idempotent (doesn't fail because the column already exists).

---

## Step 7 — implement `compute_model_p_up_v3` + dispatcher update

**Files:** `polypocket/observer.py`, `tests/test_observer.py`.

1. In `polypocket/observer.py`, add `compute_model_p_up_v3(features: dict) -> float`:
   - Load coefficients lazily from `polypocket/model_v3_coefs.json` on first call; cache. Mirror the v2 implementation pattern verbatim.
   - Read the same feature dict shape v2 reads (the v3 design specifies no train/serve skew — features must be derived from values already on the decision row).
   - For v0: 4 features. For v0.1: 4 + N book-depth features per the chosen variant. The coefs JSON's `metadata.features` list is the source of truth for feature names + order.
   - Return a float in [0, 1]; clamp on numerical edge cases.
2. Extend `compute_model_p_up_active`'s dispatcher: add `elif model_version == "v3": return compute_model_p_up_v3(features)`.
3. Update the decision-snapshot exporter (the `_persist_decision_snapshot` or equivalent function in `polypocket/signal.py` or `polypocket/ledger.py` — locate by grepping for the line that writes `model_p_up_v2` into the row). Add a parallel `model_p_up_v3` write that always populates the column regardless of `MODEL_VERSION`. **Critical:** the v0.1 variant requires bids JSON at decision time — if `up_bids_json IS NULL OR down_bids_json IS NULL`, write `NULL` to `model_p_up_v3` and skip the call rather than passing zeros to the feature builder. This is a meaningful divergence from v2's unconditional dual-log.
4. New unit test in `tests/test_observer.py`:
   - `test_compute_model_p_up_v3_returns_float_in_range` — synthetic feature dict, assert 0 ≤ output ≤ 1.
   - `test_compute_model_p_up_v3_feature_list_matches_coefs` — load the coefs JSON, assert `metadata.features` == the feature dict keys produced by the production exporter for a sample decision. This is the **train/serve skew guard** from the v3 design's §"What can break" row 9.
   - `test_compute_model_p_up_v3_v01_returns_null_without_bids` — only if the shipped variant is v0.1. Pass a feature dict with `up_bids = None` and assert the wrapper returns None (or raises a sentinel exception caught by the exporter).

**Acceptance for Step 7:**
- The three new tests pass.
- `compute_model_p_up_active` correctly routes `MODEL_VERSION=v3` to the new function.
- Decision-snapshot exporter populates `model_p_up_v3` on every decision (or NULL when v0.1 features are unavailable).

---

## Step 8 — pytest + smoke run

1. Full pytest. Expected: baseline + 3 new tests + 1 ledger test = ~4 net new tests, all green.
2. Smoke run in paper for **1 hour minimum**:
   - Confirm `MODEL_VERSION` env var stays `v2` (the gate is unchanged — v3 is dual-logged only).
   - Tail the bot log for one or more decisions; verify the log line `Decision: ... model_p_up=...` shows the v2 value (not v3).
   - Query `paper_trades.db` after the smoke: `SELECT timestamp, model_p_up, model_p_up_v2, model_p_up_v3 FROM window_snapshots WHERE snapshot_type='decision' ORDER BY id DESC LIMIT 10;` — expected: `model_p_up_v3` is non-NULL on all rows (v0 variant) or non-NULL on most rows with bids available (v0.1 variant).
   - Verify v3 values are in a sensible range (not all 0.5, not all 0.99) — compare distribution to v2 column on the same rows. v3's mean should be within ±0.05 of v2's mean across the smoke rows.

**Acceptance for Step 8:**
- `pytest` green.
- Smoke run produces ≥10 decision rows with both `model_p_up_v2` and `model_p_up_v3` populated (for v0 variant) or ≥10 rows total with ≥80% having v3 populated (for v0.1 variant).
- Visual check: v3 values are sensible. No silent failure mode.

---

## Step 9 — commit integration + open PR

1. `git add polypocket/ledger.py polypocket/observer.py polypocket/signal.py tests/test_ledger.py tests/test_observer.py`. The exporter changes from Step 7 likely touch `signal.py` (or wherever `_persist_decision_snapshot` lives — locate before adding).
2. Commit message: `feat(model): integrate v3 dispatcher + dual-log + tests`. Body lists the test count delta and links to the design doc.
3. `git log feat/logistic-p-up-v3 --oneline` — expect 2 commits: training artifact (Step 5) + integration (Step 9).
4. `gh pr create` with title `feat: train v3 logistic, dual-log behind MODEL_VERSION`. Body:
   - References the design doc.
   - Names the shipped variant (v0 or v0.1).
   - Lists the headline calibration improvements vs v2 (0.80–0.85 bin gap delta, DOWN gap delta, held-out log-loss delta).
   - Explicitly states: **this PR does not flip the active model — `MODEL_VERSION=v2` remains the gate until Phase 3 paper A/B passes (post-merge).**

**Acceptance for Step 9:**
- PR exists, all CI checks pass.
- PR description makes the "v3 dual-logged, v2 still gates" status explicit so a reviewer doesn't worry the bot's behavior changed under them.

---

## Step 10 (post-merge, deferred ~1–2 days) — Phase 3 paper A/B

This is **not in the PR** — it runs after PR merge against the production paper data the deployed v3 dual-logger collects.

1. Build `scripts/compare_model_versions.py` (it does NOT exist on `main`; the v2 design proposed it and never shipped). The script:
   - Inputs: `--db paper_trades.db`, `--versions v2,v3`, `--out scripts/_model_v3_paper_ab.md`.
   - Queries `window_snapshots` for decisions where both `model_p_up_v2` and `model_p_up_v3` are non-null and the joined close row has an outcome.
   - Per-bin reliability for both versions side-by-side.
   - Simulated gate-fire PnL for both versions at current live config (reuse the simulation path from Step 2's training script).
   - Bootstrap 95% CI on the per-bin gap delta (v3 − v2) and on the simulated PnL delta.
   - **Promotion verdict per the design's §"Phase 3" gate:**
     - Same acceptance as Phase 1 (§Q4) on the fresh A/B slice.
     - v3 gaps must not be ≥5pt worse than v3's Phase-1 held-out gaps (regime-drift guard).
2. Wait for ≥200 fresh decisions with both columns populated. At paper's ~250–300 decisions/day this is ~1 wall-clock day.
3. If the 0.80+ v3 bin has n<30 at the 200-row mark, invoke the v3 design's escape hatch:
   - Flip paper to `MODEL_VERSION=v3` for 2 wall-clock days.
   - During this window, v3 drives the gate AND populates the v3 tail. v2 stays dual-logged.
   - At the end, re-run the comparison.
4. Run `python scripts/compare_model_versions.py --versions v2,v3 --out scripts/_model_v3_paper_ab.md`.
5. Read the verdict.

**Acceptance for Step 10:**
- A/B report exists at `scripts/_model_v3_paper_ab.md`.
- The §"Promotion verdict" block reports a clear GO or NO-GO.
- If GO → proceed to Step 11.
- If NO-GO → halt. Document the failure mode. v2 remains the gate; v3 stays dual-logged. The next plan is either v3.1 (feature additions, drift handling) or a model class change.

---

## Step 11 (post-A/B, GO only) — flip `MODEL_VERSION=v3` default

**Files:** `polypocket/config.py`. **Not** `.env` — the default lives in code, and `.env` is gitignored.

1. Edit `polypocket/config.py`: change `MODEL_VERSION = os.getenv("MODEL_VERSION", "v2")` to `MODEL_VERSION = os.getenv("MODEL_VERSION", "v3")`.
2. Run pytest. The change should be invisible to most tests; any test that pinned `MODEL_VERSION=v2` for cross-version comparison needs to explicitly set the env var via `monkeypatch.setenv("MODEL_VERSION", "v2")`. Locate and fix.
3. Restart the bot. Verify the first decision after restart logs `model_p_up` matching the `model_p_up_v3` column (i.e., v3 is the active gate).
4. Update the runbook (`docs/runbooks/model-versions.md` — created in Step 5 of the v3 design's §"Files touched", or appended if it exists from another plan):
   - Document the v3 promotion + paper A/B numbers.
   - Document the rollback procedure: `MODEL_VERSION=v2` in `.env` flips back instantly without code changes.
5. Commit: `chore(model): promote v3 to default MODEL_VERSION (Phase 3 paper A/B passed)`. Reference the A/B report.

**Acceptance for Step 11:**
- `MODEL_VERSION` default is `v3` in `polypocket/config.py`.
- pytest green.
- Bot restart confirmed; v3 is the active gate in paper.
- Runbook updated with the rollback procedure.

---

## Step 12 — memory updates

Update one memory and add one. Use `Write` against the memory files; pointer entries go in `MEMORY.md`.

1. **Update `[[project_live_v2_execution_gap]]`:** add a line at the end pointing to v3's promotion as the model-side mitigation. Keep the "do not refit" history visible — the constraint was correctly held until the new evidence emerged and v3 is the response.
2. **New memory `project_logistic_v3_promoted.md`:** type=project. Frontmatter `name: project-logistic-v3-promoted`, description names the version and date. Body: (a) what changed (v3 trained on n=5,076 paper post-G1; variant v0 or v0.1; ships no-isotonic per v2's iso-overfit finding); (b) why (regime drift on v2's 0.80+ bins, surfaced by the post-only retirement review); (c) what it does NOT fix (post-only adverse selection, live FAK execution seam — both are out of scope and tracked separately); (d) rollback procedure. `**Why:**` and `**How to apply:**` lines per the memory rules.
3. **Update `MEMORY.md`** with the new pointer line:
   ```markdown
   - [Logistic v3 promoted YYYY-MM-DD](project_logistic_v3_promoted.md) — refit on n=5,076 post-G1 corpus closes the 0.80+ regime drift; v2 stays as the rollback target
   ```

**Acceptance for Step 12:**
- Two memory files updated, one added.
- `MEMORY.md` has the new pointer.

---

## What is NOT in this PR (or this plan)

- **Live cutover to v3.** Requires (a) v3 paper A/B passing (Step 10), (b) the post-only-retirement plan's Phase 3 (live FAK cohort) being written and run, (c) a combined "v3 + FAK live cohort" plan that the user explicitly approves.
- **Retiring v1 and v2 columns.** Both stay populated indefinitely as rollback targets. v1 retirement was originally proposed in the v2 design as a post-2-week cleanup; that cleanup is still deferred (and `MODEL_VERSION=v1` is the deepest rollback option if both v3 and v2 are somehow compromised).
- **Backfilling `model_p_up_v3` on pre-2026-05-17 rows.** v3's feature derivations don't depend on anything not already on the decision row, so it would be technically possible — but the value is unclear and the storage is non-trivial. Skip; v3 column populates forward only.
- **GBDT, regime-conditional models, online learning.** Same out-of-scope as the v3 design.
- **Touching the post-only retirement work**, `feat/post-only-v2-design`, or the wallet watchdog.

---

## Estimated effort

| Step | What | Time |
|---:|---|---:|
| 1 | Corpus sanity-check | ~20 min |
| 2 | Build training script | ~3–4 hr |
| 3 | Train + inspect v0 | ~30 min (incl. CV runtime) |
| 4 | Train + inspect v0.1 + ablations | ~1 hr |
| 5 | Commit training artifacts | ~10 min |
| 6 | Ledger ALTER + test | ~30 min |
| 7 | Observer dispatcher + tests | ~1 hr |
| 8 | pytest + smoke run | ~1.5 hr (1 hr is the smoke wait) |
| 9 | Commit + PR | ~15 min |
| 10 | Phase 3 paper A/B (post-merge) | ~1–2 wall-clock days; ~2 hr active work |
| 11 | MODEL_VERSION flip (post-A/B GO) | ~30 min |
| 12 | Memory updates | ~20 min |

**Total active focused work:** ~8–10 hours.
**Calendar:** ~3 days end-to-end (1 day for Steps 1–9, 1–2 days waiting for A/B, ~1 hour for Steps 11–12).

The gating risks are Step 4 (does the v0 or v0.1 variant actually pass §Q4?) and Step 10 (does v3 hold up on fresh paper data?). If Step 4 fails, the plan halts and surfaces to user before any production code is touched. If Step 10 fails, v2 remains the gate and v3 stays as dual-logged data without a default flip — no rollback needed because nothing changed in production gating.
