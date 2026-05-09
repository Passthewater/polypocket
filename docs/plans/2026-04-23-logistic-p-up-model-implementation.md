# Logistic p_up model Implementation Plan

> **For Claude:** Execute in-chat, one task at a time, linearly. No parallel subagents. Each task has file paths, concrete commands, and a verification + rollback line.
>
> **Updated 2026-04-25** after #16 / G1–G5 data-capture shipped. The paper post-G1 corpus replaces the original combined paper+live corpus; the dual-split (paper-only / live-only) gating regime is collapsed to a single chronological 60/20/20 on paper post-G1. See the design doc's update markers for context. Tasks 3, 5, 6, 7, 8 below have been revised; mechanical bugs fixed.

**Goal:** Replace Brownian `compute_model_p_up` with a data-trained logistic regression + isotonic calibration. Ship behind `MODEL_VERSION` env var, paper A/B, then promote.

**Architecture:** Exporter script writes a parquet corpus from `window_snapshots` (paper post-G1 only). A Jupyter-style notebook in `notebooks/` fits and evaluates. Coefficients persist to `polypocket/model_v2_coefs.json` (committed). `observer.py` gains `compute_model_p_up_v2` + a dispatcher. `signal.py` integrates **via the dispatcher** (single switch point). Dual-logging adds two nullable columns to `window_snapshots`: `model_p_up_v1_calibrated` and `model_p_up_v2` (both populated on every decision regardless of which version fires).

**Tech Stack:** Python 3.10+ (dataclass `str | None` annotations require it), `pandas`, `scikit-learn`, `pyarrow` (parquet), `sqlite3`, `pytest`. `scikit-learn` and `pyarrow` are new dev-only deps; not required for the live bot path.

**Design doc:** `docs/plans/2026-04-23-logistic-p-up-model-design.md`.

**Related issues:** #15 (this work), #13 (motivation — break-even-minus-fees finding), #16 (data capture, shipped — supplies the post-G1 corpus), #11 (slip cushion — not blocking).

---

## Pre-decision (user, before Task 1)

Two questions are now resolved by the design's 2026-04-25 update (see the design doc): paper-only post-G1 is the training corpus; book-depth features remain deferred to v0.1. One question still warrants pre-commitment.

**Q3 from #15 — blend vs. replace?** The design commits to testing a blended variant in the notebook and shipping the non-blended version unless blending clearly wins on held-out reliability. If the user wants to pre-commit to "replace only, no blend test" or "must blend if feasible," say so before Task 3 begins. Otherwise the default is: test ablation, ship whichever wins on held-out, document the choice in `scripts/_model_v2_training.md`.

**Timing pre-decision (new for 2026-04-25).** The post-G1 paper corpus is accumulating at ~250–300 decisions/day. The plan is written for execution at one of two horizons:

- **7-day soak (~2,000 rows).** Minimum-viable retrain. Held-out is ~400 rows; 0.80+ bin populates with 30–80 rows depending on v2's calibration. Acceptance gate's per-bin n thresholds (≥30 mid-bins, ≥20 tail) are tight but achievable.
- **14-day soak (~4,000 rows).** Preferred retrain. Held-out is ~800 rows with comfortable per-bin support; ablation results have tighter CIs.

Default is **14-day soak**. Tasks 1–2 (deps + exporter) are pure functions and can land *now*; Task 3 (the fit) waits for the soak. Confirm before starting Task 3.

---

## Task 1: Add dev dependencies

**Files:**
- Modify: `pyproject.toml` (or `requirements-dev.txt` / equivalent — whichever the repo uses)

### Step 1: Confirm the project's dev-dep location

Run: `ls pyproject.toml requirements-dev.txt setup.cfg 2>/dev/null`. Use whichever exists. If only `pyproject.toml` exists, add a `[project.optional-dependencies]` section called `ml` (or extend an existing dev group).

### Step 2: Add deps

Add `scikit-learn>=1.3`, `pandas>=2.0`, `pyarrow>=14`. Pin to majors only.

### Step 3: Install locally

