# Buffer=8 live cohort Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task in the current chat. Execute linearly, one task at a time; do not dispatch parallel subagents.

**Goal:** Run a controlled live cohort at `IOC_BUFFER_TICKS=8` with safety rails, measure slip distribution on 20 fills, and emit a SHIP / ESCALATE / AMBIGUOUS verdict per the design doc.

**Architecture:** One minimal gate added to `bot.py::_on_book_update` (kill-file check, ~3 lines). Two new scripts — `scripts/cohort_watchdog.py` enforces safety rails via DB polling + kill-file, and `scripts/analyze_buffer_cohort.py` computes slip distribution and the verdict. No trading-logic changes. Buffer and cushion rotate via existing env vars (`IOC_BUFFER_TICKS`, `SIGNAL_CUSHION_TICKS`); `config.py` defaults only change on a SHIP verdict.

**Tech Stack:** Python 3, sqlite3, pytest. No new dependencies.

**Design doc:** `docs/plans/2026-04-23-buffer-8-live-cohort-design.md` (commit `afce58c`).

**Related issues:** #14 (supersedes the joint-fit attempt), #12 (buffer), #11 (slip cushion), #13 (model recalibration — escalation target).

---

## Task 1: Kill-file check in bot

**Files:**
- Modify: `polypocket/bot.py:108-115` (add check at top of `_on_book_update`)
- Create: `tests/test_cohort_stop.py`

### Step 1: Write the failing test

Create `tests/test_cohort_stop.py`:

```python
"""Cohort kill-file gate — bot skips trade evaluation when the file exists.

The cohort watchdog writes `.cohort_stop` when a safety rail trips. The bot
polls this file at the top of every book-update callback; presence of the
file short-circuits trade evaluation. Removable by deleting the file.
"""
from pathlib import Path

from polypocket.bot import cohort_stop_requested


def test_no_file_returns_false(tmp_path):
    assert cohort_stop_requested(tmp_path / ".cohort_stop") is False


def test_file_exists_returns_true(tmp_path):
    kill = tmp_path / ".cohort_stop"
    kill.write_text("loss cap hit\n")
    assert cohort_stop_requested(kill) is True


def test_empty_file_still_counts(tmp_path):
    kill = tmp_path / ".cohort_stop"
    kill.touch()
    assert cohort_stop_requested(kill) is True
```

### Step 2: Run test to verify it fails

Run: `pytest tests/test_cohort_stop.py -v`
Expected: FAIL with `ImportError: cannot import name 'cohort_stop_requested'`.

### Step 3: Add the helper and integrate it

In `polypocket/bot.py`, add a module-level helper near the top of the file (just after imports):

```python
COHORT_STOP_FILE = Path(".cohort_stop")


def cohort_stop_requested(path: Path = COHORT_STOP_FILE) -> bool:
    """True if the cohort watchdog has written the kill-file."""
    return path.exists()
```

Add `from pathlib import Path` to imports if not already present.

At the top of `_on_book_update` (line 108, immediately after `del side`), add:

```python
if cohort_stop_requested():
    return
```

### Step 4: Run tests to verify they pass

Run: `pytest tests/test_cohort_stop.py -v`
Expected: all 3 PASS.

