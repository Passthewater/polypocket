# UP Confidence Threshold + Model Calibration — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Raise the UP side confidence threshold to 65% and add a trade-based calibration report to analyze model accuracy.

**Architecture:** Phase 1 adds a second config constant `MIN_MODEL_CONFIDENCE_UP = 0.65` and wires it into the signal engine's UP alignment check. Phase 2 adds a `calibration_report()` function to `analyze.py` that bins settled trades by `model_p_up` decile and compares predicted vs actual outcomes, with optional multi-DB support.

**Tech Stack:** Python, sqlite3, pytest

---

### Task 1: Add `MIN_MODEL_CONFIDENCE_UP` config constant

**Files:**
- Modify: `polypocket/config.py:10-11`
- Test: `tests/test_config.py`

**Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
from polypocket.config import MIN_MODEL_CONFIDENCE, MIN_MODEL_CONFIDENCE_UP

def test_up_confidence_threshold_is_higher():
    assert MIN_MODEL_CONFIDENCE_UP == 0.65
    assert MIN_MODEL_CONFIDENCE_UP > MIN_MODEL_CONFIDENCE
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py::test_up_confidence_threshold_is_higher -v`
Expected: FAIL with `ImportError` (MIN_MODEL_CONFIDENCE_UP not defined)

**Step 3: Write minimal implementation**

In `polypocket/config.py`, add after line 11 (`MIN_MODEL_CONFIDENCE = 0.60`):

```python
MIN_MODEL_CONFIDENCE_UP = 0.65
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add polypocket/config.py tests/test_config.py
git commit -m "feat: add MIN_MODEL_CONFIDENCE_UP config constant (0.65)"
```

---

### Task 2: Wire UP threshold into signal engine

**Files:**
- Modify: `polypocket/signal.py:5-8,54`
- Test: `tests/test_signal.py`

**Step 1: Write the failing test**

Add to `tests/test_signal.py`:

```python
def test_signal_engine_no_up_signal_below_65_confidence():
    """model_p_up between 0.60-0.65 should NOT fire an UP signal."""
    engine = SignalEngine()
    # displacement=0.0015 with sigma_5min=0.0012, t_remaining=180
    # produces model_p_up around 0.62 (between old 0.60 and new 0.65)
    signal = engine.evaluate(
        displacement=0.0015,
        t_elapsed=120.0,
        t_remaining=180.0,
        sigma_5min=0.0012,
        up_ask=0.45,
        down_ask=0.80,
    )
    # If signal fires, it should NOT be up (model_p_up is between 0.60 and 0.65)
    if signal is not None:
        assert signal.side != "up", (
            f"UP signal fired with model_p_up={signal.model_p_up:.3f}, "
            f"should require >= 0.65"
        )
```

Note: The exact model_p_up value depends on `compute_model_p_up`. Before committing to this test, first run a quick check:

```python
from polypocket.observer import compute_model_p_up
print(compute_model_p_up(0.0015, 180.0, 0.0012))
```

If the value is not between 0.60 and 0.65, adjust the displacement parameter until it lands in that range. The test needs a model_p_up that passes the OLD threshold (0.60) but fails the NEW one (0.65).

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_signal.py::test_signal_engine_no_up_signal_below_65_confidence -v`
Expected: FAIL (signal fires with side="up" under old 0.60 threshold)

**Step 3: Write minimal implementation**

In `polypocket/signal.py`, update the import:

```python
from polypocket.config import (
    FEE_RATE,
    MIN_EDGE_THRESHOLD,
    MIN_MODEL_CONFIDENCE,
    MIN_MODEL_CONFIDENCE_UP,
    WINDOW_ENTRY_MIN_ELAPSED,
    WINDOW_ENTRY_MIN_REMAINING,
)
```

Change line 54 from:

```python
up_aligned = model_p_up >= MIN_MODEL_CONFIDENCE
```

to:

```python
up_aligned = model_p_up >= MIN_MODEL_CONFIDENCE_UP
```

Line 55 stays unchanged (`down_aligned` still uses `MIN_MODEL_CONFIDENCE`).

**Step 4: Run ALL signal tests to verify nothing breaks**

Run: `python -m pytest tests/test_signal.py -v`
Expected: All PASS

Also verify the existing `test_signal_engine_fires_when_model_strongly_aligned` still passes — that test uses displacement=0.003 which should produce model_p_up well above 0.65.

**Step 5: Run full test suite**

Run: `python -m pytest -v`
Expected: All PASS

**Step 6: Commit**

```bash
git add polypocket/signal.py tests/test_signal.py
git commit -m "feat: raise UP confidence threshold to 65%"
```

---

### Task 3: Add trade-based calibration report function

**Files:**
- Modify: `polypocket/analyze.py`
- Test: `tests/test_config.py` (or new `tests/test_analyze.py` if needed)

**Step 1: Write the failing test**

Create a simple test that the function exists and returns expected structure. Add to a suitable test file:

```python
def test_calibration_report_returns_string():
    from polypocket.analyze import calibration_report
    # Use current DB (which has settled trades)
    result = calibration_report()
    assert isinstance(result, str)
    assert "Calibration Report" in result
    assert "Bucket" in result
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py::test_calibration_report_returns_string -v` (or wherever you placed it)
Expected: FAIL with `ImportError` (calibration_report not defined)

