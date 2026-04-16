# Fat-Tail Model Calibration — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace `norm.cdf` with `t.cdf` in `compute_model_p_up` to fix overconfidence at probability extremes, and add a df sweep tool to find the optimal degrees of freedom.

**Architecture:** Add `MODEL_TAIL_DF` config constant, swap the distribution in `observer.py`, fix affected tests, then add a sweep script that evaluates df values against historical trades from both DBs.

**Tech Stack:** Python, scipy.stats (already used — `norm` → add `t`), sqlite3, pytest

---

### Task 1: Add MODEL_TAIL_DF config constant

**Files:**
- Modify: `polypocket/config.py`
- Test: `tests/test_config.py`

**Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
from polypocket.config import MODEL_TAIL_DF

def test_model_tail_df_exists():
    assert isinstance(MODEL_TAIL_DF, (int, float))
    assert MODEL_TAIL_DF > 1
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py::test_model_tail_df_exists -v`
Expected: FAIL with `ImportError`

**Step 3: Write minimal implementation**

In `polypocket/config.py`, add after `VOLATILITY_LOOKBACK = 50`:

```python
MODEL_TAIL_DF = 4  # degrees of freedom for t-distribution; lower = fatter tails
```

Note: 4 is a starting guess — the sweep tool in Task 4 will find the optimal value.

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add polypocket/config.py tests/test_config.py
git commit -m "feat: add MODEL_TAIL_DF config constant"
```

---

### Task 2: Replace norm.cdf with t.cdf in compute_model_p_up

**Files:**
- Modify: `polypocket/observer.py:1-50`
- Test: `tests/test_observer.py`

**Step 1: Write the failing test**

Add to `tests/test_observer.py`:

```python
def test_compute_model_p_up_fat_tails():
    """With t-distribution, extreme displacements should NOT produce near-1.0 probabilities."""
    # Large positive displacement that would give ~0.977 with normal dist
    probability = compute_model_p_up(
        displacement=0.002,
        t_remaining=120.0,
        sigma_5min=0.0012,
    )
    # With t-distribution (df~4), this should be noticeably below what norm.cdf gives
    # norm.cdf would give ~0.99+, t.cdf should give something lower
    assert probability > 0.5  # still directionally correct
    assert probability < 0.97  # but NOT as extreme as normal dist
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_observer.py::test_compute_model_p_up_fat_tails -v`
Expected: FAIL because current `norm.cdf` gives ~0.99+ for this input

**Step 3: Write implementation**

In `polypocket/observer.py`, change the import and function:

Current (line ~1-2):
```python
from math import sqrt
from scipy.stats import norm
```

Change to:
```python
from math import sqrt
from scipy.stats import t as t_dist

from polypocket.config import MODEL_TAIL_DF
```

Current (line 50):
```python
return float(norm.cdf(displacement / sigma_remaining))
```

Change to:
```python
return float(t_dist.cdf(displacement / sigma_remaining, df=MODEL_TAIL_DF))
```

**Step 4: Run the new test**

Run: `python -m pytest tests/test_observer.py::test_compute_model_p_up_fat_tails -v`
Expected: PASS

**Step 5: Run ALL observer tests**

Run: `python -m pytest tests/test_observer.py -v`

Some existing tests may need adjustment:

- `test_compute_model_p_up_near_expiry`: asserts `probability > 0.99` for displacement=0.001, t_remaining=1.0. With df=4 and very little time left, sigma_remaining is tiny, so the z-score is huge and t.cdf still gives ~1.0. Should still pass.
- `test_compute_model_p_up_no_displacement`: asserts `isclose(probability, 0.5)`. t.cdf(0, df) = 0.5 for any df. Will pass.
- `test_compute_model_p_up_btc_above_open` and `btc_below_open`: assert > 0.5 and < 0.5 respectively. Will pass (direction unchanged).

If any test fails, adjust the assertion thresholds to account for the fatter-tailed distribution while preserving the test's intent.

**Step 6: Commit**

```bash
git add polypocket/observer.py tests/test_observer.py
git commit -m "feat: replace norm.cdf with t.cdf for fat-tail calibration"
```

---

### Task 3: Fix downstream test breakage

**Files:**
- Modify: `tests/test_signal.py` (if needed)
- Run: full test suite

**Step 1: Run full test suite**

Run: `python -m pytest -v`

The signal tests use specific displacement values to produce model_p_up in certain ranges. With t.cdf instead of norm.cdf, the exact probabilities change. Tests that check for specific thresholds may need displacement adjustments.

Key tests to watch:
- `test_signal_engine_up_signal_uses_fee_adjusted_up_ask` (displacement=0.002) — model_p_up will be lower with t-dist, may still fire if above 0.65
- `test_signal_engine_fires_when_model_strongly_aligned` (displacement=0.003) — same concern
- `test_signal_engine_no_up_signal_below_65_confidence` (displacement=0.0003) — needs model_p_up between 0.60 and 0.65, may shift

**Step 2: For each failing test, check the new model_p_up value**

```python
from polypocket.observer import compute_model_p_up
# Check what each test's displacement now produces
print(compute_model_p_up(0.002, 180.0, 0.0012))   # up_signal test
print(compute_model_p_up(0.003, 180.0, 0.0012))   # strongly_aligned test
print(compute_model_p_up(0.0003, 180.0, 0.0012))  # below_65 test
```

**Step 3: Adjust displacement values in tests so they test the same behavior**

For tests that need model_p_up above 0.65: increase displacement if needed.
For the below-65 test: find a displacement that gives model_p_up between 0.60 and 0.65 under t-dist.

