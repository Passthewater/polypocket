# Window Execution Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make BTC 5-minute window execution side-correct and idempotent in both paper and live modes, with restart-safe recovery and observable skip reasons.

**Architecture:** Introduce an explicit quote-validation step, refactor signal evaluation to compare fee-adjusted `UP` and `DOWN` asks directly, and move execution safety into durable per-`window_slug` state instead of bot memory alone. Paper and live mode share the same execution policy, while live mode gets a deterministic client-order-id contract and restart reconciliation hook.

**Tech Stack:** Python 3.11, SQLite, pytest, pytest-asyncio, py-clob-client, Textual

---

## File Structure

- Create: `polypocket/quotes.py`
  Purpose: two-sided quote snapshot model plus validation helper for missing, out-of-range, and overround quotes.
- Modify: `polypocket/config.py`
  Purpose: add a quote sanity constant that matches the approved spec.
- Modify: `polypocket/signal.py`
  Purpose: accept both asks, compute fee-adjusted side-specific edges, and return a signal that uses the actual bought-side ask.
- Modify: `polypocket/ledger.py`
  Purpose: add durable lookup helpers for `window_slug`, open-trade recovery, and startup-time duplicate detection plus uniqueness enforcement.
- Modify: `polypocket/executor.py`
  Purpose: make paper execution duplicate-safe and add a live execution contract with deterministic client order IDs.
- Modify: `polypocket/bot.py`
  Purpose: use quote validation, durable recovery, and mode-aware execution instead of the current in-memory-only `self._window_traded` guard.
- Modify: `polypocket/tui.py`
  Purpose: show quote validity, both asks, and recovery/consumed-window state instead of the misleading single `P(Up) Market` label.
- Create: `tests/test_quotes.py`
  Purpose: cover quote validation rules.
- Modify: `tests/test_signal.py`
  Purpose: cover fee-adjusted `UP`/`DOWN` side selection with two-sided asks.
- Modify: `tests/test_ledger.py`
  Purpose: cover `window_slug` uniqueness helpers and contradictory persisted state detection.
- Modify: `tests/test_executor.py`
  Purpose: cover duplicate-safe paper execution and live client-order-id behavior.
- Modify: `tests/test_bot.py`
  Purpose: cover quote-skip behavior, one-trade-per-slug behavior, and restart recovery.

### Task 1: Add Quote Validation

**Files:**
- Create: `polypocket/quotes.py`
- Modify: `polypocket/config.py`
- Test: `tests/test_quotes.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_quotes.py
from polypocket.quotes import QuoteSnapshot, validate_quote


def test_validate_quote_rejects_missing_side():
    result = validate_quote(QuoteSnapshot(up_ask=0.58, down_ask=None))
    assert result.is_valid is False
    assert result.reason == "missing-side"


def test_validate_quote_rejects_price_out_of_range():
    result = validate_quote(QuoteSnapshot(up_ask=1.01, down_ask=0.02))
    assert result.is_valid is False
    assert result.reason == "ask-out-of-range"


def test_validate_quote_rejects_overround():
    result = validate_quote(QuoteSnapshot(up_ask=0.60, down_ask=0.45))
    assert result.is_valid is False
    assert result.reason == "overround"


def test_validate_quote_accepts_sane_two_sided_book():
    result = validate_quote(QuoteSnapshot(up_ask=0.58, down_ask=0.41))
    assert result.is_valid is True
    assert result.reason is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_quotes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'polypocket.quotes'`

- [ ] **Step 3: Write the minimal implementation**

```python
# polypocket/config.py
BOOK_MAX_TOTAL_ASK = 1.02
```

```python
# polypocket/quotes.py
from dataclasses import dataclass

from polypocket.config import BOOK_MAX_TOTAL_ASK


@dataclass(frozen=True)
class QuoteSnapshot:
    up_ask: float | None
    down_ask: float | None


@dataclass(frozen=True)
class QuoteValidation:
    is_valid: bool
    reason: str | None = None


def validate_quote(snapshot: QuoteSnapshot) -> QuoteValidation:
    if snapshot.up_ask is None or snapshot.down_ask is None:
        return QuoteValidation(False, "missing-side")
    if not (0 < snapshot.up_ask <= 1) or not (0 < snapshot.down_ask <= 1):
        return QuoteValidation(False, "ask-out-of-range")
    if snapshot.up_ask + snapshot.down_ask > BOOK_MAX_TOTAL_ASK:
        return QuoteValidation(False, "overround")
    return QuoteValidation(True, None)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_quotes.py -v`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add polypocket/config.py polypocket/quotes.py tests/test_quotes.py
