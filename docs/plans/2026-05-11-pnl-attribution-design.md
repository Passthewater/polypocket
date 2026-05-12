# PnL attribution — decompose realized PnL into edge / slip / fee / luck

**Date:** 2026-05-11
**Closes (if shipped):** a TBD-numbered issue tracking the diagnostic gap surfaced in `_bmad-output/brainstorming/brainstorming-session-2026-05-11-1901.md` (Idea #41); partially the diagnostic side of #13.
**Depends on:** #15 dual-logging shipped (gives per-decision `model_p_up_v1_calibrated` and `model_p_up_v2`); #16 G1–G5 (decision rows carry `gate_config_json` and the bids that pin the gate-reference price).

## Problem

We can't tell *why* a trade lost. `analyze.py` today reports total PnL, win rate, and per-bin reliability. None of that distinguishes:

- **Bad model** — `model_p_up=0.72` but the true bin rate is 0.55. The trade was a coin-flip dressed up as an edge.
- **Bad fill** — model was right, but `entry_price=0.74` vs the `signal_reference=0.71` the gate compared against. The buffer (or queue position, or book move between decision and ack) ate the edge.
- **Bad luck** — model right, fill clean, the coin landed against us. Keep firing.

Every threshold-tune debate ("is v2 actually better?", "raise the IOC buffer?", "tighten `MAX_ENTRY_PRICE`?") collapses into the same `pnl < 0` signal without this split. The brainstorm of 2026-05-11 ranked PnL attribution as Tier-1 #1 precisely because every downstream Tier-1 idea — Kelly sizing (#21), adaptive buffer (#11), macro blackout (#9) — needs the split to be evaluable.

The Brownian-vs-logistic dual-log (#15) is the same problem at the calibration layer; attribution is the same problem at the *outcome* layer. The two are complementary, not redundant: dual-log answers "is the model better?", attribution answers "given the model, where did the dollars go?"

## Goal

Ship a pure function

```
attribute_pnl(trade_row, decision_snapshot_row) -> PnlAttribution
```

returning a 4-component dataclass that *sums exactly to realized PnL* under one explicit algebra. Surface the components in three places:

1. **Per-trade row** — augment `analyze.py`'s trades table with `edge_$`, `slip_$`, `fee_$`, `luck_$` columns.
2. **Rolling aggregates** — new TUI panel showing trailing 20-trade and lifetime sums per component.
3. **Weekly report** — `scripts/_pnl_attribution.md` auto-generated from the ledger, structured as "of $X realized over N trades, $A came from model edge, $B was given back to fills, $C was fee budget in expectation, $D was variance".

Acceptance is **algebraic** (sum identity holds to float precision on every settled trade) and **operational** (decomposition matches intuition on three hand-replayed reference trades from the live and paper ledgers).

## Math

Let one settled trade have:

- `side ∈ {up, down}` — the side bought.
- `size` — shares held after fill (USDC at risk = `entry_price * size`).
- `entry_price` — actual VWAP fill (post-fill update from `update_trade`).
- `signal_reference_price` — the price the gate compared `model_p_up` against at decision time. For live: pair-merge clearing `1 - best_opp_bid + cushion_ticks`. For backtest/paper-perfect-fill: snapshot ask. **This is the new persisted field.**
- `model_p_up_for_side` = `model_p_up` if `side == "up"` else `1 - model_p_up`.
- `signal_ref_for_side` = `signal_reference_price` (already in side-aligned units — the executable entry on the chosen side).
- `entry_for_side` = `entry_price` (likewise).
- `fees` = `fee_shares(size, entry_price)` — shares-denominated fee from `config.py`.
- `won = (side == outcome)`.
- `realized_pnl = (size - fees) - entry_price * size` if `won`, else `-entry_price * size`.

### Decomposition (ex-ante fee variant — principal)

```
edge_value         =  size * (model_p_up_for_side - signal_ref_for_side)
slip_value         =  size * (signal_ref_for_side - entry_for_side)
expected_fee_value = -fees * model_p_up_for_side
luck_value         =  realized_pnl - (edge_value + slip_value + expected_fee_value)
```

`realized_pnl` is **read from `trades.pnl`** — the authoritative value written by `settle_paper_trade` or `settle_live_trade`. The decomposition does *not* recompute it from a formula, because on live trades the algebraic formula `(size - fees) - entry_price * size` diverges from `trades.pnl` by construction:

- `trades.fees` is logged at trade-submit time from the *intended* `size`/`entry_price` and is **not** updated after the fill;
- `trades.size` and `trades.entry_price` *are* overwritten with the actual fill VWAP / filled size in `update_trade`;
- `settle_live_trade` computes `pnl = payout - info.cost_usdc` from CLOB-reported numbers, where `info.cost_usdc` does not necessarily equal `fill.filled_size * fill.avg_price` after Polymarket-side rounding and fee handling.

Using `trades.pnl` directly preserves the sum identity by construction (`luck_value` is defined as the residual) and lets the same code run unchanged across paper and live ledgers. On live rows, `luck_value` absorbs both outcome-luck *and* the algebra-vs-CLOB accounting residual; that conflation is acknowledged in §"Top risks" and is split out as a future workstream rather than addressed here.

Sum identity (definitional): `edge + slip + expected_fee + luck ≡ realized_pnl` for every row. The first three are deterministic at decision/fill time; `luck` carries all post-decision randomness (outcome) plus, on live rows, the small algebra-vs-CLOB accounting wedge.

### Interpretation

- **edge_value** — value of the model's bet at the price the gate said we were betting at. If the model is well-calibrated and aggregated over many trades, `sum(edge_value) ≈ sum(realized_pnl) - sum(slip) - sum(expected_fee)`. Negative average edge means we're firing trades where the model didn't actually disagree with the reference price after the gate's own thresholds applied — a config/threshold bug, not a model bug.
- **slip_value** — signed VWAP slip from gate reference to actual fill. Captures IOC buffer drag, queue priority loss, and book moves between `t_signal` and `t_fill`. Should be ≤ 0 in expectation under the current "buffer-above-mid" execution policy; positive slip in aggregate means the book is moving in our favor between decision and fill (rare; usually a sign the decision was late).
- **expected_fee_value** — fee budget in expectation. Strictly ≤ 0. Comparing `expected_fee` against `realized_fee` (= `-fees if won else 0`) over many trades surfaces whether we're paying fees more often than the model predicts (i.e., winning more than expected — a happy regime — or fees mis-spec'd).
- **luck_value** — `realized − expected`. Mean across many trades should be ≈ 0 if the model is well-calibrated. Systematic non-zero mean ⇒ miscalibration; this is the same signal the per-bin reliability diagram surfaces, but in dollar units. The two should agree; if they disagree there is a coverage bug. (No new statistical claim — just a check.)

### Why expected-fee, not realized-fee

The conditional `-fees if won else 0` form is equally valid algebraically and matches what landed in the bank account. The trade-off:

- **Expected-fee** isolates `luck` as *pure outcome surprise*. `sum(luck) ≈ 0` under a calibrated model is a clean falsifiable claim.
- **Realized-fee** dumps the "we paid fees more/less than expected" wedge into `luck`, polluting the calibration check.

The report emits **both** columns (`expected_fee_value` is the principal; `realized_fee_value = -fees * won` is shown alongside, and the gap `realized_fee_value - expected_fee_value` is labeled "fee-luck"). One row of the trades table; both summed in the aggregates.

### Sum-identity unit tests (gating)

Eight hand-built cases — `{up,down} × {win,loss} × {signal_ref < entry, signal_ref > entry}` — assert `|edge + slip + expected_fee + luck - realized_pnl| < 1e-9` for every case. A ninth case (fees=0, size=0 degenerate) checks the formula doesn't divide-by-anything.

## Required new data: `signal_reference_price` on `trades`

The decomposition cannot be derived without recording the price the gate compared against. Today the closest column is `market_p_up` on `trades`, which (per the v2 design doc's spot-check) persists raw `up_ask`, not the pair-merge `1 - best_down_bid + cushion`. Using `up_ask` to define edge here would systematically over-attribute to `edge` and under-attribute to `slip` (the same bias that drove +$0.075 mean live slippage per the slip-cushion plan).

### Forward — persist on every new trade

`polypocket/signal.py:106-109` already computes `up_entry`/`down_entry` (the pair-merge clearing prices). Plumb the side-aligned value through `Signal` and into `execute_paper_trade` / `execute_live_trade` as a new `signal_reference_price` arg, then through `log_trade` into a new nullable `trades.signal_reference_price REAL` column.

This is a strict additive change: existing readers ignore the column; the `update_trade` path doesn't touch it; backfill (below) covers historical rows.

### Backward — best-effort backfill

For rows already in `paper_trades.db` / `live_trades.db`:

- **Has `decision` snapshot with `up_bids_json` AND `down_bids_json` populated** (a minority of rows; see below): recompute `signal_reference_price` from `_effective_entry` exactly as `signal.py` does. Tag the row `signal_reference_source = "exact"`.
- **Has `decision` snapshot but lacks the side-relevant bids JSON** (the majority): fall back to `up_ask`/`down_ask` from the snapshot. Tag `signal_reference_source = "approximate"`. On these rows, `slip_value` is biased toward 0 — it under-counts the pair-merge wedge (the gap between snapshot ask and the actual `1 - best_opp_bid + cushion` reference the gate used live).
- **No `decision` snapshot at all** (very old paper rows): tag `signal_reference_source = "missing"`, leave `signal_reference_price = NULL`, exclude from aggregates with a clear log line.

A second column `trades.signal_reference_source TEXT` records provenance for every row (forward rows get `"live"`). Backfill is one-shot via `scripts/backfill_signal_reference.py`; re-runs are idempotent (only touch rows where `signal_reference_source IS NULL`).

**Empirical coverage as of 2026-05-11** (measured against the current `paper_trades.db`): 1368 of 4982 decision rows (27.5%) have non-null bids JSON. The bids-population coverage is **independent of G2** — G2 added `gate_config_json`, not bids. Bids JSON is written by `log_snapshot` only when the caller passes a `book_depth` dict containing `up_bids` / `down_bids`, which happened irregularly across the post-G2 paper soak. Implication: the backfill will tag **roughly 25–30% of historical rows `"exact"`, 65–70% `"approximate"`, and a small remainder `"missing"`**. The report and TUI must default to **excluding `"approximate"` from headline aggregates** (with the full-corpus number reported alongside as a context line) — otherwise the bias-toward-zero slip in approximate rows silently inflates `edge_sum` and deflates `slip_sum` for ~70% of the lifetime corpus.

Forward rows (post-Task 4) are populated from the live computation in `signal.py`, not from re-reading bids JSON, so they are unaffected by bids-population gaps and land as `"live"` (semantically equivalent to `"exact"`).

## Aggregation

Three time-window aggregations, exposed as pure functions in `polypocket/attribution.py`:

```
aggregate_attribution(rows: list[Attribution]) -> AggregateAttribution
```

returning per-component sums, per-component means, count of rows with `signal_reference_source = "exact"` vs `"approximate"`, and the realized-vs-expected fee gap. The caller (TUI / report / analyze.py) decides the rolling window.

Three call sites:

1. **TUI panel `AttributionPanel`** — left-aligned column showing rolling 20-trade and lifetime sums in USDC, refreshes on every settled-trade event. Lives next to the existing `StatusPanel`.
2. **`analyze.py` Section 7 "PnL attribution"** — markdown table for lifetime + last 20; per-bin breakdown by `model_p_up` decile (mirrors the existing reliability table layout so the two read side-by-side).
3. **`scripts/pnl_attribution_report.py`** — emits `scripts/_pnl_attribution.md`. Manual or weekly-cron invocation. Identical content to the analyze.py section plus a per-day trend table.

## Integration points

- **`polypocket/ledger.py`** — `trades` gains two nullable columns (`signal_reference_price REAL`, `signal_reference_source TEXT`) via idempotent ALTER on `init_db`. `log_trade` signature gains `signal_reference_price: float | None = None, signal_reference_source: str = "live"`.
- **`polypocket/signal.py`** — `Signal` gains `signal_reference_price: float` (the side-aligned value of `up_entry`/`down_entry` based on `side`).
- **`polypocket/executor.py`** — `execute_paper_trade` and `execute_live_trade` accept the new field and pass it to `log_trade`. No behavioral change to gating, fills, or settlement.
- **`polypocket/attribution.py`** *(new)* — pure-Python module: dataclass `PnlAttribution`, `attribute_pnl`, `aggregate_attribution`, `attribute_from_db`.
- **`polypocket/analyze.py`** — new Section 7 below the existing reliability section.
- **`polypocket/tui.py`** — new `AttributionPanel(Static)`; mounted alongside `StatusPanel`.
- **`scripts/backfill_signal_reference.py`** *(new)* — one-shot backfill, idempotent.
- **`scripts/pnl_attribution_report.py`** *(new)* — generates `scripts/_pnl_attribution.md`.
- **`tests/`** — `test_attribution.py` (algebra + sum identity + backfill provenance handling), `test_ledger.py` (new column ALTER + log_trade signature), `test_signal.py` (signal_reference_price plumbing).

No change to `bot.py`, `observer.py`, `risk.py`, `fillmodel.py`, `backtester.py`, the CLOB client, or the feeds. Attribution is a strictly post-hoc derived quantity over already-persisted state.

## What this deliberately doesn't do

- **Model improvement.** #15 / v2 logistic owns that. Attribution will *measure* whether v2 reduces the absolute mean of `luck_value` across calibration bins, but does not retrain.
- **Execution improvement.** The brainstorm's Tier-1 adaptive-buffer idea (#11) is a separate plan that *consumes* `slip_value` as its objective.
- **Sizing improvement.** Fractional-Kelly (#21) consumes `edge_value` as its expected-return input but is a separate plan.
- **Per-feature attribution (SHAP/LIME).** Out of scope — would require model internals and is overkill at n≈400 settled trades.
- **Size-slip attribution.** Today every fill is FOK so `filled_size == intended_size` or the trade rejects. Once IOC partials are routine, a `size_slip_value` term will be needed; the schema reserves the conceptual slot but doesn't materialize it yet.
- **Real-time per-tick attribution.** Attribution materializes at settlement only. The brainstorm's "live edge histogram" (Idea #42) is the pre-fire view; attribution is the post-settle view.

## Acceptance

1. **Algebraic.** All 8 hand-built sum-identity unit tests pass to `|diff| < 1e-9 USDC`. A property test over 1000 random `(size, entry_price, signal_ref, model_p_up, side, won, realized_pnl)` tuples asserts the same. Since `realized_pnl` is an input (not a recomputation), the identity is true by construction and the test enforces that the implementation does not accidentally drop a term.
2. **Backfill correctness.** Running `scripts/backfill_signal_reference.py` against the current `paper_trades.db`:
   - Every settled trade with a `decision` snapshot containing both side-relevant non-null bids JSON values is tagged `"exact"`.
   - Every settled trade with a `decision` snapshot lacking the side-relevant bids JSON is tagged `"approximate"`.
   - Every settled trade with no `decision` snapshot is tagged `"missing"`.
   - Re-running is a no-op (no row's columns change on second invocation).
3. **Aggregate sanity (paper, exact rows only).** Over rows where `signal_reference_source = "exact"` AND `db_path = PAPER_DB_PATH`, `sum(trades.pnl) ≈ sum(edge + slip + expected_fee + luck)` to within `1e-6 USDC`. (Trivially true since `realized_pnl` comes from `trades.pnl`; the check guards against type-coercion bugs and dropped rows.) **No equivalent check is asserted on live or on approximate rows** — the algebra-vs-CLOB residual and the bids-fallback bias respectively make a tighter equality claim meaningless. The live aggregate is reported but not gated.
4. **Cross-check against intuition.** Three reference trades, chosen manually from `paper_trades.db` (one clean win, one clean loss, one fill with ≥2-tick slip), produce attributions that match a hand-computation on the design doc's math, recorded in `scripts/_pnl_attribution_reference.md`. Tolerance: `math.isclose(actual, expected, abs_tol=1e-6)` per component.
5. **Operational.** `analyze.py` runs to completion on `paper_trades.db`. If `live_trades.db` exists, it runs there too; if not, the report skips the live section with a clear note (no silent empty section). TUI starts and renders the panel on the paper ledger. Weekly report script emits a non-empty markdown file.

## Definition of done

- `trades.signal_reference_price` and `trades.signal_reference_source` columns shipped (idempotent ALTER).
- `polypocket/attribution.py` shipped with full unit + property tests passing.
- `signal.py` / `executor.py` / `ledger.py` plumbing committed; new live and paper trades persist `signal_reference_price` with `signal_reference_source = "live"`.
- `scripts/backfill_signal_reference.py` shipped and run once against the current paper and live DBs; backfill log committed under `scripts/_backfill_signal_reference.md`.
- `analyze.py` Section 7 shipped; sample report regenerated and committed (`scripts/_polypocket_report.md` or wherever the existing one lives).
- `AttributionPanel` mounted in the TUI; smoke-tested by launching the TUI against `paper_trades.db`.
- `scripts/pnl_attribution_report.py` shipped; first `scripts/_pnl_attribution.md` generated and committed.
- 7 days of forward operation with `signal_reference_source = "live"` populating on every new settled trade; verified via a one-liner SQL count.

## Top risks

1. **Approximate rows dominate the historical corpus.** Empirically, ~70% of historical decision rows lack the side-relevant bids JSON and will be tagged `"approximate"`; the slip computation on those rows is biased toward zero, which inflates `edge_sum` and deflates `|slip_sum|`. Lifetime aggregates that silently include approximate rows therefore overstate model edge and understate execution drag. Mitigation: `aggregate_attribution` defaults to `include_approximate=False`. The report shows headline numbers from exact rows only, with an inclusive-of-approximate line reported alongside for context and clearly labeled. Forward-going rows (post-Task 4) are `"live"` and unaffected.

2. **Live `realized_pnl` carries a CLOB-accounting residual inside `luck`.** On live rows, `trades.pnl` is computed from CLOB-reported `info.cost_usdc` and `info.shares_held`, which can differ from `size * entry_price` by Polymarket-side rounding and protocol fee handling; `trades.fees` is also the *intended* fee, not the realized one. The decomposition uses `trades.pnl` as authoritative `realized_pnl`, so the sum identity holds, but the algebra-vs-CLOB wedge falls into `luck_value`. Across many trades this is small and noisy and washes out; on a single trade it can be material. Mitigation: report the per-trade `luck_value` distribution alongside the aggregates; a future plan (out of scope here) can split `luck` into `outcome_luck` and `accounting_residual` by computing both formula PnL and CLOB PnL and differencing.

3. **Calibration confounds luck.** If v1 is meaningfully miscalibrated (the #15 motivating finding), `sum(luck_value)` over the v1 corpus will be systematically non-zero, and a naive reader may interpret that as "model edge was overstated" rather than "model probability was miscalibrated". These are the same statement at the aggregate, but the per-trade decomposition will assign the gap to `luck`. Mitigation: the report's narrative paragraph explicitly cross-references the reliability section ("if the mean of `luck` exceeds its bootstrap CI under zero-error H0, model is miscalibrated, not unlucky"). Once v2 ships and calibrates the 0.80+ bin, `mean(luck)` on v2-attributed rows should drift toward zero — making attribution itself a v1-vs-v2 evaluation tool. The aggregator splits totals by inferred `MODEL_VERSION` (v1 vs v2) so this comparison is one query, not a join.

4. **Cushion drift on historical backfill.** `SIGNAL_CUSHION_TICKS = 8` today; if it was different at a historical row's decision time, the backfill's recomputed `signal_reference_price` will not equal the value the gate actually used. The plan does not attempt to detect or correct this — the backfill uses the current constant, and the residual error lands in `slip_value`. Mitigation: documented as a known limitation. If cushion was retuned in the interim (Git-blameable), re-run the backfill against the affected date range as a one-off (manual). Single-author repo; cushion retunes are infrequent; cost of getting this wrong is small (a known constant bias on one cohort) versus the cost of building drift-detection infrastructure that may never be exercised.

5. **Aggregate-window cherry-picking.** Choosing the "right" rolling window post-hoc to make a tweak look good is the obvious anti-pattern. Mitigation: report fixes the windows (rolling 20, rolling 100, lifetime, last 7 days) and forbids new windows being added without a documented rationale; weekly report is the authoritative artifact, not ad-hoc TUI screenshots.

## Out of scope (future)

- **Size-slip term** (requires routine IOC partials).
- **Hedge-leg attribution** (requires multi-leg trades, e.g. Idea #89 settlement perp hedge).
- **Cross-market portfolio attribution** (requires Idea #71 ETH/SOL expansion).
- **LLM-generated narrative summaries** of weekly reports (Idea #111).
- **Conditional cohort attribution** — "the losing trades concentrate in gate_config X / hour-of-day Y / `model_p_up` decile Z". The aggregator returns flat sums; slicing by cohort is the obvious next step but requires a second pass and a small cohort DSL. Out of scope here; the persisted per-trade attribution makes it straightforward to add.
- **Split of `luck_value`** into `outcome_luck` (definitional surprise) and `accounting_residual` (algebra-vs-CLOB wedge on live rows). Requires recomputing the formula PnL in parallel and differencing; deferred until the live-rows accounting residual is measured and judged worth separating.

## Artifact

`scripts/_pnl_attribution.md` — committed report:

- Lifetime: `(N, realized_pnl, edge, slip, expected_fee, luck, realized_fee, fee_luck)` table.
- Rolling 20-trade and rolling 100-trade aggregates.
- Per-`model_p_up`-decile breakdown (mirror of reliability section).
- Per-day trend (last 30d).
- Top-5 slip-cost trades and top-5 luck-loss trades, with `window_slug` for replay.
- Provenance counter: `N_exact / N_approximate / N_missing`.

`scripts/_backfill_signal_reference.md` — one-shot backfill log, including per-provenance counts and the aggregate exact-vs-approximate diff.

`scripts/_pnl_attribution_reference.md` — three hand-replayed trades with the math written out, serving as an example and as a regression artifact.