Do NOT change what the tests verify — only adjust the inputs to hit the same probability ranges under the new distribution.

**Step 4: Run full test suite**

Run: `python -m pytest -v`
Expected: All PASS

**Step 5: Commit if any changes were needed**

```bash
git add tests/
git commit -m "fix: adjust test displacements for t-distribution"
```

---

### Task 4: Add df sweep tool

**Files:**
- Create: `scripts/sweep_df.py`

**Step 1: Write the sweep script**

```python
"""Sweep MODEL_TAIL_DF values to find optimal calibration."""

import sqlite3
from contextlib import closing

from scipy.stats import t as t_dist
from math import sqrt


def fetch_trades(*db_paths: str) -> list[dict]:
    all_trades = []
    for db_path in db_paths:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT t.side, t.model_p_up, t.outcome, t.pnl,
                       s.displacement, s.sigma_5min, s.t_remaining
                FROM trades t
                LEFT JOIN window_snapshots s
                    ON t.window_slug = s.window_slug AND s.snapshot_type = 'decision'
                WHERE t.status = 'settled'
                  AND s.displacement IS NOT NULL
                  AND s.sigma_5min IS NOT NULL
                  AND s.t_remaining IS NOT NULL
            """).fetchall()
            all_trades.extend(dict(r) for r in rows)
    return all_trades


def recompute_p_up(displacement: float, t_remaining: float, sigma_5min: float, df: float) -> float:
    if t_remaining <= 0 or sigma_5min <= 0:
        if displacement > 0:
            return 1.0
        if displacement < 0:
            return 0.0
        return 0.5
    sigma_remaining = sigma_5min * sqrt(t_remaining / 300.0)
    if sigma_remaining <= 0:
        return 0.5
    return float(t_dist.cdf(displacement / sigma_remaining, df=df))


def calibration_error(trades: list[dict], df: float) -> dict:
    """Compute mean absolute calibration error for a given df."""
    buckets: dict[str, list] = {}
    for t in trades:
        p_up = recompute_p_up(t["displacement"], t["t_remaining"], t["sigma_5min"], df)
        decile = min(int(p_up * 10) / 10, 0.9)
        label = f"{decile:.1f}-{decile + 0.1:.1f}"
        actual_up = 1 if t["outcome"] == "up" else 0
        buckets.setdefault(label, []).append((p_up, actual_up))

    total_abs_error = 0.0
    total_n = 0
    for items in buckets.values():
        n = len(items)
        predicted = sum(p for p, _ in items) / n
        actual = sum(a for _, a in items) / n
        total_abs_error += abs(actual - predicted) * n
        total_n += n

    mae = total_abs_error / max(total_n, 1)

    # Also compute win rate if we used this df for side selection
    wins = 0
    for t in trades:
        p_up = recompute_p_up(t["displacement"], t["t_remaining"], t["sigma_5min"], df)
        predicted_side = "up" if p_up >= 0.5 else "down"
        if predicted_side == t["outcome"]:
            wins += 1

    return {"df": df, "mae": mae, "direction_accuracy": wins / len(trades), "n": total_n}


def main():
    import sys
    db_paths = sys.argv[1:] if sys.argv[1:] else ["paper_trades.db"]
    trades = fetch_trades(*db_paths)
    print(f"Loaded {len(trades)} trades from {', '.join(db_paths)}\n")

    # Sweep df from 2 to 20, plus 50 and 1000 (approximates normal)
    df_values = [2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 50, 1000]

    print(f"{'df':>6}  {'MAE':>8}  {'Dir Acc':>8}  {'N':>5}")
    print("-" * 35)

    best = None
    for df in df_values:
        result = calibration_error(trades, df)
        marker = ""
        if best is None or result["mae"] < best["mae"]:
            best = result
            marker = " <-- best"
        print(f"{df:>6}  {result['mae']:>8.4f}  {result['direction_accuracy']:>8.1%}  {result['n']:>5}{marker}")

    print(f"\nBest df: {best['df']} (MAE={best['mae']:.4f})")


if __name__ == "__main__":
    main()
```

**Step 2: Run the sweep against both DBs**

Run: `python scripts/sweep_df.py paper_trades.db paper_trades.bak.db`

This will output the optimal df value. Note the result — it will be used to update `MODEL_TAIL_DF` in config.py.

**Step 3: Update MODEL_TAIL_DF if the optimal value differs from 4**

In `polypocket/config.py`, change `MODEL_TAIL_DF = 4` to whatever the sweep identified as best.

Also update `tests/test_config.py` if the test checks a specific value.

**Step 4: Run full test suite after config change**

Run: `python -m pytest -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add scripts/sweep_df.py polypocket/config.py tests/test_config.py
git commit -m "feat: add df sweep tool, set MODEL_TAIL_DF to optimal value"
```

---

### Task 5: Generate post-change calibration report

**Step 1: Run calibration report with both DBs**

Run: `python -m polypocket.analyze --calibration paper_trades.bak.db`

Compare the gaps to the pre-change baseline:
- 0.9-1.0 bucket: was -30.2% gap, should be significantly reduced
- 0.0-0.1 bucket: was +21.2% gap, should be reduced
- 0.3-0.4 bucket: was -4.9% gap, should stay similar

**Step 2: Verify the bot still works end-to-end**

Run: `python -m pytest -v`
Expected: All PASS

**Step 3: Final commit if any cleanup needed**

```bash
git add -A
git commit -m "chore: final cleanup for fat-tail calibration"
```
