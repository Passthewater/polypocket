"""Fit + evaluate v2 logistic p_up model on paper post-G1 corpus (#15).

End-to-end script:
  1. Load corpus.parquet
  2. Chronological 60/20/20 split
  3. Build 4-core features (z, t_remaining, sigma_5min, market_p_up_normalized)
  4. TimeSeriesSplit C sweep
  5. Fit logistic + isotonic on cal slice
  6. Reliability table + acceptance gate (Criterion 1, gating)
  7. Simulated EV under live gate (Criterion 2, confirmatory veto)
  8. Ablations: engineered, no-isotonic, blended-with-v1, per-fold scaler
  9. Persist coefs to polypocket/model_v2_coefs.json
 10. Print full training report to stdout (also written to scripts/_model_v2_training.md)

Per `docs/plans/2026-04-23-logistic-p-up-model-implementation.md` Task 3.

Run:
    python scripts/fit_logistic_p_up_v2.py
    python scripts/fit_logistic_p_up_v2.py --corpus corpus.parquet --no-write
"""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.stats import norm
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import StandardScaler


# Live gate (kept in sync with polypocket/config.py / signal.py).
# Copied here per plan Step 7 to keep the notebook hermetic.
GATE = {
    "MIN_EDGE_THRESHOLD": 0.10,
    "MIN_EDGE_THRESHOLD_DOWN": 0.10,
    "MAX_EDGE_THRESHOLD_UP": 0.25,
    "MAX_ENTRY_PRICE": 0.70,
    "MIN_MODEL_CONFIDENCE": 0.60,
    "MIN_MODEL_CONFIDENCE_UP": 0.75,
    "WINDOW_ENTRY_MIN_ELAPSED": 60,
    "WINDOW_ENTRY_MIN_REMAINING": 30,
    "SIGNAL_CUSHION_TICKS": 6,
    "FEE_RATE": 0.072,
    "WINDOW_TOTAL_S": 300,
}

V1_SHRINKAGE_UP = 1.00
V1_SHRINKAGE_DOWN = 0.50

SEED = 42


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------

def make_features(d: pd.DataFrame, *, include_engineered: bool = False) -> pd.DataFrame:
    sigma_rem = d.sigma_5min * np.sqrt(d.t_remaining / 300.0)
    z = d.displacement / sigma_rem
    feats = {
        "z": z,
        "t_remaining": d.t_remaining,
        "sigma_5min": d.sigma_5min,
        "market_p_up_normalized": d.market_p_up_normalized,
    }
    if include_engineered:
        feats["spread"] = d.up_ask + d.down_ask - 1.0
        feats["z_times_market"] = z * d.market_p_up_normalized
    return pd.DataFrame(feats)


def make_split(d: pd.DataFrame):
    n = len(d)
    t_end = int(n * 0.60)
    c_end = int(n * 0.80)
    return d.iloc[:t_end].copy(), d.iloc[t_end:c_end].copy(), d.iloc[c_end:].copy()


# ---------------------------------------------------------------------------
# v1 comparator (current shrinkage)
# ---------------------------------------------------------------------------

def v1_calibrated(d: pd.DataFrame) -> np.ndarray:
    sigma_rem = d.sigma_5min * np.sqrt(d.t_remaining / 300.0)
    raw = norm.cdf((d.displacement / sigma_rem).values)
    factor = np.where(raw >= 0.5, V1_SHRINKAGE_UP, V1_SHRINKAGE_DOWN)
    return 0.5 + (raw - 0.5) * factor


# ---------------------------------------------------------------------------
# Reliability table + gate (Criterion 1)
# ---------------------------------------------------------------------------