**Step 3: Write implementation**

Add to `polypocket/analyze.py` (after the existing `generate_report` function, before `main`):

```python
def calibration_report(*db_paths: str) -> str:
    """Trade-based calibration: bins settled trades by model_p_up decile.

    Pass multiple DB paths to merge data (e.g., current + backup).
    Defaults to PAPER_DB_PATH if no paths given.
    """
    if not db_paths:
        db_paths = (PAPER_DB_PATH,)

    all_trades = []
    for db_path in db_paths:
        all_trades.extend(
            _fetch_all(db_path, "SELECT * FROM trades WHERE status='settled' AND model_p_up IS NOT NULL")
        )

    if not all_trades:
        return "Calibration Report\n\nNo settled trades with model_p_up data."

    lines: list[str] = []
    lines.append(f"# Calibration Report (N={len(all_trades)} trades)\n")

    # Bucket by model_p_up decile
    buckets: dict[str, list[dict]] = {}
    for t in all_trades:
        decile = int(t["model_p_up"] * 10) / 10  # floor to 0.0, 0.1, ..., 0.9
        decile = min(decile, 0.9)  # clamp 1.0 into 0.9-1.0 bucket
        label = f"{decile:.1f}-{decile + 0.1:.1f}"
        buckets.setdefault(label, []).append(t)

    # Header
    lines.append("| Bucket | Trades | Predicted P(up) | Actual P(up) | Gap | Side | Win Rate | PnL |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")

    for label in sorted(buckets.keys()):
        items = buckets[label]
        n = len(items)
        predicted = sum(t["model_p_up"] for t in items) / n
        actual_up = sum(1 for t in items if t["outcome"] == "up") / n
        gap = actual_up - predicted

        # Win rate (did the side match the outcome?)
        wins = sum(1 for t in items if t["side"] == t["outcome"])
        win_rate = wins / n

        # PnL
        total_pnl = sum(t["pnl"] for t in items if t["pnl"] is not None)

        # Side distribution
        up_count = sum(1 for t in items if t["side"] == "up")
        down_count = n - up_count
        side_str = f"{up_count}U/{down_count}D"

        flag = " ⚠" if abs(gap) > 0.10 else ""
        lines.append(
            f"| {label} | {n} | {predicted:.1%} | {actual_up:.1%} | {gap:+.1%}{flag} | {side_str} | {win_rate:.0%} | ${total_pnl:+.2f} |"
        )

    # Overall stats
    total_predicted = sum(t["model_p_up"] for t in all_trades) / len(all_trades)
    total_actual = sum(1 for t in all_trades if t["outcome"] == "up") / len(all_trades)
    total_wins = sum(1 for t in all_trades if t["side"] == t["outcome"])
    total_pnl = sum(t["pnl"] for t in all_trades if t["pnl"] is not None)

    lines.append("")
    lines.append(f"**Overall:** Predicted P(up)={total_predicted:.1%}, Actual P(up)={total_actual:.1%}, "
                 f"Win rate={total_wins}/{len(all_trades)} ({100*total_wins/len(all_trades):.0f}%), "
                 f"PnL=${total_pnl:+.2f}")

    return "\n".join(lines)
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py::test_calibration_report_returns_string -v`
Expected: PASS

**Step 5: Commit**

```bash
git add polypocket/analyze.py tests/test_config.py
git commit -m "feat: add trade-based calibration report function"
```

---

### Task 4: Add CLI entry point for calibration report

**Files:**
- Modify: `polypocket/analyze.py` (update `main` or add argument parsing)

**Step 1: Update main to support `--calibration` flag**

In `polypocket/analyze.py`, update the `main()` function:

```python
def main() -> None:
    import sys
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    datestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if "--calibration" in sys.argv:
        # Support merging backup DB: --calibration [extra_db_path ...]
        extra_dbs = [a for a in sys.argv[1:] if a != "--calibration" and not a.startswith("-")]
        db_paths = (PAPER_DB_PATH, *extra_dbs)
        report = calibration_report(*db_paths)
        filename = f"{datestamp}-calibration.md"
    else:
        report = generate_report()
        filename = f"{datestamp}-analysis.md"

    path = reports_dir / filename
    path.write_text(report, encoding="utf-8")
    print(report)
    print(f"\nReport saved to {path}")
```

**Step 2: Manual test**

Run: `python -m polypocket.analyze --calibration`
Expected: Calibration table printed to console and saved to `reports/2026-04-15-calibration.md`

Run: `python -m polypocket.analyze --calibration paper_trades.bak.db`
Expected: Merged report with trades from both DBs

**Step 3: Commit**

```bash
git add polypocket/analyze.py
git commit -m "feat: add --calibration CLI flag to analyze.py"
```

---

### Task 5: Run full verification

**Step 1: Run full test suite**

Run: `python -m pytest -v`
Expected: All PASS

**Step 2: Generate both reports**

Run: `python -m polypocket.analyze --calibration paper_trades.bak.db`
Expected: Merged calibration report showing all ~190 trades from both DBs

**Step 3: Final commit if any cleanup needed**

```bash
git add -A
git commit -m "chore: final cleanup for UP threshold + calibration"
```
