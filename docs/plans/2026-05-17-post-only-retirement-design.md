# Post-only retirement — restore FAK as the default entry path

**Date:** 2026-05-17
**Companion (deferred):** `2026-05-17-post-only-retirement-implementation.md` — to be drafted only after this design is approved.
**Depends on:** `feabf7b` (PR #24 post-only v1 merged) and `feat/post-only-v2-design` (unmerged, all 399 tests green). This plan keeps both code paths in tree but flips the operational default away from them.
**Closes (if shipped):** the unanswered question after the v2 ship-gate failure — "what *do* we do with this signal."

## Problem

The 2026-05-17 v2 ship gate failed on the paper replay (winrate 55.9% < 70%). Memory `[[project_v2_replay_gate_failed]]` correctly named the constraint as "upstream of execution." This plan completes the diagnosis and proposes the response.

### The diagnostic — adverse selection is at the *fill mechanism × signal* seam, not at execution

Run `_bmad-output/v2_failure_diagnostics.py` against `paper_trades.db`, filtered to v2 paper decisions only (post-2026-04-24 MODEL_VERSION=v2 cutover, n=611) and read `model_p_up_v2` directly — both to avoid the mixed-model-version artifact in the pre-cutover slice and to compare against the *only* model the live bot would run. Partition into would-have-filled vs would-NOT-have-filled cohorts using the same `best_opp_bid ≥ 1 − rest` test the v1 replay uses (OFFSET=2 to match the v1 baseline), joined to `trades.outcome` for hit rate:

| cohort | n_settled | win_rate | mean p_pred | gap |
|---|---:|---:|---:|---:|
| All eligible (FAK-equivalent) | 611 | **72.3%** | 0.755 | **−3.1pt** |
| Would-have-filled (post-only subset) | 104 | **46.2%** | 0.715 | **−25.4pt** |
| Would-NOT-have-filled | 507 | **77.7%** | 0.763 | **+1.4pt** |

The all-eligible cohort matches memory `[[project_live_v2_execution_gap]]`'s claim that paper v2 is calibrated. The filled subset sits 25.4pt below its predicted winrate; the unfilled subset sits +1.4pt above (and within sampling noise of paper). The mean p_pred difference between subsets (0.715 vs 0.763) is small — the model has only weak decision-time signal about which decisions will fill — yet realized winrates diverge by **31.5 percentage points** depending on whether the book chose to cross our rest.

The fill event itself encodes adversarial book information that the model could not have access to at decision time, because the book hadn't yet revealed it. The full report (`_bmad-output/v2_failure_diagnostics_modelver.md`) shows this is consistent on the pre-cutover slice too, but with the contaminated p_active column added (full corpus gap was −11.9pt under the mixed model-version path, which is the artifact memory's "calibrated paper v2" claim was originally written *to correct*).

The diagnostic above uses v1 mechanics (single-shot rest at OFFSET=2) and partitions decisions by whether the book ever crossed our rest before the cancel boundary. The Step-7 ship-gate failure used the *v2* mechanics (`scripts/replay_post_only_paper.py --mode v2`, OFFSET=1, K=3 repost, M=8 staleness cancel) and produced filled-cohort winrate **55.9%** at 10.4% fill rate (`scripts/_post_only_v2_replay.md`). The v2 lifecycle improvements (favorable-drift reposts + adverse-drift cancels) raise filled winrate from ~46% to ~56% but cannot close to the 70%-class bar — both data points indict the same structural seam at the fill mechanism × signal interface, via different but corroborating mechanisms.

### Regime splits do not rescue post-only

Run `_bmad-output/v2_failure_diagnostics_extra.py` (full-corpus partition; the regime split is robust to the model-version mixing because it operates within-decision):

| split | filled winrate range | unfilled winrate range |
|---|---:|---:|
| sigma_5min quintiles | 36.1% – 48.8% | 70.6% – 81.6% |
| \|displacement\| quintiles | 38.0% – 47.6% | 70.6% – 87.2% |
| t_remaining quintiles | 38.8% – 46.2% | 69.4% – 85.6% |
| low-sigma × p_pred ≥ 0.85 (n=40) | **42.5%** | n/a |

