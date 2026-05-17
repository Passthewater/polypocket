# Post-only / maker-side entries — design

**Date:** 2026-05-15
**Companion:** `2026-05-15-post-only-entries-implementation.md` (not yet drafted; gated on human review of this doc)
**Depends on:** `polymarket_clob_v2_migration` (shipped, commit ~a98e76c) — native FAK now lives on v2; post-only requires the v2 SDK's `post_only=True` flag.
**Closes (if shipped):** the structural side of the live-v2 execution gap diagnosed 2026-05-16 (see memory `project_live_v2_execution_gap`).

## Problem

First v2 live cohort (n=20) showed live calibration ~11.7 pts UNDER paper on the DOWN side at identical model + UTC band. The diagnostic landed on the execution seam: when our FAK reached the matcher 1–2 s after sign, the top-of-book opp-side liquidity it was supposed to cross against was *gone* (raced / cancelled / illusory). The four cleanest ack-time-book proxies showed near-zero drift; the FAK simply found nothing at-or-below limit and rejected. This is the **vanishing-liquidity-at-cross** failure mode — distinct from "book moved past limit", and **not fixable by widening the IOC buffer** (a wider buffer doesn't conjure depth that isn't there).

The structural fix is to stop racing for thin top-of-book and instead *provide* the liquidity others race for: rest a maker order one tick below the pair-merge clearing and let counterparties cross to us. Trades that fill earn the same edge the gate signed off on (plus a tick or two of price improvement); trades that don't fill leave us in cash. The current FAK regime trades fill certainty for execution-seam risk; the post-only regime trades fill certainty for **adverse selection** risk — a different bias with a different fix.

This plan does not modify the model, the gate, the sizing, or the risk manager. It adds a second execution mode behind a config flag, leaves FAK in place, and stages a clean A/B.

## Goal

Ship a `submit_post_only` execution path that:
- Sits behind `ENTRY_MODE=fak|post_only` (default `fak`; flip per process / per cohort).
- Posts a GTC + `post_only=True` order at `(1 − best_opp_bid) − POST_ONLY_REST_OFFSET_TICKS` (default: 1 tick below the pair-merge clearing).
- Carries a server-side `expiration` tied to window-close minus a safety buffer.
- Cancels at `WINDOW_ENTRY_MIN_REMAINING` on the bot tick as belt-and-braces.
- Records `place / fill / cancel / reject` events in `order_events` and reconciles the trade row to the final filled VWAP at cancel-or-expire.
- Honors partial fills, the stranded-fill sweep, and crash-recovery.
- Tags every trade row with `entry_mode` so PnL attribution / model_health can cohort FAK vs post-only.

Out of scope (deliberately) is changing the gate, changing the cushion, changing sizing, or changing paper-mode mechanics. Paper continues to fill instantaneously at `entry_price = ask`; post-only is a live-only construct and the live-vs-paper calibration delta is the validation knob, not paper itself.

## Key design questions

### 1. Where do we rest?

**Recommendation:** rest at `pmc − POST_ONLY_REST_OFFSET_TICKS` where `pmc = 1 − best_opp_bid` and the default offset is **2 ticks**.

Offset=2 is a deliberate compromise. Offset=1 (the most aggressive non-crossing rest) maximizes captured edge but exposes us to a known concrete failure: typical book staleness between gate-evaluation and SDK signing is ~200–500 ms, during which pmc can drift by 1 tick — at offset=1 that drift causes immediate post-only-cross rejection. Offset=3+ gives more drift safety but at proportionally lower fill rate. Offset=2 absorbs typical drift while still capturing ~7 ticks of price improvement over the FAK regime's `pmc + 8`.

The first-cohort goal is to answer "does live calibration converge to paper under maker fills" — that is answerable at any offset; offset=1 doesn't help that question, it just maximizes adverse-selection variance. The knob is plumbed so a follow-up cohort can sweep it.

Tradeoffs across the three candidate strategies:

| Strategy | Rest price | Fill condition | Pros | Cons |
|---|---|---|---|---|
| **pmc − 1 tick** *(recommended)* | One tick below the current pair-merge cross | Best opp-bid bumps up ≥1 tick (opp-side becomes more aggressive) OR a same-side SELL taker arrives at-or-below our limit | Most aggressive non-crossing rest; ~9 ticks of free price improvement over the FAK regime (since the FAK limit was `pmc + 8`); EV-positive on a much wider range of opp-side movement than a deeper rest | Highest adverse-selection exposure: fills happen *exactly when the implied clearing has moved toward us*, which is the regime most correlated with "the underlying is moving the way our signal predicted" — modest positive bias — OR "DOWN-side just got aggressively bid", which is signal-against |
| **Pair-merge clearing exactly (`pmc`)** | Right at the cross | Would-cross on placement → post-only **rejected** by server | Conceptually clean — sits at the price the gate already approved at the cushion floor | Doesn't actually rest; turns into an immediate-cancel signal |
| **Best-bid-on-our-side + 1 (`best_up_bid + 1`)** for BUY UP | Joins our own side's queue, top of new level | Same-side SELL taker arrives at our limit (rare on binary books — SELL-into-bids is uncommon vs the pair-merge path) OR opp side bumps enough to cross through `best_up_bid + 1` | Maximizes own-side queue position; price-improving | On binary books most flow is pair-merge, not same-side SELL — joining the own-side queue is a queue we don't actually want to be in. Empirically `best_up_bid` is bounded below `pmc` by no-cross, so this is always ≤ `pmc − 1`, i.e. equal-or-worse than the recommended rest for fill purposes, and worse on adverse selection |
| **Decision-time pair-merge (`pmc + cushion`)** | At the level the gate signed off on | Same as pmc but with cushion baked in | The cushion is a FAK-era reject buffer; against a resting maker, all it does is push our rest deeper into the book (further from cross), which lowers fill rate without changing realized fill price | A deeper rest is strictly conservative. We can ship that as a v2 variant; v1 ships the most-aggressive non-crossing rest |

The single tunable (`POST_ONLY_REST_OFFSET_TICKS`, default 1) lets us slide along the fill-rate vs adverse-selection frontier later without restructuring code.

### 2. What happens to the 8¢ signal cushion?

The cushion (`SIGNAL_CUSHION_TICKS = 8`) currently does two things, conflated:
1. **FAK reject buffer:** the FAK limit price was `pmc + 8` to absorb 8 ticks of DOWN-bid drift between sign and ack.
2. **Gate selectivity floor:** the gate computes `edge = model_p_up − effective_ask(pmc + 0.08)`, so a 10¢ edge over the cushioned price is the actual MIN_EDGE bar.

For post-only, role (1) is irrelevant: we never trade at a price the gate didn't approve, because we rest at `pmc − 1` which is 9 ticks more favorable than `pmc + 8`. Role (2) — selectivity — remains. The cushion still controls *which signals fire*; under post-only it stops controlling *what price we accept*.

**Recommendation: leave the cushion in the gate unchanged for v1.** Filled post-only trades earn ~9 ticks of "free" edge above what the gate scored. This is conservative — under-counts our edge, doesn't fire trades we shouldn't fire. After a paper-validated cohort, we can re-cohort selectivity (lower the cushion to widen the funnel) independently.

PnL attribution will need to be aware: the `signal_reference_price` stored on the trade row remains `pmc + cushion` (computed in `signal._effective_entry`, unchanged), but the realized `entry_price` will be ~`pmc − 1` on post-only fills. The diagnostic that "slip = sig_ref − entry_price = +0.080 across all live trades" will be a different number in the post-only cohort — slip will be ~+0.09 ($+9¢) on filled post-only trades, meaning the **realized fill came in 9¢ better than the gate's reference**. That's information, not a bug — but it inverts the slip-sign convention from the 2026-05-11 attribution design ("slip ≤ 0 in expectation under buffer-above-mid"). The PnL attribution doc (`docs/plans/2026-05-11-pnl-attribution-design.md` §"Interpretation") will need a cohort-aware reading once post-only cohorts exist — the `entry_mode` column on `trades` makes that split trivial. The new `rest_price` column persisted alongside `signal_reference_price` gives attribution a clean two-axis decompose: `(sig_ref − rest_price)` is the gate's "give-back-to-cushion" (always positive for post-only at the recommended offset), `(rest_price − entry_price)` is the actual rest-vs-fill drift (≈ 0 for post-only by construction). No code change to attribution belongs in this plan — flagged as follow-up.

