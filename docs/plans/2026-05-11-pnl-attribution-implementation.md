# PnL attribution Implementation Plan

> **For Claude:** Execute in-chat, one task at a time, linearly. No parallel subagents. Each task has file paths, concrete commands, and a verification + rollback line.
>
> **Revised 2026-05-11** after adversarial review surfaced 20 findings against the original draft. Key changes: `realized_pnl` is now sourced from `trades.pnl` (not recomputed from a formula that diverges on live rows); aggregator defaults to excluding `"approximate"` rows; test assertions reference `SIGNAL_CUSHION_TICKS` instead of hardcoded ranges; Task 0 added for pytest-import precondition; cross-platform backup commands; Task 4 and Task 5 are bundled to avoid an intermediate state where new code reads NULL columns.

**Goal:** Ship the 4-component PnL decomposition (`edge`, `slip`, `expected_fee`, `luck`) from `docs/plans/2026-05-11-pnl-attribution-design.md`. Sum identity holds to float precision on every settled trade; surfaces in `analyze.py`, the TUI, and a weekly markdown report.

**Architecture:** Pure module `polypocket/attribution.py` with no side effects, fed by two persisted fields on `trades` (`signal_reference_price`, `signal_reference_source`). `realized_pnl` is read from `trades.pnl` (authoritative for both paper and live). New trades populate via `signal.py → executor.py → log_trade`. Historical rows populate via a one-shot idempotent backfill script. Aggregations are pure functions consumed by three reporters.

**Tech stack:** Python ≥3.11 stdlib only for the core (`sqlite3`, `dataclasses`, `math`). `textual` for the TUI panel (already a dep). `pytest` + `pytest-asyncio` for tests. No new third-party deps.

**Design doc:** `docs/plans/2026-05-11-pnl-attribution-design.md`.

**Related:** #15 (dual-logging — already shipped, supplies `model_p_up_v1_calibrated` / `model_p_up_v2`); #16 / G1–G5 (data capture — supplies the `up_bids_json` / `down_bids_json` that exact backfill needs, where populated); brainstorm 2026-05-11 Idea #41.

---

## Pre-decision (user, before Task 1)

**Q1 — expected-fee vs realized-fee as the principal column.** Design defaults to expected-fee for the principal `fee_value` (clean `mean(luck) → 0` under calibration). Both are reported. If realized-fee should be principal, say so before Task 3; otherwise default holds.

---

## Task 0: Pre-flight — verify pytest can import from `scripts/`

**Purpose:** Several tasks below import `from scripts.<name> import ...` in test files. `scripts/` is not a Python package (no `__init__.py`). The existing tests (`tests/test_export_training_corpus.py`, `tests/test_cohort_watchdog.py`) do the same and work, which means pytest's `rootdir` is already on `sys.path` via `conftest.py` or `pyproject.toml`. Confirm this is still the case before authoring new tests that depend on it.

### Step 1: Confirm the pattern works today

```bash
pytest tests/test_export_training_corpus.py -k "test_join_returns_labeled_decisions" -v
```

