"""Post-hoc replay: what would the post-only execution path have filled,
given the historical paper FAK cohort's decision snapshots and book samples?

This honors the 2026-05-15 post-only-entries design's Phase-2 validation
plan: deterministic replay against real recorded book data, no synthesized
fillmodel. The runtime bot's paper path stays unchanged (FAK at ask); this
script just asks "if the bot had been resting at pmc - offset_ticks instead,
which of those decisions would have filled before the cancel boundary?"

Reads from PAPER_DB_PATH (read-only). Emits scripts/_post_only_replay.md.

Mechanism:
1. For each window with a 'decision' snapshot + non-null up_bids_json /
   down_bids_json + trade_fired=1, compute the would-be rest_price using
   the project's `post_only_rest_price` helper at the current
   POST_ONLY_REST_OFFSET_TICKS.
2. Walk forward through `window_book_samples` rows for the same
   window_slug, in chronological order. A fill is declared when any
   sample's best_opp_bid >= 1 - rest_price (i.e., the implied clearing
   has reached or crossed the rest level).
3. Stop the walk at the cancel boundary
   (window_end - POST_ONLY_CANCEL_AT_T_REMAINING_S). Fills after that
   would have been pre-empted by the bot-side cancel in live mode.
4. Join to trades.outcome for the windows where a fill is declared to
   compute hypothetical calibration.

Caveats:
- Book samples are at 30-second cadence; a fill that happened intra-30s
  is invisible. Reported fill rate is therefore a LOWER BOUND. Live data
  with sub-second granularity may show a higher fill rate.
- Replay assumes the bot's presence doesn't change other actors'
  behavior. In live mode our resting maker may suppress/attract other
  flow; the replay can't see that.
- decision-time bids JSON is required. Coverage is partial across the
  paper corpus (per the 2026-05-11 PnL attribution doc, ~27% of historical
  rows had non-null bids JSON pre-G5; forward rows always have them).

Run::

    python scripts/replay_post_only_paper.py --offset-ticks 2
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from polypocket.clients.polymarket import post_only_rest_price
from polypocket.config import (
    PAPER_DB_PATH,
    POST_ONLY_CANCEL_AT_T_REMAINING_S,
    POST_ONLY_REST_OFFSET_TICKS,
)

DEFAULT_BIN_WIDTH = 0.05
DEFAULT_BIN_LO = 0.50
DEFAULT_BIN_HI = 1.00


def _load_decisions(conn: sqlite3.Connection) -> list[dict]:
    """Eligible decisions: trade_fired=1 with both side-relevant bids JSON
    populated. Joined to trades.outcome for the calibration check (a
    decision without a settled trades row gets outcome=None, skipped from
    calibration but counted toward fill rate).

    `decision_ts` is converted from ISO text → Unix seconds at load-time
    so downstream walks compare apples-to-apples with the REAL `sampled_at`
    column in window_book_samples.
    """
    rows = conn.execute(
        """
        SELECT
            ws.window_slug AS window_slug,
            ws.preview_side AS preview_side,
            ws.model_p_up AS model_p_up,
            ws.up_bids_json AS up_bids_json,
            ws.down_bids_json AS down_bids_json,
            ws.timestamp AS decision_ts_iso,
            t.outcome AS outcome
        FROM window_snapshots ws
        LEFT JOIN trades t ON t.window_slug = ws.window_slug
        WHERE ws.snapshot_type = 'decision'
          AND ws.trade_fired = 1
          AND ws.up_bids_json IS NOT NULL
          AND ws.down_bids_json IS NOT NULL
        """
    ).fetchall()
    out = []
    for r in rows:
        up_bids = json.loads(r["up_bids_json"]) if r["up_bids_json"] else None
        down_bids = json.loads(r["down_bids_json"]) if r["down_bids_json"] else None
        try:
            decision_ts = datetime.strptime(
                r["decision_ts_iso"], "%Y-%m-%d %H:%M:%S",
            ).replace(tzinfo=timezone.utc).timestamp()
        except (ValueError, TypeError):
            decision_ts = 0.0
        out.append({
            "window_slug": r["window_slug"],
            "preview_side": r["preview_side"],
            "model_p_up": r["model_p_up"],
            "up_bids": up_bids,
            "down_bids": down_bids,
            "decision_ts": decision_ts,
            "outcome": r["outcome"],
        })
    return out


def _load_window_end(conn: sqlite3.Connection, slug: str) -> float | None:
    """Best-effort window_end approximation: the latest book sample for
    the slug rounded up to the next 5-minute boundary, or None if no
    samples exist."""
    row = conn.execute(
        "SELECT MAX(sampled_at) AS last_ts FROM window_book_samples WHERE window_slug = ?",
        (slug,),
    ).fetchone()
    if row is None or row["last_ts"] is None:
        return None
    last = float(row["last_ts"])
    # Round up to the next 5-min boundary as the implied window_end.
    return math.ceil(last / 300.0) * 300.0


def _walk_samples_for_fill(
    conn: sqlite3.Connection,
    slug: str,
    side: str,
    rest_price: float,
    decision_ts: float,
    cancel_at_ts: float | None,
) -> tuple[bool, int, float | None]:
    """Walk window_book_samples chronologically *after* decision_ts. Return
    (filled, sample_offset_at_fill, sample_ts_at_fill).
    A fill is declared when best_opp_bid >= 1 - rest_price. Walk stops at
    cancel_at_ts if provided. Pre-decision samples are skipped — the
    rest order would not have existed yet."""
    threshold = 1.0 - rest_price
    rows = conn.execute(
        """
        SELECT sampled_at, up_bids_json, down_bids_json
        FROM window_book_samples
        WHERE window_slug = ?
          AND sampled_at >= ?
        ORDER BY sampled_at ASC
        """,
        (slug, decision_ts),
    ).fetchall()
    for i, r in enumerate(rows):
        sampled_at = float(r["sampled_at"])
        if cancel_at_ts is not None and sampled_at > cancel_at_ts:
            return False, i, None
        opp_bids_json = r["down_bids_json"] if side == "up" else r["up_bids_json"]
        if not opp_bids_json:
            continue
        try:
            opp_bids = json.loads(opp_bids_json)
        except (TypeError, ValueError):
            continue
        if not opp_bids:
            continue
        best_opp = max(float(b["price"]) for b in opp_bids)
        if best_opp >= threshold - 1e-9:
            return True, i, sampled_at
    return False, len(rows), None


def _calibration_bin(p_up: float, bin_width: float, bin_lo: float, bin_hi: float) -> int | None:
    if p_up is None:
        return None
    if p_up >= 0.5:
        p_pred = p_up
    else:
        p_pred = 1.0 - p_up
    if p_pred < bin_lo or p_pred >= bin_hi:
        return None
    return int((p_pred - bin_lo) / bin_width)


def replay(
    db_path: str,
    offset_ticks: int,
    cancel_buffer_s: float,
    bin_width: float = DEFAULT_BIN_WIDTH,
) -> dict:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        decisions = _load_decisions(conn)
        n_total = len(decisions)
        results = []
        n_no_pmc = 0
        for d in decisions:
            rest = post_only_rest_price(
                d["preview_side"], d["up_bids"], d["down_bids"], offset_ticks,
            )
            if rest is None:
                n_no_pmc += 1
                results.append({
                    **d, "rest_price": None, "filled": False,
                    "fill_sample_index": None, "fill_sample_ts": None,
                })
                continue
            window_end = _load_window_end(conn, d["window_slug"])
            cancel_at = (window_end - cancel_buffer_s) if window_end else None
            filled, idx, ts = _walk_samples_for_fill(
                conn, d["window_slug"], d["preview_side"], rest,
                d["decision_ts"], cancel_at,
            )
            results.append({
                **d, "rest_price": rest, "filled": filled,
                "fill_sample_index": idx if filled else None,
                "fill_sample_ts": ts,
            })

    n_eligible = n_total - n_no_pmc
    n_filled = sum(1 for r in results if r["filled"])
    fill_rate = (n_filled / n_eligible) if n_eligible > 0 else 0.0
    fill_indices = [r["fill_sample_index"] for r in results if r["filled"]]
    median_fill_idx = sorted(fill_indices)[len(fill_indices) // 2] if fill_indices else None

    # Calibration on the would-have-filled cohort with known outcomes.
    n_bins = int(round((DEFAULT_BIN_HI - DEFAULT_BIN_LO) / bin_width))
    bins: list[dict] = [
        {"lo": DEFAULT_BIN_LO + i * bin_width, "hi": DEFAULT_BIN_LO + (i + 1) * bin_width,
         "n": 0, "sum_p": 0.0, "sum_hit": 0}
        for i in range(n_bins)
    ]
    for r in results:
        if not r["filled"] or r["outcome"] not in {"up", "down"}:
            continue
        p = r["model_p_up"]
        b = _calibration_bin(p, bin_width, DEFAULT_BIN_LO, DEFAULT_BIN_HI)
        if b is None:
            continue
        if p >= 0.5:
            p_pred = p
            hit = 1 if r["outcome"] == "up" else 0
        else:
            p_pred = 1.0 - p
            hit = 1 if r["outcome"] == "down" else 0
        bins[b]["n"] += 1
        bins[b]["sum_p"] += p_pred
        bins[b]["sum_hit"] += hit

    return {
        "db_path": db_path,
        "offset_ticks": offset_ticks,
        "cancel_buffer_s": cancel_buffer_s,
        "n_total_decisions": n_total,
        "n_eligible": n_eligible,
        "n_no_pmc": n_no_pmc,
        "n_filled": n_filled,
        "fill_rate": fill_rate,
        "median_fill_sample_index": median_fill_idx,
        "calibration_bins": bins,
    }


def _format_report(stats: dict) -> str:
    lines: list[str] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines.append(f"# Post-only paper replay — {now}")
    lines.append("")
    lines.append(f"- DB: `{stats['db_path']}`")
    lines.append(f"- offset_ticks: `{stats['offset_ticks']}`")
    lines.append(f"- cancel_buffer_s: `{stats['cancel_buffer_s']}`")
    lines.append("")
    lines.append("## Fill statistics")
    lines.append("")
    lines.append(f"- Total decisions with bids JSON: **{stats['n_total_decisions']}**")
    lines.append(f"- Eligible (pmc computable): **{stats['n_eligible']}** "
                 f"(skipped {stats['n_no_pmc']} for missing opp bid)")
    lines.append(f"- Would-have-filled: **{stats['n_filled']}**")
    lines.append(f"- Fill rate: **{stats['fill_rate'] * 100:.1f}%**")
    if stats["median_fill_sample_index"] is not None:
        idx = stats["median_fill_sample_index"]
        lines.append(f"- Median post-decision sample-index at fill: **{idx}** "
                     f"(~{idx * 30}s after decision time)")
    lines.append("")
    lines.append("Note: book samples are at 30-second cadence; reported fill rate "
                 "is a LOWER BOUND. Live data with sub-second granularity may show "
                 "a higher fill rate.")
    lines.append("")
    lines.append("## Calibration (would-have-filled cohort only)")
    lines.append("")
    lines.append("| bin | n | mean p_pred | hit rate | gap |")
    lines.append("|---|---:|---:|---:|---:|")
    for b in stats["calibration_bins"]:
        if b["n"] == 0:
            continue
        mean_p = b["sum_p"] / b["n"]
        hit_rate = b["sum_hit"] / b["n"]
        gap = hit_rate - mean_p
        lines.append(
            f"| {b['lo']:.2f}-{b['hi']:.2f} | {b['n']} | {mean_p:.3f} | "
            f"{hit_rate:.3f} | {gap:+.3f} |"
        )
    lines.append("")
    lines.append("## Acceptance check")
    lines.append("")
    rate = stats["fill_rate"]
    if rate < 0.15:
        lines.append(f"- :warning: Fill rate **{rate*100:.1f}%** is below 15% — "
                     "offset may be too deep. Consider lowering "
                     "POST_ONLY_REST_OFFSET_TICKS.")
    elif rate > 0.80:
        lines.append(f"- :warning: Fill rate **{rate*100:.1f}%** is above 80% — "
                     "the rest price is likely crossing immediately in live "
                     "(would-reject post-only). Consider raising offset.")
    else:
        lines.append(f"- Fill rate **{rate*100:.1f}%** is in the plausible 15-80% band.")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=PAPER_DB_PATH, help="Paper DB path")
    parser.add_argument("--offset-ticks", type=int, default=POST_ONLY_REST_OFFSET_TICKS)
    parser.add_argument("--cancel-buffer-s", type=float,
                        default=POST_ONLY_CANCEL_AT_T_REMAINING_S)
    parser.add_argument("--out", default="scripts/_post_only_replay.md",
                        help="Output markdown path")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"DB not found: {args.db}")
        return 1

    stats = replay(args.db, args.offset_ticks, args.cancel_buffer_s)
    report = _format_report(stats)
    Path(args.out).write_text(report, encoding="utf-8")
    print(f"Wrote {args.out}")
    print(f"  n_eligible={stats['n_eligible']}  "
          f"n_filled={stats['n_filled']}  "
          f"fill_rate={stats['fill_rate']*100:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
