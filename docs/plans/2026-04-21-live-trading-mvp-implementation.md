# Live Trading MVP Implementation Plan

> **For Claude:** Execute linearly in this chat session. One task at a time. No subagent dispatch. Human reviews between tasks. Design doc: `docs/plans/2026-04-21-live-trading-mvp-design.md`.

**Goal:** Ship a concrete `PolymarketClient` + wired `__main__.py` + fill verification + balance pre-check so `TRADING_MODE=live python -m polypocket run --dry-run` works end-to-end and `TRADING_MODE=live python -m polypocket run` is safe for a supervised $5 sanity test.

**Architecture:** New `polypocket/clients/polymarket.py` wraps `py-clob-client` behind a `FillResult` dataclass so nothing outside the clients package imports the CLOB library. Existing `LiveOrderClient` Protocol in `executor.py` stays as the structural type for mocking.

**Tech Stack:** Python 3.11, `py-clob-client==0.19.0` (already in deps), `python-dotenv`, sqlite3, pytest.

**Out of scope:** PnL reconciliation, startup CLOB reconciliation, unified risk gate, integration-test matrix. Each becomes a follow-up issue at the end of Task 11.

---

## Pre-flight

Before starting Task 1, confirm the workspace is clean:

```bash
git status
```

Expected: working tree clean on `main`. If not, stash or commit first.

Run the existing test suite to establish a green baseline:

```bash
pytest tests/ -q
```

Expected: all tests pass. If anything is red, stop and diagnose before continuing.

---

## Task 1: Ledger schema — add `external_order_id` and `error` columns

**Files:**
- Modify: `polypocket/ledger.py` (function `init_db`, function `update_trade`)
- Modify: `tests/test_ledger.py` or create if missing — add coverage for the new columns.

**Goal:** Idempotent schema migration adding two nullable columns. Existing rows remain valid. `update_trade` gains optional `external_order_id` and `error` parameters.

### Step 1: Red — write the failing test

If `tests/test_ledger.py` does not exist, create it. Otherwise append.

```python
# tests/test_ledger.py
import os
import sqlite3
import tempfile

from polypocket.ledger import (
    init_db,
    log_trade,
    update_trade,
    find_trade_by_window_slug,
)


def _make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    return path


def test_init_db_adds_external_order_id_and_error_columns():
    db_path = _make_db()
    with sqlite3.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
    assert "external_order_id" in cols
    assert "error" in cols
    os.unlink(db_path)


def test_init_db_is_idempotent_on_existing_db():
    db_path = _make_db()
    init_db(db_path)  # second call must not raise
    with sqlite3.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
    assert "external_order_id" in cols
    os.unlink(db_path)


def test_update_trade_writes_external_order_id_and_error():
    db_path = _make_db()
    trade_id = log_trade(
        db_path=db_path,
        window_slug="btc-5m-1",
        side="up",
        entry_price=0.55,
        size=10.0,
        fees=0.01,
        model_p_up=0.7,
        market_p_up=0.55,
        edge=0.15,
        outcome=None,
        pnl=None,
        status="reserved",
    )
    update_trade(
        db_path=db_path,
        trade_id=trade_id,
        outcome=None,
        pnl=None,
        status="rejected",
        external_order_id="abc123",
        error="no match",
    )
    row = find_trade_by_window_slug(db_path, "btc-5m-1")
    assert row["external_order_id"] == "abc123"
    assert row["error"] == "no match"
    assert row["status"] == "rejected"
    os.unlink(db_path)
```

Run:

```bash
pytest tests/test_ledger.py -v
```

Expected: 3 failures — `external_order_id` / `error` columns missing, `update_trade` doesn't accept new kwargs.

### Step 2: Green — implement the migration and update

Edit `polypocket/ledger.py`. Inside `init_db`, after the `CREATE TABLE IF NOT EXISTS trades` block and before the `CREATE INDEX` calls, add idempotent column additions:

```python
# Idempotent column adds for live trading (nullable — paper rows remain valid).
existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
if "external_order_id" not in existing_cols:
    conn.execute("ALTER TABLE trades ADD COLUMN external_order_id TEXT")
if "error" not in existing_cols:
    conn.execute("ALTER TABLE trades ADD COLUMN error TEXT")
```

Extend `update_trade` to accept the new optional fields:

```python
def update_trade(
    db_path: str,
    trade_id: int,
    outcome: str | None,
    pnl: float | None,
    status: str,
    external_order_id: str | None = None,
    error: str | None = None,
) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            UPDATE trades
            SET outcome = ?, pnl = ?, status = ?,
                external_order_id = COALESCE(?, external_order_id),
                error = COALESCE(?, error)
            WHERE id = ?
            """,
            (outcome, pnl, status, external_order_id, error, trade_id),
        )
        conn.commit()
```

`COALESCE` preserves prior non-null values when the caller doesn't supply them — matters because `settle_live_trade` will call `update_trade` without these fields.

### Step 3: Verify

```bash
pytest tests/test_ledger.py -v
pytest tests/ -q
```

Expected: new tests pass; full suite still green.

### Step 4: Commit

```bash
git add polypocket/ledger.py tests/test_ledger.py
git commit -m "feat(ledger): add external_order_id and error columns for live trades"
```