Expected: green. If it fails with `ModuleNotFoundError: scripts`, stop and add `scripts/__init__.py` (or extend `pyproject.toml`'s `[tool.pytest.ini_options]` with `pythonpath = ["."]`) before proceeding.

### Step 2: Capture baseline state for later verification

Run:

```bash
sqlite3 paper_trades.db "SELECT MAX(id) FROM trades" > /tmp/pre_attrib_max_trade_id.txt
sqlite3 paper_trades.db "SELECT COUNT(*) FROM trades WHERE status='settled'" >> /tmp/pre_attrib_max_trade_id.txt
sqlite3 paper_trades.db "SELECT COUNT(*), SUM(CASE WHEN up_bids_json IS NOT NULL AND down_bids_json IS NOT NULL THEN 1 ELSE 0 END) FROM window_snapshots WHERE snapshot_type='decision'" >> /tmp/pre_attrib_max_trade_id.txt
```

The captured `MAX(id)` becomes the boundary for Task 10 Step 4's 7-day forward-soak check. The bids-JSON count is the expected `"exact"` ceiling for the backfill in Task 5.

No commit; this is local diagnostic state.

**Rollback:** N/A (read-only).

---

## Task 1: Add `signal_reference_price` and `signal_reference_source` columns

**Files:**
- Modify: `polypocket/ledger.py`
- Modify: `tests/test_ledger.py`

### Step 1: Write failing test for the new columns

Add to `tests/test_ledger.py`:

```python
def test_init_db_adds_signal_reference_columns(tmp_path):
    """trades table gains signal_reference_price + signal_reference_source as nullable columns."""
    import sqlite3
    from polypocket.ledger import init_db

    db = str(tmp_path / "t.db")
    init_db(db)
    with sqlite3.connect(db) as c:
        cols = {row[1]: row[2] for row in c.execute("PRAGMA table_info(trades)").fetchall()}
    assert cols.get("signal_reference_price") == "REAL"
    assert cols.get("signal_reference_source") == "TEXT"


def test_init_db_signal_reference_columns_are_idempotent(tmp_path):
    """Calling init_db twice does not error and does not duplicate columns."""
    import sqlite3
    from polypocket.ledger import init_db

    db = str(tmp_path / "t.db")
    init_db(db)
    init_db(db)
    with sqlite3.connect(db) as c:
        col_count = sum(1 for row in c.execute("PRAGMA table_info(trades)").fetchall()
                        if row[1] == "signal_reference_price")
    assert col_count == 1
```

Run: `pytest tests/test_ledger.py -k signal_reference -v`. Expected: both FAIL on missing columns.

### Step 2: Add idempotent ALTER statements in `init_db`

In `polypocket/ledger.py`, inside the existing `existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}` block (around line 72), add:

```python
            if "signal_reference_price" not in existing_cols:
                conn.execute("ALTER TABLE trades ADD COLUMN signal_reference_price REAL")
            if "signal_reference_source" not in existing_cols:
                conn.execute("ALTER TABLE trades ADD COLUMN signal_reference_source TEXT")
```

### Step 3: Extend `log_trade` signature

Modify `log_trade` in `polypocket/ledger.py` to accept the new fields:

```python
def log_trade(
    db_path: str,
    window_slug: str,
    side: str,
    entry_price: float,
    size: float,
    fees: float,
    model_p_up: float,
    market_p_up: float,
    edge: float,
    outcome: str | None,
    pnl: float | None,
    status: str,
    signal_reference_price: float | None = None,
    signal_reference_source: str = "live",
) -> int:
```

Extend the INSERT column list and parameter tuple correspondingly. Defaults are chosen so all existing callers compile (`None` price / `"live"` source); callers are updated in Task 4.

### Step 4: Run tests

Run: `pytest tests/test_ledger.py -v`. Expected: all green (new tests pass; existing tests unchanged).

### Step 5: Commit

```bash
git add polypocket/ledger.py tests/test_ledger.py
git commit -m "feat(ledger): add signal_reference_price + source columns (PnL attribution)"
```

**Rollback:** `git revert HEAD`. New columns are nullable; existing readers unaffected.

---

## Task 2: Thread `signal_reference_price` through `Signal`

**Files:**
- Modify: `polypocket/signal.py`
- Modify: `tests/test_signal.py`

### Step 1: Write failing tests

Add to `tests/test_signal.py`:

```python
def test_signal_populates_signal_reference_price_for_up_side():
    """For a UP signal, signal_reference_price equals up_entry = (1 - best_down_bid) + cushion."""
    from polypocket.signal import SignalEngine
    from polypocket.config import SIGNAL_CUSHION_TICKS

    eng = SignalEngine()
    sig = eng.evaluate(
        displacement=0.005, t_elapsed=120, t_remaining=180, sigma_5min=0.002,
        up_ask=0.55, down_ask=0.50,
        up_bids=[{"price": 0.40}],
        down_bids=[{"price": 0.42}],
    )
    assert sig is not None
    assert sig.side == "up"
    expected = (1.0 - 0.42) + SIGNAL_CUSHION_TICKS * 0.01
    assert sig.signal_reference_price == pytest.approx(expected, abs=1e-9)


def test_signal_populates_signal_reference_price_for_down_side():
    """For a DOWN signal, signal_reference_price = (1 - best_up_bid) + cushion."""
    from polypocket.signal import SignalEngine
    from polypocket.config import SIGNAL_CUSHION_TICKS

    eng = SignalEngine()
    sig = eng.evaluate(
        displacement=-0.005, t_elapsed=120, t_remaining=180, sigma_5min=0.002,
        up_ask=0.50, down_ask=0.55,
        up_bids=[{"price": 0.42}],
        down_bids=[{"price": 0.40}],
    )
    assert sig is not None
    assert sig.side == "down"
    expected = (1.0 - 0.42) + SIGNAL_CUSHION_TICKS * 0.01
    assert sig.signal_reference_price == pytest.approx(expected, abs=1e-9)
```

Add `import pytest` to the test file if not present.

Run: `pytest tests/test_signal.py -k signal_reference -v`. Expected: AttributeError / failures on missing field.

### Step 2: Add field to `Signal` dataclass

In `polypocket/signal.py`, extend `Signal`:

```python
@dataclass
class Signal:
    side: str
    model_p_up: float
    market_price: float
    edge: float
    up_edge: float
    down_edge: float
    model_p_up_raw: float | None = None
    model_p_up_v1_calibrated: float | None = None
    model_p_up_v2: float | None = None
    signal_reference_price: float | None = None
```

### Step 3: Populate at signal-construction sites

In `SignalEngine.evaluate`, both the UP and DOWN `return Signal(...)` calls (currently lines ~128 and ~140), add:

- UP branch: `signal_reference_price=up_entry,`
- DOWN branch: `signal_reference_price=down_entry,`

(`up_entry` and `down_entry` are already computed locally at lines 106–107.)

### Step 4: Run tests

Run: `pytest tests/test_signal.py -v`. Expected: all green.

### Step 5: Commit

```bash
git add polypocket/signal.py tests/test_signal.py
git commit -m "feat(signal): expose signal_reference_price on Signal (PnL attribution)"
```

**Rollback:** `git revert HEAD`.

---

## Task 3: Implement `polypocket/attribution.py` with TDD

**Files:**
- Create: `polypocket/attribution.py`
- Create: `tests/test_attribution.py`

### Step 1: Write the algebra and aggregator tests first (will fail with ModuleNotFoundError)

Create `tests/test_attribution.py`:

```python
"""Algebraic and identity tests for PnL attribution.

Sum identity (definitional): edge + slip + expected_fee + luck == realized_pnl
for every settled trade, to float precision. realized_pnl is an *input* to the
decomposition (sourced from trades.pnl), so the identity is true by
construction — these tests guard against the implementation accidentally
dropping a term or mis-aligning side semantics.
"""
import math
import random

import pytest

from polypocket.attribution import (
    PnlAttribution,
    AggregateAttribution,
    attribute_pnl,
    attribute_from_row,
    aggregate_attribution,
)


# --- Eight hand-built canonical cases for the sum identity ----------

@pytest.mark.parametrize(
    "side, won, signal_ref, entry_price",
    [
        ("up",   True,  0.55, 0.58),  # UP win, slip against us
        ("up",   True,  0.58, 0.55),  # UP win, slip in our favor
        ("up",   False, 0.55, 0.58),  # UP loss, slip against
        ("up",   False, 0.58, 0.55),  # UP loss, slip in favor
        ("down", True,  0.55, 0.58),
        ("down", True,  0.58, 0.55),
        ("down", False, 0.55, 0.58),
        ("down", False, 0.58, 0.55),
    ],
)
def test_sum_identity(side, won, signal_ref, entry_price):
    """edge + slip + expected_fee + luck must equal realized_pnl to 1e-9."""
    from polypocket.config import fee_shares

    size = 50.0
    model_p_up = 0.68
    fees = fee_shares(size, entry_price)
    outcome = side if won else ("down" if side == "up" else "up")
    realized_pnl = ((size - fees) - entry_price * size) if won else (-entry_price * size)

    attr = attribute_pnl(
        side=side, size=size, entry_price=entry_price,
        signal_reference_price=signal_ref, model_p_up=model_p_up,
        fees=fees, outcome=outcome, realized_pnl=realized_pnl,
    )
    total = attr.edge_value + attr.slip_value + attr.expected_fee_value + attr.luck_value
    assert abs(total - realized_pnl) < 1e-9, f"diff={total - realized_pnl:.2e}"
    assert attr.realized_pnl == pytest.approx(realized_pnl, abs=1e-9)


def test_signs_are_intuitive_up_win():
    """UP win, model=0.80, gate-ref=0.60, fill=0.62: most PnL → edge; small negative slip;
    small negative expected_fee; positive luck (won at p=0.80, residual = (size-fees)*(1-0.80))."""
    from polypocket.config import fee_shares

    size = 100.0
    entry_price = 0.62
    fees = fee_shares(size, entry_price)
    realized_pnl = (size - fees) - entry_price * size
    attr = attribute_pnl(
        side="up", size=size, entry_price=entry_price,
        signal_reference_price=0.60, model_p_up=0.80,
        fees=fees, outcome="up", realized_pnl=realized_pnl,
    )
    assert attr.edge_value > 0
    assert attr.slip_value < 0
    assert attr.expected_fee_value < 0
    assert attr.luck_value >= 0
    # luck should equal (size - fees) * (1 - model_p_up_for_side)
    assert attr.luck_value == pytest.approx((size - fees) * 0.20, abs=1e-9)


# --- Property test: random tuples preserve sum identity -------------

def test_sum_identity_property():
    """Identity is definitional but mis-aligning side semantics or dropping a
    term would break it. 1000 random tuples shake out coding errors."""
    from polypocket.config import fee_shares

    rng = random.Random(0)
    for _ in range(1000):
        side = rng.choice(["up", "down"])
        outcome = rng.choice(["up", "down"])
        size = rng.uniform(1.0, 200.0)
        entry_price = rng.uniform(0.05, 0.95)
        signal_ref = rng.uniform(0.05, 0.95)
        model_p_up = rng.uniform(0.01, 0.99)
        fees = fee_shares(size, entry_price)
        won = side == outcome
        realized_pnl = ((size - fees) - entry_price * size) if won else (-entry_price * size)

        attr = attribute_pnl(
            side=side, size=size, entry_price=entry_price,
            signal_reference_price=signal_ref, model_p_up=model_p_up,
            fees=fees, outcome=outcome, realized_pnl=realized_pnl,
        )
        total = attr.edge_value + attr.slip_value + attr.expected_fee_value + attr.luck_value
        assert abs(total - realized_pnl) < 1e-9


# --- DB-row adapter -------------------------------------------------

def test_attribute_from_row_handles_missing_signal_reference():
    """When signal_reference_price is NULL, attribute_from_row returns None
    (caller must filter; aggregates skip these)."""
    row = {
        "side": "up", "size": 50.0, "entry_price": 0.60,
        "fees": 0.025, "model_p_up": 0.70, "outcome": "up", "pnl": 19.975,
        "signal_reference_price": None, "signal_reference_source": "missing",
    }
    assert attribute_from_row(row) is None


def test_attribute_from_row_returns_pnl_attribution_when_complete():
    """Complete row produces a PnlAttribution using trades.pnl as realized_pnl."""
    row = {
        "side": "up", "size": 100.0, "entry_price": 0.62,
        "fees": 0.0472, "model_p_up": 0.80, "outcome": "up", "pnl": 37.953,
        "signal_reference_price": 0.60, "signal_reference_source": "exact",
    }
    attr = attribute_from_row(row)
    assert attr is not None
    assert isinstance(attr, PnlAttribution)
    assert attr.realized_pnl == pytest.approx(37.953, abs=1e-9)


def test_attribute_from_row_requires_pnl():
    """Rows without trades.pnl (unsettled or settled-with-null) are unattributable."""
    row = {
        "side": "up", "size": 100.0, "entry_price": 0.62,
        "fees": 0.0472, "model_p_up": 0.80, "outcome": "up", "pnl": None,
        "signal_reference_price": 0.60, "signal_reference_source": "exact",
    }
    assert attribute_from_row(row) is None


# --- Aggregator -----------------------------------------------------

def _make_row(slug, source="exact", pnl=1.0, model_p_up=0.70,
              model_p_up_v2=None, entry_price=0.60, signal_ref=0.55):
    return {
        "window_slug": slug, "side": "up", "size": 10.0, "entry_price": entry_price,
        "fees": 0.005, "model_p_up": model_p_up, "outcome": "up", "pnl": pnl,
        "signal_reference_price": signal_ref, "signal_reference_source": source,
        "model_p_up_v2": model_p_up_v2,
    }


def test_aggregate_counts_provenance():
    rows = [
        _make_row("w1", "exact"),
        _make_row("w2", "exact"),
        _make_row("w3", "approximate"),
        _make_row("w4", "missing"),
        _make_row("w5", None),  # NULL source treated as missing
    ]
    agg = aggregate_attribution(rows)
    assert agg.n_total == 5
    assert agg.n_exact == 2
    assert agg.n_approximate == 1
    assert agg.n_missing == 2


def test_aggregate_excludes_approximate_by_default():
    """Default behavior: approximate rows contribute to counts but not to sums."""
    rows = [
        _make_row("w1", "exact", pnl=1.0),
        _make_row("w2", "approximate", pnl=999.0),  # would dominate if included
    ]
    agg = aggregate_attribution(rows)
    assert agg.realized_pnl == pytest.approx(1.0, abs=1e-9)


def test_aggregate_includes_approximate_when_asked():
    rows = [
        _make_row("w1", "exact", pnl=1.0),
        _make_row("w2", "approximate", pnl=999.0),
    ]
    agg = aggregate_attribution(rows, include_approximate=True)
    assert agg.realized_pnl == pytest.approx(1000.0, abs=1e-9)


def test_aggregate_excludes_missing_unconditionally():
    """Missing-source rows have NULL signal_reference_price and cannot be attributed."""
    rows = [
        _make_row("w1", "exact", pnl=1.0),
        _make_row("w2", "missing", pnl=999.0),
    ]
    rows[1]["signal_reference_price"] = None
    agg = aggregate_attribution(rows, include_approximate=True)
    assert agg.realized_pnl == pytest.approx(1.0, abs=1e-9)


def test_aggregate_infers_model_version():
    """A row is v2-cohort iff model_p_up == model_p_up_v2 (the v2 value was the one that fired)."""
    rows = [
        _make_row("w1", "exact", pnl=1.0, model_p_up=0.70, model_p_up_v2=0.62),  # v1
        _make_row("w2", "exact", pnl=2.0, model_p_up=0.62, model_p_up_v2=0.62),  # v2
    ]
    agg = aggregate_attribution(rows)
    assert agg.n_v1_attributed == 1
    assert agg.n_v2_attributed == 1
```

Run: `pytest tests/test_attribution.py -v`. Expected: all FAIL on `ModuleNotFoundError`.

### Step 2: Implement `polypocket/attribution.py`

Create `polypocket/attribution.py`:

```python
"""Pure PnL attribution: decompose realized PnL into edge / slip / expected_fee / luck.

Sum identity: edge_value + slip_value + expected_fee_value + luck_value == realized_pnl
to float precision, by construction (luck_value is defined as the residual).

realized_pnl is sourced from `trades.pnl` (authoritative for both paper and
live ledgers). The decomposition does NOT recompute realized_pnl from a
formula, because on live trades the algebraic formula diverges from
trades.pnl (see design doc §"Decomposition").

See docs/plans/2026-05-11-pnl-attribution-design.md for the full algebra.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class PnlAttribution:
    realized_pnl: float
    edge_value: float
    slip_value: float
    expected_fee_value: float
    luck_value: float
    # Auxiliary (reported alongside, not in the principal sum):
    realized_fee_value: float
    fee_luck_value: float  # realized_fee_value - expected_fee_value
    # Provenance + cohort
    signal_reference_source: str = "live"
    model_version_attributed: str = "v1"  # "v1" or "v2"; see attribute_from_row


def _side_aligned_model_p(model_p_up: float, side: str) -> float:
    return model_p_up if side == "up" else 1.0 - model_p_up


def attribute_pnl(
    *,
    side: str,
    size: float,
    entry_price: float,
    signal_reference_price: float,
    model_p_up: float,
    fees: float,
    outcome: str,
    realized_pnl: float,
    signal_reference_source: str = "live",
    model_version_attributed: str = "v1",
) -> PnlAttribution:
    """Decompose realized PnL into the four principal components.

    Args:
      side: 'up' or 'down' — the side the trade bought.
      size: shares held after fill.
      entry_price: actual VWAP fill (post-fill `update_trade` value on live).
      signal_reference_price: the price the gate compared model_p_up against.
        Side-aligned (i.e., the executable entry on the chosen side).
      model_p_up: P(BTC up) at decision; this function flips for DOWN side.
      fees: trades.fees — the *intended* fee (logged from intended size/price).
        On live, this is an estimate; on paper, it equals the realized fee on win.
      outcome: 'up' or 'down' — the resolved outcome.
      realized_pnl: trades.pnl — authoritative realized PnL from the settle path.
      signal_reference_source: provenance tag for the reference price.
      model_version_attributed: 'v1' or 'v2' — which model drove this trade.
    """
    won = side == outcome
    model_p_for_side = _side_aligned_model_p(model_p_up, side)
    edge_value = size * (model_p_for_side - signal_reference_price)
    slip_value = size * (signal_reference_price - entry_price)
    expected_fee_value = -fees * model_p_for_side
    luck_value = realized_pnl - (edge_value + slip_value + expected_fee_value)

    realized_fee_value = -fees if won else 0.0
    fee_luck_value = realized_fee_value - expected_fee_value

    return PnlAttribution(
        realized_pnl=realized_pnl,
        edge_value=edge_value,
        slip_value=slip_value,
        expected_fee_value=expected_fee_value,
        luck_value=luck_value,
        realized_fee_value=realized_fee_value,
        fee_luck_value=fee_luck_value,
        signal_reference_source=signal_reference_source,
        model_version_attributed=model_version_attributed,
    )


def _infer_model_version(row: dict) -> str:
    """Infer which model drove the trade by comparing model_p_up with model_p_up_v2.

    Per signal.py:99-102, trades.model_p_up == model_p_up_v2 iff MODEL_VERSION=v2
    was active. If the v2 column is NULL (pre-#15 dual-logging) treat as v1.
    """
    v2 = row.get("model_p_up_v2")
    if v2 is None:
        return "v1"
    return "v2" if abs(row["model_p_up"] - v2) < 1e-9 else "v1"


def attribute_from_row(row: dict) -> PnlAttribution | None:
    """Adapter: takes a trades-table row dict; returns None if not attributable.

    Skips rows missing pnl, signal_reference_price, outcome, or any required field.
    Use this in aggregation loops; callers should filter Nones and count them.
    """
    required = ("side", "size", "entry_price", "fees", "model_p_up",
                "outcome", "signal_reference_price", "pnl")
    for key in required:
        if row.get(key) is None:
            return None
    return attribute_pnl(
        side=row["side"], size=row["size"], entry_price=row["entry_price"],
        signal_reference_price=row["signal_reference_price"],
        model_p_up=row["model_p_up"], fees=row["fees"],
        outcome=row["outcome"], realized_pnl=row["pnl"],
        signal_reference_source=row.get("signal_reference_source") or "unknown",
        model_version_attributed=_infer_model_version(row),
    )


@dataclass(frozen=True)
class AggregateAttribution:
    n_total: int
    n_exact: int
    n_approximate: int
    n_missing: int
    n_v1_attributed: int
    n_v2_attributed: int
    realized_pnl: float
    edge_sum: float
    slip_sum: float
    expected_fee_sum: float
    luck_sum: float
    realized_fee_sum: float
    fee_luck_sum: float


def aggregate_attribution(
    rows: list[dict], *, include_approximate: bool = False
) -> AggregateAttribution:
    """Aggregate per-component sums over a list of trades rows.

    DEFAULT: excludes signal_reference_source='approximate' rows from the sums
    (still counted in n_approximate). Approximate rows have biased-toward-zero
    slip and would inflate edge_sum; the design's headline aggregates report
    exact rows only.

    Pass include_approximate=True for a context line alongside the headline.

    Missing-source rows are excluded unconditionally (no signal_reference_price
    to attribute against).
    """
    n_total = len(rows)
    n_exact = sum(1 for r in rows if r.get("signal_reference_source") == "exact"
                  or r.get("signal_reference_source") == "live")
    n_approximate = sum(1 for r in rows if r.get("signal_reference_source") == "approximate")
    n_missing = sum(
        1 for r in rows
        if r.get("signal_reference_source") in (None, "missing")
        or r.get("signal_reference_price") is None
    )

    n_v1 = n_v2 = 0
    realized_pnl = edge_sum = slip_sum = expected_fee_sum = luck_sum = 0.0
    realized_fee_sum = fee_luck_sum = 0.0
    for r in rows:
        if not include_approximate and r.get("signal_reference_source") == "approximate":
            continue
        a = attribute_from_row(r)
        if a is None:
            continue
        realized_pnl += a.realized_pnl
        edge_sum += a.edge_value
        slip_sum += a.slip_value
        expected_fee_sum += a.expected_fee_value
        luck_sum += a.luck_value
        realized_fee_sum += a.realized_fee_value
        fee_luck_sum += a.fee_luck_value
        if a.model_version_attributed == "v2":
            n_v2 += 1
        else:
            n_v1 += 1

    return AggregateAttribution(
        n_total=n_total, n_exact=n_exact, n_approximate=n_approximate, n_missing=n_missing,
        n_v1_attributed=n_v1, n_v2_attributed=n_v2,
        realized_pnl=realized_pnl, edge_sum=edge_sum, slip_sum=slip_sum,
        expected_fee_sum=expected_fee_sum, luck_sum=luck_sum,
        realized_fee_sum=realized_fee_sum, fee_luck_sum=fee_luck_sum,
    )
```

### Step 3: Run tests

Run: `pytest tests/test_attribution.py -v`. Expected: all green (8 parametric, 1 intuition, 1 property, 3 row-adapter, 5 aggregator = 18 tests).

### Step 4: Commit

```bash
git add polypocket/attribution.py tests/test_attribution.py
git commit -m "feat(attribution): pure 4-component PnL decomposition + aggregator"
```

**Rollback:** `git revert HEAD`. Module is pure; nothing imports it yet.

---

## Task 4 + Task 5: Thread `signal_reference_price` through executor AND backfill historical rows (one commit, sequenced)

> **Why bundled:** between Tasks 4 and 5 alone, the trades table has live `"live"` rows mixed with old `NULL`-source rows; any aggregate run in that window looks pathological. Running and shipping both in one commit guarantees the data is in a consistent state.

**Files:**
- Modify: `polypocket/executor.py`
- Modify: `tests/test_executor.py`
- Create: `scripts/backfill_signal_reference.py`
- Create: `tests/test_backfill_signal_reference.py`
- Create: `scripts/_backfill_signal_reference.md`

### Step 1: Update `execute_paper_trade` and `execute_live_trade`

Both functions currently call `log_trade(..., status=...)`. Extend them to pass `signal.signal_reference_price` and `signal_reference_source="live"`:

In `execute_paper_trade` (around line 237):
```python
        trade_id = log_trade(
            db_path=db_path,
            ...
            pnl=pnl,
            status=status,
            signal_reference_price=signal.signal_reference_price,
            signal_reference_source="live",
        )
```

In `execute_live_trade` (around line 307): same addition.

**Audit `reconcile_recovered_trade` (executor.py:78–130):** this path can resurrect a stranded fill via `update_trade(..., size=, entry_price=)` on an existing row. It does NOT call `log_trade`, so it cannot set `signal_reference_price` on a newly-discovered trade. Verify by reading: if reconcile creates new trade rows (it doesn't today — only updates existing ones), they must also persist `signal_reference_price`. Today's code only updates rows that were already logged through `execute_live_trade`, which means the reference price is already persisted. Document this assumption in the test below; if `reconcile_recovered_trade` ever starts creating new rows, this assumption breaks.

### Step 2: Add executor test

Add to `tests/test_executor.py`:

```python
def test_execute_paper_trade_persists_signal_reference_price(tmp_path):
    from polypocket.ledger import init_db, find_trade_by_window_slug
    from polypocket.executor import execute_paper_trade
    from polypocket.signal import Signal

    db = str(tmp_path / "p.db")
    init_db(db)
    sig = Signal(side="up", model_p_up=0.72, market_price=0.55, edge=0.12,
                 up_edge=0.12, down_edge=0.0, signal_reference_price=0.60)
    res = execute_paper_trade(db, sig, entry_price=0.60, size=10.0,
                              window_slug="w1", outcome="up")
    assert res.success
    row = find_trade_by_window_slug(db, "w1")
    assert row["signal_reference_price"] == pytest.approx(0.60, abs=1e-9)
    assert row["signal_reference_source"] == "live"
```

Run: `pytest tests/test_executor.py -k signal_reference -v`. Expected: green.

### Step 3: Write backfill tests

Create `tests/test_backfill_signal_reference.py`:

```python
"""Tests for the one-shot signal_reference_price backfill.

Provenance per the design:
  'exact'        : decision snapshot has the side-relevant non-null bids JSON
  'approximate'  : decision snapshot exists but lacks the side-relevant bids JSON
  'missing'      : no decision snapshot for the window
"""
import json
import sqlite3

import pytest

from polypocket.ledger import init_db, log_trade, log_snapshot
from scripts.backfill_signal_reference import backfill


def _seed_trade_and_decision(db, slug, side, has_opp_bids):
    """has_opp_bids: True populates the bids the given side's gate needs."""
    log_trade(
        db_path=db, window_slug=slug, side=side, entry_price=0.60, size=10.0,
        fees=0.024, model_p_up=0.70, market_p_up=0.58, edge=0.12,
        outcome="up", pnl=3.976, status="settled",
    )
    stats = {"btc_price": 65000, "window_open_price": 64900,
             "displacement": 0.0015, "sigma_5min": 0.002, "model_p_up": 0.70,
             "t_remaining": 200, "up_ask": 0.58, "down_ask": 0.42,
             "market_p_up": 0.58, "edge": 0.12, "preview_side": side,
             "quote_status": "ok"}
    book = None
    if has_opp_bids:
        # For side='up', the opp side is 'down', so we need down_bids.
        # For side='down', we need up_bids.
        book = {"up": [], "down": [],
                "up_bids": [{"price": 0.42}] if side == "down" else None,
                "down_bids": [{"price": 0.42}] if side == "up" else None}
    log_snapshot(db, slug, "decision", stats, book_depth=book)


def test_backfill_tags_exact_when_side_relevant_bids_present(tmp_path):
    from polypocket.config import SIGNAL_CUSHION_TICKS

    db = str(tmp_path / "t.db")
    init_db(db)
    _seed_trade_and_decision(db, "w1", "up", has_opp_bids=True)
    counts = backfill(db)
    assert counts["exact"] == 1
    with sqlite3.connect(db) as c:
        row = c.execute(
            "SELECT signal_reference_price, signal_reference_source "
            "FROM trades WHERE window_slug='w1'"
        ).fetchone()
    assert row[1] == "exact"
    expected = (1.0 - 0.42) + SIGNAL_CUSHION_TICKS * 0.01
    assert row[0] == pytest.approx(expected, abs=1e-9)


def test_backfill_tags_approximate_when_opp_bids_missing(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    _seed_trade_and_decision(db, "w2", "up", has_opp_bids=False)
    counts = backfill(db)
    assert counts["approximate"] == 1
    with sqlite3.connect(db) as c:
        row = c.execute(
            "SELECT signal_reference_price, signal_reference_source "
            "FROM trades WHERE window_slug='w2'"
        ).fetchone()
    assert row[1] == "approximate"
    assert row[0] == pytest.approx(0.58, abs=1e-9)  # falls back to up_ask


def test_backfill_tags_missing_when_no_decision_snapshot(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    log_trade(db_path=db, window_slug="w3", side="up", entry_price=0.6, size=10.0,
              fees=0.024, model_p_up=0.7, market_p_up=0.58, edge=0.12,
              outcome="up", pnl=3.976, status="settled")
    counts = backfill(db)
    assert counts["missing"] == 1
    with sqlite3.connect(db) as c:
        row = c.execute(
            "SELECT signal_reference_price, signal_reference_source "
            "FROM trades WHERE window_slug='w3'"
        ).fetchone()
    assert row[1] == "missing"
    assert row[0] is None


def test_backfill_is_idempotent(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    _seed_trade_and_decision(db, "w4", "up", has_opp_bids=True)
    backfill(db)
    with sqlite3.connect(db) as c:
        first = c.execute("SELECT signal_reference_price, signal_reference_source "
                          "FROM trades WHERE window_slug='w4'").fetchone()
    counts2 = backfill(db)
    assert counts2["skipped"] == 1
    with sqlite3.connect(db) as c:
        second = c.execute("SELECT signal_reference_price, signal_reference_source "
                           "FROM trades WHERE window_slug='w4'").fetchone()
    assert first == second


def test_backfill_does_not_overwrite_live_rows(tmp_path):
    """Rows already tagged 'live' (from a real trade post-Task 4) are left alone."""
    db = str(tmp_path / "t.db")
    init_db(db)
    log_trade(db_path=db, window_slug="w5", side="up", entry_price=0.6, size=10.0,
              fees=0.024, model_p_up=0.7, market_p_up=0.58, edge=0.12,
              outcome="up", pnl=3.976, status="settled",
              signal_reference_price=0.59, signal_reference_source="live")
    backfill(db)
    with sqlite3.connect(db) as c:
        row = c.execute("SELECT signal_reference_price, signal_reference_source "
                        "FROM trades WHERE window_slug='w5'").fetchone()
    assert row == (0.59, "live")
```

Run: `pytest tests/test_backfill_signal_reference.py -v`. Expected: FAIL on `ModuleNotFoundError`.

### Step 4: Implement the backfill

Create `scripts/backfill_signal_reference.py`:

```python
"""One-shot backfill: compute signal_reference_price for historical trades rows.

Idempotent. Skips rows already tagged (signal_reference_source IS NOT NULL).
Provenance per row:
  'exact'        — recomputed from the side-relevant decision-snapshot bids JSON
  'approximate'  — fell back to decision-snapshot up_ask/down_ask
  'missing'      — no decision snapshot, or no ask price either

Run with: python scripts/backfill_signal_reference.py [--db PATH]
Default DB is polypocket.config.PAPER_DB_PATH.
"""
import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing

from polypocket.config import PAPER_DB_PATH, SIGNAL_CUSHION_TICKS


def _effective_entry(ask: float | None, opp_bids: list[dict] | None) -> float | None:
    if not opp_bids:
        return ask
    best_opp = max(float(b["price"]) for b in opp_bids)
    return min(0.99, (1.0 - best_opp) + SIGNAL_CUSHION_TICKS * 0.01)


def _classify_and_compute(
    side: str, decision: dict | None
) -> tuple[float | None, str]:
    if decision is None:
        return None, "missing"
    up_bids = json.loads(decision["up_bids_json"]) if decision.get("up_bids_json") else None
    down_bids = json.loads(decision["down_bids_json"]) if decision.get("down_bids_json") else None
    if side == "up":
        opp = down_bids
        ask = decision.get("up_ask")
    else:
        opp = up_bids
        ask = decision.get("down_ask")
    if opp:
        return _effective_entry(ask, opp), "exact"
    if ask is not None:
        return float(ask), "approximate"
    return None, "missing"


def backfill(db_path: str) -> dict:
    """Returns counts: {'exact': N, 'approximate': N, 'missing': N, 'skipped': N}."""
    counts = {"exact": 0, "approximate": 0, "missing": 0, "skipped": 0}
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        trades = conn.execute(
            "SELECT id, window_slug, side, signal_reference_source "
            "FROM trades"
        ).fetchall()
        for t in trades:
            if t["signal_reference_source"] is not None:
                counts["skipped"] += 1
                continue
            decision = conn.execute(
                "SELECT up_ask, down_ask, up_bids_json, down_bids_json "
                "FROM window_snapshots "
                "WHERE window_slug = ? AND snapshot_type = 'decision' "
                "LIMIT 1",
                (t["window_slug"],),
            ).fetchone()
            decision_dict = dict(decision) if decision is not None else None
            price, source = _classify_and_compute(t["side"], decision_dict)
            counts[source] += 1
            conn.execute(
                "UPDATE trades SET signal_reference_price = ?, "
                "signal_reference_source = ? WHERE id = ?",
                (price, source, t["id"]),
            )
        conn.commit()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill signal_reference_price on trades")
    parser.add_argument("--db", default=PAPER_DB_PATH)
    parser.add_argument("--skip-if-missing", action="store_true",
                        help="No-op (with exit 0) if --db does not exist on disk")
    args = parser.parse_args()
    if args.skip_if_missing and not os.path.exists(args.db):
        print(f"Skipping backfill: {args.db} does not exist")
        return 0
    counts = backfill(args.db)
    print(f"Backfill complete on {args.db}: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

### Step 5: Run tests

Run: `pytest tests/test_backfill_signal_reference.py tests/test_executor.py -v`. Expected: all green.

### Step 6: Back up the databases before running against real data

Cross-platform (PowerShell on Windows, bash on POSIX):

PowerShell:
```powershell
Copy-Item paper_trades.db paper_trades.pre-attrib-backfill.bak.db
if (Test-Path live_trades.db) { Copy-Item live_trades.db live_trades.pre-attrib-backfill.bak.db }
```

bash / git-bash:
```bash
cp paper_trades.db paper_trades.pre-attrib-backfill.bak.db
[ -f live_trades.db ] && cp live_trades.db live_trades.pre-attrib-backfill.bak.db
```

### Step 7: Run the backfill

```bash
python scripts/backfill_signal_reference.py --db paper_trades.db
python scripts/backfill_signal_reference.py --db live_trades.db --skip-if-missing
```

Expected paper output: counts dict where `exact + approximate + missing` equals the row count from Task 0 Step 2, with `exact` close to (but not exceeding) the bids-JSON count captured at Task 0. Approximate is expected to dominate (~70%) per the design's empirical-coverage note.

Sanity-check:

```bash
sqlite3 paper_trades.db "SELECT signal_reference_source, COUNT(*) FROM trades GROUP BY signal_reference_source"
```

### Step 8: Write the backfill log

Write `scripts/_backfill_signal_reference.md`:

```markdown
# Signal-reference backfill log

**Date:** YYYY-MM-DD
**SIGNAL_CUSHION_TICKS at backfill time:** 8
**Task 0 baseline (paper):**
  MAX(trades.id) = N_PRE
  COUNT(settled trades) = M_PRE
  bids-JSON-populated decision rows = K_PRE / TOTAL_DECISION_PRE

## Paper DB
| source | count |
| --- | --- |
| exact | N |
| approximate | N |
| missing | N |
| skipped (already tagged) | N |

## Live DB
[same table or "DB did not exist; skipped"]

## Notes
- Approximate rows under-count slippage by the average pair-merge wedge;
  excluded from headline aggregates per design.
- A re-run is a no-op (skipped count == row count).
- Task 10 Step 4's forward-soak boundary is MAX(trades.id) = N_PRE.
```

Fill `N_PRE` / `M_PRE` / `K_PRE` / `TOTAL_DECISION_PRE` from the file written in Task 0 Step 2.

### Step 9: Commit

```bash
git add polypocket/executor.py tests/test_executor.py \
        scripts/backfill_signal_reference.py tests/test_backfill_signal_reference.py \
        scripts/_backfill_signal_reference.md
git commit -m "feat(attribution): persist signal_reference_price live + backfill history"
```

**Rollback:** `git revert HEAD` reverts code. To restore DB state, copy the `.bak.db` files back over `paper_trades.db` / `live_trades.db`. The backfill writes only to the two new columns, but DB snapshots are cheap insurance.

---

## Task 6: Wire attribution into `analyze.py`

**Files:**
- Modify: `polypocket/analyze.py`

### Step 1: Add Section 7 to `generate_report`

In `polypocket/analyze.py`, after the existing reliability section, add:

```python
    # ================================================================
    h2("7. PnL Attribution")
    # ================================================================

    from polypocket.attribution import aggregate_attribution

    agg_lifetime = aggregate_attribution(settled)
    agg_lifetime_all = aggregate_attribution(settled, include_approximate=True)
    agg_last20 = aggregate_attribution(settled[-20:]) if settled else None

    def _fmt_agg(label, a) -> list:
        if a is None or a.n_total == 0:
            return [label, "--", "--", "--", "--", "--", "--", "--"]
        return [
            label, a.n_total,
            f"${a.realized_pnl:+.2f}",
            f"${a.edge_sum:+.2f}",
            f"${a.slip_sum:+.2f}",
            f"${a.expected_fee_sum:+.2f}",
            f"${a.luck_sum:+.2f}",
            f"${a.fee_luck_sum:+.2f}",
        ]

    p("**Headline (exact + live rows only)** — `approximate` rows excluded "
      "because their slip is biased toward zero (see design doc).")
    table(
        ["Window", "N", "Realized", "Edge", "Slip", "Exp.Fee", "Luck", "Fee-luck"],
        [
            _fmt_agg("Lifetime", agg_lifetime),
            _fmt_agg("Last 20", agg_last20),
        ],
    )
    p(f"**Context (all rows incl. approximate):** "
      f"realized=${agg_lifetime_all.realized_pnl:+.2f}, "
      f"edge=${agg_lifetime_all.edge_sum:+.2f}, "
      f"slip=${agg_lifetime_all.slip_sum:+.2f}")
    p(f"**Provenance:** exact/live={agg_lifetime.n_exact}, "
      f"approximate={agg_lifetime.n_approximate}, "
      f"missing={agg_lifetime.n_missing}")
    p(f"**Model cohort:** v1-attributed={agg_lifetime.n_v1_attributed}, "
      f"v2-attributed={agg_lifetime.n_v2_attributed}")
```

### Step 2: Smoke test on the actual DB

Run the report:

```bash
python -c "from polypocket.analyze import generate_report; print(generate_report('paper_trades.db'))" > /tmp/report.md
```

Expected: report includes a "7. PnL Attribution" section with non-empty numbers and both headline and context lines. Sanity: `Realized` in the context (all rows) equals the sum of `pnl` from settled trades where `signal_reference_source` is not `"missing"` and `pnl` is not NULL.

### Step 3: Commit

```bash
git add polypocket/analyze.py
git commit -m "feat(analyze): add PnL attribution section to report"
```

**Rollback:** `git revert HEAD`.

---

## Task 7: Add `AttributionPanel` to the TUI (with a unit-testable render function)

**Files:**
- Modify: `polypocket/tui.py`
- Add test: `tests/test_attribution.py` (extend) OR `tests/test_tui_attribution.py`

### Step 1: Extract a pure render function

In `polypocket/tui.py`, define:

```python
def render_attribution_text(db_path: str) -> str:
    """Pure function: read settled trades, render the TUI panel body as text.

    Factored out of AttributionPanel so it can be unit-tested without
    instantiating textual widgets.
    """
    import sqlite3
    from contextlib import closing
    from polypocket.attribution import aggregate_attribution

    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        all_settled = [dict(r) for r in conn.execute(
            "SELECT * FROM trades WHERE status='settled' ORDER BY id"
        ).fetchall()]

    last20 = all_settled[-20:]
    agg_life = aggregate_attribution(all_settled)
    agg_20 = aggregate_attribution(last20)

    def fmt(a):
        return (f"R ${a.realized_pnl:+7.2f}  "
                f"E ${a.edge_sum:+7.2f}  "
                f"S ${a.slip_sum:+7.2f}  "
                f"F ${a.expected_fee_sum:+7.2f}  "
                f"L ${a.luck_sum:+7.2f}")

    lines = ["[bold]PNL ATTRIBUTION (exact/live only)[/bold]", ""]
    lines.append(f"Lifetime (n={agg_life.n_total}):")
    lines.append(f"  {fmt(agg_life)}")
    lines.append("")
    lines.append(f"Last 20 (n={agg_20.n_total}):")
    lines.append(f"  {fmt(agg_20)}")
    lines.append("")
    lines.append(f"Provenance: exact/live={agg_life.n_exact} "
                 f"approx={agg_life.n_approximate} missing={agg_life.n_missing}")
    lines.append(f"Cohort: v1={agg_life.n_v1_attributed} v2={agg_life.n_v2_attributed}")
    return "\n".join(lines)


class AttributionPanel(Static):
    def update_attribution(self, db_path: str) -> None:
        self.update(render_attribution_text(db_path))
```

### Step 2: Mount the panel

Locate the existing `compose()` in `PolypocketApp` (the textual app class). Add `AttributionPanel(id="attribution-panel")` to the layout next to `StatusPanel`. Hook its `update_attribution(db_path)` call into the same refresh path that calls `StatusPanel.update_stats(...)`.

### Step 3: Write a render-function test

Append to `tests/test_attribution.py` (or create `tests/test_tui_attribution.py` if preferred):

```python
def test_render_attribution_text_handles_empty_db(tmp_path):
    """Empty DB renders without errors; counts read zero."""
    from polypocket.ledger import init_db
    from polypocket.tui import render_attribution_text

    db = str(tmp_path / "empty.db")
    init_db(db)
    text = render_attribution_text(db)
    assert "PNL ATTRIBUTION" in text
    assert "Lifetime (n=0)" in text


def test_render_attribution_text_with_seeded_trades(tmp_path):
    """Realized numbers in the rendered text match the trades.pnl sum."""
    from polypocket.ledger import init_db, log_trade
    from polypocket.tui import render_attribution_text

    db = str(tmp_path / "seeded.db")
    init_db(db)
    log_trade(db_path=db, window_slug="w1", side="up", entry_price=0.60, size=10.0,
              fees=0.024, model_p_up=0.70, market_p_up=0.58, edge=0.12,
              outcome="up", pnl=3.976, status="settled",
              signal_reference_price=0.58, signal_reference_source="exact")
    text = render_attribution_text(db)
    assert "+3.98" in text  # realized PnL formatted to 2dp
```

Run: `pytest tests/test_attribution.py -v`. Expected: green.

### Step 4: Smoke test the TUI

```bash
python -m polypocket.tui
```

Expected: panel renders, numbers match those in the analyze.py report from Task 6. Quit with `q` or Ctrl+C.

### Step 5: Commit

```bash
git add polypocket/tui.py tests/test_attribution.py
git commit -m "feat(tui): add PnL attribution panel + render-function test"
```

**Rollback:** `git revert HEAD`.

---

## Task 8: Weekly attribution report script

**Files:**
- Create: `scripts/pnl_attribution_report.py`
- Update `.gitignore` to exclude generated report
- Commit one sample artifact

### Step 1: Implement the report generator

Create `scripts/pnl_attribution_report.py`:

```python
"""Generate scripts/_pnl_attribution.md from the live + paper ledgers.

Run manually or from cron; idempotent (overwrites the output file).
"""
import argparse
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from polypocket.attribution import aggregate_attribution
from polypocket.config import LIVE_DB_PATH, PAPER_DB_PATH


def _load_settled(db_path: str) -> list[dict]:
    if not os.path.exists(db_path):
        return []
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM trades WHERE status='settled' ORDER BY id"
        ).fetchall()]


