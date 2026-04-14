# Polypocket: Directional 5-Minute BTC Prediction Bot

**Date:** 2026-04-14
**Status:** Approved

## Overview

Polypocket is a directional prediction bot for Polymarket's 5-minute BTC Up/Down markets. It detects when real-time BTC price movements have not yet been reflected in Polymarket's odds, and bets on the underpriced side.

This is a separate project from `polymarket-arb`. Infrastructure patterns (WebSocket handling, paper trading, TUI) are adapted from that codebase.

**Repository:** https://github.com/Passthewater/polypocket.git

## Strategy: Latency Arbitrage

Polymarket runs recurring 5-minute binary markets: "Will BTC be up or down at the end of this window?" The market resolves using the Chainlink BTC/USD data stream.

**The edge:** There's a lag between when BTC's price actually moves and when Polymarket's odds adjust. If BTC is already up 0.4% from the window's opening price and "Up" shares are still trading at $0.58, the true probability is much higher than 58%.

### Trade Lifecycle

1. New 5-min window opens (e.g., 4:00 PM ET)
2. Record the BTC opening price from Chainlink
3. Stream real-time BTC price from Binance via ccxt pro
4. Continuously compute: current_price vs opening_price -> estimated P(Up)
5. Compare estimated P(Up) to Polymarket's implied probability
6. If edge > fee_threshold -> place order
7. Hold to resolution (4:05 PM ET) -- share redeems at $1.00 or $0.00

### Why Hold to Resolution

These are 5-minute markets. By the time you detect a signal, place an order, and the order fills, there may only be 2-3 minutes left. The spread on selling would eat your profit. Simpler and more profitable to hold and let it resolve.

## Signal Model

### P(Up) Estimation

Model 5-minute BTC returns as normally distributed (Brownian motion, no drift).

Given:
- `d = (P_now - P_open) / P_open` (current displacement)
- `t_remaining` (seconds left in window)
- `sigma` (realized volatility per second)

The probability of finishing above the open price:

```
P(Up) = Phi(d / (sigma * sqrt(t_remaining)))
```

Where `Phi` is the standard normal CDF.

**Intuition:**
- Large positive `d` -> high P(Up) -- BTC is already well above open
- Small `t_remaining` -> extreme P(Up) -- less time for reversal
- High `sigma` -> P(Up) closer to 50% -- more volatility means more uncertainty

**Example:**
- BTC is +0.05% from open, 2 minutes remaining, 5-min volatility is 0.12%
- `d = 0.0005`, `sigma * sqrt(t) = 0.0012 * sqrt(120/300) = 0.00076`
- `P(Up) = Phi(0.0005 / 0.00076) = Phi(0.658) = 74.5%`
- If Polymarket says Up = $0.58 (implied 58%), edge = 16.5%

### Volatility Estimation

Rolling realized volatility from BTC price feed:

```
sigma = std_dev(5-minute returns over last N windows)
```

Lookback of ~50 windows (~4 hours). Updated every window. Adapts to market conditions.

### Trading Decision

```
edge = P_up_model - P_up_market          (if betting Up)
edge = (1 - P_up_model) - P_down_market  (if betting Down)

if edge > MIN_EDGE_THRESHOLD + FEE_RATE:
    bet on the side with positive edge
```

### Entry Timing

```
if t_elapsed < 60s:    skip (no signal yet)
if t_remaining < 30s:  skip (too late to fill)
else:                   evaluate signal continuously
```

One bet per window. No doubling down, no flipping sides.

## Architecture

```
BTC Price Feed (ccxt pro) ---+
                              |
Chainlink Price (REST) ------+--> Signal Engine --> Executor --> Ledger
                              |
Polymarket WS (odds) --------+
                              |
                              +--> Risk Manager
```

### Modules

| Module | Purpose |
|--------|---------|
| `feeds/binance.py` | BTC/USDT real-time price via ccxt pro `watch_trades` |
| `feeds/chainlink.py` | Chainlink BTC/USD price polling (resolution source) |
| `feeds/polymarket.py` | Market discovery + order book WS for 5-min windows |
| `signal.py` | Volatility model, P(Up) estimation, edge detection |
| `executor.py` | Paper + live order execution (single-side, hold to resolve) |
| `risk.py` | Position limits, daily loss limit, consecutive loss tracking |
| `ledger.py` | SQLite trade/position/P&L tracking |
| `backtester.py` | Historical replay engine for strategy validation |
| `bot.py` | Main orchestrator |
| `tui.py` | Textual dashboard |
| `config.py` | All tunable parameters |

### Reuse from polymarket-arb