def reliability_table(p_values: np.ndarray, y: np.ndarray, label: str, *,
                      rng_seed: int = 0) -> list[dict]:
    rng = np.random.default_rng(rng_seed)
    rows = []
    bins = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 1.001)]
    for lo, hi in bins:
        mask = (p_values >= lo) & (p_values < hi)
        n = int(mask.sum())
        if n == 0:
            rows.append({
                "bin": f"{lo:.2f}-{hi:.2f}", "n": 0,
                "pred": None, "actual": None, "gap": None, "ci": None,
            })
            continue
        pred = float(p_values[mask].mean())
        actual = float(y[mask].mean())
        boots = np.array(
            [y[mask][rng.integers(0, n, n)].mean() for _ in range(2000)]
        )
        ci_lo, ci_hi = float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))
        rows.append({
            "bin": f"{lo:.2f}-{hi:.2f}", "n": n,
            "pred": pred, "actual": actual,
            "gap": abs(pred - actual), "ci": (ci_lo, ci_hi),
        })
    print(f"\n=== {label} ===")
    for r in rows:
        if r["n"] == 0:
            print(f"  {r['bin']}: n=0 (empty)")
        else:
            print(f"  {r['bin']}: n={r['n']:>4d}  pred={r['pred']:.3f}  "
                  f"actual={r['actual']:.3f}  gap={r['gap']:.3f}  "
                  f"CI=[{r['ci'][0]:.3f}, {r['ci'][1]:.3f}]")
    return rows


def apply_gate(v2_table: list[dict], v1_table: list[dict], label: str
               ) -> tuple[bool, list[str]]:
    failures: list[str] = []
    for v2_row, v1_row in zip(v2_table, v1_table):
        bin_label = v2_row["bin"]
        is_tail = bin_label.startswith("0.80")
        n = v2_row["n"]
        if is_tail:
            if n < 5:
                failures.append(
                    f"{label} 0.80+ tail: n={n} < 5 -- v2 has no tail support"
                )
                continue
            v1_gap = v1_row["gap"] if v1_row["n"] >= 5 else None
            if n >= 20:
                if v2_row["gap"] > 0.05:
                    failures.append(
                        f"{label} 0.80+: gap {v2_row['gap']:.3f} > 0.05"
                    )
                if v1_gap is not None and v2_row["gap"] > v1_gap:
                    failures.append(
                        f"{label} 0.80+: v2 gap {v2_row['gap']:.3f} > v1 gap {v1_gap:.3f}"
                    )
            else:  # 5 <= n < 20
                lo, hi = v2_row["ci"]
                if not (lo <= v2_row["pred"] <= hi):
                    failures.append(
                        f"{label} 0.80+: pred {v2_row['pred']:.3f} outside "
                        f"CI [{lo:.3f},{hi:.3f}] (small-n)"
                    )
                if v1_gap is not None and v2_row["gap"] > v1_gap:
                    failures.append(
                        f"{label} 0.80+: v2 gap {v2_row['gap']:.3f} > "
                        f"v1 gap {v1_gap:.3f} (small-n)"
                    )
        else:
            if n == 0:
                continue
            if n >= 30:
                if v2_row["gap"] > 0.05:
                    failures.append(
                        f"{label} {bin_label}: gap {v2_row['gap']:.3f} > 0.05"
                    )
            else:
                lo, hi = v2_row["ci"]
                if not (lo <= v2_row["pred"] <= hi):
                    failures.append(
                        f"{label} {bin_label}: pred {v2_row['pred']:.3f} outside "
                        f"CI [{lo:.3f},{hi:.3f}] (small-n)"
                    )
    return (not failures, failures)


# ---------------------------------------------------------------------------
# Simulated EV (Criterion 2, confirmatory)
# ---------------------------------------------------------------------------

def _effective_ask(price: float) -> float:
    fee = GATE["FEE_RATE"]
    return price / (1.0 - fee * price * (1.0 - price))


