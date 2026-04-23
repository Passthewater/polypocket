# Restore live PnL — joint re-fit of IOC buffer + gate

**Date:** 2026-04-23
**Closes (if monitoring validates):** #11, #12, #13

## Problem

Live fills post-#11 cushion fix are **−$11.98 / −$16.76 PnL** on n=59 / n=14. The model is well-calibrated on the dominant 0.70–0.75 UP bin (predicted 0.721, actual 0.714), but avg entry 0.719 ≈ realized WR — economics are break-even minus fees. Two independent levers are implicated:

- **Fill-side (#12):** `IOC_BUFFER_TICKS=15` causes fills to land at the taker limit, not at pair-merge clearing. Slip distribution bunches at 11–18t.
- **Gate-side (#13):** `MIN_EDGE_THRESHOLD=0.03` + `MAX_ENTRY_PRICE=0.70` let marginal-edge trades through; at realized slip the "edge" is fees-deep.

## Goal

Restore projected +EV on live trades by jointly re-tuning the fill-side lever (`IOC_BUFFER_TICKS`) and gate-side levers (`MIN_EDGE_THRESHOLD`, `MAX_ENTRY_PRICE`), with `SIGNAL_CUSHION_TICKS` derived from the new buffer. Ship with a bootstrap-CI gate and a post-deploy monitoring check against an N=20 live validation cohort.

## Approach

1. **Joint fit on existing corpus, not sequential live experimentation.** Rewrite the replay with real bid stacks (unblocks #11 item 2), then sweep knobs offline against the existing paper+live corpus. Avoids burning another live cohort at `IOC_BUFFER_TICKS=8` before knowing whether the gate is right.
2. **Walk-the-book fill model with reject modeling.** For each candidate trade's snapshot, walk the opposing-side bid stack top-to-bottom, filling `size` shares, compute VWAP. Implied entry = `1 − VWAP`. Cap walk at `1 − best_opp_bid + buffer_ticks·0.01`. If size can't fit under cap → reject (excluded from PnL). Parametrizes `IOC_BUFFER_TICKS` as a real sweep knob.
3. **Minimal 3-knob grid; cushion is derived.** `IOC_BUFFER_TICKS ∈ {5, 8, 11, 15}` × `MIN_EDGE_THRESHOLD ∈ {0.03, 0.05, 0.07, 0.10}` × `MAX_ENTRY_PRICE ∈ {0.62, 0.65, 0.70}` = 48 combos. `SIGNAL_CUSHION_TICKS` at each buffer setting = replay-projected median slip for that buffer (one fixed-point iteration). Per-side PnL reported at each gridpoint.
4. **Ship criterion — projection + minimums + monitoring gate.** Picked combo must meet: avg projected PnL/trade ≥ $0.25, n_kept ≥ 30, bootstrap 95% lower-bound ≥ $0. Update config and redeploy immediately; after N=20 live fills accumulate at new knobs, compare live per-trade PnL to replay bootstrap CI.

**Known bias.** Replay with real bids captures book *depth* at snapshot time but not *movement* between snapshot and matcher (200–500ms). Projections are systematically optimistic vs live; monitoring gate catches the magnitude.

## Architecture & components

### 1. Fill model (new helper)

```
simulate_pair_merge_fill(size, opp_bids, buffer_ticks)
  -> {filled_size, vwap, implied_entry, rejected: bool}
```

- Walk `opp_bids` sorted by price desc (best bid first).
- Cap at `limit = 1 - best_opp_bid + buffer_ticks * 0.01`. A bid at price `b` contributes entry cost `1 - b`; skip levels where `1 - b > limit`.
- VWAP the filled portion. If can't fill full size under cap → `rejected=True`, PnL excluded.

### 2. Rewritten replay (`scripts/replay_paper_live_fills.py`)

Replaces the 0.08 constant. For each settled paper trade with bid snapshots:
- Load `down_bids_json` / `up_bids_json` from matched `window_snapshots` row.
- Rerun gate at `{cushion, threshold, max_price}` using `1 − best_opp_bid + cushion·0.01` as effective entry.
- If gate passes, run `simulate_pair_merge_fill(size, opp_bids, buffer_ticks)`. If not rejected, compute PnL with actual outcome.
- Output: kept-trade list with projected entry, size, fees, pnl.

### 3. Joint sweep (`scripts/sweep_joint_knobs.py`, new)

48 combos × ~200 paper trades. For each combo:
- Run rewritten replay with that `{buffer, threshold, max_price}`.
- Derive `cushion = round(median(slip) of kept trades)` where `slip = simulated_entry − (1 − best_opp_bid)`.
- Re-run replay with derived cushion (one fixed-point iteration).
- Bootstrap 1000× on per-trade PnL → 95% CI on mean.
- Record: n_kept, total_pnl, avg_pnl, CI_low, CI_high, up_pnl, down_pnl, win_rate, reject_rate, derived_cushion.

### 4. Live monitoring check (`scripts/check_live_vs_projection.py`, new)

After deploy + N=20 live fills: query live_trades for fills after deploy_ts, compute actual avg per-trade PnL, compare to stored bootstrap CI. Outside CI → print divergence warning.

### Config touch

Once the picked combo passes: update `IOC_BUFFER_TICKS`, `SIGNAL_CUSHION_TICKS`, `MIN_EDGE_THRESHOLD`, `MAX_ENTRY_PRICE` defaults in `polypocket/config.py`. Update the stale comment at `config.py:99-107` per #12 task 3. No `signal.py`/`executor.py` logic changes.

## Data flow

**Input.** SQL join (paper + live):
```
trades.status='settled'
  ⟕ window_snapshots (window_slug match, trade_fired=1, nearest timestamp)
    WHERE up_bids_json IS NOT NULL AND down_bids_json IS NOT NULL
```
Only post-2026-04-23 trades have populated bid columns.

**Per-trade:** parse bids → gate check → if accepted, walk-the-book fill → if not rejected, compute PnL → append.

**Per-combo:** initial pass with cushion=11 → measure median slip → re-run with derived cushion → bootstrap 1000× → record row.

**Output.** CSV with 48 rows + top-5 printed table. Picked combo → JSON snapshot (bootstrap CI, knobs, n_kept, commit hash) for monitoring step.

**Monitoring:** query live_trades post-deploy, compare avg per-trade PnL to stored CI.

## Error handling & edge cases

- **Empty / None opp_bids** → skip trade (count as `missing_bids`).
- **Bids sorted wrong** → defensive re-sort by price desc at parse.
- **Size exceeds book depth under cap** → `rejected=True`. No partial-fill in replay; live IOC either fills or rejects.
- **Integer size quantization** (per `a4de4e0`) — replay rounds `size` the same way live does.
- **Tick float comparison** — use tick-integer comparison (`round(x * 100)`). This is the `e6c4ae7`/`a4de4e0` bug class; tests cover both paths.
- **`model_p_up IS NULL` or `best_opp_bid` missing** → skip.
- **Cushion fixed-point non-convergence** — if second pass derives yet another cushion >3 ticks from first, stop iterating, use `buffer_ticks − 3` heuristic, flag combo as "non-convergent" in output.
- **n_kept < 10 in first pass** → skip combo (no stable median).
- **Bootstrap under n=30** — compute but flag "undersize"; ship-gate minimums exclude.
- **Monitoring timezone** — deploy_ts and live_trades.timestamp both UTC. Confirm `live_trades.db` timestamp format before implementing.
- **Book churn** between snapshot and matcher — not modeled. Known optimism bias; monitoring gate is the detection.

## Testing

**Unit — `tests/test_fill_model.py` (new).** Target `simulate_pair_merge_fill` only.
- `test_full_fill_top_bid_only`
- `test_vwap_across_levels`
- `test_cap_excludes_deep_levels`
- `test_size_exceeds_book`
- `test_empty_bids`
- `test_unsorted_input_defensive`
- `test_tick_edge_case` — cap exactly equals next bid's entry cost; both `round()` and raw-multiply paths.

**No tests for sweep/replay/monitoring scripts.** Orchestration code; verified by smoke runs.

**Smoke runs.**
1. Rewritten replay at current live defaults → total PnL within ±30% of actual live PnL on the 59-fill cohort. Sanity check that the model matches reality before sweeping.
2. Full 48-combo sweep. Check: at least one combo has n_kept ≥ 30; monotone expectations hold.

## Acceptance criteria (each blocks the next)

1. `scripts/replay_paper_live_fills.py` rewritten with real bids. At current-live knobs, replay PnL within ±30% of actual. **Closes #11 item 2.**
2. `scripts/sweep_joint_knobs.py` runs 48 combos and emits CSV + top-5.
3. At least one combo meets ship gate: avg PnL/trade ≥ $0.25, n_kept ≥ 30, bootstrap 95% lower-bound ≥ $0.
4. Update `polypocket/config.py` defaults for the four knobs. Update comment at `config.py:99-107`. **Closes #12 task 3.** Commit with bootstrap snapshot JSON.
5. Deploy. Live trading resumes at new knobs.
6. After N=20 fills: monitoring script reports "within CI" (**closes #12, closes #13**) or divergence (re-open re-fit loop).

**Failure mode — no combo passes ship gate.** Don't ship. Don't "pick the best loss." Record in #13, keep live paused, escalate — model recalibration becomes the next thread.

## Out of scope

- Model retraining / per-bin recalibration (0.80+ bin flagged in #13 — separate plan if sweep can't find +EV).
- DOWN-side thresholds (`MIN_EDGE_THRESHOLD_DOWN`, `CALIBRATION_SHRINKAGE_DOWN`). Frozen; revisit if per-side PnL shows DOWN dragging.
- `MIN_MODEL_CONFIDENCE_UP`. Frozen.
- #11 items 3 and 4 (reject-rate / volume sanity). Naturally closed by monitoring step.
- Book-churn modeling.
- Any `signal.py`/`executor.py`/`bot.py` logic changes. Only config values.

## Definition of done

All three issues (#11, #12, #13) closeable. Live bot running at +EV-projected knobs with post-deploy monitoring stored. If step 6 reports divergence, re-open #13 only.