| Source | What we take | Adaptation |
|--------|-------------|------------|
| `monitor.py` | WS connection management, heartbeat, reconnect | Strip arb scanning, add to polymarket feed |
| `executor.py` | CLOB order placement, FOK construction | Single-side orders only |
| `paper.py` | Paper ledger schema, balance tracking | One side per trade, resolve on window close |
| `risk.py` | Daily loss limit, kill switch pattern | Add consecutive loss tracking |
| `tui.py` | Textual app skeleton, panel layout, keybinds | New panels and metrics |
| `config.py` | Runtime-mutable config pattern | New parameters |

## TUI

```
+----------------------------------------------------------+
| Polypocket                [PAPER]     Uptime: 00:12:34   |
+------------------------+---------------------------------+
| STATUS                 | ACTIVE WINDOW                   |
| BTC Price: $84,231.42  | Window: 4:00-4:05 PM ET        |
| Window Open: $84,198.00| Open Price: $84,198.00          |
| Displacement: +0.04%   | Current Displacement: +0.04%    |
| P(Up) Model: 62.3%     | P(Up) Market: 57.5%             |
| Edge: +4.8%            | Time Left: 3m 22s               |
| Volatility (5m): 0.12% | Position: 10 shares UP @ $0.575 |
+------------------------+---------------------------------+
| RECENT TRADES                                            |
| 3:55-4:00 UP   Won  +$4.25  (model 68% / mkt 54%)      |
| 3:50-3:55 DOWN Lost -$5.75  (model 41% / mkt 45%)      |
| 3:45-3:50 --   Skip (edge 1.2% < threshold)            |
+----------------------------------------------------------+
| STATS                                                    |
| Today: 14W/8L/22skip | P&L: +$12.40 | Win rate: 63%    |
+----------------------------------------------------------+
| LOG                                                      |
| 16:01:38 Signal: UP edge=4.8% -> placing order          |
| 16:01:38 Filled 10 shares UP @ $0.575                   |
+----------------------------------------------------------+
| [Q]uit [E]dge [S]ize [L]oss limit [R]eport              |
+----------------------------------------------------------+
```

## Data Sources

| Source | Data | Protocol | Purpose |
|--------|------|----------|---------|
| Binance via ccxt pro | BTC/USDT real-time price | WebSocket (`watch_trades`) | Fastest public BTC feed |
| Chainlink | BTC/USD data stream | REST poll | Resolution source -- opening price |
| Polymarket Gamma API | Active 5-min market discovery | REST | Find windows, get token IDs |
| Polymarket CLOB WS | Order book for active window | WebSocket | Current Up/Down prices |

**Important:** Market resolves on Chainlink prices, not Binance. Use Binance for speed, Chainlink for the opening price reference. Need to reverse-engineer how Polymarket determines the exact opening price from a few resolved markets.

## Backtester

Test strategy on historical data before live paper trading.

**Data:** Historical BTC prices at high frequency (1-min candles from Binance REST API). Interpolate intra-minute path from OHLC.

**Process:**
1. For each synthetic 5-minute window in historical data
2. Record open price at t=0
3. At each simulated second t=60..270, compute displacement and P(Up)
4. Determine if signal would have triggered (edge > threshold)
5. Record actual outcome (did BTC finish up or down?)

**Key metrics:**
- **Calibration** -- when model says 70% Up, does Up happen ~70%?
- **Win rate** -- % of triggered trades that win
- **Profit factor** -- gross wins / gross losses
- **Max drawdown** -- worst loss streak
- **Trades per day** -- signal frequency

## Configuration Defaults

```python
MIN_EDGE_THRESHOLD = 0.03        # 3% edge required above fees
FEE_RATE = 0.02                  # 2% Polymarket fee per side
POSITION_SIZE_USDC = 10.0        # USDC per trade
MAX_DAILY_LOSS = 50.0            # Kill switch
MAX_CONSECUTIVE_LOSSES = 5       # Pause after N losses in a row
VOLATILITY_LOOKBACK = 50         # Windows for rolling vol
WINDOW_ENTRY_MIN_ELAPSED = 60    # Don't bet before 60s into window
WINDOW_ENTRY_MIN_REMAINING = 30  # Don't bet with <30s remaining
TRADING_MODE = "paper"           # paper or live
```

## Project Structure

```
polypocket/
  polypocket/
    __init__.py
    config.py
    feeds/
      __init__.py
      binance.py
      chainlink.py
      polymarket.py
    signal.py
    executor.py
    risk.py
    ledger.py
    backtester.py
    bot.py
    tui.py
  data/
  docs/plans/
  tests/
  pyproject.toml
  .env.example
  README.md
```

## Dependencies

```
ccxt                # Binance WS via ccxt pro (watch_trades)
py-clob-client      # Polymarket order placement
websockets          # Polymarket CLOB WS
textual             # TUI
scipy               # norm.cdf for P(Up)
numpy               # Volatility computation
python-dotenv       # Config
aiohttp             # REST API calls (Chainlink, Gamma, Binance history)
```
