# Finetuning Data Capture Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a `window_snapshots` table to capture open/decision/close snapshots for every 5-minute window, enabling future model finetuning.

**Architecture:** New table in existing SQLite DB, new `log_snapshot()` function in `ledger.py`, book depth extraction in `polymarket.py`, and snapshot emission at three points in `bot.py`'s window lifecycle. No changes to existing `trades` table or execution flow.

**Tech Stack:** Python, SQLite, JSON serialization for order book depth.

---

### Task 1: Schema — add `window_snapshots` table to `init_db`

**Files:**
- Modify: `polypocket/ledger.py:9-74` (inside `init_db`)
- Test: `tests/test_ledger.py`

**Step 1: Write the failing test**

Add to `tests/test_ledger.py`:

```python
def test_init_creates_window_snapshots_table():
    db_path = make_db()
    conn = sqlite3.connect(db_path)
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='window_snapshots'"
    ).fetchall()
    conn.close()
    assert len(tables) == 1
    os.unlink(db_path)
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_ledger.py::test_init_creates_window_snapshots_table -v`
Expected: FAIL — table doesn't exist yet

**Step 3: Write minimal implementation**

In `polypocket/ledger.py`, inside `init_db()`, after the `idx_trades_window_slug` index creation (line 70) and before `conn.commit()` (line 71), add:

```python
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS window_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    window_slug TEXT NOT NULL,
                    snapshot_type TEXT NOT NULL,
                    btc_price REAL,
                    window_open_price REAL,
                    ptb_provisional INTEGER,
                    displacement REAL,
                    sigma_5min REAL,
                    model_p_up REAL,
                    t_remaining REAL,
                    up_ask REAL,
                    down_ask REAL,
                    market_p_up REAL,
                    edge REAL,
                    preview_side TEXT,
                    quote_status TEXT,
                    up_book_json TEXT,
                    down_book_json TEXT,
                    trade_fired INTEGER,
                    skip_reason TEXT,
                    outcome TEXT,
                    final_price REAL,
                    UNIQUE(window_slug, snapshot_type)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_snapshots_window ON window_snapshots(window_slug)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_snapshots_type ON window_snapshots(snapshot_type)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp ON window_snapshots(timestamp DESC)"
            )
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_ledger.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add polypocket/ledger.py tests/test_ledger.py
git commit -m "feat: add window_snapshots table schema"
```

---

### Task 2: Ledger — add `log_snapshot()` function

**Files:**
- Modify: `polypocket/ledger.py` (add new function after `credit_paper_balance`)
- Test: `tests/test_ledger.py`

**Step 1: Write the failing test**

Add to `tests/test_ledger.py`:

```python
from polypocket.ledger import log_snapshot, get_snapshots_for_window
```

Update the existing import block at the top to include the new functions.

