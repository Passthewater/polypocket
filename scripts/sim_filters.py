"""Replay paper_trades.db under counterfactual filter rules.

Pulls every settled trade plus its decision snapshot, then evaluates a suite
of filter/resize rules. PnL for a kept trade equals the original PnL; for a
resized trade, pnl is pro-rated by size_ratio; a skipped trade contributes 0.

Usage: python scripts/sim_filters.py
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "paper_trades.db"
FEE_RATE = 0.072


@dataclass
class Trade:
    side: str
    entry_price: float
    size: float
    model_p_up: float
    edge: float
    outcome: str
    pnl: float
    t_remaining: float
    market_up_ask: float
    market_down_ask: float
    displacement: float


def load() -> list[Trade]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT t.side, t.entry_price, t.size, t.model_p_up, t.edge,
               t.outcome, t.pnl,
               s.t_remaining, s.up_ask, s.down_ask, s.displacement
        FROM trades t
        LEFT JOIN window_snapshots s
          ON t.window_slug = s.window_slug AND s.snapshot_type='decision'
        WHERE t.outcome IS NOT NULL
        ORDER BY t.timestamp
        """
    ).fetchall()
    conn.close()
    return [
        Trade(
            side=r["side"],
            entry_price=r["entry_price"],
            size=r["size"],
            model_p_up=r["model_p_up"] or 0.5,
            edge=r["edge"] or 0.0,
            outcome=r["outcome"],
            pnl=r["pnl"],
            t_remaining=r["t_remaining"] or 240,
            market_up_ask=r["up_ask"] or r["entry_price"],
            market_down_ask=r["down_ask"] or r["entry_price"],
            displacement=r["displacement"] or 0.0,
        )
        for r in rows
    ]


def pnl_at_size(t: Trade, new_size: float) -> float:
    """Recompute pnl if the size had been new_size instead of original."""
    won = t.outcome == t.side
    cost = t.entry_price * new_size
    fee = new_size * FEE_RATE * t.entry_price * (1 - t.entry_price)
    payout = (new_size - fee) if won else 0.0
    return payout - cost


def run_sim(name: str, trades: list[Trade], keep, resize=None) -> dict:
    """keep(t) -> bool; resize(t) -> new_size or None."""
    total_pnl = 0.0
    wins = losses = kept = 0
    for t in trades:
        if not keep(t):
            continue
        kept += 1
        new_size = resize(t) if resize else t.size
        pnl = pnl_at_size(t, new_size) if new_size != t.size else t.pnl
        total_pnl += pnl
        if pnl > 0:
            wins += 1
        else:
            losses += 1
    winrate = wins / kept if kept else 0
    return {
        "name": name,
        "trades": kept,
        "wins": wins,
        "losses": losses,
        "winrate": winrate,
        "pnl": total_pnl,
        "avg": total_pnl / kept if kept else 0,
    }


def shrink_p_up(p: float, factor: float) -> float:
    """Compress toward 0.5 by factor (factor=1 → unchanged, 0 → 0.5)."""
    return 0.5 + (p - 0.5) * factor


def effective_ask(price: float) -> float:
    return price / (1.0 - FEE_RATE * price * (1 - price))