def _section(name: str, rows: list[dict]) -> list[str]:
    out = [f"## {name}\n"]
    if not rows:
        out.append("_no settled trades (or DB does not exist)_\n")
        return out
    agg_life = aggregate_attribution(rows)
    agg_life_all = aggregate_attribution(rows, include_approximate=True)
    agg_100 = aggregate_attribution(rows[-100:])
    agg_20 = aggregate_attribution(rows[-20:])
    out.append("**Headline (exact/live only):**\n")
    out.append("| Window | N | Realized | Edge | Slip | Exp.Fee | Luck | Fee-luck |")
    out.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for label, a in [("Lifetime", agg_life), ("Last 100", agg_100), ("Last 20", agg_20)]:
        out.append(
            f"| {label} | {a.n_total} | ${a.realized_pnl:+.2f} | ${a.edge_sum:+.2f} | "
            f"${a.slip_sum:+.2f} | ${a.expected_fee_sum:+.2f} | ${a.luck_sum:+.2f} | "
            f"${a.fee_luck_sum:+.2f} |"
        )
    out.append("")
    out.append(
        f"**Context (all rows incl. approximate):** lifetime "
        f"realized=${agg_life_all.realized_pnl:+.2f}, edge=${agg_life_all.edge_sum:+.2f}, "
        f"slip=${agg_life_all.slip_sum:+.2f}\n"
    )
    out.append(
        f"**Provenance:** exact/live={agg_life.n_exact}, "
        f"approximate={agg_life.n_approximate}, missing={agg_life.n_missing}\n"
    )
    out.append(
        f"**Model cohort:** v1-attributed={agg_life.n_v1_attributed}, "
        f"v2-attributed={agg_life.n_v2_attributed}\n"
    )
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="scripts/_pnl_attribution.md")
    args = parser.parse_args()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    paper = _load_settled(PAPER_DB_PATH)
    live = _load_settled(LIVE_DB_PATH)

    lines = [f"# PnL Attribution Report -- {now}\n"]
    lines += _section("Paper", paper)
    lines += _section("Live", live)

    Path(args.out).write_text("\n".join(lines))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

