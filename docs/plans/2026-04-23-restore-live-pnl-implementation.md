# Restore live PnL Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Restore projected +EV on live trades by jointly re-tuning `IOC_BUFFER_TICKS`, `SIGNAL_CUSHION_TICKS` (derived), `MIN_EDGE_THRESHOLD`, and `MAX_ENTRY_PRICE` via an offline walk-the-book replay + 48-combo sweep, shipped with a bootstrap-CI gate and a post-deploy monitoring check. Closes #11 item 2, #12, #13 if monitoring validates.

**Architecture:** New pure helper `simulate_pair_merge_fill` walks opposing bid stacks with a buffer cap. Replay is rewritten to consume real `up_bids_json`/`down_bids_json` from `live_trades.db` (84 fills post-2026-04-23, paper DB has 0 bid-logged rows so is not the corpus). Sweep runs 48 `{buffer, threshold, max_price}` combos with derived cushion (one fixed-point iteration) and bootstrap 1000× per combo. Monitoring script compares post-deploy live PnL to stored bootstrap CI.

**Tech Stack:** Python 3, sqlite3, pytest, existing `polypocket/` package. No new dependencies.

**Corpus note:** `live_trades.db` has 171 trades total, 98 in `status IN ('open','settled')`, **84 with populated `up_bids_json` / `down_bids_json`**. That 84 is the sweep corpus. The "59-fill" figure in issues #11/#12/#13 was a snapshot at analysis time; the sample has grown.

**Design doc:** `docs/plans/2026-04-23-restore-live-pnl-design.md` (committed as `a956b23`).

---

## Task 1: Fill model helper + unit tests (TDD)

**Files:**
- Create: `polypocket/fillmodel.py`
- Create: `tests/test_fill_model.py`

### Step 1: Write the failing tests

Create `tests/test_fill_model.py` with the following content:

```python
"""Tests for simulate_pair_merge_fill.

The function walks an opposing-side bid stack (best price first) buying up to
`size` shares. A bid at price `b` implies entry cost `1 - b` for a pair-merge
buy; levels whose implied entry exceeds the buffer-capped limit are skipped.
If full `size` can't be filled under the cap, the fill is rejected.
"""
import pytest

from polypocket.fillmodel import simulate_pair_merge_fill


def test_full_fill_top_bid_only():
    bids = [{"price": 0.48, "size": 5}]
    r = simulate_pair_merge_fill(size=1, opp_bids=bids, buffer_ticks=15)
    assert r.rejected is False
    assert r.filled_size == 1
    assert r.vwap == pytest.approx(0.48)
    assert r.implied_entry == pytest.approx(0.52)


def test_vwap_across_levels():
    bids = [{"price": 0.48, "size": 1}, {"price": 0.47, "size": 1}, {"price": 0.46, "size": 2}]
    r = simulate_pair_merge_fill(size=3, opp_bids=bids, buffer_ticks=15)
    assert r.rejected is False
    assert r.filled_size == 3
    # VWAP = (0.48*1 + 0.47*1 + 0.46*1) / 3
    assert r.vwap == pytest.approx((0.48 + 0.47 + 0.46) / 3)
    assert r.implied_entry == pytest.approx(1 - (0.48 + 0.47 + 0.46) / 3)


def test_cap_excludes_deep_levels():
    # best bid 0.48 -> best entry cost 0.52. Cap = 0.52 + 0.03 = 0.55.
    # second bid 0.40 -> entry cost 0.60 > 0.55 -> excluded.
    bids = [{"price": 0.48, "size": 1}, {"price": 0.40, "size": 5}]
    r = simulate_pair_merge_fill(size=3, opp_bids=bids, buffer_ticks=3)
    assert r.rejected is True


def test_size_exceeds_book():
    bids = [{"price": 0.48, "size": 2}]
    r = simulate_pair_merge_fill(size=10, opp_bids=bids, buffer_ticks=15)
    assert r.rejected is True


def test_empty_bids():
    r = simulate_pair_merge_fill(size=1, opp_bids=[], buffer_ticks=15)
    assert r.rejected is True


def test_none_bids():
    r = simulate_pair_merge_fill(size=1, opp_bids=None, buffer_ticks=15)
    assert r.rejected is True


def test_unsorted_input_defensive():
    # Input in ascending price order; function must re-sort desc.
    bids = [{"price": 0.46, "size": 2}, {"price": 0.47, "size": 1}, {"price": 0.48, "size": 1}]
    r = simulate_pair_merge_fill(size=3, opp_bids=bids, buffer_ticks=15)
    assert r.rejected is False
    assert r.vwap == pytest.approx((0.48 + 0.47 + 0.46) / 3)


def test_tick_edge_case_inclusive():
    # Cap = 1 - 0.48 + 0.04 = 0.56. Second bid 0.44 -> entry cost 0.56 == cap.
    # Inclusive: the level at exactly cap is eligible. This is the tick-float
    # bug class from e6c4ae7/a4de4e0 — do the comparison in tick-integer space.
    bids = [{"price": 0.48, "size": 1}, {"price": 0.44, "size": 2}]
    r = simulate_pair_merge_fill(size=2, opp_bids=bids, buffer_ticks=4)
    assert r.rejected is False
    assert r.filled_size == 2
    assert r.vwap == pytest.approx((0.48 + 0.44) / 2)


def test_tick_edge_case_float_artifact():
    # Cap = 1 - 0.07 + 0.15 = 1.08 (irrelevant high), but 0.1 + 0.2 = 0.30000000000000004
    # Make sure the tick-integer comparison handles float artifacts. Cap in
    # ticks: (1 - 0.48)*100 + 15 = 67. A level at 0.33 has entry 0.67 -> 67 ticks.
    # Raw multiply: (1 - 0.33) * 100 = 67.0 but floats can be 66.99999...
    bids = [{"price": 0.48, "size": 1}, {"price": 0.33, "size": 1}]
    r = simulate_pair_merge_fill(size=2, opp_bids=bids, buffer_ticks=15)
    assert r.rejected is False
    assert r.filled_size == 2
```