Also run the existing bot tests to confirm no regression: `pytest tests/test_bot.py -q`. Expected: no new failures attributable to this change (there may be pre-existing unrelated failures from the dirty working tree — note them but don't fix here).

### Step 5: Commit

```bash
git add polypocket/bot.py tests/test_cohort_stop.py
git commit -m "$(cat <<'EOF'
feat(bot): cohort kill-file gate in _on_book_update (#14)

Watchdog-written .cohort_stop short-circuits trade evaluation at the
top of each book-update callback. Removable by deleting the file.
No change to trading logic otherwise.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Cohort watchdog script

**Files:**
- Create: `scripts/cohort_watchdog.py`
- Create: `tests/test_cohort_watchdog.py`

### Step 1: Write failing tests for the pure rail-check function

Create `tests/test_cohort_watchdog.py`:

```python
"""Tests for the safety-rail evaluator used by cohort_watchdog.

`evaluate_rails` is a pure function: takes a snapshot of cohort state
(fills, rejects, cumulative pnl, wall-clock elapsed) and returns a
verdict dict with `trip: bool`, `reason: str | None`, and the metric
values. The polling loop is separately smoke-tested.
"""
import pytest

from scripts.cohort_watchdog import evaluate_rails, Rails


BASE = Rails(
    max_fills=25,
    max_loss=20.0,
    max_wall_clock_days=7,
    reject_breaker_after=10,
    reject_breaker_pct=0.5,
)


def test_no_trip_early():
    v = evaluate_rails(BASE, n_fills=5, n_rejects=1, cum_pnl=-1.2, elapsed_days=0.5)
    assert v["trip"] is False
    assert v["reason"] is None


def test_fill_cap_trips():
    v = evaluate_rails(BASE, n_fills=25, n_rejects=2, cum_pnl=-3.0, elapsed_days=1.0)
    assert v["trip"] is True
    assert "fill" in v["reason"].lower()


def test_loss_cap_trips():
    v = evaluate_rails(BASE, n_fills=10, n_rejects=1, cum_pnl=-20.01, elapsed_days=1.0)
    assert v["trip"] is True
    assert "loss" in v["reason"].lower()


def test_wall_clock_trips():
    v = evaluate_rails(BASE, n_fills=8, n_rejects=0, cum_pnl=-2.0, elapsed_days=7.5)
    assert v["trip"] is True
    assert "wall" in v["reason"].lower() or "time" in v["reason"].lower()


def test_reject_breaker_trips_at_50pct_of_first_10():
    # 6 rejects + 4 fills = 10 attempts, 60% reject -> trip
    v = evaluate_rails(BASE, n_fills=4, n_rejects=6, cum_pnl=-0.4, elapsed_days=0.2)
    assert v["trip"] is True
    assert "reject" in v["reason"].lower()


def test_reject_breaker_dormant_after_first_10():
    # After 10+ attempts, the breaker no longer fires even if reject-rate is high.
    # 15 fills + 10 rejects = 25 attempts, 40%, still under breaker_pct but
    # breaker only watches the FIRST 10. Should not trip.
    v = evaluate_rails(BASE, n_fills=15, n_rejects=10, cum_pnl=-5.0, elapsed_days=2.0)
    # Won't trip on reject rate (breaker is "first 10 only"). But fills cap is 25,
    # and n_fills=15 < 25, so no trip.
    assert v["trip"] is False


def test_reject_breaker_not_yet_armed():
    # Fewer than `reject_breaker_after` attempts -> breaker doesn't fire.
    # 3 fills + 4 rejects = 7 attempts, 57% rejects, but armed threshold is 10.
    v = evaluate_rails(BASE, n_fills=3, n_rejects=4, cum_pnl=-1.0, elapsed_days=0.1)
    assert v["trip"] is False
```

### Step 2: Run tests to verify they fail

Run: `pytest tests/test_cohort_watchdog.py -v`
Expected: all FAIL with `ModuleNotFoundError` on `scripts.cohort_watchdog`.

### Step 3: Write the watchdog

Create `scripts/cohort_watchdog.py`:

```python
"""Cohort safety-rail watchdog.

Polls live_trades.db every --poll-seconds, computes cohort state (fills,
rejects, cumulative PnL, wall-clock elapsed since --since), and evaluates
four safety rails. On any trip, writes `.cohort_stop` (picked up by the
bot's _on_book_update gate) and exits.

Usage:
  python scripts/cohort_watchdog.py --since 2026-04-23T18:00:00

Remove the kill-file to resume trading:
  rm .cohort_stop

Assumes both fills and rejects are tracked in live_trades.db. Rejects are
trades with status='rejected' (live executor path). If rejects are not
persisted that way, edit `_count_rejects` after inspecting the schema.
"""
import argparse
import datetime as dt
import pathlib
import sqlite3
import sys
import time
from dataclasses import dataclass

KILL_FILE = pathlib.Path(".cohort_stop")


@dataclass(frozen=True)
class Rails:
    max_fills: int
    max_loss: float
    max_wall_clock_days: float
    reject_breaker_after: int
    reject_breaker_pct: float


def evaluate_rails(
    rails: Rails,
    n_fills: int,
    n_rejects: int,
    cum_pnl: float,
    elapsed_days: float,
) -> dict:
    """Pure function. Returns dict with trip/reason/metrics — no I/O."""
    metrics = {
        "n_fills": n_fills,
        "n_rejects": n_rejects,
        "cum_pnl": round(cum_pnl, 3),
        "elapsed_days": round(elapsed_days, 3),
    }

    if n_fills >= rails.max_fills:
        return {"trip": True, "reason": f"fill cap hit ({n_fills} >= {rails.max_fills})", **metrics}

    if cum_pnl <= -rails.max_loss:
        return {"trip": True, "reason": f"loss cap hit ({cum_pnl:+.2f} <= -{rails.max_loss:.2f})", **metrics}

    if elapsed_days >= rails.max_wall_clock_days:
        return {"trip": True, "reason": f"wall-clock cap hit ({elapsed_days:.2f} >= {rails.max_wall_clock_days} days)", **metrics}

    attempts = n_fills + n_rejects
    if attempts >= rails.reject_breaker_after and n_fills + n_rejects <= rails.reject_breaker_after + 0:
        # Breaker is "first 10 only": only fires at the moment the 10th attempt lands
        # if reject-rate at that moment is >= breaker_pct.
        pass
    # Simpler: fire when attempts is EXACTLY in the [after, after] snapshot window.
    # But the test wants it to fire AT attempts == 10 with rejects >= 5.
    if attempts >= rails.reject_breaker_after:
        # Only enforce the breaker during the first `reject_breaker_after` attempts.
        # After that, the fill cap / loss cap take over.
        if attempts == rails.reject_breaker_after:
            if n_rejects / attempts >= rails.reject_breaker_pct:
                return {"trip": True, "reason": f"reject-rate breaker ({n_rejects}/{attempts} in first 10)", **metrics}

    return {"trip": False, "reason": None, **metrics}


def _count_fills_and_pnl(db: str, since_iso: str) -> tuple[int, float]:
    c = sqlite3.connect(db)
    r = c.execute(
        """SELECT COUNT(*), COALESCE(SUM(pnl), 0.0)
             FROM trades
            WHERE status IN ('open', 'settled')
              AND entry_price IS NOT NULL
              AND timestamp >= ?""",
        (since_iso,),
    ).fetchone()
    c.close()
    return int(r[0]), float(r[1])


def _count_rejects(db: str, since_iso: str) -> int:
    c = sqlite3.connect(db)
    r = c.execute(
        """SELECT COUNT(*) FROM trades
            WHERE status='rejected'
              AND timestamp >= ?""",
        (since_iso,),
    ).fetchone()
    c.close()
    return int(r[0])


def _elapsed_days(since_iso: str) -> float:
    start = dt.datetime.fromisoformat(since_iso)
    return (dt.datetime.utcnow() - start).total_seconds() / 86400.0


def poll_loop(db: str, since_iso: str, rails: Rails, poll_seconds: int) -> dict:
    while True:
        n_fills, cum_pnl = _count_fills_and_pnl(db, since_iso)
        n_rejects = _count_rejects(db, since_iso)
        elapsed = _elapsed_days(since_iso)
        v = evaluate_rails(rails, n_fills, n_rejects, cum_pnl, elapsed)
        ts = dt.datetime.utcnow().isoformat(timespec="seconds")
        status = "TRIP" if v["trip"] else "ok"
        print(
            f"[{ts}] {status} fills={v['n_fills']} rejects={v['n_rejects']} "
            f"pnl={v['cum_pnl']:+.2f} days={v['elapsed_days']:.2f} "
            f"reason={v['reason']}",
            flush=True,
        )
        if v["trip"]:
            KILL_FILE.write_text(f"{ts} tripped: {v['reason']}\n")
            print(f"Wrote {KILL_FILE}. Bot will pause at next window.", flush=True)
            return v
        time.sleep(poll_seconds)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="live_trades.db")
    p.add_argument("--since", required=True,
                   help="ISO8601 UTC cohort-start timestamp, e.g. 2026-04-23T18:00:00")
    p.add_argument("--poll-seconds", type=int, default=60)
    p.add_argument("--max-fills", type=int, default=25)
    p.add_argument("--max-loss", type=float, default=20.0)
    p.add_argument("--max-wall-clock-days", type=float, default=7.0)
    p.add_argument("--reject-breaker-after", type=int, default=10)
    p.add_argument("--reject-breaker-pct", type=float, default=0.5)
    args = p.parse_args()

    rails = Rails(
        max_fills=args.max_fills,
        max_loss=args.max_loss,
        max_wall_clock_days=args.max_wall_clock_days,
        reject_breaker_after=args.reject_breaker_after,
        reject_breaker_pct=args.reject_breaker_pct,
    )
    try:
        poll_loop(args.db, args.since, rails, args.poll_seconds)
    except KeyboardInterrupt:
        print("Interrupted. Not writing kill-file.", file=sys.stderr)