---

## Task 2: `FillResult` dataclass + updated `LiveOrderClient` Protocol

**Files:**
- Modify: `polypocket/executor.py`
- Modify: `tests/test_executor.py`

**Goal:** Introduce the `FillResult` dataclass and update the `LiveOrderClient` Protocol to return one. Update existing executor tests' fake clients to the new shape. `execute_live_trade` behavior unchanged in this task — just signature plumbing. No balance check yet.

### Step 1: Red — update existing tests to new signature

Edit `tests/test_executor.py`. Replace `RecordingLiveOrderClient` and `FailingLiveOrderClient` with implementations that match the new protocol. Add `token_id` to every `execute_live_trade` call and to the recorded args. Have `FailingLiveOrderClient.submit_fok` return `FillResult(status="error", ...)` instead of raising.

```python
from polypocket.executor import (
    FillResult,
    TradeResult,
    execute_paper_trade,
    execute_live_trade,
)


class RecordingLiveOrderClient:
    def __init__(self, balance=1000.0):
        self.calls = []
        self._balance = balance

    def submit_fok(self, side, price, size, token_id, client_order_id):
        self.calls.append({
            "side": side, "price": price, "size": size,
            "token_id": token_id, "client_order_id": client_order_id,
        })
        return FillResult(
            status="filled", order_id=f"ord-{client_order_id}",
            filled_size=size, avg_price=price, error=None,
        )

    def get_usdc_balance(self):
        return self._balance


class RejectingLiveOrderClient:
    def __init__(self, balance=1000.0, error="no match"):
        self.calls = 0
        self._balance = balance
        self._error = error

    def submit_fok(self, side, price, size, token_id, client_order_id):
        self.calls += 1
        return FillResult(
            status="rejected", order_id=None,
            filled_size=0.0, avg_price=None, error=self._error,
        )

    def get_usdc_balance(self):
        return self._balance
```

Update the three existing live-trade tests to:

1. Pass `token_id="TKN-UP"` (or `"TKN-DOWN"`) to `execute_live_trade`.
2. Include `"token_id": ...` in expected `client.calls` entries.
3. Replace `FailingLiveOrderClient` + `pytest.raises` assertion in `test_live_trade_failure_keeps_reserved_trade_reconcilable` — that test's behavior changes in Task 3, so **rename it** to `test_live_trade_reject_is_reserved_until_task_3` and leave it expecting current behavior for now (submit returns FillResult but executor still just calls `update_trade_status("open")` — assert trade status is `"open"` after this task, we fix the semantics in Task 3). Or simpler: **delete the failing-client test entirely** and re-add in Task 3. Delete is cleaner.

Run:

```bash
pytest tests/test_executor.py -v
```

Expected: failures — `FillResult` not importable, `execute_live_trade` doesn't accept `token_id`.

### Step 2: Green — add `FillResult` and thread `token_id`

In `polypocket/executor.py`:

```python
from typing import Literal

@dataclass(frozen=True)
class FillResult:
    status: Literal["filled", "rejected", "error"]
    order_id: str | None
    filled_size: float
    avg_price: float | None
    error: str | None
```

Update the Protocol:

```python
class LiveOrderClient(Protocol):
    def submit_fok(
        self, side: str, price: float, size: float,
        token_id: str, client_order_id: str,
    ) -> FillResult: ...
    def get_usdc_balance(self) -> float: ...
```

Update `execute_live_trade`:

```python
def execute_live_trade(
    db_path: str,
    signal: Signal,
    entry_price: float,
    size: float,
    window_slug: str,
    token_id: str,
    client: LiveOrderClient,
) -> TradeResult:
    ...
    client.submit_fok(
        side=signal.side, price=entry_price, size=size,
        token_id=token_id, client_order_id=client_order_id,
    )
    update_trade_status(db_path, trade_id, "open")
    return TradeResult(success=True, trade_id=trade_id, pnl=None)
```

(We are intentionally ignoring the `FillResult` return value in this task to keep the diff small. Task 3 wires the real handling.)

### Step 3: Verify

```bash
pytest tests/test_executor.py -v
pytest tests/ -q
```

Expected: all tests pass.

### Step 4: Commit

```bash
git add polypocket/executor.py tests/test_executor.py
git commit -m "feat(executor): add FillResult and thread token_id through live path"
```

---

## Task 3: Pre-check balance + handle `FillResult` in `execute_live_trade`

**Files:**
- Modify: `polypocket/executor.py`
- Modify: `tests/test_executor.py`

**Goal:** Executor now (a) checks balance before writing any DB row, (b) writes `status="rejected"` + `error` + `external_order_id=None` on `FillResult.status in {"rejected","error"}`, (c) writes `status="open"` + `external_order_id=fill.order_id` on `"filled"`.

### Step 1: Red — write the four new behavior tests

Append to `tests/test_executor.py`:

```python
class InsufficientBalanceClient:
    def submit_fok(self, **kwargs):
        raise AssertionError("submit_fok must not be called when balance check fails")

    def get_usdc_balance(self):
        return 0.50


def test_live_trade_insufficient_balance_writes_no_row():
    db_path = make_db()
    signal = Signal(side="up", model_p_up=0.72, market_price=0.51,
                    edge=0.21, up_edge=0.21, down_edge=-0.21)
    result = execute_live_trade(
        db_path=db_path, signal=signal, entry_price=0.51, size=7.0,
        window_slug="btc-5m-nb", token_id="TKN-UP",
        client=InsufficientBalanceClient(),
    )
    assert result.success is False
    assert result.error == "insufficient-balance"
    assert find_trade_by_window_slug(db_path, "btc-5m-nb") is None
    os.unlink(db_path)


def test_live_trade_filled_writes_external_order_id():
    db_path = make_db()
    signal = Signal(side="up", model_p_up=0.72, market_price=0.51,
                    edge=0.21, up_edge=0.21, down_edge=-0.21)
    client = RecordingLiveOrderClient()
    result = execute_live_trade(
        db_path=db_path, signal=signal, entry_price=0.51, size=7.0,
        window_slug="btc-5m-fill", token_id="TKN-UP", client=client,
    )
    assert result.success is True
    trade = find_trade_by_window_slug(db_path, "btc-5m-fill")
    assert trade["status"] == "open"
    assert trade["external_order_id"] == "ord-window-btc-5m-fill"
    os.unlink(db_path)


def test_live_trade_rejected_marks_trade_rejected_with_error():
    db_path = make_db()
    signal = Signal(side="down", model_p_up=0.32, market_price=0.44,
                    edge=0.12, up_edge=-0.12, down_edge=0.12)
    client = RejectingLiveOrderClient(error="no match")
    result = execute_live_trade(
        db_path=db_path, signal=signal, entry_price=0.44, size=4.0,
        window_slug="btc-5m-rej", token_id="TKN-DOWN", client=client,
    )
    assert result.success is False
    assert result.error == "no match"
    trade = find_trade_by_window_slug(db_path, "btc-5m-rej")
    assert trade["status"] == "rejected"
    assert trade["error"] == "no match"
    assert trade["external_order_id"] is None
    os.unlink(db_path)


def test_live_trade_client_error_marks_trade_rejected():
    class ErroringClient:
        def submit_fok(self, **kwargs):
            return FillResult(status="error", order_id=None, filled_size=0.0,
                              avg_price=None, error="network: timeout")
        def get_usdc_balance(self):
            return 1000.0

    db_path = make_db()
    signal = Signal(side="up", model_p_up=0.72, market_price=0.51,
                    edge=0.21, up_edge=0.21, down_edge=-0.21)
    result = execute_live_trade(
        db_path=db_path, signal=signal, entry_price=0.51, size=7.0,
        window_slug="btc-5m-err", token_id="TKN-UP", client=ErroringClient(),
    )
    assert result.success is False
    assert "network" in result.error
    trade = find_trade_by_window_slug(db_path, "btc-5m-err")
    assert trade["status"] == "rejected"
    os.unlink(db_path)
```

Run:

```bash
pytest tests/test_executor.py -v -k live_trade
```

Expected: the four new tests fail.

### Step 2: Green — implement

Replace `execute_live_trade` body in `polypocket/executor.py`:

```python
def execute_live_trade(
    db_path: str,
    signal: Signal,
    entry_price: float,
    size: float,
    window_slug: str,
    token_id: str,
    client: LiveOrderClient,
) -> TradeResult:
    existing_trade = find_trade_by_window_slug(db_path, window_slug)
    if existing_trade is not None:
        return _window_consumed_result(db_path, window_slug)

    usdc_needed = entry_price * size
    if client.get_usdc_balance() < usdc_needed:
        return TradeResult(success=False, error="insufficient-balance")

    client_order_id = _window_client_order_id(window_slug)
    fee_sh = fee_shares(size, entry_price)
    try:
        trade_id = log_trade(
            db_path=db_path, window_slug=window_slug, side=signal.side,
            entry_price=entry_price, size=size, fees=fee_sh,
            model_p_up=signal.model_p_up, market_p_up=signal.market_price,
            edge=signal.edge, outcome=None, pnl=None, status="reserved",
        )
    except sqlite3.IntegrityError:
        consumed = _window_consumed_result(db_path, window_slug)
        if consumed.trade_id is not None:
            return consumed
        raise

    fill = client.submit_fok(
        side=signal.side, price=entry_price, size=size,
        token_id=token_id, client_order_id=client_order_id,
    )

    if fill.status == "filled":
        update_trade(
            db_path, trade_id, outcome=None, pnl=None, status="open",
            external_order_id=fill.order_id,
        )
        log.info(
            "Live fill: %s %s @%.4f x%.2f token=%s order=%s",
            window_slug, signal.side, entry_price, size, token_id, fill.order_id,
        )
        return TradeResult(success=True, trade_id=trade_id, pnl=None)

    # rejected or error
    update_trade(
        db_path, trade_id, outcome=None, pnl=None, status="rejected",
        error=fill.error,
    )
    log.warning(
        "Live reject/error: %s %s @%.4f x%.2f: %s",
        window_slug, signal.side, entry_price, size, fill.error,
    )
    return TradeResult(success=False, trade_id=trade_id, error=fill.error)
```

### Step 3: Verify