### 3. Order type and SDK call — confirmed

`py_clob_client_v2` source (`client.py:829–841`):

```python
def create_and_post_order(
    self,
    order_args: OrderArgsV2,
    options: PartialCreateOrderOptions = None,
    order_type: OrderType = OrderType.GTC,
    post_only: bool = False,
    defer_exec: bool = False,
):
    return self._retry_on_version_update(
        lambda: self.post_order(
            self.create_order(order_args, options), order_type, post_only, defer_exec
        )
    )
```

with `post_order` (`client.py:856–883`) explicitly:

```python
if post_only and order_type in (OrderType.FOK, OrderType.FAK):
    raise ValueError("post_only is not supported for FOK/FAK orders")
```

So post-only **must** use `OrderType.GTC` (or `OrderType.GTD` for an additional time-bound semantic — see Q5 / `expiration`). The SDK client-side enforces "no post-only on market orders"; the server enforces post-only crossing rejection.

`OrderArgsV2` (`clob_types.py:75`):
- `token_id, price, side, size` (size in **shares**, not USDC — important: this differs from `MarketOrderArgsV2.amount` which is USDC for BUY).
- `expiration: int = 0` (Unix timestamp, 0 = no expiration).
- `builder_code, metadata, user_usdc_balance` — we don't use these.

When the order would cross, the **server** returns the rejection (not the SDK). Empirical confirmation of the error shape is a Step-1 verification task in the implementation plan (likely `success: false, errorMsg: "post_only_would_cross"` or similar HTTP-400 PolyApiException; the v2-server precedent for FAK no-match was a 400 with `{"error": "no orders found to match"}`). The `_classify_no_match_error` helper in `clients/polymarket.py` will need a sibling for the post-only-cross case so we surface a clean `error="post-only-would-cross"` rather than a generic `network:` string.

### 4. Cancel-on-timeout

Two layers, server-side and bot-side, with the bot-side being authoritative:

- **Server-side `expiration`** on `OrderArgsV2.expiration`: set to `window.end_time − POST_ONLY_EXPIRY_SAFETY_BUFFER_S` (default 30 s). If the bot crashes, the server auto-kills the order at this timestamp. This is defense-in-depth — it does *not* free us from doing our own cancel.
- **Bot-side cancel** in `_on_book_update`: on each tick, if `self._open_trade` is a post-only resting order (status `placed`) and `t_remaining ≤ POST_ONLY_CANCEL_AT_T_REMAINING_S` (default 30 s), call `client.cancel_order(order_id)` then `client.get_settlement_info(order_id)` to reconcile partial fills.

**Trigger:** wall-clock `t_remaining ≤ 30 s`, full stop, not mid-window mid-moves or signal staleness. Mid-window re-evaluation (cancel-and-repost when pmc moves K ticks, cancel when signal flips) is a v2 enhancement explicitly **out of scope** for v1. The simplest possible lifecycle is: one place, one cancel-or-fill, no repost. Multi-shot reposting changes the n=trades semantics (does a re-posted-after-move order count as one decision or two?), and we'd rather measure the single-shot version first.