```python
def test_log_snapshot_inserts_and_retrieves():
    db_path = make_db()
    log_snapshot(
        db_path,
        window_slug="btc-updown-5m-100",
        snapshot_type="open",
        stats={
            "btc_price": 84250.0,
            "window_open_price": 84198.0,
            "ptb_provisional": False,
            "displacement": 0.000617,
            "sigma_5min": 0.0012,
            "model_p_up": 0.68,
            "t_remaining": 280.0,
            "up_ask": 0.55,
            "down_ask": 0.45,
            "market_p_up": 0.55,
            "edge": 0.06,
            "preview_side": "up",
            "quote_status": "valid",
        },
    )
    rows = get_snapshots_for_window(db_path, "btc-updown-5m-100")
    assert len(rows) == 1
    assert rows[0]["snapshot_type"] == "open"
    assert rows[0]["btc_price"] == 84250.0
    assert rows[0]["displacement"] == pytest.approx(0.000617)
    os.unlink(db_path)


def test_log_snapshot_upserts_on_duplicate():
    db_path = make_db()
    log_snapshot(
        db_path,
        window_slug="btc-updown-5m-100",
        snapshot_type="open",
        stats={
            "btc_price": 84250.0,
            "window_open_price": 84198.0,
            "ptb_provisional": True,
            "displacement": 0.0006,
            "sigma_5min": 0.001,
            "model_p_up": 0.65,
            "t_remaining": 290.0,
            "up_ask": 0.55,
            "down_ask": 0.45,
            "market_p_up": 0.55,
            "edge": 0.05,
            "preview_side": "up",
            "quote_status": "valid",
        },
    )
    # Update with new data — should replace, not duplicate
    log_snapshot(
        db_path,
        window_slug="btc-updown-5m-100",
        snapshot_type="open",
        stats={
            "btc_price": 84300.0,
            "window_open_price": 84198.0,
            "ptb_provisional": False,
            "displacement": 0.0012,
            "sigma_5min": 0.001,
            "model_p_up": 0.70,
            "t_remaining": 280.0,
            "up_ask": 0.56,
            "down_ask": 0.44,
            "market_p_up": 0.56,
            "edge": 0.07,
            "preview_side": "up",
            "quote_status": "valid",
        },
    )
    rows = get_snapshots_for_window(db_path, "btc-updown-5m-100")
    assert len(rows) == 1
    assert rows[0]["btc_price"] == 84300.0
    os.unlink(db_path)


def test_log_snapshot_with_book_depth_and_decision_fields():
    db_path = make_db()
    log_snapshot(
        db_path,
        window_slug="btc-updown-5m-200",
        snapshot_type="decision",
        stats={
            "btc_price": 84350.0,
            "window_open_price": 84198.0,
            "ptb_provisional": False,
            "displacement": 0.0018,
            "sigma_5min": 0.0015,
            "model_p_up": 0.75,
            "t_remaining": 120.0,
            "up_ask": 0.55,
            "down_ask": 0.45,
            "market_p_up": 0.55,
            "edge": 0.12,
            "preview_side": "up",
            "quote_status": "valid",
        },
        book_depth={
            "up": [{"price": 0.55, "size": 120}, {"price": 0.56, "size": 80}],
            "down": [{"price": 0.45, "size": 100}, {"price": 0.46, "size": 60}],
        },
        trade_fired=True,
    )
    rows = get_snapshots_for_window(db_path, "btc-updown-5m-200")
    assert len(rows) == 1
    assert rows[0]["trade_fired"] == 1
    assert '"price": 0.55' in rows[0]["up_book_json"]
    os.unlink(db_path)


def test_log_snapshot_close_with_outcome():
    db_path = make_db()
    log_snapshot(
        db_path,
        window_slug="btc-updown-5m-300",
        snapshot_type="close",
        stats={
            "btc_price": 84400.0,
            "window_open_price": 84198.0,
            "ptb_provisional": False,
            "displacement": 0.0024,
            "sigma_5min": 0.0015,
            "model_p_up": 0.82,
            "t_remaining": 0.0,
            "up_ask": 0.90,
            "down_ask": 0.10,
            "market_p_up": 0.90,
            "edge": 0.0,
            "preview_side": "up",
            "quote_status": "valid",
        },
        trade_fired=False,
        skip_reason="no-edge",
        outcome="up",
        final_price=84400.0,
    )
    rows = get_snapshots_for_window(db_path, "btc-updown-5m-300")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "up"
    assert rows[0]["final_price"] == 84400.0
    assert rows[0]["skip_reason"] == "no-edge"
    assert rows[0]["trade_fired"] == 0
    os.unlink(db_path)
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ledger.py -k "snapshot" -v`
Expected: FAIL — `log_snapshot` and `get_snapshots_for_window` don't exist

**Step 3: Write minimal implementation**

Add to `polypocket/ledger.py` after `credit_paper_balance`:

```python
def log_snapshot(
    db_path: str,
    window_slug: str,
    snapshot_type: str,
    stats: dict,
    book_depth: dict | None = None,
    trade_fired: bool | None = None,
    skip_reason: str | None = None,
    outcome: str | None = None,
    final_price: float | None = None,
) -> None:
    """Write a window snapshot (open/decision/close) for finetuning data capture."""
    import json

    up_book_json = None
    down_book_json = None
    if book_depth is not None:
        up_book_json = json.dumps(book_depth.get("up"))
        down_book_json = json.dumps(book_depth.get("down"))

    trade_fired_int = None
    if trade_fired is not None:
        trade_fired_int = 1 if trade_fired else 0

    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO window_snapshots (
                window_slug, snapshot_type,
                btc_price, window_open_price, ptb_provisional, displacement,
                sigma_5min, model_p_up, t_remaining,
                up_ask, down_ask, market_p_up, edge, preview_side, quote_status,
                up_book_json, down_book_json,
                trade_fired, skip_reason,
                outcome, final_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                window_slug,
                snapshot_type,
                stats.get("btc_price"),
                stats.get("window_open_price"),
                1 if stats.get("ptb_provisional") else 0,
                stats.get("displacement"),
                stats.get("sigma_5min"),
                stats.get("model_p_up"),
                stats.get("t_remaining"),
                stats.get("up_ask"),
                stats.get("down_ask"),
                stats.get("market_p_up"),
                stats.get("edge"),
                stats.get("preview_side"),
                stats.get("quote_status"),
                up_book_json,
                down_book_json,
                trade_fired_int,
                skip_reason,
                outcome,
                final_price,
            ),
        )
        conn.commit()


def get_snapshots_for_window(db_path: str, window_slug: str) -> list[dict]:
    """Retrieve all snapshots for a given window slug."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM window_snapshots WHERE window_slug = ? ORDER BY id",
            (window_slug,),
        ).fetchall()
        return [dict(row) for row in rows]
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ledger.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add polypocket/ledger.py tests/test_ledger.py
git commit -m "feat: add log_snapshot and get_snapshots_for_window to ledger"
```

---

### Task 3: Polymarket feed — expose top 3 book levels

**Files:**
- Modify: `polypocket/feeds/polymarket.py:106-123` (`parse_book_event`)
- Modify: `polypocket/feeds/polymarket.py:24-38` (`Window` dataclass)
- Modify: `polypocket/feeds/polymarket.py:393-410` (`subscribe_and_stream` loop)
- Test: `tests/test_ledger.py` (or a new focused test if preferred — but keep in existing file for simplicity)

**Step 1: Write the failing test**

