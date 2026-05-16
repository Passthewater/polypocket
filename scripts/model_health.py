"""Nightly model-health checker: reliability diagram + bin WR drift (issue #22).

Re-emits the #15 acceptance diagnostic over a rolling window of recent decisions
for both v1-calibrated and v2 models, on paper and live ledgers. Output is a
markdown report (default ``scripts/_model_health.md``).

Run manually, weekly at first::

    python scripts/model_health.py --window-days 14

The report flags bins where ``n >= 20`` and ``|gap| > 0.05`` -- two consecutive
weekly reports flagging the same bin warrants a refit (humans decide; this
script does not refit).
"""
import argparse
import math
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from polypocket.config import LIVE_DB_PATH, PAPER_DB_PATH

DEFAULT_BIN_WIDTH = 0.05
DEFAULT_BIN_LO = 0.50
DEFAULT_BIN_HI = 1.00
GAP_FLAG_THRESHOLD = 0.05
N_FLAG_THRESHOLD = 20
SAMPLE_SIZE_GATE = 200  # informational, per #15: no refit on < 200 fresh decisions


def _pred_and_hit(p_up: float, outcome: str) -> tuple[float, int, str]:
    """Reorient (p_up, outcome) onto the model's leaning side.

    Returns (p_pred, hit, side) where side is 'up' if p_up >= 0.5 else 'down',
    p_pred is the model's confidence on that side, and hit is 1 if the outcome
    matches the leaning.
    """
    if p_up >= 0.5:
        return p_up, 1 if outcome == "up" else 0, "up"
    return 1.0 - p_up, 1 if outcome == "down" else 0, "down"


def reliability_table(
    rows: list[dict],
    p_col: str,
    bin_width: float = DEFAULT_BIN_WIDTH,
    bin_lo: float = DEFAULT_BIN_LO,
    bin_hi: float = DEFAULT_BIN_HI,
) -> list[dict]:
    """Per-bin reliability of the model's confidence on its leaning side."""
    if bin_width <= 0:
        raise ValueError("bin_width must be positive")
    n_bins = int(round((bin_hi - bin_lo) / bin_width))
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(n_bins)]
    for r in rows:
        p_up = r.get(p_col)
        outcome = r.get("outcome")
        if p_up is None or outcome not in ("up", "down"):
            continue
        p_pred, hit, _ = _pred_and_hit(float(p_up), outcome)
        # Clamp to [bin_lo, bin_hi] and assign by floor; the last bin is
        # right-inclusive to capture p_pred == 1.0. The 1e-9 nudge pulls
        # boundary values (e.g. 1-0.30 = 0.6999... or 0.7000...1) into the
        # bin whose name they read like.
        idx = int((p_pred - bin_lo) / bin_width + 1e-9)
        if idx < 0 or idx >= n_bins:
            if p_pred >= bin_hi:
                idx = n_bins - 1
            else:
                continue
        buckets[idx].append((p_pred, hit))

    out = []
    for i, items in enumerate(buckets):
        lo = bin_lo + i * bin_width
        hi = lo + bin_width
        n = len(items)
        if n == 0:
            out.append({"bin_lo": lo, "bin_hi": hi, "n": 0,
                        "predicted_wr": None, "actual_wr": None, "gap": None})
            continue
        predicted_wr = sum(p for p, _ in items) / n
        actual_wr = sum(h for _, h in items) / n
        out.append({
            "bin_lo": lo, "bin_hi": hi, "n": n,
            "predicted_wr": predicted_wr,
            "actual_wr": actual_wr,
            "gap": actual_wr - predicted_wr,
        })
    return out


def brier_score(rows: list[dict], p_col: str) -> float | None:
    """Mean squared error of raw p_up against 1{outcome == 'up'}."""
    sq = 0.0
    n = 0
    for r in rows:
        p_up = r.get(p_col)
        outcome = r.get("outcome")
        if p_up is None or outcome not in ("up", "down"):
            continue
        y = 1.0 if outcome == "up" else 0.0
        sq += (float(p_up) - y) ** 2
        n += 1
    return sq / n if n else None


def log_loss(rows: list[dict], p_col: str, eps: float = 1e-9) -> float | None:
    """Clipped binary log loss of raw p_up against 1{outcome == 'up'}."""
    s = 0.0
    n = 0
    for r in rows:
        p_up = r.get(p_col)
        outcome = r.get("outcome")
        if p_up is None or outcome not in ("up", "down"):
            continue
        p = min(max(float(p_up), eps), 1.0 - eps)
        if outcome == "up":
            s += -math.log(p)
        else:
            s += -math.log(1.0 - p)
        n += 1
    return s / n if n else None