### Step 2: Run tests — verify they fail

Run: `pytest tests/test_fill_model.py -v`
Expected: all tests FAIL with `ModuleNotFoundError: polypocket.fillmodel`.

### Step 3: Write minimal implementation

Create `polypocket/fillmodel.py`:

```python
"""Pair-merge fill simulation for offline replay.

Live BUYs on binary markets clear via pair-merge: a BUY UP matches against a
DOWN-side bid such that (up_fill + down_bid) = 1. So for our size S and the
opposing-side bid stack, we walk from the best bid downward, filling S shares
at their VWAP. The implied entry price we pay is (1 - VWAP).

The live IOC has a buffer cap: limit_price = 1 - best_opp_bid + buffer*0.01.
Bids below that threshold (entry cost > cap) are not matchable. If our size
can't be filled under the cap, live would reject/partial and we exclude the
trade from PnL.

All price comparisons use tick-integer space (round(x * 100)) to avoid the
float artifact bug class from e6c4ae7/a4de4e0.
"""
from dataclasses import dataclass


@dataclass
class FillResult:
    filled_size: float
    vwap: float
    implied_entry: float
    rejected: bool


def simulate_pair_merge_fill(
    size: float,
    opp_bids: list[dict] | None,
    buffer_ticks: int,
) -> FillResult:
    if not opp_bids or size <= 0:
        return FillResult(0.0, 0.0, 0.0, True)

    bids = sorted(
        ({"price": float(b["price"]), "size": float(b["size"])} for b in opp_bids),
        key=lambda b: -b["price"],
    )
    best = bids[0]["price"]
    # Cap in tick-integer space.
    cap_ticks = round((1.0 - best) * 100) + buffer_ticks

    remaining = size
    cost = 0.0
    filled = 0.0
    for b in bids:
        entry_ticks = round((1.0 - b["price"]) * 100)
        if entry_ticks > cap_ticks:
            break
        take = min(remaining, b["size"])
        cost += b["price"] * take
        filled += take
        remaining -= take
        if remaining <= 0:
            break

    if filled + 1e-9 < size:
        return FillResult(filled, 0.0, 0.0, True)

    vwap = cost / filled
    return FillResult(filled, vwap, 1.0 - vwap, False)
```

### Step 4: Run tests — verify they pass

