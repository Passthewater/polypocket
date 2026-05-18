"""Train logistic p_up v3 on the n=5,275 paper post-G1 corpus.

Companion design: docs/plans/2026-05-17-logistic-p-up-v3-design.md
Companion impl:   docs/plans/2026-05-17-logistic-p-up-v3-implementation.md

Outputs the chosen variant's coefficients + a markdown report. Run with:

    python scripts/train_model_v3.py --variant both \
        --out-coefs polypocket/model_v3_coefs.candidate.json \
        --out-report scripts/_model_v3_training.md

Or per-variant:

    python scripts/train_model_v3.py --variant v0     ...
    python scripts/train_model_v3.py --variant v0.1   ...

Reproducibility: seed=42, time-series 5-fold CV inside the chronological 60/20/20
training slice. Standardization is fit once on the full training slice (v2's
documented choice; per-fold scaling delta was ε at n>1k per v2's ablation).
Re-running with the same --seed produces a bitwise-identical coefs JSON.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import StandardScaler


# --- Constants matching the polypocket runtime gate (replicated to avoid
#     importing the runtime, which would couple training to env vars). ---
SIGNAL_CUSHION_TICKS = 8  # polypocket/config.py:21
MAX_ENTRY_PRICE = 0.70    # config.py:30
MIN_MODEL_CONFIDENCE = 0.60       # config.py:41 (DOWN-side floor on 1-p_up)
MIN_MODEL_CONFIDENCE_UP = 0.75    # config.py:42
MIN_EDGE_THRESHOLD = 0.10         # config.py:10
MIN_EDGE_THRESHOLD_DOWN = 0.10    # config.py:26
MAX_EDGE_THRESHOLD_UP = 0.25      # config.py:36
FEE_RATE = 0.072                  # config.py:54
PAPER_POSITION_USDC = 10.0        # mid-range of MIN_POSITION_USDC..MAX_POSITION_USDC

# §Q4 acceptance gate (from the v3 design):
GATE_BIN_TOL_PT = 5.0              # |gap| ≤ 5pt per n≥30 bin
GATE_TAIL_8085_MIN_PT = -5.0       # 0.80-0.85 must be ≥ -5pt (beat v2's -10.2)
GATE_TAIL_8590_MIN_PT = -6.0       # 0.85-0.90 must be ≥ -6pt (beat v2's -13.8 by ≥8pt)
GATE_DOWN_TOL_PT = 5.0             # DOWN overall ∈ [-5pt, +5pt]
GATE_LOGLOSS_MARGIN_NATS = 0.005   # held-out log-loss < v2_holdout - 0.005

# Reliability bin edges (finer at the tail than v2's bins, per design §Q4).
BIN_EDGES = [0.50, 0.60, 0.70, 0.80, 0.85, 0.90, 1.001]
BIN_LABELS = ["0.50-0.60", "0.60-0.70", "0.70-0.80", "0.80-0.85", "0.85-0.90", "0.90-1.00"]

V0_FEATURES = ["z", "t_remaining", "sigma_5min", "market_p_up_normalized"]
V01_EXTRA_FEATURES = ["book_imbalance", "spread", "pre_decision_pmc_sigma", "z_times_market"]

V2_HOLDOUT_LOGLOSS = 0.4514  # from scripts/_model_v2_training.md:38


def effective_ask(price: float) -> float:
    """Break-even model probability to buy at `price` (fee inflation)."""
    return price / (1.0 - FEE_RATE * price * (1.0 - price))


def parse_bids(bids_json: str | None) -> list[dict]:
    if not bids_json:
        return []
    try:
        return json.loads(bids_json)
    except (json.JSONDecodeError, TypeError):
        return []


def parse_asks(asks_json: str | None) -> list[dict]:
    return parse_bids(asks_json)


def best_price(orders: list[dict]) -> float | None:
    if not orders:
        return None
    try:
        return max(float(o["price"]) for o in orders)
    except (KeyError, ValueError, TypeError):
        return None


def add_v0_features(df: pd.DataFrame) -> pd.DataFrame:
    """Build the four v0 features (matches v2's training shape)."""
    df = df.copy()
    df["z"] = df["displacement"] / (df["sigma_5min"] * np.sqrt(df["t_remaining"] / 300.0))
    # market_p_up_normalized already in parquet
    return df


def _pmc_from_sample(up_asks_json: str | None, down_asks_json: str | None) -> float | None:
    """Replicate the runtime's market_p_up_normalized from a book sample."""
    up_asks = parse_asks(up_asks_json)
    down_asks = parse_asks(down_asks_json)
    if not up_asks or not down_asks:
        return None
    up_ask = min(float(a["price"]) for a in up_asks)
    down_ask = min(float(a["price"]) for a in down_asks)
    denom = up_ask + down_ask
    if denom <= 0:
        return None
    return up_ask / denom


def compute_pre_decision_pmc_sigma(
    db_path: str,
    df: pd.DataFrame,
    *,
    min_samples: int = 3,
) -> pd.Series:
    """For each (window_slug, decision_ts), std of pmc over pre-decision samples.

    Falls back to sigma_5min when fewer than `min_samples` book samples are
    available pre-decision (per impl plan §Step 2.3).
    """
    out = np.full(len(df), np.nan, dtype=float)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        for i, row in df.iterrows():
            slug = row["window_slug"]
            ts_str = row["decision_timestamp"]
            try:
                ts_epoch = datetime.fromisoformat(ts_str).timestamp()
            except ValueError:
                ts_epoch = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").timestamp()
            samples = conn.execute(
                "SELECT up_asks_json, down_asks_json FROM window_book_samples "
                "WHERE window_slug = ? AND sampled_at < ?",
                (slug, ts_epoch),
            ).fetchall()
            pmcs = []
            for s in samples:
                p = _pmc_from_sample(s["up_asks_json"], s["down_asks_json"])
                if p is not None:
                    pmcs.append(p)
            if len(pmcs) >= min_samples:
                out[i] = float(np.std(pmcs))
            else:
                out[i] = float(row["sigma_5min"])  # fallback proxy
    return pd.Series(out, index=df.index, name="pre_decision_pmc_sigma")


def add_v01_features(df: pd.DataFrame, *, db_path: str) -> pd.DataFrame:
    """Build v0.1 features on top of v0. Mutates a copy."""
    df = add_v0_features(df).copy()
    # book_imbalance from top-of-book bid sizes
    def _imb(row):
        up_bids = parse_bids(row["up_bids_json"])
        down_bids = parse_bids(row["down_bids_json"])
        try:
            u = float(up_bids[0]["size"]) if up_bids else 0.0
            d = float(down_bids[0]["size"]) if down_bids else 0.0
        except (KeyError, ValueError, TypeError):
            return 0.0
        s = u + d
        return 0.0 if s <= 0 else (u - d) / s
    df["book_imbalance"] = df.apply(_imb, axis=1)
    df["spread"] = df["up_ask"] + df["down_ask"] - 1.0
    df["pre_decision_pmc_sigma"] = compute_pre_decision_pmc_sigma(db_path, df)
    df["z_times_market"] = df["z"] * df["market_p_up_normalized"]
    return df


def chrono_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Chronological 60/20/20 split on decision_timestamp."""
    df = df.sort_values("decision_timestamp").reset_index(drop=True)
    n = len(df)
    a = int(n * 0.6)
    b = int(n * 0.8)
    return df.iloc[:a].copy(), df.iloc[a:b].copy(), df.iloc[b:].copy()


def fit_logistic(
    X_train: np.ndarray,
    y_train: np.ndarray,
    *,
    timestamps: np.ndarray,
    c_grid: list[float] = [0.1, 1.0, 10.0, 100.0],
    seed: int = 42,
    n_splits: int = 5,
) -> tuple[LogisticRegression, float, dict[float, float]]:
    """Time-series CV grid search over L2 strength; pin C=10 on tie (v2 rule).

    Returns (fitted_model, chosen_C, per_C_logloss_means).
    """
    tscv = TimeSeriesSplit(n_splits=n_splits)
    cv_means: dict[float, float] = {}
    for C in c_grid:
        fold_losses = []
        for tr_idx, va_idx in tscv.split(X_train):
            m = LogisticRegression(
                C=C, penalty="l2", solver="lbfgs", max_iter=2000, random_state=seed
            )
            m.fit(X_train[tr_idx], y_train[tr_idx])
            p = m.predict_proba(X_train[va_idx])[:, 1]
            fold_losses.append(log_loss(y_train[va_idx], p, labels=[0, 1]))
        cv_means[C] = float(np.mean(fold_losses))
    # Pick best; tie-break toward C=10 if within 1e-4
    best_C = min(cv_means, key=cv_means.get)
    if 10.0 in cv_means and abs(cv_means[10.0] - cv_means[best_C]) < 1e-4:
        best_C = 10.0
    model = LogisticRegression(
        C=best_C, penalty="l2", solver="lbfgs", max_iter=2000, random_state=seed
    )
    model.fit(X_train, y_train)
    return model, best_C, cv_means


def reliability_table(
    p_pred: np.ndarray, y: np.ndarray, n_bootstrap: int = 1000, seed: int = 42
) -> list[dict]:
    """Per-bin reliability with bootstrap CI on actual win rate."""
    rng = np.random.default_rng(seed)
    rows = []
    for lo, hi, label in zip(BIN_EDGES[:-1], BIN_EDGES[1:], BIN_LABELS):
        mask = (p_pred >= lo) & (p_pred < hi)
        n = int(mask.sum())
        if n == 0:
            rows.append({"bin": label, "n": 0, "pred": None, "actual": None, "gap_pt": None, "ci": [None, None]})
            continue
        pred = float(p_pred[mask].mean())
        actual = float(y[mask].mean())
        y_bin = y[mask]
        boot = rng.choice(y_bin, size=(n_bootstrap, n), replace=True).mean(axis=1)
        ci = (float(np.quantile(boot, 0.025)), float(np.quantile(boot, 0.975)))
        rows.append({
            "bin": label, "n": n, "pred": pred, "actual": actual,
            "gap_pt": (actual - pred) * 100, "ci": list(ci),
        })
    return rows


def side_overall_gap(
    p_pred: np.ndarray, y: np.ndarray, ask_up: np.ndarray, ask_down: np.ndarray
) -> dict:
    """Decompose mean-gap by which side the bot would have favored.

    Side rule mirrors signal.py: UP-bias when p_up >= MIN_MODEL_CONFIDENCE_UP,
    DOWN-bias when p_up <= 1 - MIN_MODEL_CONFIDENCE. Rows in neither band are
    'no_side' and excluded from the side decomposition.
    """
    up_mask = p_pred >= MIN_MODEL_CONFIDENCE_UP
    down_mask = p_pred <= (1.0 - MIN_MODEL_CONFIDENCE)
    out = {}
    for label, mask in [("up", up_mask), ("down", down_mask)]:
        n = int(mask.sum())
        if n == 0:
            out[label] = {"n": 0, "mean_pred": None, "mean_actual": None, "gap_pt": None}
            continue
        if label == "up":
            mean_pred = float(p_pred[mask].mean())
            mean_actual = float(y[mask].mean())
        else:
            mean_pred = float((1.0 - p_pred[mask]).mean())
            # For DOWN-side, "winning" means outcome=0 (down). y is up=1, so flip.
            mean_actual = float((1.0 - y[mask]).mean())
        out[label] = {
            "n": n, "mean_pred": mean_pred, "mean_actual": mean_actual,
            "gap_pt": (mean_actual - mean_pred) * 100,
        }
    return out


def simulate_pnl(
    p_pred: np.ndarray,
    y: np.ndarray,
    up_ask: np.ndarray,
    down_ask: np.ndarray,
    up_bids_json: list[str | None],
    down_bids_json: list[str | None],
    *,
    notional: float = PAPER_POSITION_USDC,
) -> tuple[float, np.ndarray]:
    """Replicate signal.py's gate + book.py's pair-merge entry, sum PnL per fire.

    Returns (total_pnl, per_row_pnl_array). Non-firing rows contribute 0.
    """
    pnl = np.zeros(len(p_pred), dtype=float)
    for i in range(len(p_pred)):
        ua = float(up_ask[i])
        da = float(down_ask[i])
        if not (0 < ua <= 1 and 0 < da <= 1):
            continue
        ub = parse_bids(up_bids_json[i])
        db = parse_bids(down_bids_json[i])
        best_down_bid = best_price(db)
        best_up_bid = best_price(ub)
        up_entry = (
            min(0.99, (1.0 - best_down_bid) + SIGNAL_CUSHION_TICKS * 0.01)
            if best_down_bid is not None else ua
        )
        down_entry = (
            min(0.99, (1.0 - best_up_bid) + SIGNAL_CUSHION_TICKS * 0.01)
            if best_up_bid is not None else da
        )
        p = float(p_pred[i])
        up_edge = p - effective_ask(up_entry)
        down_edge = (1.0 - p) - effective_ask(down_entry)
        up_aligned = p >= MIN_MODEL_CONFIDENCE_UP
        down_aligned = p <= (1.0 - MIN_MODEL_CONFIDENCE)
        up_price_ok = up_entry < MAX_ENTRY_PRICE
        down_price_ok = down_entry < MAX_ENTRY_PRICE
        side, entry = None, None
        if (up_aligned and up_price_ok
                and up_edge >= MIN_EDGE_THRESHOLD
                and up_edge < MAX_EDGE_THRESHOLD_UP
                and up_edge >= down_edge):
            side, entry = "up", up_entry
        elif (down_aligned and down_price_ok and down_edge >= MIN_EDGE_THRESHOLD_DOWN):
            side, entry = "down", down_entry
        if side is None:
            continue
        size = notional / entry  # shares purchased
        won = (side == "up" and y[i] == 1) or (side == "down" and y[i] == 0)
        # Polymarket v2 charges fees in shares on the BUY side: you pay
        # `notional` USDC and receive `size - fee_shares` shares of the bought
        # outcome. On a win each share pays $1; on a loss it pays $0.
        fee_shares = size * FEE_RATE * entry * (1.0 - entry)
        if won:
            pnl[i] = (size - fee_shares) * 1.0 - notional
        else:
            pnl[i] = -notional
    return float(pnl.sum()), pnl


def bootstrap_pnl_delta_ci(
    pnl_a: np.ndarray, pnl_b: np.ndarray, n_bootstrap: int = 1000, seed: int = 42
) -> tuple[float, float]:
    """95% CI on (A - B) total PnL, paired bootstrap on row indices."""
    rng = np.random.default_rng(seed)
    n = len(pnl_a)
    diffs = pnl_a - pnl_b
    sums = rng.choice(diffs, size=(n_bootstrap, n), replace=True).sum(axis=1)
    return float(np.quantile(sums, 0.025)), float(np.quantile(sums, 0.975))


def apply_q4_gate(
    reliability: list[dict],
    side_gap: dict,
    holdout_logloss: float,
    v2_holdout_logloss: float,
    pnl_delta_ci: tuple[float, float],
) -> tuple[bool, list[str]]:
    """Apply the design's §Q4 acceptance gate. Returns (ship_ok, failures)."""
    failures = []
    # All n>=30 bins within +/-5pt
    for r in reliability:
        if r["n"] >= 30 and r["gap_pt"] is not None:
            if abs(r["gap_pt"]) > GATE_BIN_TOL_PT:
                failures.append(f"bin {r['bin']} gap {r['gap_pt']:+.1f}pt outside +/-{GATE_BIN_TOL_PT}pt (n={r['n']})")
    # Tail-bin specific
    for r in reliability:
        if r["bin"] == "0.80-0.85" and r["n"] >= 30 and r["gap_pt"] is not None:
            if r["gap_pt"] < GATE_TAIL_8085_MIN_PT:
                failures.append(f"bin 0.80-0.85 gap {r['gap_pt']:+.1f}pt < required {GATE_TAIL_8085_MIN_PT:+.1f}pt")
        if r["bin"] == "0.85-0.90" and r["n"] >= 30 and r["gap_pt"] is not None:
            if r["gap_pt"] < GATE_TAIL_8590_MIN_PT:
                failures.append(f"bin 0.85-0.90 gap {r['gap_pt']:+.1f}pt < required {GATE_TAIL_8590_MIN_PT:+.1f}pt")
    # DOWN overall
    dg = side_gap.get("down", {}).get("gap_pt")
    if dg is not None and abs(dg) > GATE_DOWN_TOL_PT:
        failures.append(f"DOWN overall gap {dg:+.1f}pt outside +/-{GATE_DOWN_TOL_PT}pt")
    # Log-loss margin vs v2
    if holdout_logloss > v2_holdout_logloss - GATE_LOGLOSS_MARGIN_NATS:
        failures.append(
            f"held-out log-loss {holdout_logloss:.4f} not better than v2 ({v2_holdout_logloss:.4f}) by >={GATE_LOGLOSS_MARGIN_NATS} nats"
        )
    # PnL veto
    lo, hi = pnl_delta_ci
    if hi < 0:
        failures.append(f"PnL veto: 95% CI on (v3 - v2) total PnL = [{lo:+.2f}, {hi:+.2f}] entirely below zero")
    return (len(failures) == 0, failures)


def compute_v2_predictions(df: pd.DataFrame, v2_coefs_path: str) -> np.ndarray:
    """Re-derive v2's predicted p_up from features (avoids ledger dependency)."""
    with open(v2_coefs_path) as f:
        c = json.load(f)
    features = c["features"]
    mean = np.array(c["scaler_mean"])
    scale = np.array(c["scaler_scale"])
    coef = np.array(c["logistic_coef"])
    intercept = float(c["logistic_intercept"])
    # Build the v2 feature columns on df (needs z built first).
    df = add_v0_features(df)
    X = df[features].to_numpy()
    Xs = (X - mean) / scale
    logits = Xs @ coef + intercept
    return 1.0 / (1.0 + np.exp(-logits))


def train_variant(
    corpus_full: pd.DataFrame,
    variant: str,
    *,
    db_path: str,
    seed: int = 42,
    v2_coefs_path: str = "polypocket/model_v2_coefs.json",
) -> dict[str, Any]:
    """Train one variant end-to-end. Returns a dict with model artifacts + metrics."""
    if variant == "v0":
        df = add_v0_features(corpus_full).reset_index(drop=True)
        features = list(V0_FEATURES)
    elif variant == "v0.1":
        sub = corpus_full[
            corpus_full["up_bids_json"].notna() & corpus_full["down_bids_json"].notna()
        ].reset_index(drop=True)
        df = add_v01_features(sub, db_path=db_path).reset_index(drop=True)
        features = list(V0_FEATURES) + list(V01_EXTRA_FEATURES)
    else:
        raise ValueError(f"unknown variant: {variant}")

    train, cal, heldout = chrono_split(df)
    y_train = train["outcome_int"].to_numpy()
    y_heldout = heldout["outcome_int"].to_numpy()

    X_train_raw = train[features].to_numpy()
    X_heldout_raw = heldout[features].to_numpy()
    scaler = StandardScaler().fit(X_train_raw)
    X_train = scaler.transform(X_train_raw)
    X_heldout = scaler.transform(X_heldout_raw)

    timestamps = pd.to_datetime(train["decision_timestamp"]).to_numpy()
    model, chosen_C, cv_means = fit_logistic(
        X_train, y_train, timestamps=timestamps, seed=seed
    )

    p_heldout = model.predict_proba(X_heldout)[:, 1]
    holdout_logloss = float(log_loss(y_heldout, p_heldout, labels=[0, 1]))
    holdout_brier = float(brier_score_loss(y_heldout, p_heldout))

    # Reliability + side gap
    reliability = reliability_table(p_heldout, y_heldout, seed=seed)
    side_gap = side_overall_gap(
        p_heldout, y_heldout,
        heldout["up_ask"].to_numpy(), heldout["down_ask"].to_numpy(),
    )

    # v2-on-v3-holdout for the v2-vs-v3 head-to-head + gate
    p_v2_heldout = compute_v2_predictions(heldout, v2_coefs_path)
    v2_holdout_logloss = float(log_loss(y_heldout, p_v2_heldout, labels=[0, 1]))
    v2_reliability = reliability_table(p_v2_heldout, y_heldout, seed=seed)
    v2_side_gap = side_overall_gap(
        p_v2_heldout, y_heldout,
        heldout["up_ask"].to_numpy(), heldout["down_ask"].to_numpy(),
    )

    # PnL simulation: v3 vs v2 under the runtime gate, paired bootstrap
    up_ask = heldout["up_ask"].to_numpy()
    down_ask = heldout["down_ask"].to_numpy()
    ub_json = heldout["up_bids_json"].tolist()
    db_json = heldout["down_bids_json"].tolist()
    v3_total, v3_pnl = simulate_pnl(p_heldout, y_heldout, up_ask, down_ask, ub_json, db_json)
    v2_total, v2_pnl = simulate_pnl(p_v2_heldout, y_heldout, up_ask, down_ask, ub_json, db_json)
    pnl_delta_ci = bootstrap_pnl_delta_ci(v3_pnl, v2_pnl, seed=seed)

    # §Q4 gate
    ship_ok, failures = apply_q4_gate(
        reliability, side_gap, holdout_logloss, v2_holdout_logloss, pnl_delta_ci
    )

    # Isotonic ablation (report-only, may override default if it wins per design §8)
    cal_X = scaler.transform(cal[features].to_numpy())
    p_cal = model.predict_proba(cal_X)[:, 1]
    iso = IsotonicRegression(out_of_bounds="clip").fit(p_cal, cal["outcome_int"].to_numpy())
    p_heldout_iso = iso.predict(p_heldout)
    iso_logloss = float(log_loss(y_heldout, np.clip(p_heldout_iso, 1e-6, 1 - 1e-6), labels=[0, 1]))
    iso_reliability = reliability_table(p_heldout_iso, y_heldout, seed=seed)

    # Feature hull (min/max from training slice, for runtime out-of-domain check)
    hull = {f: [float(train[f].min()), float(train[f].max())] for f in features}

    # Per-feature ablation for v0.1 only (drop-one)
    per_feature_ablation: dict[str, dict] = {}
    if variant == "v0.1":
        for drop in V01_EXTRA_FEATURES:
            keep = [f for f in features if f != drop]
            # Re-fit a fresh scaler+model on the reduced feature set
            sub_scaler = StandardScaler().fit(train[keep].to_numpy())
            sub_X_tr = sub_scaler.transform(train[keep].to_numpy())
            sub_X_he = sub_scaler.transform(heldout[keep].to_numpy())
            sub_m = LogisticRegression(
                C=chosen_C, penalty="l2", solver="lbfgs", max_iter=2000, random_state=seed
            )
            sub_m.fit(sub_X_tr, y_train)
            p_sub = sub_m.predict_proba(sub_X_he)[:, 1]
            sub_loss = float(log_loss(y_heldout, p_sub, labels=[0, 1]))
            per_feature_ablation[drop] = {
                "dropped": drop,
                "remaining_features": keep,
                "holdout_logloss": sub_loss,
                "delta_vs_full": sub_loss - holdout_logloss,
            }

    return {
        "variant": variant,
        "features": features,
        "n_train": int(len(train)),
        "n_cal": int(len(cal)),
        "n_heldout": int(len(heldout)),
        "date_ranges": {
            "train": [str(train["decision_timestamp"].iloc[0]), str(train["decision_timestamp"].iloc[-1])],
            "cal":   [str(cal["decision_timestamp"].iloc[0]), str(cal["decision_timestamp"].iloc[-1])],
            "heldout": [str(heldout["decision_timestamp"].iloc[0]), str(heldout["decision_timestamp"].iloc[-1])],
        },
        "base_rate_train": float(y_train.mean()),
        "base_rate_heldout": float(y_heldout.mean()),
        "chosen_C": chosen_C,
        "cv_means": cv_means,
        "holdout_logloss": holdout_logloss,
        "holdout_brier": holdout_brier,
        "reliability": reliability,
        "side_gap": side_gap,
        "v2_holdout_logloss": v2_holdout_logloss,
        "v2_reliability": v2_reliability,
        "v2_side_gap": v2_side_gap,
        "pnl_v3_total": v3_total,
        "pnl_v2_total": v2_total,
        "pnl_delta_ci": list(pnl_delta_ci),
        "iso_holdout_logloss": iso_logloss,
        "iso_reliability": iso_reliability,
        "ship_ok": ship_ok,
        "gate_failures": failures,
        "scaler_mean": scaler.mean_.tolist(),
        "scaler_scale": scaler.scale_.tolist(),
        "logistic_coef": model.coef_[0].tolist(),
        "logistic_intercept": float(model.intercept_[0]),
        "feature_hull": hull,
        "per_feature_ablation": per_feature_ablation,
    }


def write_coefs_json(result: dict, path: str, trained_at: str, *, git_sha: str | None = None) -> None:
    """Write a v2-compatible coefs JSON with a v3 metadata block."""
    payload = {
        "model_version": "v3",
        "trained_on_git_sha": git_sha,
        "trained_at": trained_at,
        "shipping_config": f"{result['variant']}+no-iso",
        "corpus": {
            "source": "paper post-G1",
            "total_n": result["n_train"] + result["n_cal"] + result["n_heldout"],
            "train_n": result["n_train"],
            "cal_n": result["n_cal"],
            "held_n": result["n_heldout"],
            "held_dates": result["date_ranges"]["heldout"],
            "base_rate_train": result["base_rate_train"],
            "base_rate_held": result["base_rate_heldout"],
        },
        "features": result["features"],
        "scaler_mean": result["scaler_mean"],
        "scaler_scale": result["scaler_scale"],
        "logistic_coef": result["logistic_coef"],
        "logistic_intercept": result["logistic_intercept"],
        "logistic_C": result["chosen_C"],
        "isotonic_x": None,
        "isotonic_y": None,
        "feature_hull": result["feature_hull"],
        "held_out_metrics": {
            "log_loss_v3": result["holdout_logloss"],
            "log_loss_v2_on_v3_holdout": result["v2_holdout_logloss"],
            "brier_v3": result["holdout_brier"],
            "reliability_v3": result["reliability"],
            "reliability_v2": result["v2_reliability"],
            "side_gap_v3": result["side_gap"],
            "side_gap_v2": result["v2_side_gap"],
            "ev_v3_total_pnl": result["pnl_v3_total"],
            "ev_v2_total_pnl": result["pnl_v2_total"],
            "ev_delta_ci": result["pnl_delta_ci"],
            "ev_veto": result["pnl_delta_ci"][1] < 0,
            "gate_pass": result["ship_ok"],
            "gate_failures": result["gate_failures"],
        },
        "ablations": {
            "no_isotonic_log_loss": result["holdout_logloss"],
            "with_isotonic_log_loss": result["iso_holdout_logloss"],
            "per_feature": result["per_feature_ablation"],
        },
        "metadata": {
            "variant": result["variant"],
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=False)


def _fmt_reliability_table(rows: list[dict]) -> str:
    lines = [
        f"| bin | n | mean_pred | actual | gap_pt | 95% CI |",
        f"|---|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        if r["n"] == 0:
            lines.append(f"| {r['bin']} | 0 | — | — | — | — |")
        else:
            lines.append(
                f"| {r['bin']} | {r['n']} | {r['pred']:.3f} | {r['actual']:.3f} | "
                f"{r['gap_pt']:+.1f} | [{r['ci'][0]:.3f}, {r['ci'][1]:.3f}] |"
            )
    return "\n".join(lines)


def write_report(results: list[dict], path: str, *, winner_variant: str | None) -> None:
    lines = []
    lines.append("# v3 logistic p_up training report")
    lines.append("")
    lines.append("_seed=42; sklearn LogisticRegression L2; TimeSeriesSplit n_splits=5_")
    lines.append("")
    if winner_variant is None:
        lines.append("## Gate verdict: ship_ok = False (no variant passed §Q4)")
    else:
        lines.append(f"## Gate verdict: ship_ok = True — shipped variant: **{winner_variant}**")
    lines.append("")
    for r in results:
        lines.append(f"## Variant: `{r['variant']}` ({'PASS' if r['ship_ok'] else 'FAIL'})")
        lines.append("")
        lines.append(f"- Features: {r['features']}")
        lines.append(f"- N train={r['n_train']} cal={r['n_cal']} heldout={r['n_heldout']}")
        lines.append(f"- train dates: {r['date_ranges']['train'][0]} ->{r['date_ranges']['train'][1]}")
        lines.append(f"- heldout dates: {r['date_ranges']['heldout'][0]} ->{r['date_ranges']['heldout'][1]}")
        lines.append(f"- base rate (up): train={r['base_rate_train']:.3f}  held={r['base_rate_heldout']:.3f}")
        lines.append(f"- chosen L2 C = {r['chosen_C']}; CV means: {r['cv_means']}")
        lines.append("")
        lines.append(f"### Held-out metrics")
        lines.append(f"- v3 log-loss = {r['holdout_logloss']:.4f}; v3 Brier = {r['holdout_brier']:.4f}")
        lines.append(f"- v2-on-v3-holdout log-loss = {r['v2_holdout_logloss']:.4f}")
        lines.append(f"- v2-on-v2-holdout log-loss (reference, _model_v2_training.md) = {V2_HOLDOUT_LOGLOSS:.4f}")
        lines.append(f"- delta (v3 - v2) on v3 holdout = {r['holdout_logloss']-r['v2_holdout_logloss']:+.4f} nats (required ≤ -0.005 for gate)")
        lines.append("")
        lines.append("### v3 reliability (held-out)")
        lines.append(_fmt_reliability_table(r["reliability"]))
        lines.append("")
        lines.append("### v2 reliability on the same held-out rows")
        lines.append(_fmt_reliability_table(r["v2_reliability"]))
        lines.append("")
        lines.append(f"### Side decomposition (gate-aligned)")
        for side in ("up", "down"):
            sg = r["side_gap"][side]
            v2sg = r["v2_side_gap"][side]
            if sg["n"] == 0:
                lines.append(f"- {side.upper()}-bias: n=0")
                continue
            lines.append(
                f"- {side.upper()}-bias v3: n={sg['n']} mean_pred={sg['mean_pred']:.3f} "
                f"actual={sg['mean_actual']:.3f} gap={sg['gap_pt']:+.1f}pt | "
                f"v2: n={v2sg['n']} gap={v2sg['gap_pt']:+.1f}pt"
            )
        lines.append("")
        lines.append("### Simulated EV under live gate (held-out)")
        lines.append(f"- v3 total PnL: ${r['pnl_v3_total']:+.2f}")
        lines.append(f"- v2 total PnL: ${r['pnl_v2_total']:+.2f}")
        lines.append(f"- delta (v3 - v2) 95% CI: [${r['pnl_delta_ci'][0]:+.2f}, ${r['pnl_delta_ci'][1]:+.2f}]")
        lines.append(f"- EV veto: {r['pnl_delta_ci'][1] < 0}")
        lines.append("")
        lines.append("### Isotonic ablation")
        lines.append(f"- no-iso log-loss: {r['holdout_logloss']:.4f}")
        lines.append(f"- with-iso log-loss: {r['iso_holdout_logloss']:.4f}")
        lines.append(f"- delta (iso - no-iso) = {r['iso_holdout_logloss']-r['holdout_logloss']:+.4f} nats "
                     f"(ship iso only if ≤ -0.01 AND iso bin gate passes)")
        lines.append("")
        if r["variant"] == "v0.1" and r["per_feature_ablation"]:
            lines.append("### Per-feature ablation (drop-one, retrain at chosen C)")
            lines.append("| dropped | held-out log-loss | Δ vs full |")
            lines.append("|---|---:|---:|")
            for k, v in r["per_feature_ablation"].items():
                lines.append(f"| {k} | {v['holdout_logloss']:.4f} | {v['delta_vs_full']:+.4f} |")
            lines.append("")
        if r["gate_failures"]:
            lines.append("### Gate failures")
            for f in r["gate_failures"]:
                lines.append(f"- {f}")
            lines.append("")
    lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def pick_winner(results: list[dict]) -> str | None:
    """Mechanical winner selection per impl plan §Step 4.

    1. If both pass §Q4: keep v0 (simpler/larger support), unless v0.1 beats v0
       on 0.80-0.85 / 0.85-0.90 bins by ≥2pt — then ship v0.1.
    2. If only one passes: ship that one.
    3. If neither passes: return None (halt, surface to user).
    """
    by_v = {r["variant"]: r for r in results}
    v0 = by_v.get("v0")
    v01 = by_v.get("v0.1")

    if v0 and v01 and v0["ship_ok"] and v01["ship_ok"]:
        def _bin_gap(r, label):
            for b in r["reliability"]:
                if b["bin"] == label and b["n"] >= 30 and b["gap_pt"] is not None:
                    return b["gap_pt"]
            return None
        margins = []
        for label in ("0.80-0.85", "0.85-0.90"):
            g0 = _bin_gap(v0, label)
            g01 = _bin_gap(v01, label)
            if g0 is not None and g01 is not None:
                margins.append(g01 - g0)
        if margins and all(m >= 2.0 for m in margins):
            return "v0.1"
        return "v0"

    if v0 and v0["ship_ok"]:
        return "v0"
    if v01 and v01["ship_ok"]:
        return "v0.1"
    return None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--corpus", default="_bmad-output/v3_corpus.parquet")
    p.add_argument("--db", default="paper_trades.db", help="SQLite path for window_book_samples lookups (v0.1 only)")
    p.add_argument("--variant", choices=["v0", "v0.1", "both"], default="both")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out-coefs", default="polypocket/model_v3_coefs.candidate.json")
    p.add_argument("--out-report", default="scripts/_model_v3_training.md")
    p.add_argument("--baseline-coefs", default="polypocket/model_v2_coefs.json")
    args = p.parse_args()

    corpus = pd.read_parquet(args.corpus)
    # Filter to t_remaining > 0 (already in exporter, defense-in-depth)
    corpus = corpus[corpus["t_remaining"] > 0].reset_index(drop=True)
    print(f"Loaded corpus: {len(corpus)} rows, "
          f"{corpus['decision_timestamp'].min()} ->{corpus['decision_timestamp'].max()}")

    variants = ["v0", "v0.1"] if args.variant == "both" else [args.variant]
    results = []
    for v in variants:
        print(f"\n--- Training variant: {v} ---")
        r = train_variant(corpus, v, db_path=args.db, seed=args.seed, v2_coefs_path=args.baseline_coefs)
        print(f"  ship_ok={r['ship_ok']}, holdout_logloss={r['holdout_logloss']:.4f}, "
              f"PnL v3=${r['pnl_v3_total']:+.2f}, v2=${r['pnl_v2_total']:+.2f}")
        if r["gate_failures"]:
            print(f"  failures: {r['gate_failures']}")
        results.append(r)

    winner = pick_winner(results)
    trained_at = datetime.utcnow().isoformat() + "Z"
    write_report(results, args.out_report, winner_variant=winner)
    print(f"\nReport written to {args.out_report}")

    if winner is not None:
        winning = next(r for r in results if r["variant"] == winner)
        write_coefs_json(winning, args.out_coefs, trained_at)
        print(f"Coefs written to {args.out_coefs} (variant={winner})")
        return 0
    else:
        print("NO VARIANT PASSED §Q4. No coefs file written.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
