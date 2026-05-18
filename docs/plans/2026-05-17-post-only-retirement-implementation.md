# Post-only retirement — implementation plan

**Companion:** `2026-05-17-post-only-retirement-design.md`. Read that first; this doc is the linear step-by-step build, no design discussion.

**Branch:** a new branch off `main`. **Do NOT use** `feat/post-only-v2-design`. Suggested name: `feat/post-only-retirement`.

**Test discipline:** the only new code paths in this PR are two scripts under `scripts/` and one unit test. Run `pytest` after Step 4. Each step's "acceptance" is the concrete check that must pass before the next step starts.

**Total scope estimate:** half a day, mostly script work. No production code changes.

---

## Step 1 — Phase 1 script: paper FAK calibration replay

**Files:** `scripts/fak_paper_calibration.py` (new), `scripts/_fak_paper_calibration.md` (output, committed).

The script reproduces the post-cutover v2-only paper FAK calibration analysis from `_bmad-output/v2_failure_diagnostics_modelver.py`, but as a committed reusable artifact (no scratch dependency).

1. Inputs:
   - `--db` (default `paper_trades.db` via `polypocket.config.PAPER_DB_PATH`).
   - `--cutoff` (default `2026-04-24 00:00:00` — the MODEL_VERSION=v2 cutover).
   - `--p-column` (default `model_p_up_v2` — read the v2 column directly to avoid pre-cutover mixing).
   - `--bin-width` (default `0.05`).
   - `--out` (default `scripts/_fak_paper_calibration.md`).

2. Query pattern (mirror `v2_failure_diagnostics_modelver.py::load`):
   ```sql
   SELECT ws.window_slug, ws.preview_side, ws.model_p_up_v2,
          ws.timestamp, t.outcome
   FROM window_snapshots ws
   LEFT JOIN trades t ON t.window_slug = ws.window_slug
   WHERE ws.snapshot_type = 'decision'
     AND ws.trade_fired = 1
     AND ws.model_p_up_v2 IS NOT NULL
     AND ws.timestamp >= ?
   ```
   The `trade_fired = 1` filter is the FAK-equivalent — in paper mode the bot fills every eligible decision at market. No book-cross simulation is needed for FAK calibration: paper FAK *is* the all-eligible cohort.