Run: `pip install -e '.[ml]'` (or the equivalent for the repo's layout). Expected: fresh install of sklearn/pandas/pyarrow with no conflicts against existing pinned deps. If conflict appears, stop and escalate — don't downgrade an existing live dep for a training tool.

### Step 4: Commit

```bash
git add pyproject.toml  # or requirements-dev.txt
git commit -m "chore: add sklearn/pandas/pyarrow as ml-only dev deps (#15)"
```

**Rollback:** `git revert HEAD`. No runtime impact.

---

## Task 2: Corpus exporter script

**Files:**
- Create: `scripts/export_training_corpus.py`
- Create: `tests/test_export_training_corpus.py`

### Step 1: Write failing tests for the join logic

Create `tests/test_export_training_corpus.py`:

```python
"""Tests for the decision→close join in the training corpus exporter.

These are pure-function tests: the exporter's public helpers should take an
in-memory sqlite3.Connection (:memory:) and return a list of dicts. The
CLI/parquet path is not tested here — it's smoke-tested in Step 5.
"""
import sqlite3
from scripts.export_training_corpus import join_decision_close, Row


def _make_db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute(
        """CREATE TABLE window_snapshots (
            id INTEGER PRIMARY KEY, window_slug TEXT, snapshot_type TEXT,
            timestamp TEXT, btc_price REAL, window_open_price REAL,
            displacement REAL, sigma_5min REAL, t_remaining REAL,
            model_p_up REAL, market_p_up REAL, up_ask REAL, down_ask REAL,
            up_bids_json TEXT, down_bids_json TEXT, trade_fired INTEGER,
            outcome TEXT, final_price REAL
        )"""
    )
    return c


def test_join_returns_labeled_decisions(tmp_path):
    c = _make_db()
    c.execute(
        """INSERT INTO window_snapshots (window_slug, snapshot_type, timestamp,
            displacement, sigma_5min, t_remaining, market_p_up, up_ask, down_ask,
            trade_fired) VALUES ('w1', 'decision', '2026-04-25 00:00:00',
            0.001, 0.0008, 200, 0.55, 0.58, 0.42, 1)"""
    )
    c.execute(
        """INSERT INTO window_snapshots (window_slug, snapshot_type, timestamp,
            outcome, final_price, trade_fired) VALUES
            ('w1', 'close', '2026-04-25 00:05:00', 'up', 65001.0, 1)"""
    )
    rows = join_decision_close(c, source="paper")
    assert len(rows) == 1
    assert rows[0].outcome == "up"
    assert rows[0].source == "paper"
    assert rows[0].displacement == 0.001


def test_join_respects_since_timestamp_cutoff():
    c = _make_db()
    # Pre-cutoff row — should be excluded.
    c.execute(
        """INSERT INTO window_snapshots (window_slug, snapshot_type, timestamp,
            displacement, sigma_5min, t_remaining, market_p_up, up_ask, down_ask,
            trade_fired) VALUES ('pre', 'decision', '2026-04-20 00:00:00',
            0.001, 0.0008, 200, 0.55, 0.58, 0.42, 1)"""
    )
    c.execute(
        """INSERT INTO window_snapshots (window_slug, snapshot_type, timestamp,
            outcome, final_price, trade_fired) VALUES
            ('pre', 'close', '2026-04-20 00:05:00', 'up', 65001.0, 1)"""
    )
    # Post-cutoff row — should be included.
    c.execute(
        """INSERT INTO window_snapshots (window_slug, snapshot_type, timestamp,
            displacement, sigma_5min, t_remaining, market_p_up, up_ask, down_ask,
            trade_fired) VALUES ('post', 'decision', '2026-04-25 00:00:00',
            0.001, 0.0008, 200, 0.55, 0.58, 0.42, 0)"""
    )
    c.execute(
        """INSERT INTO window_snapshots (window_slug, snapshot_type, timestamp,
            outcome, final_price, trade_fired) VALUES
            ('post', 'close', '2026-04-25 00:05:00', 'down', 64999.0, 0)"""
    )
    rows = join_decision_close(c, source="paper", since_timestamp="2026-04-24T00:00:00")
    assert [r.window_slug for r in rows] == ["post"]


def test_join_drops_unlabeled_decision():
    c = _make_db()
    c.execute(
        """INSERT INTO window_snapshots (window_slug, snapshot_type, timestamp,
            displacement, sigma_5min, t_remaining, market_p_up, up_ask, down_ask,
            trade_fired) VALUES ('w2', 'decision', '2026-04-20 00:00:00',
            0, 0.0008, 200, 0.50, 0.52, 0.48, 0)"""
    )
    # no close row for w2
    rows = join_decision_close(c, source="paper")
    assert rows == []


def test_join_drops_decision_with_missing_core_feature():
    c = _make_db()
    c.execute(
        """INSERT INTO window_snapshots (window_slug, snapshot_type, timestamp,
            displacement, sigma_5min, t_remaining, market_p_up, up_ask, down_ask,
            trade_fired) VALUES ('w3', 'decision', '2026-04-20 00:00:00',
            0.001, NULL, 200, 0.55, 0.58, 0.42, 1)"""
    )
    c.execute(
        """INSERT INTO window_snapshots (window_slug, snapshot_type, timestamp,
            outcome, trade_fired) VALUES
            ('w3', 'close', '2026-04-20 00:05:00', 'up', 1)"""
    )
    rows = join_decision_close(c, source="paper")
    assert rows == []


def test_join_drops_t_remaining_leq_zero():
    c = _make_db()
    c.execute(
        """INSERT INTO window_snapshots (window_slug, snapshot_type, timestamp,
            displacement, sigma_5min, t_remaining, market_p_up, up_ask, down_ask,
            trade_fired) VALUES ('w4', 'decision', '2026-04-20 00:00:00',
            0.001, 0.0008, 0, 0.55, 0.58, 0.42, 1)"""
    )
    c.execute(
        """INSERT INTO window_snapshots (window_slug, snapshot_type, timestamp,
            outcome, trade_fired) VALUES
            ('w4', 'close', '2026-04-20 00:05:00', 'up', 1)"""
    )
    rows = join_decision_close(c, source="paper")
    assert rows == []
```

### Step 2: Run tests — expect ModuleNotFoundError

Run: `pytest tests/test_export_training_corpus.py -v`
Expected: all FAIL on `ModuleNotFoundError: scripts.export_training_corpus`.

### Step 3: Implement the exporter

Create `scripts/export_training_corpus.py`:

```python
"""Export a labeled training corpus from paper + live ledgers to parquet.

Joins window_snapshots rows of snapshot_type='decision' to their corresponding
snapshot_type='close' row on window_slug, filters to rows with all core
features present and a non-null outcome, and writes one row per window.

Usage:
  python scripts/export_training_corpus.py \
    --paper-db paper_trades.db \
    --live-db live_trades.db \
    --out corpus.parquet
"""
import argparse
import json
import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path

import pandas as pd


CORE_FIELDS = (
    "displacement",
    "sigma_5min",
    "t_remaining",
    # NOTE: the ledger's `market_p_up` column persists the raw up_ask, not a
    # normalized probability (verified 2026-04-23). We compute the normalized
    # value from up_ask/down_ask in both training and inference — see the
    # `market_p_up_normalized` derivation below. That means market_p_up from
    # the ledger is NOT required here; only up_ask / down_ask are.
    "up_ask",
    "down_ask",
)


@dataclass(frozen=True)
class Row:
    window_slug: str
    source: str  # "paper" | "live"
    decision_timestamp: str
    displacement: float
    sigma_5min: float
    t_remaining: float
    # Normalized from up_ask / (up_ask + down_ask) — see module note above.
    market_p_up_normalized: float
    up_ask: float
    down_ask: float
    model_p_up_v1_raw: float | None  # ledger's model_p_up — shrinkage varied; DO NOT use as feature
    up_bids_json: str | None
    down_bids_json: str | None
    outcome: str  # "up" | "down"
    outcome_int: int  # 1 if up else 0
    final_price: float | None


def join_decision_close(
    conn: sqlite3.Connection,
    source: str,
    *,
    since_timestamp: str | None = None,
) -> list[Row]:
    """Return labeled decision rows from a single DB.

    `since_timestamp` (optional ISO-8601 string) filters to decisions on or
    after the cutoff — used to restrict to post-G1 paper rows. The G1 commit
    (5f76bea) changed close-row semantics from `trade_fired=1`-only to every
    window with a BTC-derived label; pre-G1 rows have a different label
    population and are excluded from v2 training.
    """
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """
        SELECT
            d.window_slug,
            d.timestamp AS decision_timestamp,
            d.displacement, d.sigma_5min, d.t_remaining,
            d.up_ask, d.down_ask,
            d.model_p_up AS model_p_up_v1,
            d.up_bids_json, d.down_bids_json,
            c.outcome, c.final_price
        FROM window_snapshots d
        JOIN window_snapshots c
          ON c.window_slug = d.window_slug AND c.snapshot_type = 'close'
        WHERE d.snapshot_type = 'decision'
          AND c.outcome IS NOT NULL
          AND (? IS NULL OR d.timestamp >= ?)
        """,
        (since_timestamp, since_timestamp),
    )
    out: list[Row] = []
    for r in cur.fetchall():
        # Core feature completeness. Any null here = drop row.
        if any(r[f] is None for f in CORE_FIELDS):
            continue
        # Brownian z requires t_remaining > 0; feature engineering relies on this.
        if r["t_remaining"] <= 0:
            continue
        if r["outcome"] not in ("up", "down"):
            continue
        up_ask = float(r["up_ask"])
        down_ask = float(r["down_ask"])
        denom = up_ask + down_ask
        if denom <= 0:  # nonsensical book, skip
            continue
        out.append(
            Row(
                window_slug=r["window_slug"],
                source=source,
                decision_timestamp=r["decision_timestamp"],
                displacement=float(r["displacement"]),
                sigma_5min=float(r["sigma_5min"]),
                t_remaining=float(r["t_remaining"]),
                market_p_up_normalized=up_ask / denom,
                up_ask=up_ask,
                down_ask=down_ask,
                model_p_up_v1_raw=(
                    float(r["model_p_up_v1"]) if r["model_p_up_v1"] is not None else None
                ),
                up_bids_json=r["up_bids_json"],
                down_bids_json=r["down_bids_json"],
                outcome=r["outcome"],
                outcome_int=1 if r["outcome"] == "up" else 0,
                final_price=(float(r["final_price"]) if r["final_price"] is not None else None),
            )
        )
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--paper-db", default="paper_trades.db")
    p.add_argument("--live-db", default=None,
                   help="Live DB to include. Default: skip — v2 trains on paper post-G1 only.")
    p.add_argument("--out", default="corpus.parquet")
    p.add_argument("--since", default=None,
                   help="ISO-8601 cutoff. Default: paper post-G1 (commit 5f76bea merge time). "
                        "Use '1970-01-01' to include all rows for diagnostic exports.")
    args = p.parse_args()

    # Default cutoff: G1 commit merge time. Adjust if the repo's history shifts.
    since = args.since or "2026-04-24T00:00:00"

    rows: list[Row] = []
    sources: list[tuple[str, str]] = [(args.paper_db, "paper")]
    if args.live_db is not None:
        sources.append((args.live_db, "live"))
    for path, source in sources:
        if not Path(path).exists():
            print(f"warning: {path} not found, skipping")
            continue
        conn = sqlite3.connect(path)
        try:
            rows.extend(join_decision_close(conn, source, since_timestamp=since))
        finally:
            conn.close()

    df = pd.DataFrame([asdict(r) for r in rows])
    df = df.sort_values("decision_timestamp").reset_index(drop=True)
    df.to_parquet(args.out, index=False)

    print(f"Exported {len(df)} rows to {args.out}")
    print(f"  paper: {(df['source']=='paper').sum()}")
    print(f"  live:  {(df['source']=='live').sum()}")
    print(f"  base rate (up): {df['outcome_int'].mean():.3f}")
    print(f"  rows with bids: {df['up_bids_json'].notna().sum()}")


if __name__ == "__main__":
    main()
```

### Step 4: Run tests — expect PASS

Run: `pytest tests/test_export_training_corpus.py -v`
Expected: all 4 PASS.

### Step 5: Smoke-run the exporter on the real ledgers

Run:

```bash
python scripts/export_training_corpus.py --out corpus.parquet
```

Expected stdout (paper-only post-G1, default cutoff):
```
Exported <N> rows to corpus.parquet
  paper: <N>
  live:  0
  base rate (up): ~0.50–0.58
  rows with bids: 0
```

`<N>` depends on the soak duration at the time you run this:

- ~1 day post-G1 → ~250–325 rows
- ~7 days post-G1 → ~1,800–2,100 rows
- ~14 days post-G1 → ~3,500–4,200 rows

Sanity-check: row count divided by hours-since-G1 should land around 10–14 rows/hour. If the rate is meaningfully off, the G1 close-row migration may not be capturing every window — re-verify against #16's Phase 1 success criteria before continuing.

For diagnostic comparison against the original 2026-04-23 measurement (N=404 on combined paper+live with `trade_fired=1` filter), pass `--since 1970-01-01 --live-db live_trades.db` and add a `WHERE trade_fired=1` clause manually — but **do not** ship a model fit on that corpus; v2's training corpus is paper post-G1 by design.

Add `corpus.parquet` to `.gitignore` (it's regenerable from the ledgers and will change each run):

```bash
grep -qxF 'corpus.parquet' .gitignore 2>/dev/null || echo 'corpus.parquet' >> .gitignore
```

### Step 6: Commit

```bash
git add scripts/export_training_corpus.py tests/test_export_training_corpus.py .gitignore
git commit -m "$(cat <<'EOF'
feat(scripts): export_training_corpus joins decision+close ledger rows to parquet (#15)

Defaults to paper post-G1 (decisions on/after the G1 commit, where every
window emits a close row with a BTC-derived label — see #16). Live ledger
opt-in via --live-db; pre-G1 paper opt-in via --since. Tests cover the
join filter, null-feature exclusion, and the timestamp cutoff. Parquet
output is gitignored; regenerate on demand.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Rollback:** `git revert HEAD`; delete `corpus.parquet`. No production impact.

---

## Task 3: Fit + evaluate notebook

**Files:**
- Create: `notebooks/2026-04-23-fit-logistic-p-up-v2.ipynb` (or `.py` if the repo doesn't use notebooks — check `ls notebooks/ 2>/dev/null` first; if the dir doesn't exist, use a plain script `scripts/fit_logistic_p_up_v2.py` and run it end-to-end).

This task produces the fitted model and its metrics. Treat the notebook as a script: each cell is ordered, deterministic, and seeded.

### Step 1: Load and build the chronological split

Read `corpus.parquet`. Build a **single** chronological 60/20/20 split on the paper post-G1 corpus (design doc §"Train / calibrate / eval split", updated 2026-04-25).

```python
import pandas as pd, numpy as np
from math import sqrt

df = pd.read_parquet("corpus.parquet").sort_values("decision_timestamp").reset_index(drop=True)
assert (df.source == "paper").all(), \
    "Expected paper-only corpus; rerun the exporter without --live-db."
N = len(df)

def make_split(d: pd.DataFrame):
    n = len(d)
    t_end = int(n * 0.60); c_end = int(n * 0.80)
    return d.iloc[:t_end].copy(), d.iloc[t_end:c_end].copy(), d.iloc[c_end:].copy()

train, cal, held = make_split(df)

print(f"N={N}: train={len(train)} cal={len(cal)} held={len(held)}")
print(f"  train dates: {train.decision_timestamp.min()} - {train.decision_timestamp.max()}")
print(f"  cal   dates: {cal.decision_timestamp.min()} - {cal.decision_timestamp.max()}")
print(f"  held  dates: {held.decision_timestamp.min()} - {held.decision_timestamp.max()}")
print(f"  base rate train: {train.outcome_int.mean():.3f}")
print(f"  base rate cal:   {cal.outcome_int.mean():.3f}")
print(f"  base rate held:  {held.outcome_int.mean():.3f}")
```

**Sanity:** at the 7-day soak horizon expect N ≈ 2,000 with held ≈ 400; at 14-day expect N ≈ 4,000 with held ≈ 800. If N is below the configured horizon's lower bound (Task 1 §"Timing pre-decision"), stop and wait — the per-bin reliability gate's small-n thresholds will not be satisfied.

### Step 2: Build features

Default feature set is 4 columns (design doc §"Feature set for v0"): `z`, `t_remaining`, `sigma_5min`, `market_p_up_normalized`. Engineered features (`spread`, `z_times_market`) are tested in the ablation (Step 8) and included only if they lift held-out log-loss.

```python
def make_features(d: pd.DataFrame, include_engineered: bool = False) -> pd.DataFrame:
    sigma_rem = d.sigma_5min * np.sqrt(d.t_remaining / 300.0)
    z = d.displacement / sigma_rem
    feats = {
        "z": z,
        "t_remaining": d.t_remaining,
        "sigma_5min": d.sigma_5min,
        "market_p_up_normalized": d.market_p_up_normalized,
    }
    if include_engineered:
        feats["spread"] = d.up_ask + d.down_ask - 1.0
        feats["z_times_market"] = z * d.market_p_up_normalized
    return pd.DataFrame(feats)

def prep(train_df, cal_df, held_df, include_engineered=False):
    return (
        make_features(train_df, include_engineered), train_df.outcome_int.values,
        make_features(cal_df, include_engineered), cal_df.outcome_int.values,
        make_features(held_df, include_engineered), held_df.outcome_int.values,
    )

X_train, y_train, X_cal, y_cal, X_held, y_held = prep(train, cal, held)
```

### Step 3: Time-series CV for L2 strength

Standardize features using the training slice's mean/std. Per-fold scaling is purer; at n≥1,200 train the difference is epsilon — document the choice in the report.

Sweep `C ∈ {0.01, 0.1, 1, 10, 100}` with a 5-fold `TimeSeriesSplit`. Pick C with lowest mean log-loss. Record mean + std per C in the training report.

```python
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import log_loss

scaler = StandardScaler().fit(X_train)
Xs_train = scaler.transform(X_train)
Xs_cal   = scaler.transform(X_cal)
Xs_held  = scaler.transform(X_held)

tscv = TimeSeriesSplit(n_splits=5)
for C in [0.01, 0.1, 1, 10, 100]:
    scores = []
    for tr_idx, val_idx in tscv.split(Xs_train):
        m = LogisticRegression(C=C, max_iter=500).fit(Xs_train[tr_idx], y_train[tr_idx])
        scores.append(log_loss(y_train[val_idx], m.predict_proba(Xs_train[val_idx])[:,1]))
    print(f"C={C}: mean={np.mean(scores):.4f} std={np.std(scores):.4f}")
```

### Step 4: Fit the shipping model + isotonic

```python
from sklearn.isotonic import IsotonicRegression

C_CHOSEN = ...  # from Step 3

logistic = LogisticRegression(C=C_CHOSEN, max_iter=500).fit(Xs_train, y_train)
raw_cal  = logistic.predict_proba(Xs_cal)[:, 1]
raw_held = logistic.predict_proba(Xs_held)[:, 1]

iso = IsotonicRegression(out_of_bounds="clip").fit(raw_cal, y_cal)
p2_held = iso.transform(raw_held)
```

### Step 5: Compute v1's calibrated comparator on the held-out

The reliability gate compares v2 against the v1 that's *live today*, not the historical `model_p_up` column on the parquet (which used whatever shrinkage was active on each row's day). Recompute v1's calibrated output from raw features using current shrinkage.

```python
from scipy.stats import norm

def v1_calibrated(d: pd.DataFrame) -> np.ndarray:
    sigma_rem = d.sigma_5min * np.sqrt(d.t_remaining / 300.0)
    raw = norm.cdf(d.displacement / sigma_rem).values
    # calibrate_p_up with current shrinkage (config.py — keep in sync)
    up_factor, down_factor = 1.00, 0.50
    factor = np.where(raw >= 0.5, up_factor, down_factor)
    return 0.5 + (raw - 0.5) * factor

p1_held = v1_calibrated(held)
```

### Step 6: Criterion 1 — reliability (gating)

Build a 4-bin reliability table for both v1 and v2 on the held-out: n, mean predicted, mean actual, absolute gap, 95% bootstrap CI on actual WR.

```python
def reliability_table(p_values, y, label, rng_seed=0):
    rng = np.random.default_rng(rng_seed)
    rows = []
    for lo, hi in [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 1.001)]:
        mask = (p_values >= lo) & (p_values < hi)
        n = int(mask.sum())
        if n == 0:
            rows.append({"bin": f"{lo:.2f}-{hi:.2f}", "n": 0, "pred": None, "actual": None,
                         "gap": None, "ci": None})
            continue
        pred = float(p_values[mask].mean())
        actual = float(y[mask].mean())
        boots = np.array([y[mask][rng.integers(0, n, n)].mean() for _ in range(2000)])
        ci_lo, ci_hi = np.percentile(boots, [2.5, 97.5])
        rows.append({
            "bin": f"{lo:.2f}-{hi:.2f}", "n": n, "pred": pred, "actual": actual,
            "gap": abs(pred - actual), "ci": (float(ci_lo), float(ci_hi)),
        })
    print(f"\n=== {label} ===")
    for r in rows:
        print(r)
    return rows

v2_held = reliability_table(p2_held, y_held, "v2 on paper post-G1 held-out")
v1_held = reliability_table(p1_held, y_held, "v1 on paper post-G1 held-out")
```

**Gating logic** (design doc §"Acceptance gate" — Criterion 1):

For each non-tail bin (0.50–0.80):
- `n ≥ 30`: `abs(v2.pred - v2.actual) ≤ 0.05` must hold.
- `n < 30` and `n > 0`: `v2.actual ∈ v2.ci`.

For the 0.80+ tail bin:
- `n ≥ 20`: `v2.gap ≤ 0.05` AND `v2.gap ≤ v1.gap`.
- `5 ≤ n < 20`: `v2.actual ∈ v2.ci` AND `v2.gap ≤ v1.gap`.
- `n < 5`: **fail.** A v2 with no tail support has no evidence it fixes #15.

```python
def apply_gate(v2_table, v1_table, label):
    failures = []
    for v2_row, v1_row in zip(v2_table, v1_table):
        bin_label = v2_row["bin"]
        is_tail = bin_label.startswith("0.80")
        n = v2_row["n"]
        if is_tail:
            if n < 5:
                failures.append(f"{label} 0.80+ tail: n={n} < 5 — v2 has no tail support")
                continue
            v1_gap = v1_row["gap"] if v1_row["n"] >= 5 else None
            if n >= 20:
                if v2_row["gap"] > 0.05:
                    failures.append(f"{label} 0.80+: gap {v2_row['gap']:.3f} > 0.05")
                if v1_gap is not None and v2_row["gap"] > v1_gap:
                    failures.append(f"{label} 0.80+: v2 gap {v2_row['gap']:.3f} > v1 gap {v1_gap:.3f}")
            else:  # 5 <= n < 20
                lo, hi = v2_row["ci"]
                if not (lo <= v2_row["pred"] <= hi):
                    failures.append(f"{label} 0.80+: pred {v2_row['pred']:.3f} outside CI [{lo:.3f},{hi:.3f}] (small-n)")
                if v1_gap is not None and v2_row["gap"] > v1_gap:
                    failures.append(f"{label} 0.80+: v2 gap {v2_row['gap']:.3f} > v1 gap {v1_gap:.3f} (small-n)")
        else:
            if n == 0:
                continue  # empty middle bins are not a failure (rare under this gate)
            if n >= 30:
                if v2_row["gap"] > 0.05:
                    failures.append(f"{label} {bin_label}: gap {v2_row['gap']:.3f} > 0.05")
            else:  # small-n CI check
                lo, hi = v2_row["ci"]
                if not (lo <= v2_row["pred"] <= hi):
                    failures.append(f"{label} {bin_label}: pred {v2_row['pred']:.3f} outside CI [{lo:.3f},{hi:.3f}] (small-n)")
    return (not failures, failures)

ship_ok, fails = apply_gate(v2_held, v1_held, "paper-post-G1")
print(f"\nship_ok={ship_ok}")
for f in fails:
    print(f"  - {f}")
```

If `ship_ok` is False, print the failures and stop — do not proceed to Step 9.

### Step 7: Criterion 2 — simulated held-out EV (confirmatory, veto-on-regression only)

This criterion is **reported** alongside reliability but does **not** gate the ship. Two reasons it stays confirmatory: (a) the EV simulation uses paper `entry_price` under the perfect-fill paper assumption (#16 §"Paper-mode realism upgrade" — the realism gap biases absolute magnitudes; only the *relative* v1-vs-v2 delta has signal); (b) PnL bootstrap CIs at held-out N often span zero. The veto: if v2's simulated PnL is *worse* than v1's by a statistically meaningful margin (95% bootstrap CI of delta entirely negative), do **not** ship.

Simulate the current live gate (`MIN_EDGE_THRESHOLD=0.10`, `MAX_ENTRY_PRICE=0.70`, `MIN_MODEL_CONFIDENCE_UP=0.75`, `MIN_MODEL_CONFIDENCE=0.60`, `MAX_EDGE_THRESHOLD_UP=0.25`) on each held-out row with v1 and v2 probabilities. Copy the gate structure from `signal.py::evaluate` into the notebook — do not import, to keep the notebook hermetic. Simulated PnL per fire uses the row's actual `final_price` outcome and actual paper `entry_price` (from the parquet).

Report:

- Total PnL v1 vs v2
- Number of fires per version
- Bootstrap 95% CI on `pnl_v2 - pnl_v1`
- Veto check: if CI entirely < 0 → RECORD VETO

If the veto trips, stop — investigate before proceeding.

### Step 8: Ablations (reported, not gating)

All ablations are scored on the held-out (same evaluation rubric as Step 6).

(a) **Engineered features in**: refit with `include_engineered=True` (adds `spread`, `z_times_market`). If held-out log-loss improves by ≥0.005 *and* the reliability gate still passes, include engineered features in the shipping model. Otherwise, ship the 4-feature default.

(b) **Logistic-only (no isotonic)**: compare held-out log-loss + reliability.

(c) **Blended with v1 as a feature**: add v1's calibrated output (under *current* shrinkage, recomputed — same helper as Step 5) as a 5th feature to the default set. Refit, re-isotonic. If it beats the primary by ≥0.005 log-loss on the held-out, escalate to the user before shipping — it may be worth including.

(d) **Per-fold scaler ablation**: refit with `StandardScaler` re-fit inside each TimeSeriesSplit fold rather than once on the full training slice. Compare CV log-loss. Informative for verifying the "epsilon at n≥1,200" claim in §"Train / calibrate / eval split".

None of (a)–(d) change the shipping gate; they populate the training report.

### Step 9: Persist the fitted model

Write `polypocket/model_v2_coefs.json` with everything needed to compute `compute_model_p_up_v2` at inference time:

```python
import json, subprocess

def _ts_to_iso(v) -> str:
    """Coerce pandas/numpy timestamps to ISO-8601 strings deterministically."""
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)