git commit -m "feat: add two-sided quote validation"
```

### Task 2: Refactor Signal Evaluation To Use Actual Side Prices

**Files:**
- Modify: `polypocket/signal.py`
- Modify: `tests/test_signal.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_signal.py
from polypocket.signal import SignalEngine


def test_signal_engine_uses_down_ask_for_down_side():
    engine = SignalEngine()
    signal = engine.evaluate(
        displacement=-0.002,
        t_elapsed=120.0,
        t_remaining=180.0,
        sigma_5min=0.0012,
        up_ask=0.99,
        down_ask=0.15,
    )
    assert signal is not None
    assert signal.side == "down"
    assert signal.market_price == 0.15


def test_signal_engine_skips_expensive_down_even_if_up_is_99c():
    engine = SignalEngine()
    signal = engine.evaluate(
        displacement=-0.002,
        t_elapsed=120.0,
        t_remaining=180.0,
        sigma_5min=0.0012,
        up_ask=0.99,
        down_ask=0.99,
    )
    assert signal is None


def test_signal_engine_prefers_better_fee_adjusted_side():
    engine = SignalEngine()
    signal = engine.evaluate(
        displacement=0.002,
        t_elapsed=120.0,
        t_remaining=180.0,
        sigma_5min=0.0012,
        up_ask=0.55,
        down_ask=0.48,
    )
    assert signal is not None
    assert signal.side == "up"
    assert signal.market_price == 0.55
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run: `pytest tests/test_signal.py -v`
Expected: FAIL with `TypeError` because `SignalEngine.evaluate()` does not accept `up_ask` and `down_ask`

- [ ] **Step 3: Write the minimal implementation**

```python
# polypocket/signal.py
from dataclasses import dataclass

from polypocket.config import FEE_RATE, MIN_EDGE_THRESHOLD, WINDOW_ENTRY_MIN_ELAPSED, WINDOW_ENTRY_MIN_REMAINING
from polypocket.observer import compute_model_p_up


@dataclass
class Signal:
    side: str
    model_p_up: float
    market_price: float
    edge: float
    up_edge: float
    down_edge: float


class SignalEngine:
    def evaluate(
        self,
        displacement: float,
        t_elapsed: float,
        t_remaining: float,
        sigma_5min: float,
        up_ask: float | None,
        down_ask: float | None,
    ) -> Signal | None:
        if t_elapsed < WINDOW_ENTRY_MIN_ELAPSED or t_remaining < WINDOW_ENTRY_MIN_REMAINING:
            return None
        if up_ask is None or down_ask is None or sigma_5min <= 0:
            return None

        model_p_up = compute_model_p_up(displacement, t_remaining, sigma_5min)
        up_edge = model_p_up - (up_ask * (1 + FEE_RATE))
        down_edge = (1 - model_p_up) - (down_ask * (1 + FEE_RATE))

        if up_edge >= MIN_EDGE_THRESHOLD and up_edge >= down_edge:
            return Signal("up", model_p_up, up_ask, up_edge, up_edge, down_edge)
        if down_edge >= MIN_EDGE_THRESHOLD:
            return Signal("down", model_p_up, down_ask, down_edge, up_edge, down_edge)
        return None
```

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run: `pytest tests/test_signal.py -v`
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add polypocket/signal.py tests/test_signal.py
git commit -m "feat: evaluate signals from fee-adjusted side asks"
```

### Task 3: Add Durable `window_slug` Recovery And Duplicate Detection In The Ledger

**Files:**
- Modify: `polypocket/ledger.py`
- Modify: `tests/test_ledger.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ledger.py
import sqlite3

import pytest

from polypocket.ledger import (
    find_trade_by_window_slug,
    get_open_trade_by_window_slug,
    find_duplicate_window_slugs,
    init_db,
    log_trade,
)


