"""Runtime-mutable configuration. TUI keybinds modify these at runtime."""

import os

from dotenv import load_dotenv

load_dotenv()

# --- Signal thresholds ---
MIN_EDGE_THRESHOLD = 0.03
MIN_MODEL_CONFIDENCE = 0.60
FEE_RATE = 0.072

# --- Position sizing ---
POSITION_SIZE_USDC = 10.0

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
