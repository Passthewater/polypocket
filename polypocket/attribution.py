"""Pure PnL attribution: decompose realized PnL into edge / slip / expected_fee / luck.

Sum identity: edge_value + slip_value + expected_fee_value + luck_value == realized_pnl
to float precision, by construction (luck_value is defined as the residual).

realized_pnl is sourced from `trades.pnl` (authoritative for both paper and
live ledgers). The decomposition does NOT recompute realized_pnl from a
formula, because on live trades the algebraic formula diverges from
trades.pnl (see design doc §"Decomposition").

Fees are recomputed internally from realized size/entry_price via
`config.fee_shares` — `trades.fees` is the intended fee (logged at submit-time
from intended values, never refreshed). On live partial fills, intended !=
realized; recomputing here keeps every component scaled by the same trade.

Model-version (v1 vs v2) cohort assignment requires `model_p_up_v2` from
window_snapshots — use `attach_v2_cohort(rows, db_path)` before passing to
aggregate_attribution if you want the split. Without the join, all rows are
classified v1.

See docs/plans/2026-05-11-pnl-attribution-design.md for the full algebra.
"""
import sqlite3
from contextlib import closing
from dataclasses import dataclass

from polypocket.config import fee_shares


@dataclass(frozen=True)
class PnlAttribution:
    realized_pnl: float
    edge_value: float
    slip_value: float
    expected_fee_value: float
    luck_value: float
    # Auxiliary (reported alongside, not in the principal sum). `realized_fee_value`
    # is `-fees if won else 0` using the *recomputed* fee from realized
    # size/entry_price — accurate on paper; on live it's the "fee that would
    # have been paid at the realized fill price if Polymarket's fee schedule
    # matched our formula exactly." Algebra-vs-CLOB residual on live is
    # absorbed into luck_value (design risk #2).
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
    outcome: str,
    realized_pnl: float,
    signal_reference_source: str = "live",
    model_version_attributed: str = "v1",
) -> PnlAttribution:
    """Decompose realized PnL into the four principal components.

    Args:
      side: 'up' or 'down' — the side the trade bought.
      size: shares held after fill (realized, from trades.size post-update).
      entry_price: actual VWAP fill (realized, from trades.entry_price post-update).
      signal_reference_price: the price the gate compared model_p_up against.
        Side-aligned (i.e., the executable entry on the chosen side).
      model_p_up: P(BTC up) at decision; this function flips for DOWN side.
      outcome: 'up' or 'down' — the resolved outcome.
      realized_pnl: trades.pnl — authoritative realized PnL from the settle path.
      signal_reference_source: provenance tag for the reference price.
      model_version_attributed: 'v1' or 'v2' — which model drove this trade.

    Note: fees are intentionally NOT a parameter. They are recomputed from
    realized size/entry_price via config.fee_shares so the algebra is
    internally consistent across paper, live full fills, and live partial fills.
    """
    won = side == outcome
    model_p_for_side = _side_aligned_model_p(model_p_up, side)
    fees = fee_shares(size, entry_price)
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
    was active. If the v2 column is NULL or absent (pre-#15 dual-logging, or
    the caller skipped attach_v2_cohort) treat as v1.
    """
    v2 = row.get("model_p_up_v2")
    if v2 is None:
        return "v1"
    return "v2" if abs(row["model_p_up"] - v2) < 1e-9 else "v1"


def attribute_from_row(row: dict) -> PnlAttribution | None:
    """Adapter: takes a trades-table row dict; returns None if not attributable.

    Skips rows missing pnl, signal_reference_price, outcome, or any required field.
    Use this in aggregation loops; callers should filter Nones and count them.

    `fees` is NOT in the required set — it's recomputed inside attribute_pnl.
    """
    required = ("side", "size", "entry_price", "model_p_up",
                "outcome", "signal_reference_price", "pnl")
    for key in required:
        if row.get(key) is None:
            return None
    return attribute_pnl(
        side=row["side"], size=row["size"], entry_price=row["entry_price"],
        signal_reference_price=row["signal_reference_price"],
        model_p_up=row["model_p_up"],
        outcome=row["outcome"], realized_pnl=row["pnl"],
        signal_reference_source=row.get("signal_reference_source") or "unknown",
        model_version_attributed=_infer_model_version(row),
    )


def attach_v2_cohort(rows: list[dict], db_path: str) -> list[dict]:
    """Join model_p_up_v2 from window_snapshots into each row by window_slug.

    Pure helper for callers (analyze.py, render_attribution_text, report) that
    want the v1/v2 cohort split. `model_p_up_v2` lives on window_snapshots
    (#15 dual-logging), not trades — without this join, _infer_model_version
    classifies every row as v1.

    Rows are returned as new dicts (shallow-copied + augmented); the input
    list is not mutated. Rows whose window_slug has no decision snapshot get
    model_p_up_v2 = None.
    """
    slugs = [r["window_slug"] for r in rows if r.get("window_slug")]
    if not slugs:
        return [dict(r) for r in rows]
    placeholders = ",".join("?" * len(slugs))
    by_slug: dict[str, float | None] = {}
    with closing(sqlite3.connect(db_path)) as conn:
        for slug, v2 in conn.execute(
            f"SELECT window_slug, model_p_up_v2 FROM window_snapshots "
            f"WHERE snapshot_type='decision' AND window_slug IN ({placeholders})",
            slugs,
        ).fetchall():
            by_slug[slug] = v2
    out = []
    for r in rows:
        new = dict(r)
        new["model_p_up_v2"] = by_slug.get(r.get("window_slug"))
        out.append(new)
    return out


@dataclass(frozen=True)
class AggregateAttribution:
    # n_total = n_exact + n_approximate + n_missing + n_unattributable.
    # The first three are *attributable* rows (have non-null pnl / signal_ref);
    # n_unattributable counts rows dropped despite a valid provenance tag
    # (typically null pnl from settle_live_trade lookup failures).
    n_total: int
    n_exact: int
    n_approximate: int
    n_missing: int
    n_unattributable: int
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

    Provenance counts come from the SAME pass that builds the sums — a row
    tagged 'exact' but dropped because pnl IS NULL lands in n_unattributable,
    not n_exact, so the four count buckets sum to n_total.
    """
    n_total = len(rows)
    n_exact = n_approximate = n_missing = n_unattributable = 0
    n_v1 = n_v2 = 0
    realized_pnl = edge_sum = slip_sum = expected_fee_sum = luck_sum = 0.0
    realized_fee_sum = fee_luck_sum = 0.0

    for r in rows:
        src = r.get("signal_reference_source")
        # Classify provenance before attribution attempt; final bucket may
        # downgrade to n_unattributable if attribute_from_row returns None
        # for a reason other than missing reference (e.g., null pnl).
        if src in (None, "missing") or r.get("signal_reference_price") is None:
            provenance = "missing"
        elif src == "approximate":
            provenance = "approximate"
        else:  # 'exact' or 'live'
            provenance = "exact"

        if provenance == "approximate" and not include_approximate:
            n_approximate += 1
            continue

        a = attribute_from_row(r)
        if a is None:
            if provenance == "missing":
                n_missing += 1
            else:
                n_unattributable += 1
            continue

        # Successful attribution — count by provenance, accumulate sums.
        if provenance == "exact":
            n_exact += 1
        elif provenance == "approximate":
            n_approximate += 1
        else:  # provenance == "missing" but attribute_from_row returned non-None
            # (shouldn't happen — missing implies null reference — but defensive)
            n_missing += 1

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
        n_total=n_total, n_exact=n_exact, n_approximate=n_approximate,
        n_missing=n_missing, n_unattributable=n_unattributable,
        n_v1_attributed=n_v1, n_v2_attributed=n_v2,
        realized_pnl=realized_pnl, edge_sum=edge_sum, slip_sum=slip_sum,
        expected_fee_sum=expected_fee_sum, luck_sum=luck_sum,
        realized_fee_sum=realized_fee_sum, fee_luck_sum=fee_luck_sum,
    )