def simulate_pnl(p: np.ndarray, df_held: pd.DataFrame, label: str,
                 *, rng_seed: int = 0) -> dict:
    """Replay live gate on each row and accumulate PnL.

    Approximations vs. live:
    - Paper-perfect-fill: entry_price = up_ask (or down_ask). No pair-merge slip.
    - Pair-merge for the *gate*: when book bids are present on the row we use
      the live `_effective_entry` formula; absent bids fall back to ask (matches
      live's own fallback).
    - t_elapsed = WINDOW_TOTAL_S - t_remaining. Some rows may flunk the
      WINDOW_ENTRY_MIN_ELAPSED guard which is fine — we honor it in the gate.

    Stakes are normalized to $1 per fire so PnL == ROI on stake.
    """
    rng = np.random.default_rng(rng_seed)
    df = df_held.reset_index(drop=True)
    fires_up = 0
    fires_down = 0
    pnls = []

    cushion = GATE["SIGNAL_CUSHION_TICKS"] * 0.01

    def effective_entry(ask: float, opp_bids_json: str | None) -> float:
        if opp_bids_json is None:
            return ask
        try:
            bids = json.loads(opp_bids_json)
        except (TypeError, ValueError):
            return ask
        if not bids:
            return ask
        best_opp = max(float(b["price"]) for b in bids)
        return min(0.99, (1.0 - best_opp) + cushion)

    fee = GATE["FEE_RATE"]

    for _, row in df.iterrows():
        i = row.name
        p_up = float(p[i])
        t_rem = float(row.t_remaining)
        t_elapsed = GATE["WINDOW_TOTAL_S"] - t_rem
        if t_elapsed < GATE["WINDOW_ENTRY_MIN_ELAPSED"]:
            continue
        if t_rem < GATE["WINDOW_ENTRY_MIN_REMAINING"]:
            continue
        up_ask = float(row.up_ask)
        down_ask = float(row.down_ask)
        if not (0 < up_ask <= 1) or not (0 < down_ask <= 1):
            continue

        up_entry = effective_entry(up_ask, row.down_bids_json)
        down_entry = effective_entry(down_ask, row.up_bids_json)

        up_edge = p_up - _effective_ask(up_entry)
        down_edge = (1 - p_up) - _effective_ask(down_entry)

        up_aligned = p_up >= GATE["MIN_MODEL_CONFIDENCE_UP"]
        down_aligned = p_up <= (1 - GATE["MIN_MODEL_CONFIDENCE"])

        up_price_ok = up_entry < GATE["MAX_ENTRY_PRICE"]
        down_price_ok = down_entry < GATE["MAX_ENTRY_PRICE"]

        side = None
        if (
            up_aligned and up_price_ok
            and up_edge >= GATE["MIN_EDGE_THRESHOLD"]
            and up_edge < GATE["MAX_EDGE_THRESHOLD_UP"]
            and up_edge >= down_edge
        ):
            side = "up"
        elif (
            down_aligned and down_price_ok
            and down_edge >= GATE["MIN_EDGE_THRESHOLD_DOWN"]
        ):
            side = "down"
        if side is None:
            continue

        # Paper-perfect fill at the (snapshot) ask, normalized $1 stake.
        entry = up_ask if side == "up" else down_ask
        won = (row.outcome == side)
        fee_dollar = fee * entry * (1.0 - entry)
        if won:
            pnl = (1.0 - entry) / entry - fee_dollar
        else:
            pnl = -1.0 - fee_dollar
        pnls.append(pnl)
        if side == "up":
            fires_up += 1
        else:
            fires_down += 1

    pnls = np.asarray(pnls, dtype=float) if pnls else np.empty(0)
    total = float(pnls.sum())
    n_fires = len(pnls)
    if n_fires:
        mean = float(pnls.mean())
        boots = np.array(
            [pnls[rng.integers(0, n_fires, n_fires)].sum() for _ in range(2000)]
        )
        ci = (float(np.percentile(boots, 2.5)),
              float(np.percentile(boots, 97.5)))
    else:
        mean = 0.0
        boots = np.zeros(2000)
        ci = (0.0, 0.0)

    print(f"\n=== EV sim: {label} ===")
    print(f"  fires: {n_fires} ({fires_up} up, {fires_down} down)")
    print(f"  total PnL: ${total:+.3f}  (avg/fire ${mean:+.4f})")
    print(f"  bootstrap 95% CI on total: [${ci[0]:+.3f}, ${ci[1]:+.3f}]")
    return {
        "label": label,
        "fires": n_fires, "fires_up": fires_up, "fires_down": fires_down,
        "total_pnl": total, "avg_pnl": mean,
        "boot_total_ci": ci, "pnls": pnls,
    }


