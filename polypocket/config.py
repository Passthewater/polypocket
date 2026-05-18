"""Runtime-mutable configuration. TUI keybinds modify these at runtime."""

import os

from dotenv import load_dotenv

load_dotenv(override=True)

# --- Signal thresholds ---
MIN_EDGE_THRESHOLD = 0.10
# Ticks added to the pair-merge clearing price (1 - best_opp_bid) when
# computing the edge gate. The gate uses the live-executable price, not the
# snapshot ask — a BUY UP on a binary market clears via pair-merge against a
# DOWN bid, so its real entry is (1 - best_down_bid), not up_ask. Separate
# from IOC_BUFFER_TICKS, which is the taker's limit-price buffer for the
# live order. Re-fit on n=84 post-2026-04-23 fills with logged bids (#11):
# empirical slip median 8.5¢ (UP 9.8¢, DOWN 6.2¢), mean 9.2¢. Replay sweep
# at production thresholds returned cushion=8 as the single-knob PnL optimum
# (−$7.11 vs −$11.64 at cushion=6 on n=19 admitted trades). Per-side
# asymmetry exists but a global value is held until #15 reworks UP gating.
SIGNAL_CUSHION_TICKS = int(os.getenv("SIGNAL_CUSHION_TICKS", "8"))
# Edge threshold checks run on the CALIBRATED probability (see shrinkage
# factors below). DOWN threshold kept at 0.10 to remain close to sim_filters.py
# option 11 (`down_shrink_0.30`) — less curve-fit than the in-sample PnL optimum
# on n=32 DOWN trades.
MIN_EDGE_THRESHOLD_DOWN = 0.10
# Skip any side whose ask is at or above this. Entries at ≥0.70 lost money on
# both sides over 203 trades — fee drag plus compressed upside make the math
# unfavorable near the middle of the book.
MAX_ENTRY_PRICE = 0.70
# Cap on the UP calibrated edge. Live corpus n=117 (2026-04-23): UP trades with
# edge ≥0.25 went 3/13 (23% WR) for −$40.68 PnL — 85% of total loss comes from
# this single bucket. Neighboring bin 0.20–0.25 was 4/5 (80% WR, +$2.58), so
# the cliff is in the data, not smooth. Matches issue #13's "0.80+ bin may be
# miscalibrated" warning. No symmetric DOWN cap: 0.25+ DOWN was n=3.
MAX_EDGE_THRESHOLD_UP = 0.25
# DOWN threshold (via `model_p_up <= 1 - MIN_MODEL_CONFIDENCE`) and the symmetric
# floor for UP. Raised from 0.70 to 0.75 after gate-only replay on n=41 settled
# fills (2026-04-24): UP at conf>=0.75 + threshold=0.10 reaches ~break-even
# (-$0.003/trade) vs -$0.662/trade at conf>=0.70 + threshold=0.05. See #13.
MIN_MODEL_CONFIDENCE = 0.60
MIN_MODEL_CONFIDENCE_UP = 0.75
# --- Calibration (per-side shrinkage toward 0.5) ---
# n=218 checkpoint: UP gap -4.4pts (converging, identity holds); DOWN gap at
# k=0.30 grew to +12.3pts (under-confident), tripping the plan's pre-committed
# "raise toward 0.5" trigger. k=0.50 brings DOWN gap to +4.3pts (in-band) on
# n=31 DOWN trades; log-loss plateau from 0.50-0.60 is flat, so landing on
# 0.50 executes the rule without chasing the noise-floor minimum.
CALIBRATION_SHRINKAGE_UP = 1.00
CALIBRATION_SHRINKAGE_DOWN = 0.50
# Polymarket crypto taker fee coefficient. Actual fee per trade is
# `size * FEE_RATE * p * (1 - p)` — peaks at p=0.50, zero at the extremes.
# Fees are charged in shares on buys; worthless on losing side.
FEE_RATE = 0.072


def fee_shares(size: float, price: float) -> float:
    """Fee charged in shares on a buy of `size` shares at `price`."""
    return size * FEE_RATE * price * (1.0 - price)


def effective_ask(price: float) -> float:
    """Break-even model probability to buy at `price` (price inflated for fee)."""
    return price / (1.0 - FEE_RATE * price * (1.0 - price))

# --- Position sizing ---
MIN_POSITION_USDC = float(os.getenv("MIN_POSITION_USDC", "5.0"))
MAX_POSITION_USDC = float(os.getenv("MAX_POSITION_USDC", "20.0"))
VOL_FLOOR = 0.0005
VOL_RANGE = 0.0005
EDGE_FLOOR = 0.03
EDGE_RANGE = 0.17

# --- Risk ---
MAX_DAILY_LOSS = 15.0
MAX_CONSECUTIVE_LOSSES = 5

