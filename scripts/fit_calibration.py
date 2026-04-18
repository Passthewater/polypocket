"""Replay paper_trades.db with the calibration configured in config.py.

Reports:
  1. Per-side calibration gap (pre- and post-calibration) — success target ±5pts.
  2. Log-loss improvement.
  3. Projected EV/trade if today's filter config had been in force.

Also sweeps candidate DOWN shrinkage factors and min-edge thresholds so
future retuning has a single entry point.

Usage: python -m scripts.fit_calibration
"""

from __future__ import annotations

import math
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from polypocket.config import (
    CALIBRATION_SHRINKAGE_DOWN,
    CALIBRATION_SHRINKAGE_UP,
    FEE_RATE,
    MIN_EDGE_THRESHOLD,
    MIN_EDGE_THRESHOLD_DOWN,
    effective_ask,
)
from polypocket.observer import calibrate_p_up

DB = Path(__file__).resolve().parents[1] / "paper_trades.db"


def load_trades() -> list[dict]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT t.side, t.entry_price, t.size, s.model_p_up, t.edge, t.outcome, t.pnl,
               s.up_ask, s.down_ask
        FROM trades t
        LEFT JOIN window_snapshots s
          ON t.window_slug = s.window_slug AND s.snapshot_type='decision'
        WHERE t.status='settled' AND t.outcome IS NOT NULL
          AND s.model_p_up IS NOT NULL
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def per_side_calibration(trades: list[dict], up_factor: float, down_factor: float) -> None:
    """Report average predicted win rate vs actual, raw and calibrated."""
    print(f"{'side':5s} {'n':>3s} {'pred_raw':>9s} {'pred_cal':>9s} {'actual':>7s} "
          f"{'gap_raw':>9s} {'gap_cal':>9s}")
    for side in ("up", "down"):
        side_rows = [t for t in trades if t["side"] == side]
        if not side_rows:
            continue
        n = len(side_rows)
        if side == "up":
            preds_raw = [t["model_p_up"] for t in side_rows]
            preds_cal = [
                calibrate_p_up(t["model_p_up"], up_factor=up_factor, down_factor=down_factor)
                for t in side_rows
            ]
            wins = sum(1 for t in side_rows if t["outcome"] == "up")
        else:
            preds_raw = [1 - t["model_p_up"] for t in side_rows]
            preds_cal = [
                1 - calibrate_p_up(t["model_p_up"], up_factor=up_factor, down_factor=down_factor)
                for t in side_rows
            ]
            wins = sum(1 for t in side_rows if t["outcome"] == "down")
        avg_raw = sum(preds_raw) / n
        avg_cal = sum(preds_cal) / n
        actual = wins / n
        print(f"{side.upper():5s} {n:>3d} {avg_raw:>9.3f} {avg_cal:>9.3f} {actual:>7.3f} "
              f"{actual - avg_raw:>+9.3f} {actual - avg_cal:>+9.3f}")


def log_loss(trades: list[dict], up_factor: float, down_factor: float) -> tuple[float, float]:
    """Compute raw and calibrated log-loss per trade (lower is better)."""
    raw_loss = cal_loss = 0.0
    for t in trades:
        p_raw = t["model_p_up"]
        p_cal = calibrate_p_up(p_raw, up_factor=up_factor, down_factor=down_factor)
        won = (t["outcome"] == t["side"])
        if t["side"] == "up":
            pw_raw, pw_cal = p_raw, p_cal
        else:
            pw_raw, pw_cal = 1 - p_raw, 1 - p_cal
        raw_loss += -math.log(max(1e-6, pw_raw if won else 1 - pw_raw))
        cal_loss += -math.log(max(1e-6, pw_cal if won else 1 - pw_cal))
    n = len(trades)
    return raw_loss / n, cal_loss / n


def simulate_filter(
    trades: list[dict],
    up_factor: float,
    down_factor: float,
    min_edge_up: float,
    min_edge_down: float,
) -> dict:
    """Replay trades under the given calibration and filter config."""
    kept = wins = 0
    pnl = 0.0
    for t in trades:
        p_cal = calibrate_p_up(t["model_p_up"], up_factor=up_factor, down_factor=down_factor)
        uask = t["up_ask"] or t["entry_price"]
        dask = t["down_ask"] or t["entry_price"]
        if t["side"] == "up":
            edge = p_cal - effective_ask(uask)
            if edge < min_edge_up:
                continue
        else:
            edge = (1 - p_cal) - effective_ask(dask)
            if edge < min_edge_down:
                continue
        kept += 1
        pnl += t["pnl"]
        if t["pnl"] > 0:
            wins += 1
    return {"n": kept, "wins": wins, "pnl": pnl, "avg": pnl / kept if kept else 0.0}


def main() -> None:
    trades = load_trades()
    print(f"Loaded {len(trades)} settled trades from {DB.name}")
    print(f"Active config: UP shrinkage={CALIBRATION_SHRINKAGE_UP}, "
          f"DOWN shrinkage={CALIBRATION_SHRINKAGE_DOWN}, "
          f"min_edge(up)={MIN_EDGE_THRESHOLD}, min_edge(down)={MIN_EDGE_THRESHOLD_DOWN}")
    print(f"Fee coeff={FEE_RATE}\n")

    print("=== Per-side calibration gap ===")
    per_side_calibration(trades, CALIBRATION_SHRINKAGE_UP, CALIBRATION_SHRINKAGE_DOWN)

    print("\n=== Log-loss per trade (lower = better) ===")
    raw_ll, cal_ll = log_loss(trades, CALIBRATION_SHRINKAGE_UP, CALIBRATION_SHRINKAGE_DOWN)
    print(f"raw:        {raw_ll:.4f}")
    print(f"calibrated: {cal_ll:.4f}  (delta {cal_ll - raw_ll:+.4f})")

    print("\n=== Filter replay with current calibration vs baseline ===")
    # Baseline = what actually happened (no calibration, min_edge_down=0.10)
    base = simulate_filter(trades, 1.0, 1.0, 0.03, 0.10)
    print(f"baseline (raw, min_down=0.10): n={base['n']:3d} pnl=${base['pnl']:.2f} avg=${base['avg']:.3f}")
    # Current config
    live = simulate_filter(
        trades,
        CALIBRATION_SHRINKAGE_UP,
        CALIBRATION_SHRINKAGE_DOWN,
        MIN_EDGE_THRESHOLD,
        MIN_EDGE_THRESHOLD_DOWN,
    )
    print(f"calibrated+current filters:    n={live['n']:3d} pnl=${live['pnl']:.2f} avg=${live['avg']:.3f}")
    print(f"delta: ${live['pnl'] - base['pnl']:+.2f}")

    print("\n=== DOWN-shrink x min_edge_down sweep (UP untouched) ===")
    print(f"{'k_down':>7s} {'min_down':>9s} {'n':>4s} {'win%':>6s} {'pnl':>9s} {'avg':>7s}")
    for k in (1.00, 0.75, 0.50, 0.30, 0.20):
        for med in (0.03, 0.05, 0.07, 0.10):
            r = simulate_filter(trades, 1.0, k, 0.03, med)
            if r["n"]:
                print(f"{k:>7.2f} {med:>9.2f} {r['n']:>4d} {r['wins']/r['n']*100:>5.1f}% "
                      f"{r['pnl']:>8.2f} {r['avg']:>7.3f}")


if __name__ == "__main__":
    main()