def main() -> None:
    trades = load()
    print(f"Loaded {len(trades)} settled trades\n")

    sims = []

    # Baseline
    sims.append(run_sim("baseline", trades, lambda t: True))

    # 1. DOWN min edge ≥ 0.10
    sims.append(run_sim(
        "down_edge_0.10",
        trades,
        lambda t: not (t.side == "down" and t.edge < 0.10),
    ))

    # 2. Reject entry_price ≥ 0.70 on either side
    sims.append(run_sim(
        "price_cap_0.70",
        trades,
        lambda t: t.entry_price < 0.70,
    ))

    # 3. Reject entry_price ≥ 0.65
    sims.append(run_sim(
        "price_cap_0.65",
        trades,
        lambda t: t.entry_price < 0.65,
    ))

    # 4. Cap shares at 40 (pro-rate PnL)
    sims.append(run_sim(
        "share_cap_40",
        trades,
        lambda t: True,
        resize=lambda t: min(t.size, 40.0),
    ))

    # 5. Cap shares at 30
    sims.append(run_sim(
        "share_cap_30",
        trades,
        lambda t: True,
        resize=lambda t: min(t.size, 30.0),
    ))

    # 6. Floor entry_price at 0.30 (reject very low entries → oversized)
    sims.append(run_sim(
        "price_floor_0.30",
        trades,
        lambda t: t.entry_price >= 0.30,
    ))

    # 7. DOWN confidence tighter: require model_p_up ≤ 0.30
    sims.append(run_sim(
        "down_conf_0.70",
        trades,
        lambda t: not (t.side == "down" and t.model_p_up > 0.30),
    ))

    # 8. Combo: DOWN edge 0.10 + price cap 0.70
    sims.append(run_sim(
        "combo_down0.10_cap0.70",
        trades,
        lambda t: (t.entry_price < 0.70)
        and not (t.side == "down" and t.edge < 0.10),
    ))

    # 9. Combo full: DOWN edge 0.10 + price cap 0.70 + share cap 40
    sims.append(run_sim(
        "combo_full",
        trades,
        lambda t: (t.entry_price < 0.70)
        and not (t.side == "down" and t.edge < 0.10),
        resize=lambda t: min(t.size, 40.0),
    ))

    # 10. DOWN shrinkage: recompute edge with model_p_up shrunk toward 0.5
    #     Requires the trade to still pass min edge (0.03) under new edge.
    def shrink_keep(t, factor, min_edge=0.03):
        if t.side == "up":
            # UP also shrunk by same factor for fairness
            new_p = shrink_p_up(t.model_p_up, factor)
            new_edge = new_p - effective_ask(t.market_up_ask)
            return new_edge >= min_edge and new_p >= 0.70
        new_p = shrink_p_up(t.model_p_up, factor)
        new_edge = (1 - new_p) - effective_ask(t.market_down_ask)
        return new_edge >= min_edge and new_p <= 0.40

    for factor in (0.75, 0.50, 0.30):
        sims.append(run_sim(
            f"shrink_{factor:.2f}",
            trades,
            lambda t, f=factor: shrink_keep(t, f),
        ))

    # 11. DOWN-only shrinkage (UP left alone)
    def down_only_shrink(t, factor, min_edge=0.03):
        if t.side == "up":
            return True
        new_p = shrink_p_up(t.model_p_up, factor)
        new_edge = (1 - new_p) - effective_ask(t.market_down_ask)
        return new_edge >= min_edge and new_p <= 0.40

    for factor in (0.50, 0.30):
        sims.append(run_sim(
            f"down_shrink_{factor:.2f}",
            trades,
            lambda t, f=factor: down_only_shrink(t, f),
        ))

    # 12. MIN_ELAPSED heuristic: skip trades with t_remaining > 210
    #     (i.e., fired within first 30s after the 60s gate)
    sims.append(run_sim(
        "later_entry_210",
        trades,
        lambda t: t.t_remaining <= 210,
    ))

    # Kitchen sink
    sims.append(run_sim(
        "combo_kitchen",
        trades,
        lambda t: (t.entry_price < 0.70)
        and not (t.side == "down" and t.edge < 0.10)
        and not (t.side == "down" and t.model_p_up > 0.30),
        resize=lambda t: min(t.size, 40.0),
    ))

    # Print
    print(f"{'name':<28} {'n':>4} {'win%':>6} {'pnl':>9} {'avg':>7} {'vs base':>8}")
    print("-" * 72)
    base = sims[0]["pnl"]
    for s in sims:
        delta = s["pnl"] - base
        print(
            f"{s['name']:<28} {s['trades']:>4} {s['winrate']*100:>5.1f}% "
            f"{s['pnl']:>8.2f} {s['avg']:>7.3f} {delta:>+8.2f}"
        )


if __name__ == "__main__":
    main()
