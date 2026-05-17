"""Phase 2: ack-time book depth-support analysis.

Reads ``order_events`` rows where ``event_type='ack'`` and
``payload_json LIKE '%book_at_ack%'``, joins to ``trades`` for entry_price
and side, and computes total USDC of liquidity that could have filled the
BUY at its limit price, across both Polymarket NegRisk match paths.

Match paths for a BUY {side} at limit ``X``:
  (a) Direct: opposing-side asks at ``ask_price <= X``.  Per-share cost is
      the ask price ``q``; USDC = ``size * q``.
  (b) Pair-merge: same-side-mirror bids at ``bid_price >= 1 - X``.  A mirror
      bid at ``p`` pair-merges into an implied opposing ask at ``1 - p``;
      for our limit ``X`` to fill we need ``1 - p <= X`` <=> ``p >= 1 - X``.
      Per-share cost is ``(1 - p)``; USDC = ``size * (1 - p)``.

Concretely for a BUY UP at limit ``X``:
  (a) Direct: ``up_book["asks"]`` with ``price <= X``.
  (b) Pair-merge: ``down_book["bids"]`` with ``price >= 1 - X``.

BUY DOWN is the mirror (``down_book["asks"]`` direct, ``up_book["bids"]``
pair-merge).

Expected output against today's ``live_trades.db``: 0 ack events with
``book_at_ack`` populated (diagnostic landed commit ``a98e76c`` 2026-05-16
03:25 UTC, *after* the live cohort ended 2026-05-16 02:17 UTC).

Usage::

    python scripts/fak_ack_depth_retrospective.py [--db PATH]
        [--min-depth-usdc FLOAT] [--out PATH]

Exit code always 0 (no pass/fail bar on this phase — it's preparatory tooling).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from polypocket.config import LIVE_DB_PATH  # noqa: E402

DEFAULT_MIN_DEPTH_USDC = 0.0
DEFAULT_OUT = ROOT / "scripts" / "_fak_ack_depth_retrospective.md"

NO_DATA_MESSAGE = (
    "Diagnostic landed 2026-05-15 23:25 EDT, after last live trade in this DB. "
    "Rerun after next live cohort."
)


# ---------------------------------------------------------------------------
# Core per-fill depth computation (pure function — unit-testable)
# ---------------------------------------------------------------------------

def depth_usdc_at_or_below_limit(
    side: str,
    entry_price: float,
    book_at_ack: dict,
) -> float:
    """Total USDC of liquidity that can fill a BUY {side} IOC at limit ``entry_price``.

    Polymarket NegRisk markets match a buy two ways:

    (a) Direct: opposing-side asks at ``ask_price <= entry_price``.  Per-share
        cost is the ask price ``q``, so USDC contribution = ``size * q``.
    (b) Pair-merge: same-side-mirror bids at ``bid_price >= 1 - entry_price``.
        A mirror bid at ``p`` pair-merges into an implied opposing ask at
        ``1 - p``; for our limit ``X`` to fill we need ``1 - p <= X``
        which is ``p >= 1 - X``.  Per-share cost is ``(1 - p)``, so USDC
        contribution = ``size * (1 - p)``.

    For BUY UP at limit ``X``:
        (a) sum ``size * price`` over ``up_book["asks"]`` with ``price <= X``.
        (b) sum ``size * (1 - price)`` over ``down_book["bids"]`` with
            ``price >= 1 - X``.

    BUY DOWN is the mirror (``down_book["asks"]`` direct, ``up_book["bids"]``
    pair-merge).

    Args:
        side: ``"up"`` or ``"down"`` — the trade side (BUY UP / BUY DOWN).
        entry_price: The FAK limit price baked into the live order.
        book_at_ack: Dict with keys ``"up_book"`` and ``"down_book"``, each a
            dict with keys ``"bids"`` and ``"asks"`` containing lists of
            ``{"price": str_or_float, "size": str_or_float}`` dicts (top-N
            levels, best-first).  This is the shape emitted by
            ``executor._book_top_n``.

    Returns:
        Total fillable USDC across both match paths.
    """
    if side == "up":
        direct_asks = (book_at_ack.get("up_book") or {}).get("asks") or []
        merge_bids = (book_at_ack.get("down_book") or {}).get("bids") or []
    else:  # side == "down"
        direct_asks = (book_at_ack.get("down_book") or {}).get("asks") or []
        merge_bids = (book_at_ack.get("up_book") or {}).get("bids") or []

    total = 0.0
    # (a) direct match against opposing asks at ask_price <= entry_price
    for level in direct_asks:
        try:
            price = float(level["price"])
            size = float(level["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if price <= entry_price + 1e-9:
            total += size * price
    # (b) pair-merge against mirror bids at bid_price >= 1 - entry_price
    threshold = 1.0 - entry_price
    for level in merge_bids:
        try:
            price = float(level["price"])
            size = float(level["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if price >= threshold - 1e-9:
            total += size * (1.0 - price)
    return total


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_ack_events(db_path: str) -> list[dict]:
    """Load ack events that include ``book_at_ack`` payloads, joined to trades."""
    sql = """
        SELECT oe.id AS event_id, oe.trade_id, oe.window_slug, oe.payload_json,
               t.side, t.entry_price, t.outcome
        FROM order_events oe
        LEFT JOIN trades t ON t.id = oe.trade_id
        WHERE oe.event_type = 'ack'
          AND oe.payload_json LIKE '%book_at_ack%'
    """
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql).fetchall()

    out: list[dict] = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"])
        except (TypeError, ValueError):
            continue
        book_at_ack = payload.get("book_at_ack")
        if not book_at_ack:
            continue
        out.append(
            {
                "event_id": r["event_id"],
                "trade_id": r["trade_id"],
                "window_slug": r["window_slug"],
                "side": r["side"],
                "entry_price": r["entry_price"],
                "outcome": r["outcome"],
                "book_at_ack": book_at_ack,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def build_report(
    events: list[dict],
    min_depth_usdc: float,
    db_path: str,
    total_ack_events: int,
) -> str:
    lines: list[str] = []
    lines.append("# FAK ack-time depth-support retrospective")
    lines.append("")
    lines.append(f"**DB:** `{db_path}`")
    lines.append(f"**MIN_DEPTH_USDC:** `{min_depth_usdc}`")
    lines.append(f"**Generated:** 2026-05-17")
    lines.append("")

    n_book = len(events)
    lines.append(
        f"**Ack events with `book_at_ack`:** {n_book} / {total_ack_events}"
    )
    lines.append("")

    if n_book == 0:
        lines.append(f"> {NO_DATA_MESSAGE}")
        lines.append("")
        return "\n".join(lines) + "\n"

    # Per-fill table
    rows_data: list[dict] = []
    for ev in events:
        ep = ev["entry_price"] or 0.0
        depth = depth_usdc_at_or_below_limit(ev["side"], ep, ev["book_at_ack"])
        supported = depth >= min_depth_usdc
        rows_data.append(
            {
                "fill_id": ev["trade_id"],
                "side": ev["side"],
                "limit_price": ep,
                "depth_usdc": depth,
                "supported": supported,
                "outcome": ev["outcome"],
            }
        )

    lines.append("## Per-fill depth")
    lines.append("")
    lines.append(
        "| fill_id | side | limit_price | depth_usdc_at_or_below_limit"
        " | depth_supported | outcome |"
    )
    lines.append("|---|---|---:|---:|---|---|")
    for rd in rows_data:
        lines.append(
            f"| {rd['fill_id']} | {rd['side']} | {rd['limit_price']:.4f}"
            f" | {rd['depth_usdc']:.2f} | {rd['supported']} | {rd['outcome']} |"
        )
    lines.append("")

    # Summary
    n_sup = sum(1 for rd in rows_data if rd["supported"])
    lines.append("## Summary")
    lines.append("")
    lines.append(f"| threshold | depth-supported | total |")
    lines.append("|---|---:|---:|")
    lines.append(f"| ≥ $0 (binary) | {n_sup} | {n_book} |")
    n_10 = sum(1 for rd in rows_data if rd["depth_usdc"] >= 10.0)
    lines.append(f"| ≥ $10 | {n_10} | {n_book} |")
    lines.append("")

    # Side split
    lines.append("## By side")
    lines.append("")
    lines.append("| side | n | depth-supported (≥$0) |")
    lines.append("|---|---:|---:|")
    for side in ("up", "down"):
        sub = [rd for rd in rows_data if rd["side"] == side]
        n_s = len(sub)
        n_s_sup = sum(1 for rd in sub if rd["supported"])
        lines.append(f"| {side} | {n_s} | {n_s_sup} |")
    lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--db",
        default=str(ROOT / LIVE_DB_PATH),
        help="Path to live_trades.db",
    )
    p.add_argument(
        "--min-depth-usdc",
        type=float,
        default=DEFAULT_MIN_DEPTH_USDC,
        dest="min_depth_usdc",
        help="Minimum USDC depth to count as 'depth-supported' (default 0.0)",
    )
    p.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help="Output markdown path",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Count total ack events for the report header
    with closing(sqlite3.connect(args.db)) as conn:
        total_ack = conn.execute(
            "SELECT COUNT(*) FROM order_events WHERE event_type='ack'"
        ).fetchone()[0]

    events = load_ack_events(args.db)
    report = build_report(events, args.min_depth_usdc, args.db, total_ack)

    out_path = Path(args.out)
    out_path.write_text(report, encoding="utf-8")
    print(f"wrote {out_path}")

    if len(events) == 0:
        print(f"0/{total_ack} ack events have book_at_ack — {NO_DATA_MESSAGE}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