Create `tests/test_polymarket_feed.py` (if it doesn't already exist; otherwise add to it):

```python
from polypocket.feeds.polymarket import parse_book_event


def test_parse_book_event_extracts_top_3_levels():
    msg = {
        "asset_id": "tok_up",
        "asks": [
            {"price": "0.60", "size": "50"},
            {"price": "0.55", "size": "120"},
            {"price": "0.57", "size": "80"},
            {"price": "0.65", "size": "30"},
            {"price": "0.56", "size": "90"},
        ],
    }
    result = parse_book_event(msg)
    assert result["best_ask"] == 0.55
    assert result["best_ask_size"] == 120.0
    assert len(result["top_asks"]) == 3
    assert result["top_asks"][0] == {"price": 0.55, "size": 120.0}
    assert result["top_asks"][1] == {"price": 0.56, "size": 90.0}
    assert result["top_asks"][2] == {"price": 0.57, "size": 80.0}


def test_parse_book_event_fewer_than_3_asks():
    msg = {
        "asset_id": "tok_up",
        "asks": [
            {"price": "0.55", "size": "120"},
        ],
    }
    result = parse_book_event(msg)
    assert len(result["top_asks"]) == 1
    assert result["top_asks"][0] == {"price": 0.55, "size": 120.0}


def test_parse_book_event_empty_asks():
    msg = {"asset_id": "tok_up", "asks": []}
    result = parse_book_event(msg)
    assert result["top_asks"] == []
    assert result["best_ask"] is None
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_polymarket_feed.py -v`
Expected: FAIL — `top_asks` key doesn't exist in return dict

**Step 3: Write minimal implementation**

Modify `parse_book_event` in `polypocket/feeds/polymarket.py`:

```python
def parse_book_event(msg: dict) -> dict:
    """Extract best ask price, size, and top 3 levels from a WS book event."""
    asks = msg.get("asks", [])
    best_ask = None
    best_ask_size = None
    top_asks = []
    if asks:
        sorted_asks = sorted(asks, key=lambda a: float(a["price"]))
        best = sorted_asks[0]
        best_ask = float(best["price"])
        best_ask_size = float(best["size"])
        top_asks = [
            {"price": float(a["price"]), "size": float(a["size"])}
            for a in sorted_asks[:3]
        ]
    return {
        "asset_id": msg.get("asset_id"),
        "best_ask": best_ask,
        "best_ask_size": best_ask_size,
        "top_asks": top_asks,
    }
```

Then add `up_book` and `down_book` fields to the `Window` dataclass:

```python
@dataclass
class Window:
    """A single 5-minute BTC up/down market window."""
    condition_id: str
    question: str
    up_token_id: str
    down_token_id: str
    end_time: float
    slug: str
    price_to_beat: float | None
    up_ask: float | None = None
    up_ask_size: float | None = None
    down_ask: float | None = None
    down_ask_size: float | None = None
    up_book: list[dict] | None = None
    down_book: list[dict] | None = None
```

Then update `subscribe_and_stream` to store the book depth on the window. In the loop body where book updates are applied (around line 402-404), add:

```python
                        if side == "up":
                            window.up_ask = parsed["best_ask"]
                            window.up_ask_size = parsed["best_ask_size"]
                            window.up_book = parsed["top_asks"]
                        else:
                            window.down_ask = parsed["best_ask"]
                            window.down_ask_size = parsed["best_ask_size"]
                            window.down_book = parsed["top_asks"]
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_polymarket_feed.py -v`
Expected: ALL PASS

Also run the full test suite to make sure nothing broke:

Run: `pytest -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add polypocket/feeds/polymarket.py tests/test_polymarket_feed.py
git commit -m "feat: expose top 3 order book levels in polymarket feed"
```

---

### Task 4: Bot — emit open snapshot on new window

**Files:**
- Modify: `polypocket/bot.py:24` (add `log_snapshot` import)
- Modify: `polypocket/bot.py:82-142` (`_on_book_update`, new window detection block)
- Test: `tests/test_bot.py`

**Step 1: Write the failing test**

Add to `tests/test_bot.py`:

```python
from polypocket.ledger import get_snapshots_for_window


@pytest.mark.asyncio
async def test_bot_emits_open_snapshot_on_new_window(tmp_path: Path):
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
        slug="btc-updown-5m-snap-open",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
    )

    await bot._on_book_update(window, "up")

    snapshots = get_snapshots_for_window(str(db_path), "btc-updown-5m-snap-open")
    assert len(snapshots) == 1
    assert snapshots[0]["snapshot_type"] == "open"
    assert snapshots[0]["btc_price"] == 84250.0
    assert snapshots[0]["window_open_price"] == 84198.0
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_bot.py::test_bot_emits_open_snapshot_on_new_window -v`
Expected: FAIL — no snapshots written

**Step 3: Write minimal implementation**

In `polypocket/bot.py`:

1. Add to imports (line 24): add `log_snapshot` to the import from `polypocket.ledger`:

```python
from polypocket.ledger import find_trade_by_window_slug, init_db, log_snapshot
```

2. Add a new instance variable to track whether the open snapshot has been emitted. In `__init__` after `self._ptb_provisional` (line 52):

```python
        self._open_snapshot_emitted = False
```

3. In `_on_book_update`, in the new-window block (after `self._ptb_last_fetch = 0.0` around line 141), add:

```python
            self._open_snapshot_emitted = False
```

4. After the `self.stats.update(...)` block and the first `on_stats_update` callback (after line 202), add:

```python
        if not self._open_snapshot_emitted and self.stats["up_ask"] is not None and self.stats["down_ask"] is not None:
            self._open_snapshot_emitted = True
            book_depth = None
            if window.up_book or window.down_book:
                book_depth = {"up": window.up_book, "down": window.down_book}
            log_snapshot(
                self.db_path,
                window_slug=window.slug,
                snapshot_type="open",
                stats=self.stats,
                book_depth=book_depth,
            )
```

Note: The open snapshot fires after the first book update that has both sides. If only one side is available initially, it waits. This ensures we don't log empty data.

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_bot.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add polypocket/bot.py tests/test_bot.py
git commit -m "feat: emit open snapshot on new window detection"
```

---

### Task 5: Bot — track best edge and emit decision snapshot on trade

**Files:**
- Modify: `polypocket/bot.py` (add `_best_edge_snapshot` tracking, emit decision snapshot on trade)
- Test: `tests/test_bot.py`

**Step 1: Write the failing test**

Add to `tests/test_bot.py`:

```python
@pytest.mark.asyncio
async def test_bot_emits_decision_snapshot_on_trade(tmp_path: Path, monkeypatch):
    from polypocket.bot import Bot

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))

    bot = Bot(db_path=str(db_path))
    bot.binance.latest_price = 84350.0
    bot.signal_engine.evaluate = lambda **kwargs: Signal(
        side="up",
        model_p_up=0.75,
        market_price=0.55,
        edge=0.20,
        up_edge=0.20,
        down_edge=-0.20,
    )
    bot.risk.check = lambda: (True, "")

    execute_mock = Mock(return_value=TradeResult(success=True, trade_id=1, pnl=None))
    monkeypatch.setattr("polypocket.bot.execute_paper_trade", execute_mock)

    window = Window(
        condition_id="abc123",
        question="BTC Up or Down",
        up_token_id="tok_up",
        down_token_id="tok_down",
        end_time=time.time() + 180,
        slug="btc-updown-5m-snap-decision",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
    )

    await bot._on_book_update(window, "up")

    snapshots = get_snapshots_for_window(str(db_path), "btc-updown-5m-snap-decision")
    decision = [s for s in snapshots if s["snapshot_type"] == "decision"]
    assert len(decision) == 1
    assert decision[0]["trade_fired"] == 1
    assert decision[0]["skip_reason"] is None
    assert decision[0]["btc_price"] == 84350.0
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_bot.py::test_bot_emits_decision_snapshot_on_trade -v`
Expected: FAIL — no decision snapshot

**Step 3: Write minimal implementation**

In `polypocket/bot.py`:

1. Add `_best_edge_snapshot` to `__init__`:

```python
        self._best_edge_abs: float = 0.0
        self._best_edge_snapshot: dict | None = None