iso_thresholds = list(iso.X_thresholds_.tolist())
iso_values = list(iso.y_thresholds_.tolist())
git_sha = subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip()

payload = {
    "model_version": "v2",
    "trained_on_git_sha": git_sha,
    "trained_at": pd.Timestamp.utcnow().isoformat(),
    "corpus": {
        "source": "paper post-G1",
        "total_n": int(N),
        "train_n": int(len(train)),
        "cal_n": int(len(cal)),
        "held_n": int(len(held)),
        "held_dates": [_ts_to_iso(held.decision_timestamp.min()),
                       _ts_to_iso(held.decision_timestamp.max())],
        "base_rate_train": float(y_train.mean()),
        "base_rate_cal": float(y_cal.mean()),
        "base_rate_held": float(y_held.mean()),
    },
    "features": list(X_train.columns),
    "scaler_mean": scaler.mean_.tolist(),
    "scaler_scale": scaler.scale_.tolist(),
    "logistic_coef": logistic.coef_[0].tolist(),
    "logistic_intercept": float(logistic.intercept_[0]),
    "logistic_C": float(C_CHOSEN),
    "isotonic_x": iso_thresholds,
    "isotonic_y": iso_values,
    # Per-feature training-support hull. Used at inference time to warn on
    # feature vectors outside the range we trained on (see Task 4, Step 3).
    "feature_hull": {
        name: [float(X_train[name].min()), float(X_train[name].max())]
        for name in X_train.columns
    },
    "held_out_metrics": {
        "log_loss_v2": float(log_loss(y_held, p2_held)),
        "log_loss_v1_baseline": float(log_loss(y_held, p1_held)),
        "reliability_v2": v2_held,
        "reliability_v1": v1_held,
        "gate_pass": ship_ok,
        "gate_failures": fails,
    },
}
# default=str fallback only as belt-and-suspenders; explicit isoformat above
# is the canonical path for any pandas Timestamp.
with open("polypocket/model_v2_coefs.json", "w") as f:
    json.dump(payload, f, indent=2, default=str)
