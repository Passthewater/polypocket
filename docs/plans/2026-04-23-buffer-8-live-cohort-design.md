# Buffer=8 live cohort — sequential re-fit of IOC execution

**Date:** 2026-04-23
**Closes (if SHIP verdict):** #12, #14; partially #11
**Escalates to (if ESCALATE verdict):** new model-recalibration issue tied to #13

## Problem

The prior attempt at joint offline re-fit (`docs/plans/2026-04-23-restore-live-pnl-design.md`, commits `a956b23` and `68f2bb7`) hit two walls during Task 2 smoke-testing: the walk-the-book replay projected +$21.33 against actual −$11.98 on n=59 (+$33 / +8t optimism bias), and the gate-only fallback sweep produced no +EV combo. Root cause of the projection miss: book churn between decision-time snapshot and matcher (~200–500ms signing latency) compounds with `ioc_limit_price()` being recomputed at submit time against the moved book — the offline model has the wrong cap, not just the wrong starting book. Full analysis in issue #14.

Because we only have historical fills at one buffer setting (`IOC_BUFFER_TICKS=15`), we can't fit a churn-vs-buffer curve offline. A second live data point at a different buffer is required.

## Goal

Determine whether reducing `IOC_BUFFER_TICKS` from 15 to 8 materially cuts realized slip. The outcome is a ship-or-escalate decision on the execution-side hypothesis, gated on a single-observable metric (slip ticks) rather than noisy PnL on small-n.

## Approach

Run a controlled live cohort at `IOC_BUFFER_TICKS=8` with all other knobs frozen. After 20 fills (or a safety-rail trip), measure slip distribution and compare against the buffer=15 baseline. Decide via a pre-committed slip-delta gate.

## Protocol

### Config change (launch-only, no commits)

- `IOC_BUFFER_TICKS=8` — set via env var on bot launch. `config.py` default stays at 15 until the SHIP verdict lands.
- `SIGNAL_CUSHION_TICKS=11` — frozen at current live value. Yes, this cushion was calibrated against historical buffer=15 slip, making the gate slightly too strict for the new regime. Acceptable cost for a clean single-variable experiment: changing buffer and cushion simultaneously would confound the slip-delta analysis.
- All other knobs frozen at current `config.py` defaults (`MIN_EDGE_THRESHOLD=0.03`, `MAX_ENTRY_PRICE=0.70`, etc.).

### Cohort definition

N=20 fills, where a "fill" is a live trade with `status IN ('open','settled')` and `entry_price IS NOT NULL`, timestamped after the launch of the experimental cohort. Rejects and cancels do not count toward the 20 but are monitored separately for the reject-rate circuit breaker.

### Baseline

The current 59-fill post-2026-04-23 cohort at buffer=15 is the "before" side of the comparison. Slip median 11.6¢, mean 10.5¢ (per `config.py:17` comment). No re-collection needed.

### Safety rails

Four stop conditions, whichever trips first:

1. **Hard fill cap:** 25 fills (5 over target to allow one in-flight).
2. **Hard loss cap:** −$20 cumulative cohort PnL (2.5× the $8 expected cost from the current loss rate).
3. **Wall-clock cap:** 7 days after cohort start.
4. **Reject-rate circuit breaker:** among the first 10 order attempts (fills + rejects), if rejects > 5 (≥50%), pause immediately. Catches the "buffer=8 is too tight for current book depth" failure mode, which would otherwise compound silently.

### Mechanism — external watchdog

`scripts/cohort_watchdog.py` polls `live_trades.db` every ~60s and writes `.cohort_stop` (kill-file) when any rail trips. The bot checks the kill-file at the top of each window and pauses if present. No changes to core trading logic; the watchdog is additive and removable after the cohort.

The cohort start timestamp is supplied to the watchdog as a CLI arg (`--since`) so reject counting and PnL aggregation scope only to the cohort.

## Analysis

### Slip measurement (primary signal)

For each cohort fill:

```
slip_ticks = round((entry_price - (1 - best_opp_bid)) * 100)
```

Tick-integer arithmetic to avoid the float-artifact bug class from `e6c4ae7`/`a4de4e0` (same pattern as `fillmodel.py:163-169`). `best_opp_bid` is already captured at decision-time in `bot.py:558-568`. Both columns already live in `live_trades.db`.

