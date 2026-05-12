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
from polypocket.ledger import init_db


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
    # Idempotent — adds the signal_reference_* columns if running against a
    # pre-existing DB that hasn't been opened by init_db since Task 1.
    init_db(db_path)
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