Run: `pytest tests/test_fill_model.py -v`
Expected: all 9 tests PASS.

### Step 5: Commit

```bash
git add polypocket/fillmodel.py tests/test_fill_model.py
git commit -m "feat(fillmodel): walk-the-book pair-merge fill simulator (#11 item 2)"
```

---

## Task 2: Rewrite replay to use real bids + walk-the-book model

**Files:**
- Modify: `scripts/replay_paper_live_fills.py` (full rewrite — rename intent, point at `live_trades.db`)

### Step 1: Inspect the current state of `scripts/replay_paper_live_fills.py`

Read it. Note:
- It uses `PAPER_DB = "paper_trades.db"` and a constant `SLIP_PREMIUM = 0.08`.
- Paper DB has 0 trades with `up_bids_json` populated; only live DB has them.
- The new replay must query `live_trades.db` instead.

### Step 2: Write the new replay

Replace the file contents with:

```python
"""Replay live trades under a parametric pair-merge fill model.

Rewrites issue #11 item 2: instead of a constant SLIP_PREMIUM=0.08, walk the
real `up_bids_json` / `down_bids_json` stacks (captured at decision time) with
simulate_pair_merge_fill(). Sweepable knobs: buffer_ticks, cushion_ticks,
threshold, max_price.

Corpus: live_trades.db, trades where status IN ('open','settled') and the
paired window_snapshot row has both bid-stack JSONs populated.
"""
import argparse
import json
import sqlite3
import statistics
from dataclasses import dataclass

from polypocket.config import FEE_RATE, effective_ask, fee_shares
from polypocket.fillmodel import simulate_pair_merge_fill

DEFAULT_DB = "live_trades.db"


@dataclass
class KeptTrade:
    tid: int
    side: str
    outcome: str | None
    model_p_up: float
    best_opp_bid: float
    filled_size: float
    implied_entry: float
    fees: float
    pnl: float


def _load_rows(db_path: str) -> list[sqlite3.Row]:
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        """SELECT t.id AS tid, t.window_slug, t.side, t.size AS intended_size,
                  t.model_p_up, t.outcome, t.status,
                  s.up_ask, s.down_ask, s.up_bids_json, s.down_bids_json
             FROM trades t
             JOIN window_snapshots s
               ON s.window_slug = t.window_slug AND s.trade_fired = 1
            WHERE t.status IN ('open', 'settled')
              AND s.up_bids_json IS NOT NULL
              AND s.down_bids_json IS NOT NULL
              AND t.model_p_up IS NOT NULL
            ORDER BY t.id"""
    ).fetchall()
    c.close()
    return rows


def replay(
    db_path: str,
    buffer_ticks: int,
    cushion_ticks: int,
    threshold: float,
    max_price: float,
    threshold_down: float = 0.10,
    min_model_conf_up: float = 0.70,
    min_model_conf: float = 0.60,
) -> list[KeptTrade]:
    """Run one combo. Return kept trades with simulated PnL."""
    rows = _load_rows(db_path)
    kept: list[KeptTrade] = []

    for r in rows:
        up_bids = json.loads(r["up_bids_json"])
        down_bids = json.loads(r["down_bids_json"])
        if not up_bids or not down_bids:
            continue

        model_p_up = r["model_p_up"]
        # Gate: UP side
        best_down_bid = max(float(b["price"]) for b in down_bids)
        up_entry_gate = min(0.99, (1.0 - best_down_bid) + cushion_ticks * 0.01)
        up_edge = model_p_up - effective_ask(up_entry_gate)

        # Gate: DOWN side
        best_up_bid = max(float(b["price"]) for b in up_bids)
        down_entry_gate = min(0.99, (1.0 - best_up_bid) + cushion_ticks * 0.01)
        down_edge = (1.0 - model_p_up) - effective_ask(down_entry_gate)

        up_ok = (
            model_p_up >= min_model_conf_up
            and up_entry_gate < max_price
            and up_edge >= threshold
            and up_edge >= down_edge
        )
        down_ok = (
            model_p_up <= (1.0 - min_model_conf)
            and down_entry_gate < max_price
            and down_edge >= threshold_down
        )

        if up_ok:
            side = "up"
            opp_bids, best_opp = down_bids, best_down_bid
        elif down_ok:
            side = "down"
            opp_bids, best_opp = up_bids, best_up_bid
        else:
            continue

        # Live quantizes size to an integer (a4de4e0). Do the same.
        size = max(1, round(r["intended_size"]))
        fill = simulate_pair_merge_fill(size, opp_bids, buffer_ticks)
        if fill.rejected:
            continue

        fees = fee_shares(fill.filled_size, fill.implied_entry)
        won = r["outcome"] == side
        payout = (fill.filled_size - fees) if won else 0.0
        cost = fill.implied_entry * fill.filled_size
        pnl = payout - cost

        kept.append(
            KeptTrade(
                tid=r["tid"],
                side=side,
                outcome=r["outcome"],
                model_p_up=model_p_up,
                best_opp_bid=best_opp,
                filled_size=fill.filled_size,
                implied_entry=fill.implied_entry,
                fees=fees,
                pnl=pnl,
            )
        )

    return kept


def _print_summary(kept: list[KeptTrade], label: str) -> None:
    if not kept:
        print(f"{label}: 0 trades kept")
        return
    n = len(kept)
    total = sum(k.pnl for k in kept)
    wins = sum(1 for k in kept if k.outcome == k.side)
    slips = [
        round((k.implied_entry - (1 - k.best_opp_bid)) * 100) for k in kept
    ]
    print(
        f"{label}: n={n}  pnl={total:+.2f}  avg={total/n:+.3f}  "
        f"wins={wins} ({wins/n*100:.1f}%)  slip_median_ticks={statistics.median(slips):.1f}"
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--buffer-ticks", type=int, default=15)
    p.add_argument("--cushion-ticks", type=int, default=11)
    p.add_argument("--threshold", type=float, default=0.03)
    p.add_argument("--max-price", type=float, default=0.70)
    args = p.parse_args()

    kept = replay(
        db_path=args.db,
        buffer_ticks=args.buffer_ticks,
        cushion_ticks=args.cushion_ticks,
        threshold=args.threshold,
        max_price=args.max_price,
    )
    label = (
        f"buf={args.buffer_ticks} cush={args.cushion_ticks} "
        f"thr={args.threshold} cap={args.max_price}"
    )
    _print_summary(kept, label)


if __name__ == "__main__":
    main()
```

