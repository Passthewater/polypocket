"""Replay historical live trades through a candidate gate configuration.

Rewrites issue #11 item 2 as a GATE-ONLY replay. An earlier walk-the-book
fill model (see polypocket/fillmodel.py) was too optimistic: live IOCs have
their limit computed at submit time on a post-churn book, and the decision-
time bid snapshot can't predict matcher behavior 200-500ms later. Offline
simulation of fill prices was biased +$0.56/trade vs actuals on n=59.

This rewrite instead asks: "of the trades that actually fired historically,
which ones would a candidate {cushion, threshold, max_price} gate have
admitted, and what's their actual PnL?" No fill simulation. The gate is
re-evaluated on the decision-time snapshot + stored calibrated model_p_up,
and admitted trades contribute their actual historical entry_price and pnl
to the bootstrap.

IOC_BUFFER_TICKS is NOT a sweep knob here — changing it would change the
fill regime, and we can't project that offline. Defer to a live cohort
(issue #12).

Corpus: live_trades.db, settled trades whose decision snapshot has bids.
"""
import argparse
import json
import sqlite3
from dataclasses import dataclass

from polypocket.config import effective_ask

DEFAULT_DB = "live_trades.db"


@dataclass
class KeptTrade:
    tid: int
    side: str
    outcome: str | None
    model_p_up: float
    entry_price: float
    pnl: float


def _load_rows(db_path: str) -> list[sqlite3.Row]:
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        """SELECT t.id AS tid, t.window_slug, t.side, t.entry_price,
                  t.size, t.model_p_up, t.outcome, t.pnl, t.status,
                  s.up_ask, s.down_ask, s.up_bids_json, s.down_bids_json
             FROM trades t
             JOIN window_snapshots s
               ON s.window_slug = t.window_slug AND s.trade_fired = 1
            WHERE t.status = 'settled'
              AND t.pnl IS NOT NULL
              AND s.up_bids_json IS NOT NULL
              AND s.down_bids_json IS NOT NULL
              AND t.model_p_up IS NOT NULL
            ORDER BY t.id"""
    ).fetchall()
    c.close()
    return rows


def replay(
    db_path: str,
    cushion_ticks: int,
    threshold: float,
    max_price: float,
    threshold_down: float = 0.10,
    min_model_conf_up: float = 0.70,
    min_model_conf: float = 0.60,
) -> list[KeptTrade]:
    """Run gate with candidate knobs; return trades that would fire, with actual PnL."""
    rows = _load_rows(db_path)
    kept: list[KeptTrade] = []

    for r in rows:
        up_bids = json.loads(r["up_bids_json"])
        down_bids = json.loads(r["down_bids_json"])
        if not up_bids or not down_bids:
            continue

        model_p_up = r["model_p_up"]
        best_down_bid = max(float(b["price"]) for b in down_bids)
        up_entry_gate = min(0.99, (1.0 - best_down_bid) + cushion_ticks * 0.01)
        up_edge = model_p_up - effective_ask(up_entry_gate)

        best_up_bid = max(float(b["price"]) for b in up_bids)
        down_entry_gate = min(0.99, (1.0 - best_up_bid) + cushion_ticks * 0.01)
        down_edge = (1.0 - model_p_up) - effective_ask(down_entry_gate)

        up_ok = (
            model_p_up >= min_model_conf_up
            and up_entry_gate < max_price
            and up_edge >= threshold
            and up_edge >= down_edge
        )
        down_ok = (
            model_p_up <= (1.0 - min_model_conf)
            and down_entry_gate < max_price
            and down_edge >= threshold_down
        )

        if up_ok and r["side"] == "up":
            pass
        elif down_ok and r["side"] == "down":
            pass
        else:
            continue

        kept.append(
            KeptTrade(
                tid=r["tid"],
                side=r["side"],
                outcome=r["outcome"],
                model_p_up=model_p_up,
                entry_price=r["entry_price"],
                pnl=r["pnl"],
            )
        )

    return kept


def _print_summary(kept: list[KeptTrade], label: str) -> None:
    if not kept:
        print(f"{label}: 0 trades kept")
        return
    n = len(kept)
    total = sum(k.pnl for k in kept)
    wins = sum(1 for k in kept if k.outcome == k.side)
    up = [k for k in kept if k.side == "up"]
    down = [k for k in kept if k.side == "down"]
    up_pnl = sum(k.pnl for k in up)
    down_pnl = sum(k.pnl for k in down)
    print(
        f"{label}: n={n}  pnl={total:+.2f}  avg={total/n:+.3f}  "
        f"wins={wins} ({wins/n*100:.1f}%)  "
        f"UP n={len(up)} pnl={up_pnl:+.2f}  DOWN n={len(down)} pnl={down_pnl:+.2f}"
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--cushion-ticks", type=int, default=11)
    p.add_argument("--threshold", type=float, default=0.03)
    p.add_argument("--max-price", type=float, default=0.70)
    args = p.parse_args()

    kept = replay(
        db_path=args.db,
        cushion_ticks=args.cushion_ticks,
        threshold=args.threshold,
        max_price=args.max_price,
    )
    label = (
        f"cush={args.cushion_ticks} thr={args.threshold} cap={args.max_price}"
    )
    _print_summary(kept, label)


if __name__ == "__main__":
    main()