The filled-cohort winrate stays in a 36–49% band across every regime tested. There is **no decision-time pre-filter** — confidence floor, vol filter, time-in-window cutoff, book-distance bucket — that produces a filled subset with paper-comparable winrate. The bias is structural to the fill mechanism, not a tuning miss.

### Confidence-floor sensitivity confirms the same shape

Per `D2` in the diagnostics report, the all-vs-filled winrate gap is **uniformly −26 to −29pt** at every confidence floor from 0.55 to 0.85. Tightening the gate doesn't narrow the selection bias — it just shrinks both populations proportionally.

### Side asymmetry tracks the live-v2 execution-gap memory

Post-cutover × `model_p_up_v2` (n=611):

| cohort | side | n_settled | win_rate | mean p_pred | gap |
|---|---|---:|---:|---:|---:|
| All eligible | up | 196 | 71.4% | 0.715 | −0.1pt |
| All eligible | down | 415 | 72.8% | 0.774 | **−4.6pt** |
| Filled | up | 42 | 50.0% | 0.610 | −11.0pt |
| Filled | down | 62 | 43.5% | 0.787 | **−35.2pt** |
| Unfilled | up | 154 | 77.3% | 0.744 | +2.9pt |
| Unfilled | down | 353 | 77.9% | 0.771 | +0.8pt |

Two side-related observations:
1. Even on the FAK-equivalent all-eligible slice, DOWN underperforms UP by 4.5pt of calibration gap — pre-existing signal drift, additive to anything execution-side.
2. The post-only adverse-selection effect is dramatically worse on DOWN: filled-DOWN gap is **−35.2pt** vs filled-UP's −11.0pt. The DOWN signal is most damaged by the fill mechanism. This matches the live post-only cohort's 0/3 DOWN result (n=7 total).

This is the same DOWN-asymmetry memory `[[project_live_v2_execution_gap]]` flagged under live FAK (gap −11.7pt). The signal degrades on DOWN regardless of execution mode — independent of, and additive to, the post-only adverse-selection effect. This plan does *not* fix the DOWN asymmetry; see Q6.

## Goal

This plan does *two* things operationally:

1. **Revert `.env` `ENTRY_MODE` from `post_only` to `fak`** *after the Phase 1 calibration report is generated, Phase 2's code-path audit confirms the ack-time diagnostic is wired, and Step 3.5 lifts the wallet watchdog onto `main`.* The bot returns to FAK execution. No runtime-path execution code changes; the model and gate are untouched.
2. **Lift the wallet-balance watchdog from `feat/post-only-v2-design` (commits `9790763` + `3449e55`) to `main`.** Defense-in-depth against the silent ledger-vs-wallet divergence the v1 cohort exhibited. Independent of post-only execution; gives us *more* protection on FAK than the v2-paper state we're leaving. See §Q7.

And *three* things by decision:

3. **Do not merge `feat/post-only-v2-design`.** The branch stays as a frozen reference — all the lifecycle, drift detection, repost throttle, and replay scaffolding remain available if a future signal makes post-only viable again. The watchdog code is cherry-picked, not branch-merged; the post-only execution code stays put. Closing the PR is left to user discretion; the branch is not deleted.
4. **Document this retirement in `docs/runbooks/post-only-live-cohort.md`** so the next operator inheriting this code understands why the v1 path is in tree but not in use. Link the diagnostic reports.
5. **Define the next gate as a pair of replay checks** (paper FAK calibration report + retrospective depth-support tooling) — no new live capital is required to reach a GO decision on this plan. Live promotion is a separate, deferred plan that depends on Phase 1 not surfacing a DOWN-side regression.

