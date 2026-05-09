"""Export a labeled training corpus from the paper (and optionally live) ledgers to parquet.

Joins window_snapshots rows of snapshot_type='decision' to their corresponding
snapshot_type='close' row on window_slug, filters to rows with all core
features present and a non-null outcome, and writes one row per window.

Default cutoff is the G1 commit time (2026-04-24) — pre-G1 close rows had a
different population (only `trade_fired=1` windows emitted closes), so the v2
training corpus is paper post-G1 only.

Usage:
    python scripts/export_training_corpus.py --out corpus.parquet
    python scripts/export_training_corpus.py --paper-db paper_trades.db --live-db live_trades.db --out corpus.parquet
    python scripts/export_training_corpus.py --since 1970-01-01 --out corpus_full.parquet  # diagnostic only
"""
import argparse
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd


# Per #15 design: the ledger's stored `market_p_up` column is the raw up_ask,
# not a normalized probability. We normalize from up_ask / (up_ask + down_ask)
# in both training and inference. Hence `market_p_up` is NOT a required core
# feature here -- only up_ask / down_ask are.
CORE_FIELDS = (
    "displacement",
    "sigma_5min",
    "t_remaining",
    "up_ask",
    "down_ask",
)

# G1 commit (5f76bea) merged 2026-04-24; before that, close rows were only
# emitted on trade_fired=1 windows, so labels are non-representative.
# NB: SQLite stores `window_snapshots.timestamp` as 'YYYY-MM-DD HH:MM:SS'
# (space separator). The cutoff MUST use the same separator -- a 'T' would
# lexicographically exceed every space-separated row from the same date and
# silently drop a day of training data.
DEFAULT_SINCE = "2026-04-24 00:00:00"


@dataclass(frozen=True)
class Row:
    window_slug: str
    source: str  # "paper" | "live"
    decision_timestamp: str
    displacement: float
    sigma_5min: float
    t_remaining: float
    market_p_up_normalized: float
    up_ask: float
    down_ask: float
    model_p_up_v1_raw: float | None  # ledger's model_p_up; shrinkage varied -- DO NOT use as feature
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
    """Return labeled decision rows joined to their close row.

    `since_timestamp` (optional ISO-8601 string) filters to decisions on or
    after the cutoff -- used to restrict to post-G1 paper rows.
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
        if any(r[f] is None for f in CORE_FIELDS):
            continue
        if r["t_remaining"] <= 0:
            continue
        if r["outcome"] not in ("up", "down"):
            continue
        up_ask = float(r["up_ask"])
        down_ask = float(r["down_ask"])
        denom = up_ask + down_ask
        if denom <= 0:
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
                final_price=(
                    float(r["final_price"]) if r["final_price"] is not None else None
                ),
            )
        )
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--paper-db", default="paper_trades.db")
    p.add_argument(
        "--live-db",
        default=None,
        help="Live DB to include. Default: skip -- v2 trains on paper post-G1 only.",
    )
    p.add_argument("--out", default="corpus.parquet")
    p.add_argument(
        "--since",
        default=None,
        help=f"ISO-8601 cutoff. Default: {DEFAULT_SINCE} (G1 commit). "
        "Use '1970-01-01' to include all rows for diagnostic exports.",
    )
    args = p.parse_args()

    since = args.since or DEFAULT_SINCE

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
    print(f"  paper: {(df['source'] == 'paper').sum()}")
    print(f"  live:  {(df['source'] == 'live').sum()}")
    if len(df):
        print(f"  base rate (up): {df['outcome_int'].mean():.3f}")
        print(f"  rows with bids: {df['up_bids_json'].notna().sum()}")
        print(f"  date span: {df['decision_timestamp'].min()} -> {df['decision_timestamp'].max()}")


if __name__ == "__main__":
    main()