The choice of 30 s mirrors `WINDOW_ENTRY_MIN_REMAINING` (the gate's signal-acceptance band-end). Filling inside the last 30 s would have us holding a position with no time for the signal to play out, in the regime the gate already deems unsafe to *enter*.

### 5. Cancel-on-window-close

The bot-side tick-driven cancel above runs in `_on_book_update`, which fires on every book event during the live window. Two failure modes for tick-driven cancel:
- **Bot is silent at the cancel deadline** (no book events for >30 s near window-end). Mitigated by the `MAX_BOOK_AGE_S = 3.0` staleness gate already running — but that gate refuses new trades, it doesn't cancel resting ones. Bridging: the server-side `expiration` covers this case. Order dies even if bot doesn't see the boundary.
- **Window-transition firing during cancel processing.** `_on_book_update` advances the window before checking `self._open_trade`; the cancel logic must guard against trying to cancel an order whose status has already been reconciled by the transition.

**Polymarket does not "auto-expire on window resolution"** — its `expiration` field is wall-clock-only. The bot must drive cancel; the server's expiration is the safety net.

Crash-recovery: if a resting order survived a bot restart into a new window, `reconcile_recovered_trade` needs a new branch for CLOB status `"live"` (= still resting on server). Today the reconciler handles `"matched"`, `"canceled" / "cancelled" / "unmatched"`, and unknown. A live order from a stale window should be cancelled-then-reconciled explicitly during recovery — the window context is gone so we should not let it sit and potentially fill.

### 6. Partial-fill handling

Post-only's chief lifecycle complication. Three states across one order's life:

```
place → (placed, 0 filled) → tick 1 fill 10 shares → (placed, 10 filled)
       → tick 2 fill 15 shares → (placed, 25 filled)
       → cancel (or window-end) → (cancelled, 25 filled, 5 unfilled)
```

We do not need real-time fill detection in v1. The deciding moment is **cancel/expire**: at that point we query `get_settlement_info(order_id)` and learn the final cumulative `shares_held` + `cost_usdc`. The trade row is updated once:

- `shares_held > 0`: status → `open` (continues to settlement at window-close), `size = shares_held`, `entry_price = cost_usdc / shares_held`. This is identical to FAK's partial-fill path — the existing settle flow already handles it.
- `shares_held == 0`: status → `rejected`, `error = "post-only-no-fill"`.

While the order is resting (between place and cancel), the trade row stays at `status = "placed"` with `size = intended_size` and `entry_price = rest_price` — the *intended* values, not the realized ones. This mirrors how `status = "reserved"` works for the FAK path today (intended size/price persisted; overwritten on actual fill).

We optionally write a `fill` event per partial in `order_events` for diagnostic richness, but the trade row reconciles once at cancel — not per partial. Rationale: per-partial trade-row updates require either polling (expensive) or a websocket fill-feed (more SDK surface than we want to take on for v1). One reconciliation at cancel is sufficient for correct sizing/PnL.

**Dust threshold:** the existing `execute_live_trade` warns when `filled_size * avg_price < MIN_POSITION_USDC * 0.25`. Reuse this on post-only's cancel-reconcile path. Below that floor the position is still settled in the normal flow (we don't try to refund); we just log it loudly.

### 7. Config flag and constants

New env-backed constants in `polypocket/config.py`:

| Constant | Default | Purpose |
|---|---|---|
| `ENTRY_MODE` | `"fak"` | `"fak"` keeps current path; `"post_only"` switches to new path. Read in `bot.py`. |
| `POST_ONLY_REST_OFFSET_TICKS` | `2` | Ticks below pmc to rest. Default absorbs typical sub-second pmc drift between gate-eval and SDK sign while still capturing ~7 ticks of edge over FAK's `pmc + 8`. Tune later from cohort data. |
| `POST_ONLY_CANCEL_AT_T_REMAINING_S` | `30` | Bot-side cancel deadline. Matches `WINDOW_ENTRY_MIN_REMAINING`. |
| `POST_ONLY_EXPIRY_SAFETY_BUFFER_S` | `30` | Subtracted from `window.end_time` for server-side `expiration`. Same value as the bot deadline by design. |

**Every one of these must be added to `tests/conftest.py::_key`** — failure to do so leaks the developer's `.env` into CI (per `project-context.md`'s "single most common silent bug vector"). This is the load-bearing change to the test plumbing.

Append the same names to `snapshot_gate_config()` so `gate_config_json` on `decision` snapshots records the active entry mode and offset for every trade.

### 8. Order-events lifecycle

Today FAK writes 4 event types: `submit → ack → (fill | reject)`. Post-only mirrors that shape — same five-type alphabet — instead of inventing a new dialect:

| Event | When | Payload |
|---|---|---|
| `place` | Before `client.submit_post_only` call. Analogue of FAK `submit`, but the order rests instead of crossing. | side, intended_size, rest_price, expiration, signal_reference_price, book_age_s_monotonic |
| `ack` | Immediately after the SDK call returns | status (`"placed"` or `"rejected"`), success, order_id, error (if any) |
| `fill` | Each detected partial fill (best-effort; v1 derives at cancel-time, not in-flight) | this_fill_size, this_fill_price, cumulative_size, cumulative_cost |
| `cancel` | Bot or reconciler issues cancel | trigger ∈ {`window-close`, `crash-recovery`, `manual`}, cancel_success, final_shares_held, final_cost_usdc, retries |
| `reject` | Post-only would have crossed at placement (terminal) | error = `"post-only-would-cross"`, current pmc snapshot |

Net: existing analysis joins on `event_type IN ('submit','ack','fill','reject')` add `'place'` and `'cancel'`; nothing already-written changes meaning. The `submit` vs `place` naming distinction is load-bearing — it lets a cohort query split FAK-attempts from post-only-attempts without a join through `trades.entry_mode`.

### 9. Telemetry

New fields:

- **`trades.entry_mode TEXT`** — `"fak"` or `"post_only"`. Idempotent ALTER on `init_db`. Populated by both executor paths.
- **`trades.rest_price REAL`** *(post-only only; nullable)* — the price we rested at, distinct from `entry_price` (which on a partial fill is the realized VWAP). For FAK rows this stays NULL.
- **`order_events.payload_json`** already supports arbitrary keys — no schema change there.

New analyses (out of scope to write here; documented as follow-ups):