### Step 2: Decide what to commit

The report regenerates frequently. Two precedents in the repo:
- `scripts/_model_v2_paper_ab.md` — committed once as a reference artifact, not regenerated in CI.
- Most ad-hoc `analyze.py` output — gitignored.

Adopted policy: commit the **first generated artifact** as `scripts/_pnl_attribution.md` so it's discoverable; add the path to `.gitignore` to suppress noisy diffs on subsequent regenerations. If a future change to the report format warrants an updated reference, re-add and commit explicitly.

Update `.gitignore`:

```
# Generated PnL attribution reports (committed sample is whitelisted on first run only).
scripts/_pnl_attribution.md
```

### Step 3: Generate the first report and force-add the sample

```bash
python scripts/pnl_attribution_report.py
git add -f scripts/_pnl_attribution.md  # bypass .gitignore for the one-time sample
```

### Step 4: Commit

```bash
git add scripts/pnl_attribution_report.py .gitignore scripts/_pnl_attribution.md
git commit -m "feat(scripts): weekly PnL attribution report generator"
```

**Rollback:** `git revert HEAD`. Re-runs do not pollute git status thanks to `.gitignore`.

---

## Task 9: Hand-replay reference trades

**Files:**
- Create: `scripts/_pnl_attribution_reference.md`