3. Report sections (mirror v2 design's replay report shape):
   - **Top-line:** n_settled, win_rate, mean_p_pred, gap, Brier.
   - **By confidence bin** (0.50–1.00 at 0.05 width): n, mean p_pred, hit rate, gap. Mark bins n < 20 as "small".
   - **By side** (up/down): n_settled, win_rate, mean p_pred, gap.
   - **By UTC-band slice** (memory's 19:40–02:25 band): same top-line for direct cross-check against memory `[[project_live_v2_execution_gap]]`'s Brier 0.1167.
   - **Acceptance verdict (the headline output):**
     - Overall n ≥ 500 settled decisions → PASS/FAIL.
     - Every bin with n ≥ 20 has gap ∈ [−10pt, +10pt] → PASS/FAIL.
     - No bin with n ≥ 20 has gap < −15pt → PASS/FAIL.
     - DOWN-side overall gap ∈ [−7pt, +7pt] → PASS/FAIL.
     - Overall **GATE: PASS** only if all four pass.

4. Exit code: 0 on PASS, 1 on FAIL. Allows CI gating later if useful.

5. No tests for this script — it's a one-shot reporter against an immutable DB. If the user wants tests, factor the bin/gap logic into a tiny pure-function module and unit test that, but for "minimum change" skip the test.

**Acceptance for Step 1:**
- Script runs end-to-end against `paper_trades.db` without crash.
- Output file `scripts/_fak_paper_calibration.md` is committed.
- Output GATE verdict is one of PASS or FAIL. Both outcomes proceed to the next step — the verdict gates the *.env flip* in Step 5, not the script's execution.

---

## Step 2 — Phase 2: executor code-path audit

**Files:** read-only (no edits).

Before writing the depth-support script, confirm the diagnostic emits the payload it claims to. This is a 10-minute sanity check that's load-bearing for any future live cohort.

1. Open `polypocket/executor.py` and locate the FAK path's `ack` event log site. Confirm it includes `book_at_ack` in the payload dict (commit `a98e76c` introduced this).
2. Search the test suite (`tests/test_executor.py`) for a test that verifies `book_at_ack` makes it into `payload_json`. If none exists, note it as a gap.
3. Search for any conditional that could silently skip the `book_at_ack` block (e.g., a try/except that swallows everything, an env-var gate, a `if TRADING_MODE == ...` branch that excludes live).

**Acceptance for Step 2:**
- `book_at_ack` is emitted unconditionally on the FAK ack event when the snapshot fetch succeeds. (Fetch-failure is swallowed by design per the comment around line 410.)
- If the audit finds a regression (e.g., a refactor broke the emit path), STOP — file the bug fix as a separate PR. Do not proceed with the retirement until the diagnostic is wired.

---

## Step 3 — Phase 2 script: depth-support analyzer

**Files:** `scripts/fak_ack_depth_retrospective.py` (new), `tests/test_fak_ack_depth_retrospective.py` (new), `scripts/_fak_ack_depth_retrospective.md` (output, committed).

The script computes depth-support per fill. Today's data: zero fills with `book_at_ack`. The script must produce a sensible "no data" report and be ready for future live data.

1. Inputs:
   - `--db` (default `live_trades.db`).
   - `--min-depth-usdc` (default `0.0` — the binary existence check).
   - `--out` (default `scripts/_fak_ack_depth_retrospective.md`).

2. Query: `SELECT trade_id, window_slug, payload_json FROM order_events WHERE event_type='ack' AND payload_json LIKE '%book_at_ack%'`. JSON-parse and join to `trades` for `side`, `entry_price`, `outcome`.

3. Per-fill depth-support computation:
   - Pull `book_at_ack.up_book` and `book_at_ack.down_book` (each is a top-N list `[{price, size}, ...]`).
   - Our limit price at ack time is `trades.entry_price` (the FAK limit; with `IOC_BUFFER_TICKS` already baked in).
   - For a BUY UP fill, "depth at-or-below our limit" = sum of `size` across `down_book` entries where `price ≤ entry_price` (the down-side bids that match against our up-side IOC pair-merge).
   - Mirror for BUY DOWN against `up_book`.
   - Convert to USDC: `size * price`.
   - Depth-supported: `depth_usdc ≥ MIN_DEPTH_USDC`.

4. Report sections:
   - Total `ack` events with `book_at_ack` populated (expected: 0 against today's `live_trades.db`).
   - If 0: emit "Diagnostic landed 2026-05-15 23:25 EDT, after last live trade in this DB. Rerun after next live cohort." Exit 0.
   - If > 0: per-fill table (fill_id, side, limit_price, depth_usdc_at_or_below_limit, depth_supported, outcome), summary rate by threshold (0, $10), side split.

5. Unit test (`tests/test_fak_ack_depth_retrospective.py`):
   - Single test `test_depth_support_with_synthetic_payload`:
     - Build a `book_at_ack` dict by hand with known sizes/prices.
     - Call the per-fill depth function with side=up, limit=0.55, payload.
     - Assert returned `depth_usdc_at_or_below_limit` equals the hand-computed sum.
   - No DB tests; the script-level test is exercised by Step 4.

**Acceptance for Step 3:**
- Script runs against `live_trades.db` and reports the "no data" message (`0/40 ack events` or similar).
- Unit test passes.
- `pytest` total count goes up by 1; all existing tests still green.

---

## Step 3.5 — cherry-pick the wallet-balance watchdog from the v2 branch

**Files (incoming):** `polypocket/risk.py` (watchdog helpers), `polypocket/bot.py` (new `_check_wallet_divergence` hook on `_on_book_update`), `polypocket/config.py` (new `WALLET_LEDGER_DIVERGENCE_HALT_USDC=5.0`), `tests/conftest.py` (config key registration), `tests/test_risk.py` and any other test files touched by the watchdog commits, `docs/runbooks/post-only-live-cohort.md` (the v2-branch watchdog notes — overwritten in Step 7 below).

The wallet watchdog is currently only on `feat/post-only-v2-design` (commits `9790763` "feat(risk): wallet-balance watchdog with halt-on-divergence" and `3449e55` "fix(v2): wallet-watchdog cost SUM + staleness anchor"). The post-only-retirement PR brings it onto `main` because returning to FAK without it leaves the bot worse-protected against silent ledger-vs-wallet divergence — the v1 cohort's failure mode.

1. From `feat/post-only-retirement`, cherry-pick the two commits in order:
   - `git cherry-pick 9790763 3449e55`
   - Expect conflicts only on `docs/runbooks/post-only-live-cohort.md` (the v2 watchdog runbook section overlaps with Step 7's retirement section). Resolve by keeping the watchdog content and deferring the retirement append to Step 7.
   - If any post-only-v2-specific code accidentally rides along (`POST_ONLY_REPOST_ON_DRIFT_TICKS`, `_check_post_only_drift`, the `place_time_pmc` column ALTER, the `post_only_v2` ENTRY_MODE branch in `bot.py`), revert those specific hunks before the cherry-pick lands — they belong on the v2 branch. The watchdog is independent of all of them.
2. Verify scope by `git diff main..HEAD --stat` — expected files: `polypocket/risk.py`, `polypocket/bot.py`, `polypocket/config.py`, `tests/conftest.py`, `tests/test_risk.py` (or equivalent), `tests/test_bot.py` (if the wallet check has a bot-level test), `docs/runbooks/post-only-live-cohort.md`. Anything else means a v2 execution-path hunk slipped in — revert it.
3. Run full pytest. Expect the v2 branch's test additions (the watchdog tests) to pass on top of main's 399. If anything fails, the issue is almost certainly a missing dependency (e.g., a v2-only helper imported by the watchdog code) — diagnose and either inline the dependency or, if it's a larger v2-only refactor, abort the cherry-pick and document the regression in Step 7's runbook append instead.

**Acceptance for Step 3.5:**
- Cherry-pick clean (modulo the runbook conflict, resolved by deferral).
- `pytest` green. Test count goes up by the number of watchdog tests on the v2 branch (~5–10 tests, based on the design's §"What can break" enumeration).
- `git diff main..HEAD --stat` lists only watchdog files plus the Step 1–3 artifacts.
- `WALLET_LEDGER_DIVERGENCE_HALT_USDC` is present in `polypocket/config.py` with default `5.0`.
- `_check_wallet_divergence` is reachable from `polypocket/bot.py::_on_book_update` and only fires when `TRADING_MODE=live` (per the v2 design's §"Q5").
- If the cherry-pick *cannot* be completed cleanly, halt this step, document the failure as a `## Known regression` block in Step 7's runbook append, and proceed with the .env flip on the explicit understanding that the bot is unprotected against the v1-cohort-class bug until a follow-up PR lifts the watchdog.

---

## Step 4 — pytest + commit Steps 1–3.5 as PR-prep commits

1. Run full pytest. Expect: 399 baseline + 1 from Step 3 + ~5–10 from Step 3.5 = roughly 405–410 green.
2. Two commits on `feat/post-only-retirement`:
   - First: `git add scripts/fak_paper_calibration.py scripts/_fak_paper_calibration.md scripts/fak_ack_depth_retrospective.py scripts/_fak_ack_depth_retrospective.md tests/test_fak_ack_depth_retrospective.py` → `feat(scripts): FAK paper calibration + depth-support tooling (post-only retirement Phase 1+2)`.
   - The Step 3.5 cherry-picks (`9790763`, `3449e55`) are already separate commits from the cherry-pick — leave them as-is so the watchdog history stays attributable.
3. Reference the design doc in the new commit's message.

**Acceptance for Step 4:**
- Test count matches expectation.
- `git log feat/post-only-retirement --oneline` shows three commits: the two cherry-picked + one new tooling commit.

---

## Step 5 — read Phase 1 report, check the two real blockers

This is a *read-and-decide* step, not a code step. The design's Phase 1 reframe pre-commits the plan to option (a) — accept high-confidence bin drift and proceed. Phase 1's role here is to surface either of two specific NEW signals that would block the flip; if neither fires, proceed.

Read `scripts/_fak_paper_calibration.md` and check the two blockers from the design's §"Phase 1":

1. **DOWN-side per-bin regression.** Scan the DOWN-side n≥20 bins; if any bin's gap is worse than the overall DOWN gap by ≥10pt, that's a side-asymmetric model failure not visible in the overall DOWN summary. Surface to user before proceeding.
2. **Overall DOWN gap drift.** If the report's overall DOWN gap is outside [−7pt, +7pt], the model has drifted relative to the existing diagnostic's −4.6pt. Surface to user before proceeding.

If neither blocker fires, the bin-level drift at 0.80–0.85 (−10.2pt) and 0.85–0.90 (−13.8pt) is **expected and accepted**. Proceed to Step 6.

If either blocker fires, halt this plan at Step 5. Document the blocker as a one-paragraph addendum at the bottom of `_fak_paper_calibration.md` and surface to the user; do not flip `.env`. The retirement decision still stands; the choice of replacement (paper-only-indefinitely, confidence ceiling, refit) becomes a follow-up plan informed by the new evidence.

**Acceptance for Step 5:**
- Phase 1 report exists and has been read.
- Either: no blocker fires → Step 6 unblocked. Or: a blocker fires → addendum written, user surfaced, Step 6 skipped, Steps 7–9 still run (the watchdog lift, runbook update, and memory updates are unaffected by the .env flip).

---

## Step 6 — flip `.env` ENTRY_MODE and restart the bot

**Files:** `.env`. **Not part of the PR commit** — `.env` is gitignored (verified: `.gitignore:6`). This is a local-machine edit + a bot-process restart.

Conditional on Step 5 not surfacing a blocker. Skip if a blocker fired.

1. Edit `.env`: `ENTRY_MODE=post_only` → `ENTRY_MODE=fak`.
2. Confirm `POST_ONLY_REST_OFFSET_TICKS=1` and other post-only env vars stay (harmless when the code path isn't dispatched; removing them is a separate cleanup). Confirm `TRADING_MODE` remains whatever it currently is (paper per `[[project_live_unprofitable]]`).
3. **Restart the bot process.** The `.env` flip does not propagate to a running process; the next decision tick under the old config still uses `ENTRY_MODE=post_only`. Whichever supervisor or launcher you use (systemd, tmux session, `python -m polypocket.bot`), stop and re-launch.
4. On first tick after restart, verify the bot dispatches FAK. Two confirmation paths:
   - `grep -n "ENTRY_MODE" .env` shows `ENTRY_MODE=fak`.
   - The first decision after restart logs `execute_paper_trade` (paper FAK) — not `execute_paper_trade_post_only`. Tail the log for one decision cycle.

**Acceptance for Step 6:**
- `.env` shows `ENTRY_MODE=fak` (verified by grep).
- Bot process is restarted; the next decision in the bot log shows the FAK code path, not the post-only one.
- No commit is created for this step — `.env` is gitignored.

---

## Step 7 — runbook append

**Files:** `docs/runbooks/post-only-live-cohort.md`.

Append a clearly-dated retirement section so future operators understand the state:

```markdown
## Retired 2026-05-17

The post-only entry path (v1 merged in #24, v2 designed on `feat/post-only-v2-design`)
is operationally retired as of 2026-05-17. ENTRY_MODE flipped from `post_only` back to `fak`.
The wallet-balance watchdog originally Step 6 of the v2 design was lifted onto `main` as
part of this PR (commits `9790763` + `3449e55`) — it is execution-mode-independent and
addresses the silent ledger-vs-wallet divergence that affected the v1 cohort.

### Reason

Diagnostic at `docs/plans/2026-05-17-post-only-retirement-design.md` shows the post-only
fill mechanism is adversely selected at a structural level against this signal:

- Paper post-cutover v2-only would-have-fill cohort: 46.2% wr at 0.715 predicted (gap −25.4pt).
- Same-decision would-NOT-fill cohort: 77.7% wr at 0.763 predicted (gap +1.4pt).
- No regime split (sigma, |displacement|, t_remaining, confidence floor) narrows the gap.

The v2 ship gate failed on Step-7 paper replay (`scripts/_post_only_v2_replay.md`, winrate
55.9% < 70% at OFFSET=1 with the full v2 lifecycle — corroborates the v1-mechanic diagnostic).
The v2 branch `feat/post-only-v2-design` remains intentionally unmerged as a frozen reference
(399 tests green at commit `6e440d3`). Do not rebase, do not delete.

### Known carry-over risks (NOT addressed by this retirement)

- Bin-level paper FAK calibration drift at p≥0.80 (0.80–0.85 gap −10.2pt n=125; 0.85–0.90
  gap −13.8pt n=131 on the all-eligible v2 cohort). Independent of the post-only adverse
  selection; will affect FAK live decisions. Tracked as a refit-trigger candidate.
- DOWN-side asymmetry (−4.6pt overall under v2 on the all-eligible cohort, −11.7pt on the
  live FAK n=20 cohort). The signal degrades on DOWN regardless of execution mode.

### Conditions under which post-only could be revisited

A future signal change (refit, new features, alternative model architecture) that produces
a model whose post-only filled subset is calibrated to within ±5pt of paper-overall. At that
point the v2 branch is the starting point — the lifecycle, drift detection, repost throttle,
and wallet watchdog implementations are all complete and tested.

### Diagnostic artifacts

- `_bmad-output/v2_failure_diagnostics.md` — full-corpus partition (D1–D4).
- `_bmad-output/v2_failure_diagnostics_extra.md` — regime splits (E1–E4).
- `_bmad-output/v2_failure_diagnostics_modelver.md` — post-cutover v2-only verification.
- `scripts/_fak_paper_calibration.md` — paper FAK calibration replay (Phase 1 of this plan).
- `scripts/_fak_ack_depth_retrospective.md` — depth-support tooling (Phase 2, awaiting data).
```

**Acceptance for Step 7:**
- The runbook has a "Retired 2026-05-17" section at the bottom.
- All four diagnostic artifact paths in the section actually exist.

---

## Step 8 — memory updates

Update two memories and add one. Use the Write tool against the memory files; pointer entries go in `MEMORY.md`.

1. **Update `[[project_v2_replay_gate_failed]]`** at `C:\Users\Matt\.claude\projects\C--Users-Matt-polypocket\memory\project_v2_replay_gate_failed.md`:
   - Add a line at the end pointing to `[[project_post_only_retired]]` and `docs/plans/2026-05-17-post-only-retirement-design.md` as the follow-up.
   - Keep the "v2 code on `feat/post-only-v2-design` is reviewed and tested" line; that's still true.
2. **New memory `project_post_only_retired.md`:** type=project. Frontmatter `name: project-post-only-retired`, description names the operational state succinctly. Body covers: (a) what was retired (post-only v1 in `.env`, v2 left unmerged); (b) why (the structural −25.4pt filled-cohort gap); (c) when revisit is appropriate (after a signal change that calibrates the filled subset); (d) link to the design doc. Add `**Why:**` and `**How to apply:**` lines per the memory-writing rules.
3. **Update `[[project_live_v2_execution_gap]]`** at `project_live_v2_execution_gap.md`:
   - Correct the line "Ack-time book diagnostic landed 2026-05-16" — the commit `a98e76c` landed 2026-05-15 23:25 EDT (03:25 UTC 2026-05-16), but it landed *after* the live cohort ended at 02:17 UTC on 2026-05-16. So the live cohort itself has zero `book_at_ack` payloads. Reword to "diagnostic landed in commit a98e76c (2026-05-16 03:25 UTC), after the live cohort. Data available only on next live run."
   - Link to `[[project_post_only_retired]]`.
4. **Add pointer entries** in `MEMORY.md`:
   ```markdown
   - [Post-only retired 2026-05-17](project_post_only_retired.md) — ENTRY_MODE flipped to fak; v2 branch stays unmerged as a frozen reference
   ```

**Acceptance for Step 8:**
- The three memory files are updated.
- `MEMORY.md` has the new pointer line.
- All `[[name]]` links resolve to existing files.

---

## Step 9 — open PR

1. `git status` — confirm only the files listed in design §"Files touched" appear (Step 1+3 tooling + Step 3.5 watchdog cherry-pick + Step 7 runbook). `.env` is gitignored and the memory updates from Step 8 live outside the repo.
2. `git log feat/post-only-retirement --oneline` — expect **4 commits**: the two cherry-picked watchdog commits (`9790763`, `3449e55`) at the bottom, the tooling commit from Step 4 above them, and the runbook commit from Step 7 on top.
3. `gh pr create` with title `chore: retire post-only entry mode, restore FAK default + lift wallet watchdog`. Body references the design doc, summarizes the Phase 1 outcome (blocker fired or not), and explicitly calls out that the wallet-watchdog cherry-pick is bundled into this PR per the design's Q7.
4. Do NOT open a PR for `feat/post-only-v2-design`. That branch stays as-is.

**Acceptance for Step 9:**
- PR exists, all CI checks pass (the watchdog tests run on `main` for the first time — confirm they pass without any v2-branch-only fixtures).
- The PR body explicitly names the watchdog lift as part of scope.
- Reviewer sees the design doc link as the primary context.

---

## What is NOT in this PR

- Phase 3 (live FAK cohort under the ack-time diagnostic) — separate plan, gated on Phase 1 reading green and user GO.
- Any `MAX_MODEL_CONFIDENCE` ceiling — only enters scope as a follow-up if the deferred Phase-3 plan needs it.
- Any model refit. The new bin-level evidence at p≥0.80 may justify re-opening the "do not refit" constraint, but only as a *separate* plan triggered by user decision.
- Any change to `signal.py`, `executor.py`, `ledger.py`, `clients/polymarket.py`, or the post-only execution code. The watchdog cherry-pick touches `risk.py`, `bot.py`, `config.py`, and tests — that's the entire production-code surface of this PR.
- Deletion of v1 or v2 post-only code.
- Touching `feat/post-only-v2-design`.

---

## Estimated effort

- Step 1 (~120 min): script writing + DB query iteration.
- Step 2 (~15 min): code-path audit.
- Step 3 (~90 min): script + 1 unit test + dry run.
- Step 3.5 (~45 min): cherry-pick + conflict resolution + pytest. Add ~30 min if the cherry-pick pulls a v2-only dependency that needs inlining or aborting.
- Step 4 (~10 min): commit.
- Step 5 (~5 min): read the report, check the two blockers.
- Step 6 (~10 min): one-line `.env` edit + bot restart + log tail.
- Step 7 (~15 min): runbook append.
- Step 8 (~20 min): memory updates.
- Step 9 (~10 min): PR.

Total: ~6 hours focused, 1 day calendar. The gating risks are Step 3.5 (cherry-pick clean?) and Step 5 (blocker fires?). Step 3.5 abort path documents the regression and proceeds; Step 5 blocker halts at Step 6 but Steps 7–9 still run.
