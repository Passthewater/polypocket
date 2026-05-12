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
    attach_v2_cohort,
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
    """edge + slip + expected_fee + luck must equal realized_pnl to 1e-9.

    Note: attribute_pnl recomputes fees internally from realized size/entry_price.
    The test supplies realized_pnl using the same formula so the identity is exact.
    """
    from polypocket.config import fee_shares

    size = 50.0
    model_p_up = 0.68
    fees = fee_shares(size, entry_price)
    outcome = side if won else ("down" if side == "up" else "up")
    realized_pnl = ((size - fees) - entry_price * size) if won else (-entry_price * size)

    attr = attribute_pnl(
        side=side, size=size, entry_price=entry_price,
        signal_reference_price=signal_ref, model_p_up=model_p_up,
        outcome=outcome, realized_pnl=realized_pnl,
    )
    total = attr.edge_value + attr.slip_value + attr.expected_fee_value + attr.luck_value
    assert abs(total - realized_pnl) < 1e-9, f"diff={total - realized_pnl:.2e}"
    assert attr.realized_pnl == pytest.approx(realized_pnl, abs=1e-9)


def test_signs_are_intuitive_up_win():
    """UP win, model=0.80, gate-ref=0.60, fill=0.62: most PnL -> edge; small negative slip;
    small negative expected_fee; positive luck (won at p=0.80, residual = (size-fees)*(1-0.80))."""
    from polypocket.config import fee_shares

    size = 100.0
    entry_price = 0.62
    fees = fee_shares(size, entry_price)
    realized_pnl = (size - fees) - entry_price * size
    attr = attribute_pnl(
        side="up", size=size, entry_price=entry_price,
        signal_reference_price=0.60, model_p_up=0.80,
        outcome="up", realized_pnl=realized_pnl,
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
            outcome=outcome, realized_pnl=realized_pnl,
        )
        total = attr.edge_value + attr.slip_value + attr.expected_fee_value + attr.luck_value
        assert abs(total - realized_pnl) < 1e-9


# --- DB-row adapter -------------------------------------------------

def test_attribute_from_row_handles_missing_signal_reference():
    """When signal_reference_price is NULL, attribute_from_row returns None
    (caller must filter; aggregates skip these)."""
    row = {
        "side": "up", "size": 50.0, "entry_price": 0.60,
        "model_p_up": 0.70, "outcome": "up", "pnl": 19.975,
        "signal_reference_price": None, "signal_reference_source": "missing",
    }
    assert attribute_from_row(row) is None


def test_attribute_from_row_returns_pnl_attribution_when_complete():
    """Complete row produces a PnlAttribution using trades.pnl as realized_pnl.

    fees are recomputed from realized size/entry_price — the row's `fees` field
    is not read (it's the intended fee, see design §"Math").
    """
    row = {
        "side": "up", "size": 100.0, "entry_price": 0.62,
        "model_p_up": 0.80, "outcome": "up", "pnl": 37.953,
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
        "model_p_up": 0.80, "outcome": "up", "pnl": None,
        "signal_reference_price": 0.60, "signal_reference_source": "exact",
    }
    assert attribute_from_row(row) is None


# --- Aggregator -----------------------------------------------------

def _make_row(slug, source="exact", pnl=1.0, model_p_up=0.70,
              model_p_up_v2=None, entry_price=0.60, signal_ref=0.55):
    return {
        "window_slug": slug, "side": "up", "size": 10.0, "entry_price": entry_price,
        "model_p_up": model_p_up, "outcome": "up", "pnl": pnl,
        "signal_reference_price": signal_ref, "signal_reference_source": source,
        "model_p_up_v2": model_p_up_v2,
    }


