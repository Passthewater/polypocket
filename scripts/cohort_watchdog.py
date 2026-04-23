"""Cohort safety-rail watchdog.

Polls live_trades.db every --poll-seconds, computes cohort state (fills,
rejects, cumulative PnL, wall-clock elapsed since --since), and evaluates
four safety rails. On any trip, writes `.cohort_stop` (picked up by the
bot's _on_book_update gate) and exits.

Usage:
  python scripts/cohort_watchdog.py --since 2026-04-23T18:00:00

Remove the kill-file to resume trading:
  rm .cohort_stop

Assumes both fills and rejects are tracked in live_trades.db. Rejects are
trades with status='rejected' (live executor path). If rejects are not
persisted that way, edit `_count_rejects` after inspecting the schema.
"""
import argparse
import datetime as dt
import os
import pathlib
import sqlite3
import sys
import time
from dataclasses import dataclass

# Same resolution as bot.py (parent of the polypocket/ package == repo root).
_DEFAULT_KILL = pathlib.Path(__file__).resolve().parent.parent / ".cohort_stop"
KILL_FILE = pathlib.Path(os.environ.get("COHORT_STOP_FILE", str(_DEFAULT_KILL)))


@dataclass(frozen=True)
class Rails:
    max_fills: int
    max_loss: float
    max_wall_clock_days: float
    reject_breaker_after: int
    reject_breaker_pct: float


def evaluate_rails(
    rails: Rails,
    n_fills: int,
    n_rejects: int,
    cum_pnl: float,
    elapsed_days: float,
) -> dict:
    """Pure function. Returns dict with trip/reason/metrics — no I/O."""
    metrics = {
        "n_fills": n_fills,
        "n_rejects": n_rejects,
        "cum_pnl": round(cum_pnl, 3),
        "elapsed_days": round(elapsed_days, 3),
    }

    if n_fills >= rails.max_fills:
        return {"trip": True, "reason": f"fill cap hit ({n_fills} >= {rails.max_fills})", **metrics}

    if cum_pnl <= -rails.max_loss:
        return {"trip": True, "reason": f"loss cap hit ({cum_pnl:+.2f} <= -{rails.max_loss:.2f})", **metrics}

    if elapsed_days >= rails.max_wall_clock_days:
        return {"trip": True, "reason": f"wall-clock cap hit ({elapsed_days:.2f} >= {rails.max_wall_clock_days} days)", **metrics}

    # Reject-rate breaker: armed while attempts <= reject_breaker_after,
    # fires the moment rate crosses threshold. Range check (not equality)
    # so a fast burst that skips past attempts==10 is still caught.
    # Minimum 2 attempts to avoid n=1 noise.
    attempts = n_fills + n_rejects
    if 2 <= attempts <= rails.reject_breaker_after:
        if n_rejects / attempts >= rails.reject_breaker_pct:
            return {
                "trip": True,
                "reason": f"reject-rate breaker ({n_rejects}/{attempts} in first {rails.reject_breaker_after})",
                **metrics,
            }

    return {"trip": False, "reason": None, **metrics}


def _count_fills_and_pnl(db: str, since_iso: str) -> tuple[int, float]:
    since_iso = since_iso.replace("T", " ")
    # SUM(pnl) treats NULL (open trades) as 0 — fine for short-lived windows.
    c = sqlite3.connect(db)
    r = c.execute(
        """SELECT COUNT(*), COALESCE(SUM(pnl), 0.0)
             FROM trades
            WHERE status IN ('open', 'settled')
              AND entry_price IS NOT NULL
              AND timestamp >= ?""",
        (since_iso,),
    ).fetchone()
    c.close()
    return int(r[0]), float(r[1])


def _count_rejects(db: str, since_iso: str) -> int:
    since_iso = since_iso.replace("T", " ")
    c = sqlite3.connect(db)
    r = c.execute(
        """SELECT COUNT(*) FROM trades
            WHERE status='rejected'
              AND timestamp >= ?""",
        (since_iso,),
    ).fetchone()
    c.close()
    return int(r[0])


def _elapsed_days(since_iso: str) -> float:
    start = dt.datetime.fromisoformat(since_iso)
    return (dt.datetime.utcnow() - start).total_seconds() / 86400.0


def poll_loop(db: str, since_iso: str, rails: Rails, poll_seconds: int) -> dict:
    while True:
        n_fills, cum_pnl = _count_fills_and_pnl(db, since_iso)
        n_rejects = _count_rejects(db, since_iso)
        elapsed = _elapsed_days(since_iso)
        v = evaluate_rails(rails, n_fills, n_rejects, cum_pnl, elapsed)
        ts = dt.datetime.utcnow().isoformat(timespec="seconds")
        status = "TRIP" if v["trip"] else "ok"
        print(
            f"[{ts}] {status} fills={v['n_fills']} rejects={v['n_rejects']} "
            f"pnl={v['cum_pnl']:+.2f} days={v['elapsed_days']:.2f} "
            f"reason={v['reason']}",
            flush=True,
        )
        if v["trip"]:
            KILL_FILE.write_text(f"{ts} tripped: {v['reason']}\n")
            print(f"Wrote {KILL_FILE}. Bot will pause at next window.", flush=True)
            return v
        time.sleep(poll_seconds)


def _default_since() -> str | None:
    f = pathlib.Path(".cohort_start")
    if f.exists():
        return f.read_text().strip()
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="live_trades.db")
    p.add_argument("--since", default=_default_since(),
                   help="ISO8601 UTC cohort-start timestamp. "
                        "Defaults to reading .cohort_start file.")
    p.add_argument("--poll-seconds", type=int, default=60)
    p.add_argument("--max-fills", type=int, default=25)
    p.add_argument("--max-loss", type=float, default=20.0)
    p.add_argument("--max-wall-clock-days", type=float, default=7.0)
    p.add_argument("--reject-breaker-after", type=int, default=10)
    p.add_argument("--reject-breaker-pct", type=float, default=0.5)
    args = p.parse_args()
    if not args.since:
        p.error("--since required (or create .cohort_start)")

    rails = Rails(
        max_fills=args.max_fills,
        max_loss=args.max_loss,
        max_wall_clock_days=args.max_wall_clock_days,
        reject_breaker_after=args.reject_breaker_after,
        reject_breaker_pct=args.reject_breaker_pct,
    )
    try:
        poll_loop(args.db, args.since, rails, args.poll_seconds)
    except KeyboardInterrupt:
        print("Interrupted. Not writing kill-file.", file=sys.stderr)


if __name__ == "__main__":
    main()