Report: median, mean, p25, p75, min, max, reject-rate. Compare side-by-side with the buffer=15 baseline.

### Decision gate (primary)

Pre-committed thresholds on cohort median slip:

- **SHIP** if median slip ≤ 6 ticks. Interpretation: buffer reduction materially cut slip; the churn-vs-buffer hypothesis holds.
- **ESCALATE** if median slip ≥ 8 ticks. Interpretation: matcher is filling at or near the limit regardless of buffer; execution isn't the fix.
- **AMBIGUOUS** if median slip is exactly 7 ticks. Extend cohort to 40 fills before deciding. The 1-tick deadband prevents a coin-flip on a point-estimate.

### Replay-PnL sidecar (sanity check, not decisional)

Re-run `scripts/replay_paper_live_fills.py` on the combined 79-fill corpus with `buffer_ticks=8` and the cohort-measured median slip as cushion. Report projected avg PnL and bootstrap 95% CI.

If the replay now lands within ±30% of actual cohort PnL → the fill model is calibrated for the new regime; unblocks the joint sweep the original #11/#13 plan needed. If the replay still diverges → book-churn modeling remains an unresolved gap, tracked as follow-up.

**Why slip is decisional and PnL is not at n=20:** bootstrapping 20 PnL observations produces a CI roughly ±$1/trade wide. A slip-tick measurement on 20 observations has a standard error around 0.3 ticks (historical SD ≈ 3 ticks on the 59-fill cohort). Slip is ~10× more decisive per observation.

### Decision branches

**SHIP branch.** Update `polypocket/config.py`:

- `IOC_BUFFER_TICKS = int(os.getenv("IOC_BUFFER_TICKS", "8"))`
- `SIGNAL_CUSHION_TICKS = int(os.getenv("SIGNAL_CUSHION_TICKS", "<cohort median>"))`
- Refresh the stale comment block at `config.py:11-20` and `config.py:99-107` to reference the combined 79-fill cohort and the new slip distribution.

Then run a full gate-only sweep on the combined 79-fill corpus (reusing `scripts/replay_paper_live_fills.py` over a `threshold × max_price` grid) to optionally tighten the gate in a follow-up commit. That sweep is not blocking the buffer commit — ship the execution fix first, then iterate on gate.

Close #12, partially close #11 (items 3 and 4 satisfied by cohort observation).

**ESCALATE branch.** Keep live paused. Open issue "#15 model recalibration: UP-side 0.70+ bins" linking to #13's 0.80+ bin flag. Scope = audit calibration across 0.70+, 0.75+, 0.80+ bins on the expanded ~79-fill corpus (not just the original n=7 tail). Buffer stays at 15 in config. The fill-model replay gap (#11 item 2 root cause) remains open pending book-churn modeling.

**AMBIGUOUS branch.** Repeat cohort analysis at n=40. Do not change config. If still ambiguous at n=40, escalate.

## Artifact

`scripts/_cohort_analysis.md` — committed short markdown report containing:

- Cohort window (start/end timestamps, git commit of bot at launch)
- Distribution statistics (median, mean, p25, p75, min, max, n_fills, n_rejects, reject_rate)
- Baseline comparison (buffer=15 row, buffer=8 row, delta)
- Replay-PnL sidecar outcome
- Verdict: SHIP / ESCALATE / AMBIGUOUS
- Rationale paragraph tying verdict to the pre-committed thresholds

This artifact is what closes (or reframes) the issue thread.

## Out of scope

- Joint sweeps over threshold/max_price/cushion in the cohort window. Only `IOC_BUFFER_TICKS` varies.
- Model recalibration (escalation target, not current scope).
- Book-churn modeling (known gap, tracked as follow-up).
- Any change to `signal.py`/`executor.py`/`bot.py` beyond the kill-file check.
- Position-sizing changes. Fill cost at full size is the metric that matters.

## Definition of done

Cohort watchdog running, 20 fills accumulated (or safety-rail trip), `scripts/_cohort_analysis.md` committed with a verdict. Config updated only on SHIP verdict. Issue #14 updated with the verdict and next steps.