```

2. Reset in the new-window block (same place as `_open_snapshot_emitted`):

```python
            self._best_edge_abs = 0.0
            self._best_edge_snapshot = None
```

3. After the open snapshot emission block, track best edge:

```python
        # Track best edge snapshot for decision logging on no-trade windows
        current_edge_abs = abs(self.stats.get("edge") or 0.0)
        if current_edge_abs > self._best_edge_abs:
            self._best_edge_abs = current_edge_abs
            self._best_edge_snapshot = dict(self.stats)
```

4. Right before calling `execute_paper_trade` or `execute_live_trade` (around line 265), emit the decision snapshot:

```python
        book_depth = None
        if window.up_book or window.down_book:
            book_depth = {"up": window.up_book, "down": window.down_book}
        log_snapshot(
            self.db_path,
            window_slug=window.slug,
            snapshot_type="decision",
            stats=self.stats,
            book_depth=book_depth,
            trade_fired=True,
        )
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_bot.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add polypocket/bot.py tests/test_bot.py
git commit -m "feat: emit decision snapshot on trade execution"
```

---

### Task 6: Bot — emit decision snapshot on skip + close snapshot on resolution

**Files:**
- Modify: `polypocket/bot.py` (flush best-edge snapshot on window close for skipped windows, emit close snapshot on settlement)
- Test: `tests/test_bot.py`

**Step 1: Write the failing tests**

Add to `tests/test_bot.py`:

```python
@pytest.mark.asyncio
async def test_bot_emits_close_snapshot_on_settlement(tmp_path: Path, monkeypatch):
    import polypocket.bot as bot_module
    from polypocket.bot import Bot

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))

    bot = Bot(db_path=str(db_path))
    bot.binance.latest_price = 84350.0
    bot.signal_engine.evaluate = lambda **kwargs: Signal(
        side="up",
        model_p_up=0.75,
        market_price=0.55,
        edge=0.20,
        up_edge=0.20,
        down_edge=-0.20,
    )
    bot.risk.check = lambda: (True, "")

    execute_mock = Mock(return_value=TradeResult(success=True, trade_id=1, pnl=None))
    monkeypatch.setattr("polypocket.bot.execute_paper_trade", execute_mock)
    monkeypatch.setattr("polypocket.bot.settle_paper_trade", lambda *args, **kwargs: 4.5)

    # First update: triggers open + decision + trade
    active_window = Window(
        condition_id="abc123",
        question="BTC Up or Down",
        up_token_id="tok_up",
        down_token_id="tok_down",
        end_time=time.time() + 180,
        slug="btc-updown-5m-snap-close",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
    )
    await bot._on_book_update(active_window, "up")

    # Move to next window to trigger settlement of previous
    async def mock_resolution(slug):
        return "up"

    monkeypatch.setattr(bot_module, "fetch_resolution", mock_resolution)

    next_window = Window(
        condition_id="def456",
        question="BTC Up or Down",
        up_token_id="tok_up2",
        down_token_id="tok_down2",
        end_time=time.time() + 480,
        slug="btc-updown-5m-snap-close-next",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
    )
    await bot._on_book_update(next_window, "up")

    snapshots = get_snapshots_for_window(str(db_path), "btc-updown-5m-snap-close")
    close = [s for s in snapshots if s["snapshot_type"] == "close"]
    assert len(close) == 1
    assert close[0]["outcome"] == "up"
    assert close[0]["trade_fired"] == 1