### Step 3: Smoke-run against current live defaults

Run:
```
python scripts/replay_paper_live_fills.py --buffer-ticks 15 --cushion-ticks 11 --threshold 0.03 --max-price 0.70
```

Expected: prints one summary line. Record the output.

### Step 4: Compare against actual live PnL

Run:
```
sqlite3 live_trades.db "SELECT COUNT(*), ROUND(SUM(pnl),2), ROUND(AVG(pnl),3) FROM trades WHERE status='settled' AND pnl IS NOT NULL AND timestamp >= '2026-04-23';"
```

Expected: shows actual (n, total_pnl, avg_pnl) for the post-bid-logging cohort.

**Acceptance:** Replay total PnL at current-live knobs is within **±30%** of actual (sign and order-of-magnitude agreement is what we're after — the replay doesn't model book churn, so a ~10–20% optimism bias is expected).

If divergence is larger than ±30%: **stop.** Don't continue to the sweep. The fill model or SQL join is wrong; debug that first.

### Step 5: Commit

```bash
git add scripts/replay_paper_live_fills.py
git commit -m "refactor(scripts): replay uses real bids + walk-the-book fill (#11 item 2)"
```

---

## Task 3: Joint-sweep script

**Files:**
- Create: `scripts/sweep_joint_knobs.py`

### Step 1: Write the sweep

Create `scripts/sweep_joint_knobs.py`:

