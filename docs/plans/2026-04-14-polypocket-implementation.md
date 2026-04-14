# Polypocket Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a directional prediction bot that detects latency between real-time BTC price movements and Polymarket's 5-minute Up/Down market odds, and bets on the underpriced side.

**Architecture:** Three async data feeds (Binance price via ccxt pro, Polymarket market discovery via Gamma REST, Polymarket order book via CLOB WS) converge into a signal engine that computes P(Up) using a Brownian motion model, compares to market odds, and triggers paper/live trades when edge exceeds threshold. Textual TUI for monitoring.

**Tech Stack:** Python 3.11+, ccxt (pro WebSocket), py-clob-client, websockets, textual, scipy, numpy, aiohttp, SQLite

**Source repo:** `C:/Users/Matt/polypocket`
**Reference codebase:** `C:/Users/Matt/polymarket-arb` (adapt patterns, don't modify)

---

## Task 1: Project Scaffold & Config

**Files:**
- Create: `polypocket/__init__.py`
- Create: `polypocket/config.py`
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `tests/__init__.py`
- Create: `tests/test_config.py`

**Step 1: Write the failing test**

```python
# tests/test_config.py
from polypocket.config import (
    MIN_EDGE_THRESHOLD,
    FEE_RATE,
    POSITION_SIZE_USDC,
    MAX_DAILY_LOSS,
    MAX_CONSECUTIVE_LOSSES,
    VOLATILITY_LOOKBACK,
    WINDOW_ENTRY_MIN_ELAPSED,
    WINDOW_ENTRY_MIN_REMAINING,
    TRADING_MODE,
)


def test_defaults_are_sane():
    assert MIN_EDGE_THRESHOLD == 0.03
    assert FEE_RATE == 0.02
    assert POSITION_SIZE_USDC == 10.0
    assert MAX_DAILY_LOSS == 50.0
    assert MAX_CONSECUTIVE_LOSSES == 5
    assert VOLATILITY_LOOKBACK == 50
    assert WINDOW_ENTRY_MIN_ELAPSED == 60
    assert WINDOW_ENTRY_MIN_REMAINING == 30
    assert TRADING_MODE == "paper"


def test_edge_threshold_exceeds_fee():
    """Min edge must be greater than fee rate to be profitable."""
    assert MIN_EDGE_THRESHOLD > FEE_RATE
```

**Step 2: Run test to verify it fails**

Run: `cd /c/Users/Matt/polypocket && python -m pytest tests/test_config.py -v`
Expected: FAIL — ModuleNotFoundError

**Step 3: Write pyproject.toml**

```toml
# pyproject.toml
[project]
name = "polypocket"
version = "0.1.0"
description = "Directional 5-minute BTC prediction bot for Polymarket"
requires-python = ">=3.11"
dependencies = [
    "ccxt>=4.0.0",
    "py-clob-client==0.19.0",
    "websockets>=12.0",
    "textual>=3.0.0",
    "scipy>=1.11.0",
    "numpy>=1.24.0",
    "python-dotenv>=1.0.0",
    "aiohttp>=3.9.0",
]

[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio"]

[build-system]
requires = ["setuptools>=68.0"]
build-backend = "setuptools.backends._legacy:_Backend"
```

**Step 4: Write .env.example**

```bash
# .env.example
# Polymarket (only needed for live trading)
PRIVATE_KEY=
# Trading mode: "paper" or "live"
TRADING_MODE=paper
```

**Step 5: Write config.py**

Reference `C:/Users/Matt/polymarket-arb/config.py` for the mutable-config pattern.

```python
# polypocket/config.py
"""Runtime-mutable configuration. TUI keybinds modify these at runtime."""

import os
from dotenv import load_dotenv

load_dotenv()

# --- Signal thresholds ---
MIN_EDGE_THRESHOLD = 0.03        # 3% edge required above fees to trade
FEE_RATE = 0.02                  # Polymarket taker fee per side

# --- Position sizing ---
POSITION_SIZE_USDC = 10.0        # USDC per trade

# --- Risk ---
MAX_DAILY_LOSS = 50.0            # Kill switch: stop trading if daily loss exceeds
MAX_CONSECUTIVE_LOSSES = 5       # Pause after N consecutive losses

# --- Signal model ---
VOLATILITY_LOOKBACK = 50         # Rolling window count for realized vol (~4 hours)

# --- Entry timing ---
WINDOW_ENTRY_MIN_ELAPSED = 60    # Don't bet before 60s into a window
WINDOW_ENTRY_MIN_REMAINING = 30  # Don't bet with <30s remaining

# --- Mode ---
TRADING_MODE = os.getenv("TRADING_MODE", "paper")

# --- Paper trading ---
PAPER_STARTING_BALANCE = 1000.0
PAPER_DB_PATH = "paper_trades.db"

# --- Polymarket ---
POLYMARKET_HOST = "https://clob.polymarket.com"
POLYMARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CHAIN_ID = 137
```

**Step 6: Write __init__.py files**

```python
# polypocket/__init__.py
# (empty)
```

```python
# tests/__init__.py
# (empty)
```

**Step 7: Install and run tests**

Run: `cd /c/Users/Matt/polypocket && pip install -e ".[dev]" && python -m pytest tests/test_config.py -v`
Expected: 2 PASSED

**Step 8: Commit**

```bash
cd /c/Users/Matt/polypocket && git add -A && git commit -m "feat: project scaffold with config and pyproject.toml"
```

---

## Task 2: Binance Price Feed (ccxt pro)

**Files:**
- Create: `polypocket/feeds/__init__.py`
- Create: `polypocket/feeds/binance.py`
- Create: `tests/test_binance_feed.py`

**Step 1: Write the failing test**

```python
# tests/test_binance_feed.py
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from polypocket.feeds.binance import BinanceFeed


def test_binance_feed_init():
    feed = BinanceFeed()
    assert feed.latest_price is None
    assert feed.prices == []


def test_binance_feed_on_trade_updates_price():
    feed = BinanceFeed()
    feed._on_trade({"price": 84231.42, "timestamp": 1713100000000})
    assert feed.latest_price == 84231.42
    assert len(feed.prices) == 1


def test_binance_feed_rolling_returns():
    """Test 5-minute return calculation from price history."""
    feed = BinanceFeed()
    # Simulate prices at 5-min intervals (timestamps in ms)
    base_ts = 1713100000000
    prices = [80000.0, 80100.0, 80050.0, 80200.0, 80150.0]
    for i, p in enumerate(prices):
        feed._on_trade({"price": p, "timestamp": base_ts + i * 300_000})
    returns = feed.get_5min_returns()
    assert len(returns) == 4
    # First return: (80100 - 80000) / 80000
    assert abs(returns[0] - 0.00125) < 1e-6
```

**Step 2: Run test to verify it fails**

Run: `cd /c/Users/Matt/polypocket && python -m pytest tests/test_binance_feed.py -v`
Expected: FAIL — ModuleNotFoundError

**Step 3: Write the implementation**

```python
# polypocket/feeds/__init__.py
# (empty)
```

```python
# polypocket/feeds/binance.py
"""Real-time BTC/USDT price feed via ccxt pro WebSocket."""

import asyncio
import logging
import time
from dataclasses import dataclass, field

import ccxt.pro as ccxtpro

log = logging.getLogger(__name__)

# We store one price snapshot per 5-minute boundary for volatility calc
SNAPSHOT_INTERVAL_S = 300


class BinanceFeed:
    """Streams BTC/USDT trades from Binance. Tracks latest price and
    periodic snapshots for rolling volatility estimation."""

    def __init__(self):
        self.latest_price: float | None = None
        self.latest_ts: float | None = None  # epoch seconds
        self.prices: list[dict] = []  # [{"price": float, "ts": float}, ...]
        self._last_snapshot_ts: float = 0.0

    def _on_trade(self, trade: dict) -> None:
        """Process a single trade. Called for each trade from watch_trades."""
        price = float(trade["price"])
        ts = trade["timestamp"] / 1000.0  # ccxt gives ms, we use seconds
        self.latest_price = price
        self.latest_ts = ts

        # Store periodic snapshots for volatility calculation
        if ts - self._last_snapshot_ts >= SNAPSHOT_INTERVAL_S:
            self.prices.append({"price": price, "ts": ts})
            self._last_snapshot_ts = ts
            # Keep only what we need for volatility lookback
            max_snapshots = 200
            if len(self.prices) > max_snapshots:
                self.prices = self.prices[-max_snapshots:]

    def get_5min_returns(self) -> list[float]:
        """Compute log-like returns between consecutive 5-min snapshots."""
        if len(self.prices) < 2:
            return []
        returns = []
        for i in range(1, len(self.prices)):
            p_prev = self.prices[i - 1]["price"]
            p_curr = self.prices[i]["price"]
            returns.append((p_curr - p_prev) / p_prev)
        return returns

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        """Connect to Binance and stream BTC/USDT trades indefinitely."""
        exchange = ccxtpro.binance()
        try:
            while stop_event is None or not stop_event.is_set():
                try:
                    trades = await exchange.watch_trades("BTC/USDT")
                    for trade in trades:
                        self._on_trade(trade)
                except Exception as e:
                    log.error("Binance feed error: %s", e)
                    await asyncio.sleep(1)
        finally:
            await exchange.close()
```

**Step 4: Run tests**

Run: `cd /c/Users/Matt/polypocket && python -m pytest tests/test_binance_feed.py -v`
Expected: 3 PASSED

**Step 5: Commit**

```bash
cd /c/Users/Matt/polypocket && git add -A && git commit -m "feat: Binance BTC/USDT price feed via ccxt pro"
```

---

## Task 3: Polymarket Market Discovery & Order Book Feed

**Files:**
- Create: `polypocket/feeds/polymarket.py`
- Create: `tests/test_polymarket_feed.py`

Reference: `C:/Users/Matt/polymarket-arb/monitor.py` for WS patterns (heartbeat, reconnect, book event parsing).

**Step 1: Write the failing test**

```python
# tests/test_polymarket_feed.py
import json
from polypocket.feeds.polymarket import (
    parse_5min_btc_markets,
    parse_book_event,
    Window,
)


def test_parse_5min_btc_markets():
    """Should extract active 5-min BTC up/down markets from Gamma API response."""
    markets = [
        {
            "condition_id": "abc123",
            "question": "Bitcoin Up or Down - April 14, 4:00PM-4:05PM ET",
            "slug": "btc-updown-5m-1776196800",
            "tokens": [
                {"token_id": "tok_yes", "outcome": "Up"},
                {"token_id": "tok_no", "outcome": "Down"},
            ],
            "end_date_iso": "2026-04-14T20:05:00Z",
            "closed": False,
        },
        {
            "condition_id": "def456",
            "question": "Will ETH hit $5000?",
            "slug": "eth-5000",
            "tokens": [],
            "end_date_iso": "2026-05-01T00:00:00Z",
            "closed": False,
        },
    ]
    windows = parse_5min_btc_markets(markets)
    assert len(windows) == 1
    assert windows[0].condition_id == "abc123"
    assert windows[0].up_token_id == "tok_yes"
    assert windows[0].down_token_id == "tok_no"


def test_parse_book_event():
    """Should extract best ask price and size from a book event."""
    msg = {
        "event_type": "book",
        "asset_id": "tok_yes",
        "market": "abc123",
        "asks": [
            {"price": "0.58", "size": "100"},
            {"price": "0.60", "size": "50"},
        ],
        "bids": [
            {"price": "0.55", "size": "80"},
        ],
    }
    result = parse_book_event(msg)
    assert result["asset_id"] == "tok_yes"
    assert result["best_ask"] == 0.58
    assert result["best_ask_size"] == 100.0


def test_window_dataclass():
    w = Window(
        condition_id="abc",
        question="BTC Up or Down",
        up_token_id="tok_up",
        down_token_id="tok_down",
        end_time=1713100000.0,
        slug="btc-updown-5m-1776196800",
    )
    # Window start is 5 minutes before end
    assert w.start_time == 1713100000.0 - 300.0
```

**Step 2: Run test to verify it fails**

Run: `cd /c/Users/Matt/polypocket && python -m pytest tests/test_polymarket_feed.py -v`
Expected: FAIL — ModuleNotFoundError

**Step 3: Write the implementation**

```python
# polypocket/feeds/polymarket.py
"""Polymarket 5-minute BTC market discovery and order book feed.

Adapted from polymarket-arb/monitor.py — uses same WS protocol
(heartbeat, book events) but focused on discovering and tracking
5-minute BTC Up/Down windows specifically.
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field

import aiohttp
import websockets

from polypocket.config import POLYMARKET_WS

log = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
HEARTBEAT_INTERVAL = 8  # Polymarket requires ping every 10s, we use 8s
BTC_5MIN_SLUG_PATTERN = re.compile(r"btc-updown-5m-\d+")


@dataclass
class Window:
    """A single 5-minute BTC Up/Down market window."""
    condition_id: str
    question: str
    up_token_id: str
    down_token_id: str
    end_time: float        # epoch seconds when window closes
    slug: str
    up_ask: float | None = None
    up_ask_size: float | None = None
    down_ask: float | None = None
    down_ask_size: float | None = None

    @property
    def start_time(self) -> float:
        return self.end_time - 300.0

    @property
    def up_implied_prob(self) -> float | None:
        return self.up_ask

    @property
    def down_implied_prob(self) -> float | None:
        return self.down_ask


def parse_5min_btc_markets(markets: list[dict]) -> list[Window]:
    """Filter Gamma API markets to active 5-min BTC Up/Down windows."""
    windows = []
    for m in markets:
        slug = m.get("slug", "")
        if not BTC_5MIN_SLUG_PATTERN.match(slug):
            continue
        if m.get("closed"):
            continue
        tokens = m.get("tokens", [])
        up_token = None
        down_token = None
        for t in tokens:
            outcome = t.get("outcome", "").lower()
            if outcome == "up":
                up_token = t.get("token_id")
            elif outcome == "down":
                down_token = t.get("token_id")
        if not up_token or not down_token:
            continue

        end_iso = m.get("end_date_iso", "")
        # Parse ISO timestamp to epoch
        from datetime import datetime, timezone
        try:
            end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
            end_epoch = end_dt.timestamp()
        except (ValueError, AttributeError):
            continue

        windows.append(Window(
            condition_id=m["condition_id"],
            question=m.get("question", ""),
            up_token_id=up_token,
            down_token_id=down_token,
            end_time=end_epoch,
            slug=slug,
        ))
    return windows


def parse_book_event(msg: dict) -> dict:
    """Extract best ask price and size from a WS book event."""
    asks = msg.get("asks", [])
    best_ask = None
    best_ask_size = None
    if asks:
        # Asks are sorted by price ascending; first is best
        best_ask = float(asks[0]["price"])
        best_ask_size = float(asks[0]["size"])
    return {
        "asset_id": msg.get("asset_id"),
        "best_ask": best_ask,
        "best_ask_size": best_ask_size,
    }


async def fetch_active_windows() -> list[Window]:
    """Fetch currently active 5-min BTC Up/Down markets from Gamma API."""
    url = f"{GAMMA_API}/markets"
    params = {
        "closed": "false",
        "limit": 100,
        "tag": "btc",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                log.error("Gamma API returned %d", resp.status)
                return []
            data = await resp.json()
            return parse_5min_btc_markets(data)


async def subscribe_and_stream(
    windows: list[Window],
    on_book_update,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Connect to Polymarket WS, subscribe to window token IDs,
    and call on_book_update(window, side, best_ask, best_ask_size)
    for each book event.
    """
    token_to_window: dict[str, tuple[Window, str]] = {}
    for w in windows:
        token_to_window[w.up_token_id] = (w, "up")
        token_to_window[w.down_token_id] = (w, "down")

    token_ids = list(token_to_window.keys())
    if not token_ids:
        return

    backoff = 1
    while stop_event is None or not stop_event.is_set():
        try:
            async with websockets.connect(POLYMARKET_WS) as ws:
                # Subscribe to assets
                sub_msg = {
                    "type": "market",
                    "assets_ids": token_ids,
                }
                await ws.send(json.dumps(sub_msg))
                log.info("Subscribed to %d token IDs", len(token_ids))
                backoff = 1

                last_ping = time.time()
                while stop_event is None or not stop_event.is_set():
                    # Heartbeat
                    if time.time() - last_ping > HEARTBEAT_INTERVAL:
                        await ws.send("PING")
                        last_ping = time.time()

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=HEARTBEAT_INTERVAL)
                    except asyncio.TimeoutError:
                        continue

                    if raw == "PONG":
                        continue

                    try:
                        msgs = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    if not isinstance(msgs, list):
                        msgs = [msgs]

                    for msg in msgs:
                        event_type = msg.get("event_type")
                        if event_type != "book":
                            continue
                        asset_id = msg.get("asset_id")
                        if asset_id not in token_to_window:
                            continue

                        parsed = parse_book_event(msg)
                        window, side = token_to_window[asset_id]

                        if side == "up":
                            window.up_ask = parsed["best_ask"]
                            window.up_ask_size = parsed["best_ask_size"]
                        else:
                            window.down_ask = parsed["best_ask"]
                            window.down_ask_size = parsed["best_ask_size"]

                        if on_book_update:
                            await on_book_update(window, side)

        except Exception as e:
            log.error("Polymarket WS error: %s (reconnect in %ds)", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
```

**Step 4: Run tests**

Run: `cd /c/Users/Matt/polypocket && python -m pytest tests/test_polymarket_feed.py -v`
Expected: 3 PASSED

**Step 5: Commit**

```bash
cd /c/Users/Matt/polypocket && git add -A && git commit -m "feat: Polymarket 5-min BTC market discovery and order book feed"
```

---

## Task 4: Observation Logger (Validate Edge Exists)

**THIS IS THE MOST IMPORTANT GATE.** Before building the execution pipeline, we must verify that exploitable latency actually exists between BTC price movements and Polymarket odds. This task builds a standalone observation tool that logs both feeds side-by-side.

**Files:**
- Create: `polypocket/observer.py`
- Create: `tests/test_observer.py`

**Step 1: Write the failing test**

```python
# tests/test_observer.py
from polypocket.observer import ObservationRecord, compute_model_p_up
from math import isclose


def test_compute_model_p_up_btc_above_open():
    """When BTC is above window open with time remaining, P(Up) > 0.5."""
    p = compute_model_p_up(
        displacement=0.0005,    # +0.05%
        t_remaining=120.0,      # 2 min left
        sigma_5min=0.0012,      # 0.12% per 5-min window
    )
    assert p > 0.5
    assert p < 1.0


def test_compute_model_p_up_btc_below_open():
    """When BTC is below window open, P(Up) < 0.5."""
    p = compute_model_p_up(
        displacement=-0.0005,
        t_remaining=120.0,
        sigma_5min=0.0012,
    )
    assert p < 0.5
    assert p > 0.0


def test_compute_model_p_up_no_displacement():
    """Zero displacement gives P(Up) = 0.5 regardless of time or vol."""
    p = compute_model_p_up(
        displacement=0.0,
        t_remaining=120.0,
        sigma_5min=0.0012,
    )
    assert isclose(p, 0.5, abs_tol=1e-9)


def test_compute_model_p_up_near_expiry():
    """With very little time left and positive displacement, P(Up) -> 1.0."""
    p = compute_model_p_up(
        displacement=0.001,
        t_remaining=1.0,        # 1 second left
        sigma_5min=0.0012,
    )
    assert p > 0.99


def test_compute_model_p_up_zero_remaining():
    """With zero time remaining, P(Up) is 1.0 if positive, 0.0 if negative."""
    p_up = compute_model_p_up(displacement=0.001, t_remaining=0.0, sigma_5min=0.0012)
    p_dn = compute_model_p_up(displacement=-0.001, t_remaining=0.0, sigma_5min=0.0012)
    assert p_up == 1.0
    assert p_dn == 0.0


def test_observation_record():
    rec = ObservationRecord(
        timestamp=1713100000.0,
        window_slug="btc-updown-5m-123",
        btc_price=84231.42,
        window_open_price=84198.00,
        displacement=0.0004,
        t_remaining=180.0,
        sigma_5min=0.0012,
        model_p_up=0.62,
        market_p_up=0.575,
        edge=0.045,
    )
    assert rec.edge == 0.045
```

**Step 2: Run test to verify it fails**

Run: `cd /c/Users/Matt/polypocket && python -m pytest tests/test_observer.py -v`
Expected: FAIL — ModuleNotFoundError

**Step 3: Write the implementation**

```python
# polypocket/observer.py
"""Observation mode: log model P(Up) vs market P(Up) to validate edge.

Run this BEFORE building the execution pipeline. Connect to Binance +
Polymarket feeds, record every data point, and output a CSV for analysis.

Usage:
    python -m polypocket.observer

The P(Up) model:
    sigma_5min = std dev of 5-minute BTC returns (e.g. 0.0012 = 0.12%)
    displacement = (price_now - price_open) / price_open
    t_remaining = seconds left in 5-min window
    P(Up) = Phi(displacement / (sigma_5min * sqrt(t_remaining / 300)))

    sigma_5min is per-5-minute-window. We scale it by sqrt(t_remaining/300)
    to get the std dev of the remaining random walk.
"""

import asyncio
import csv
import logging
import time
from dataclasses import dataclass, asdict
from math import sqrt

from scipy.stats import norm

log = logging.getLogger(__name__)


@dataclass
class ObservationRecord:
    timestamp: float
    window_slug: str
    btc_price: float
    window_open_price: float
    displacement: float
    t_remaining: float
    sigma_5min: float
    model_p_up: float
    market_p_up: float | None
    edge: float | None


def compute_model_p_up(
    displacement: float,
    t_remaining: float,
    sigma_5min: float,
) -> float:
    """Compute probability BTC finishes above window open price.

    Args:
        displacement: (price_now - price_open) / price_open
        t_remaining: seconds remaining in the 5-min window
        sigma_5min: realized volatility as std dev of 5-min returns

    Returns:
        Probability in [0, 1].
    """
    if t_remaining <= 0:
        return 1.0 if displacement > 0 else (0.5 if displacement == 0 else 0.0)

    # Scale sigma from per-5-min to per-remaining-time
    sigma_remaining = sigma_5min * sqrt(t_remaining / 300.0)

    if sigma_remaining <= 0:
        return 1.0 if displacement > 0 else (0.5 if displacement == 0 else 0.0)

    z = displacement / sigma_remaining
    return float(norm.cdf(z))


def compute_realized_vol(returns: list[float], lookback: int = 50) -> float:
    """Compute realized volatility from a list of 5-minute returns.

    Returns std dev of the most recent `lookback` returns.
    Returns 0.0 if insufficient data.
    """
    if len(returns) < 2:
        return 0.0
    recent = returns[-lookback:]
    mean = sum(recent) / len(recent)
    variance = sum((r - mean) ** 2 for r in recent) / (len(recent) - 1)
    return variance ** 0.5


class Observer:
    """Connects to both feeds and logs observations to CSV."""

    def __init__(self, output_path: str = "observations.csv"):
        self.output_path = output_path
        self.records: list[ObservationRecord] = []

    def log_observation(self, record: ObservationRecord) -> None:
        self.records.append(record)
        log.info(
            "window=%s disp=%.4f%% t_rem=%.0fs model=%.1f%% mkt=%s edge=%s",
            record.window_slug,
            record.displacement * 100,
            record.t_remaining,
            record.model_p_up * 100,
            f"{record.market_p_up * 100:.1f}%" if record.market_p_up else "N/A",
            f"{record.edge * 100:.1f}%" if record.edge else "N/A",
        )

    def save_csv(self) -> None:
        if not self.records:
            return
        fieldnames = list(asdict(self.records[0]).keys())
        with open(self.output_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for rec in self.records:
                writer.writerow(asdict(rec))
        log.info("Saved %d observations to %s", len(self.records), self.output_path)
```

**Step 4: Run tests**

Run: `cd /c/Users/Matt/polypocket && python -m pytest tests/test_observer.py -v`
Expected: 6 PASSED

**Step 5: Commit**

```bash
cd /c/Users/Matt/polypocket && git add -A && git commit -m "feat: observation logger with P(Up) model for edge validation"
```

---

## Task 5: Observer CLI (Wire Feeds Together)

**Files:**
- Create: `polypocket/__main__.py`
- Modify: `polypocket/observer.py` — add `run_observer()` async entry point

**Step 1: Write the observer runner**

Add to the bottom of `polypocket/observer.py`:

```python
async def run_observer(duration_minutes: int = 60) -> None:
    """Run observation mode: connect feeds, log model vs market for N minutes.

    Usage: python -m polypocket observe
    """
    from polypocket.feeds.binance import BinanceFeed
    from polypocket.feeds.polymarket import fetch_active_windows, subscribe_and_stream
    from polypocket.config import VOLATILITY_LOOKBACK

    observer = Observer()
    binance = BinanceFeed()
    stop = asyncio.Event()

    # Track which window we're currently observing
    current_window = None
    window_open_price = None

    async def on_book_update(window, side):
        nonlocal current_window, window_open_price
        if binance.latest_price is None:
            return

        now = time.time()
        t_remaining = window.end_time - now
        if t_remaining < 0:
            return

        # Record open price on first observation of a new window
        if current_window is None or current_window.condition_id != window.condition_id:
            current_window = window
            window_open_price = binance.latest_price
            log.info("New window: %s, open price: %.2f", window.slug, window_open_price)

        if window_open_price is None:
            return

        displacement = (binance.latest_price - window_open_price) / window_open_price
        returns = binance.get_5min_returns()
        sigma = compute_realized_vol(returns, VOLATILITY_LOOKBACK)

        if sigma <= 0:
            sigma = 0.001  # fallback before we have enough data

        model_p_up = compute_model_p_up(displacement, t_remaining, sigma)
        market_p_up = window.up_ask
        edge = (model_p_up - market_p_up) if market_p_up else None

        observer.log_observation(ObservationRecord(
            timestamp=now,
            window_slug=window.slug,
            btc_price=binance.latest_price,
            window_open_price=window_open_price,
            displacement=displacement,
            t_remaining=t_remaining,
            sigma_5min=sigma,
            model_p_up=model_p_up,
            market_p_up=market_p_up,
            edge=edge,
        ))

    async def poll_windows():
        """Periodically discover new windows and subscribe."""
        while not stop.is_set():
            windows = await fetch_active_windows()
            if windows:
                log.info("Found %d active windows", len(windows))
                await subscribe_and_stream(windows, on_book_update, stop)
            await asyncio.sleep(30)

    log.info("Starting observation mode for %d minutes", duration_minutes)

    # Run feeds concurrently
    try:
        await asyncio.wait_for(
            asyncio.gather(
                binance.run(stop),
                poll_windows(),
            ),
            timeout=duration_minutes * 60,
        )
    except asyncio.TimeoutError:
        pass
    finally:
        stop.set()
        observer.save_csv()
        log.info("Observation complete. %d records saved.", len(observer.records))
```

**Step 2: Write __main__.py**

```python
# polypocket/__main__.py
"""CLI entry point: python -m polypocket <command>"""

import asyncio
import logging
import sys


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cmd = sys.argv[1] if len(sys.argv) > 1 else "observe"

    if cmd == "observe":
        from polypocket.observer import run_observer
        duration = int(sys.argv[2]) if len(sys.argv) > 2 else 60
        asyncio.run(run_observer(duration))
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: python -m polypocket observe [duration_minutes]")
        sys.exit(1)


if __name__ == "__main__":
    main()
```

**Step 3: Smoke test**

Run: `cd /c/Users/Matt/polypocket && python -m polypocket observe 1`
Expected: Connects to Binance, starts logging BTC prices. If no 5-min windows are active, logs "Found 0 active windows." Ctrl+C to stop. Should save observations.csv.

**Step 4: Commit**

```bash
cd /c/Users/Matt/polypocket && git add -A && git commit -m "feat: observer CLI wiring Binance + Polymarket feeds"
```

---

## Task 6: Investigate Chainlink Opening Price

**Files:**
- Create: `polypocket/feeds/chainlink.py`
- Create: `tests/test_chainlink.py`

**Purpose:** The market resolves on Chainlink BTC/USD, not Binance. We need to understand how Polymarket determines the "opening price" for each 5-min window. This task fetches a few resolved markets and inspects the resolution data.

**Step 1: Write investigation script**

```python
# polypocket/feeds/chainlink.py
"""Chainlink BTC/USD price feed — the resolution source for 5-min windows.

The Polymarket 5-min BTC markets resolve based on the Chainlink BTC/USD
data stream (https://data.chain.link/streams/btc-usd).

This module:
1. Polls for the current Chainlink BTC/USD price
2. Investigates how Polymarket maps Chainlink prices to window open/close
"""

import asyncio
import logging
import time

import aiohttp

log = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"


async def fetch_resolved_5min_markets(limit: int = 10) -> list[dict]:
    """Fetch recently resolved 5-min BTC markets to study resolution data."""
    url = f"{GAMMA_API}/markets"
    params = {
        "closed": "true",
        "limit": limit,
        "order": "end_date_iso",
        "ascending": "false",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            data = await resp.json()
            # Filter to 5-min BTC markets
            import re
            pattern = re.compile(r"btc-updown-5m-\d+")
            return [m for m in data if pattern.match(m.get("slug", ""))]


async def investigate_resolution():
    """Print resolution details of recent 5-min windows.

    Run: python -c "import asyncio; from polypocket.feeds.chainlink import investigate_resolution; asyncio.run(investigate_resolution())"
    """
    markets = await fetch_resolved_5min_markets(20)
    print(f"\nFound {len(markets)} resolved 5-min BTC markets:\n")
    for m in markets:
        print(f"  Slug: {m.get('slug')}")
        print(f"  Question: {m.get('question')}")
        print(f"  End date: {m.get('end_date_iso')}")
        print(f"  Outcome: {m.get('outcome')}")
        print(f"  Resolution source: {m.get('resolution_source')}")
        # Print any resolution-related fields
        for key in sorted(m.keys()):
            if "resol" in key.lower() or "price" in key.lower() or "result" in key.lower():
                print(f"  {key}: {m[key]}")
        print()
```

**Step 2: Write a basic test**

```python
# tests/test_chainlink.py
from polypocket.feeds.chainlink import fetch_resolved_5min_markets
import pytest


@pytest.mark.asyncio
async def test_fetch_resolved_markets_returns_list():
    """Integration test — hits real Gamma API."""
    markets = await fetch_resolved_5min_markets(5)
    assert isinstance(markets, list)
    # May be empty if no resolved markets available
```

**Step 3: Run the investigation**

Run: `cd /c/Users/Matt/polypocket && python -c "import asyncio; from polypocket.feeds.chainlink import investigate_resolution; asyncio.run(investigate_resolution())"`
Expected: Prints resolution details. Study the output to understand:
- What fields contain the opening/closing prices
- Whether resolution_source confirms Chainlink
- What timestamp precision is used

**Step 4: Document findings**

Based on the investigation output, update `polypocket/feeds/chainlink.py` with:
- The exact field names for open/close prices
- How to compute the opening price for a given window
- Any discrepancy between Chainlink and Binance timing

**Step 5: Commit**

```bash
cd /c/Users/Matt/polypocket && git add -A && git commit -m "feat: Chainlink feed + resolution investigation"
```

---

## Task 7: Signal Engine

**Files:**
- Create: `polypocket/signal.py`
- Create: `tests/test_signal.py`

The signal engine pulls together: P(Up) model (from observer.py), volatility estimation, edge calculation, and entry timing rules.

**Step 1: Write the failing tests**

```python
# tests/test_signal.py
from polypocket.signal import SignalEngine, Signal
from polypocket.feeds.binance import BinanceFeed


def test_signal_engine_no_signal_too_early():
    """Should not produce signal in first 60 seconds."""
    engine = SignalEngine()
    signal = engine.evaluate(
        displacement=0.001,
        t_elapsed=30.0,
        t_remaining=270.0,
        sigma_5min=0.0012,
        market_p_up=0.55,
    )
    assert signal is None


def test_signal_engine_no_signal_too_late():
    """Should not produce signal with < 30s remaining."""
    engine = SignalEngine()
    signal = engine.evaluate(
        displacement=0.001,
        t_elapsed=280.0,
        t_remaining=20.0,
        sigma_5min=0.0012,
        market_p_up=0.55,
    )
    assert signal is None


def test_signal_engine_no_signal_insufficient_edge():
    """Small displacement = small edge = no signal."""
    engine = SignalEngine()
    signal = engine.evaluate(
        displacement=0.00001,    # tiny displacement
        t_elapsed=120.0,
        t_remaining=180.0,
        sigma_5min=0.0012,
        market_p_up=0.50,
    )
    assert signal is None


def test_signal_engine_up_signal():
    """Large positive displacement with stale market = UP signal."""
    engine = SignalEngine()
    signal = engine.evaluate(
        displacement=0.002,      # +0.2% — substantial move
        t_elapsed=120.0,
        t_remaining=180.0,
        sigma_5min=0.0012,
        market_p_up=0.55,       # Market hasn't caught up
    )
    assert signal is not None
    assert signal.side == "up"
    assert signal.edge > 0.05   # MIN_EDGE + FEE


def test_signal_engine_down_signal():
    """Large negative displacement with stale market = DOWN signal."""
    engine = SignalEngine()
    signal = engine.evaluate(
        displacement=-0.002,
        t_elapsed=120.0,
        t_remaining=180.0,
        sigma_5min=0.0012,
        market_p_up=0.50,       # Market still at 50/50
    )
    assert signal is not None
    assert signal.side == "down"
    assert signal.edge > 0.05


def test_signal_engine_no_signal_missing_market_price():
    """If market price is None, no signal."""
    engine = SignalEngine()
    signal = engine.evaluate(
        displacement=0.002,
        t_elapsed=120.0,
        t_remaining=180.0,
        sigma_5min=0.0012,
        market_p_up=None,
    )
    assert signal is None
```

**Step 2: Run test to verify it fails**

Run: `cd /c/Users/Matt/polypocket && python -m pytest tests/test_signal.py -v`
Expected: FAIL

**Step 3: Write the implementation**

```python
# polypocket/signal.py
"""Signal engine: evaluate edge and produce trading signals."""

from dataclasses import dataclass

from polypocket.config import (
    FEE_RATE,
    MIN_EDGE_THRESHOLD,
    WINDOW_ENTRY_MIN_ELAPSED,
    WINDOW_ENTRY_MIN_REMAINING,
)
from polypocket.observer import compute_model_p_up


@dataclass
class Signal:
    side: str            # "up" or "down"
    model_p_up: float    # model's P(Up) estimate
    market_p_up: float   # Polymarket's implied P(Up)
    edge: float          # model_p - market_p for chosen side


class SignalEngine:
    """Evaluates whether an exploitable edge exists in the current window."""

    def evaluate(
        self,
        displacement: float,
        t_elapsed: float,
        t_remaining: float,
        sigma_5min: float,
        market_p_up: float | None,
    ) -> Signal | None:
        """Evaluate signal for current window state.

        Returns a Signal if edge exceeds threshold, None otherwise.
        """
        # Timing gates
        if t_elapsed < WINDOW_ENTRY_MIN_ELAPSED:
            return None
        if t_remaining < WINDOW_ENTRY_MIN_REMAINING:
            return None
        if market_p_up is None:
            return None
        if sigma_5min <= 0:
            return None

        model_p_up = compute_model_p_up(displacement, t_remaining, sigma_5min)

        # Edge for betting UP
        up_edge = model_p_up - market_p_up
        # Edge for betting DOWN
        down_edge = (1.0 - model_p_up) - (1.0 - market_p_up)
        # Note: down_edge == -up_edge, so we just pick the positive side

        min_required = MIN_EDGE_THRESHOLD + FEE_RATE

        if up_edge >= min_required:
            return Signal(
                side="up",
                model_p_up=model_p_up,
                market_p_up=market_p_up,
                edge=up_edge,
            )
        elif (-up_edge) >= min_required:
            return Signal(
                side="down",
                model_p_up=model_p_up,
                market_p_up=market_p_up,
                edge=-up_edge,
            )

        return None
```

**Step 4: Run tests**

Run: `cd /c/Users/Matt/polypocket && python -m pytest tests/test_signal.py -v`
Expected: 6 PASSED

**Step 5: Commit**

```bash
cd /c/Users/Matt/polypocket && git add -A && git commit -m "feat: signal engine with edge detection and timing gates"
```

---

## Task 8: Ledger (SQLite Trade/Position Tracking)

**Files:**
- Create: `polypocket/ledger.py`
- Create: `tests/test_ledger.py`

Reference: `C:/Users/Matt/polymarket-arb/paper.py` and `C:/Users/Matt/polymarket-arb/logger.py` for SQLite patterns.

**Step 1: Write the failing tests**

```python
# tests/test_ledger.py
import os
import tempfile
from polypocket.ledger import (
    init_db,
    log_trade,
    get_daily_pnl,
    get_recent_trades,
    get_session_stats,
    get_paper_balance,
    deduct_paper_balance,
    credit_paper_balance,
)


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    return path


def test_init_creates_tables():
    db = make_db()
    # Should not raise
    init_db(db)
    os.unlink(db)


def test_log_and_retrieve_trade():
    db = make_db()
    log_trade(
        db,
        window_slug="btc-5m-123",
        side="up",
        entry_price=0.575,
        size=10.0,
        fees=0.115,
        model_p_up=0.72,
        market_p_up=0.575,
        edge=0.145,
        outcome=None,
        pnl=None,
        status="open",
    )
    trades = get_recent_trades(db, limit=10)
    assert len(trades) == 1
    assert trades[0]["side"] == "up"
    assert trades[0]["entry_price"] == 0.575
    os.unlink(db)


def test_daily_pnl():
    db = make_db()
    log_trade(db, "w1", "up", 0.55, 10, 0.11, 0.7, 0.55, 0.15, "up", 3.4, "settled")
    log_trade(db, "w2", "down", 0.50, 10, 0.10, 0.3, 0.50, 0.2, "up", -5.1, "settled")
    pnl = get_daily_pnl(db)
    assert abs(pnl - (-1.7)) < 0.01
    os.unlink(db)


def test_session_stats():
    db = make_db()
    log_trade(db, "w1", "up", 0.55, 10, 0.11, 0.7, 0.55, 0.15, "up", 3.0, "settled")
    log_trade(db, "w2", "down", 0.50, 10, 0.10, 0.3, 0.50, 0.2, "up", -5.0, "settled")
    log_trade(db, "w3", "up", 0.60, 10, 0.12, 0.8, 0.60, 0.2, "up", 2.8, "settled")
    stats = get_session_stats(db)
    assert stats["wins"] == 2
    assert stats["losses"] == 1
    assert stats["total"] == 3
    os.unlink(db)


def test_paper_balance():
    db = make_db()
    bal = get_paper_balance(db)
    assert bal == 1000.0  # default starting balance
    deduct_paper_balance(db, 50.0)
    assert get_paper_balance(db) == 950.0
    credit_paper_balance(db, 60.0)
    assert get_paper_balance(db) == 1010.0
    os.unlink(db)
```

**Step 2: Run test to verify it fails**

Run: `cd /c/Users/Matt/polypocket && python -m pytest tests/test_ledger.py -v`
Expected: FAIL

**Step 3: Write the implementation**

```python
# polypocket/ledger.py
"""SQLite trade and paper-balance ledger."""

import sqlite3
from contextlib import closing
from datetime import date

from polypocket.config import PAPER_STARTING_BALANCE


def init_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                window_slug TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                size REAL NOT NULL,
                fees REAL NOT NULL,
                model_p_up REAL,
                market_p_up REAL,
                edge REAL,
                outcome TEXT,
                pnl REAL,
                status TEXT NOT NULL DEFAULT 'open'
            );

            CREATE TABLE IF NOT EXISTS paper_account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cash_balance REAL NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            INSERT OR IGNORE INTO paper_account (id, cash_balance)
            VALUES (1, {balance});
        """.format(balance=PAPER_STARTING_BALANCE))


def log_trade(
    db_path: str,
    window_slug: str,
    side: str,
    entry_price: float,
    size: float,
    fees: float,
    model_p_up: float,
    market_p_up: float,
    edge: float,
    outcome: str | None,
    pnl: float | None,
    status: str,
) -> int:
    with closing(sqlite3.connect(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO trades
               (window_slug, side, entry_price, size, fees,
                model_p_up, market_p_up, edge, outcome, pnl, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (window_slug, side, entry_price, size, fees,
             model_p_up, market_p_up, edge, outcome, pnl, status),
        )
        conn.commit()
        return cur.lastrowid


def update_trade(db_path: str, trade_id: int, outcome: str, pnl: float, status: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "UPDATE trades SET outcome=?, pnl=?, status=? WHERE id=?",
            (outcome, pnl, status, trade_id),
        )
        conn.commit()


def get_recent_trades(db_path: str, limit: int = 20) -> list[dict]:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_daily_pnl(db_path: str) -> float:
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(pnl), 0.0) FROM trades
               WHERE date(timestamp) = date('now') AND pnl IS NOT NULL"""
        ).fetchone()
        return row[0]


def get_session_stats(db_path: str) -> dict:
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            """SELECT pnl FROM trades
               WHERE date(timestamp) = date('now') AND pnl IS NOT NULL"""
        ).fetchall()
    wins = sum(1 for r in rows if r[0] > 0)
    losses = sum(1 for r in rows if r[0] < 0)
    return {
        "wins": wins,
        "losses": losses,
        "total": wins + losses,
        "pnl": sum(r[0] for r in rows),
    }


def get_paper_balance(db_path: str) -> float:
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute("SELECT cash_balance FROM paper_account WHERE id=1").fetchone()
        return row[0]


def deduct_paper_balance(db_path: str, amount: float) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "UPDATE paper_account SET cash_balance = cash_balance - ?, updated_at = CURRENT_TIMESTAMP WHERE id=1",
            (amount,),
        )
        conn.commit()


def credit_paper_balance(db_path: str, amount: float) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "UPDATE paper_account SET cash_balance = cash_balance + ?, updated_at = CURRENT_TIMESTAMP WHERE id=1",
            (amount,),
        )
        conn.commit()
```

**Step 4: Run tests**

Run: `cd /c/Users/Matt/polypocket && python -m pytest tests/test_ledger.py -v`
Expected: 5 PASSED

**Step 5: Commit**

```bash
cd /c/Users/Matt/polypocket && git add -A && git commit -m "feat: SQLite trade ledger with paper balance tracking"
```

---

## Task 9: Paper Executor

**Files:**
- Create: `polypocket/executor.py`
- Create: `tests/test_executor.py`

**Step 1: Write the failing tests**

```python
# tests/test_executor.py
import os
import tempfile
from polypocket.executor import execute_paper_trade, TradeResult
from polypocket.ledger import init_db, get_paper_balance
from polypocket.signal import Signal


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    return path


def test_paper_trade_up_win():
    db = make_db()
    signal = Signal(side="up", model_p_up=0.75, market_p_up=0.55, edge=0.20)
    result = execute_paper_trade(
        db_path=db,
        signal=signal,
        entry_price=0.55,
        size=10.0,
        window_slug="btc-5m-123",
        outcome="up",
    )
    assert result.success is True
    assert result.pnl > 0
    # Balance should have gone down (entry cost) then back up (settlement)
    bal = get_paper_balance(db)
    assert bal > 990.0  # started at 1000, won
    os.unlink(db)


def test_paper_trade_up_loss():
    db = make_db()
    signal = Signal(side="up", model_p_up=0.75, market_p_up=0.55, edge=0.20)
    result = execute_paper_trade(
        db_path=db,
        signal=signal,
        entry_price=0.55,
        size=10.0,
        window_slug="btc-5m-456",
        outcome="down",
    )
    assert result.success is True
    assert result.pnl < 0
    bal = get_paper_balance(db)
    assert bal < 1000.0
    os.unlink(db)


def test_paper_trade_insufficient_balance():
    db = make_db()
    signal = Signal(side="up", model_p_up=0.75, market_p_up=0.55, edge=0.20)
    result = execute_paper_trade(
        db_path=db,
        signal=signal,
        entry_price=0.55,
        size=20000.0,  # way more than $1000 balance
        window_slug="btc-5m-789",
        outcome="up",
    )
    assert result.success is False
    assert "balance" in result.error.lower()
    os.unlink(db)
```

**Step 2: Run test to verify it fails**

Run: `cd /c/Users/Matt/polypocket && python -m pytest tests/test_executor.py -v`
Expected: FAIL

**Step 3: Write the implementation**

```python
# polypocket/executor.py
"""Trade execution — paper mode and (future) live mode.

Paper mode: simulate fills, immediately resolve based on window outcome.
Live mode: place FOK order via CLOB, hold to resolution.
"""

import logging
from dataclasses import dataclass

from polypocket.config import FEE_RATE
from polypocket.ledger import (
    log_trade,
    update_trade,
    get_paper_balance,
    deduct_paper_balance,
    credit_paper_balance,
)
from polypocket.signal import Signal

log = logging.getLogger(__name__)


@dataclass
class TradeResult:
    success: bool
    trade_id: int | None = None
    pnl: float | None = None
    error: str | None = None


def execute_paper_trade(
    db_path: str,
    signal: Signal,
    entry_price: float,
    size: float,
    window_slug: str,
    outcome: str | None = None,
) -> TradeResult:
    """Execute a paper trade. If outcome is provided, settle immediately.

    Args:
        db_path: SQLite database path
        signal: The trading signal
        entry_price: Price to buy at (best ask for chosen side)
        size: Number of shares
        window_slug: Window identifier
        outcome: "up" or "down" if known (for immediate settlement)
    """
    cost = entry_price * size
    fees = cost * FEE_RATE

    # Check balance
    balance = get_paper_balance(db_path)
    if balance < cost + fees:
        return TradeResult(
            success=False,
            error=f"Insufficient balance: need ${cost + fees:.2f}, have ${balance:.2f}",
        )

    # Deduct cost + fees
    deduct_paper_balance(db_path, cost + fees)

    # Calculate P&L if outcome is known
    pnl = None
    status = "open"
    if outcome is not None:
        won = (signal.side == outcome)
        if won:
            payout = size * 1.0  # winning shares pay $1 each
            pnl = payout - cost - fees
        else:
            payout = 0.0         # losing shares pay $0
            pnl = -cost - fees
        # Credit payout
        credit_paper_balance(db_path, payout)
        status = "settled"

    trade_id = log_trade(
        db_path=db_path,
        window_slug=window_slug,
        side=signal.side,
        entry_price=entry_price,
        size=size,
        fees=fees,
        model_p_up=signal.model_p_up,
        market_p_up=signal.market_p_up,
        edge=signal.edge,
        outcome=outcome,
        pnl=pnl,
        status=status,
    )

    if pnl is not None:
        log.info(
            "Paper trade %s: %s @ $%.3f x%.1f -> %s (P&L: $%.2f)",
            window_slug, signal.side, entry_price, size,
            "WON" if pnl > 0 else "LOST", pnl,
        )

    return TradeResult(success=True, trade_id=trade_id, pnl=pnl)


def settle_paper_trade(
    db_path: str,
    trade_id: int,
    entry_price: float,
    size: float,
    side: str,
    outcome: str,
) -> float:
    """Settle an open paper trade when the window resolves.

    Returns realized P&L.
    """
    fees = entry_price * size * FEE_RATE
    cost = entry_price * size
    won = (side == outcome)
    payout = size * 1.0 if won else 0.0
    pnl = payout - cost - fees

    credit_paper_balance(db_path, payout)
    update_trade(db_path, trade_id, outcome=outcome, pnl=pnl, status="settled")

    return pnl
```

**Step 4: Run tests**

Run: `cd /c/Users/Matt/polypocket && python -m pytest tests/test_executor.py -v`
Expected: 3 PASSED

**Step 5: Commit**

```bash
cd /c/Users/Matt/polypocket && git add -A && git commit -m "feat: paper trade executor with immediate and deferred settlement"
```

---

## Task 10: Risk Manager

**Files:**
- Create: `polypocket/risk.py`
- Create: `tests/test_risk.py`

**Step 1: Write the failing tests**

```python
# tests/test_risk.py
import os
import tempfile
from polypocket.risk import RiskManager
from polypocket.ledger import init_db, log_trade


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    return path


def test_risk_allows_normal_trade():
    db = make_db()
    rm = RiskManager(db_path=db)
    ok, reason = rm.check()
    assert ok is True
    assert reason == ""
    os.unlink(db)


def test_risk_blocks_after_max_daily_loss():
    db = make_db()
    # Log enough losing trades to exceed MAX_DAILY_LOSS ($50)
    for i in range(6):
        log_trade(db, f"w{i}", "up", 0.5, 20, 0.2, 0.6, 0.5, 0.1, "down", -10.2, "settled")
    rm = RiskManager(db_path=db)
    ok, reason = rm.check()
    assert ok is False
    assert "daily loss" in reason.lower()
    os.unlink(db)


def test_risk_blocks_after_consecutive_losses():
    db = make_db()
    rm = RiskManager(db_path=db)
    for _ in range(5):
        rm.record_loss()
    ok, reason = rm.check()
    assert ok is False
    assert "consecutive" in reason.lower()
    os.unlink(db)


def test_risk_resets_consecutive_on_win():
    db = make_db()
    rm = RiskManager(db_path=db)
    for _ in range(4):
        rm.record_loss()
    rm.record_win()
    ok, reason = rm.check()
    assert ok is True
    os.unlink(db)
```

**Step 2: Run test to verify it fails**

Run: `cd /c/Users/Matt/polypocket && python -m pytest tests/test_risk.py -v`
Expected: FAIL

**Step 3: Write the implementation**

```python
# polypocket/risk.py
"""Risk manager: daily loss limit and consecutive loss tracking."""

import logging

from polypocket.config import MAX_DAILY_LOSS, MAX_CONSECUTIVE_LOSSES
from polypocket.ledger import get_daily_pnl

log = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._consecutive_losses = 0

    def check(self) -> tuple[bool, str]:
        """Check if trading is allowed. Returns (ok, reason)."""
        # Daily loss limit
        daily_pnl = get_daily_pnl(self.db_path)
        if daily_pnl < -MAX_DAILY_LOSS:
            return False, f"Daily loss limit hit: ${daily_pnl:.2f} < -${MAX_DAILY_LOSS}"

        # Consecutive losses
        if self._consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            return False, f"Consecutive loss limit: {self._consecutive_losses} >= {MAX_CONSECUTIVE_LOSSES}"

        return True, ""

    def record_loss(self) -> None:
        self._consecutive_losses += 1
        log.warning("Consecutive losses: %d / %d", self._consecutive_losses, MAX_CONSECUTIVE_LOSSES)

    def record_win(self) -> None:
        self._consecutive_losses = 0
```

**Step 4: Run tests**

Run: `cd /c/Users/Matt/polypocket && python -m pytest tests/test_risk.py -v`
Expected: 4 PASSED

**Step 5: Commit**

```bash
cd /c/Users/Matt/polypocket && git add -A && git commit -m "feat: risk manager with daily loss and consecutive loss limits"
```

---

## Task 11: Bot Orchestrator

**Files:**
- Create: `polypocket/bot.py`
- Modify: `polypocket/__main__.py` — add `run` command

This ties everything together: feeds → signal → risk → executor. Runs as an async event loop.

**Step 1: Write the implementation**

```python
# polypocket/bot.py
"""Main bot orchestrator: connects feeds, evaluates signals, executes trades."""

import asyncio
import logging
import time

from polypocket.config import (
    POSITION_SIZE_USDC,
    TRADING_MODE,
    PAPER_DB_PATH,
    VOLATILITY_LOOKBACK,
    WINDOW_ENTRY_MIN_ELAPSED,
)
from polypocket.feeds.binance import BinanceFeed
from polypocket.feeds.polymarket import (
    Window,
    fetch_active_windows,
    subscribe_and_stream,
)
from polypocket.observer import compute_realized_vol
from polypocket.signal import SignalEngine, Signal
from polypocket.executor import execute_paper_trade, settle_paper_trade
from polypocket.risk import RiskManager
from polypocket.ledger import init_db

log = logging.getLogger(__name__)


class Bot:
    def __init__(self, db_path: str = PAPER_DB_PATH):
        self.db_path = db_path
        self.binance = BinanceFeed()
        self.signal_engine = SignalEngine()
        self.risk = RiskManager(db_path=db_path)
        self.stop = asyncio.Event()

        # State per window
        self._current_window_id: str | None = None
        self._window_open_price: float | None = None
        self._window_traded: bool = False
        self._open_trade: dict | None = None  # {trade_id, side, entry_price, size}

        # Stats (for TUI)
        self.stats = {
            "btc_price": None,
            "window_open_price": None,
            "displacement": None,
            "model_p_up": None,
            "market_p_up": None,
            "edge": None,
            "sigma_5min": None,
            "t_remaining": None,
            "window_slug": None,
            "position": None,
        }

        # Callbacks for TUI
        self.on_trade = None      # Called with (TradeResult, signal, window_slug)
        self.on_stats_update = None

    async def _on_book_update(self, window: Window, side: str) -> None:
        """Called on every Polymarket order book update."""
        if self.binance.latest_price is None:
            return

        now = time.time()
        t_remaining = window.end_time - now
        t_elapsed = now - window.start_time

        # New window detection
        if self._current_window_id != window.condition_id:
            # Settle previous trade if still open
            if self._open_trade and self._current_window_id:
                await self._settle_previous_window(window)

            self._current_window_id = window.condition_id
            self._window_open_price = self.binance.latest_price
            self._window_traded = False
            self._open_trade = None
            log.info("New window: %s open=%.2f", window.slug, self._window_open_price)

        if self._window_open_price is None:
            return

        # Compute signal inputs
        displacement = (self.binance.latest_price - self._window_open_price) / self._window_open_price
        returns = self.binance.get_5min_returns()
        sigma = compute_realized_vol(returns, VOLATILITY_LOOKBACK)
        if sigma <= 0:
            sigma = 0.001

        # Update stats for TUI
        from polypocket.observer import compute_model_p_up
        model_p = compute_model_p_up(displacement, max(t_remaining, 0), sigma)
        self.stats.update({
            "btc_price": self.binance.latest_price,
            "window_open_price": self._window_open_price,
            "displacement": displacement,
            "model_p_up": model_p,
            "market_p_up": window.up_ask,
            "edge": (model_p - window.up_ask) if window.up_ask else None,
            "sigma_5min": sigma,
            "t_remaining": t_remaining,
            "window_slug": window.slug,
        })
        if self.on_stats_update:
            self.on_stats_update(self.stats)

        # Window expired — settle
        if t_remaining <= 0:
            if self._open_trade:
                outcome = "up" if self.binance.latest_price >= self._window_open_price else "down"
                await self._settle_trade(outcome)
            return

        # Already traded this window
        if self._window_traded:
            return

        # Evaluate signal
        signal = self.signal_engine.evaluate(
            displacement=displacement,
            t_elapsed=t_elapsed,
            t_remaining=t_remaining,
            sigma_5min=sigma,
            market_p_up=window.up_ask,
        )

        if signal is None:
            return

        # Risk check
        ok, reason = self.risk.check()
        if not ok:
            log.warning("Risk blocked: %s", reason)
            return

        # Execute
        entry_price = window.up_ask if signal.side == "up" else window.down_ask
        if entry_price is None:
            return

        size = POSITION_SIZE_USDC / entry_price

        log.info(
            "SIGNAL: %s edge=%.1f%% (model=%.1f%% mkt=%.1f%%) -> %s @ $%.3f x%.1f",
            signal.side.upper(), signal.edge * 100,
            signal.model_p_up * 100, signal.market_p_up * 100,
            signal.side, entry_price, size,
        )

        result = execute_paper_trade(
            db_path=self.db_path,
            signal=signal,
            entry_price=entry_price,
            size=size,
            window_slug=window.slug,
        )

        if result.success:
            self._window_traded = True
            self._open_trade = {
                "trade_id": result.trade_id,
                "side": signal.side,
                "entry_price": entry_price,
                "size": size,
            }
            self.stats["position"] = f"{size:.1f} {signal.side.upper()} @ ${entry_price:.3f}"
            if self.on_trade:
                self.on_trade(result, signal, window.slug)

    async def _settle_trade(self, outcome: str) -> None:
        if not self._open_trade:
            return
        t = self._open_trade
        pnl = settle_paper_trade(
            self.db_path, t["trade_id"], t["entry_price"], t["size"], t["side"], outcome,
        )
        if pnl > 0:
            self.risk.record_win()
        else:
            self.risk.record_loss()
        log.info("SETTLED: %s -> P&L $%.2f", outcome.upper(), pnl)
        self._open_trade = None
        self.stats["position"] = None
        if self.on_trade:
            from polypocket.executor import TradeResult
            self.on_trade(TradeResult(success=True, trade_id=t["trade_id"], pnl=pnl), None, None)

    async def _settle_previous_window(self, new_window: Window) -> None:
        """Settle the previous window's trade based on final BTC price."""
        if not self._open_trade or not self._window_open_price:
            return
        # Use current BTC price as the close of the previous window
        outcome = "up" if self.binance.latest_price >= self._window_open_price else "down"
        await self._settle_trade(outcome)

    async def run(self) -> None:
        init_db(self.db_path)
        log.info("Polypocket bot starting (mode=%s)", TRADING_MODE)

        async def poll_and_stream():
            while not self.stop.is_set():
                windows = await fetch_active_windows()
                if windows:
                    log.info("Tracking %d active windows", len(windows))
                    await subscribe_and_stream(windows, self._on_book_update, self.stop)
                await asyncio.sleep(30)

        try:
            await asyncio.gather(
                self.binance.run(self.stop),
                poll_and_stream(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            self.stop.set()
            log.info("Bot stopped.")
```

**Step 2: Update __main__.py**

Add to `polypocket/__main__.py`:

```python
# Add this elif branch in the main() function, after the "observe" branch:
    elif cmd == "run":
        from polypocket.bot import Bot
        bot = Bot()
        try:
            asyncio.run(bot.run())
        except KeyboardInterrupt:
            pass
```

**Step 3: Smoke test**

Run: `cd /c/Users/Matt/polypocket && python -m polypocket run`
Expected: Bot starts, connects to Binance, polls for windows, logs signal evaluations. Ctrl+C to stop.

**Step 4: Commit**

```bash
cd /c/Users/Matt/polypocket && git add -A && git commit -m "feat: bot orchestrator wiring feeds -> signal -> risk -> executor"
```

---

## Task 12: TUI Dashboard

**Files:**
- Create: `polypocket/tui.py`
- Modify: `polypocket/__main__.py` — add `tui` command

Reference: `C:/Users/Matt/polymarket-arb/tui.py` for Textual patterns (panels, keybinds, log handler, refresh loop).

**Step 1: Write the TUI**

```python
# polypocket/tui.py
"""Textual TUI dashboard for Polypocket bot."""

import asyncio
import logging
import threading
from datetime import datetime, timedelta

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Header, Footer, Static, RichLog, Input
from textual.binding import Binding
from textual import work

from polypocket.bot import Bot
from polypocket.config import (
    MIN_EDGE_THRESHOLD,
    FEE_RATE,
    POSITION_SIZE_USDC,
    MAX_DAILY_LOSS,
    TRADING_MODE,
)
from polypocket.ledger import (
    init_db,
    get_daily_pnl,
    get_recent_trades,
    get_session_stats,
    get_paper_balance,
)

log = logging.getLogger(__name__)


class StatusPanel(Static):
    def update_stats(self, stats: dict, db_path: str) -> None:
        btc = stats.get("btc_price")
        open_p = stats.get("window_open_price")
        disp = stats.get("displacement")
        model = stats.get("model_p_up")
        market = stats.get("market_p_up")
        edge = stats.get("edge")
        sigma = stats.get("sigma_5min")
        t_rem = stats.get("t_remaining")
        pos = stats.get("position")
        slug = stats.get("window_slug", "")

        bal = get_paper_balance(db_path)
        daily = get_daily_pnl(db_path)

        lines = ["[bold]STATUS[/bold]", ""]
        lines.append(f"BTC Price: ${btc:,.2f}" if btc else "BTC Price: --")
        lines.append(f"Window Open: ${open_p:,.2f}" if open_p else "Window Open: --")
        lines.append(f"Displacement: {disp:+.4%}" if disp is not None else "Displacement: --")
        lines.append(f"P(Up) Model: {model:.1%}" if model is not None else "P(Up) Model: --")
        lines.append(f"P(Up) Market: {market:.1%}" if market is not None else "P(Up) Market: --")
        lines.append(f"Edge: {edge:+.1%}" if edge is not None else "Edge: --")
        lines.append(f"Volatility: {sigma:.4%}" if sigma else "Volatility: --")
        lines.append("")
        lines.append(f"Paper Balance: ${bal:,.2f}")
        lines.append(f"Daily P&L: ${daily:+,.2f}")
        if pos:
            lines.append(f"Position: {pos}")

        self.update("\n".join(lines))


class WindowPanel(Static):
    def update_stats(self, stats: dict) -> None:
        slug = stats.get("window_slug", "--")
        t_rem = stats.get("t_remaining")
        model = stats.get("model_p_up")
        market = stats.get("market_p_up")
        edge = stats.get("edge")

        lines = ["[bold]ACTIVE WINDOW[/bold]", ""]
        lines.append(f"Window: {slug}")
        if t_rem is not None and t_rem > 0:
            m, s = divmod(int(t_rem), 60)
            lines.append(f"Time Left: {m}m {s:02d}s")
        else:
            lines.append("Time Left: --")

        if model is not None and market is not None:
            lines.append(f"Model: {model:.1%}  Market: {market:.1%}")
            if edge is not None:
                indicator = " SIGNAL" if abs(edge) > MIN_EDGE_THRESHOLD + FEE_RATE else ""
                lines.append(f"Edge: {edge:+.1%}{indicator}")

        self.update("\n".join(lines))


class TradesPanel(Static):
    def update_trades(self, db_path: str) -> None:
        trades = get_recent_trades(db_path, limit=8)
        lines = ["[bold]RECENT TRADES[/bold]", ""]
        if not trades:
            lines.append("  No trades yet")
        for t in trades:
            ts = t["timestamp"][:8] if t["timestamp"] else ""
            side = t["side"].upper()
            status = t["status"]
            pnl = t["pnl"]
            model = t.get("model_p_up")
            mkt = t.get("market_p_up")
            if pnl is not None:
                outcome = "Won" if pnl > 0 else "Lost"
                pnl_str = f"${pnl:+.2f}"
                model_str = f"model {model:.0%}" if model else ""
                mkt_str = f"mkt {mkt:.0%}" if mkt else ""
                lines.append(f"  {ts} {side:4s} {outcome} {pnl_str}  ({model_str} / {mkt_str})")
            else:
                lines.append(f"  {ts} {side:4s} {status}")
        self.update("\n".join(lines))


class StatsBar(Static):
    def update_stats(self, db_path: str) -> None:
        s = get_session_stats(db_path)
        w, l, t = s["wins"], s["losses"], s["total"]
        pnl = s["pnl"]
        wr = f"{w/t:.0%}" if t > 0 else "--"
        self.update(
            f"[bold]STATS[/bold]  {w}W / {l}L / {t} total  |  "
            f"P&L: ${pnl:+,.2f}  |  Win rate: {wr}"
        )


class PolypocketApp(App):
    CSS = """
    #top { height: 12; }
    #status { width: 1fr; }
    #window { width: 1fr; }
    #trades { height: 12; }
    #stats-bar { height: 3; }
    #log { height: 1fr; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("e", "adjust_edge", "Edge"),
        Binding("s", "adjust_size", "Size"),
        Binding("l", "adjust_loss", "Loss Limit"),
        Binding("r", "report", "Report"),
    ]

    def __init__(self):
        super().__init__()
        self.bot = Bot()
        self._start_time = datetime.now()

    def compose(self) -> ComposeResult:
        mode = TRADING_MODE.upper()
        yield Header()
        yield Horizontal(
            StatusPanel(id="status"),
            WindowPanel(id="window"),
            id="top",
        )
        yield TradesPanel(id="trades")
        yield StatsBar(id="stats-bar")
        yield RichLog(id="log", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"Polypocket [{TRADING_MODE.upper()}]"

        # Set up logging to RichLog
        rich_log = self.query_one("#log", RichLog)

        class TUIHandler(logging.Handler):
            def __init__(self, widget):
                super().__init__()
                self.widget = widget

            def emit(self, record):
                try:
                    msg = self.format(record)
                    self.widget.write(msg)
                except Exception:
                    pass

        handler = TUIHandler(rich_log)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
        logging.root.addHandler(handler)
        logging.root.setLevel(logging.INFO)

        # Wire bot callbacks
        self.bot.on_stats_update = lambda stats: self.call_from_thread(self._refresh_panels)

        # Start bot in background thread
        self._bot_thread = threading.Thread(target=self._run_bot, daemon=True)
        self._bot_thread.start()

        # Periodic refresh
        self.set_interval(1.0, self._refresh_panels)

    def _run_bot(self) -> None:
        asyncio.run(self.bot.run())

    def _refresh_panels(self) -> None:
        self.query_one("#status", StatusPanel).update_stats(self.bot.stats, self.bot.db_path)
        self.query_one("#window", WindowPanel).update_stats(self.bot.stats)
        self.query_one("#trades", TradesPanel).update_trades(self.bot.db_path)
        self.query_one("#stats-bar", StatsBar).update_stats(self.bot.db_path)

        # Update uptime in title
        elapsed = datetime.now() - self._start_time
        h, rem = divmod(int(elapsed.total_seconds()), 3600)
        m, s = divmod(rem, 60)
        self.title = f"Polypocket [{TRADING_MODE.upper()}]  Uptime: {h:02d}:{m:02d}:{s:02d}"

    def action_report(self) -> None:
        rich_log = self.query_one("#log", RichLog)
        stats = get_session_stats(self.bot.db_path)
        rich_log.write(f"\n--- SESSION REPORT ---")
        rich_log.write(f"Wins: {stats['wins']}  Losses: {stats['losses']}")
        rich_log.write(f"Total P&L: ${stats['pnl']:+,.2f}")
        rich_log.write(f"Win rate: {stats['wins']/stats['total']:.0%}" if stats['total'] > 0 else "No trades")
        rich_log.write(f"Paper balance: ${get_paper_balance(self.bot.db_path):,.2f}")

    def action_quit(self) -> None:
        self.bot.stop.set()
        self.exit()

    def action_adjust_edge(self) -> None:
        import polypocket.config as cfg
        self.query_one("#log", RichLog).write(
            f"Current min edge: {cfg.MIN_EDGE_THRESHOLD:.1%}. "
            "Type new value (e.g. 0.05 for 5%) and press Enter."
        )
        # TODO: mount Input widget for threshold editing

    def action_adjust_size(self) -> None:
        import polypocket.config as cfg
        self.query_one("#log", RichLog).write(
            f"Current position size: ${cfg.POSITION_SIZE_USDC:.2f}."
        )

    def action_adjust_loss(self) -> None:
        import polypocket.config as cfg
        self.query_one("#log", RichLog).write(
            f"Current max daily loss: ${cfg.MAX_DAILY_LOSS:.2f}."
        )
```

**Step 2: Update __main__.py**

Add `tui` command:

```python
    elif cmd == "tui":
        from polypocket.tui import PolypocketApp
        app = PolypocketApp()
        app.run()
```

**Step 3: Smoke test**

Run: `cd /c/Users/Matt/polypocket && python -m polypocket tui`
Expected: TUI launches with panels. BTC price streams in. Window discovery runs. Ctrl+Q to quit.

**Step 4: Commit**

```bash
cd /c/Users/Matt/polypocket && git add -A && git commit -m "feat: Textual TUI dashboard with live stats, trades, and log panels"
```

---

## Task 13: Backtester

**Files:**
- Create: `polypocket/backtester.py`
- Create: `tests/test_backtester.py`

**Step 1: Write the failing tests**

```python
# tests/test_backtester.py
from polypocket.backtester import (
    simulate_window,
    WindowResult,
    fetch_historical_klines,
)


def test_simulate_window_up():
    """Window where price ends above open should return outcome='up'."""
    # Fake 1-min candles: open, high, low, close for each minute
    candles = [
        {"open": 80000, "high": 80050, "low": 79980, "close": 80020, "ts": 0},
        {"open": 80020, "high": 80100, "low": 80010, "close": 80080, "ts": 60},
        {"open": 80080, "high": 80150, "low": 80060, "close": 80120, "ts": 120},
        {"open": 80120, "high": 80200, "low": 80100, "close": 80180, "ts": 180},
        {"open": 80180, "high": 80250, "low": 80150, "close": 80200, "ts": 240},
    ]
    result = simulate_window(candles, sigma_5min=0.0012, market_p_up=0.50)
    assert result.outcome == "up"
    assert result.open_price == 80000
    assert result.close_price == 80200


def test_simulate_window_no_signal():
    """Flat price action should produce no signal."""
    candles = [
        {"open": 80000, "high": 80001, "low": 79999, "close": 80000, "ts": 0},
        {"open": 80000, "high": 80001, "low": 79999, "close": 80000, "ts": 60},
        {"open": 80000, "high": 80001, "low": 79999, "close": 80000, "ts": 120},
        {"open": 80000, "high": 80001, "low": 79999, "close": 80000, "ts": 180},
        {"open": 80000, "high": 80001, "low": 79999, "close": 80000, "ts": 240},
    ]
    result = simulate_window(candles, sigma_5min=0.0012, market_p_up=0.50)
    assert result.signal_fired is False
```

**Step 2: Run test to verify it fails**

Run: `cd /c/Users/Matt/polypocket && python -m pytest tests/test_backtester.py -v`
Expected: FAIL

**Step 3: Write the implementation**

```python
# polypocket/backtester.py
"""Backtester: replay historical BTC price data through the signal model.

Fetches 1-minute candles from Binance, slices them into 5-minute windows,
and simulates signal generation + P&L for each window.

Usage: python -m polypocket backtest [days]
"""

import asyncio
import logging
import time
from dataclasses import dataclass

import aiohttp

from polypocket.config import (
    MIN_EDGE_THRESHOLD,
    FEE_RATE,
    WINDOW_ENTRY_MIN_ELAPSED,
    WINDOW_ENTRY_MIN_REMAINING,
)
from polypocket.observer import compute_model_p_up
from polypocket.signal import SignalEngine

log = logging.getLogger(__name__)

BINANCE_KLINE_URL = "https://api.binance.com/api/v3/klines"


@dataclass
class WindowResult:
    open_price: float
    close_price: float
    outcome: str               # "up" or "down"
    signal_fired: bool
    signal_side: str | None    # "up" or "down"
    signal_time_s: float | None  # seconds into window when signal fired
    model_p_up: float | None
    edge: float | None
    pnl: float | None          # simulated P&L if traded


def simulate_window(
    candles: list[dict],
    sigma_5min: float,
    market_p_up: float = 0.50,  # assume fair odds for backtest
) -> WindowResult:
    """Simulate one 5-minute window using 1-minute candle data.

    Args:
        candles: List of 5 candles (1 per minute) with open/high/low/close.
        sigma_5min: Realized volatility (std dev of 5-min returns).
        market_p_up: Simulated market implied probability.

    Returns:
        WindowResult with outcome and simulated trade details.
    """
    if len(candles) < 5:
        return WindowResult(
            open_price=0, close_price=0, outcome="up",
            signal_fired=False, signal_side=None, signal_time_s=None,
            model_p_up=None, edge=None, pnl=None,
        )

    open_price = candles[0]["open"]
    close_price = candles[-1]["close"]
    outcome = "up" if close_price >= open_price else "down"

    engine = SignalEngine()
    signal_result = None

    # Simulate checking at the close of each minute candle
    for i, candle in enumerate(candles):
        t_elapsed = (i + 1) * 60.0       # seconds elapsed
        t_remaining = 300.0 - t_elapsed   # seconds remaining
        current_price = candle["close"]
        displacement = (current_price - open_price) / open_price

        signal = engine.evaluate(
            displacement=displacement,
            t_elapsed=t_elapsed,
            t_remaining=t_remaining,
            sigma_5min=sigma_5min,
            market_p_up=market_p_up,
        )

        if signal is not None and signal_result is None:
            # First signal wins (one trade per window)
            entry_price = market_p_up if signal.side == "up" else (1.0 - market_p_up)
            won = (signal.side == outcome)
            payout = 1.0 if won else 0.0
            fees = entry_price * FEE_RATE
            pnl = payout - entry_price - fees

            signal_result = WindowResult(
                open_price=open_price,
                close_price=close_price,
                outcome=outcome,
                signal_fired=True,
                signal_side=signal.side,
                signal_time_s=t_elapsed,
                model_p_up=signal.model_p_up,
                edge=signal.edge,
                pnl=pnl,
            )

    if signal_result:
        return signal_result

    return WindowResult(
        open_price=open_price,
        close_price=close_price,
        outcome=outcome,
        signal_fired=False,
        signal_side=None,
        signal_time_s=None,
        model_p_up=None,
        edge=None,
        pnl=None,
    )


async def fetch_historical_klines(
    symbol: str = "BTCUSDT",
    interval: str = "1m",
    days: int = 7,
) -> list[dict]:
    """Fetch historical 1-minute candles from Binance REST API."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (days * 24 * 60 * 60 * 1000)
    all_candles = []

    async with aiohttp.ClientSession() as session:
        cursor = start_ms
        while cursor < end_ms:
            params = {
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1000,
            }
            async with session.get(BINANCE_KLINE_URL, params=params) as resp:
                data = await resp.json()
                if not data:
                    break
                for k in data:
                    all_candles.append({
                        "ts": k[0],
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                    })
                cursor = data[-1][0] + 60_000  # next minute

    log.info("Fetched %d candles (%d days)", len(all_candles), days)
    return all_candles


def run_backtest(candles: list[dict], sigma_override: float | None = None) -> dict:
    """Run backtest over all 5-minute windows in the candle data.

    Returns summary statistics.
    """
    # Compute rolling volatility from 5-minute returns
    five_min_returns = []
    results = []

    for i in range(0, len(candles) - 4, 5):
        window_candles = candles[i:i + 5]
        if len(window_candles) < 5:
            break

        # Compute 5-min return for vol estimation
        ret = (window_candles[-1]["close"] - window_candles[0]["open"]) / window_candles[0]["open"]
        five_min_returns.append(ret)

        # Use rolling vol or override
        if sigma_override:
            sigma = sigma_override
        elif len(five_min_returns) >= 10:
            recent = five_min_returns[-50:]
            mean = sum(recent) / len(recent)
            var = sum((r - mean) ** 2 for r in recent) / (len(recent) - 1)
            sigma = var ** 0.5
        else:
            sigma = 0.001  # bootstrap

        result = simulate_window(window_candles, sigma_5min=sigma)
        results.append(result)

    # Compute summary
    traded = [r for r in results if r.signal_fired]
    wins = [r for r in traded if r.pnl and r.pnl > 0]
    losses = [r for r in traded if r.pnl and r.pnl <= 0]
    total_pnl = sum(r.pnl for r in traded if r.pnl)

    return {
        "total_windows": len(results),
        "signals_fired": len(traded),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(traded) if traded else 0,
        "total_pnl": total_pnl,
        "avg_pnl_per_trade": total_pnl / len(traded) if traded else 0,
        "profit_factor": (
            sum(r.pnl for r in wins if r.pnl) / abs(sum(r.pnl for r in losses if r.pnl))
            if losses and any(r.pnl for r in losses)
            else float("inf")
        ),
        "max_consecutive_losses": _max_streak(traded, lambda r: r.pnl and r.pnl <= 0),
    }


def _max_streak(items, predicate) -> int:
    streak = 0
    max_s = 0
    for item in items:
        if predicate(item):
            streak += 1
            max_s = max(max_s, streak)
        else:
            streak = 0
    return max_s


async def run_backtest_cli(days: int = 7) -> None:
    """CLI entry point for backtesting."""
    log.info("Fetching %d days of BTC 1-min candles from Binance...", days)
    candles = await fetch_historical_klines(days=days)
    log.info("Running backtest over %d candles...", len(candles))

    summary = run_backtest(candles)

    print("\n=== BACKTEST RESULTS ===")
    print(f"Period: {days} days ({summary['total_windows']} windows)")
    print(f"Signals fired: {summary['signals_fired']}")
    print(f"Wins: {summary['wins']}  Losses: {summary['losses']}")
    print(f"Win rate: {summary['win_rate']:.1%}")
    print(f"Total P&L: ${summary['total_pnl']:+,.2f} (per $1 position)")
    print(f"Avg P&L per trade: ${summary['avg_pnl_per_trade']:+,.4f}")
    print(f"Profit factor: {summary['profit_factor']:.2f}")
    print(f"Max consecutive losses: {summary['max_consecutive_losses']}")
    print()
```

**Step 4: Update __main__.py**

Add `backtest` command:

```python
    elif cmd == "backtest":
        from polypocket.backtester import run_backtest_cli
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
        asyncio.run(run_backtest_cli(days))
```

**Step 5: Run tests**

Run: `cd /c/Users/Matt/polypocket && python -m pytest tests/test_backtester.py -v`
Expected: 2 PASSED

**Step 6: Run a real backtest**

Run: `cd /c/Users/Matt/polypocket && python -m polypocket backtest 7`
Expected: Fetches 7 days of data, prints backtest results. This validates whether the strategy has any edge at all.

**Step 7: Commit**

```bash
cd /c/Users/Matt/polypocket && git add -A && git commit -m "feat: backtester with historical BTC candle replay"
```

---

## Task 14: Final Integration & Push

**Step 1: Run full test suite**

Run: `cd /c/Users/Matt/polypocket && python -m pytest tests/ -v`
Expected: All tests pass

**Step 2: Run the backtest to validate strategy**

Run: `cd /c/Users/Matt/polypocket && python -m polypocket backtest 14`
Expected: Study results. If win rate > 55% and profit factor > 1.2, the strategy has potential.

**Step 3: Push to remote**

```bash
cd /c/Users/Matt/polypocket && git push -u origin main
```

---

## Execution Order Summary

| Task | Module | Gate? |
|------|--------|-------|
| 1 | Config + scaffold | -- |
| 2 | Binance feed (ccxt pro) | -- |
| 3 | Polymarket feed (market discovery + WS) | -- |
| 4 | Observation logger (P(Up) model) | -- |
| 5 | Observer CLI (wire feeds) | -- |
| 6 | Chainlink investigation | **GATE: understand resolution source** |
| 7 | Signal engine | -- |
| 8 | Ledger (SQLite) | -- |
| 9 | Paper executor | -- |
| 10 | Risk manager | -- |
| 11 | Bot orchestrator | -- |
| 12 | TUI dashboard | -- |
| 13 | Backtester | **GATE: validate strategy has edge** |
| 14 | Integration + push | -- |

Tasks 1-5 can be built and tested independently. Task 6 is an investigation gate. Tasks 7-12 build the trading pipeline. Task 13 is a validation gate.
