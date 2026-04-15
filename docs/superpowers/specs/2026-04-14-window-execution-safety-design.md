# Window Execution Safety Design

Date: 2026-04-14
Status: Approved for planning

## Summary

Polypocket is entering trades immediately and repeatedly in the same BTC 5-minute window because two safety invariants are missing:

- execution is not durably idempotent per `window_slug`
- `DOWN` signals are derived from `1 - up_ask` while execution pays `down_ask`

This design fixes both paper and live trading by introducing a shared execution policy:

- trade at most once per `window_slug`
- only trade from a complete and sane two-sided quote
- evaluate `UP` and `DOWN` from the actual ask price of the side being bought
- recover safely after restart by resuming settlement for an existing open trade without allowing re-entry

## Goals

- Prevent duplicate entries for the same market window in both paper and live modes.
- Ensure signal selection and entry pricing are internally consistent for `UP` and `DOWN`.
- Reject malformed or incomplete order book data before execution.
- Support restart and reconnect recovery without stranding open positions.

## Non-Goals

- Redesign the probability model itself.
- Change risk sizing, volatility estimation, or TUI layout beyond what is needed for consistency.
- Implement full live exchange reconciliation beyond the minimum needed for idempotent submission and recovery.

## Shared Trading Policy

`window_slug` is the execution identity for a BTC 5-minute market. The strategy may evaluate a window many times, but it may submit at most one trade for that slug.

This policy applies to both modes:

- Paper mode enforces uniqueness in durable storage.
- Live mode enforces uniqueness through deterministic client order identity and startup reconciliation.

If the bot restarts during an active window and finds one existing open trade for the active `window_slug`, it must resume tracking that position for settlement only and must not submit another entry for that slug.

If the bot finds contradictory state, such as multiple open trades for the same slug, it must halt trading and log a hard error.

## Quote Validation

The bot must not treat a one-sided quote as tradable market state. A window is eligible for trading only when it has both `up_ask` and `down_ask` and they pass sanity checks.

The validation rules are:

- `up_ask` and `down_ask` must both be present
- each ask must be within `(0, 1]`
- `up_ask + down_ask` must not exceed `1.02`

If any rule fails, the bot may continue updating observability fields, but it must skip execution for that quote snapshot.

## Signal Rule

The canonical signal rule is side-specific and must use the price actually paid for the chosen side.

- `up_edge = model_p_up - (up_ask * (1 + FEE_RATE))`
- `down_edge = (1 - model_p_up) - (down_ask * (1 + FEE_RATE))`

The engine selects the better side only if that side's edge exceeds the configured minimum edge threshold. The threshold contract is:

- `required_edge = MIN_EDGE_THRESHOLD`

This keeps the fee treatment inside the side-specific edge itself so that a signal cannot appear profitable while being unprofitable after fees.

The emitted signal must remain internally consistent:

- `side` matches the chosen side
- stored market probability reflects the ask for that side
- execution entry price uses the same ask used during signal evaluation

## Execution Gate

Execution is a distinct gate after signal evaluation.

The gate answers one question: has this `window_slug` already been consumed for entry?

If yes, skip entry without error. If no, submit once and mark the slug as consumed.

Paper mode requirements:

- durable uniqueness per `window_slug`
- duplicate attempts resolve as "already traded" rather than creating a second row
- settlement updates the existing trade record

Live mode requirements:

- deterministic client order key derived from `window_slug`
- duplicate submit attempts are treated as already consumed
- startup reconciliation restores knowledge of any open trade for the active slug

## Recovery Behavior

On startup or reconnect:

- load the active window
- load any persisted open trade for that `window_slug`
- if exactly one open trade exists, resume settlement tracking only
- if none exist, allow normal signal evaluation and entry
- if state is contradictory, disable trading and emit a hard error

Settlement is resumable. Re-entry is not.

## Observability

The bot and TUI should expose enough state to explain skipped or blocked entries:

- quote validity status
- reason for skip when a quote fails validation
- whether the active `window_slug` has already been consumed
- whether the bot is in recovery/settlement-only mode for the active slug

This is meant to make duplicate prevention and malformed-book handling visible during paper testing and later live operation.

## Testing Scope

The implementation plan must include tests for:

- no trade when only one side of the book is present
- no trade when an ask is outside `(0, 1]`
- no trade when `up_ask + down_ask` exceeds the sanity threshold
- correct `UP` selection using `up_ask`
- correct `DOWN` selection using `down_ask`
- no duplicate paper trade for repeated callbacks on the same `window_slug`
- no duplicate trade after restart when an open trade for the slug already exists
- hard failure path when contradictory persisted state exists for the same slug
- live-mode idempotent submission contract keyed by `window_slug`

## Acceptance Criteria

- A `DOWN` trade cannot be triggered solely because `up_ask` is high if `down_ask` is also expensive.
- Repeated book updates, reconnects, or restarts do not create multiple trades for one `window_slug`.
- Paper and live modes follow the same one-trade-per-window policy.
- Restart recovery preserves settlement behavior without permitting re-entry.