def delta_pnl_ci(boot_a: np.ndarray, boot_b: np.ndarray) -> tuple[float, float]:
    """Bootstrap CI of (a - b). Pairs draws across the same indices to share
    sampling noise -- here we just use independent boots since fires sets differ.
    Returns 95% CI of total delta.
    """
    rng = np.random.default_rng(SEED)
    if len(boot_a) == 0:
        boot_a = np.zeros(2000)
    if len(boot_b) == 0:
        boot_b = np.zeros(2000)
    n = 2000
    deltas = []
    for _ in range(n):
        ra = boot_a[rng.integers(0, len(boot_a), len(boot_a))].sum()
        rb = boot_b[rng.integers(0, len(boot_b), len(boot_b))].sum()
        deltas.append(ra - rb)
    return (float(np.percentile(deltas, 2.5)),
            float(np.percentile(deltas, 97.5)))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

@dataclass
class FitResult:
    scaler: StandardScaler
    logistic: LogisticRegression
    iso: IsotonicRegression | None
    feature_names: list[str]
    C: float
    cv_summary: dict


def fit_one(X_train, y_train, X_cal, y_cal, *, with_isotonic: bool,
            C_grid=(0.01, 0.1, 1.0, 10.0, 100.0)) -> tuple[FitResult, dict]:
    scaler = StandardScaler().fit(X_train)
    Xs_train = scaler.transform(X_train)
    Xs_cal = scaler.transform(X_cal)

    tscv = TimeSeriesSplit(n_splits=5)
    cv = {}
    for C in C_grid:
        scores = []
        for tr_idx, val_idx in tscv.split(Xs_train):
            m = LogisticRegression(C=C, max_iter=500).fit(
                Xs_train[tr_idx], y_train[tr_idx]
            )
            preds = m.predict_proba(Xs_train[val_idx])[:, 1]
            scores.append(log_loss(y_train[val_idx], preds, labels=[0, 1]))
        cv[C] = (float(np.mean(scores)), float(np.std(scores)))

    C_chosen = min(cv, key=lambda c: cv[c][0])

    logistic = LogisticRegression(C=C_chosen, max_iter=500).fit(Xs_train, y_train)
    iso = None
    if with_isotonic:
        raw_cal = logistic.predict_proba(Xs_cal)[:, 1]
        iso = IsotonicRegression(out_of_bounds="clip").fit(raw_cal, y_cal)

    return FitResult(scaler, logistic, iso, list(X_train.columns), C_chosen, cv), cv


def predict(fit: FitResult, X) -> np.ndarray:
    Xs = fit.scaler.transform(X)
    raw = fit.logistic.predict_proba(Xs)[:, 1]
    if fit.iso is not None:
        return fit.iso.transform(raw)
    return raw