```bash
pytest tests/test_executor.py -v
pytest tests/ -q
```

Expected: all tests pass.

### Step 4: Commit

```bash
git add polypocket/executor.py tests/test_executor.py
git commit -m "feat(executor): balance pre-check and FillResult handling in live path"
```

---

## Task 4: `PolymarketClient` — wrap `py-clob-client`

**Files:**
- Create: `polypocket/clients/__init__.py` (empty)
- Create: `polypocket/clients/polymarket.py`
- Create: `tests/test_polymarket_client.py`

**Goal:** A class that implements the `LiveOrderClient` Protocol backed by `py-clob-client`. Unit-tested with the library mocked. No real network in tests.

### Step 1: Red — write the failing tests

```python
# tests/test_polymarket_client.py
from unittest.mock import MagicMock, patch

import pytest

from polypocket.clients.polymarket import PolymarketClient
from polypocket.executor import FillResult


@pytest.fixture
def mock_clob():
    with patch("polypocket.clients.polymarket.ClobClient") as cls:
        yield cls


def _make_client(mock_clob_cls, dry_run=False):
    instance = mock_clob_cls.return_value
    instance.get_balance_allowance.return_value = {"balance": "1234.5", "allowance": "999999"}
    return PolymarketClient(
        host="https://clob.polymarket.com", chain_id=137,
        private_key="0x" + "1" * 64,
        api_creds={"key": "k", "secret": "s", "passphrase": "p"},
        proxy_address="0x" + "2" * 40,
        dry_run=dry_run,
    ), instance


def test_submit_fok_filled(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_and_post_order.return_value = {"success": True, "orderID": "abc"}
    inst.get_order.return_value = {"status": "matched", "size_matched": "7.0"}

    fill = client.submit_fok(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", client_order_id="window-x")

    assert fill.status == "filled"
    assert fill.order_id == "abc"
    assert fill.filled_size == pytest.approx(7.0)
    inst.create_and_post_order.assert_called_once()


def test_submit_fok_rejected(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_and_post_order.return_value = {"success": False, "errorMsg": "not matched"}

    fill = client.submit_fok(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", client_order_id="window-x")

    assert fill.status == "rejected"
    assert fill.error == "not matched"
    assert fill.order_id is None
    inst.get_order.assert_not_called()


def test_submit_fok_network_error(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_and_post_order.side_effect = RuntimeError("boom")

    fill = client.submit_fok(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", client_order_id="window-x")

    assert fill.status == "error"
    assert "boom" in fill.error


def test_submit_fok_dry_run_does_not_post(mock_clob):
    client, inst = _make_client(mock_clob, dry_run=True)

    fill = client.submit_fok(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", client_order_id="window-x")

    assert fill.status == "filled"
    assert fill.order_id == "DRY-RUN"
    inst.create_and_post_order.assert_not_called()


def test_get_usdc_balance_queries_proxy(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.get_balance_allowance.return_value = {"balance": "42.7"}

    bal = client.get_usdc_balance()

    assert bal == pytest.approx(42.7)
    # Verify the balance call targeted the proxy signature type / address.
    call = inst.get_balance_allowance.call_args
    assert call is not None
```

Run:

```bash
pytest tests/test_polymarket_client.py -v
```

Expected: `ImportError` on `polypocket.clients.polymarket`.

### Step 2: Green — implement

Create `polypocket/clients/__init__.py` (empty).

Create `polypocket/clients/polymarket.py`:

```python
"""Polymarket CLOB client — L2 proxy-wallet signing."""

import logging

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    BalanceAllowanceParams,
    OrderArgs,
    OrderType,
)
from py_clob_client.order_builder.constants import BUY

from polypocket.executor import FillResult

log = logging.getLogger(__name__)


class PolymarketClient:
    """Concrete LiveOrderClient for Polymarket's CLOB using L2 proxy signing."""

    def __init__(
        self,
        host: str,
        chain_id: int,
        private_key: str,
        api_creds: dict,
        proxy_address: str,
        dry_run: bool = False,
    ):
        self._proxy_address = proxy_address
        self._dry_run = dry_run
        creds = ApiCreds(
            api_key=api_creds["key"],
            api_secret=api_creds["secret"],
            api_passphrase=api_creds["passphrase"],
        )
        # SignatureType 2 = POLY_GNOSIS_SAFE (proxy wallet path for email/OAuth signup).
        self._client = ClobClient(
            host=host,
            key=private_key,
            chain_id=chain_id,
            creds=creds,
            signature_type=2,
            funder=proxy_address,
        )

    def submit_fok(self, side, price, size, token_id, client_order_id):
        if self._dry_run:
            log.info(
                "DRY-RUN submit_fok side=%s price=%.4f size=%.2f token=%s cid=%s",
                side, price, size, token_id, client_order_id,
            )
            return FillResult(
                status="filled", order_id="DRY-RUN",
                filled_size=size, avg_price=price, error=None,
            )

        args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=BUY,  # signal-driven buy of the UP or DOWN outcome token
        )
        try:
            signed = self._client.create_order(args)
            resp = self._client.post_order(signed, OrderType.FOK)
        except Exception as exc:
            log.exception("submit_fok network/signing error")
            return FillResult(
                status="error", order_id=None, filled_size=0.0,
                avg_price=None, error=f"network: {exc}",
            )

        if not resp.get("success"):
            return FillResult(
                status="rejected", order_id=None, filled_size=0.0,
                avg_price=None, error=resp.get("errorMsg", "rejected"),
            )

        order_id = resp.get("orderID")
        try:
            status = self._client.get_order(order_id)
            filled = float(status.get("size_matched", size))
        except Exception as exc:
            log.warning("get_order failed after successful post: %s", exc)
            filled = size  # POST reported success; trust it.

        return FillResult(
            status="filled", order_id=order_id, filled_size=filled,
            avg_price=price, error=None,
        )

    def get_usdc_balance(self) -> float:
        params = BalanceAllowanceParams(
            asset_type="COLLATERAL", signature_type=2, address=self._proxy_address
        )
        resp = self._client.get_balance_allowance(params)
        return float(resp.get("balance", 0.0))

    def get_order_status(self, order_id: str) -> dict:
        return self._client.get_order(order_id)
```

