"""Analyze a buffer=8 cohort: emit slip distribution, verdict, and a markdown report.

Produces `scripts/_cohort_analysis.md` with the verdict artifact the design
doc (`docs/plans/2026-04-23-buffer-8-live-cohort-design.md`) requires.

Usage:
  python scripts/analyze_buffer_cohort.py --since 2026-04-23T18:00:00

Verdict thresholds (pre-committed):
  median slip <= 6 ticks -> SHIP
  median slip == 7 ticks -> AMBIGUOUS (extend cohort)
  median slip >= 8 ticks -> ESCALATE
"""
import argparse
import datetime as dt
import json
import pathlib
import sqlite3
import statistics
import sys

REPORT_PATH = pathlib.Path("scripts/_cohort_analysis.md")

BASELINE_BUFFER = 15
BASELINE_N = 59
BASELINE_SLIP_MEDIAN = 11.6  # cents (i.e. ticks), per config.py:17
BASELINE_SLIP_MEAN = 10.5


def compute_slip_ticks(entry: float, best_opp_bid: float) -> int:
    """Tick-integer slip computation. Guards against float artifacts."""
    clearing_ticks = round((1.0 - best_opp_bid) * 100)
    entry_ticks = round(entry * 100)
    return entry_ticks - clearing_ticks


def slip_distribution(slips: list[int]) -> dict:
    if not slips:
        return {"n": 0, "median": None, "mean": None, "min": None, "max": None, "p25": None, "p75": None}
    s = sorted(slips)
    n = len(s)
    return {
        "n": n,
        "median": statistics.median(s),
        "mean": round(sum(s) / n, 2),
        "min": s[0],
        "max": s[-1],
        "p25": s[max(0, n // 4 - 1)] if n >= 4 else s[0],
        "p75": s[min(n - 1, 3 * n // 4)] if n >= 4 else s[-1],
    }


def classify_verdict(median_slip) -> str:
    if median_slip is None:
        return "AMBIGUOUS"
    if median_slip <= 6:
        return "SHIP"
    if median_slip >= 8:
        return "ESCALATE"
    return "AMBIGUOUS"


def _load_cohort(db: str, since_iso: str) -> list[dict]:
    since_iso = since_iso.replace("T", " ")
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        """SELECT t.id, t.side, t.entry_price, t.pnl, t.status, t.timestamp,
                  s.up_bids_json, s.down_bids_json
             FROM trades t
             LEFT JOIN window_snapshots s
               ON s.window_slug = t.window_slug AND s.trade_fired = 1
            WHERE t.status IN ('open', 'settled')
              AND t.entry_price IS NOT NULL
              AND t.timestamp >= ?
            ORDER BY t.id""",
        (since_iso,),
    ).fetchall()
    c.close()
    out = []
    for r in rows:
        bids_json = r["down_bids_json"] if r["side"] == "up" else r["up_bids_json"]
        if not bids_json:
            continue
        try:
            bids = json.loads(bids_json)
            best = max(float(b["price"]) for b in bids)
        except Exception:
            continue
        out.append({
            "id": r["id"],
            "side": r["side"],
            "entry_price": float(r["entry_price"]),
            "best_opp_bid": best,
            "pnl": float(r["pnl"]) if r["pnl"] is not None else None,
            "status": r["status"],
        })
    return out


def _count_rejects(db: str, since_iso: str) -> int:
    since_iso = since_iso.replace("T", " ")
    c = sqlite3.connect(db)
    r = c.execute(
        "SELECT COUNT(*) FROM trades WHERE status='rejected' AND timestamp >= ?",
        (since_iso,),
    ).fetchone()
    c.close()
    return int(r[0])


def _emit_report(since_iso: str, dist: dict, verdict: str, n_rejects: int, total_pnl: float) -> str:
    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    attempts = dist["n"] + n_rejects
    reject_rate = (n_rejects / attempts) if attempts else 0.0
    delta_median = (dist["median"] - BASELINE_SLIP_MEDIAN) if dist["median"] is not None else None
    delta_str = f"{delta_median:+.1f}" if delta_median is not None else "n/a"

    lines = [
        "# Buffer=8 cohort analysis",
        "",
        f"**Generated:** {now}Z",
        f"**Cohort start:** {since_iso}",
        f"**Config:** IOC_BUFFER_TICKS=8, SIGNAL_CUSHION_TICKS=11 (all other knobs frozen)",
        "",
        "## Slip distribution",
        "",
        "| metric | buffer=15 baseline | buffer=8 cohort | delta |",
        "|---|---|---|---|",
        f"| n | {BASELINE_N} | {dist['n']} | — |",
        f"| median (ticks) | {BASELINE_SLIP_MEDIAN} | {dist['median']} | {delta_str} |",
        f"| mean (ticks) | {BASELINE_SLIP_MEAN} | {dist['mean']} | — |",
        f"| min / max | — | {dist['min']} / {dist['max']} | — |",
        f"| p25 / p75 | — | {dist['p25']} / {dist['p75']} | — |",
        "",
        "## Reject rate",
        "",
        f"- Attempts: {attempts} (fills {dist['n']} + rejects {n_rejects})",
        f"- Reject rate: {reject_rate:.1%}",
        "",
        "## Cohort PnL",
        "",
        f"- Total: {total_pnl:+.2f}",
        f"- Avg/trade: {(total_pnl / dist['n']):+.3f}" if dist["n"] else "- Avg/trade: n/a",
        "",
        "## Verdict",
        "",
        f"**{verdict}**",
        "",
    ]
    if verdict == "SHIP":
        lines.append("Median slip is at or below the 6-tick threshold. Update `polypocket/config.py` defaults (`IOC_BUFFER_TICKS=8`, `SIGNAL_CUSHION_TICKS` = cohort median slip), refresh the calibration comment blocks, and run a follow-up gate-only sweep on the combined 79-fill corpus.")
    elif verdict == "ESCALATE":
        lines.append("Median slip is at or above the 8-tick threshold. Matcher is filling near the limit regardless of buffer. Keep live paused; open model-recalibration issue against UP-side 0.70+ bins (per #13).")
    else:
        lines.append("Result is in the 1-tick deadband (median=7) or sample is missing. Extend the cohort to 40 fills before deciding. Do not change config.")

    body = "\n".join(lines) + "\n"
    REPORT_PATH.write_text(body)
    return body


def _default_since() -> str | None:
    f = pathlib.Path(".cohort_start")
    if f.exists():
        return f.read_text().strip()
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="live_trades.db")
    p.add_argument("--since", default=_default_since(),
                   help="ISO8601 UTC cohort-start timestamp. Defaults to .cohort_start.")
    args = p.parse_args()
    if not args.since:
        p.error("--since required (or create .cohort_start)")

    rows = _load_cohort(args.db, args.since)
    slips = [compute_slip_ticks(r["entry_price"], r["best_opp_bid"]) for r in rows]
    dist = slip_distribution(slips)
    verdict = classify_verdict(dist["median"])
    n_rejects = _count_rejects(args.db, args.since)
    total_pnl = sum(r["pnl"] for r in rows if r["pnl"] is not None)

    body = _emit_report(args.since, dist, verdict, n_rejects, total_pnl)
    print(body)
    print(f"Wrote {REPORT_PATH}")

    # Exit code communicates verdict for downstream automation.
    sys.exit({"SHIP": 0, "AMBIGUOUS": 1, "ESCALATE": 2}[verdict])


if __name__ == "__main__":
    main()