```python
"""48-combo joint sweep of {buffer, threshold, max_price} with derived cushion.

For each combo:
  1. Run replay at cushion=11 (initial guess).
  2. Compute median slip on kept trades -> derived_cushion.
  3. If |derived - initial| > 3 ticks, re-run once with derived_cushion.
     If still non-convergent, fall back to cushion = buffer - 3 and flag.
  4. Bootstrap 1000x per-trade PnL for 95% CI on mean.
  5. Record row.

Outputs: CSV at scripts/_sweep_results.csv plus top-5 table on stdout.
"""
import argparse
import csv
import random
import statistics
import sys

from scripts.replay_paper_live_fills import KeptTrade, replay

BUFFERS = [5, 8, 11, 15]
THRESHOLDS = [0.03, 0.05, 0.07, 0.10]
MAX_PRICES = [0.62, 0.65, 0.70]
BOOT_N = 1000
SHIP_MIN_AVG_PNL = 0.25
SHIP_MIN_N_KEPT = 30


def _median_slip_ticks(kept: list[KeptTrade]) -> int:
    if not kept:
        return 0
    slips = [round((k.implied_entry - (1 - k.best_opp_bid)) * 100) for k in kept]
    return round(statistics.median(slips))


def _bootstrap_ci(pnls: list[float], n_boot: int, rng: random.Random) -> tuple[float, float]:
    if len(pnls) < 2:
        avg = pnls[0] if pnls else 0.0
        return (avg, avg)
    means = []
    k = len(pnls)
    for _ in range(n_boot):
        sample = [pnls[rng.randrange(k)] for _ in range(k)]
        means.append(sum(sample) / k)
    means.sort()
    lo = means[int(0.025 * n_boot)]
    hi = means[int(0.975 * n_boot)]
    return (lo, hi)


def sweep(db: str) -> list[dict]:
    rng = random.Random(42)
    results = []
    for buffer_ticks in BUFFERS:
        for threshold in THRESHOLDS:
            for max_price in MAX_PRICES:
                # Pass 1: cushion=11
                kept1 = replay(db, buffer_ticks, 11, threshold, max_price)
                if len(kept1) < 10:
                    results.append(
                        {
                            "buffer": buffer_ticks, "threshold": threshold,
                            "max_price": max_price, "cushion": 11,
                            "n_kept": len(kept1), "note": "skipped-undersize",
                            "total_pnl": 0, "avg_pnl": 0,
                            "ci_low": 0, "ci_high": 0,
                            "up_pnl": 0, "down_pnl": 0, "win_rate": 0,
                            "convergent": True,
                        }
                    )
                    continue
                derived = _median_slip_ticks(kept1)

                # Pass 2 (if needed): re-run with derived cushion
                convergent = True
                if abs(derived - 11) > 3:
                    kept2 = replay(db, buffer_ticks, derived, threshold, max_price)
                    if len(kept2) >= 10:
                        derived2 = _median_slip_ticks(kept2)
                        if abs(derived2 - derived) > 3:
                            convergent = False
                            # Fallback heuristic
                            cushion = max(1, buffer_ticks - 3)
                            kept = replay(db, buffer_ticks, cushion, threshold, max_price)
                        else:
                            cushion = derived
                            kept = kept2
                    else:
                        convergent = False
                        cushion = max(1, buffer_ticks - 3)
                        kept = replay(db, buffer_ticks, cushion, threshold, max_price)
                else:
                    cushion = derived
                    kept = kept1

                if len(kept) < 10:
                    results.append(
                        {
                            "buffer": buffer_ticks, "threshold": threshold,
                            "max_price": max_price, "cushion": cushion,
                            "n_kept": len(kept), "note": "skipped-undersize-after-iter",
                            "total_pnl": 0, "avg_pnl": 0,
                            "ci_low": 0, "ci_high": 0,
                            "up_pnl": 0, "down_pnl": 0, "win_rate": 0,
                            "convergent": convergent,
                        }
                    )
                    continue

                pnls = [k.pnl for k in kept]
                total = sum(pnls)
                avg = total / len(pnls)
                lo, hi = _bootstrap_ci(pnls, BOOT_N, rng)
                up_pnl = sum(k.pnl for k in kept if k.side == "up")
                down_pnl = sum(k.pnl for k in kept if k.side == "down")
                wins = sum(1 for k in kept if k.outcome == k.side)

                results.append(
                    {
                        "buffer": buffer_ticks, "threshold": threshold,
                        "max_price": max_price, "cushion": cushion,
                        "n_kept": len(kept), "note": "" if convergent else "non-convergent",
                        "total_pnl": round(total, 2),
                        "avg_pnl": round(avg, 3),
                        "ci_low": round(lo, 3),
                        "ci_high": round(hi, 3),
                        "up_pnl": round(up_pnl, 2),
                        "down_pnl": round(down_pnl, 2),
                        "win_rate": round(wins / len(kept), 3),
                        "convergent": convergent,
                    }
                )
    return results


def _write_csv(results: list[dict], path: str) -> None:
    if not results:
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)


def _print_top(results: list[dict]) -> None:
    # Sort by avg_pnl desc; exclude undersize rows.
    eligible = [r for r in results if r.get("note") != "skipped-undersize"
                and r.get("note") != "skipped-undersize-after-iter"]
    eligible.sort(key=lambda r: r["avg_pnl"], reverse=True)
    print(f"\nTop 5 by avg_pnl (of {len(eligible)} eligible / {len(results)} total combos):\n")
    print(f"{'buf':>4s} {'thr':>6s} {'cap':>5s} {'cush':>5s} {'n':>4s} "
          f"{'tot':>8s} {'avg':>7s} {'ci_lo':>7s} {'ci_hi':>7s} "
          f"{'up':>7s} {'dn':>7s} {'wr':>5s} {'note':>15s}")
    for r in eligible[:5]:
        ship = (r["avg_pnl"] >= SHIP_MIN_AVG_PNL
                and r["n_kept"] >= SHIP_MIN_N_KEPT
                and r["ci_low"] >= 0.0)
        mark = " *SHIP*" if ship else ""
        print(f"{r['buffer']:>4d} {r['threshold']:>6.2f} {r['max_price']:>5.2f} "
              f"{r['cushion']:>5d} {r['n_kept']:>4d} "
              f"{r['total_pnl']:>+8.2f} {r['avg_pnl']:>+7.3f} "
              f"{r['ci_low']:>+7.3f} {r['ci_high']:>+7.3f} "
              f"{r['up_pnl']:>+7.2f} {r['down_pnl']:>+7.2f} "
              f"{r['win_rate']:>5.2f} {r.get('note',''):>15s}{mark}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="live_trades.db")
    p.add_argument("--out", default="scripts/_sweep_results.csv")
    args = p.parse_args()

    results = sweep(args.db)
    _write_csv(results, args.out)
    _print_top(results)

    ship = [
        r for r in results
        if r["avg_pnl"] >= SHIP_MIN_AVG_PNL
        and r["n_kept"] >= SHIP_MIN_N_KEPT
        and r["ci_low"] >= 0.0
    ]
    print(f"\n{len(ship)} combo(s) meet ship gate "
          f"(avg_pnl>={SHIP_MIN_AVG_PNL}, n>={SHIP_MIN_N_KEPT}, ci_low>=0).")
    if not ship:
        print("NO COMBO PASSES SHIP GATE. Do not update config; escalate to #13.")
        sys.exit(2)


if __name__ == "__main__":
    main()
```