# --- Signal model ---
VOLATILITY_LOOKBACK = 50

# --- Entry timing ---
WINDOW_ENTRY_MIN_ELAPSED = 60
WINDOW_ENTRY_MIN_REMAINING = 30

# --- Mode ---
TRADING_MODE = os.getenv("TRADING_MODE", "paper").strip().lower()

# --- Paper trading ---
PAPER_STARTING_BALANCE = 1000.0
PAPER_DB_PATH = "paper_trades.db"

# --- Polymarket ---
POLYMARKET_HOST = "https://clob.polymarket.com"
POLYMARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CHAIN_ID = 137
BOOK_MAX_TOTAL_ASK = 1.02

# --- Live trading ---
LIVE_DB_PATH = "live_trades.db"
# FOK limit = best_ask + this many ticks. 0 means "fill only at quoted ask",
# which kills whenever the book has less depth than our size at that exact
# level — common on 5m BTC books. 3 ticks matches typical cross-level move
# during 200–500ms signal→post latency on 5m BTC books; the depth check in
# bot.py ensures the extra tick stays affordable.
FOK_SLIPPAGE_TICKS = int(os.getenv("FOK_SLIPPAGE_TICKS", "3"))
# IOC buffer added to the pair-merge clearing price. For a BUY UP, the
# implied clearing price is `1 - best_down_bid`, and we post at that plus
# this many ticks to absorb DOWN-book churn during the ~200–500 ms signing
# window. Reduced from 15 to 8 after the 2026-04-23 buffer=8 cohort (n=19
# fills) produced median slip 6¢ vs 11.6¢ at buffer=15 — SHIP verdict per
# design doc. See #14, closes #12.
IOC_BUFFER_TICKS = int(os.getenv("IOC_BUFFER_TICKS", "8"))
# Execution mode for live trades. "fak" keeps the current pair-merge taker
# (FAK-via-v2 SDK) path; "post_only" routes through a GTC + post_only
# resting maker order. Paper mode ignores this entirely. Default "fak"
# until the post-only path completes paper-replay + dry-run probe + small
# live cohort validation per the 2026-05-15 post-only-entries design.
ENTRY_MODE = os.getenv("ENTRY_MODE", "fak").strip().lower()
# Ticks below the pair-merge clearing price (1 - best_opp_bid) to rest a
# post-only maker order. Default 2: absorbs typical 200-500ms book drift
# between gate-eval and SDK sign while still capturing ~7 ticks of edge
# over the FAK regime's `pmc + IOC_BUFFER_TICKS`. Tune from cohort data
# after the first 50-100 fills land.
POST_ONLY_REST_OFFSET_TICKS = int(os.getenv("POST_ONLY_REST_OFFSET_TICKS", "2"))
# Wall-clock seconds-remaining at which the bot tick cancels a resting
# post-only order. Matches WINDOW_ENTRY_MIN_REMAINING so a post-only fill
# never lands inside the dead-band where the gate refuses new signals.
POST_ONLY_CANCEL_AT_T_REMAINING_S = float(os.getenv("POST_ONLY_CANCEL_AT_T_REMAINING_S", "30"))
# Seconds subtracted from window.end_time when computing the server-side
# order `expiration` field. Defense-in-depth against a bot tick that
# misses its cancel deadline — server kills the order if the bot is
# silent through the boundary. Default 60 mirrors the server's own
# security threshold (see POLYMARKET_MIN_EXPIRATION_BUFFER_S): the
# server rejects any expiration value less than now + 60s with a 400
# (empirical, Step-9 probe 2026-05-16).
POST_ONLY_EXPIRY_SAFETY_BUFFER_S = float(os.getenv("POST_ONLY_EXPIRY_SAFETY_BUFFER_S", "60"))
# Polymarket's server-side security threshold for GTD `expiration` values.
# Any expiration < now + 60s is rejected with HTTP 400. The bot computes
# expiration = window.end_time - POST_ONLY_EXPIRY_SAFETY_BUFFER_S; when
# that falls below `now + this + safety`, the dispatch site floors to a
# server-accepted value (bot-side cancel handles window-end protection
# in that band). 65 = 60s threshold + 5s safety against clock skew /
# request transit time.
POLYMARKET_MIN_EXPIRATION_BUFFER_S = float(os.getenv("POLYMARKET_MIN_EXPIRATION_BUFFER_S", "65"))
# Fraction of book depth (at <= FOK limit price) we ask for as our FOK
# size. Leaves headroom for the book to thin between our depth read and
# the signed order reaching the matcher. 0.9 = ask for at most 90% of
# visible fillable size.
DEPTH_CLAMP_BUFFER = float(os.getenv("DEPTH_CLAMP_BUFFER", "0.9"))
# Minimum fraction of intended size a trade must be able to fill for us
# to bother. If depth-clamped target_size < intended * MIN_FILL_RATIO,
# skip the window with reason "book-too-thin".
MIN_FILL_RATIO = float(os.getenv("MIN_FILL_RATIO", "0.5"))
# Max age of the last WS book event before a signal is considered unsafe to
# trade on. Covers WS reconnect gaps and silent book stalls.
MAX_BOOK_AGE_S = float(os.getenv("MAX_BOOK_AGE_S", "3.0"))