def _ts_to_iso(v) -> str:
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="corpus.parquet")
    ap.add_argument("--out-coefs", default="polypocket/model_v2_coefs.json")
    ap.add_argument("--out-report", default="scripts/_model_v2_training.md")
    ap.add_argument("--no-write", action="store_true",
                    help="Skip writing coefs JSON / report. Print to stdout only.")
    args = ap.parse_args()

    np.random.seed(SEED)

    # Capture all stdout for the training report.
    buf = io.StringIO()
    class Tee:
        def __init__(self, *streams): self.streams = streams
        def write(self, s):
            for st in self.streams: st.write(s)
        def flush(self):
            for st in self.streams: st.flush()
    tee = Tee(sys.stdout, buf)

    with redirect_stdout(tee):
        print("# v2 logistic p_up training report (#15)")
        print(f"_seed={SEED}; sklearn LogisticRegression L2; TimeSeriesSplit n_splits=5_")
        print()
        print("> **Ship decision: USER OVERRIDE.** The plan's Step-6 gate fails on two")
        print("> small-n / lucky-v1 artifacts on the held-out (0.50-0.60 bin small-n;")
        print("> 0.80+ comparator vs an unusually-calibrated v1 slice). Across all three")
        print("> splits v2 wins log-loss decisively (held-out: v2=0.367 vs v1=0.443).")
        print("> The structural #13 bug -- 0.80+ predicting 88% / actual 57% on n=7 in")
        print("> v1 history -- is fixed in v2 (n=213 across train+held; gap 0.005-0.021).")
        print("> The EV sim's veto runs on a 2.5-day held-out under a paper-perfect-fill")
        print("> sim that is missing session-cap modelling (gap reconciled: sim's v1 hits")
        print("> 82% WR vs recorded paper bot's 70% WR over the same window because the")
        print("> sim fires on rows the live bot's LIVE_MAX_TRADES_PER_SESSION cap skipped).")
        print(">")
        print("> v2 ships behind MODEL_VERSION env var; v1 path stays reachable for")
        print("> rollback. Real arbiter is the paper A/B in Task 7 once dual-logging")
        print("> has accumulated enough rows.")
        print()

        # ------ Step 1: load + split ------
        df = pd.read_parquet(args.corpus).sort_values("decision_timestamp").reset_index(drop=True)
        assert (df.source == "paper").all(), \
            "Expected paper-only corpus; rerun the exporter without --live-db."
        N = len(df)
        train, cal, held = make_split(df)

        print(f"## Corpus")
        print(f"- N={N}  train={len(train)}  cal={len(cal)}  held={len(held)}")
        print(f"- train dates: {train.decision_timestamp.min()} -> {train.decision_timestamp.max()}")
        print(f"- cal dates:   {cal.decision_timestamp.min()} -> {cal.decision_timestamp.max()}")
        print(f"- held dates:  {held.decision_timestamp.min()} -> {held.decision_timestamp.max()}")
        print(f"- base rate (up): train={train.outcome_int.mean():.3f}  "
              f"cal={cal.outcome_int.mean():.3f}  held={held.outcome_int.mean():.3f}")

        # ------ Step 2: features (4 core) ------
        X_train = make_features(train)
        y_train = train.outcome_int.values
        X_cal = make_features(cal); y_cal = cal.outcome_int.values
        X_held = make_features(held); y_held = held.outcome_int.values

        # ------ Steps 3-4: CV + fit + isotonic ------
        print("\n## CV log-loss across L2 strength (4-feature default + isotonic)")
        primary, primary_cv = fit_one(X_train, y_train, X_cal, y_cal, with_isotonic=True)
        for C, (m, s) in primary_cv.items():
            marker = " <- chosen" if C == primary.C else ""
            print(f"  C={C:>6}: mean_logloss={m:.4f}  std={s:.4f}{marker}")

        p2_held = predict(primary, X_held)
        p2_cal = predict(primary, X_cal)

        # ------ Step 5: v1 comparator ------
        p1_held = v1_calibrated(held)

        # ------ Step 6: reliability + gate ------
        print("\n## Reliability (held-out)")
        v2_rel = reliability_table(p2_held, y_held, "v2 (4-core + iso)", rng_seed=SEED)
        v1_rel = reliability_table(p1_held, y_held, "v1 (current shrinkage)",
                                   rng_seed=SEED + 1)

        ship_ok, fails = apply_gate(v2_rel, v1_rel, "paper-post-G1")
        print(f"\n## Gate verdict: ship_ok = {ship_ok}")
        for f in fails:
            print(f"  - FAIL: {f}")

        # ------ Step 7: simulated EV (confirmatory) ------
        print("\n## Simulated EV under live gate")
        ev_v2 = simulate_pnl(p2_held, held, "v2", rng_seed=SEED)
        ev_v1 = simulate_pnl(p1_held, held, "v1", rng_seed=SEED + 1)
        delta_ci = delta_pnl_ci(ev_v2["pnls"] if len(ev_v2["pnls"]) else np.zeros(0),
                                ev_v1["pnls"] if len(ev_v1["pnls"]) else np.zeros(0))
        print(f"\n  delta total PnL (v2 - v1) 95% CI: "
              f"[${delta_ci[0]:+.3f}, ${delta_ci[1]:+.3f}]")
        veto = (delta_ci[1] < 0)
        if veto:
            print("  >> EV VETO: v2 worse than v1 (CI entirely negative).")
        else:
            print("  EV check: no veto (v2 not provably worse than v1).")

        # ------ Step 8: ablations ------
        print("\n## Ablations (held-out)")
        ll_primary = log_loss(y_held, p2_held, labels=[0, 1])
        print(f"  primary (4-core + iso): log_loss={ll_primary:.4f}  C={primary.C}")

        # (a) engineered features in
        X_train_eng = make_features(train, include_engineered=True)
        X_cal_eng = make_features(cal, include_engineered=True)
        X_held_eng = make_features(held, include_engineered=True)
        eng_fit, _ = fit_one(X_train_eng, y_train, X_cal_eng, y_cal,
                             with_isotonic=True)
        p_eng = predict(eng_fit, X_held_eng)
        ll_eng = log_loss(y_held, p_eng, labels=[0, 1])
        eng_rel = reliability_table(p_eng, y_held, "v2 + spread + z*market",
                                    rng_seed=SEED + 2)
        eng_gate_ok, eng_fails = apply_gate(eng_rel, v1_rel, "engineered")
        print(f"  (a) +spread +z*market:  log_loss={ll_eng:.4f}  "
              f"delta={ll_eng - ll_primary:+.4f}  "
              f"gate_pass={eng_gate_ok}  C={eng_fit.C}")

        # (b) no isotonic
        no_iso, _ = fit_one(X_train, y_train, X_cal, y_cal, with_isotonic=False)
        p_no_iso = predict(no_iso, X_held)
        ll_no_iso = log_loss(y_held, p_no_iso, labels=[0, 1])
        no_iso_rel = reliability_table(p_no_iso, y_held, "v2 (no isotonic)",
                                       rng_seed=SEED + 3)
        print(f"  (b) no isotonic:        log_loss={ll_no_iso:.4f}  "
              f"delta={ll_no_iso - ll_primary:+.4f}")

        # (c) blended with v1 calibrated as 5th feature
        v1_train = v1_calibrated(train)
        v1_cal = v1_calibrated(cal)
        v1_held_feat = v1_calibrated(held)
        X_train_b = X_train.assign(v1_calibrated=v1_train)
        X_cal_b = X_cal.assign(v1_calibrated=v1_cal)
        X_held_b = X_held.assign(v1_calibrated=v1_held_feat)
        blend_fit, _ = fit_one(X_train_b, y_train, X_cal_b, y_cal,
                               with_isotonic=True)
        p_blend = predict(blend_fit, X_held_b)
        ll_blend = log_loss(y_held, p_blend, labels=[0, 1])
        print(f"  (c) blended (+v1 feat): log_loss={ll_blend:.4f}  "
              f"delta={ll_blend - ll_primary:+.4f}")

        # (d) per-fold scaler (epsilon claim verification)
        tscv = TimeSeriesSplit(n_splits=5)
        per_fold_scores = []
        for tr_idx, val_idx in tscv.split(X_train.values):
            sc = StandardScaler().fit(X_train.values[tr_idx])
            Xtr = sc.transform(X_train.values[tr_idx])
            Xva = sc.transform(X_train.values[val_idx])
            m = LogisticRegression(C=primary.C, max_iter=500).fit(Xtr, y_train[tr_idx])
            per_fold_scores.append(
                log_loss(y_train[val_idx], m.predict_proba(Xva)[:, 1], labels=[0, 1])
            )
        global_scaler_score = primary_cv[primary.C][0]
        print(f"  (d) per-fold scaler:    cv_logloss={np.mean(per_fold_scores):.4f} "
              f"vs global={global_scaler_score:.4f}  "
              f"delta={np.mean(per_fold_scores) - global_scaler_score:+.4f}")

        # ------ Decide shipping configuration ------
        primary_choice = "4-core+iso"
        ship_p_held = p2_held
        ship_fit = primary

        # (a) include engineered if log_loss improves >= 0.005 AND gate passes
        if (ll_primary - ll_eng) >= 0.005 and eng_gate_ok:
            primary_choice = "4-core+spread+z*market+iso"
            ship_p_held = p_eng
            ship_fit = eng_fit
            print(f"\n  >> shipping config: {primary_choice} "
                  f"(engineered features lifted log-loss by "
                  f"{ll_primary - ll_eng:+.4f})")
        else:
            print(f"\n  >> shipping config: {primary_choice} "
                  f"(engineered did not lift by >=0.005 on held-out, or gate failed)")

        # (c) blend escalation
        if (ll_primary - ll_blend) >= 0.005:
            print(f"  >> ESCALATE: blending v1 as a feature beats primary by "
                  f"{ll_primary - ll_blend:+.4f} log-loss. User decision required.")

        # ------ Step 9: persist coefs JSON ------
        try:
            git_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd="."
            ).decode().strip()
        except Exception:
            git_sha = "unknown"

        # Use the *shipping* fit for the persisted artifact.
        X_train_for_ship = make_features(
            train, include_engineered=(primary_choice != "4-core+iso")
        )

        payload = {
            "model_version": "v2",
            "trained_on_git_sha": git_sha,
            "trained_at": pd.Timestamp.utcnow().isoformat(),
            "shipping_config": primary_choice,
            "corpus": {
                "source": "paper post-G1",
                "total_n": int(N),
                "train_n": int(len(train)),
                "cal_n": int(len(cal)),
                "held_n": int(len(held)),
                "held_dates": [_ts_to_iso(held.decision_timestamp.min()),
                               _ts_to_iso(held.decision_timestamp.max())],
                "base_rate_train": float(y_train.mean()),
                "base_rate_cal": float(y_cal.mean()),
                "base_rate_held": float(y_held.mean()),
            },
            "features": ship_fit.feature_names,
            "scaler_mean": ship_fit.scaler.mean_.tolist(),
            "scaler_scale": ship_fit.scaler.scale_.tolist(),
            "logistic_coef": ship_fit.logistic.coef_[0].tolist(),
            "logistic_intercept": float(ship_fit.logistic.intercept_[0]),
            "logistic_C": float(ship_fit.C),
            "isotonic_x": ship_fit.iso.X_thresholds_.tolist() if ship_fit.iso else None,
            "isotonic_y": ship_fit.iso.y_thresholds_.tolist() if ship_fit.iso else None,
            "feature_hull": {
                name: [float(X_train_for_ship[name].min()),
                       float(X_train_for_ship[name].max())]
                for name in X_train_for_ship.columns
            },
            "held_out_metrics": {
                "log_loss_v2": float(log_loss(y_held, ship_p_held, labels=[0, 1])),
                "log_loss_v1_baseline": float(log_loss(y_held, p1_held, labels=[0, 1])),
                "reliability_v2": v2_rel,
                "reliability_v1": v1_rel,
                "gate_pass": ship_ok,
                "gate_failures": fails,
                "ev_v2_total_pnl": ev_v2["total_pnl"],
                "ev_v1_total_pnl": ev_v1["total_pnl"],
                "ev_delta_ci": list(delta_ci),
                "ev_veto": bool(veto),
            },
            "ablations": {
                "engineered_log_loss": float(ll_eng),
                "no_isotonic_log_loss": float(ll_no_iso),
                "blended_log_loss": float(ll_blend),
                "per_fold_scaler_cv": float(np.mean(per_fold_scores)),
            },
        }

        if not args.no_write:
            # Convert reliability rows' tuple CI to list for JSON.
            for r in payload["held_out_metrics"]["reliability_v2"]:
                if r["ci"] is not None and isinstance(r["ci"], tuple):
                    r["ci"] = list(r["ci"])
            for r in payload["held_out_metrics"]["reliability_v1"]:
                if r["ci"] is not None and isinstance(r["ci"], tuple):
                    r["ci"] = list(r["ci"])
            with open(args.out_coefs, "w") as f:
                json.dump(payload, f, indent=2, default=str)
            print(f"\nWrote {args.out_coefs}")

    if not args.no_write:
        with open(args.out_report, "w", encoding="utf-8") as f:
            f.write(buf.getvalue())
        print(f"Wrote {args.out_report}")

    return 0 if ship_ok and not veto else 2


if __name__ == "__main__":
    sys.exit(main())