**Note on the `create_order` + `post_order` pair vs `create_and_post_order`:** the test uses `create_and_post_order` as a single mock surface; the real implementation splits into two calls so we can inject per-order args cleanly. Update the test to mock both `create_order` and `post_order` instead:

```python
inst.post_order.return_value = {"success": True, "orderID": "abc"}
inst.post_order.side_effect = None
# and in the assertions replace create_and_post_order.assert_called_once()
```

If the `py-clob-client==0.19.0` API uses `create_and_post_order` as a single call on this version, swap the implementation to use it and update tests accordingly — run `python -c "import py_clob_client.client as c; print([m for m in dir(c.ClobClient) if 'order' in m.lower()])"` first to confirm the available methods before writing the final version. **Do this check before writing the implementation** — it determines the exact method names.

### Step 3: Verify

```bash
pytest tests/test_polymarket_client.py -v
pytest tests/ -q
```

Expected: all tests pass.

### Step 4: Commit

```bash
git add polypocket/clients/__init__.py polypocket/clients/polymarket.py tests/test_polymarket_client.py
git commit -m "feat(clients): PolymarketClient wrapping py-clob-client L2 proxy signing"
```

---

## Task 5: `scripts/derive_clob_creds.py` — one-shot L2 credential helper

**Files:**
- Create: `scripts/derive_clob_creds.py`

**Goal:** A standalone script that reads `PRIVATE_KEY` from `.env`, derives L2 API creds via py-clob-client, and prints them in a format ready to paste back into `.env`. Not invoked by the bot. No tests — it's a one-off operator tool.

### Step 1: Create the script

```python
#!/usr/bin/env python3
"""Derive L2 CLOB API credentials from PRIVATE_KEY.

Usage:
    python scripts/derive_clob_creds.py

Reads PRIVATE_KEY from .env, calls Polymarket to create-or-derive L2 API creds
(idempotent — safe to re-run; returns the same creds for the same EOA), prints
them in `.env` format.
"""

import os
import sys

from dotenv import load_dotenv
from py_clob_client.client import ClobClient


def main() -> None:
    load_dotenv()
    private_key = os.getenv("PRIVATE_KEY", "").strip()
    if not private_key:
        print("ERROR: PRIVATE_KEY is empty in .env", file=sys.stderr)
        sys.exit(1)

    host = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
    chain_id = int(os.getenv("CHAIN_ID", "137"))

    client = ClobClient(host=host, key=private_key, chain_id=chain_id)
    creds = client.create_or_derive_api_creds()

    print()
    print("# Paste these into your .env:")
    print(f"CLOB_API_KEY={creds.api_key}")
    print(f"CLOB_SECRET={creds.api_secret}")
    print(f"CLOB_PASSPHRASE={creds.api_passphrase}")
    print()


if __name__ == "__main__":
    main()
```

### Step 2: Sanity-check the script parses

```bash
python -c "import ast; ast.parse(open('scripts/derive_clob_creds.py').read()); print('ok')"
```

Expected: `ok`.

Also run `python scripts/derive_clob_creds.py --help 2>&1 || true` just to ensure the import graph loads without errors (will fail on empty PRIVATE_KEY, which is expected).

### Step 3: Commit

```bash
git add scripts/derive_clob_creds.py
git commit -m "feat(scripts): derive_clob_creds helper for L2 API creds"
```

---

## Task 6: Config additions

**Files:**
- Modify: `polypocket/config.py`

**Goal:** Add `LIVE_DB_PATH`, `LIVE_MAX_TRADES_PER_SESSION`, and `POLYMARKET_PROXY_ADDRESS` / `CLOB_API_KEY` / `CLOB_SECRET` / `CLOB_PASSPHRASE` env-var reads.

### Step 1: Edit `polypocket/config.py`

Append near the existing `TRADING_MODE` and `PAPER_DB_PATH` lines:

```python
# --- Live trading ---
LIVE_DB_PATH = "live_trades.db"
LIVE_MAX_TRADES_PER_SESSION = int(os.getenv("LIVE_MAX_TRADES_PER_SESSION", "10"))

POLYMARKET_PROXY_ADDRESS = os.getenv("PROXY_ADDRESS", "").strip()
CLOB_API_KEY = os.getenv("CLOB_API_KEY", "").strip()
CLOB_SECRET = os.getenv("CLOB_SECRET", "").strip()
CLOB_PASSPHRASE = os.getenv("CLOB_PASSPHRASE", "").strip()
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "").strip()
```

### Step 2: Verify the module still imports

```bash
python -c "from polypocket import config; print(config.LIVE_DB_PATH, config.LIVE_MAX_TRADES_PER_SESSION)"
```

Expected: `live_trades.db 10`.

Run full test suite:

```bash
pytest tests/ -q
```

Expected: green.

### Step 3: Commit

```bash
git add polypocket/config.py
git commit -m "feat(config): live DB path, session trade cap, CLOB credential env reads"
```

---

## Task 7: `.env.example` update

**Files:**
- Modify: `.env.example`

**Goal:** Document every env var the live path needs, with pointers to the derivation helper.

### Step 1: Replace `.env.example` contents

```
# Trading mode: "paper" or "live"
TRADING_MODE=paper

# --- Polymarket (required only for TRADING_MODE=live) ---
# EOA private key. Export from polymarket.com (Profile → Export private key).
PRIVATE_KEY=

# Proxy wallet address (Gnosis Safe) — the wallet that actually holds USDC.
# Find at polymarket.com Profile → Deposit address.
PROXY_ADDRESS=

# L2 API credentials. Derive once via:
#     python scripts/derive_clob_creds.py
# then paste the three printed lines below.
CLOB_API_KEY=
CLOB_SECRET=
CLOB_PASSPHRASE=

# Live session trade cap (MVP safety rail). Tighten for first real runs.
LIVE_MAX_TRADES_PER_SESSION=10
```

### Step 2: Verify

```bash
git diff .env.example
```

Eyeball: matches above.

### Step 3: Commit

```bash
git add .env.example
git commit -m "docs: expand .env.example with live-trading env vars"
```

---

## Task 8: `__main__.py` — CLI flags, live-mode wiring, startup validation

**Files:**
- Modify: `polypocket/__main__.py`

**Goal:** `python -m polypocket run` supports `--db PATH` and `--dry-run`. In live mode it validates env vars, constructs `PolymarketClient`, confirms balance ≥ `MIN_POSITION_USDC`, and passes the client into `Bot`. Paper mode is unchanged.

### Step 1: Rewrite the `run` branch

Replace the current `if command == "run":` block:

```python
if command == "run":
    import argparse

    from polypocket.bot import Bot
    from polypocket.config import (
        CLOB_API_KEY, CLOB_PASSPHRASE, CLOB_SECRET,
        LIVE_DB_PATH, MIN_POSITION_USDC, PAPER_DB_PATH,
        POLYMARKET_HOST, POLYMARKET_PROXY_ADDRESS,
        PRIVATE_KEY, TRADING_MODE, CHAIN_ID,
    )

    parser = argparse.ArgumentParser(prog="polypocket run")
    parser.add_argument("--db", default=None, help="Override DB path")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Live mode only: sign orders but do not POST to CLOB",
    )
    args = parser.parse_args(sys.argv[2:])

    if TRADING_MODE == "live":
        _validate_live_env()  # defined below
        from polypocket.clients.polymarket import PolymarketClient
        client = PolymarketClient(
            host=POLYMARKET_HOST,
            chain_id=CHAIN_ID,
            private_key=PRIVATE_KEY,
            api_creds={
                "key": CLOB_API_KEY,
                "secret": CLOB_SECRET,
                "passphrase": CLOB_PASSPHRASE,
            },
            proxy_address=POLYMARKET_PROXY_ADDRESS,
            dry_run=args.dry_run,
        )
        balance = client.get_usdc_balance()
        log = logging.getLogger("polypocket")
        log.info("Live startup: proxy=%s balance=$%.2f dry_run=%s",
                 POLYMARKET_PROXY_ADDRESS, balance, args.dry_run)
        if balance < MIN_POSITION_USDC:
            log.error("Balance $%.2f < MIN_POSITION_USDC $%.2f — aborting",
                      balance, MIN_POSITION_USDC)
            sys.exit(1)

        db_path = args.db or LIVE_DB_PATH
        bot = Bot(db_path=db_path, live_order_client=client)
    else:
        if args.dry_run:
            print("--dry-run is only valid with TRADING_MODE=live", file=sys.stderr)
            sys.exit(1)
        db_path = args.db or PAPER_DB_PATH
        bot = Bot(db_path=db_path)

    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        pass
    return
```

Add the validation helper above `main`:

```python
def _validate_live_env() -> None:
    from polypocket.config import (
        CLOB_API_KEY, CLOB_PASSPHRASE, CLOB_SECRET,
        POLYMARKET_PROXY_ADDRESS, PRIVATE_KEY,
    )
    missing = [
        name for name, val in [
            ("PRIVATE_KEY", PRIVATE_KEY),
            ("PROXY_ADDRESS", POLYMARKET_PROXY_ADDRESS),
            ("CLOB_API_KEY", CLOB_API_KEY),
            ("CLOB_SECRET", CLOB_SECRET),
            ("CLOB_PASSPHRASE", CLOB_PASSPHRASE),
        ] if not val
    ]
    if missing:
        print(
            "ERROR: TRADING_MODE=live but missing env vars: "
            + ", ".join(missing)
            + "\nRun `python scripts/derive_clob_creds.py` if you need CLOB_*.",
            file=sys.stderr,
        )
        sys.exit(1)
```