### Step 1: Pick three trades

From `paper_trades.db`, query for representative rows. Restrict to `exact` or `live` provenance — `approximate` rows are not suitable references because their slip is biased.

```bash
sqlite3 paper_trades.db "SELECT id, window_slug, side, entry_price, signal_reference_price, model_p_up, fees, outcome, pnl FROM trades WHERE status='settled' AND signal_reference_source IN ('exact', 'live') ORDER BY ABS(entry_price - signal_reference_price) DESC LIMIT 5"
```

Pick the highest-slip row. Then a low-slip win and a low-slip loss:

```bash
sqlite3 paper_trades.db "SELECT id, window_slug, side, entry_price, signal_reference_price, model_p_up, fees, outcome, pnl FROM trades WHERE status='settled' AND signal_reference_source IN ('exact', 'live') AND ABS(entry_price - signal_reference_price) < 0.005 AND outcome = side ORDER BY id DESC LIMIT 3"
sqlite3 paper_trades.db "SELECT id, window_slug, side, entry_price, signal_reference_price, model_p_up, fees, outcome, pnl FROM trades WHERE status='settled' AND signal_reference_source IN ('exact', 'live') AND ABS(entry_price - signal_reference_price) < 0.005 AND outcome != side ORDER BY id DESC LIMIT 3"
```