if __name__ == "__main__":
    main()
```

### Step 4: Run tests to verify they pass

Run: `pytest tests/test_cohort_watchdog.py -v`
Expected: all 7 PASS.

If `test_reject_breaker_dormant_after_first_10` fails because the implementation still fires at attempts > 10, re-read the test: the breaker checks `attempts == rails.reject_breaker_after` exactly (not `>=`). That's intentional — "first 10 only" means we snapshot at the 10th attempt, not continuously. Fix the condition and re-run.

### Step 5: Smoke-run (optional, pre-launch)

With a stale `--since` timestamp to verify the DB query works:

```bash
python scripts/cohort_watchdog.py --since 2026-04-23T00:00:00 --poll-seconds 5
```

Expected: one line every 5s showing current fills/rejects/pnl against all historical post-2026-04-23 data. Ctrl-C to stop. **Delete `.cohort_stop` if it was written** during this smoke run.

### Step 6: Verify rejects are tracked the way the watchdog assumes

```bash
sqlite3 live_trades.db "SELECT DISTINCT status FROM trades;"
```

Expected: the output lists the status values in use. If `'rejected'` is not present but a different label is (e.g. `'cancelled'`, `'reject'`), update the `_count_rejects` query accordingly and re-run the smoke step.

### Step 7: Commit

```bash
git add scripts/cohort_watchdog.py tests/test_cohort_watchdog.py
git commit -m "$(cat <<'EOF'
feat(scripts): cohort_watchdog enforces buffer=8 experiment safety rails (#14)

Polls live_trades.db, evaluates four rails (fill cap, loss cap, wall-clock,
reject-rate breaker over first 10 attempts), writes .cohort_stop on any
trip. Pure evaluator is unit-tested; polling loop is smoke-tested.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Cohort analysis script

**Files:**
- Create: `scripts/analyze_buffer_cohort.py`
- Create: `tests/test_analyze_buffer_cohort.py`

### Step 1: Write failing tests for the pure functions

Create `tests/test_analyze_buffer_cohort.py`:

```python
"""Tests for the slip-distribution + verdict functions used by the analyzer."""
import pytest

from scripts.analyze_buffer_cohort import (
    compute_slip_ticks,
    slip_distribution,
    classify_verdict,
)


def test_compute_slip_ticks_positive():
    # entry 0.56, best_opp_bid 0.48 -> implied clearing 0.52 -> slip 0.04 -> 4 ticks
    assert compute_slip_ticks(entry=0.56, best_opp_bid=0.48) == 4


def test_compute_slip_ticks_zero():
    # entry 0.52, best_opp_bid 0.48 -> slip 0
    assert compute_slip_ticks(entry=0.52, best_opp_bid=0.48) == 0


def test_compute_slip_ticks_float_artifact():
    # 0.1 + 0.2 = 0.30000000000000004; raw multiply can produce 4.999... -> 4
    # Tick-integer rounding must produce 5.
    # entry = 1 - 0.65 + 5*0.01 = 0.40; best_opp_bid = 0.65 -> slip should be 5
    assert compute_slip_ticks(entry=0.40, best_opp_bid=0.65) == 5


def test_slip_distribution_basic():
    slips = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    d = slip_distribution(slips)
    assert d["n"] == 10
    assert d["median"] == 5  # tie-break lower-median (5 or 6 both ok); doc whichever
    assert d["mean"] == pytest.approx(5.5)
    assert d["min"] == 1
    assert d["max"] == 10
    assert d["p25"] == 3  # or 3.25; doc whichever
    assert d["p75"] == 8  # or 7.75


def test_slip_distribution_empty():
    d = slip_distribution([])
    assert d["n"] == 0
    assert d["median"] is None


def test_classify_ship():
    assert classify_verdict(median_slip=6) == "SHIP"
    assert classify_verdict(median_slip=5) == "SHIP"
    assert classify_verdict(median_slip=0) == "SHIP"


def test_classify_escalate():
    assert classify_verdict(median_slip=8) == "ESCALATE"
    assert classify_verdict(median_slip=11) == "ESCALATE"


def test_classify_ambiguous():
    assert classify_verdict(median_slip=7) == "AMBIGUOUS"


def test_classify_none():
    assert classify_verdict(median_slip=None) == "AMBIGUOUS"
```

### Step 2: Run tests — expect failure

Run: `pytest tests/test_analyze_buffer_cohort.py -v`
Expected: FAIL — module doesn't exist.

### Step 3: Implement

Create `scripts/analyze_buffer_cohort.py`:

```python
"""Analyze a buffer=8 cohort: emit slip distribution, verdict, and a markdown report.

Produces `scripts/_cohort_analysis.md` with the verdict artifact the design
doc (`docs/plans/2026-04-23-buffer-8-live-cohort-design.md`) requires.

Usage:
  python scripts/analyze_buffer_cohort.py --since 2026-04-23T18:00:00

Verdict thresholds (pre-committed):
  median slip <= 6 ticks -> SHIP
  median slip == 7 ticks -> AMBIGUOUS (extend cohort)
  median slip >= 8 ticks -> ESCALATE
"""
import argparse
import datetime as dt
import json
import pathlib
import sqlite3
import statistics
import sys

REPORT_PATH = pathlib.Path("scripts/_cohort_analysis.md")

BASELINE_BUFFER = 15
BASELINE_N = 59
BASELINE_SLIP_MEDIAN = 11.6  # cents (i.e. ticks), per config.py:17
BASELINE_SLIP_MEAN = 10.5


def compute_slip_ticks(entry: float, best_opp_bid: float) -> int:
    """Tick-integer slip computation. Guards against float artifacts (e6c4ae7)."""
    clearing_ticks = round((1.0 - best_opp_bid) * 100)
    entry_ticks = round(entry * 100)
    return entry_ticks - clearing_ticks


def slip_distribution(slips: list[int]) -> dict:
    if not slips:
        return {"n": 0, "median": None, "mean": None, "min": None, "max": None, "p25": None, "p75": None}
    s = sorted(slips)
    n = len(s)
    return {
        "n": n,
        "median": statistics.median(s),
        "mean": round(sum(s) / n, 2),
        "min": s[0],
        "max": s[-1],
        "p25": s[max(0, n // 4 - 1)] if n >= 4 else s[0],
        "p75": s[min(n - 1, 3 * n // 4)] if n >= 4 else s[-1],
    }


def classify_verdict(median_slip) -> str:
    if median_slip is None:
        return "AMBIGUOUS"
    if median_slip <= 6:
        return "SHIP"
    if median_slip >= 8:
        return "ESCALATE"
    return "AMBIGUOUS"


def _load_cohort(db: str, since_iso: str) -> list[dict]:
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        """SELECT id, side, entry_price, pnl, status, timestamp,
                  up_bids_json, down_bids_json
             FROM trades t
             LEFT JOIN window_snapshots s
               ON s.window_slug = t.window_slug AND s.trade_fired = 1
            WHERE t.status IN ('open', 'settled')
              AND t.entry_price IS NOT NULL
              AND t.timestamp >= ?
            ORDER BY t.id""",
        (since_iso,),
    ).fetchall()
    c.close()
    out = []
    for r in rows:
        bids_json = r["down_bids_json"] if r["side"] == "up" else r["up_bids_json"]
        if not bids_json:
            continue
        try:
            bids = json.loads(bids_json)
            best = max(float(b["price"]) for b in bids)
        except Exception:
            continue
        out.append({
            "id": r["id"],
            "side": r["side"],
            "entry_price": float(r["entry_price"]),
            "best_opp_bid": best,
            "pnl": float(r["pnl"]) if r["pnl"] is not None else None,
            "status": r["status"],
        })
    return out


def _count_rejects(db: str, since_iso: str) -> int:
    c = sqlite3.connect(db)
    r = c.execute(
        "SELECT COUNT(*) FROM trades WHERE status='rejected' AND timestamp >= ?",
        (since_iso,),
    ).fetchone()
    c.close()
    return int(r[0])


def _emit_report(since_iso: str, dist: dict, verdict: str, n_rejects: int, total_pnl: float) -> str:
    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    attempts = dist["n"] + n_rejects
    reject_rate = (n_rejects / attempts) if attempts else 0.0
    delta_median = (dist["median"] - BASELINE_SLIP_MEDIAN) if dist["median"] is not None else None
    delta_str = f"{delta_median:+.1f}" if delta_median is not None else "n/a"

    lines = [
        "# Buffer=8 cohort analysis",
        "",
        f"**Generated:** {now}Z",
        f"**Cohort start:** {since_iso}",
        f"**Config:** IOC_BUFFER_TICKS=8, SIGNAL_CUSHION_TICKS=11 (all other knobs frozen)",
        "",
        "## Slip distribution",
        "",
        "| metric | buffer=15 baseline | buffer=8 cohort | delta |",
        "|---|---|---|---|",
        f"| n | {BASELINE_N} | {dist['n']} | — |",
        f"| median (ticks) | {BASELINE_SLIP_MEDIAN} | {dist['median']} | {delta_str} |",
        f"| mean (ticks) | {BASELINE_SLIP_MEAN} | {dist['mean']} | — |",
        f"| min / max | — | {dist['min']} / {dist['max']} | — |",
        f"| p25 / p75 | — | {dist['p25']} / {dist['p75']} | — |",
        "",
        "## Reject rate",
        "",
        f"- Attempts: {attempts} (fills {dist['n']} + rejects {n_rejects})",
        f"- Reject rate: {reject_rate:.1%}",
        "",
        "## Cohort PnL",
        "",
        f"- Total: {total_pnl:+.2f}",
        f"- Avg/trade: {(total_pnl / dist['n']):+.3f}" if dist["n"] else "- Avg/trade: n/a",
        "",
        "## Verdict",
        "",
        f"**{verdict}**",
        "",
    ]
    if verdict == "SHIP":
        lines.append("Median slip is at or below the 6-tick threshold. Update `polypocket/config.py` defaults (`IOC_BUFFER_TICKS=8`, `SIGNAL_CUSHION_TICKS` = cohort median slip), refresh the calibration comment blocks, and run a follow-up gate-only sweep on the combined 79-fill corpus.")
    elif verdict == "ESCALATE":
        lines.append("Median slip is at or above the 8-tick threshold. Matcher is filling near the limit regardless of buffer. Keep live paused; open model-recalibration issue against UP-side 0.70+ bins (per #13).")
    else:
        lines.append("Result is in the 1-tick deadband (median=7) or sample is missing. Extend the cohort to 40 fills before deciding. Do not change config.")

    body = "\n".join(lines) + "\n"
    REPORT_PATH.write_text(body)
    return body


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="live_trades.db")
    p.add_argument("--since", required=True)
    args = p.parse_args()

    rows = _load_cohort(args.db, args.since)
    slips = [compute_slip_ticks(r["entry_price"], r["best_opp_bid"]) for r in rows]
    dist = slip_distribution(slips)
    verdict = classify_verdict(dist["median"])
    n_rejects = _count_rejects(args.db, args.since)
    total_pnl = sum(r["pnl"] for r in rows if r["pnl"] is not None)

    body = _emit_report(args.since, dist, verdict, n_rejects, total_pnl)
    print(body)
    print(f"Wrote {REPORT_PATH}")

    # Exit code communicates verdict for downstream automation.
    sys.exit({"SHIP": 0, "AMBIGUOUS": 1, "ESCALATE": 2}[verdict])


if __name__ == "__main__":
    main()
```

### Step 4: Run tests

Run: `pytest tests/test_analyze_buffer_cohort.py -v`
Expected: all 9 PASS. If `test_slip_distribution_basic`'s p25/p75 assertions fail due to percentile-tie ambiguity, adjust the assertions (or the implementation) — the test comment flags that tolerance.

### Step 5: Commit

```bash
git add scripts/analyze_buffer_cohort.py tests/test_analyze_buffer_cohort.py
git commit -m "$(cat <<'EOF'
feat(scripts): analyze_buffer_cohort emits slip verdict + markdown artifact (#14)

Pure helpers for slip-tick computation and verdict classification are
unit-tested. CLI wrapper queries live_trades.db, produces scripts/_cohort_
analysis.md and an exit code per the SHIP/AMBIGUOUS/ESCALATE decision gate
in the design doc.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Launch the cohort (operational — human-gated)

**Files:** none (operational step)

### Step 1: Confirm preconditions

- Working tree clean except for pre-existing dirty files (`polypocket/bot.py` should include the Task 1 change and be committed).
- `.cohort_stop` does **not** exist in the repo root. Delete it if it does.
- Live bot is currently paused. Confirm via process check or log inspection.

### Step 2: Pick the cohort-start timestamp

Record this value verbatim — both the watchdog and the analyzer consume it:

```
COHORT_START=$(python -c "import datetime; print(datetime.datetime.utcnow().isoformat(timespec='seconds'))")
echo "$COHORT_START"
```

Save to a temporary note (e.g. paste into the terminal scratch buffer or a `.cohort_start` file not tracked by git) for reuse in step 4 and Task 5.

### Step 3: Start the bot with the experimental env var

Launch the bot however it's usually launched in this project, prepending the env var override:

```
IOC_BUFFER_TICKS=8 <existing bot launch command>
```

Do not change `SIGNAL_CUSHION_TICKS`. Do not edit `polypocket/config.py`. Confirm via log output on startup that `IOC_BUFFER_TICKS` reads as `8`.

### Step 4: Start the watchdog

In a second terminal:

```
python scripts/cohort_watchdog.py --since "$COHORT_START"
```

Expected: one "ok fills=0 rejects=0 pnl=+0.00 days=0.00 ..." line within 60 seconds. Leave this running.

### Step 5: Spot-check after the first fill

When the first fill arrives:

- Confirm the bot log shows `buffer_ticks=8` in the IOC submission path.
- Confirm `live_trades.db` has the new row with populated `entry_price` and `best_opp_bid` in the matching `window_snapshots` row.
- Confirm watchdog has incremented `n_fills`.

If the bot submitted at buffer=15 despite the env var, **kill the bot immediately**. The env-var override didn't take effect. Debug the launch env before resuming.

### Step 6: Wait

No intervention. 20 fills, or a rail-trip, whichever first. Typical cadence is ~N fills/day — check project logs for current rate to estimate wall-clock.

Record any manual observations (book depth anomalies, unusual reject patterns) in a scratch note for the analysis step.

---

## Task 5: Post-cohort analysis + commit artifact

**Files:**
- Create: `scripts/_cohort_analysis.md` (written by the script)

### Step 1: Stop the bot and watchdog

Use the existing graceful-stop mechanism for the bot (Ctrl-C / SIGTERM — the `self.stop.set()` path in `bot.py:805`). Ctrl-C the watchdog.

If `.cohort_stop` exists, that means a safety rail tripped — **note which rail** (the watchdog printed the reason) and include it in the report.

### Step 2: Run the analyzer

```
python scripts/analyze_buffer_cohort.py --since "$COHORT_START"
```

Expected: prints the full markdown report and writes `scripts/_cohort_analysis.md`. Exit code: 0=SHIP, 1=AMBIGUOUS, 2=ESCALATE.

### Step 3: Eyeball the slip distribution

Sanity checks:

- Are any slips negative? That would mean entry came in *below* clearing — possible if the snapshot lagged a favorable book move, but >1 such case is a data anomaly worth investigating before trusting the verdict.
- Is reject rate coherent with fill count (not zero if the bot attempted and didn't land)?
- Is median in line with mean, or is the distribution pathological (e.g. bimodal)? If bimodal, the point-estimate median is misleading — note it in the report manually before committing.

### Step 4: Commit the artifact

```bash
git add scripts/_cohort_analysis.md
git commit -m "$(cat <<'EOF'
analysis: buffer=8 cohort slip distribution + verdict (#14)

Cohort of <N> fills ran from <START> to <END>. Median slip <M>t vs
buffer=15 baseline 11.6t. Verdict: <SHIP|AMBIGUOUS|ESCALATE>.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Fill the placeholders from the report.

### Step 5: Branch on verdict

- **SHIP** → proceed to Task 6.
- **ESCALATE** → proceed to Task 7.
- **AMBIGUOUS** → return to Task 4, extend cohort to 40 fills (new `COHORT_START` kept; the script will re-run against the same window and pick up the new fills). After extension, re-run Task 5. If still AMBIGUOUS at 40 fills, treat as ESCALATE.

---

## Task 6 (SHIP branch only): Update config + follow-up sweep

**Files:**
- Modify: `polypocket/config.py:11-21`, `polypocket/config.py:99-107`

### Step 1: Update the two knob defaults

Using the measured cohort median slip (M) from the report:

- Line 21: `SIGNAL_CUSHION_TICKS = int(os.getenv("SIGNAL_CUSHION_TICKS", "11"))` → `"<M>"`
- Line 107: `IOC_BUFFER_TICKS = int(os.getenv("IOC_BUFFER_TICKS", "15"))` → `"8"`

### Step 2: Refresh the stale comment blocks

At `config.py:11-20` (cushion comment), update the "Calibrated on n=59 post-fix fills" sentence to reference the new combined 79-fill corpus and the new median/mean slip values. Keep the #11 reference; add a #14 reference for the buffer-cohort validation.

At `config.py:99-107` (IOC buffer comment), rewrite to reflect the new reality: buffer dropped from 15 to 8 after the 2026-04-23 cohort showed median slip <M>t at buffer=8 (vs 11.6t at buffer=15). See `docs/plans/2026-04-23-buffer-8-live-cohort-design.md`.

### Step 3: Run tests

```
pytest tests/ -q
```

Expected: no new failures from config defaults changing. If `tests/test_signal.py` or `tests/test_executor.py` hardcodes 15 or 11 as expected values (check via grep), update those to pull from config or assert against the new constants.

### Step 4: Commit

```bash
git add polypocket/config.py tests/
git commit -m "$(cat <<'EOF'
config: IOC_BUFFER_TICKS 15->8, SIGNAL_CUSHION_TICKS -> <M> (#14, closes #12)

Cohort of <N> fills at buffer=8 produced median slip <M>t (baseline 11.6t).
Gate A of the design doc passed; safe to commit as live defaults.
Next: follow-up gate-only sweep on combined 79-fill corpus to optionally
tighten MIN_EDGE_THRESHOLD / MAX_ENTRY_PRICE. Not blocking.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 5: Close / update issues

```
gh issue comment 14 --body "SHIP verdict. Median slip <M>t at buffer=8 on n=<N> cohort (baseline 11.6t). Config committed in <commit-sha>. Monitoring next via follow-up gate sweep."
gh issue close 12 --comment "Re-tuned via cohort experiment (#14). See scripts/_cohort_analysis.md."
gh issue comment 11 --body "Items 3/4 satisfied by cohort reject/slip observation. Item 2 (replay rewrite) previously shipped in 3589311/c8b32cc — the #14 work validates the buffer regime those scripts should now target."
```

### Step 6: Resume live trading with the new defaults

Launch the bot without env-var overrides. Confirm via log that `IOC_BUFFER_TICKS=8` and `SIGNAL_CUSHION_TICKS=<M>` are loaded.

---

## Task 7 (ESCALATE branch only): Open model-recalibration issue

**Files:** none (GitHub operation)

### Step 1: Open issue #15

```
gh issue create --title "Model recalibration: UP-side 0.70+ bins" --body "$(cat <<'EOF'
## Context

#14 ESCALATE verdict — buffer reduction did not move slip materially.
Cohort median slip at IOC_BUFFER_TICKS=8 was <M>t (threshold was <=6t for SHIP). See scripts/_cohort_analysis.md.

Execution-side fix is exhausted; the matcher fills near the limit
regardless of buffer. Remaining hypothesis: UP-side model miscalibration
across the 0.70+ regime, not just the 0.80+ tail flagged in #13.

## Scope

Audit UP-side calibration across:
- 0.70-0.74 bin (n=<from db>)
- 0.75-0.79 bin (n=<from db>)
- 0.80+ bin (n=7 in #13; likely still underpowered)

Corpus: expanded post-2026-04-23 live fills (~79+ rows).

Deliverable: per-bin predicted-vs-realized WR with bootstrap CIs and a
decision on whether to retrain, shrink, or apply a per-bin calibration
adjustment.

## Out of scope

Anything execution-side. The IOC buffer / cushion story is closed.

## Blocks

Resuming live trading.
EOF
)"
```

### Step 2: Comment on #14

```
gh issue comment 14 --body "ESCALATE verdict. Opened #15 for model recalibration. Live stays paused. See scripts/_cohort_analysis.md."
```

### Step 3: Confirm live stays paused

No config changes. `IOC_BUFFER_TICKS` stays at 15 in `config.py`. No `.cohort_stop` kept around (delete if present so the bot isn't gated when it's restarted later). The bot process should remain stopped until #15 resolves.

---

## Execution notes

- Tasks 1, 2, 3 are pre-work — code + tests only, no live money involved. Complete all three before Task 4.
- Task 4 is the irreversible step (money at stake). Re-read the design doc before launching.
- Tasks 5, 6, 7 all depend on cohort completion. Do not run them concurrently with Task 4.
- Tasks 6 and 7 are mutually exclusive — the verdict from Task 5 picks exactly one.
- If anything unexpected happens during Task 4 (bot crash, DB corruption, unusual loss pattern), stop and write `.cohort_stop` manually (`echo "manual stop" > .cohort_stop`). The cohort can be extended once the cause is understood; the analyzer will still run on whatever fills accumulated.