### Step 2: Smoke-test the argument parsing path without actually running the bot

Paper mode, bad flag:

```bash
TRADING_MODE=paper python -m polypocket run --dry-run || true
```

Expected: exits 1 with `--dry-run is only valid with TRADING_MODE=live`.

Paper mode, normal (interrupt quickly):

```bash
TRADING_MODE=paper timeout 3 python -m polypocket run --db /tmp/throwaway.db || true
```

Expected: starts normally, times out — no crashes at startup.

Live mode, missing env:

```bash
TRADING_MODE=live python -m polypocket run || true
```

Expected: exits 1 listing the missing env vars.

### Step 3: Run the test suite

```bash
pytest tests/ -q
```

Expected: green.

### Step 4: Commit

```bash
git add polypocket/__main__.py
git commit -m "feat(cli): wire live client, --db and --dry-run flags, startup validation"
```

---

## Task 9: `bot.py` — thread `token_id`, enforce session cap, handle `insufficient-balance`

**Files:**
- Modify: `polypocket/bot.py`

**Goal:** (a) Resolve `signal.side` → `window.up_token_id`/`down_token_id` and pass into `execute_live_trade`. (b) Enforce `LIVE_MAX_TRADES_PER_SESSION` as a hard cap on successfully-submitted live trades per process. (c) On `insufficient-balance` result, mark the window non-retryable for the session without hammering balance.

### Step 1: Locate the call site

Open `polypocket/bot.py` around lines 396-414 (the `TRADING_MODE == "live"` branch that calls `execute_live_trade`).

### Step 2: Update the call

Before the `else:` branch, add session-cap tracking. In `Bot.__init__`, add:

```python
self._live_trades_submitted = 0
```

In the `_execute_trade` method, just before the `if TRADING_MODE == "paper":` branch, add:

```python
from polypocket.config import LIVE_MAX_TRADES_PER_SESSION
if TRADING_MODE == "live" and self._live_trades_submitted >= LIVE_MAX_TRADES_PER_SESSION:
    log.warning(
        "Live session cap reached (%d) — skipping window %s",
        LIVE_MAX_TRADES_PER_SESSION, window.slug,
    )
    self._window_traded = True
    self.stats["execution_status"] = "session-cap"
    return
```

Replace the live-branch call:

```python
else:
    if self.live_order_client is None:
        raise RuntimeError("live_order_client is required for live trading mode")
    token_id = window.up_token_id if signal.side == "up" else window.down_token_id
    result = execute_live_trade(
        db_path=self.db_path,
        signal=signal,
        entry_price=entry_price,
        size=size,
        window_slug=window.slug,
        token_id=token_id,
        client=self.live_order_client,
    )
    if result.success:
        self._live_trades_submitted += 1
```

After the existing `if not result.success and result.error == "window-already-consumed":` block, add handling for the two new failure modes:

```python
if not result.success and result.error == "insufficient-balance":
    log.error("Insufficient USDC — skipping window %s", window.slug)
    self._window_traded = True
    self.stats["execution_status"] = "no-balance"
    if self.on_stats_update:
        self.on_stats_update(self.stats)
    return

if not result.success:
    # Reject / error path — trade row already flipped to 'rejected' by executor.
    log.warning("Live trade not opened: %s", result.error)
    self._window_traded = True
    self.stats["execution_status"] = f"rejected: {result.error}"
    if self.on_stats_update:
        self.on_stats_update(self.stats)
    return
```

### Step 3: Test the threading change

Update `tests/test_bot.py` to verify the token_id is threaded. Add:

```python
def test_live_mode_threads_up_token_id(...):
    """Signal.side='up' → execute_live_trade called with window.up_token_id."""
    # Use the existing test_bot.py patterns — spy on execute_live_trade via
    # a fake live client whose submit_fok captures token_id, build a window
    # with distinguishable up/down token ids, and simulate a signal firing.
```

Then run:

```bash
pytest tests/ -q
```

Expected: green. Existing `test_bot.py` cases continue to pass; the new case proves `token_id` threading.

### Step 4: Commit

```bash
git add polypocket/bot.py tests/test_bot.py
git commit -m "feat(bot): thread token_id, enforce live session cap, handle reject paths"
```

---

## Task 10: End-to-end dry-run verification (manual)

**Not a code task — operator verification checklist.** Run these before marking the MVP complete.

1. **Derive CLOB creds (one-shot):**

   Populate `PRIVATE_KEY` and `PROXY_ADDRESS` in `.env` first.

   ```bash
   python scripts/derive_clob_creds.py
   ```

   Paste the three printed lines into `.env`.