```

### Step 10: Write the training report

Generate `scripts/_model_v2_training.md` by hand (or via a `print()` that you copy-paste) with the full report per the design doc's Artifact section.

### Step 11: Commit the notebook + coefficients + report

```bash
git add notebooks/2026-04-23-fit-logistic-p-up-v2.ipynb polypocket/model_v2_coefs.json scripts/_model_v2_training.md
git commit -m "$(cat <<'EOF'
feat(model): fit logistic p_up v2 with isotonic calibration (#15)

Paper post-G1 corpus, 60/20/20 chronological. L2 logistic on the 4 core
features (z, t_remaining, sigma_5min, market_p_up_normalized); engineered
features included only if Step 8 ablation showed lift. Isotonic calibration
fit on the cal slice.

Held-out reliability + gate verdict: see scripts/_model_v2_training.md.
Coefficients and isotonic breakpoints persisted to
polypocket/model_v2_coefs.json for inference-time load.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Decision gate before proceeding to Task 4:** if the acceptance gate fails (either criterion), stop. Do not ship. Either collect more data (wait-and-refit), or escalate — more features may need book-depth which blocks on forward collection. Document the failure in the training report and ping the user.

**Rollback:** `git revert HEAD`. Coefficients file is the only committed artifact; removing it disables Task 4's inference path but breaks no existing code (observer.py's v2 path checks for the file).

