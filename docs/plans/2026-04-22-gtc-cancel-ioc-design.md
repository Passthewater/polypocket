# GTC + immediate cancel of remainder (approach B from FOK reject design)

Resolves issue #9. Follow-up to `docs/plans/2026-04-21-reducing-fok-rejects-design.md`
after three live sessions confirmed the size-to-depth clamp (commits `7106b44`
+ `ef527df`) and widening `FOK_SLIPPAGE_TICKS` 3 → 6 did not reduce reject
rate (53–60% across sessions).

## Problem recap

Every post-clamp reject had a snapshotted book at decision time with 6× to
457× the required size at ≤ FOK limit price. The binding constraint is not
depth or slippage band — it is either price drift during the 200–500 ms
signing window, stale WS snapshots passing the 3 s staleness gate, or
Polymarket pair-merge matcher semantics we don't model. See
`.airplane-watch.md` for the three-iteration diagnosis.

A static FOK tolerance cannot fix any of those. Partial fills must become
acceptable outcomes.

## Goal

Replace FOK with GTC-at-FOK-limit + immediate cancel of any unmatched
remainder. Use the existing `fok_limit_price` unchanged so any fill —
including race fills during the cancel window — stays within slippage budget
by construction. Partial fills become real positions; the pre-trade floor is
tightened so the worst-case partial is still above `MIN_POSITION_USDC`.

## Design

Flow (live mode), diffed against current:

1. Edge/vol sizing, balance clamp, staleness gate — unchanged.
2. Depth clamp (unchanged):
   `target_size = min(intended, fillable × DEPTH_CLAMP_BUFFER)`.
3. **CHANGED — floor gate.** Skip `"book-too-thin"` iff
   `fillable × price < MIN_POSITION_USDC / MIN_FILL_RATIO`. The worst-case
   partial under B is bounded by `fillable`; the check guarantees a
   `MIN_FILL_RATIO` slice of visible depth still clears the floor.
4. **NEW — submit_ioc.** Post `OrderType.GTC` at
   `fok_limit_price(entry_price)` with `target_size`.
5. **NEW — cancel remainder.** Skip cancel on a full-match response.
   Otherwise call `cancel(order_id)` with retry (`CANCEL_RETRY_MAX=2`,
   backoff `0.25 s`). Cancel failures log `ERROR cancel-failed` but do not
   fail the trade — the startup reconciler is the backstop.
6. **NEW — settle from /trades.** Existing `get_settlement_info` returns
   real `shares_held` and `cost_usdc` from per-fill data.
   `FillResult.filled_size = shares_held`,
   `avg_price = cost_usdc / shares_held`.
7. **NEW — post-fill dust check.** If
   `filled_size × avg_price < MIN_POSITION_USDC × 0.25`, log `WARN dust-fill`
   but record the position. No counter-order.
8. **NEW — diagnostic log.** One INFO line per trade:
   `intended=X target=Y fillable=Z filled=F vwap=P limit=L shortfall=...`.

Status mapping:
- `filled_size >= target_size × 0.99` → `filled`.
- `0 < filled_size < target_size × 0.99` → `filled` (ledger sees smaller row).
- `filled_size == 0` → `rejected` with `error="gtc-no-fill"`.

### Why this works

The FOK-kill failure mode requires the matcher to fail to cover
`target_size` at ≤ limit at the moment our order lands. Under GTC, whatever
depth is present at ≤ limit matches; the rest rests momentarily and is
cancelled. Since the limit is unchanged from FOK, slippage budget is
preserved. Any cancel-race fill is also within budget by construction.

### Components

**`polypocket/clients/polymarket.py`** — new
`submit_ioc(side, price, size, token_id, condition_id) -> FillResult`.
Keep `submit_fok` temporarily (deprecated) for the probe script.

**`polypocket/executor.py`** — `execute_live_trade` already drives PnL from
`SettlementInfo` per commit `66c1517`; should be a no-op.

