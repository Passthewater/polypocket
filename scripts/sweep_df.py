"""Sweep MODEL_TAIL_DF values to find optimal calibration."""

import sqlite3
import sys
from contextlib import closing
from math import sqrt

from scipy.stats import t as t_dist


def fetch_trades(*db_paths: str) -> list[dict]:
    all_trades = []
    for db_path in db_paths:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT t.side, t.model_p_up, t.outcome, t.pnl,
                       s.displacement, s.sigma_5min, s.t_remaining
                FROM trades t
                LEFT JOIN window_snapshots s
                    ON t.window_slug = s.window_slug AND s.snapshot_type = 'decision'
                WHERE t.status = 'settled'
                  AND s.displacement IS NOT NULL
                  AND s.sigma_5min IS NOT NULL
                  AND s.t_remaining IS NOT NULL
            """).fetchall()
            all_trades.extend(dict(r) for r in rows)
    return all_trades


def recompute_p_up(displacement: float, t_remaining: float, sigma_5min: float, df: float) -> float:
    if t_remaining <= 0 or sigma_5min <= 0:
        if displacement > 0:
            return 1.0
        if displacement < 0:
            return 0.0
        return 0.5
    sigma_remaining = sigma_5min * sqrt(t_remaining / 300.0)
    if sigma_remaining <= 0:
        return 0.5
    return float(t_dist.cdf(displacement / sigma_remaining, df=df))


def calibration_error(trades: list[dict], df: float) -> dict:
    """Compute mean absolute calibration error for a given df."""
    buckets: dict[str, list] = {}
    for t in trades:
        p_up = recompute_p_up(t["displacement"], t["t_remaining"], t["sigma_5min"], df)
        decile = min(int(p_up * 10) / 10, 0.9)
        label = f"{decile:.1f}-{decile + 0.1:.1f}"
        actual_up = 1 if t["outcome"] == "up" else 0
        buckets.setdefault(label, []).append((p_up, actual_up))

    total_abs_error = 0.0
    total_n = 0
    for items in buckets.values():
        n = len(items)
        predicted = sum(p for p, _ in items) / n
        actual = sum(a for _, a in items) / n
        total_abs_error += abs(actual - predicted) * n
        total_n += n

    mae = total_abs_error / max(total_n, 1)

    # Also compute win rate if we used this df for side selection
    wins = 0
    for t in trades:
        p_up = recompute_p_up(t["displacement"], t["t_remaining"], t["sigma_5min"], df)
        predicted_side = "up" if p_up >= 0.5 else "down"
        if predicted_side == t["outcome"]:
            wins += 1

    return {"df": df, "mae": mae, "direction_accuracy": wins / len(trades), "n": total_n}


def main():
    db_paths = sys.argv[1:] if sys.argv[1:] else ["paper_trades.db"]
    trades = fetch_trades(*db_paths)
    print(f"Loaded {len(trades)} trades from {', '.join(db_paths)}\n")

    if not trades:
        print("No qualifying trades found. Need settled trades with decision snapshots.")
        return

    df_values = [2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 50, 1000]

    print(f"{'df':>6}  {'MAE':>8}  {'Dir Acc':>8}  {'N':>5}")
    print("-" * 35)

    best = None
    for df in df_values:
        result = calibration_error(trades, df)
        marker = ""
        if best is None or result["mae"] < best["mae"]:
            best = result
            marker = " <-- best"
        print(f"{df:>6}  {result['mae']:>8.4f}  {result['direction_accuracy']:>8.1%}  {result['n']:>5}{marker}")

    print(f"\nBest df: {best['df']} (MAE={best['mae']:.4f})")


if __name__ == "__main__":
    main()