# --- Wallet watchdog ---
# Halt-not-alert threshold for proxy USDC vs ledger-expected USDC. Catches the
# class of bug that made v1's cohort silently bleed (untracked maker fills).
# $5 ≈ one trade's worth of capital; smaller risks false-halt on fee/rounding
# drift, larger means the watchdog fires only once damage is well underway.
# Independent of ENTRY_MODE — active whenever TRADING_MODE=live.
WALLET_LEDGER_DIVERGENCE_HALT_USDC = float(os.getenv("WALLET_LEDGER_DIVERGENCE_HALT_USDC", "5.0"))

POLYMARKET_PROXY_ADDRESS = os.getenv("PROXY_ADDRESS", "").strip()
CLOB_API_KEY = os.getenv("CLOB_API_KEY", "").strip()
CLOB_SECRET = os.getenv("CLOB_SECRET", "").strip()
CLOB_PASSPHRASE = os.getenv("CLOB_PASSPHRASE", "").strip()
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "").strip()


def snapshot_gate_config() -> dict:
    """Return a plain-dict snapshot of TUI-mutable gate/sizing constants.

    Read at call time via module globals so TUI keybind mutations (which
    rebind the module attribute) are reflected. Serialize with json.dumps
    at the call site. When adding a new tunable constant above, append
    its name here in the same commit.
    """
    return {
        "MIN_EDGE_THRESHOLD": MIN_EDGE_THRESHOLD,
        "MIN_EDGE_THRESHOLD_DOWN": MIN_EDGE_THRESHOLD_DOWN,
        "MAX_ENTRY_PRICE": MAX_ENTRY_PRICE,
        "MAX_EDGE_THRESHOLD_UP": MAX_EDGE_THRESHOLD_UP,
        "MIN_MODEL_CONFIDENCE": MIN_MODEL_CONFIDENCE,
        "MIN_MODEL_CONFIDENCE_UP": MIN_MODEL_CONFIDENCE_UP,
        "CALIBRATION_SHRINKAGE_UP": CALIBRATION_SHRINKAGE_UP,
        "CALIBRATION_SHRINKAGE_DOWN": CALIBRATION_SHRINKAGE_DOWN,
        "SIGNAL_CUSHION_TICKS": SIGNAL_CUSHION_TICKS,
        "IOC_BUFFER_TICKS": IOC_BUFFER_TICKS,
        "FOK_SLIPPAGE_TICKS": FOK_SLIPPAGE_TICKS,
        "ENTRY_MODE": ENTRY_MODE,
        "POST_ONLY_REST_OFFSET_TICKS": POST_ONLY_REST_OFFSET_TICKS,
        "POST_ONLY_CANCEL_AT_T_REMAINING_S": POST_ONLY_CANCEL_AT_T_REMAINING_S,
        "POST_ONLY_EXPIRY_SAFETY_BUFFER_S": POST_ONLY_EXPIRY_SAFETY_BUFFER_S,
        "POLYMARKET_MIN_EXPIRATION_BUFFER_S": POLYMARKET_MIN_EXPIRATION_BUFFER_S,
        "WALLET_LEDGER_DIVERGENCE_HALT_USDC": WALLET_LEDGER_DIVERGENCE_HALT_USDC,
        "DEPTH_CLAMP_BUFFER": DEPTH_CLAMP_BUFFER,
        "MIN_FILL_RATIO": MIN_FILL_RATIO,
        "MAX_BOOK_AGE_S": MAX_BOOK_AGE_S,
        "WINDOW_ENTRY_MIN_ELAPSED": WINDOW_ENTRY_MIN_ELAPSED,
        "WINDOW_ENTRY_MIN_REMAINING": WINDOW_ENTRY_MIN_REMAINING,
        "VOLATILITY_LOOKBACK": VOLATILITY_LOOKBACK,
        "MIN_POSITION_USDC": MIN_POSITION_USDC,
        "MAX_POSITION_USDC": MAX_POSITION_USDC,
        "EDGE_FLOOR": EDGE_FLOOR,
        "EDGE_RANGE": EDGE_RANGE,
        "VOL_FLOOR": VOL_FLOOR,
        "VOL_RANGE": VOL_RANGE,
        "FEE_RATE": FEE_RATE,
        "TRADING_MODE": TRADING_MODE,
    }
