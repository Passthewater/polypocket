"""Snapshot analysis: data quality, model calibration, and finetuning insights."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from polypocket.config import (
    BOOK_MAX_TOTAL_ASK,
    EDGE_FLOOR,
    EDGE_RANGE,
    FEE_RATE,
    MAX_POSITION_USDC,
    MIN_EDGE_THRESHOLD,
    MIN_POSITION_USDC,
    PAPER_DB_PATH,
    VOL_FLOOR,
    VOL_RANGE,
    VOLATILITY_LOOKBACK,
    WINDOW_ENTRY_MIN_ELAPSED,
    WINDOW_ENTRY_MIN_REMAINING,
)


def _fetch_all(db_path: str, query: str, params: tuple = ()) -> list[dict]:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def _fetch_one(db_path: str, query: str, params: tuple = ()) -> dict | None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(query, params).fetchone()
        return dict(row) if row else None


def generate_report(db_path: str = PAPER_DB_PATH) -> str:
    lines: list[str] = []

    def h1(text: str) -> None:
        lines.append(f"# {text}\n")

    def h2(text: str) -> None:
        lines.append(f"## {text}\n")

    def h3(text: str) -> None:
        lines.append(f"### {text}\n")

    def p(text: str) -> None:
        lines.append(f"{text}\n")

    def table(headers: list[str], rows: list[list]) -> None:
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("| " + " | ".join("---" for _ in headers) + " |")
        for row in rows:
            lines.append("| " + " | ".join(str(c) for c in row) + " |")
        lines.append("")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    h1(f"Polypocket Analysis Report -- {now}")

    # ── Trades ──
    trades = _fetch_all(db_path, "SELECT * FROM trades ORDER BY timestamp")
    settled = [t for t in trades if t["status"] == "settled"]
    open_trades = [t for t in trades if t["status"] in ("open", "reserved")]

    # ── Snapshots ──
    snapshots = _fetch_all(db_path, "SELECT * FROM window_snapshots ORDER BY timestamp")
    snap_by_window: dict[str, dict[str, dict]] = {}
    for s in snapshots:
        snap_by_window.setdefault(s["window_slug"], {})[s["snapshot_type"]] = s

    # ================================================================
    h2("1. Data Quality")
    # ================================================================

    total_windows = len(snap_by_window)
    full_coverage = sum(
        1 for types in snap_by_window.values()
        if set(types.keys()) == {"open", "decision", "close"}
    )
    has_open = sum(1 for types in snap_by_window.values() if "open" in types)
    has_decision = sum(1 for types in snap_by_window.values() if "decision" in types)
    has_close = sum(1 for types in snap_by_window.values() if "close" in types)

    p(f"**Windows observed:** {total_windows}")
    p(f"**Full 3-snapshot coverage:** {full_coverage} / {total_windows} ({100*full_coverage/max(total_windows,1):.0f}%)")

    table(
        ["Snapshot Type", "Count", "Coverage"],
        [
            ["open", has_open, f"{100*has_open/max(total_windows,1):.0f}%"],
            ["decision", has_decision, f"{100*has_decision/max(total_windows,1):.0f}%"],
            ["close", has_close, f"{100*has_close/max(total_windows,1):.0f}%"],
        ],
    )

    # Trade/snapshot join
    traded_slugs = {t["window_slug"] for t in trades}
    traded_with_decision = sum(
        1 for slug in traded_slugs
        if slug in snap_by_window and "decision" in snap_by_window[slug]
    )
    p(f"**Trades with matching decision snapshot:** {traded_with_decision} / {len(traded_slugs)}")

    # Null field audit on decision snapshots
    decision_snaps = [s for s in snapshots if s["snapshot_type"] == "decision"]
    if decision_snaps:
        audit_fields = [
            "btc_price", "window_open_price", "displacement", "sigma_5min",
            "model_p_up", "t_remaining", "up_ask", "down_ask", "edge",
        ]
        audit_rows = []
        for field in audit_fields:
            null_count = sum(1 for s in decision_snaps if s[field] is None)
            if null_count > 0:
                audit_rows.append([field, null_count, f"{100*null_count/len(decision_snaps):.0f}%"])
        if audit_rows:
            h3("Null Field Audit (decision snapshots)")
            table(["Field", "Null Count", "% Null"], audit_rows)
        else:
            p("**Null field audit:** All key fields populated in decision snapshots.")

    # ================================================================
    h2("2. Model Calibration")
    # ================================================================

    # P(Up) calibration: bucket by model_p_up, compare to actual outcome
    close_with_outcome = [
        s for s in snapshots
        if s["snapshot_type"] == "decision" and s.get("model_p_up") is not None
    ]
    # Join outcome from close snapshots
    calibration_data = []
    for s in close_with_outcome:
        slug = s["window_slug"]
        close_snap = snap_by_window.get(slug, {}).get("close")
        if close_snap and close_snap.get("outcome"):
            calibration_data.append({
                "model_p_up": s["model_p_up"],
                "actual_up": 1 if close_snap["outcome"] == "up" else 0,
                "edge": s.get("edge"),
                "trade_fired": s.get("trade_fired"),
                "preview_side": s.get("preview_side"),
            })

    if calibration_data:
        # Bucket by 10% bands
        buckets: dict[str, list] = {}
        for d in calibration_data:
            bucket = int(d["model_p_up"] * 10) / 10  # floor to nearest 0.1
            bucket_label = f"{bucket:.0%}-{bucket+0.1:.0%}"
            buckets.setdefault(bucket_label, []).append(d)

        cal_rows = []
        total_abs_error = 0.0
        total_cal_n = 0
        for label in sorted(buckets.keys()):
            items = buckets[label]
            n = len(items)
            predicted = sum(d["model_p_up"] for d in items) / n
            actual = sum(d["actual_up"] for d in items) / n
            error = actual - predicted
            total_abs_error += abs(error) * n
            total_cal_n += n
            cal_rows.append([
                label, n, f"{predicted:.1%}", f"{actual:.1%}",
                f"{error:+.1%}",
            ])

        table(
            ["P(Up) Bucket", "N", "Predicted", "Actual", "Error"],
            cal_rows,
        )

        avg_abs_error = total_abs_error / max(total_cal_n, 1)
        p(f"**Mean absolute calibration error:** {avg_abs_error:.1%}")
    else:
        p("*Not enough data with outcomes for calibration analysis.*")

    # Edge vs win rate
    traded_cal = [d for d in calibration_data if d["trade_fired"] == 1]
    if traded_cal:
        h3("Edge vs Win Rate (traded windows only)")
        edge_buckets: dict[str, list] = {}
        for d in traded_cal:
            if d["edge"] is None:
                continue
            bucket = round(d["edge"] * 50) / 50  # 2% bands
            label = f"{bucket:+.0%}"
            edge_buckets.setdefault(label, []).append(d)

        edge_rows = []
        for label in sorted(edge_buckets.keys()):
            items = edge_buckets[label]
            n = len(items)
            wins = sum(1 for d in items if
                       (d["preview_side"] == "up" and d["actual_up"] == 1) or
                       (d["preview_side"] == "down" and d["actual_up"] == 0))
            edge_rows.append([label, n, f"{wins/n:.0%}", f"${sum(1 for _ in items)}"])

        table(["Edge Bucket", "N", "Win Rate", "Trades"], edge_rows)

    # ================================================================
    h2("3. Trade Selection")
    # ================================================================

    # Skip reason distribution
    skipped = [s for s in decision_snaps if s["trade_fired"] == 0]
    fired = [s for s in decision_snaps if s["trade_fired"] == 1]
    p(f"**Windows traded:** {len(fired)}")
    p(f"**Windows skipped:** {len(skipped)}")

    if skipped:
        skip_reasons: dict[str, int] = {}
        for s in skipped:
            reason = s.get("skip_reason") or "unknown"
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

        skip_rows = sorted(skip_reasons.items(), key=lambda x: -x[1])
        table(
            ["Skip Reason", "Count", "% of Skips"],
            [[r, n, f"{100*n/len(skipped):.0f}%"] for r, n in skip_rows],
        )

    # Missed opportunities
    missed = []
    for s in skipped:
        slug = s["window_slug"]
        close_snap = snap_by_window.get(slug, {}).get("close")
        if close_snap and close_snap.get("outcome") and s.get("preview_side"):
            would_have_won = s["preview_side"] == close_snap["outcome"]
            entry_price = s.get("up_ask") if s["preview_side"] == "up" else s.get("down_ask")
            if entry_price and entry_price > 0:
                edge = s.get("edge") or 0.0
                sigma = s.get("sigma_5min") or 0.0
                edge_scale = min(max((edge - EDGE_FLOOR) / EDGE_RANGE, 0.0), 1.0)
                vol_scale = min(max((sigma - VOL_FLOOR) / VOL_RANGE, 0.0), 1.0)
                size_usdc = MIN_POSITION_USDC + (edge_scale * vol_scale) * (MAX_POSITION_USDC - MIN_POSITION_USDC)
                size = size_usdc / entry_price
                fees = entry_price * size * FEE_RATE
                cost = entry_price * size
                payout = size if would_have_won else 0.0
                hypothetical_pnl = payout - cost - fees
                missed.append({
                    "slug": slug,
                    "side": s["preview_side"],
                    "edge": s.get("edge"),
                    "entry_price": entry_price,
                    "outcome": close_snap["outcome"],
                    "won": would_have_won,
                    "pnl": hypothetical_pnl,
                    "skip_reason": s.get("skip_reason"),
                })

    if missed:
        h3("Missed Opportunities (skipped windows with known outcome)")
        wins = [m for m in missed if m["won"]]
        losses = [m for m in missed if not m["won"]]
        total_missed_pnl = sum(m["pnl"] for m in missed)
        won_pnl = sum(m["pnl"] for m in wins)

        p(f"**Would have won:** {len(wins)} / {len(missed)} ({100*len(wins)/max(len(missed),1):.0f}%)")
        p(f"**Estimated missed PnL (all):** ${total_missed_pnl:+.2f}")
        p(f"**Estimated missed PnL (wins only):** ${won_pnl:+.2f}")

        table(
            ["Window", "Side", "Edge", "Entry", "Outcome", "Hyp. PnL", "Skip Reason"],
            [
                [
                    m["slug"].replace("btc-updown-5m-", ""),
                    m["side"],
                    f"{m['edge']:.1%}" if m["edge"] else "?",
                    f"${m['entry_price']:.3f}",
                    m["outcome"],
                    f"${m['pnl']:+.2f}",
                    m["skip_reason"],
                ]
                for m in missed
            ],
        )

    # ================================================================
    h2("4. Execution Analysis")
    # ================================================================

    # Entry timing
    traded_decisions = [s for s in decision_snaps if s["trade_fired"] == 1 and s.get("t_remaining") is not None]
    if traded_decisions:
        h3("Entry Timing")
        avg_t_remaining = sum(s["t_remaining"] for s in traded_decisions) / len(traded_decisions)
        p(f"**Average t_remaining at entry:** {avg_t_remaining:.0f}s")

        # Timing vs outcome
        timing_win = []
        timing_lose = []
        for s in traded_decisions:
            slug = s["window_slug"]
            close_snap = snap_by_window.get(slug, {}).get("close")
            if close_snap and close_snap.get("outcome") and s.get("preview_side"):
                won = s["preview_side"] == close_snap["outcome"]
                if won:
                    timing_win.append(s["t_remaining"])
                else:
                    timing_lose.append(s["t_remaining"])

        if timing_win and timing_lose:
            p(f"**Avg t_remaining (wins):** {sum(timing_win)/len(timing_win):.0f}s")
            p(f"**Avg t_remaining (losses):** {sum(timing_lose)/len(timing_lose):.0f}s")

    # Side bias
    if traded_decisions:
        h3("Side Bias")
        up_trades = [s for s in traded_decisions if s.get("preview_side") == "up"]
        down_trades = [s for s in traded_decisions if s.get("preview_side") == "down"]
        p(f"**UP trades:** {len(up_trades)} | **DOWN trades:** {len(down_trades)}")

        for side_name, side_list in [("UP", up_trades), ("DOWN", down_trades)]:
            if side_list:
                wins = sum(
                    1 for s in side_list
                    if snap_by_window.get(s["window_slug"], {}).get("close", {}).get("outcome") == s.get("preview_side")
                )
                p(f"**{side_name} win rate:** {wins}/{len(side_list)} ({100*wins/len(side_list):.0f}%)")

    # Book depth
    traded_with_books = [
        s for s in traded_decisions
        if s.get("up_book_json") and s.get("down_book_json")
    ]
    if traded_with_books:
        h3("Book Depth at Entry")
        total_liq = []
        for s in traded_with_books:
            try:
                up_book = json.loads(s["up_book_json"])
                down_book = json.loads(s["down_book_json"])
                up_size = sum(l["size"] for l in up_book) if up_book else 0
                down_size = sum(l["size"] for l in down_book) if down_book else 0
                total_liq.append(up_size + down_size)
            except (json.JSONDecodeError, TypeError):
                pass
        if total_liq:
            p(f"**Avg total book depth (top 3 levels):** {sum(total_liq)/len(total_liq):.0f} shares")

    # ================================================================
    h2("5. Summary Stats")
    # ================================================================

    if settled:
        total_pnl = sum(t["pnl"] for t in settled if t["pnl"] is not None)
        wins = sum(1 for t in settled if t["pnl"] is not None and t["pnl"] > 0)
        losses = sum(1 for t in settled if t["pnl"] is not None and t["pnl"] <= 0)
        avg_win = 0.0
        avg_loss = 0.0
        if wins:
            avg_win = sum(t["pnl"] for t in settled if t["pnl"] and t["pnl"] > 0) / wins
        if losses:
            avg_loss = sum(t["pnl"] for t in settled if t["pnl"] is not None and t["pnl"] <= 0) / losses

        table(
            ["Metric", "Value"],
            [
                ["Total trades", len(trades)],
                ["Settled", len(settled)],
                ["Open", len(open_trades)],
                ["Wins", wins],
                ["Losses", losses],
                ["Win rate", f"{100*wins/max(wins+losses,1):.0f}%"],
                ["Total PnL", f"${total_pnl:+.2f}"],
                ["Avg win", f"${avg_win:+.2f}"],
                ["Avg loss", f"${avg_loss:.2f}"],
                ["Windows observed", total_windows],
                ["Trade rate", f"{100*len(fired)/max(total_windows,1):.0f}%"],
            ],
        )

    # ================================================================
    h2("6. Current Config")
    # ================================================================

    table(
        ["Parameter", "Value"],
        [
            ["MIN_EDGE_THRESHOLD", f"{MIN_EDGE_THRESHOLD:.1%}"],
            ["FEE_RATE", f"{FEE_RATE:.1%}"],
            ["POSITION_SIZE", f"${MIN_POSITION_USDC:.0f}-${MAX_POSITION_USDC:.0f} (edge x vol)"],
            ["VOLATILITY_LOOKBACK", f"{VOLATILITY_LOOKBACK} windows"],
            ["WINDOW_ENTRY_MIN_ELAPSED", f"{WINDOW_ENTRY_MIN_ELAPSED}s"],
            ["WINDOW_ENTRY_MIN_REMAINING", f"{WINDOW_ENTRY_MIN_REMAINING}s"],
            ["BOOK_MAX_TOTAL_ASK", f"{BOOK_MAX_TOTAL_ASK}"],
        ],
    )

    # ================================================================
    h2("7. Raw Snapshot Data")
    # ================================================================

    if decision_snaps:
        h3("Decision Snapshots")
        raw_headers = [
            "window", "fired", "side", "edge", "model_p_up", "mkt_p_up",
            "btc", "disp", "sigma", "t_rem", "skip",
        ]
        raw_rows = []
        for s in decision_snaps:
            slug_short = s["window_slug"].replace("btc-updown-5m-", "")
            raw_rows.append([
                slug_short,
                "Y" if s["trade_fired"] == 1 else "N",
                s.get("preview_side") or "-",
                f"{s['edge']:.1%}" if s.get("edge") is not None else "-",
                f"{s['model_p_up']:.1%}" if s.get("model_p_up") is not None else "-",
                f"{s['market_p_up']:.1%}" if s.get("market_p_up") is not None else "-",
                f"{s['btc_price']:.2f}" if s.get("btc_price") else "-",
                f"{s['displacement']:.4%}" if s.get("displacement") is not None else "-",
                f"{s['sigma_5min']:.4f}" if s.get("sigma_5min") is not None else "-",
                f"{s['t_remaining']:.0f}" if s.get("t_remaining") is not None else "-",
                s.get("skip_reason") or "-",
            ])
        table(raw_headers, raw_rows)

    close_snaps = [s for s in snapshots if s["snapshot_type"] == "close"]
    if close_snaps:
        h3("Close Snapshots (outcomes)")
        close_headers = ["window", "outcome", "final_price", "btc_price"]
        close_rows = []
        for s in close_snaps:
            slug_short = s["window_slug"].replace("btc-updown-5m-", "")
            close_rows.append([
                slug_short,
                s.get("outcome") or "-",
                f"{s['final_price']:.2f}" if s.get("final_price") else "-",
                f"{s['btc_price']:.2f}" if s.get("btc_price") else "-",
            ])
        table(close_headers, close_rows)

    # ================================================================
    h2("8. Claude Analysis Prompt")
    # ================================================================

    p("Copy this entire report and paste it into Claude with the following prompt:\n")
    p("```")
    p("You are analyzing trading data from a Polymarket BTC 5-minute window bot.")
    p("The bot bets on whether BTC will close a 5-minute window above or below")
    p("a Chainlink-reported opening price. It uses a probability model based on")
    p("displacement (current price vs open), realized volatility, and time remaining")
    p("to estimate P(Up), then compares against market odds to find edge.")
    p("")
    p("The data below contains:")
    p("- Model calibration analysis (predicted vs actual P(Up))")
    p("- Trade selection stats (what was traded vs skipped and why)")
    p("- Missed opportunity analysis (skipped windows that would have been profitable)")
    p("- Execution timing and side bias analysis")
    p("- Raw snapshot data for every window observed")
    p("- Current bot configuration parameters")
    p("")
    p("Your job:")
    p("1. DIAGNOSE: Where is the probability model miscalibrated? Are there systematic")
    p("   biases (e.g., overconfident at extremes, underconfident near 50%)? Does")
    p("   calibration differ by volatility regime or time-of-day?")
    p("2. TRADE SELECTION: Is the edge threshold too conservative or too aggressive?")
    p("   Are we leaving money on the table with skipped windows? Are there patterns")
    p("   in the missed opportunities?")
    p("3. EXECUTION: Does entry timing matter? Is there a side bias? Does book depth")
    p("   correlate with outcomes?")
    p("4. RECOMMEND: For each finding, recommend a specific parameter change or model")
    p("   adjustment. Estimate how many past trades it would have affected and the")
    p("   PnL delta. Be quantitative -- cite row counts, percentages, and dollar amounts.")
    p("5. PRIORITIZE: Rank recommendations by estimated PnL impact.")
    p("")
    p("Be skeptical of small sample sizes. Flag when N is too low for confidence.")
    p("```")

    return "\n".join(lines)


def calibration_report(*db_paths: str) -> str:
    """Trade-based calibration: bins settled trades by model_p_up decile.

    Pass multiple DB paths to merge data (e.g., current + backup).
    Defaults to PAPER_DB_PATH if no paths given.
    """
    if not db_paths:
        db_paths = (PAPER_DB_PATH,)

    all_trades = []
    for db_path in db_paths:
        all_trades.extend(
            _fetch_all(db_path, "SELECT * FROM trades WHERE status='settled' AND model_p_up IS NOT NULL")
        )

    if not all_trades:
        return "Calibration Report\n\nNo settled trades with model_p_up data."

    lines: list[str] = []
    lines.append(f"# Calibration Report (N={len(all_trades)} trades)\n")

    # Bucket by model_p_up decile
    buckets: dict[str, list[dict]] = {}
    for t in all_trades:
        decile = int(t["model_p_up"] * 10) / 10
        decile = min(decile, 0.9)  # clamp 1.0 into 0.9-1.0 bucket
        label = f"{decile:.1f}-{decile + 0.1:.1f}"
        buckets.setdefault(label, []).append(t)

    # Header
    lines.append("| Bucket | Trades | Predicted P(up) | Actual P(up) | Gap | Side | Win Rate | PnL |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")

    for label in sorted(buckets.keys()):
        items = buckets[label]
        n = len(items)
        predicted = sum(t["model_p_up"] for t in items) / n
        actual_up = sum(1 for t in items if t["outcome"] == "up") / n
        gap = actual_up - predicted

        wins = sum(1 for t in items if t["side"] == t["outcome"])
        win_rate = wins / n

        total_pnl = sum(t["pnl"] for t in items if t["pnl"] is not None)

        up_count = sum(1 for t in items if t["side"] == "up")
        down_count = n - up_count
        side_str = f"{up_count}U/{down_count}D"

        flag = " [!]" if abs(gap) > 0.10 else ""
        lines.append(
            f"| {label} | {n} | {predicted:.1%} | {actual_up:.1%} | {gap:+.1%}{flag} | {side_str} | {win_rate:.0%} | ${total_pnl:+.2f} |"
        )

    # Overall stats
    total_predicted = sum(t["model_p_up"] for t in all_trades) / len(all_trades)
    total_actual = sum(1 for t in all_trades if t["outcome"] == "up") / len(all_trades)
    total_wins = sum(1 for t in all_trades if t["side"] == t["outcome"])
    total_pnl = sum(t["pnl"] for t in all_trades if t["pnl"] is not None)

    lines.append("")
    lines.append(f"**Overall:** Predicted P(up)={total_predicted:.1%}, Actual P(up)={total_actual:.1%}, "
                 f"Win rate={total_wins}/{len(all_trades)} ({100*total_wins/len(all_trades):.0f}%), "
                 f"PnL=${total_pnl:+.2f}")

    return "\n".join(lines)


def main() -> None:
    import sys

    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    datestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if "--calibration" in sys.argv:
        extra_dbs = [a for a in sys.argv[1:] if a != "--calibration" and not a.startswith("-")]
        db_paths = (PAPER_DB_PATH, *extra_dbs)
        report = calibration_report(*db_paths)
        filename = f"{datestamp}-calibration.md"
    else:
        report = generate_report()
        filename = f"{datestamp}-analysis.md"

    path = reports_dir / filename
    path.write_text(report, encoding="utf-8")
    print(report)
    print(f"\nReport saved to {path}")


if __name__ == "__main__":
    main()