Out of scope by design: model refit (excluded per `[[project_live_v2_execution_gap]]`'s "do not refit", though the new bin-level evidence at p≥0.80 — see §"What can break" row 3 — is sufficient to re-open the refit question in a *separate* plan); a new execution mode (hybrid rest-then-aggress, marketable limit with slippage cap, etc.); deletion of v1 or v2 post-only code; any change to `signal.py`, `executor.py`, the ledger schema, or the model; running a fresh live cohort.

## Key design questions

### 1. Why isn't there a tuning fix?

Because the gap doesn't shrink at any examined cut. Diagnostics §D2 shows the all-vs-filled gap is flat (−27pt ± 2) across confidence floors 0.55–0.85. Diagnostics §E1–E4 show flat-to-mildly-rising filled winrate across sigma, |displacement|, t_remaining, and joint low-vol×high-confidence — best case 48.8%. No tuning lever, applied within the existing signal and within post-only mechanics, lifts the filled cohort to a 70%-class winrate. The fill event encodes information the gate cannot anticipate.

### 2. Why is FAK an improvement if it also failed live?

FAK's live failure (50% wr, −$7.84 PnL on n=20) and post-only's live failure (14% wr, −$24.89 PnL on n=7) are not the same failure. Memory `[[project_live_v2_execution_gap]]` already named FAK's failure as the *execution seam* — racing/thinning liquidity at the matcher, where book depth observed at decision is taken or vanishes by the time our IOC arrives 1–2s later. That's a *seam* — a closeable defect tied to a specific code path (FAK submit → ack timing). Post-only's failure is a *property* — the conditional distribution of book-cross events is adversarial to the signal regardless of any tuning. Seams are fixable; conditional-distribution gaps require a different signal (refit, excluded) or a different mechanism.

Concretely: paper FAK on the post-cutover v2-only slice is calibrated to −3.1pt overall (§Problem), matching memory's UTC-band-restricted prior. Paper post-only on the same slice has a structural −25.4pt selection bias on the would-fill subset. FAK *fills the entire eligible cohort*, so paper FAK's calibration is the relevant baseline; post-only only fills the adverse subset. The minimum change toward something tradable is the mode that fills the calibrated population.

### 3. Why not delete v1 / v2 post-only code outright?

Three reasons. First, the v2 lifecycle, drift detection, repost throttle, and wallet watchdog are correct implementations — they passed 399 tests and addressed real failure modes the v1 cohort surfaced. They're only "wrong" against this signal. Second, the wallet watchdog (Step 6 of v2) is *orthogonal* defense-in-depth that the user may want regardless of execution mode; deleting v2 wholesale removes that as a future opt-in. Third, leaving the code present makes the retirement decision auditable later — "we kept the engineering, we changed the operating mode" is a clearer state than "we deleted everything."

If the user prefers a clean tree, the cheaper move is closing the PR without merge and leaving the branch on origin as a tag-equivalent. That's a user decision, not a code change.

### 4. Is there a "stop trading" alternative that's cheaper than going back to FAK?

Yes — set `TRADING_MODE=paper` and run no live capital until either (a) a refit is approved or (b) FAK live is validated under the new ack-time diagnostic. This is a reasonable alternative if the user is not willing to take on FAK's known execution-seam risk on small live capital. The cost is no live PnL signal until something changes. The plan's recommended path is "FAK + ack-time diagnostic + small cohort," but "paper only until next decision" is a strictly safer fallback.

### 5. What does "the next gate" actually look like for FAK?

The gate is two-part: a paper-replay calibration check (the equivalent of v2 design's Phase 2) and a retrospective depth-support check against the existing v2-FAK live cohort. Both run against data already on disk — no new live capital required.

**Paper-replay gate (the headline acceptance criterion).** Compute v2-only paper FAK calibration (the n=611 all-eligible cohort) by confidence bin and by side. The existing diagnostic already gives most of the picture (`_bmad-output/v2_failure_diagnostics_modelver.md:20-29`):

| n≥20 bin | n | gap | passes ±10pt? | passes −15pt floor? |
|---|---:|---:|---|---|
| 0.60–0.65 | 85 | +8.5pt | yes | yes |
| 0.65–0.70 | 68 | +3.1pt | yes | yes |
| 0.75–0.80 | 121 | +0.8pt | yes | yes |
| 0.80–0.85 | 125 | **−10.2pt** | **no (earliest fail)** | yes |
| 0.85–0.90 | 131 | **−13.8pt** | **no** | yes |

The 0.95–1.00 bin (n=12, gap −23.4pt) is *exempt* from the n≥20 rule. The earliest gate failure on existing data is therefore 0.80–0.85 at −10.2pt (just outside ±10pt), not the dramatic −23pt bin. Phase 1 will fail the strict ±10pt rule by construction — what's not yet known is the DOWN-side per-bin shape, which is the actual decision-input (§"Phase 1 reframe" below).

**Depth-support retrospective (revised after empirical check, 2026-05-17).** Memory `[[project_live_v2_execution_gap]]` claimed the ack-time book diagnostic (`order_events.ack.book_at_ack`) landed mid-v2-FAK-live. Direct check of all live DBs (`live_trades.db`, `live_trades_post_only_cohort.db`, and pre-cutover backups) shows zero rows with `book_at_ack` populated. Git log: commit `a98e76c` landed 2026-05-15 23:25 EDT (03:25 UTC); the v2-FAK live cohort ran 2026-05-15 19:46–2026-05-16 02:17 UTC — the diagnostic landed *after* the cohort ended, not during it. **There is no existing data for a retrospective depth-support check.**

The depth-support gate therefore can't be a replay; it has to be a code-path audit + a deferred analysis ready for the next live run:

- A fill is "depth-supported" if `book_at_ack` shows any resting size at-or-below our limit price.
- Optional refinement: depth ≥ 2× order size ($10 at $5/trade), tunable via `MIN_ACK_DEPTH_USDC` (defaults: `0` for the binary existence check, `10.0` for the strict-depth follow-up).
- The Phase 2 script in this plan is therefore *stub-and-test*: it implements the analysis ready to run against future live data, plus a one-shot code-path verification that the `book_at_ack` payload is actually emitted by today's `executor.py` FAK path (a regression-guard against the diagnostic being silently broken when the next live cohort runs).

The only replay gate active in this plan is therefore Phase 1 (paper FAK calibration). Phase 2 is preparatory — a code-path audit and reusable tooling — without a pass/fail bar that gates the `.env` flip.

### 6. Is the side asymmetry a separate problem?

Probably yes — and this plan does not fix it. The DOWN-side gap of −11.7pt under live FAK and −10.7pt under paper post-only is consistent across both execution modes, suggesting it lives in the signal. The "do not refit" constraint blocks a clean fix. Reasonable holding pattern: log the asymmetry, watch it on the next live cohort, surface it as a refit-trigger if it widens. If the user prefers to act on it now, the cheapest option is gating DOWN entries off entirely (`MIN_MODEL_CONFIDENCE_UP` exists; an analogous `down_only_disabled=True` flag could be added — but it's a separate plan).

### 7. What about the dormant v2 wallet watchdog?

The wallet-balance watchdog (Step 6, branch commit `9790763` + fixups `3449e55`) is independent of post-only and would catch a class of bug (silent ledger-vs-wallet divergence) that the v1 live cohort exhibited. It currently lives only on `feat/post-only-v2-design`.

**Decision (2026-05-17 review):** lift the watchdog into this PR. Returning to FAK on `main` *without* the watchdog leaves the bot worse-protected than the v2-paper state it replaces — the v1 cohort's silent bleed was the canonical wallet-vs-ledger divergence the watchdog catches. Treating it as "a clean separable win" in a follow-up plan inverts the framing: it is actually a regression-avoidance step, not a bonus feature. The lift is a small, well-tested cherry-pick (`9790763` + `3449e55`) of code that already passes 399 tests on the v2 branch, with no dependency on the post-only execution path.

## What can break

| Failure mode | Severity | Mitigation |
|---|---|---|
| **User reads "retire post-only" as a sunk-cost surrender and resists.** The v2 PR is built, tested, designed-defended; abandoning it after one replay feels premature. | High (social) | The replay is exhaustive: 1669 decisions across every regime axis on the full corpus; the load-bearing post-cutover v2-only slice has n=611 with a −25.4pt filled-cohort gap. The bias is robust to model-version filtering. This plan's job is to make that diagnostic legible enough that the retirement reads as evidence-driven, not abandonment. If the user disagrees, the NO-GO branches in §Go/no-go list what we'd need to change to keep post-only alive. |
| **FAK execution seam (racing/thinning at ack) is not fixed by switching modes.** Live FAK could continue to bleed. | Medium | Phase 2 of validation is exactly the retrospective check on this — uses existing `live_trades.db` data (n=20, with `book_at_ack` populated mid-cohort). The plan does NOT promote FAK to live; it only restores it as the paper default and proposes a follow-up plan to gate live promotion. The downside of FAK paper is zero. |
| **The model has bin-level drift at p_pred ≥ 0.80** (0.80–0.85 gap −10.2pt n=125; 0.85–0.90 gap −13.8pt n=131) **that FAK won't escape**, even though the overall gap is −3.1pt. Note this is drift on the *all-eligible* paper cohort — separate from the post-only filled-cohort selection effect, additive to anything execution-side. | Medium | Treated as known carry-over risk per the Phase 1 reframe (§"Phase 1"). The plan pre-commits to accept this drift and proceed with the flip. The next live cohort plan (Phase 3, deferred) must size by middle-bin performance and either gate p≥0.85 out, add a `MAX_MODEL_CONFIDENCE` ceiling, or trigger a refit conversation. None of those are in scope for this PR. |
| **Wallet watchdog regression on retirement to FAK.** Returning to FAK on `main` without lifting the v2 watchdog leaves the bot worse-protected against the silent ledger-vs-wallet divergence that the v1 cohort exhibited ($24.89 bleed, 7 fills the bot didn't know about). | High (latent) | Mitigated by bundling the watchdog cherry-pick (`9790763` + `3449e55`) into this PR — see §"Files touched" and Step 3.5 of the implementation plan. If the cherry-pick is rejected (merge conflict, scope concern), document the regression explicitly in the runbook update and treat the unprotected-FAK-paper state as TRADING_MODE=paper-only-indefinite until the lift lands in a follow-up. |
| **Side asymmetry (DOWN gap) bites under FAK too.** | Medium | See Q6 — known carry-over risk, watched not fixed. |
| **Removing post-only as the live default loses paper/live comparability for the diagnostic.** | Low | The v1 cohort DB (`live_trades_post_only_cohort.db`, n=18) is preserved. The v2 paper replay artifacts (`_post_only_v2_replay.md`, `_post_only_replay.md`) are committed. The retirement is documented in the runbook so the comparison is reconstructable. |
| **A future post-only-viable signal arrives and the v2 code has bit-rotted.** | Low | All 399 tests still green on the branch as of commit `6e440d3`. Branch lifecycle is: leave on origin, no rebase, no merge. CI on main won't touch it. Memory `[[project_v2_replay_gate_failed]]` documents the reason it's parked. |
| **"Stop and refit" turns out to be the right call sooner than expected**, and we wasted 4 weeks on execution iteration. | Low | The 4 weeks produced (a) a tested wallet watchdog, (b) the ack-time book diagnostic, (c) a precise diagnosis of where the signal-execution interaction breaks — which is exactly the information a refit would need. The work is not lost. |

## Files touched (preview)

| File | Change | Lines |
|---|---|---:|
| `.env` (gitignored, edited locally; not part of the PR commit) | `ENTRY_MODE=post_only` → `ENTRY_MODE=fak`; bot restart required for the change to take effect. | 1 |
| `docs/runbooks/post-only-live-cohort.md` | Append "Retired 2026-05-17" section: reason, diagnostic links, conditions under which post-only could be revisited, watchdog-lift note. | ~35 |
| `scripts/fak_paper_calibration.py` | New replay script for Phase 1 (paper FAK calibration by bin × side). Mirrors the diagnostic scripts in `_bmad-output/` but committed as a reusable artifact. | ~150 |
| `scripts/fak_ack_depth_retrospective.py` | New replay script for Phase 2 (depth-support analysis on existing `live_trades.db`). | ~120 |
| `scripts/_fak_paper_calibration.md` | Phase 1 output (committed). | n/a |
| `scripts/_fak_ack_depth_retrospective.md` | Phase 2 output (committed). | n/a |
| `polypocket/risk.py` + tests + `polypocket/config.py` (`WALLET_LEDGER_DIVERGENCE_HALT_USDC`) + `polypocket/bot.py` `_check_wallet_divergence` | **Cherry-pick** the wallet-balance watchdog from `feat/post-only-v2-design` commits `9790763` + `3449e55`. Defense-in-depth against silent ledger-vs-wallet divergence; independent of post-only execution path. See §Q7 and Step 3.5 of the implementation plan. | ~250 |
| `docs/plans/2026-05-17-post-only-retirement-implementation.md` | Companion (drafted alongside). | ~120 |
| Memory | Update `[[project_v2_replay_gate_failed]]` to point at this plan; add new `[[project_post_only_retired]]` reference. | n/a |

Explicitly NOT touched: `polypocket/signal.py`, `polypocket/executor.py`, `polypocket/ledger.py` (no schema changes; the watchdog reads `trades` cost columns which already exist on `main`), `polypocket/clients/polymarket.py`, `feat/post-only-v2-design` branch contents, `live_trades.db` (read-only access for Phase 2).

## Validation plan

Per the user's brief, this plan is **replay-gated, not live-cohort-gated**. The diagnosis that drives the retirement is itself a replay. Validation of the FAK *re*-promotion is also replay-gated: paper data and existing v2-FAK-live trace data, no new live capital required until both gates pass and a separate plan promotes.

### Phase 1 (immediate, ~1 hour): paper FAK calibration replay (report, not gate)

Generate `_bmad-output/fak_paper_calibration.md` from `paper_trades.db`, filtered to v2-only post-2026-04-24 decisions and reading `model_p_up_v2`. The script is similar to `_bmad-output/v2_failure_diagnostics_modelver.py` but reports the all-eligible cohort (which IS paper FAK applied to the same decisions) by confidence bin × side. Include per-bin n, mean p_pred, hit rate, gap, Brier. UTC-band-restricted variants for cross-checking against memory's prior calibration numbers.

**Reframe (2026-05-17).** Existing diagnostic data (above) already establishes that the strict bin gate fails at 0.80–0.85. Phase 1's value is therefore not a PASS/FAIL ceremony — it is **(a)** a committed, reproducible artifact of the FAK calibration shape under the v2 model, **(b)** a per-side per-bin DOWN check that the diagnostic only reports at the overall-DOWN level, and **(c)** a forward-baseline against which the next live cohort's calibration can be compared.

**This plan pre-commits to option (a) — accept the high-confidence bin drift as a known risk and proceed with the .env flip.** The diagnosis that drives the retirement is independent of this calibration drift; FAK is being restored as the operationally-correct mode given that post-only adversely selects, not because FAK calibration is perfect. The 0.80–0.85 and 0.85–0.90 bins' drift is real but separate from the retirement decision and is tracked as known carry-over risk (§"What can break" row 3).

**Phase 1 generates two real signals that *would* block the flip:**

1. **DOWN-side per-bin regression.** If any DOWN n≥20 bin shows a gap worse than the overall DOWN gap by ≥10pt, that's new evidence of a side-asymmetric model failure not previously surfaced; halt the flip and surface to user before proceeding.
2. **Overall DOWN gap ∉ [−7pt, +7pt].** The existing diagnostic reports overall DOWN gap −4.6pt — if the Phase-1 reproduction shifts this past −7pt, the model has drifted under the model-version-clean computation; halt and surface.

If neither blocker fires, proceed to Step 6 (the .env flip) regardless of the high-confidence bin drift.

**If a blocker fires:** no FAK live, no .env flip. The retirement decision (don't run post-only) still stands; the choice of replacement (paper-only-indefinitely vs. confidence-ceiling vs. refit) becomes its own plan.

### Phase 2 (preparatory, ~1 hour): code-path audit + reusable depth-support tooling

Empirical check (see §5) shows zero existing live trades have `book_at_ack` populated — the diagnostic landed *after* the v2-FAK cohort. This phase is therefore preparatory rather than gating:

1. **Code-path audit (load-bearing):** read `polypocket/executor.py` to confirm the `book_at_ack` payload is emitted on the FAK path's `ack` event under current `main`. If a regression has crept in between commit `a98e76c` and now, the diagnostic won't fire on the next live cohort and Phase 3 will be blind.
2. **Stub script (`scripts/fak_ack_depth_retrospective.py`):** implements the per-fill depth-support analysis described in §5, with a small unit test against a synthetic `book_at_ack` payload. First run against `live_trades.db` is expected to report "0/20 fills have `book_at_ack` populated — diagnostic landed after this cohort; rerun after next live cohort." That's the correct output for now.
3. **First-real-run gate (deferred):** the actual depth-support analysis runs against the next live cohort's data, which is itself part of the Phase-3 follow-up plan, not this one.

**Acceptance for this phase:**
- The audit confirms the executor FAK path still emits `book_at_ack` under `main`. If not, file a separate bug-fix; do not flip `.env`.
- The script runs end-to-end on `live_trades.db` and produces the expected "no data yet" report.
- Unit test on the synthetic payload passes.

There is no pass/fail bar on data because there is no data. The phase ensures the tooling and code path are ready when the next live cohort produces data.

### Phase 3 (DEFERRED — out of scope of this plan)

A small live FAK cohort with the ack-time diagnostic active. Not part of this plan. Requires a follow-up plan once Phases 1–2 are written and reviewed, and gates user-explicit GO. The follow-up plan defines the cohort size, the daily-loss cap, the per-trade size, and the acceptance bar (informed by Phase 1 calibration numbers and Phase 2 depth-support outcome).

### Phase 4 (DEFERRED, indefinitely): post-only revisit conditions

If a future signal change (refit, new features, alternative model architecture) produces a model whose paper-filled subset is calibrated within ±5pt of paper-overall, post-only becomes re-viable. The v2 branch is the starting point for that work. Conditions for revisit are spelled out in the runbook update.

## Go / no-go criterion for the human

**GO if all hold:**

1. You accept that the −25pt gap on the post-only filled cohort (post-cutover × `model_p_up_v2`, n=104), flat across every regime tested, is structural rather than tunable. Diagnostics §D1 and §E1–E4 are the evidence; the v2-lifecycle replay's 55.9% wr corroborates via a different mechanism.
2. You agree that FAK's known execution seam is a separate, narrower defect — a closeable racing/thinning problem on submit→ack timing — and that a Phase-1-report + Phase-2-tooling + watchdog lift is the right shape for restoring FAK as the paper default. Live promotion is a separate plan, deferred.
3. You're comfortable with the v2 branch staying unmerged but undeleted as a frozen artifact, with the wallet-watchdog code cherry-picked onto `main` as the only carry-over from that work.
4. You accept the high-confidence bin drift (0.80–0.85 at −10.2pt, 0.85–0.90 at −13.8pt) as a known carry-over risk per Phase 1's reframe — not blocking this plan, but a refit-trigger conversation that becomes its own plan if the live-FAK follow-up needs it.

**NO-GO triggers — revise this design:**

- You think one more pre-filter is worth trying before retirement (book-volatility-from-pre-decision-samples is the strongest unexplored axis — would require pulling pre-decision rows from `window_book_samples` that I deliberately filtered out). Say so and I'll add it as Phase 0 before retirement is finalized.
- You want this plan to also lift the wallet watchdog (v2 Step 6) into main rather than deferring it — separable but a clean win. Reasonable add-on.
- You think the right move is `TRADING_MODE=paper` indefinitely and *no* FAK live regardless of replay results, until a refit lands. That's a strictly safer fallback; the doc supports it — would just adjust Phase 3's deferred status to "indefinite."
- You see a failure mode in §"What can break" that we haven't sized correctly.
- You disagree with §3 (keep dormant code vs. delete) — happy to redo with deletion as part of this PR.
- You see a problem in how I've structured the Phase 1 bin/side gates (the cutoffs are my proposal; the user's tolerance for high-conf-bin drift may differ).

**Decision required:** GO / NO-GO. If GO, the companion `…-implementation.md` will cover: (a) `scripts/fak_paper_calibration.py` Phase-1 implementation, (b) `scripts/fak_ack_depth_retrospective.py` Phase-2 implementation, (c) `.env` flip (post-gates), (d) runbook append, (e) memory updates. If NO-GO, your reason determines which axis we explore next.