### Step 2: Run the sweep

Run:
```
python -m scripts.sweep_joint_knobs
```

Expected: CSV at `scripts/_sweep_results.csv`, top-5 table printed, final line reporting count of ship-gate-passing combos. Exit code 0 if at least one passes, 2 if none.

### Step 3: Commit

```bash
git add scripts/sweep_joint_knobs.py
git commit -m "feat(scripts): joint-knob sweep with bootstrap CI and ship gate"
```

---

## Task 4: Monitoring check script (build before deploy)

**Files:**
- Create: `scripts/check_live_vs_projection.py`
- Create: `scripts/_ship_snapshot.json` (written by Task 5; referenced here)

### Step 1: Write the monitoring script

Create `scripts/check_live_vs_projection.py`:

```python
"""Post-deploy monitoring: live PnL vs pre-ship bootstrap CI.

Reads the stored ship snapshot JSON from Task 5 and the live_trades table.
If >=20 fills have landed since deploy_ts, compute avg per-trade PnL and
compare to the 95% CI from the sweep. Outside the CI -> print divergence.
"""
import argparse
import json
import sqlite3
import sys

DEFAULT_SNAPSHOT = "scripts/_ship_snapshot.json"
MIN_COHORT = 20


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="live_trades.db")
    p.add_argument("--snapshot", default=DEFAULT_SNAPSHOT)
    args = p.parse_args()

    with open(args.snapshot) as f:
        snap = json.load(f)

    c = sqlite3.connect(args.db)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        """SELECT pnl FROM trades
            WHERE status='settled'
              AND pnl IS NOT NULL
              AND timestamp >= ?
            ORDER BY id""",
        (snap["deploy_ts"],),
    ).fetchall()
    c.close()

    n = len(rows)
    print(f"Fills since deploy_ts={snap['deploy_ts']}: {n}")
    if n < MIN_COHORT:
        print(f"Need {MIN_COHORT - n} more fills before monitoring check.")
        sys.exit(0)

    pnls = [r["pnl"] for r in rows]
    avg = sum(pnls) / n
    total = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)

    lo, hi = snap["ci_low"], snap["ci_high"]
    proj = snap["avg_pnl"]
    print(f"Live:      n={n}  total={total:+.2f}  avg={avg:+.3f}  "
          f"wins={wins} ({wins/n*100:.1f}%)")
    print(f"Projected: avg={proj:+.3f}  95% CI=[{lo:+.3f}, {hi:+.3f}]")

    if lo <= avg <= hi:
        print("WITHIN CI -- projection holds. Close #12 and #13.")
        sys.exit(0)
    else:
        print("DIVERGENCE -- live PnL outside projection CI.")
        print("Action: re-open #13 with new corpus; do not re-run sweep until "
              "enough fresh fills exist at current knobs to be meaningful.")
        sys.exit(1)


if __name__ == "__main__":
    main()
```