---

## Task 4: Inference path — `compute_model_p_up_v2` + dispatcher

**Files:**
- Modify: `polypocket/observer.py`
- Create: `tests/test_observer_v2.py`

### Step 1: Write failing tests

Create `tests/test_observer_v2.py`:

```python
"""Tests for compute_model_p_up_v2 and the MODEL_VERSION dispatcher.

v2 loads polypocket/model_v2_coefs.json at import time. The tests construct
a small synthetic coefs file and monkeypatch the module's loader to use it,
keeping the tests independent of the real shipped coefficients.
"""
import json
from pathlib import Path

import pytest

from polypocket import observer


@pytest.fixture
def fake_coefs(tmp_path, monkeypatch):
    p = tmp_path / "fake_coefs.json"
    p.write_text(json.dumps({
        "model_version": "v2",
        "features": ["z", "t_remaining", "sigma_5min", "market_p_up_normalized"],
        "scaler_mean": [0, 200, 0.001, 0.5],
        "scaler_scale": [1, 60, 0.0005, 0.1],
        "logistic_coef": [1.0, 0, 0, 0],  # only z matters in fake
        "logistic_intercept": 0.0,
        "isotonic_x": [0.0, 0.5, 1.0],
        "isotonic_y": [0.0, 0.5, 1.0],  # identity isotonic in fake
        "feature_hull": {
            "z": [-5.0, 5.0],
            "t_remaining": [30.0, 300.0],
            "sigma_5min": [0.0001, 0.01],
            "market_p_up_normalized": [0.1, 0.9],
        },
    }, indent=2))
    monkeypatch.setenv("MODEL_V2_COEFS_PATH", str(p))
    # force-reload module-level state if cached
    observer._reset_v2_cache_for_tests()
    yield p


def test_v2_returns_probability_in_01(fake_coefs):
    p = observer.compute_model_p_up_v2(
        displacement=0.001, t_remaining=200, sigma_5min=0.0008,
        up_ask=0.58, down_ask=0.42,
    )
    assert 0.0 <= p <= 1.0


def test_v2_is_deterministic(fake_coefs):
    args = dict(displacement=0.001, t_remaining=200, sigma_5min=0.0008,
                up_ask=0.58, down_ask=0.42)
    assert observer.compute_model_p_up_v2(**args) == observer.compute_model_p_up_v2(**args)


def test_dispatcher_v1_default(monkeypatch):
    monkeypatch.delenv("MODEL_VERSION", raising=False)
    p = observer.compute_model_p_up_active(
        displacement=0.001, t_remaining=200, sigma_5min=0.0008,
        up_ask=0.58, down_ask=0.42,
    )
    # With MODEL_VERSION unset, dispatcher must return v1's RAW output
    # (caller applies calibrate_p_up separately per the v1 contract).
    expected = observer.compute_model_p_up(0.001, 200, 0.0008)
    assert p == expected


def test_dispatcher_v2_env(monkeypatch, fake_coefs):
    monkeypatch.setenv("MODEL_VERSION", "v2")
    p = observer.compute_model_p_up_active(
        displacement=0.001, t_remaining=200, sigma_5min=0.0008,
        up_ask=0.58, down_ask=0.42,
    )
    expected = observer.compute_model_p_up_v2(
        displacement=0.001, t_remaining=200, sigma_5min=0.0008,
        up_ask=0.58, down_ask=0.42,
    )
    assert p == expected


def test_v2_guards_t_remaining_leq_zero(fake_coefs):
    # Same guard as v1 — returns 0/0.5/1 without calling the logistic.
    assert observer.compute_model_p_up_v2(
        displacement=0.01, t_remaining=0, sigma_5min=0.0008,
        up_ask=0.58, down_ask=0.42,
    ) == 1.0
    assert observer.compute_model_p_up_v2(
        displacement=-0.01, t_remaining=0, sigma_5min=0.0008,
        up_ask=0.58, down_ask=0.42,
    ) == 0.0


def test_v2_warns_outside_training_hull(fake_coefs, caplog):
    # z = displacement / sigma_remaining with sigma_5min=0.0008, t=200 -> sigma_rem ≈ 0.000653
    # displacement = 0.01 -> z ≈ 15.3, well outside the fake hull [-5, 5]
    import logging
    with caplog.at_level(logging.WARNING, logger="polypocket.observer"):
        observer.compute_model_p_up_v2(
            displacement=0.01, t_remaining=200, sigma_5min=0.0008,
            up_ask=0.58, down_ask=0.42,
        )
    assert any("outside training hull" in r.message for r in caplog.records)
```

### Step 2: Run tests — expect fail

Run: `pytest tests/test_observer_v2.py -v`
Expected: FAIL — `compute_model_p_up_v2`, `compute_model_p_up_active`, `_reset_v2_cache_for_tests` don't exist yet.

### Step 3: Implement v2 + dispatcher in `observer.py`