### Step 2: Hand-compute each one

For each chosen trade, write the math step-by-step in `scripts/_pnl_attribution_reference.md`:

```markdown
# PnL attribution reference trades (regression artifact)

For each trade, the math is recomputed by hand using the algebra in
`docs/plans/2026-05-11-pnl-attribution-design.md`. realized_pnl is taken
directly from trades.pnl (not recomputed). These serve as a regression
artifact: if `attribution.py` ever drifts from the design, re-running it
against these trades should reproduce these numbers to abs_tol=1e-6.

## Trade 1 — clean win (window_slug=...)
- side=up, size=..., entry_price=..., signal_ref=..., model_p_up=..., fees=..., outcome=up
- trades.pnl = ... (authoritative realized_pnl)
- edge_value = size*(model_p_up - signal_ref) = ...
- slip_value = size*(signal_ref - entry_price) = ...
- expected_fee_value = -fees*model_p_up = ...
- luck_value = trades.pnl - (edge + slip + exp_fee) = ...
- Sum check: edge + slip + exp_fee + luck = ... ≈ trades.pnl ✓

## Trade 2 — clean loss ...
## Trade 3 — high-slip fill ...
```

### Step 3: Verify by running attribution.py

```bash
python -c "
import math
import sqlite3
from polypocket.attribution import attribute_from_row
with sqlite3.connect('paper_trades.db') as c:
    c.row_factory = sqlite3.Row
    for slug in ['SLUG_1', 'SLUG_2', 'SLUG_3']:
        r = dict(c.execute('SELECT * FROM trades WHERE window_slug=?', (slug,)).fetchone())
        a = attribute_from_row(r)
        print(slug, a)
"
```