### Step 2: Commit

```bash
git add scripts/check_live_vs_projection.py
git commit -m "feat(scripts): monitoring check — live PnL vs ship-time CI"
```

---

## Task 5: Pick the ship combo and write snapshot

**Files:**
- Create: `scripts/_ship_snapshot.json` (committed)

This is a human-in-the-loop decision step — there is no script to write, but the deliverable is a committed JSON file.

### Step 1: Inspect sweep output

Re-read `scripts/_sweep_results.csv` and the printed top-5 from Task 3 step 2.

Identify the combo with highest `avg_pnl` among those marked `*SHIP*`.

### Step 2: Sanity-check the picked combo

Confirm:
- `n_kept >= 30`
- `avg_pnl >= 0.25`
- `ci_low >= 0.0`
- `note` is empty (convergent)
- `up_pnl` and `down_pnl` are both >= 0 OR one is small enough that its loss doesn't imply per-side miscalibration

If all four hold, proceed. If `down_pnl` is negative and large in magnitude while `up_pnl` carries the combo: note this in the commit message; the DOWN-side freeze (per design Q3) may need revisiting next iteration.

**If no combo passes (exit code 2 from Task 3 step 2):** stop here. Record findings in issue #13, leave live trading paused, do not proceed to Task 6. The model itself needs work, not the knobs.

### Step 3: Write the snapshot

Get current UTC timestamp and git HEAD:
```
python -c "import datetime; print(datetime.datetime.utcnow().isoformat(timespec='seconds'))"
git rev-parse HEAD
```

Create `scripts/_ship_snapshot.json` with the picked combo and context. Example schema (fill with actual values):

```json
{
  "deploy_ts": "2026-04-23T21:00:00",
  "commit": "<git HEAD sha>",
  "buffer_ticks": 8,
  "cushion_ticks": 6,
  "min_edge_threshold": 0.07,
  "max_entry_price": 0.65,
  "n_kept": 34,
  "avg_pnl": 0.42,
  "total_pnl": 14.28,
  "ci_low": 0.08,
  "ci_high": 0.76,
  "up_pnl": 9.10,
  "down_pnl": 5.18,
  "win_rate": 0.62,
  "source_corpus": "live_trades.db n=84 fills post-2026-04-23 bid logging"
}
```

`deploy_ts` is the timestamp at which the new knobs go live (set it to just before you update config in Task 6; monitoring filters trades by `timestamp >= deploy_ts`).

### Step 4: Commit the snapshot

```bash
git add scripts/_ship_snapshot.json
git commit -m "chore: ship-snapshot for live-PnL re-fit (monitoring input)"
```