Append to `polypocket/observer.py` (keep v1's `compute_model_p_up` and `calibrate_p_up` exactly as-is — they stay reachable). If `observer.py` doesn't already have a module logger, add it as part of this change.

```python
import json
import logging
import os
from math import exp, sqrt
from pathlib import Path
from bisect import bisect_right

log = logging.getLogger(__name__)  # only add if not already present in observer.py

_DEFAULT_COEFS_PATH = Path(__file__).parent / "model_v2_coefs.json"
_v2_coefs = None  # lazy-loaded
_HULL_WARNED: set[str] = set()  # module-level dedupe — warn once per feature per process


def _reset_v2_cache_for_tests() -> None:
    """Test-only hook: clears memoized v2 coefs and hull-warn dedupe set."""
    global _v2_coefs
    _v2_coefs = None
    _HULL_WARNED.clear()


def _load_v2_coefs() -> dict:
    global _v2_coefs
    if _v2_coefs is not None:
        return _v2_coefs
    path = Path(os.environ.get("MODEL_V2_COEFS_PATH", str(_DEFAULT_COEFS_PATH)))
    with open(path) as f:
        _v2_coefs = json.load(f)
    return _v2_coefs


def _isotonic_apply(x: float, xs: list[float], ys: list[float]) -> float:
    """Piecewise-linear interpolation with clipping — mirrors sklearn's out_of_bounds='clip'."""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect_right(xs, x) - 1
    # linear between (xs[i], ys[i]) and (xs[i+1], ys[i+1])
    x0, x1 = xs[i], xs[i + 1]
    y0, y1 = ys[i], ys[i + 1]
    if x1 == x0:
        return y0
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def compute_model_p_up_v2(
    *,
    displacement: float,
    t_remaining: float,
    sigma_5min: float,
    up_ask: float,
    down_ask: float,
) -> float:
    """v2: L2 logistic + isotonic calibration. Coefficients in model_v2_coefs.json.

    Note: market_p_up is NOT a parameter — it's derived from up_ask/down_ask
    inside this function to match the exporter's formula (see design doc).
    """
    # Same end-of-window guard as v1 to avoid divide-by-zero on sigma_remaining.
    if t_remaining <= 0:
        if displacement > 0:
            return 1.0
        if displacement < 0:
            return 0.0
        return 0.5

    sigma_remaining = sigma_5min * sqrt(t_remaining / 300.0)
    if sigma_remaining <= 0:
        if displacement > 0:
            return 1.0
        if displacement < 0:
            return 0.0
        return 0.5

    coefs = _load_v2_coefs()

    # market_p_up_normalized recomputed from asks — the ledger's market_p_up
    # column persists the raw up_ask, not a probability. Canonical formula
    # matches the exporter (scripts/export_training_corpus.py).
    denom = up_ask + down_ask
    market_p_up_normalized = (up_ask / denom) if denom > 0 else 0.5

    all_features = {
        "z": displacement / sigma_remaining,
        "t_remaining": t_remaining,
        "sigma_5min": sigma_5min,
        "market_p_up_normalized": market_p_up_normalized,
        # Engineered (only populated if present in coefs["features"])
        "spread": up_ask + down_ask - 1.0,
        "z_times_market": (displacement / sigma_remaining) * market_p_up_normalized,
    }

    # Feature-hull warning: any served feature outside the training-support
    # range triggers a one-shot warning per feature per process (deduped via
    # _HULL_WARNED). Does NOT clip — the prediction still runs with the real
    # value. Purpose is audit trail for drift / novel regime, not gating.
    hull = coefs.get("feature_hull")
    if hull is not None:
        for name in coefs["features"]:
            lo, hi = hull[name]
            v = all_features[name]
            if (v < lo or v > hi) and name not in _HULL_WARNED:
                _HULL_WARNED.add(name)
                log.warning(
                    "compute_model_p_up_v2: feature %s=%.4f outside training hull "
                    "[%.4f, %.4f] (further out-of-hull values for this feature suppressed)",
                    name, v, lo, hi,
                )

    # Standardize, then logistic.
    values = [all_features[name] for name in coefs["features"]]
    standardized = [
        (v - coefs["scaler_mean"][i]) / coefs["scaler_scale"][i]
        for i, v in enumerate(values)
    ]
    logit = coefs["logistic_intercept"] + sum(
        c * v for c, v in zip(coefs["logistic_coef"], standardized)
    )
    raw = 1.0 / (1.0 + exp(-logit))

    # Isotonic calibration.
    return _isotonic_apply(raw, coefs["isotonic_x"], coefs["isotonic_y"])


def compute_model_p_up_active(
    *,
    displacement: float,
    t_remaining: float,
    sigma_5min: float,
    up_ask: float,
    down_ask: float,
) -> float:
    """Dispatch to v1 (raw) or v2 based on MODEL_VERSION env var. Default v1.

    up_ask/down_ask are accepted unconditionally so the caller doesn't branch
    on model version; v1 ignores them, v2 needs them.
    """
    version = os.environ.get("MODEL_VERSION", "v1").strip().lower()
    if version == "v2":
        return compute_model_p_up_v2(
            displacement=displacement,
            t_remaining=t_remaining,
            sigma_5min=sigma_5min,
            up_ask=up_ask,
            down_ask=down_ask,
        )
    # v1 contract: return RAW norm.cdf output; caller applies calibrate_p_up.
    return compute_model_p_up(displacement, t_remaining, sigma_5min)
```

### Step 4: Run tests — expect PASS

Run: `pytest tests/test_observer_v2.py -v`
Expected: all 5 PASS. If `test_v2_guards_t_remaining_leq_zero` fails because the fake isotonic clamps to its own range, fix the guard in `compute_model_p_up_v2` — the guard must short-circuit *before* the logistic / isotonic chain.

Also run the full observer tests to confirm no regression:

```bash
pytest tests/test_bot.py tests/test_signal.py -q
```

Expected: no new failures (pre-existing failures noted in `git status` remain — ignore).

### Step 5: Commit

```bash
git add polypocket/observer.py tests/test_observer_v2.py
git commit -m "$(cat <<'EOF'
feat(observer): compute_model_p_up_v2 + MODEL_VERSION dispatcher (#15)

Logistic + isotonic inference path reads coefficients from
polypocket/model_v2_coefs.json. Dispatcher routes on MODEL_VERSION env var
(default v1 preserves current behavior). v1 code path untouched.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Rollback:** `git revert HEAD`. v1 path is untouched; nothing breaks.

---

## Task 5: Signal integration + dual-logging columns

**Files:**
- Modify: `polypocket/ledger.py` — add `model_p_up_v1_calibrated` and `model_p_up_v2` columns to `window_snapshots` (idempotent), and thread both through `log_snapshot`.
- Modify: `polypocket/signal.py` — route through `compute_model_p_up_active` (the dispatcher from Task 4), compute both versions unconditionally for logging.
- Modify: `polypocket/bot.py` (or wherever `log_snapshot` is called from — grep for it to locate the call site).
- Modify: `tests/test_signal.py` — assert the dispatcher is used and that both `model_p_up_v1_calibrated` and `model_p_up_v2` are computed on every evaluate call.

### Step 1: Add the columns (idempotent migration)

In `polypocket/ledger.py`, inside `init_db` where `snap_cols` is already being checked (see lines 109–114), add the same pattern for both columns:

```python
if "model_p_up_v1_calibrated" not in snap_cols:
    conn.execute("ALTER TABLE window_snapshots ADD COLUMN model_p_up_v1_calibrated REAL")
if "model_p_up_v2" not in snap_cols:
    conn.execute("ALTER TABLE window_snapshots ADD COLUMN model_p_up_v2 REAL")
```

Extend `log_snapshot`'s signature and the `INSERT OR REPLACE` statement to accept and persist both columns (pulled from `stats.get("model_p_up_v1_calibrated")` and `stats.get("model_p_up_v2")`).

**Why both, not just v2:** the existing `model_p_up` column gets overwritten with whichever version fires the trade. After the cutover (Task 8), `model_p_up == model_p_up_v2`, which makes the column useless as a v1 source for the comparison script. `model_p_up_v1_calibrated` is the stable, version-independent v1 column used by Task 7's A/B analysis.

### Step 2: Integrate in signal.py

At `polypocket/signal.py:77`, replace:

```python
model_p_up_raw = compute_model_p_up(displacement, t_remaining, sigma_5min)
model_p_up = calibrate_p_up(
    model_p_up_raw,
    up_factor=CALIBRATION_SHRINKAGE_UP,
    down_factor=CALIBRATION_SHRINKAGE_DOWN,
)
```

with:

```python
from polypocket.observer import (
    calibrate_p_up,
    compute_model_p_up,
    compute_model_p_up_active,
    compute_model_p_up_v2,
)

# Always compute both versions for dual-logging. The dispatcher decides
# which one drives the trade gate.
model_p_up_raw = compute_model_p_up(displacement, t_remaining, sigma_5min)
model_p_up_v1_calibrated = calibrate_p_up(
    model_p_up_raw,
    up_factor=CALIBRATION_SHRINKAGE_UP,
    down_factor=CALIBRATION_SHRINKAGE_DOWN,
)
model_p_up_v2 = compute_model_p_up_v2(
    displacement=displacement,
    t_remaining=t_remaining,
    sigma_5min=sigma_5min,
    up_ask=up_ask,
    down_ask=down_ask,
)

# Single switch point: the dispatcher reads MODEL_VERSION and routes.
# Note v1 contract: dispatcher returns raw norm.cdf for v1 — apply
# calibrate_p_up here to match historical signal.py behavior.
if compute_model_p_up_active is None:  # belt-and-suspenders for import-time issues
    raise RuntimeError("compute_model_p_up_active unavailable — observer import broken")

import os  # ensure available; safe if already imported at module top
_active_version = os.environ.get("MODEL_VERSION", "v1").strip().lower()
if _active_version == "v2":
    model_p_up = model_p_up_v2
else:
    model_p_up = model_p_up_v1_calibrated  # v1 = calibrated, matches existing live behavior
```

**Note on the dispatcher.** `compute_model_p_up_active` is intentionally *not* called from this path. Its v1 contract is "return raw norm.cdf" (so unit tests can pin it to a single function), but signal.py needs the calibrated v1 (`model_p_up_v1_calibrated`) for actual gating. We keep the dispatcher as the canonical version-routing point for any *future* caller that wants the unified surface; signal.py stays explicit because it has to apply v1 calibration. Verify no third caller exists (`grep compute_model_p_up_active polypocket/`); if one does, that caller must follow signal.py's pattern, not the dispatcher's.

Both versions are computed unconditionally (dual-logging is what enables the paper A/B in Task 6). `compute_model_p_up_v2` derives `market_p_up` internally from `up_ask`/`down_ask` — no new scalar needs threading through `evaluate()`'s signature.

The Signal dataclass already has `model_p_up_raw: float | None = None`. Add two more optional fields — `model_p_up_v1_calibrated: float | None` and `model_p_up_v2: float | None` — populated regardless of which version actually drives the gate.

**`os` import:** verify `signal.py` has `import os` at module top. If not, add it as part of this change rather than the inline `import os` shown above.

### Step 3: Update the logger call site

Verified (2026-04-23): `log_snapshot` is called from `polypocket/bot.py` (the only caller outside `ledger.py` itself). Grep: `grep -n log_snapshot polypocket/bot.py`. At each call site that passes a decision snapshot, add **both** keys to the `stats` dict so they persist on every decision row regardless of which version fired:

```python
stats["model_p_up_v1_calibrated"] = signal.model_p_up_v1_calibrated
stats["model_p_up_v2"] = signal.model_p_up_v2
```

### Step 4: Update existing signal tests

In `tests/test_signal.py`, find any test that calls `evaluate` and asserts `model_p_up` matches a specific value. Add **two** new test cases asserting that both `signal.model_p_up_v1_calibrated` AND `signal.model_p_up_v2` are populated (each a float in [0,1]) when `MODEL_VERSION=v1` is the default. Add a third test that sets `MODEL_VERSION=v2`, fires a decision, and asserts `signal.model_p_up == signal.model_p_up_v2` and `signal.model_p_up_v1_calibrated` is still populated (so dual-logging still holds under v2).

Also fix any test that imports `compute_model_p_up` directly and relies on its return value being the final probability — the v1 calibrated value is now `model_p_up_v1_calibrated`, not `compute_model_p_up`'s raw return. Read the failing test and update, don't blind-patch.

### Step 5: Run tests — expect PASS

Run: `pytest tests/test_signal.py tests/test_observer_v2.py -v`
Expected: all PASS. Also run `pytest tests/ -q` for the full suite. Pre-existing failures noted in `git status` remain; no new failures attributable to this change.

### Step 6: Smoke-check in paper mode

Launch the bot in paper mode for one window with `MODEL_VERSION` unset (v1 default):

```bash
# whatever the paper launch command is — grep README or justfile
```

Wait for one `decision` snapshot to land. Verify all three columns populated:

```bash
sqlite3 paper_trades.db "
SELECT window_slug, model_p_up, model_p_up_v1_calibrated, model_p_up_v2
FROM window_snapshots
WHERE snapshot_type='decision'
ORDER BY id DESC
LIMIT 3;"
```

Expected: all three non-null, all three in [0,1]. Under MODEL_VERSION=v1 (default), `model_p_up == model_p_up_v1_calibrated`. `model_p_up_v2` will diverge from both — that's the whole point.

### Step 7: Commit

```bash
git add polypocket/ledger.py polypocket/signal.py polypocket/bot.py tests/test_signal.py
git commit -m "$(cat <<'EOF'
feat(signal): dual-log v1_calibrated + v2 on every decision (#15)

MODEL_VERSION env var routes which probability fires the trade gate; both
v1_calibrated and v2 are computed unconditionally and logged to dedicated
nullable columns. Adds model_p_up_v1_calibrated + model_p_up_v2 to
window_snapshots (idempotent migration). Enables paper A/B without
rerunning decisions, and survives the cutover (existing model_p_up column
gets overwritten by the firing version, so the dedicated columns are the
stable source for the comparison script).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Rollback:** `git revert HEAD`. Both `model_p_up_v1_calibrated` and `model_p_up_v2` columns stay in the schema (harmless; new rows post-revert just write NULL because the reverted `log_snapshot` signature no longer threads them through). v1 firing path is unchanged. If a follow-up Task 5.1 ever needs to add columns separately from the integration commit, split the migration into its own commit so revert of the integration doesn't desync schema and code.

---

## Task 6: Launch paper A/B

**Files:** none (operational)

### Step 1: Start paper with dual-logging (no version change)

Launch paper mode normally (MODEL_VERSION unset, default `v1` drives the gate). Dual-logging now populates `model_p_up_v2` on every decision regardless.

Note the launch timestamp:

```bash
python -c "import datetime; print(datetime.datetime.utcnow().isoformat(timespec='seconds'))" > .v2_ab_start
```

Add to `.gitignore` if not already covered.

### Step 2: Wait

Target: **≥200 labeled decisions AND ≥20 decisions where v2 predicts p ≥ 0.80**, whichever is later. At the post-G1 paper rate (~250–300/day) the first threshold lands in 1 day; the tail-bin threshold may take longer if v2's tail is sparse under v1's gate.

Periodically check progress:

```bash
sqlite3 paper_trades.db "
SELECT
    COUNT(*) AS labeled_total,
    SUM(CASE WHEN d.model_p_up_v2 >= 0.80 THEN 1 ELSE 0 END) AS tail_bin_n
FROM window_snapshots d
JOIN window_snapshots c
  ON c.window_slug = d.window_slug AND c.snapshot_type='close'
WHERE d.snapshot_type='decision'
  AND c.outcome IS NOT NULL
  AND d.model_p_up_v2 IS NOT NULL
  AND d.model_p_up_v1_calibrated IS NOT NULL
  AND d.timestamp >= '$(cat .v2_ab_start)';
"
```

Proceed to Task 7 when both counts meet their thresholds.

### Step 3: Tail-bin escape hatch

If after 7 wall-clock days the tail-bin count is still <20, v1's gate is starving v2's tail (v2 only sees rows v1 also let through to a decision; if v2 disagrees with v1 in the tail, v2's tail bin stays empty). Flip the paper bot to `MODEL_VERSION=v2` for an additional 2 days so v2's gate populates v2's own tail. After that window expires, re-run the count. If still <10, **do not promote** — v2 has no calibration evidence at the tail, which is exactly the bin #15 was written about.

Document the escape-hatch invocation in `scripts/_model_v2_paper_ab.md` (Task 7) under a "Caveats" section: a tail-bin populated under v2's own gate is not the same as one populated under v1's gate, and that distinction matters for the ship interpretation.

### Step 4: No code changes during the A/B window

If the bot needs restarting, relaunch with the same env. Do not change `config.py` or signal logic during the observation window — it contaminates the sample. The escape hatch in Step 3 is the only sanctioned mid-window change, and it's an env-var flip, not a code change.

---

## Task 7: Analyze paper A/B + promotion decision

**Files:**
- Create: `scripts/compare_model_versions.py`
- Create: `tests/test_compare_model_versions.py`

### Step 1: Write failing tests for the comparison pure functions

Create `tests/test_compare_model_versions.py` covering:

- `bin_reliability(p_values, outcomes, bins)` → per-bin (n, pred, actual, bootstrap_ci).
- `simulate_gate_pnl(rows, p_column, config)` → total PnL, list of per-row fires.
- `bootstrap_diff_ci(series_a, series_b, n_boots)` → (lo, hi) tuple.

Include the same kind of edge-case tests used in Task 2 / Task 3 (empty input, single-sample bin, tie handling).

### Step 2: Run tests — expect FAIL on ModuleNotFoundError

### Step 3: Implement the comparison script

Create `scripts/compare_model_versions.py`:

- Reads `paper_trades.db` (and optionally `live_trades.db`) for decision rows after `--since` with both `model_p_up_v1_calibrated` and `model_p_up_v2` non-null AND a matching close row with outcome. **Reads `model_p_up_v1_calibrated` directly — not `model_p_up`** — because `model_p_up` reflects whichever version fired (= v2 after cutover) and is unreliable as a v1 source.
- Computes the 4-bin reliability table for both versions on the fresh slice.
- Computes simulated gate PnL for both versions using the same gate logic as the live bot (copy structure from `signal.py::evaluate`).
- Bootstraps the PnL delta 95% CI.
- Writes `scripts/_model_v2_paper_ab.md` with:
  - Sample size (total, fires-per-version, win rate)
  - Side-by-side reliability tables (v1 calibrated vs v2)
  - Simulated PnL per version, with the paper-fill-realism caveat noted
  - Delta + bootstrap CI
  - Whether the tail-bin escape hatch was invoked (mid-window flip to MODEL_VERSION=v2)
  - Verdict: **PROMOTE / HOLD / REGRESSION**
- Exit codes: 0=PROMOTE, 1=HOLD, 2=REGRESSION.

Verdict rules (mirror the design doc's promotion criteria):

- **PROMOTE** — fresh-slice reliability gate passes (same rubric as Task 3 Step 6) AND no Criterion 2 veto.
- **REGRESSION** — v2's mean reliability gap on the fresh slice is >5pts worse than on the original training held-out (evidence of regime drift or training overfit), OR Criterion 2 veto trips.
- **HOLD** — Neither. Collect more data or escalate.

### Step 4: Run tests — expect PASS

### Step 5: Run the comparison on the paper A/B data

```bash
python scripts/compare_model_versions.py --since "$(cat .v2_ab_start)"
```

Read the verdict. If PROMOTE, proceed to Task 8. If REGRESSION, stop and escalate (investigate what changed between training corpus and paper deployment). If HOLD, extend the A/B window and re-run.

### Step 6: Commit the script + the A/B artifact

```bash
git add scripts/compare_model_versions.py tests/test_compare_model_versions.py scripts/_model_v2_paper_ab.md
git commit -m "$(cat <<'EOF'
feat(scripts): compare_model_versions A/B report for v2 promotion (#15)

Reads dual-logged model_p_up + model_p_up_v2 from window_snapshots, emits
reliability + simulated-PnL comparison and a PROMOTE/HOLD/REGRESSION verdict.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Rollback:** none needed — this is a read-only analysis artifact.

---

## Task 8: Live cutover

**Files:** none in code (operational); env-var change only. `config.py` default remains `v1` through the 2-week watch window.

### Step 1: Human-approval gate

Cutover affects live trading P&L; an exit-code-0 verdict from the comparison script is *necessary* but not sufficient. Before flipping the env var, the user (not the model agent) must:

1. Re-read `scripts/_model_v2_paper_ab.md` end to end.
2. Confirm verdict is PROMOTE.
3. Confirm the tail-bin n the verdict was based on is what was expected (n≥20 ideally; <20 only if the §"Caveats" notes explain the situation and the user accepts).
4. Confirm the paper-fill-realism caveat does not change the EV interpretation in a way that worries the user.
5. Explicitly approve the cutover in writing (commit message, comment on #15, or equivalent durable artifact).

If any of (1)–(4) raises a concern, do **not** proceed to Step 2. Either extend the A/B window or escalate.

### Step 2: Flip live with the env var

Stop the live bot. Relaunch with `MODEL_VERSION=v2` in the env. Verify on startup that the new dispatch path is live by tailing the log for the first decision snapshot and confirming `model_p_up` ≈ `model_p_up_v2` (they should now both reflect the v2 computation, and `model_p_up_v1_calibrated` is still populated alongside).

### Step 3: Post-flip spot-check after first ~50 live fills

Sample size matters here. Under a true 80%-calibrated predictor, n=5 has a ±35pt bootstrap CI — far too noisy to call regression. Bumping the threshold:

After 50 live fills under v2 (or 1 wall-clock week, whichever lands first):

- Compute the 4-bin reliability on the live-v2 subset (using the same `reliability_table` helper as Task 3 Step 6).
- Compare against the training held-out's reliability from `model_v2_coefs.json["held_out_metrics"]`.
- **Tail bin (0.80+) regression rule:** if the bin has n≥10 AND realized WR is below the lower bound of a one-sided 95% binomial test against the predicted probability (i.e. the observed WR is *worse* than chance against the prediction at p=0.05), flip back (`MODEL_VERSION=v1`), comment the failure mode on #15, and diagnose. With n<10 in the tail bin after 50 fills, extend the watch to 100 fills before applying any rule — the tail bin needs real samples, not a noise-driven trip.
- **Any-bin gross regression rule:** if any bin with n≥20 has gap >10pts (twice the training tolerance), flip back regardless of statistical test — that's a regime mismatch, not noise.

### Step 4: Comment on #15

```bash
gh issue comment 15 --body "v2 cut over to live at <timestamp>. Paper A/B report: scripts/_model_v2_paper_ab.md. Watch window: 2 weeks. Cleanup (retire shrinkage + MAX_EDGE_THRESHOLD_UP) lands if no regression."
```

---

## Task 9: Cleanup (after 2 weeks of live-v2 with no regression)

**Files:**
- Modify: `polypocket/config.py`, `polypocket/signal.py`, `polypocket/observer.py`, relevant tests.

### Step 1: Verify no regressions

Query the post-cutover live fills and confirm:

- Total PnL ≥ baseline paper A/B projected PnL within bootstrap CI (mind the paper-fill-realism caveat — live PnL is the real number; paper projection is biased toward optimism).
- No reliability bin with n≥30 has a gap >7pts.
- 0.80+ bin (if populated to n≥10) has realized WR within the one-sided 95% binomial bound against the predicted probability.

If any fail, stop. Do not clean up; continue monitoring or flip back.

### Step 2: Remove v1 calibration plumbing

- `polypocket/config.py`: delete `CALIBRATION_SHRINKAGE_UP`, `CALIBRATION_SHRINKAGE_DOWN`, `MAX_EDGE_THRESHOLD_UP` and their comment blocks.
- `polypocket/signal.py`: remove the `calibrate_p_up` import, the `MAX_EDGE_THRESHOLD_UP` check in the UP branch, and the `model_version == "v1"` branch (v2 is now the only path). `model_p_up_raw` field on Signal can also go — no caller reads it after v2 cutover.
- `polypocket/observer.py`: delete `calibrate_p_up`. Keep `compute_model_p_up` (v1) for at least 2 more weeks so the env-var escape hatch still works if a silent regression shows up late.
- `polypocket/config.py`: change `MODEL_VERSION` default: `os.getenv("MODEL_VERSION", "v2")`.

### Step 3: Update tests

Any test importing the removed symbols fails. Either delete the test (if it was testing v1-only behavior) or update it to target v2.

### Step 4: Run full test suite

```bash
pytest tests/ -q
```

Expected: all pass (modulo pre-existing failures).

### Step 5: Commit

```bash
git add polypocket/ tests/
git commit -m "$(cat <<'EOF'
chore: retire v1 calibration shrinkage + MAX_EDGE_THRESHOLD_UP (#15)

2 weeks of live-v2 with no regression. CALIBRATION_SHRINKAGE_{UP,DOWN},
MAX_EDGE_THRESHOLD_UP, and the shrinkage path in signal.py are removed.
MODEL_VERSION default is now v2; v1's compute_model_p_up stays as a
reachable fallback for ~2 more weeks.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 6: Close #15 and partially close #13

```bash
gh issue close 15 --comment "v2 shipped, live for 2+ weeks with no regression. Shrinkage and MAX_EDGE_THRESHOLD_UP retired. v1 code path remains for env-var fallback; final removal scheduled for <date+2w>."
gh issue comment 13 --body "UP 0.80+ miscalibration addressed by #15. MAX_EDGE_THRESHOLD_UP retired. Closing the 0.80+ strand; keeping the issue open only if the break-even-minus-fees concern persists on v2 (monitor next 2 weeks)."
```

---

## Task 10 (≥2 weeks after Task 9): Final v1 removal

Delete `compute_model_p_up` from `observer.py`. Remove the v1 branch of `compute_model_p_up_active` (the dispatcher can now always return v2 or be deleted in favor of a direct call). Drop remaining v1-only tests. Commit.

---

## Execution notes

- Tasks 1–5 are code-only. No money at stake. Complete and validate before Task 6.
- Task 6 launches the A/B. No config change — just relaunch with dual-logging active.
- Task 7's comparison is the decision gate. Honor the verdict: if HOLD, extend the window; if REGRESSION, do not flip live.
- Task 8 flips live via env var only. No code change. Easy rollback (unset env, restart).
- Tasks 9–10 are cleanup, separated from cutover by ≥2 weeks of live observation. Do not combine.

## Observability cross-check

Every decision row now has three probability columns after Task 5: `model_p_up` (whichever version fired), `model_p_up_v2` (v2 regardless), and — implicitly via v1's raw path — the raw Brownian output available from recomputation. The comparison script (Task 7) and any future audits operate entirely on this dual-logged trail. No mid-conversation compute needed.