def per_side_gap(rows: list[dict], p_col: str) -> dict:
    """Calibration gap split by the model's leaning side (UP vs DOWN)."""
    sides: dict[str, list[tuple[float, int]]] = {"up": [], "down": []}
    for r in rows:
        p_up = r.get(p_col)
        outcome = r.get("outcome")
        if p_up is None or outcome not in ("up", "down"):
            continue
        p_pred, hit, side = _pred_and_hit(float(p_up), outcome)
        sides[side].append((p_pred, hit))
    out = {}
    for side, items in sides.items():
        n = len(items)
        if n == 0:
            out[side] = {"n": 0, "predicted_wr": None, "actual_wr": None, "gap": None}
            continue
        predicted_wr = sum(p for p, _ in items) / n
        actual_wr = sum(h for _, h in items) / n
        out[side] = {
            "n": n,
            "predicted_wr": predicted_wr,
            "actual_wr": actual_wr,
            "gap": actual_wr - predicted_wr,
        }
    return out


def flag_breaches(
    table: list[dict],
    gap_threshold: float = GAP_FLAG_THRESHOLD,
    n_threshold: int = N_FLAG_THRESHOLD,
) -> list[dict]:
    """Return rows where n >= n_threshold AND |gap| > gap_threshold."""
    flagged = []
    for row in table:
        if row["gap"] is None:
            continue
        if row["n"] >= n_threshold and abs(row["gap"]) > gap_threshold:
            flagged.append(row)
    return flagged


def load_decisions(
    db_path: str,
    window_days: int | None = None,
    max_decisions: int | None = None,
) -> list[dict]:
    """Return decision rows joined with close.outcome, newest last.

    Filters out rows where both v1 and v2 probabilities are NULL or where no
    close snapshot exists. If ``window_days`` is given, restrict by
    ``decision.timestamp``. If ``max_decisions`` is given, keep the most recent
    N after filtering.
    """
    if not os.path.exists(db_path):
        return []
    sql = """
        SELECT d.window_slug, d.timestamp,
               d.model_p_up_v1_calibrated, d.model_p_up_v2,
               c.outcome
        FROM window_snapshots d
        JOIN window_snapshots c
          ON c.window_slug = d.window_slug AND c.snapshot_type = 'close'
        WHERE d.snapshot_type = 'decision'
          AND c.outcome IN ('up', 'down')
          AND (d.model_p_up_v1_calibrated IS NOT NULL
               OR d.model_p_up_v2 IS NOT NULL)
    """
    params: list = []
    if window_days is not None:
        sql += " AND d.timestamp >= datetime('now', ?)"
        params.append(f"-{int(window_days)} days")
    sql += " ORDER BY d.timestamp ASC, d.id ASC"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    if max_decisions is not None and len(rows) > max_decisions:
        rows = rows[-max_decisions:]
    return rows


def _fmt_pct(x: float | None) -> str:
    return f"{x*100:+.1f}pt" if x is not None else "—"


def _fmt_wr(x: float | None) -> str:
    return f"{x*100:.1f}%" if x is not None else "—"


def _render_reliability(table: list[dict], flagged_keys: set[tuple]) -> list[str]:
    out = ["| bin (p_pred) | n | predicted_wr | actual_wr | gap | flag |",
           "| --- | ---: | ---: | ---: | ---: | :---: |"]
    for row in table:
        key = (row["bin_lo"], row["bin_hi"])
        flag = "*" if key in flagged_keys else ""
        out.append(
            f"| {row['bin_lo']:.2f}-{row['bin_hi']:.2f} | {row['n']} | "
            f"{_fmt_wr(row['predicted_wr'])} | {_fmt_wr(row['actual_wr'])} | "
            f"{_fmt_pct(row['gap'])} | {flag} |"
        )
    return out