Compare each component against hand-computed values. Each `|actual − expected| < 1e-6` per `math.isclose(..., abs_tol=1e-6)`.

### Step 4: Commit

```bash
git add scripts/_pnl_attribution_reference.md
git commit -m "docs(scripts): reference PnL attributions for 3 trades (regression artifact)"
```

---

## Task 10: Verification and closeout

### Step 1: Full test run

```bash
pytest -v
```

Expected: full suite green, including all new tests.

### Step 2: Verify aggregate sanity on paper DB (exact/live rows only)

```bash
python -c "
import sqlite3
from polypocket.attribution import aggregate_attribution
with sqlite3.connect('paper_trades.db') as c:
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        \"SELECT * FROM trades WHERE status='settled' AND signal_reference_source IN ('exact', 'live') AND pnl IS NOT NULL\"
    ).fetchall()]
realized_from_pnl = sum(r['pnl'] for r in rows)
agg = aggregate_attribution(rows)
print(f'realized_from_pnl_column = {realized_from_pnl:.6f}')
print(f'realized_from_attribution = {agg.realized_pnl:.6f}')
print(f'diff = {realized_from_pnl - agg.realized_pnl:.2e}')
"
```

Expected: `diff` < 1e-6 USDC. (Definitionally true since `realized_pnl` is `trades.pnl`; this check guards against type-coercion bugs and dropped rows.)