**`polypocket/bot.py`** — swap `submit_fok` → `submit_ioc`; add floor gate
and diagnostic log.

**`polypocket/config.py`** — add optional
`GTC_CANCEL_TIMEOUT_S = 2.0`. No other new vars.

**`scripts/probe_gtc_cancel.py`** (new, step 0) — manual, one-shot script
that posts a deliberate small-size GTC at a favorable price on a quiet
market, cancels, reads `/order`, prints result. Confirms cancel applies to
remaining-open quantity (not original), before any code ships.

### Error handling

- GTC post fails → `FillResult(status="error", error="network: ...")`. No
  cancel (no `order_id`).
- `success=False` from server → `status="rejected"`, no cancel.
- Full match → skip cancel (server errors on cancel-of-filled).
- Cancel fails after retries → log `ERROR cancel-failed`, return
  `FillResult` from whatever `/trades` reports. Startup reconciler
  (`docs/plans/2026-04-21-startup-order-reconciliation-design.md`) catches
  orphans.
- Empty `/trades` on no-match → `status="rejected"`, `error="gtc-no-fill"`.
- Cancel-race fill → no special case; within slippage by construction.

### Paper mode

Unchanged. GTC+cancel path is live-only, same as the depth clamp.

## Testing

Unit tests:

1. `submit_ioc` full fill (no cancel call).
2. `submit_ioc` partial fill (cancel called, settlement drives result).
3. `submit_ioc` no fill → `rejected` / `gtc-no-fill`.
4. `submit_ioc` post fails → `error`.
5. `submit_ioc` cancel fails → retries, logs, returns best-available result.
6. `submit_ioc` dry-run synthetic result.
7. Bot floor gate engages at `fillable × price < MIN_POSITION_USDC / MIN_FILL_RATIO`.
8. Bot floor gate passes at boundary exactly.
9. Diagnostic log line has expected fields.
10. Executor partial-fill row is consistent with ledger expectations.

Manual probe: run `scripts/probe_gtc_cancel.py` once, document in
implementation plan.

Live rollout gate (≥ 10 trades):
- Reject rate ≈ 0 (tolerate ≤ 1 `gtc-no-fill`).
- No orphan `status='open'` rows post-session.
- Zero `cancel-failed`.
- Zero `429`.

## Decisions locked in

- **Partial fill disposition:** pre-trade floor bump (based on
  `fillable × MIN_FILL_RATIO`) + keep-whatever-fills as backstop. No
  counter-order on dust.
- **Cancel-race handling:** GTC at `fok_limit_price` — any race fill is
  within slippage budget by construction. No post-hoc VWAP guardrail.
- **Cancel semantics:** verified once via `scripts/probe_gtc_cancel.py`
  before main code ships. If probe fails, fall through to a defensive
  read-cancel-read shape.
- **Diagnostics:** one INFO log line per trade comparing snapshot vs.
  actual fill, so if rejects persist we can discriminate between price
  drift, stale feed, and pair-merge semantics without another watch
  session.

## Alternatives rejected

- **Widening slippage band further.** Already tried `FOK_SLIPPAGE_TICKS=6`
  across 17 closed trades; no improvement over 5 ticks or 3 ticks.
- **Counter-order flatten on dust.** Doubles fees, adds a second cancel
  race on the exit, and the pre-trade floor gate already makes dust rare.
- **Post-hoc VWAP guardrail.** Redundant given limit-price choice.
- **A+B belt-and-suspenders.** A (clamp) already stays in place; no need
  for additional pre-trade machinery beyond the floor change.

## Rollout

1. Run `scripts/probe_gtc_cancel.py`; document cancel semantics.
2. Ship `submit_ioc` + bot wiring + floor change + diagnostic log.
3. Unit tests green.
4. One live session ≥ 10 trades; verify rollout gate.
5. Revert `.env` `FOK_SLIPPAGE_TICKS=6 → 3` (stop-gap is no longer
   needed).
6. Delete deprecated `submit_fok` after one clean session.
