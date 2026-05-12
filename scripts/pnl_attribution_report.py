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

from polypocket.attribution import aggregate_attribution, attach_v2_cohort
from polypocket.config import LIVE_DB_PATH, PAPER_DB_PATH


def _load_settled(db_path: str) -> list[dict]:
    """Return settled trades joined with model_p_up_v2 from window_snapshots.

    Empty list if the DB does not exist. ORDER BY id (not timestamp) so the
    rows[-20:] / rows[-100:] slices match analyze.py and the TUI.
    """
    if not os.path.exists(db_path):
        return []
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM trades WHERE status='settled' ORDER BY id"
        ).fetchall()]
    return attach_v2_cohort(rows, db_path)


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
        f"approximate={agg_life.n_approximate}, missing={agg_life.n_missing}, "
        f"unattributable={agg_life.n_unattributable}\n"
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
