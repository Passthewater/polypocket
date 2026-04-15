"""Backtest simulator: replay historical trades through configurable filters."""

import argparse
import sqlite3
import sys


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_trades(conn: sqlite3.Connection) -> list[dict]:
    """Fetch all settled trades joined with their decision snapshots."""
    rows = conn.execute("""
        SELECT
            t.id, t.side, t.entry_price, t.size, t.fees,
            t.model_p_up, t.market_p_up, t.edge, t.outcome, t.pnl,
            s.displacement, s.sigma_5min, s.t_remaining,
            s.timestamp as decision_time
        FROM trades t
        LEFT JOIN window_snapshots s
            ON t.window_slug = s.window_slug AND s.snapshot_type = 'decision'
        WHERE t.status = 'settled'
        ORDER BY t.timestamp
    """).fetchall()
    return [dict(r) for r in rows]


def _apply_filters(trades: list[dict], args: argparse.Namespace) -> list[dict]:
    """Filter trades based on CLI arguments."""
    filtered = []
    for t in trades:
        # Edge filter
        if t["edge"] is not None and abs(t["edge"]) < args.min_edge:
            continue

        # Model alignment filter
        if args.min_alignment > 0.50:
            if t["model_p_up"] is None:
                continue
            if t["side"] == "up" and t["model_p_up"] < args.min_alignment:
                continue
            if t["side"] == "down" and t["model_p_up"] > (1 - args.min_alignment):
                continue

        # Displacement/sigma filter
        if args.min_disp_sigma > 0 and t["displacement"] is not None and t["sigma_5min"] is not None:
            sigma = t["sigma_5min"]
            if sigma > 0:
                ratio = abs(t["displacement"]) / sigma
                if ratio < args.min_disp_sigma:
                    continue

        # Timing filters (from decision snapshot)
        if t["t_remaining"] is not None and t["t_remaining"] < args.min_remaining:
            continue

        filtered.append(t)
    return filtered


def _compute_stats(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0, "wins": 0, "winrate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0}
    wins = sum(1 for t in trades if t["pnl"] is not None and t["pnl"] > 0)
    total_pnl = sum(t["pnl"] for t in trades if t["pnl"] is not None)
    return {
        "trades": len(trades),
        "wins": wins,
        "winrate": 100.0 * wins / len(trades) if trades else 0.0,
        "total_pnl": total_pnl,
        "avg_pnl": total_pnl / len(trades) if trades else 0.0,
    }


def _bucket(value: float, breakpoints: list[tuple[float, str]], overflow_label: str) -> str:
    for threshold, label in breakpoints:
        if value < threshold:
            return label
    return overflow_label


def _print_header(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def _print_table(headers: list[str], rows: list[list], col_widths: list[int] | None = None) -> None:
    if col_widths is None:
        col_widths = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=4)) + 2 for i, h in enumerate(headers)]
    header_line = "".join(str(h).ljust(w) for h, w in zip(headers, col_widths))
    print(header_line)
    print("-" * sum(col_widths))
    for row in rows:
        print("".join(str(v).ljust(w) for v, w in zip(row, col_widths)))


def _breakdown_by(trades: list[dict], key_fn, label: str) -> None:
    buckets: dict[str, list[dict]] = {}
    for t in trades:
        k = key_fn(t)
        if k is None:
            continue
        buckets.setdefault(k, []).append(t)

    _print_header(f"Breakdown by {label}")
    headers = ["Bucket", "Trades", "Wins", "Winrate", "Avg PnL", "Total PnL"]
    rows = []
    for bucket_name in sorted(buckets.keys()):
        group = buckets[bucket_name]
        s = _compute_stats(group)
        rows.append([
            bucket_name,
            s["trades"],
            s["wins"],
            f'{s["winrate"]:.1f}%',
            f'${s["avg_pnl"]:.2f}',
            f'${s["total_pnl"]:.2f}',
        ])
    _print_table(headers, rows)


def run_backtest(args: argparse.Namespace) -> None:
    conn = _connect(args.db)
    all_trades = _fetch_trades(conn)
    conn.close()

    if not all_trades:
        print("No settled trades found.")
        return

    filtered = _apply_filters(all_trades, args)
    baseline = _compute_stats(all_trades)
    result = _compute_stats(filtered)
    cut = baseline["trades"] - result["trades"]

    # --- Summary ---
    _print_header("Baseline vs Filtered")
    headers = ["", "Trades", "Wins", "Winrate", "Avg PnL", "Total PnL"]
    rows = [
        [
            "Baseline",
            baseline["trades"],
            baseline["wins"],
            f'{baseline["winrate"]:.1f}%',
            f'${baseline["avg_pnl"]:.2f}',
            f'${baseline["total_pnl"]:.2f}',
        ],
        [
            "Filtered",
            result["trades"],
            result["wins"],
            f'{result["winrate"]:.1f}%',
            f'${result["avg_pnl"]:.2f}',
            f'${result["total_pnl"]:.2f}',
        ],
    ]
    _print_table(headers, rows)
    print(f"\nFilters applied: edge>={args.min_edge}, alignment>={args.min_alignment}, "
          f"disp/sigma>={args.min_disp_sigma}, remaining>={args.min_remaining}")
    print(f"Trades filtered out: {cut}")

    if not filtered:
        print("\nNo trades pass the filters.")
        return

    # --- Breakdowns ---
    _breakdown_by(filtered, lambda t: t["side"], "Side")

    _breakdown_by(
        filtered,
        lambda t: _bucket(abs(t["edge"] or 0), [
            (0.08, "< 8%"), (0.12, "8-12%"), (0.16, "12-16%"),
            (0.20, "16-20%"), (0.25, "20-25%"),
        ], "25%+"),
        "Edge",
    )

    _breakdown_by(
        filtered,
        lambda t: _bucket(t["model_p_up"], [
            (0.20, "strong down (<0.20)"),
            (0.35, "lean down (0.20-0.35)"),
            (0.45, "slight down (0.35-0.45)"),
            (0.55, "neutral (0.45-0.55)"),
            (0.65, "slight up (0.55-0.65)"),
            (0.80, "lean up (0.65-0.80)"),
        ], "strong up (0.80+)") if t["model_p_up"] is not None else None,
        "Model Confidence",
    )

    _breakdown_by(
        filtered,
        lambda t: _bucket(
            abs(t["displacement"]) / t["sigma_5min"] if t["sigma_5min"] and t["sigma_5min"] > 0 else 0,
            [(0.3, "< 0.3"), (0.5, "0.3-0.5"), (0.7, "0.5-0.7"), (1.0, "0.7-1.0")],
            "1.0+",
        ) if t["displacement"] is not None else None,
        "Displacement/Sigma",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backtest trading filters against historical paper trades",
    )
    parser.add_argument("--db", default="paper_trades.db", help="Path to trades database")
    parser.add_argument("--min-edge", type=float, default=0.03, help="Minimum edge threshold (default: 0.03)")
    parser.add_argument("--min-alignment", type=float, default=0.50,
                        help="Model confidence alignment filter (default: 0.50 = no filter, 0.60 = require strong alignment)")
    parser.add_argument("--min-disp-sigma", type=float, default=0.0,
                        help="Minimum |displacement|/sigma ratio (default: 0.0 = no filter)")
    parser.add_argument("--min-remaining", type=float, default=30.0,
                        help="Minimum seconds remaining in window (default: 30)")

    args = parser.parse_args()
    run_backtest(args)


if __name__ == "__main__":
    main()