def _render_aggregate(rows: list[dict], p_col: str) -> list[str]:
    brier = brier_score(rows, p_col)
    ll = log_loss(rows, p_col)
    side = per_side_gap(rows, p_col)
    return [
        f"- **Brier:** {brier:.4f}" if brier is not None else "- **Brier:** —",
        f"- **Log loss:** {ll:.4f}" if ll is not None else "- **Log loss:** —",
        f"- **UP-leaning gap:** n={side['up']['n']}, "
        f"predicted={_fmt_wr(side['up']['predicted_wr'])}, "
        f"actual={_fmt_wr(side['up']['actual_wr'])}, "
        f"gap={_fmt_pct(side['up']['gap'])}",
        f"- **DOWN-leaning gap:** n={side['down']['n']}, "
        f"predicted={_fmt_wr(side['down']['predicted_wr'])}, "
        f"actual={_fmt_wr(side['down']['actual_wr'])}, "
        f"gap={_fmt_pct(side['down']['gap'])}",
    ]


def _model_section(
    rows: list[dict], p_col: str, label: str, bin_width: float
) -> list[str]:
    out = [f"### {label}", ""]
    usable = [r for r in rows if r.get(p_col) is not None]
    if not usable:
        out.append("_no decisions with this model logged in window_")
        out.append("")
        return out
    table = reliability_table(usable, p_col, bin_width=bin_width)
    breaches = flag_breaches(table)
    flagged_keys = {(b["bin_lo"], b["bin_hi"]) for b in breaches}
    out += _render_reliability(table, flagged_keys)
    out.append("")
    out += _render_aggregate(usable, p_col)
    if breaches:
        bins = ", ".join(f"{b['bin_lo']:.2f}-{b['bin_hi']:.2f}" for b in breaches)
        out.append(f"- **Flagged bins** (n>={N_FLAG_THRESHOLD}, "
                   f"|gap|>{int(GAP_FLAG_THRESHOLD*100)}pt): {bins}")
    if len(usable) < SAMPLE_SIZE_GATE:
        out.append(f"- _Sample-size gate not yet met "
                   f"({len(usable)}/{SAMPLE_SIZE_GATE} fresh decisions)._")
    out.append("")
    return out


def _source_section(
    db_path: str,
    name: str,
    window_days: int | None,
    max_decisions: int | None,
    bin_width: float,
) -> list[str]:
    out = [f"## {name}", ""]
    rows = load_decisions(db_path, window_days=window_days, max_decisions=max_decisions)
    if not rows:
        out.append("_no decisions in window (or DB does not exist)_")
        out.append("")
        return out
    out.append(f"_{len(rows)} decisions in window, "
               f"{rows[0]['timestamp']} → {rows[-1]['timestamp']}_")
    out.append("")
    out += _model_section(rows, "model_p_up_v1_calibrated", "v1 (calibrated)", bin_width)
    out += _model_section(rows, "model_p_up_v2", "v2", bin_width)
    return out


def render_report(
    paper_rows_db: str,
    live_rows_db: str,
    window_days: int | None,
    max_decisions: int | None,
    bin_width: float,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(timezone.utc)
    header_when = now.strftime("%Y-%m-%d %H:%M UTC")
    window_desc = []
    if window_days is not None:
        window_desc.append(f"last {window_days} days")
    if max_decisions is not None:
        window_desc.append(f"last {max_decisions} decisions")
    if not window_desc:
        window_desc.append("all available decisions")
    lines = [
        f"# Model Health Report -- {header_when}",
        "",
        f"_Window: {', '.join(window_desc)}. "
        f"Bin width: {bin_width:.2f}. "
        f"Flag rule: n>={N_FLAG_THRESHOLD} and |gap|>{int(GAP_FLAG_THRESHOLD*100)}pt._",
        "",
    ]
    lines += _source_section(paper_rows_db, "Paper", window_days, max_decisions, bin_width)
    lines += _source_section(live_rows_db, "Live", window_days, max_decisions, bin_width)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--paper-db", default=PAPER_DB_PATH)
    parser.add_argument("--live-db", default=LIVE_DB_PATH)
    parser.add_argument("--window-days", type=int, default=None,
                        help="restrict to decisions in the last N days")
    parser.add_argument("--max-decisions", type=int, default=None,
                        help="cap to the most recent N decisions after filtering")
    parser.add_argument("--bin-width", type=float, default=DEFAULT_BIN_WIDTH)
    parser.add_argument("--out", default="scripts/_model_health.md")
    args = parser.parse_args(argv)

    report = render_report(
        paper_rows_db=args.paper_db,
        live_rows_db=args.live_db,
        window_days=args.window_days,
        max_decisions=args.max_decisions,
        bin_width=args.bin_width,
    )
    Path(args.out).write_text(report, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