---

## Task 6: Update config to picked knobs + refresh stale comment

**Files:**
- Modify: `polypocket/config.py`

### Step 1: Update the four knob defaults

Using the combo from `scripts/_ship_snapshot.json`, change these lines in `polypocket/config.py` (exact values from snapshot):

- `MIN_EDGE_THRESHOLD = 0.03` → `MIN_EDGE_THRESHOLD = <picked>`
- `MAX_ENTRY_PRICE = 0.70` → `MAX_ENTRY_PRICE = <picked>`
- `SIGNAL_CUSHION_TICKS = int(os.getenv("SIGNAL_CUSHION_TICKS", "11"))` → `"...", "<picked>"`
- `IOC_BUFFER_TICKS = int(os.getenv("IOC_BUFFER_TICKS", "15"))` → `"...", "<picked>"`

### Step 2: Refresh the stale `IOC_BUFFER_TICKS` comment (#12 task 3)

Replace the comment at `polypocket/config.py:99-107` with wording that reflects the new reality. Example:

```python
# IOC buffer added to the pair-merge clearing price. For a BUY UP, the
# implied clearing price is `1 - best_down_bid`, and we post at that plus
# this many ticks to absorb DOWN-book churn during the ~200–500 ms signing
# window. Lowered from 15 to <N> on 2026-04-23 after the joint sweep: at 15t,
# live fills were landing near the IOC limit (slip distribution bunched at
# 11–18t), not at pair-merge clearing. Narrower buffer keeps fills closer to
# the clearing price and preserves edge. See docs/plans/2026-04-23-restore-
# live-pnl-design.md. Tune via IOC_BUFFER_TICKS env var.
```

Also update the `SIGNAL_CUSHION_TICKS` comment's "Calibrated on n=59 post-fix fills..." line to reflect the new n and median/mean used for the picked cushion.

### Step 3: Run tests

Run: `pytest tests/ -q`
Expected: all pass. If `tests/test_signal.py` or `tests/test_executor.py` hard-codes old threshold values, update those tests to pull from config or to assert against the new constants.

### Step 4: Commit

```bash
git add polypocket/config.py tests/
git commit -m "config: update live knobs + refresh IOC_BUFFER_TICKS comment (#12 task 3)

Joint-sweep result: buffer=<N>, cushion=<N>, threshold=<N>, max_price=<N>.
Projected avg PnL <+N> / trade (95% CI [<lo>, <hi>]) on n_kept=<N>.
See scripts/_ship_snapshot.json and docs/plans/2026-04-23-restore-live-pnl-design.md."
```

---

## Task 7: Deploy + monitoring (manual, human-owned)

**Files:** none — this is operational.

### Step 1: Deploy

Restart the live bot. Confirm via logs that it picks up the new config values (they read from env with defaults, so a restart is sufficient unless env vars override them).

### Step 2: Let ≥20 fills accumulate

No intervention. Wait for cohort.

### Step 3: Run monitoring check

Run: `python scripts/check_live_vs_projection.py`

Expected outcomes:
- **Exit 0, "WITHIN CI":** close #11, #12, #13.
- **Exit 0, "Need N more fills":** wait.
- **Exit 1, "DIVERGENCE":** re-open #13 with the new live fill data as part of the next corpus; do NOT immediately re-run the sweep on the small diverged cohort — that risks curve-fitting on 20 points.

### Step 4: Close the issues (if WITHIN CI)

```bash
gh issue close 11 --comment "Item 2 (replay rewrite) shipped in Task 2; items 3/4 validated by monitoring cohort."
gh issue close 12 --comment "IOC_BUFFER_TICKS re-tuned via joint sweep; monitoring cohort WITHIN CI."
gh issue close 13 --comment "Joint sweep found +EV combo; monitoring cohort WITHIN CI."
```

---

## Execution notes

- Tasks 1, 2, 3, 4 can all be completed before the sweep is run (they are just code + smoke).
- Task 5 is the first human-judgment step (picking the combo).
- Tasks 6 and 7 are the live-deploy path.
- If the sweep fails the ship gate, stop at Task 5 — the remaining tasks don't execute.
- The plan is linear; each task is small enough to review individually before continuing.