def test_find_trade_by_window_slug_returns_existing_trade(tmp_path):
    db_path = tmp_path / "ledger.db"
    init_db(str(db_path))
    trade_id = log_trade(str(db_path), "btc-5m-123", "up", 0.55, 10, 0.11, 0.72, 0.55, 0.14, None, None, "open")
    trade = find_trade_by_window_slug(str(db_path), "btc-5m-123")
    assert trade["id"] == trade_id


def test_get_open_trade_by_window_slug_returns_none_when_settled(tmp_path):
    db_path = tmp_path / "ledger.db"
    init_db(str(db_path))
    log_trade(str(db_path), "btc-5m-123", "up", 0.55, 10, 0.11, 0.72, 0.55, 0.14, "up", 2.0, "settled")
    assert get_open_trade_by_window_slug(str(db_path), "btc-5m-123") is None


def test_find_duplicate_window_slugs_reports_legacy_duplicates(tmp_path):
    db_path = tmp_path / "ledger.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            '''
            CREATE TABLE trades (
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
            '''
        )
        conn.execute("INSERT INTO trades (window_slug, side, entry_price, size, fees, model_p_up, market_p_up, edge, outcome, pnl, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", ("btc-5m-123", "up", 0.55, 10, 0.11, 0.72, 0.55, 0.14, None, None, "open"))
        conn.execute("INSERT INTO trades (window_slug, side, entry_price, size, fees, model_p_up, market_p_up, edge, outcome, pnl, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", ("btc-5m-123", "down", 0.45, 10, 0.11, 0.28, 0.45, 0.13, None, None, "open"))
        conn.commit()
    assert find_duplicate_window_slugs(str(db_path)) == ["btc-5m-123"]


def test_init_db_creates_unique_index_for_window_slug(tmp_path):
    db_path = tmp_path / "ledger.db"
    init_db(str(db_path))

    log_trade(str(db_path), "btc-5m-123", "up", 0.55, 10, 0.11, 0.72, 0.55, 0.14, "up", 2.0, "settled")

    with pytest.raises(sqlite3.IntegrityError):
        log_trade(str(db_path), "btc-5m-123", "down", 0.45, 10, 0.11, 0.28, 0.45, 0.13, None, None, "open")
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run: `pytest tests/test_ledger.py -v`
Expected: FAIL with `ImportError` for the new helper functions

- [ ] **Step 3: Write the minimal implementation**

```python
# polypocket/ledger.py
def find_duplicate_window_slugs(db_path: str) -> list[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT window_slug
            FROM trades
            GROUP BY window_slug
            HAVING COUNT(*) > 1
            ORDER BY window_slug
            """
        ).fetchall()
    return [row[0] for row in rows]


def _fetchone_dict(conn: sqlite3.Connection, query: str, params: tuple) -> dict | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(query, params).fetchone()
    return dict(row) if row is not None else None


def find_trade_by_window_slug(db_path: str, window_slug: str) -> dict | None:
    with closing(sqlite3.connect(db_path)) as conn:
        return _fetchone_dict(
            conn,
            "SELECT * FROM trades WHERE window_slug = ? ORDER BY id DESC LIMIT 1",
            (window_slug,),
        )


def get_open_trade_by_window_slug(db_path: str, window_slug: str) -> dict | None:
    with closing(sqlite3.connect(db_path)) as conn:
        return _fetchone_dict(
            conn,
            "SELECT * FROM trades WHERE window_slug = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
            (window_slug,),
        )


def init_db(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(
            f"""
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
            VALUES (1, {PAPER_STARTING_BALANCE});
            """
        )
        duplicate_slugs = [
            row[0]
            for row in conn.execute(
                """
                SELECT window_slug
                FROM trades
                GROUP BY window_slug
                HAVING COUNT(*) > 1
                """
            ).fetchall()
        ]
        if duplicate_slugs:
            raise RuntimeError(f"duplicate window_slug rows present: {', '.join(duplicate_slugs)}")
        conn.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_window_slug_unique ON trades(window_slug);
            CREATE INDEX IF NOT EXISTS idx_trades_window_slug_status ON trades(window_slug, status);
            """
        )
```

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run: `pytest tests/test_ledger.py -v`
Expected: `9 passed`

- [ ] **Step 5: Commit**

```bash
git add polypocket/ledger.py tests/test_ledger.py
git commit -m "feat: add durable trade lookup helpers by window slug"
```

### Task 4: Make Execution Duplicate-Safe In Paper Mode And Define The Live Idempotency Contract

**Files:**
- Modify: `polypocket/executor.py`
- Modify: `polypocket/ledger.py`
- Modify: `tests/test_executor.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_executor.py
from unittest.mock import Mock

from polypocket.executor import execute_live_trade, execute_paper_trade
from polypocket.ledger import get_open_trade_by_window_slug, init_db
from polypocket.signal import Signal


def test_execute_paper_trade_skips_duplicate_window_slug(tmp_path):
    db_path = tmp_path / "paper.db"
    init_db(str(db_path))
    signal = Signal(side="up", model_p_up=0.75, market_price=0.55, edge=0.12, up_edge=0.12, down_edge=-0.50)

    first = execute_paper_trade(str(db_path), signal, 0.55, 10.0, "btc-5m-123")
    second = execute_paper_trade(str(db_path), signal, 0.55, 10.0, "btc-5m-123")

    assert first.success is True
    assert second.success is False
    assert second.error == "window-already-consumed"
    assert get_open_trade_by_window_slug(str(db_path), "btc-5m-123")["id"] == first.trade_id


def test_execute_live_trade_uses_deterministic_client_order_id(tmp_path):
    db_path = tmp_path / "live.db"
    init_db(str(db_path))
    signal = Signal(side="down", model_p_up=0.22, market_price=0.18, edge=0.11, up_edge=-0.84, down_edge=0.11)
    client = Mock()
    client.submit_fok.return_value = "order-123"

    result = execute_live_trade(
        db_path=str(db_path),
        signal=signal,
        entry_price=0.18,
        size=20.0,
        window_slug="btc-updown-5m-1776217800",
        client=client,
    )

    assert result.success is True
    client.submit_fok.assert_called_once_with(
        side="down",
        price=0.18,
        size=20.0,
        client_order_id="window-btc-updown-5m-1776217800",
    )
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run: `pytest tests/test_executor.py -v`
Expected: FAIL because duplicate execution is not blocked and `execute_live_trade` does not exist

- [ ] **Step 3: Write the minimal implementation**

```python
# polypocket/executor.py
from typing import Protocol

from polypocket.ledger import find_trade_by_window_slug, get_paper_balance, log_trade, deduct_paper_balance, credit_paper_balance, update_trade


class LiveOrderClient(Protocol):
    def submit_fok(self, *, side: str, price: float, size: float, client_order_id: str) -> str:
        raise NotImplementedError


def _window_client_order_id(window_slug: str) -> str:
    return f"window-{window_slug}"


def execute_paper_trade(
    db_path: str,
    signal: Signal,
    entry_price: float,
    size: float,
    window_slug: str,
    outcome: str | None = None,
) -> TradeResult:
    existing = find_trade_by_window_slug(db_path, window_slug)
    if existing is not None:
        return TradeResult(success=False, trade_id=existing["id"], error="window-already-consumed")

    cost = entry_price * size
    fees = cost * FEE_RATE
    balance = get_paper_balance(db_path)
    if balance < cost + fees:
        return TradeResult(success=False, error=f"Insufficient balance: need ${cost + fees:.2f}, have ${balance:.2f}")

    deduct_paper_balance(db_path, cost + fees)
    trade_id = log_trade(
        db_path=db_path,
        window_slug=window_slug,
        side=signal.side,
        entry_price=entry_price,
        size=size,
        fees=fees,
        model_p_up=signal.model_p_up,
        market_p_up=signal.market_price,
        edge=signal.edge,
        outcome=outcome,
        pnl=None,
        status="open" if outcome is None else "settled",
    )
    if outcome is not None:
        payout = size if signal.side == outcome else 0.0
        credit_paper_balance(db_path, payout)
        update_trade(
            db_path,
            trade_id,
            outcome=outcome,
            pnl=payout - cost - fees,
            status="settled",
        )
    return TradeResult(success=True, trade_id=trade_id, pnl=None)


def execute_live_trade(
    db_path: str,
    signal: Signal,
    entry_price: float,
    size: float,
    window_slug: str,
    client: LiveOrderClient,
) -> TradeResult:
    existing = find_trade_by_window_slug(db_path, window_slug)
    if existing is not None:
        return TradeResult(success=False, trade_id=existing["id"], error="window-already-consumed")

    client_order_id = _window_client_order_id(window_slug)
    order_id = client.submit_fok(
        side=signal.side,
        price=entry_price,
        size=size,
        client_order_id=client_order_id,
    )
    trade_id = log_trade(
        db_path=db_path,
        window_slug=window_slug,
        side=signal.side,
        entry_price=entry_price,
        size=size,
        fees=entry_price * size * FEE_RATE,
        model_p_up=signal.model_p_up,
        market_p_up=signal.market_price,
        edge=signal.edge,
        outcome=None,
        pnl=None,
        status="open",
    )
    log.info("Live trade submitted %s order_id=%s client_order_id=%s", window_slug, order_id, client_order_id)
    return TradeResult(success=True, trade_id=trade_id, pnl=None)
```

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run: `pytest tests/test_executor.py -v`
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add polypocket/executor.py polypocket/ledger.py tests/test_executor.py
git commit -m "feat: add idempotent paper and live execution contract"
```

### Task 5: Move The Bot To Quote Validation, Durable Recovery, And Mode-Aware Execution

**Files:**
- Modify: `polypocket/bot.py`
- Modify: `tests/test_bot.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_bot.py
@pytest.mark.asyncio
async def test_bot_skips_trade_when_book_is_one_sided(tmp_path, monkeypatch):
    from polypocket.bot import Bot

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))
    bot = Bot(db_path=str(db_path))
    bot.binance.latest_price = 84350.0
    execute_mock = Mock(return_value=TradeResult(success=True, trade_id=1, pnl=None))
    monkeypatch.setattr("polypocket.bot.execute_paper_trade", execute_mock)

    window = Window(
        condition_id="abc123",
        question="BTC Up or Down",
        up_token_id="tok_up",
        down_token_id="tok_down",
        end_time=time.time() + 180,
        slug="btc-updown-5m-123",
        price_to_beat=84198.0,
        up_ask=0.99,
        down_ask=None,
    )

    await bot._on_book_update(window, "up")

    assert bot.stats["quote_status"] == "missing-side"
    assert execute_mock.call_count == 0


@pytest.mark.asyncio
async def test_bot_recovers_existing_open_trade_for_active_slug(tmp_path):
    from polypocket.bot import Bot
    from polypocket.ledger import log_trade

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))
    trade_id = log_trade(str(db_path), "btc-updown-5m-123", "up", 0.55, 10.0, 0.11, 0.75, 0.55, 0.12, None, None, "open")

    bot = Bot(db_path=str(db_path))
    bot.binance.latest_price = 84350.0
    bot.signal_engine.evaluate = lambda **kwargs: None

    window = Window(
        condition_id="abc123",
        question="BTC Up or Down",
        up_token_id="tok_up",
        down_token_id="tok_down",
        end_time=time.time() + 180,
        slug="btc-updown-5m-123",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
    )

    await bot._on_book_update(window, "up")

    assert bot._open_trade["trade_id"] == trade_id
    assert bot.stats["execution_status"] == "recovery"
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run: `pytest tests/test_bot.py -v`
Expected: FAIL because bot stats do not include quote/execution status and restart recovery is not loaded from the ledger

- [ ] **Step 3: Write the minimal implementation**

```python
# polypocket/bot.py
from polypocket.quotes import QuoteSnapshot, validate_quote
from polypocket.ledger import get_open_trade_by_window_slug, init_db
```

```python
# polypocket/bot.py
self.stats.update(
    {
        "up_ask": window.up_ask,
        "down_ask": window.down_ask,
        "quote_status": None,
        "execution_status": None,
    }
)

quote_check = validate_quote(QuoteSnapshot(window.up_ask, window.down_ask))
if not quote_check.is_valid:
    self.stats["quote_status"] = quote_check.reason
    if self.on_stats_update:
        self.on_stats_update(self.stats)
    return

persisted_trade = get_open_trade_by_window_slug(self.db_path, window.slug)
if persisted_trade is not None and self._open_trade is None:
    self._open_trade = {
        "trade_id": persisted_trade["id"],
        "side": persisted_trade["side"],
        "entry_price": persisted_trade["entry_price"],
        "size": persisted_trade["size"],
    }
    self.stats["execution_status"] = "recovery"

signal = self.signal_engine.evaluate(
    displacement=displacement,
    t_elapsed=t_elapsed,
    t_remaining=t_remaining,
    sigma_5min=sigma,
    up_ask=window.up_ask,
    down_ask=window.down_ask,
)
```

```python
# polypocket/bot.py
if TRADING_MODE == "paper":
    result = execute_paper_trade(
        db_path=self.db_path,
        signal=signal,
        entry_price=entry_price,
        size=size,
        window_slug=window.slug,
    )
else:
    result = execute_live_trade(
        db_path=self.db_path,
        signal=signal,
        entry_price=entry_price,
        size=size,
        window_slug=window.slug,
        client=self.live_client,
    )

if result.success:
    self.stats["execution_status"] = "open"
elif result.error == "window-already-consumed":
    self.stats["execution_status"] = "consumed"
```

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run: `pytest tests/test_bot.py -v`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add polypocket/bot.py tests/test_bot.py
git commit -m "feat: recover trades by window slug and skip invalid books"
```

### Task 6: Update TUI Observability And Run End-To-End Verification

**Files:**
- Modify: `polypocket/tui.py`
- Modify: `tests/test_bot.py`
- Modify: `tests/test_executor.py`

- [ ] **Step 1: Write the failing assertion for new operator-facing state**

```python
# tests/test_bot.py
@pytest.mark.asyncio
async def test_bot_stats_expose_both_asks_and_status_fields(tmp_path):
    from polypocket.bot import Bot

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))
    bot = Bot(db_path=str(db_path))
    bot.binance.latest_price = 84250.0
    bot.signal_engine.evaluate = lambda **kwargs: None

    window = Window(
        condition_id="abc123",
        question="BTC Up or Down",
        up_token_id="tok_up",
        down_token_id="tok_down",
        end_time=time.time() + 180,
        slug="btc-updown-5m-123",
        price_to_beat=84198.0,
        up_ask=0.57,
        down_ask=0.43,
    )

    await bot._on_book_update(window, "up")

    assert bot.stats["up_ask"] == 0.57
    assert bot.stats["down_ask"] == 0.43
    assert "quote_status" in bot.stats
    assert "execution_status" in bot.stats
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run: `pytest tests/test_bot.py::test_bot_stats_expose_both_asks_and_status_fields -v`
Expected: FAIL with `KeyError` for `up_ask` or missing status fields

- [ ] **Step 3: Write the minimal implementation**

```python
# polypocket/tui.py
lines.append(f"Up Ask: {stats['up_ask']:.1%}" if stats.get("up_ask") is not None else "Up Ask: --")
lines.append(f"Down Ask: {stats['down_ask']:.1%}" if stats.get("down_ask") is not None else "Down Ask: --")
lines.append(f"Quote Status: {stats['quote_status']}" if stats.get("quote_status") else "Quote Status: ok")
lines.append(
    f"Execution: {stats['execution_status']}"
    if stats.get("execution_status")
    else "Execution: eligible"
)
```

```python
# polypocket/tui.py
if pnl is not None:
    market_str = f"ask {trade.get('market_p_up', 0):.0%}" if trade.get("market_p_up") is not None else ""
    lines.append(f"  {timestamp} {side:4s} {outcome} {pnl_str}  ({market_str})")
```

- [ ] **Step 4: Run the full suite**

Run: `pytest -v`
Expected: all tests pass, including the new quote, signal, ledger, executor, and bot cases

- [ ] **Step 5: Commit**

```bash
git add polypocket/tui.py tests/test_bot.py tests/test_executor.py
git commit -m "feat: expose quote and execution safety state in tui"
```

## Self-Review Checklist

- Spec coverage:
  - two-sided quote validation is implemented in Task 1
  - fee-adjusted side-specific signal selection is implemented in Task 2
  - durable `window_slug` recovery and contradiction checks are implemented in Task 3
  - paper/live idempotent execution policy is implemented in Task 4
  - bot recovery and no-reentry behavior are implemented in Task 5
  - observability requirements are implemented in Task 6
- Placeholder scan:
  - no `TODO`, `TBD`, or "handle appropriately" steps remain
  - each task includes concrete file paths, code, test commands, and commit commands
- Type consistency:
  - `Signal.market_price` is used consistently after Task 2
  - bot stats keys `up_ask`, `down_ask`, `quote_status`, and `execution_status` are introduced in Task 5 and consumed in Task 6