def test_aggregate_counts_provenance_from_attributable_rows_only():
    """n_exact/n_approximate/n_missing count only rows that produced an
    attribution. Rows dropped for null pnl land in n_unattributable instead."""
    rows = [
        _make_row("w1", "exact"),
        _make_row("w2", "exact"),
        _make_row("w3", "approximate"),
        _make_row("w4", "missing"),
        _make_row("w5", None),  # NULL source treated as missing
        _make_row("w6", "exact", pnl=None),  # null pnl: unattributable
    ]
    rows[3]["signal_reference_price"] = None  # missing rows have null reference
    rows[4]["signal_reference_price"] = None
    agg = aggregate_attribution(rows, include_approximate=True)
    assert agg.n_total == 6
    assert agg.n_exact == 2          # w1, w2 (w6 dropped to n_unattributable)
    assert agg.n_approximate == 1    # w3
    assert agg.n_missing == 2        # w4, w5
    assert agg.n_unattributable == 1 # w6
    # Sanity: counted buckets account for every row.
    assert (agg.n_exact + agg.n_approximate + agg.n_missing
            + agg.n_unattributable) == agg.n_total


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


def test_aggregate_infers_model_version_from_joined_v2_column():
    """A row is v2-cohort iff model_p_up == model_p_up_v2 (the v2 value fired).

    model_p_up_v2 lives on window_snapshots, not trades — callers must use
    attach_v2_cohort() before passing rows to aggregate_attribution. This test
    pre-joins the column inline; integration coverage of attach_v2_cohort lives
    in test_attach_v2_cohort_joins_decision_snapshot below.
    """
    rows = [
        _make_row("w1", "exact", pnl=1.0, model_p_up=0.70, model_p_up_v2=0.62),  # v1
        _make_row("w2", "exact", pnl=2.0, model_p_up=0.62, model_p_up_v2=0.62),  # v2
    ]
    agg = aggregate_attribution(rows)
    assert agg.n_v1_attributed == 1
    assert agg.n_v2_attributed == 1


def test_aggregate_treats_missing_model_p_up_v2_as_v1():
    """A row whose model_p_up_v2 key is absent (pre-#15 dual-log, or no join
    performed) is classified v1, not crashed."""
    rows = [
        _make_row("w1", "exact", pnl=1.0, model_p_up=0.70, model_p_up_v2=None),
    ]
    rows[0].pop("model_p_up_v2")  # key entirely absent
    agg = aggregate_attribution(rows)
    assert agg.n_v1_attributed == 1
    assert agg.n_v2_attributed == 0


def test_attach_v2_cohort_joins_decision_snapshot(tmp_path):
    """attach_v2_cohort fetches model_p_up_v2 from window_snapshots and merges
    it into each row dict. Rows without a decision snapshot get None."""
    from polypocket.ledger import init_db, log_trade, log_snapshot
    from polypocket.attribution import attach_v2_cohort

    db = str(tmp_path / "j.db")
    init_db(db)
    log_trade(db_path=db, window_slug="w1", side="up", entry_price=0.6, size=10.0,
              fees=0.024, model_p_up=0.7, market_p_up=0.58, edge=0.12,
              outcome="up", pnl=3.976, status="settled",
              signal_reference_price=0.58, signal_reference_source="live")
    log_snapshot(db, "w1", "decision", {
        "btc_price": 65000, "window_open_price": 64900,
        "displacement": 0.0015, "sigma_5min": 0.002, "model_p_up": 0.70,
        "t_remaining": 200, "up_ask": 0.58, "down_ask": 0.42,
        "market_p_up": 0.58, "edge": 0.12, "preview_side": "up",
        "quote_status": "ok",
        "model_p_up_v1_calibrated": 0.70, "model_p_up_v2": 0.62,
    })
    # Second trade lacks a decision snapshot.
    log_trade(db_path=db, window_slug="w2", side="up", entry_price=0.6, size=10.0,
              fees=0.024, model_p_up=0.7, market_p_up=0.58, edge=0.12,
              outcome="up", pnl=3.976, status="settled",
              signal_reference_price=0.58, signal_reference_source="live")

    rows = [
        {"window_slug": "w1", "model_p_up": 0.70},
        {"window_slug": "w2", "model_p_up": 0.70},
    ]
    joined = attach_v2_cohort(rows, db)
    assert joined[0]["model_p_up_v2"] == pytest.approx(0.62, abs=1e-9)
    assert joined[1]["model_p_up_v2"] is None


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