@pytest.mark.asyncio
async def test_bot_emits_decision_snapshot_on_skip(tmp_path: Path, monkeypatch):
    import polypocket.bot as bot_module
    from polypocket.bot import Bot

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))

    bot = Bot(db_path=str(db_path))
    bot.binance.latest_price = 84250.0
    # No signal — edge never high enough
    bot.signal_engine.evaluate = lambda **kwargs: None

    # Active window with no trade
    active_window = Window(
        condition_id="abc123",
        question="BTC Up or Down",
        up_token_id="tok_up",
        down_token_id="tok_down",
        end_time=time.time() + 180,
        slug="btc-updown-5m-snap-skip",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
    )
    await bot._on_book_update(active_window, "up")

    # Move to next window — should flush decision snapshot for skipped window
    next_window = Window(
        condition_id="def456",
        question="BTC Up or Down",
        up_token_id="tok_up2",
        down_token_id="tok_down2",
        end_time=time.time() + 480,
        slug="btc-updown-5m-snap-skip-next",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
    )
    await bot._on_book_update(next_window, "up")

    snapshots = get_snapshots_for_window(str(db_path), "btc-updown-5m-snap-skip")
    decision = [s for s in snapshots if s["snapshot_type"] == "decision"]
    assert len(decision) == 1
    assert decision[0]["trade_fired"] == 0
    assert decision[0]["skip_reason"] is not None
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_bot.py -k "close_snapshot or skip" -v`
Expected: FAIL — no close or skip decision snapshots

**Step 3: Write minimal implementation**

In `polypocket/bot.py`:

1. Add `_window_skip_reason` to `__init__` and reset in new-window block:

```python
        self._window_skip_reason: str | None = None
```

Reset in new-window block:
```python
            self._window_skip_reason = None
```

2. In `_on_book_update`, where the bot determines *why* it didn't trade, track the skip reason. After `signal = self.signal_engine.evaluate(...)` returns `None` (around line 242):

```python
        if signal is None:
            if not self._window_traded and self._window_skip_reason is None:
                self._window_skip_reason = "no-edge"
            return
```

After `ok, reason = self.risk.check()` returns not ok (around line 247):

```python
        if not ok:
            log.warning("Risk blocked: %s", reason)
            if self._window_skip_reason is None:
                self._window_skip_reason = "risk-blocked"
            return
```

Note: The `SignalEngine.evaluate` already handles timing checks internally and returns `None`, so the timing skip reasons (`timing-early`, `timing-late`) would require inspecting why the signal engine returned None. For simplicity, those are captured as `no-edge`. If more granularity is needed later, the signal engine can be extended to return rejection reasons.

3. In the new-window detection block (where `self._current_window_id != window.condition_id`), *before* resetting state, flush the previous window's snapshots:

```python
            # Flush snapshots for the previous window
            if self._current_window is not None:
                prev_slug = self._current_window.slug
                # Flush decision snapshot for skipped windows
                if not self._window_traded and self._best_edge_snapshot is not None:
                    log_snapshot(
                        self.db_path,
                        window_slug=prev_slug,
                        snapshot_type="decision",
                        stats=self._best_edge_snapshot,
                        trade_fired=False,
                        skip_reason=self._window_skip_reason or "no-edge",
                    )
```

4. In `_settle_trade`, after settlement is complete and before clearing `_open_trade`, emit the close snapshot:

```python
        log_snapshot(
            self.db_path,
            window_slug=self._current_window.slug if self._current_window else "unknown",
            snapshot_type="close",
            stats=self.stats,
            trade_fired=True,
            outcome=outcome,
        )