- **Cohort fill rate**: `% placed → filled` per entry_mode, daily and rolling.
- **Cohort latency**: median ms from `place` to first `fill`.
- **Cohort calibration**: per-bin reliability split by entry_mode in `scripts/model_health.py`. The same script that surfaced the live-vs-paper gap is what closes the validation loop here.
- **Adverse-selection probe**: for filled post-only trades, persist book state at fill-time (already covered by `book_at_ack`'s sibling — extend the `_book_top_n` snapshot pattern to fill events) and compute the cross-of-rest-price vs cross-at-fill-time gap.

### 10. What can break

| Failure mode | Severity | Mitigation |
|---|---|---|
| **Adverse selection** — book moves against signal between place and fill; we get picked off at a now-unfavorable price. This is the dominant new failure mode and is structural, not a bug. | High (expected) | Single-shot rest with `expiration ≤ 30 s before window-close` caps exposure to the window's remaining vol. Bot-side cancel at `t_remaining = 30 s` is faster than server expiry. Future v2: cancel-and-repost on pmc-move ≥ K ticks. |
| **Cancel race**: bot fires cancel, fill lands 50 ms before cancel reaches server. Server fills partial, then cancels. | Medium | Always call `get_settlement_info(order_id)` after `cancel_order` returns, regardless of cancel's success flag. The post-cancel CLOB state is authoritative. |
| **Ghost fill**: bot thinks cancel succeeded with 0 shares; actually order fully filled before cancel reached server, we miss recording the position. | Medium | Same mitigation: post-cancel `get_settlement_info`. If `shares_held > 0`, promote trade to `open`, settle normally. This is the stranded-fill-sweep pattern, applied at cancel time instead of recovery time. |
| **Cross-window resting order on crash-recovery**: bot died mid-rest. Server expiration may or may not have fired by recovery time. | Medium | `reconcile_recovered_trade` learns a new branch: if `/order` says `"live"`, call `cancel_order` explicitly, then `get_settlement_info`, then promote/reject per shares_held. Don't let a stale-window order keep resting. |
| **Settlement under partial fills**: window resolves with a partial position. | Low | `settle_live_trade` already reads `shares_held` and `cost_usdc` from the CLOB at settle-time. Works unchanged. Path simply scales by `shares_held` instead of `intended_size`. |
| **Dust position from partial fill**: e.g., $1 filled of a $10 intended order. | Low | Reuse the existing dust-warn line in `execute_live_trade` (`notional < MIN_POSITION_USDC * 0.25`). Log loud; settle normal. |
| **Post-only rejection on placement (would-cross)**: pmc moved by ≥ `POST_ONLY_REST_OFFSET_TICKS` between gate decision and SDK call. | Low | Server returns rejection. Trade row goes to `status = "rejected"`, error `"post-only-would-cross"`, window consumed. Same shape as FAK's `"fak-no-fill"`. Frequency expected to be low at offset=1; will rise if we tune offset down to 0. |
| **`OrderArgsV2.size` units bug**: this path passes shares (not USDC) where FAK passed USDC. Easy to confuse. | Medium | Single integration test with a hand-computed size+price → expected JSON payload. Code path runs through `_tick_safe_size` as defense-in-depth (it's a no-op on the share dimension but a check on `size * limit`). |
| **`expiration` units bug**: Unix seconds, not millis; bot computes from `window.end_time` (already Unix seconds). | Low | Type-check in tests; one passing test asserts the JSON payload's expiration is an int and matches `int(window.end_time - 30)`. |
| **TUI keybind mutates `POST_ONLY_REST_OFFSET_TICKS` mid-window**: existing TUI keybind contract treats config constants as live references. If the user re-tunes mid-rest, the next *new* order picks up the new value; the resting order is unaffected. | Low | Standard pattern. No change needed; verify in passing. |
| **Paper mode silently broken**: post-only is a live concept. Paper continues as today. | Low | `bot.py` only routes post-only when `TRADING_MODE != "paper"`. The `ENTRY_MODE` flag is read but ignored in paper. Single test confirms. |

## Files touched (preview)

| File | Change |
|---|---|
| `polypocket/config.py` | Add `ENTRY_MODE`, `POST_ONLY_REST_OFFSET_TICKS`, `POST_ONLY_CANCEL_AT_T_REMAINING_S`, `POST_ONLY_EXPIRY_SAFETY_BUFFER_S`. Add to `snapshot_gate_config()`. |
| `tests/conftest.py` | Append the four env keys to `_key` tuple. |
| `polypocket/ledger.py` | Idempotent ALTER on `trades`: add `entry_mode TEXT`, `rest_price REAL`. Plumb both through `log_trade` (defaulted nullable). |
| `polypocket/clients/polymarket.py` | New `post_only_rest_price(side, up_bids, down_bids, offset_ticks) -> float \| None` pure helper next to `ioc_limit_price`. New `submit_post_only` method on `PolymarketClient`. New `_classify_post_only_cross_error` helper. |
| `polypocket/executor.py` | Extend `LiveOrderClient` Protocol with `submit_post_only`. New `PlaceResult` frozen dataclass (status ∈ `"placed"`, `"rejected"`, `"error"`). New `execute_live_trade_post_only` function. **Place-time pmc recompute inside this function**: the caller passes the gate-time bids alongside fresh `client.get_order_book` results; rest_price is recomputed against the freshest book right before signing, so place-time pmc drift between gate-eval and signing does not silently produce a stale-price cross-and-reject. Extend `reconcile_recovered_trade` for `"live"` CLOB status. |
| `scripts/replay_post_only_paper.py` *(new)* | Post-hoc replay: read `paper_trades.db` `decision` snapshots, compute the hypothetical post-only rest_price from persisted bids JSON, walk forward through `window_book_samples` for the same window_slug to determine fill outcome (filled iff any sample shows `best_opp_bid ≥ 1 − rest_price`), emit `scripts/_post_only_replay.md` with fill rate, fill price distribution, and would-have-been calibration per `entry_mode='post_only'` cohort. Read-only — does not write to the DB. |
| `polypocket/bot.py` | Read `ENTRY_MODE`. Dispatch in the live branch. New `_cancel_resting_order` helper called on each tick when post-only order is open and `t_remaining ≤ POST_ONLY_CANCEL_AT_T_REMAINING_S`. **Add `"placed"` to the live-mode `recoverable_statuses` set** so a resting order survives bot restart through the existing reconciler hook. |
| `tests/test_executor.py`, `tests/test_polymarket_client.py`, `tests/test_bot.py`, `tests/test_signal.py` | New tests. See implementation plan §"Test plan". |

Untouched by design: `signal.py` (gate logic unchanged), `risk.py`, `feeds/`, `observer.py`, `backtester.py`, `fillmodel.py` (paper is unaffected), `analyze.py`, `model_health.py` (cohort splits happen via the new `entry_mode` column — pure-read changes, easy follow-ups, not bundled into this PR).

## Validation plan

### Honoring the memory's plan — post-hoc replay, not real-time paper execution

The diagnostic memory (`project_live_v2_execution_gap`) prescribes:

> "Validation plan when post-only lands: **3-5 days paper validation watching fill rate (will be lower than FAK — acceptable), fill prices (must land at resting limit), and paper v2 calibration under maker path.** Then small live rollout at $5/trade with the new ack-time book diagnostic."

A literal reading of "paper validation watching fill rate" implies adding a post-only execution path to paper mode — defer settlement, track resting state, model when a rest "fills" against a moving book. That doubles the bot's lifecycle surface area (four paths instead of two), and any synthesized fill probability would be calibrated against FAK-era live data whose execution seam is the bug we're escaping.

This plan honors the memory's intent via a smaller, more honest mechanism: **post-hoc replay against the existing `window_book_samples` table.** The bot's runtime paper path stays unchanged (FAK-shape, instant ask fill). After-the-fact, `scripts/replay_post_only_paper.py` reads paper decisions, computes the hypothetical rest_price at the current `POST_ONLY_REST_OFFSET_TICKS`, walks the 30-second-cadence book samples forward through the window, and marks a fill iff any sample shows `best_opp_bid ≥ 1 − rest_price` before the cancel boundary. Outputs:

- **Fill rate** (count of "would have filled" / count of "would have placed").
- **Fill timing** (median sample-index at first fill).
- **Calibration** under the hypothetical post-only cohort, joined to the existing `outcome` resolution per window.

This is deterministic over real recorded book data — no synthesized fillmodel, no encoded assumptions. It under-counts intra-30s fills (book samples are coarse) so the reported fill rate is a lower bound. It cannot observe adverse selection that would emerge from post-only's *presence* on the book changing other actors' behavior — only live can. But it answers the memory's three load-bearing questions ("fill rate", "fill prices land at limit", "paper calibration under maker path") at a fraction of the runtime work.

The live cohort at $5/trade remains the authoritative test of the execution-seam fix. The replay is the cheap pre-flight check that buys confidence in the new code paths before money goes in.

### Phases

**Phase 1 (mandatory before ship):** structural validation in paper mode. The new code paths must not crash, the new config flag must round-trip through `snapshot_gate_config`, the new ledger columns must persist, and **the FAK path must remain bit-identical** when `ENTRY_MODE=fak`. Test suite green. Paper trades continue to fill at ask — `ENTRY_MODE` is read but the live-only post-only branch is never entered in paper.

**Phase 2 (mandatory before live promotion):** post-hoc replay against current `paper_trades.db`. Run `scripts/replay_post_only_paper.py` against all decisions with non-null bids JSON. Acceptance:
- Fill rate is a plausible number (not 0%, not 100%). Below ~15% is a signal the offset is too deep; above ~80% is a signal the offset is too aggressive (probably crossing immediately and rejecting in real life).
- Reported calibration on the would-have-filled cohort is closer to (or at least no worse than) paper's existing FAK-equivalent calibration. If post-only replay surfaces a calibration *worse* than paper FAK, that's a flag to investigate before going live — paper book samples are the same data source for both.

**Phase 3 (mandatory before live promotion):** dry-run live probe. One single-shot post-only placement against a real BTC up/down market, off the bot, at minimum size. Verify: order id returned, server reports `success: true status: "live"`, the order shows up via `/order` lookup at the expected rest price, the bot can cancel it, post-cancel `get_settlement_info` returns shares_held=0 / cost_usdc=0. Document the exact server response shape for both success and would-cross failure in the implementation plan's Step 7 acceptance log.

**Phase 4 (recommended; small live cohort):** 50-100 live trades at `MIN_POSITION_USDC = 5` to validate the calibration premise empirically. Measure:
- Fill rate: compare to Phase-2 replay's lower-bound estimate. Live should be at-or-above the replay number (since live observes intra-30s fills the replay misses). A live fill rate materially *below* the replay is a flag — suggests post-only's presence on the book is changing other actors' behavior in a way that suppresses fills.
- Realized entry vs rest price: expect entry ≈ rest (no slip in either direction since post-only fills only at rest).
- Live calibration vs paper: target gap closure on the DOWN bin — from the diagnostic's −11.7pt back to within ±3pt of paper.

**Phase 5 (out of scope here):** if Phase 4 closes the gap, promote default to `ENTRY_MODE=post_only`. If Phase 4 surfaces adverse selection as a new dominant cost (fills concentrate on the wrong side of book moves), iterate on `POST_ONLY_REST_OFFSET_TICKS` or design the v2 cancel-and-repost extension. If Phase 4 fails to close the gap at all, that's evidence the live-side post-only execution still misses something the replay didn't catch — investigate from a position of having the cohort data.

## What this deliberately does NOT do

- **Does not change the gate, the cushion, MIN_EDGE_THRESHOLD, MIN_MODEL_CONFIDENCE, or sizing.** The gate runs identically; only the execution path downstream differs.
- **Does not implement cancel-and-repost on book-moves.** Single-shot rest only. Multi-shot is the obvious v2 enhancement once we see how often book-move-during-rest matters.
- **Does not implement signal-staleness cancel.** The bot does not re-evaluate the signal during the rest period to decide whether to cancel. Window-close is the only programmed cancel trigger.
- **Does not change paper mode.** Paper continues to fill at ask, instantly. Paper-vs-live calibration validation is structural (does the new code run?) not economic (does the rest-fill timing match?). The diagnostic memo's conclusion that paper is the source of truth for the model still holds; paper is **not** the test bed for post-only's fill economics.
- **Does not implement a websocket fill feed.** Real-time fill detection during the rest period is unnecessary for v1 — we reconcile once at cancel and that's sufficient for sizing, PnL, and settle. A WS fill feed is a richer-telemetry follow-up if/when we want per-partial diagnostics live.
- **Does not migrate scripts/probe code.** All one-off probes referenced in the FAK era remain FAK-shaped. The new post-only path gets a single throwaway probe (Step 7 of the implementation plan) that does not get committed.

## Go / no-go criterion for the human

**GO if all of the following hold:**

1. The maker-side hypothesis is the right structural fix — i.e., you believe the live-vs-paper gap is liquidity racing (per the diagnostic memo), not a deeper model bug, and providing liquidity rather than taking it should narrow the gap.
2. You're comfortable shipping a feature whose first phase of validation is structural (paper + dry-run probe), with the *economic* validation requiring real money on the line at $5/trade.
3. You're comfortable with the v1 simplicity tradeoffs explicitly listed in §"What this deliberately does NOT do" — particularly single-shot-rest with no repost on book-moves and no signal-staleness re-evaluation.
4. The four new config constants and their `tests/conftest.py::_key` additions feel like the right surface area (vs. starting with fewer knobs and adding later).
5. The Protocol extension to `LiveOrderClient` (one new method, one new dataclass) is acceptable in scope. This is the bot's first multi-state order lifecycle and the Protocol is the seam test against.

**NO-GO triggers (push back and revise this design):**

- You want a real-time post-only path in paper mode (not just the replay script). Revise: that's an additional execution-path with its own lifecycle, comparable scope to the live-side work.
- You want to ship cancel-and-repost on book-moves in v1 (revise: that's roughly +3 tasks and +2 failure modes worth of plan).
- You want to retire the cushion in the same PR (revise: bundle gate-selectivity change separately; the cushion-retirement question is a v1-validation-output question).
- You want a different rest offset as the default (e.g. `pmc − 1` for max edge, or `pmc − 5` for adverse-selection safety) — revise the default, keep the offset knob.
- You see a failure mode in §"What can break" we haven't accounted for — surface it, we re-design.

**Decision required:** GO / NO-GO. If GO, the companion `…-implementation.md` will be drafted as a linear ~8-task plan covering Protocol extension, SDK wrapper, executor lifecycle with place-time pmc recompute, bot.py dispatch, config plumbing including `conftest.py::_key`, the replay script, and the test plan (focused unit per Protocol method + an integration test for the partial-fill + cancel race).
