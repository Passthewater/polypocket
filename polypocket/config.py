"""Runtime-mutable configuration. TUI keybinds modify these at runtime."""

import os

from dotenv import load_dotenv

load_dotenv()

# --- Signal thresholds ---
MIN_EDGE_THRESHOLD = 0.03
# DOWN threshold (via `model_p_up <= 1 - MIN_MODEL_CONFIDENCE`) and the symmetric
# floor for UP. UP gets its own, higher threshold because UP-side trades in the
# 60–70% bucket have historically been -EV; see reports/2026-04-16-calibration.md.
MIN_MODEL_CONFIDENCE = 0.60
MIN_MODEL_CONFIDENCE_UP = 0.70
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