### Step 3: Live DB — report the divergence, do not gate on it

If `live_trades.db` exists, the same check on live rows is expected to be tighter than 1e-6 because every live row also has `signal_reference_source='live'`. But the design's risk #2 notes that `luck_value` on live rows absorbs the algebra-vs-CLOB accounting residual; this affects per-component sums, not the realized sum. Report observed live numbers, do not assert.

```bash
[ -f live_trades.db ] && python -c "
import sqlite3
from polypocket.attribution import aggregate_attribution
with sqlite3.connect('live_trades.db') as c:
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        \"SELECT * FROM trades WHERE status='settled' AND pnl IS NOT NULL\"
    ).fetchall()]
print(aggregate_attribution(rows))
"
```

### Step 4: Verify the report regenerates cleanly

```bash
python scripts/pnl_attribution_report.py
```

Expected: writes `scripts/_pnl_attribution.md` without errors. `git status` shows the file as gitignored / not in working-tree diffs (post-Task 8 Step 2).

### Step 5: 7-day forward-soak check (deferred, runs naturally)

After 7 days of operation post-shipping, verify all newly settled trades have `signal_reference_source = "live"` and non-NULL `signal_reference_price`. Read `MAX(id)` baseline from `scripts/_backfill_signal_reference.md` (Task 5 Step 8 captured it).

```bash
# Replace N_PRE with the value from _backfill_signal_reference.md
sqlite3 paper_trades.db "SELECT COUNT(*) FROM trades WHERE status='settled' AND id > N_PRE AND signal_reference_source != 'live'"
```

Expected: `0`. If non-zero, investigate which path created a trade row without populating `signal_reference_price` (likely candidates: `reconcile_recovered_trade`, a new test seed, or a regression in `signal.py`).

### Step 6: Final state check

```bash
git status   # should be clean
git log --oneline | head -10
```

Confirm the commit chain shows the task commits in order: ledger, signal, attribution, executor+backfill (bundled), analyze, tui, report, reference, [optional cleanup].

---

## Definition of Done (from design doc, mirrored here)

- [x] `trades.signal_reference_price` + `trades.signal_reference_source` columns shipped (Task 1).
- [x] `polypocket/attribution.py` with full test coverage including aggregator (Task 3).
- [x] `signal.py` / `executor.py` / `ledger.py` plumbing committed (Tasks 2, 4+5).
- [x] Backfill script run; log committed with Task 0 baseline numbers (Task 5).
- [x] `analyze.py` Section 7 shipped with headline + context lines (Task 6).
- [x] `AttributionPanel` mounted in TUI with a unit-testable render function (Task 7).
- [x] Weekly report generator + sample artifact committed; subsequent regenerations gitignored (Task 8).
- [x] Hand-replay reference trades committed (Task 9).
- [x] Full test suite green; aggregate sanity verified on paper to `< 1e-6 USDC`; live divergence reported, not gated (Task 10).
- [ ] 7-day forward-soak shows 100% `"live"` provenance on new trades (Task 10 Step 5, deferred).
