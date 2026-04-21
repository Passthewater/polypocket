"""Runtime-mutable configuration. TUI keybinds modify these at runtime."""

import os

from dotenv import load_dotenv

load_dotenv()

# --- Signal thresholds ---
MIN_EDGE_THRESHOLD = 0.03
# Edge threshold checks run on the CALIBRATED probability (see shrinkage
# factors below). DOWN threshold kept at 0.10 to remain close to sim_filters.py
# option 11 (`down_shrink_0.30`) — less curve-fit than the in-sample PnL optimum
# on n=32 DOWN trades.
MIN_EDGE_THRESHOLD_DOWN = 0.10
# Skip any side whose ask is at or above this. Entries at ≥0.70 lost money on
# both sides over 203 trades — fee drag plus compressed upside make the math
# unfavorable near the middle of the book.
MAX_ENTRY_PRICE = 0.70
# DOWN threshold (via `model_p_up <= 1 - MIN_MODEL_CONFIDENCE`) and the symmetric
# floor for UP. UP gets its own, higher threshold because UP-side trades in the
# 60–70% bucket have historically been -EV; see reports/2026-04-16-calibration.md.
MIN_MODEL_CONFIDENCE = 0.60
MIN_MODEL_CONFIDENCE_UP = 0.70
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
MIN_POSITION_USDC = 5.0
MAX_POSITION_USDC = 20.0
VOL_FLOOR = 0.0005
VOL_RANGE = 0.0005
EDGE_FLOOR = 0.03
EDGE_RANGE = 0.17

# --- Risk ---
MAX_DAILY_LOSS = 50.0
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
LIVE_MAX_TRADES_PER_SESSION = int(os.getenv("LIVE_MAX_TRADES_PER_SESSION", "10"))

POLYMARKET_PROXY_ADDRESS = os.getenv("PROXY_ADDRESS", "").strip()
CLOB_API_KEY = os.getenv("CLOB_API_KEY", "").strip()
CLOB_SECRET = os.getenv("CLOB_SECRET", "").strip()
CLOB_PASSPHRASE = os.getenv("CLOB_PASSPHRASE", "").strip()
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "").strip()
