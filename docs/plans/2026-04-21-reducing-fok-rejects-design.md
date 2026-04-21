# Reducing FOK rejects via size-to-depth clamping

## Problem

In the first live-trading session (9 trades), 5 of 9 orders were rejected by
Polymarket's CLOB with:

> `order couldn't be fully filled. FOK orders are fully filled or killed.`

Rejects skewed toward the lower-priced "down" side (entry 0.16–0.63), where
the same USDC budget buys more shares and books are typically thinner.

### Root cause

`bot.py` posts a fill-or-kill (FOK) order at `best_ask + 3 ticks`. Before
posting, it runs a **depth gate**: cumulative ask size at ≤ limit price must
exceed `intended_size × 1.1` (a 10% cushion).

Between the depth read and the order landing at the matcher (~200–500 ms of
signing + network), resting asks can be consumed or pulled. The 10% cushion
protects the *server's* snapshot but not *our* order — if churn exceeds the
cushion, the FOK demands more shares than the book still holds and is killed.

## Goal

Reduce FOK rejects without introducing partial stranded positions and without
changing order semantics (still fully-filled or killed).

Non-goal: switching to GTC+cancel or a true IOC semantic. Considered; see
"Alternatives".

## Design

Invert the depth check from a **gate** to a **clamp**. Today, "book can fund
intended × 1.1 → ok, else skip." New logic: "intended size clamped down to
90% of what the book can fund; only skip if the clamped size falls below a
minimum useful fraction of intended."

### Flow (live mode)

1. Edge/vol scaling → `size_usdc`.
2. Balance clamp (unchanged): `size_usdc = min(size_usdc, balance × 0.98)`;
   skip `insufficient-balance` if `< MIN_POSITION_USDC`.
3. `intended_size = size_usdc / entry_price`.
4. Staleness gate (unchanged): book age ≤ `MAX_BOOK_AGE_S`.
5. **NEW — depth clamp:** compute
   `fillable = Σ size where price ≤ fok_limit_price(entry_price)`,
   then `target_size = min(intended_size, fillable × DEPTH_CLAMP_BUFFER)`.
6. **NEW — min-fill-ratio gate:** if
   `target_size < intended_size × MIN_FILL_RATIO`, skip with
   `_window_skip_reason = "book-too-thin"`.
7. **NEW — floor re-check:** if
   `target_size × entry_price < MIN_POSITION_USDC`, skip `"book-too-thin"`.
8. Pass `target_size` into `execute_live_trade` (fees, PnL all scale linearly
   with size; no other accounting impact).

### Why this works

Today the cushion protects the server snapshot: `fillable ≥ intended × 1.1`.
After this change, the cushion protects our order: `target ≤ fillable × 0.9`
— i.e. we ask for at most 90% of what the book holds. A churn event must
consume *more than 10%* of depth at ≤ limit in the 200–500 ms signing window
to still kill the order, vs. merely *any* 10% of the cushion today.

### Config (`polypocket/config.py`)

```python
DEPTH_CLAMP_BUFFER = float(os.getenv("DEPTH_CLAMP_BUFFER", "0.9"))
MIN_FILL_RATIO     = float(os.getenv("MIN_FILL_RATIO",     "0.5"))
```

### Logging

New INFO line when the clamp engages (`target_size < intended_size`):

```
Downsizing trade to depth: intended=X.X target=Y.Y fillable=Z.Z limit=$P.PP
```

Existing SIGNAL/error logs unchanged.

### Error handling

- FOK still kills on unlucky races → same `status='rejected'`, same
  `error="network: …"` row. No regression.
- `book-too-thin` skip reuses the existing `_window_skip_reason` value.

### Paper mode

Unchanged. Depth gate is a live-only branch today and stays live-only.

## Testing

Unit tests in `tests/` covering:

1. `fillable >> intended` → no clamp, trade fires at intended size.
2. `fillable ≈ intended` → `target = fillable × 0.9`, still ≥ ratio floor →
   trade fires at clamped size.
3. `fillable < intended × (ratio / buffer)` → skip `book-too-thin`.
4. `fillable == 0` / empty book → skip `book-too-thin`.
5. `target × price < MIN_POSITION_USDC` → skip `book-too-thin`.
6. Paper mode: depth clamp branch not exercised → existing behavior.

## Alternatives considered

**B. GTC + immediate cancel (true IOC).** Post GTC at limit, cancel any
unmatched remainder. Pros: eliminates FOK-kill class entirely — take whatever
matches. Cons: creates real partial positions (vs. A which pre-sizes), extra
API calls (post + cancel + status), cancel-race with other takers, forces a
choice between dust positions and fee-eating reversals. Deferred: can be
layered on top of A later if rejects persist.

**C. Size-to-depth + GTC+cancel.** Belt-and-suspenders version of A+B. Most
complex, not justified until A's race-residual rate is known.

**Widen FOK_SLIPPAGE_TICKS / cushion only.** Simpler but strictly worse —
pays more slippage on every fill without addressing the race, and still fails
on thin books.

## Rollout

1. Implement + unit test.
2. Run live for one session with defaults (`DEPTH_CLAMP_BUFFER=0.9`,
   `MIN_FILL_RATIO=0.5`) and compare reject-rate to baseline (5/9 = 56%).
3. If rejects still > ~10%, tighten buffer to 0.8 first (env var, no code
   change) before escalating to approach B.