```

5. In the new-window flush block, also emit a close snapshot for non-traded windows (no resolution data available since the bot didn't wait for it, but we can still mark it):

```python
                # Close snapshot for non-traded windows won't have resolution data
                # since the bot doesn't wait for resolution on skipped windows.
                # Resolution can be backfilled later if needed.
```

Note: For non-traded windows, the close snapshot won't automatically have outcome/final_price since the bot doesn't fetch resolution for windows it didn't trade. This is acceptable — the close snapshot primarily matters for traded windows. For analysis, outcomes can be backfilled from the Gamma API in batch.

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_bot.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add polypocket/bot.py tests/test_bot.py
git commit -m "feat: emit decision snapshot on skip and close snapshot on settlement"
```

---

### Task 7: Integration test — full window lifecycle produces 3 snapshots

**Files:**
- Test: `tests/test_bot.py`

**Step 1: Write the integration test**

```python
@pytest.mark.asyncio
async def test_full_window_lifecycle_produces_three_snapshots(tmp_path: Path, monkeypatch):
    import polypocket.bot as bot_module
    from polypocket.bot import Bot

    db_path = tmp_path / "bot.db"
    init_db(str(db_path))

    bot = Bot(db_path=str(db_path))
    bot.binance.latest_price = 84350.0
    bot.signal_engine.evaluate = lambda **kwargs: Signal(
        side="up",
        model_p_up=0.75,
        market_price=0.55,
        edge=0.20,
        up_edge=0.20,
        down_edge=-0.20,
    )
    bot.risk.check = lambda: (True, "")

    execute_mock = Mock(return_value=TradeResult(success=True, trade_id=1, pnl=None))
    monkeypatch.setattr("polypocket.bot.execute_paper_trade", execute_mock)
    monkeypatch.setattr("polypocket.bot.settle_paper_trade", lambda *args, **kwargs: 4.5)

    async def mock_resolution(slug):
        return "up"

    monkeypatch.setattr(bot_module, "fetch_resolution", mock_resolution)

    # Window 1: active, trade fires
    w1 = Window(
        condition_id="w1",
        question="BTC Up or Down",
        up_token_id="tok_up",
        down_token_id="tok_down",
        end_time=time.time() + 180,
        slug="btc-updown-5m-lifecycle",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
    )
    await bot._on_book_update(w1, "up")

    # Window 2: triggers settlement of window 1
    w2 = Window(
        condition_id="w2",
        question="BTC Up or Down",
        up_token_id="tok_up2",
        down_token_id="tok_down2",
        end_time=time.time() + 480,
        slug="btc-updown-5m-lifecycle-next",
        price_to_beat=84198.0,
        up_ask=0.55,
        down_ask=0.45,
    )
    await bot._on_book_update(w2, "up")

    snapshots = get_snapshots_for_window(str(db_path), "btc-updown-5m-lifecycle")
    types = {s["snapshot_type"] for s in snapshots}
    assert types == {"open", "decision", "close"}
    assert len(snapshots) == 3

    # Verify decision was a trade
    decision = next(s for s in snapshots if s["snapshot_type"] == "decision")
    assert decision["trade_fired"] == 1

    # Verify close has outcome
    close = next(s for s in snapshots if s["snapshot_type"] == "close")
    assert close["outcome"] == "up"
```

**Step 2: Run test**

Run: `pytest tests/test_bot.py::test_full_window_lifecycle_produces_three_snapshots -v`
Expected: PASS (if all prior tasks were implemented correctly)

**Step 3: Run full test suite**

Run: `pytest -v`
Expected: ALL PASS

**Step 4: Commit**

```bash
git add tests/test_bot.py
git commit -m "test: add integration test for full snapshot lifecycle"
```

---

### Task 8: Final verification — run all tests, check no regressions

**Step 1: Run the full test suite**

Run: `pytest -v`
Expected: ALL PASS with no warnings related to snapshot code

**Step 2: Verify schema in a fresh DB**

Run: `python -c "from polypocket.ledger import init_db; init_db('/tmp/test_schema.db'); import sqlite3; c = sqlite3.connect('/tmp/test_schema.db'); print([r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()])"`
Expected: `['trades', 'paper_account', 'window_snapshots']`

**Step 3: Commit any remaining changes**

```bash
git status
# If clean, nothing to commit. If stragglers:
git add -A && git commit -m "chore: final cleanup for finetuning data capture"
```