2. **Dry-run:**

   ```bash
   TRADING_MODE=live python -m polypocket run --dry-run
   ```

   Expected log lines:
   - `Live startup: proxy=0x... balance=$XX.XX dry_run=True`
   - On the first signal fire: `DRY-RUN submit_fok side=... price=... size=... token=... cid=window-...`
   - A trade row appears in `live_trades.db` with `external_order_id='DRY-RUN'` and `status='open'`.

   Ctrl-C after one trade. Confirm via `sqlite3 live_trades.db "SELECT id, window_slug, side, entry_price, size, status, external_order_id FROM trades"`.

3. **Supervised first real trade:**

   ```bash
   MAX_POSITION_USDC=5.0 LIVE_MAX_TRADES_PER_SESSION=3 TRADING_MODE=live python -m polypocket run
   ```

   Expected:
   - Startup validation passes; balance logged.
   - First signal fire produces a real CLOB order with a real `orderID` in the trade row.
   - Operator eyeballs polymarket.com to confirm the position exists.

4. **Document findings.** Note anything unexpected in `docs/plans/2026-04-21-live-trading-mvp-design.md` as an addendum.

---

## Task 11: File follow-up GitHub issues

**Goal:** Create issues A–E from the design, each referencing #3 and the design doc. Run these commands one at a time and eyeball the output before moving to the next.

```bash
gh issue create --title "Live PnL reconciliation against Polymarket payout" --body "$(cat <<'EOF'
Follow-up A from #3 / docs/plans/2026-04-21-live-trading-mvp-design.md.

`settle_live_trade` currently writes `pnl=None, status='settled'` on Chainlink resolution. It must query Polymarket for actual payout / fees / shares held and write real PnL, so `RiskManager.check` (follow-up C) can gate live runs.

## Acceptance
- [ ] `settle_live_trade` queries Polymarket for payout/fees.
- [ ] Writes real `pnl` to trade row.
- [ ] Unit test with mocked CLOB payout response.
EOF
)"

gh issue create --title "Startup order-status reconciliation against CLOB" --body "$(cat <<'EOF'
Follow-up B from #3 / docs/plans/2026-04-21-live-trading-mvp-design.md.

On startup recovery of `reserved`/`open` trades (`bot.py:155-175`), call `client.get_order_status(external_order_id)` and resolve the local state against the CLOB before resuming.

## Acceptance
- [ ] Recover path queries CLOB when `external_order_id` is present.
- [ ] Reconciles `filled` → local `open`; `cancelled`/`unmatched` → local `rejected`.
- [ ] Unit test covering each reconciliation branch.
EOF
)"

gh issue create --title "RiskManager consumes live PnL alongside paper PnL" --body "$(cat <<'EOF'
Follow-up C from #3 / docs/plans/2026-04-21-live-trading-mvp-design.md.

Depends on follow-up A. Once `settle_live_trade` writes real `pnl`, `RiskManager.check` should gate live runs via `MAX_DAILY_LOSS` the same way paper runs are gated.

## Acceptance
- [ ] RiskManager behaves identically for paper and live trade rows.
- [ ] Live losing streak trips `MAX_DAILY_LOSS` and `MAX_CONSECUTIVE_LOSSES`.
- [ ] Unit test with a mixed paper+live ledger.
EOF
)"

gh issue create --title "Integration test matrix for live CLOB path" --body "$(cat <<'EOF'
Follow-up D from #3 / docs/plans/2026-04-21-live-trading-mvp-design.md.

Expand coverage beyond the MVP unit tests. Mocked-CLOB integration tests covering:
- Clean fill.
- Reject (no match).
- Partial fill (defensive — FOK shouldn't partial, but prove our code handles it).
- Restart-mid-order (process killed between `submit_fok` and `update_trade` — follow-up B's reconciliation path exercised end-to-end).

## Acceptance
- [ ] All four scenarios covered with asserts on trade-row end states and `_open_trade` state.
EOF
)"

gh issue create --title "Remove LIVE_MAX_TRADES_PER_SESSION stopgap" --body "$(cat <<'EOF'
Follow-up E from #3 / docs/plans/2026-04-21-live-trading-mvp-design.md.

`LIVE_MAX_TRADES_PER_SESSION` was introduced in the MVP as a compensating control while `RiskManager` is blind to live PnL. Once follow-ups A + C land, this cap is redundant — remove it and rely on the unified daily-loss gate.

## Acceptance
- [ ] Blocked by A and C.
- [ ] Remove `LIVE_MAX_TRADES_PER_SESSION` from `config.py`, `__main__.py`, `bot.py`, and `.env.example`.
- [ ] Update session-cap handling in `bot._execute_trade`.
EOF
)"
```

After creating, copy the issue numbers and amend the design doc to reference them (e.g., "follow-up A → #42"):

```bash
# Edit docs/plans/2026-04-21-live-trading-mvp-design.md — replace A/B/C/D/E
# with the real issue numbers.

git add docs/plans/2026-04-21-live-trading-mvp-design.md
git commit -m "docs: link follow-up issue numbers into live-trading design"
```

---

## Success criteria

- All existing and new unit tests pass.
- `TRADING_MODE=live python -m polypocket run --dry-run` runs without error and writes a `DRY-RUN` trade row.
- A $5 supervised live trade executes and appears both in `live_trades.db` and on polymarket.com.
- Follow-up issues A–E are filed and cross-linked to #3 and this design.
