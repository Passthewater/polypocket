# GTC + Cancel IOC Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace `submit_fok` with a `submit_ioc` that posts `OrderType.GTC` at the existing `fok_limit_price`, immediately cancels any unmatched remainder, and returns a `FillResult` derived from per-fill `/trades` data. Update `bot.py` to use it and tighten the pre-trade floor gate so worst-case partial fills stay above `MIN_POSITION_USDC`.

**Architecture:** Layer B on top of the shipped depth clamp (A). Keep `submit_fok` in place temporarily for the probe script. Partial fills become real positions; pre-trade gate uses `fillable × MIN_FILL_RATIO × price` as the floor so any slice above `MIN_FILL_RATIO` of visible depth is guaranteed to clear `MIN_POSITION_USDC`. One diagnostic INFO log line per trade captures snapshot vs. realized fill for future root-cause analysis.

**Tech Stack:** Python 3, `py_clob_client`, pytest with `unittest.mock`.

**Design doc:** [`docs/plans/2026-04-22-gtc-cancel-ioc-design.md`](./2026-04-22-gtc-cancel-ioc-design.md)

---

## Task 0: Probe cancel semantics before coding

**Goal:** Confirm `py_clob_client.cancel(order_id)` applies to the *remaining-open* quantity, not the original. If that assumption fails, the implementation shape changes (we'd read `/order` before cancel).

**Files:**
- Create: `scripts/probe_gtc_cancel.py`

**Step 1: Write the probe script**

```python
"""One-shot probe: confirm cancel(order_id) applies to remaining, not original.

Run against live CLOB. Posts a deliberately small GTC at a favorable price
on a known quiet market, waits briefly, cancels, reads /order, prints
size / size_matched / status. If size_matched > 0 and cancel did not
wipe it, the assumption holds.

Usage:
    python scripts/probe_gtc_cancel.py --token <TOKEN_ID> --condition <COND_ID> \\
        --price 0.50 --size 2.0

Requires POLYMARKET_* env vars (same as main bot).
"""

import argparse
import logging
import os
import time

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds, MarketOrderArgs, OrderType,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("probe")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--token", required=True, help="outcome token id")
    p.add_argument("--condition", required=True, help="condition id")
    p.add_argument("--price", type=float, required=True, help="limit price")
    p.add_argument("--size", type=float, required=True, help="share size")
    p.add_argument("--wait-ms", type=int, default=200)
    args = p.parse_args()

    client = ClobClient(
        host=os.environ["POLYMARKET_HOST"],
        key=os.environ["POLYMARKET_PRIVATE_KEY"],
        chain_id=int(os.environ.get("POLYMARKET_CHAIN_ID", "137")),
        creds=ApiCreds(
            api_key=os.environ["POLYMARKET_API_KEY"],
            api_secret=os.environ["POLYMARKET_API_SECRET"],
            api_passphrase=os.environ["POLYMARKET_API_PASSPHRASE"],
        ),
        signature_type=1,
        funder=os.environ["POLYMARKET_PROXY_ADDRESS"],
    )

    market = client.get_market(args.condition)
    fee_rate_bps = int(market.get("taker_base_fee", 0) or 0)

    log.info("posting GTC size=%.2f @ $%.2f token=%s", args.size, args.price, args.token)
    order_args = MarketOrderArgs(
        token_id=args.token,
        amount=round(args.size * args.price, 2),
        price=args.price,
        fee_rate_bps=fee_rate_bps,
    )
    signed = client.create_market_order(order_args)
    resp = client.post_order(signed, OrderType.GTC)
    log.info("post_order resp: %s", resp)

    order_id = resp.get("orderID")
    if not order_id:
        log.error("no order id in response; aborting")
        return

    log.info("sleeping %d ms before cancel", args.wait_ms)
    time.sleep(args.wait_ms / 1000.0)

    before = client.get_order(order_id)
    log.info("pre-cancel /order: size=%s size_matched=%s status=%s",
             before.get("size"), before.get("size_matched"), before.get("status"))

    cancel_resp = client.cancel(order_id)
    log.info("cancel resp: %s", cancel_resp)

    after = client.get_order(order_id)
    log.info("post-cancel /order: size=%s size_matched=%s status=%s",
             after.get("size"), after.get("size_matched"), after.get("status"))


if __name__ == "__main__":
    main()
```

**Step 2: Run the probe**

Choose a live, low-volume BTC up/down market with a visible ask where you can place a small order without moving the book. Pick `--price` equal to the best ask (so it matches immediately). Intended outcome: order matches fully, cancel returns an error like "order not found / already filled" — that's fine, it means cancel is a no-op on a fully-filled order.

For the remainder-preservation test: pick `--price` 1 tick *below* the best ask (so the order rests). Expect the order to partially or fully rest; cancel should succeed; `size_matched` stays unchanged after cancel.

Run:
```
python scripts/probe_gtc_cancel.py --token TOKEN --condition COND --price P --size S 2>&1 | tee probe-out.txt
```

**Step 3: Document in the design doc**

Append a brief "Probe results" section to `docs/plans/2026-04-22-gtc-cancel-ioc-design.md` summarizing: did cancel apply to remainder only? Any surprising fields in `/order` response? If anything deviates from the design assumption, STOP and surface the finding before continuing.

**Step 4: Commit**

```bash
git add scripts/probe_gtc_cancel.py docs/plans/2026-04-22-gtc-cancel-ioc-design.md
git commit -m "feat(scripts): probe GTC+cancel semantics (issue #9)"
```

---

## Task 1: Extend `LiveOrderClient` protocol with `cancel` and `submit_ioc`

**Files:**
- Modify: `polypocket/executor.py:50-58` (the `LiveOrderClient` Protocol)

**Step 1: Write failing tests for the protocol-shape change**

Skip — protocols don't have behavior to test. Go straight to implementation; the typecheck happens implicitly through `test_polymarket_client.py`.

**Step 2: Edit `LiveOrderClient`**

Add these methods to the Protocol. Do not remove `submit_fok` yet (Task 0's probe script doesn't use it but we keep it for one commit so the deprecate-and-delete is a clean later commit).

```python
class LiveOrderClient(Protocol):
    def submit_fok(
        self, side: str, price: float, size: float,
        token_id: str, condition_id: str,
    ) -> FillResult: ...
    def submit_ioc(
        self, side: str, price: float, size: float,
        token_id: str, condition_id: str,
    ) -> FillResult: ...
    def cancel_order(self, order_id: str) -> bool: ...
    def get_usdc_balance(self) -> float: ...
    def get_settlement_info(self, order_id: str) -> SettlementInfo: ...
    def get_order_status(self, order_id: str) -> dict: ...
```

Note: `cancel_order` (not `cancel`) to avoid stepping on the reserved word in some linters and to parallel `get_order_status`.

**Step 3: Commit**

```bash
git add polypocket/executor.py
git commit -m "feat(live): extend LiveOrderClient protocol with submit_ioc + cancel_order"
```

---

## Task 2: Add `cancel_order` to `PolymarketClient`

**Files:**
- Modify: `polypocket/clients/polymarket.py` (add method after `submit_fok`)
- Test: `tests/test_polymarket_client.py`

**Step 1: Write the failing tests**

Append to `tests/test_polymarket_client.py`:

```python
def test_cancel_order_success(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.cancel.return_value = {"canceled": ["abc"]}
    ok = client.cancel_order("abc")
    assert ok is True
    inst.cancel.assert_called_once_with(order_id="abc")


def test_cancel_order_dry_run(mock_clob):
    client, _ = _make_client(mock_clob, dry_run=True)
    assert client.cancel_order("DRY-RUN") is True
    assert client.cancel_order("anything") is True


def test_cancel_order_retries_then_succeeds(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.cancel.side_effect = [Exception("transient"), {"canceled": ["abc"]}]
    ok = client.cancel_order("abc")
    assert ok is True
    assert inst.cancel.call_count == 2


def test_cancel_order_gives_up_after_retries(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.cancel.side_effect = Exception("persistent")
    ok = client.cancel_order("abc")
    assert ok is False
    assert inst.cancel.call_count == 3  # 1 + 2 retries (CANCEL_RETRY_MAX=2)
```

**Step 2: Run failing**

```
pytest tests/test_polymarket_client.py -k cancel_order -v
```
Expected: FAIL with `AttributeError: 'PolymarketClient' object has no attribute 'cancel_order'`.

**Step 3: Implement `cancel_order`**

In `polypocket/clients/polymarket.py`, add near the top:

```python
import time

CANCEL_RETRY_MAX = 2
CANCEL_RETRY_BACKOFF_S = 0.25
```

Add the method inside `PolymarketClient`, below `submit_fok`:

```python
def cancel_order(self, order_id: str) -> bool:
    """Cancel a resting order. Retries on transient errors.

    Returns True on success, False if all retries fail. Errors are logged
    but not raised — the caller records whatever matched via /trades and
    the startup reconciler catches orphans.
    """
    if self._dry_run:
        return True

    last_exc: Exception | None = None
    for attempt in range(CANCEL_RETRY_MAX + 1):
        try:
            self._client.cancel(order_id=order_id)
            return True
        except Exception as exc:
            last_exc = exc
            if attempt < CANCEL_RETRY_MAX:
                time.sleep(CANCEL_RETRY_BACKOFF_S * (attempt + 1))
    log.error("cancel_order failed after %d attempts for order %s: %s",
              CANCEL_RETRY_MAX + 1, order_id, last_exc)
    return False
```

**Step 4: Run tests**

```
pytest tests/test_polymarket_client.py -k cancel_order -v
```
Expected: 4 PASSED.

**Step 5: Commit**

```bash
git add polypocket/clients/polymarket.py tests/test_polymarket_client.py
git commit -m "feat(live): cancel_order with retry on PolymarketClient"
```

---

## Task 3: Implement `submit_ioc` on `PolymarketClient`

**Files:**
- Modify: `polypocket/clients/polymarket.py`
- Test: `tests/test_polymarket_client.py`

**Step 1: Write the failing tests**

Append to `tests/test_polymarket_client.py`:

```python
def test_submit_ioc_full_match(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_market_order.return_value = MagicMock()
    inst.post_order.return_value = {
        "success": True, "status": "matched", "orderID": "abc",
    }
    # get_order returns fully-matched (size_matched == size)
    inst.get_order.return_value = {
        "size_matched": "7.0",
        "associate_trades": ["t1"],
    }
    inst.get_trades.return_value = [
        {"taker_order_id": "abc", "size": "7.0", "price": "0.51", "fee_rate_bps": 1000},
    ]

    fill = client.submit_ioc(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND")

    assert fill.status == "filled"
    assert fill.order_id == "abc"
    # shares_held = 7.0 * (1 - 0.10) = 6.3
    assert fill.filled_size == pytest.approx(6.3, abs=0.001)
    inst.cancel.assert_not_called()


def test_submit_ioc_partial_match(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_market_order.return_value = MagicMock()
    # Server says "matched" but get_order shows a smaller size_matched than
    # we asked for — realistic response when only part of the book crossed.
    inst.post_order.return_value = {
        "success": True, "status": "matched", "orderID": "abc",
    }
    inst.get_order.return_value = {
        "size_matched": "3.0",
        "associate_trades": ["t1"],
    }
    inst.get_trades.return_value = [
        {"taker_order_id": "abc", "size": "3.0", "price": "0.51", "fee_rate_bps": 1000},
    ]
    inst.cancel.return_value = {"canceled": ["abc"]}

    fill = client.submit_ioc(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND")

    assert fill.status == "filled"
    assert fill.filled_size == pytest.approx(2.7, abs=0.001)  # 3.0 * 0.9
    inst.cancel.assert_called_once()


def test_submit_ioc_no_match_returns_rejected(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_market_order.return_value = MagicMock()
    inst.post_order.return_value = {
        "success": True, "status": "unmatched", "orderID": "abc",
    }
    inst.get_order.return_value = {"size_matched": "0", "associate_trades": []}
    inst.cancel.return_value = {"canceled": ["abc"]}

    fill = client.submit_ioc(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND")

    assert fill.status == "rejected"
    assert fill.error == "gtc-no-fill"
    assert fill.filled_size == 0.0
    inst.cancel.assert_called_once()


def test_submit_ioc_post_raises_returns_error(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_market_order.side_effect = Exception("network down")

    fill = client.submit_ioc(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND")

    assert fill.status == "error"
    assert "network" in fill.error
    inst.cancel.assert_not_called()


def test_submit_ioc_success_false_is_rejected(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_market_order.return_value = MagicMock()
    inst.post_order.return_value = {
        "success": False, "errorMsg": "fee mismatch",
    }

    fill = client.submit_ioc(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND")

    assert fill.status == "rejected"
    assert "fee mismatch" in fill.error
    inst.cancel.assert_not_called()


def test_submit_ioc_cancel_fails_still_returns_fill(mock_clob):
    client, inst = _make_client(mock_clob)
    inst.create_market_order.return_value = MagicMock()
    inst.post_order.return_value = {
        "success": True, "status": "matched", "orderID": "abc",
    }
    inst.get_order.return_value = {
        "size_matched": "3.0", "associate_trades": ["t1"],
    }
    inst.get_trades.return_value = [
        {"taker_order_id": "abc", "size": "3.0", "price": "0.51", "fee_rate_bps": 1000},
    ]
    inst.cancel.side_effect = Exception("persistent")

    fill = client.submit_ioc(side="up", price=0.51, size=7.0,
                             token_id="TKN-UP", condition_id="0xCOND")

    # Cancel failure is logged but doesn't flip success — we have a real fill.
    assert fill.status == "filled"
    assert fill.filled_size == pytest.approx(2.7, abs=0.001)


def test_submit_ioc_dry_run(mock_clob):
    client, _ = _make_client(mock_clob, dry_run=True)
    fill = client.submit_ioc(side="up", price=0.51, size=7.0,
                             token_id="TKN", condition_id="COND")
    assert fill.status == "filled"
    assert fill.filled_size == 7.0
```

**Step 2: Run failing**

```
pytest tests/test_polymarket_client.py -k submit_ioc -v
```
Expected: 7 FAIL with `AttributeError: 'PolymarketClient' object has no attribute 'submit_ioc'`.

**Step 3: Implement `submit_ioc`**

In `polypocket/clients/polymarket.py`, add below `submit_fok`:

```python
def submit_ioc(self, side, price, size, token_id, condition_id):
    """Post GTC at FOK-limit price, immediately cancel remainder.

    True-IOC semantic layered on GTC since py_clob_client doesn't expose
    IOC natively. Any match at <= fok_limit_price fills (within slippage
    budget by construction); remainder is cancelled. Returned filled_size
    is shares_held from per-fill /trades data (post-fee).
    """
    if self._dry_run:
        log.info(
            "DRY-RUN submit_ioc side=%s price=%.4f size=%.2f token=%s cond=%s",
            side, price, size, token_id, condition_id,
        )
        return FillResult(
            status="filled", order_id="DRY-RUN",
            filled_size=size, avg_price=price, error=None,
        )

    fee_rate_bps = self._fee_rate_bps(condition_id)
    limit_price = fok_limit_price(price)
    args = MarketOrderArgs(
        token_id=token_id,
        amount=round(size * price, 2),
        price=limit_price,
        fee_rate_bps=fee_rate_bps,
    )

    try:
        signed = self._client.create_market_order(args)
        resp = self._client.post_order(signed, OrderType.GTC)
    except Exception as exc:
        log.exception("submit_ioc network/signing error")
        return FillResult(
            status="error", order_id=None, filled_size=0.0,
            avg_price=None, error=f"network: {exc}",
        )

    if not resp.get("success"):
        err = resp.get("errorMsg") or f"status={resp.get('status')!r}"
        return FillResult(
            status="rejected", order_id=None, filled_size=0.0,
            avg_price=None, error=err,
        )

    order_id = resp.get("orderID")
    if not order_id:
        return FillResult(
            status="rejected", order_id=None, filled_size=0.0,
            avg_price=None, error="no-order-id",
        )

    # Cancel any remainder. A fully-matched order will return an error
    # here which is fine — cancel_order swallows it and logs.
    self.cancel_order(order_id)

    # Derive real fill from per-fill /trades data (post-fee shares).
    try:
        info = self.get_settlement_info(order_id)
    except Exception as exc:
        log.warning("submit_ioc: get_settlement_info failed for %s: %s", order_id, exc)
        return FillResult(
            status="error", order_id=order_id, filled_size=0.0,
            avg_price=None, error=f"settlement-lookup: {exc}",
        )

    if info.shares_held <= 0:
        return FillResult(
            status="rejected", order_id=order_id, filled_size=0.0,
            avg_price=None, error="gtc-no-fill",
        )

    avg_price = info.cost_usdc / info.shares_held if info.shares_held > 0 else price
    return FillResult(
        status="filled", order_id=order_id,
        filled_size=info.shares_held, avg_price=avg_price, error=None,
    )
```

**Step 4: Run tests**

```
pytest tests/test_polymarket_client.py -k submit_ioc -v
```
Expected: 7 PASSED.

**Step 5: Run the full client test file**

```
pytest tests/test_polymarket_client.py -v
```
Expected: all pass (including pre-existing `submit_fok` tests, which remain untouched).

**Step 6: Commit**

```bash
git add polypocket/clients/polymarket.py tests/test_polymarket_client.py
git commit -m "feat(live): submit_ioc — GTC + cancel remainder, settle from /trades (issue #9)"
```

---

## Task 4: Update `execute_live_trade` to use `submit_ioc` and record actual filled size

**Files:**
- Modify: `polypocket/executor.py:190-258` (the `execute_live_trade` function)
- Test: `tests/test_executor.py`

**Step 1: Write the failing tests**

Open `tests/test_executor.py`. Find the existing `execute_live_trade` tests (search for `test_execute_live_trade` or `submit_fok`). Add:

```python
def test_execute_live_trade_uses_submit_ioc(tmp_db, sample_signal):
    """Verify executor calls submit_ioc, not submit_fok."""
    client = MagicMock()
    client.get_usdc_balance.return_value = 100.0
    client.submit_ioc.return_value = FillResult(
        status="filled", order_id="abc",
        filled_size=7.0, avg_price=0.51, error=None,
    )

    result = execute_live_trade(
        db_path=tmp_db, signal=sample_signal, entry_price=0.51,
        size=7.0, window_slug="w1", token_id="T", condition_id="C",
        client=client,
    )

    assert result.success
    client.submit_ioc.assert_called_once()
    client.submit_fok.assert_not_called()


def test_execute_live_trade_partial_fill_persists_actual_size(tmp_db, sample_signal):
    """Partial fill: ledger row reflects filled_size, not requested size."""
    client = MagicMock()
    client.get_usdc_balance.return_value = 100.0
    client.submit_ioc.return_value = FillResult(
        status="filled", order_id="abc",
        filled_size=3.5, avg_price=0.52, error=None,
    )

    result = execute_live_trade(
        db_path=tmp_db, signal=sample_signal, entry_price=0.51,
        size=7.0, window_slug="w1", token_id="T", condition_id="C",
        client=client,
    )

    assert result.success
    # Read the ledger row back and confirm size/entry_price came from the fill.
    from polypocket.ledger import find_trade_by_window_slug
    row = find_trade_by_window_slug(tmp_db, "w1")
    assert row["size"] == pytest.approx(3.5)
    assert row["entry_price"] == pytest.approx(0.52)
```

The exact fixture names (`tmp_db`, `sample_signal`) may differ in your conftest — use the existing patterns from `test_executor.py`.

**Step 2: Run failing**

```
pytest tests/test_executor.py -k "submit_ioc or partial_fill" -v
```
Expected: FAIL — executor still calls `submit_fok`.

**Step 3: Modify `execute_live_trade`**

In `polypocket/executor.py`, change:

```python
fill = client.submit_fok(
```
to:
```python
fill = client.submit_ioc(
```

Then update the filled-success branch to persist the actual fill data:

```python
if fill.status == "filled":
    update_trade(
        db_path, trade_id,
        outcome=None, pnl=None, status="open",
        external_order_id=fill.order_id,
        size=fill.filled_size,
        entry_price=fill.avg_price,
    )
    log.info(
        "Live fill: %s %s requested=%.2f filled=%.4f vwap=$%.4f token=%s order=%s",
        window_slug, signal.side, size, fill.filled_size,
        fill.avg_price, token_id, fill.order_id,
    )
    return TradeResult(success=True, trade_id=trade_id, pnl=None)
```

This requires `update_trade` to accept `size` and `entry_price` kwargs. Check `polypocket/ledger.py` — if it already passes through arbitrary fields (most DB update helpers do), you're done. If not, you'll need a small extension in Task 4b below.

**Step 3a: Confirm `update_trade` signature**

```
grep -n "def update_trade" polypocket/ledger.py
```

If `update_trade` does NOT accept `size`/`entry_price`, extend it:

```python
def update_trade(
    db_path: str,
    trade_id: int,
    outcome: str | None = None,
    pnl: float | None = None,
    status: str | None = None,
    external_order_id: str | None = None,
    error: str | None = None,
    size: float | None = None,
    entry_price: float | None = None,
):
    # existing body — add size/entry_price to the SET clause when not None
```

Follow the existing pattern for conditional SETs.

**Step 4: Run tests**

```
pytest tests/test_executor.py -v
```
Expected: all pass including the two new ones.

**Step 5: Commit**

```bash
git add polypocket/executor.py tests/test_executor.py polypocket/ledger.py
git commit -m "feat(live): execute_live_trade uses submit_ioc; persist actual filled size/vwap"
```

---

## Task 5: Tighten the pre-trade floor gate in `bot.py`

**Files:**
- Modify: `polypocket/bot.py:424-460` (the depth-clamp block)
- Test: `tests/test_bot.py`

**Step 1: Write the failing tests**

In `tests/test_bot.py`, find the existing depth-clamp tests. Add:

```python
def test_bot_floor_gate_engages_when_fillable_below_min_position(...):
    """Pre-trade gate skips 'book-too-thin' when even MIN_FILL_RATIO of
    visible depth cannot clear MIN_POSITION_USDC.
    """
    # Setup: book with fillable * price * MIN_FILL_RATIO < MIN_POSITION_USDC
    # e.g. MIN_POSITION_USDC=5, price=0.50, MIN_FILL_RATIO=0.5
    #  => need fillable * 0.50 * 0.5 >= 5 => fillable >= 20
    # Make fillable = 10 (10 * 0.5 * 0.5 = 2.5 < 5).
    # ... (use the same pattern as existing depth-clamp tests)
    ...
    # Assert: _window_skip_reason == "book-too-thin", submit_ioc not called.


def test_bot_floor_gate_passes_at_boundary(...):
    """Exactly at the floor boundary, the gate allows the trade through."""
    # fillable * price * MIN_FILL_RATIO == MIN_POSITION_USDC exactly.
    # Assert: submit_ioc called.
```

Model these after whatever depth-clamp tests already exist — reuse their fixture/mocking shape.

**Step 2: Run failing**

```
pytest tests/test_bot.py -k floor_gate -v
```
Expected: FAIL — current code uses `target_size * entry_price < MIN_POSITION_USDC`, which is looser than the new gate.

**Step 3: Replace the current floor check**

In `polypocket/bot.py`, find the block starting at line ~430 (`book = window.up_book if signal.side == "up" else window.down_book`). Replace the existing two-part check (`target_size < size * MIN_FILL_RATIO` and `target_size * entry_price < MIN_POSITION_USDC`) with:

```python
book = window.up_book if signal.side == "up" else window.down_book
limit = fok_limit_price(entry_price)
fillable = sum(
    lvl["size"] for lvl in (book or []) if lvl["price"] <= limit + 1e-9
)

# Floor gate: under IOC, the realized fill can be anywhere between 0 and
# target_size. Skip unless even a MIN_FILL_RATIO slice of visible depth
# clears MIN_POSITION_USDC. This guarantees any non-skipped trade's
# worst-acceptable partial is above the dust floor.
floor_usdc = MIN_POSITION_USDC
if fillable * entry_price * MIN_FILL_RATIO < floor_usdc:
    self._window_skip_reason = "book-too-thin"
    log.warning(
        "Skipping signal: book too thin — fillable=%.2f @ <=$%.2f, "
        "min_slice_value=$%.2f < floor=$%.2f",
        fillable, limit, fillable * entry_price * MIN_FILL_RATIO, floor_usdc,
    )
    return

target_size = min(size, fillable * DEPTH_CLAMP_BUFFER)
if target_size < size:
    log.info(
        "Downsizing trade to depth: intended=%.2f target=%.2f "
        "fillable=%.2f limit=$%.2f",
        size, target_size, fillable, limit,
    )
    size = target_size
    size_usdc = target_size * entry_price
```

Note: this removes the separate `target_size < size * MIN_FILL_RATIO` check. It's subsumed by the new fillable-based gate: if `fillable * entry_price * MIN_FILL_RATIO >= floor_usdc`, then by definition there's enough depth that the gate passed on its own terms — the old intended-ratio check was only meaningful for FOK, where the *whole* target had to fit.

**Step 4: Run tests**

```
pytest tests/test_bot.py -v
```
Expected: all pass, including the new floor-gate tests. Existing depth-clamp tests may need minor updates — walk through each and adjust fixture numbers if they hit the new boundary. Any test that was relying on the removed `target_size < size * MIN_FILL_RATIO` branch needs to be re-expressed in terms of the new gate.

**Step 5: Commit**

```bash
git add polypocket/bot.py tests/test_bot.py
git commit -m "feat(live): tighten pre-trade floor to fillable*MIN_FILL_RATIO*price (issue #9)"
```

---

## Task 6: Add diagnostic INFO log per live trade

**Files:**
- Modify: `polypocket/bot.py` (right before the `execute_live_trade` call at line ~502)
- Test: `tests/test_bot.py`

**Step 1: Write the failing test**

In `tests/test_bot.py`:

```python
def test_bot_emits_diagnostic_log_line(caplog, ...):
    """Per-trade INFO line records intended vs. actual fill for root-cause analysis."""
    caplog.set_level(logging.INFO, logger="polypocket.bot")
    # Setup a happy-path live trade with a known fill
    ...
    # Assert: log output contains "IOC_DIAG" with expected fields
    records = [r for r in caplog.records if "IOC_DIAG" in r.getMessage()]
    assert len(records) == 1
    msg = records[0].getMessage()
    assert "intended=" in msg
    assert "target=" in msg
    assert "fillable=" in msg
    assert "limit=" in msg
```

We log the *post-submit* values after `execute_live_trade` returns, so `filled` and `vwap` are observable.

**Step 2: Run failing**

```
pytest tests/test_bot.py -k diagnostic -v
```
Expected: FAIL.

**Step 3: Implement the log line**

In `polypocket/bot.py`, just after the `execute_live_trade` call returns in the `TRADING_MODE != "paper"` branch and just before the result-branching block, add (inside an `if TRADING_MODE != "paper":` guard):

```python
if TRADING_MODE != "paper":
    filled = getattr(result, "filled_size", None)
    # Pull actual filled from ledger row since TradeResult carries only success/id/pnl.
    recorded = find_trade_by_window_slug(self.db_path, window.slug)
    actual_size = recorded.get("size") if recorded else None
    actual_price = recorded.get("entry_price") if recorded else None
    shortfall = None
    if actual_size is not None:
        shortfall = size - actual_size
    log.info(
        "IOC_DIAG intended=%.4f target=%.4f fillable=%.4f limit=%.4f "
        "filled=%s vwap=%s shortfall=%s",
        intended_size_pre_clamp, size, fillable, fok_limit_price(entry_price),
        f"{actual_size:.4f}" if actual_size is not None else "n/a",
        f"{actual_price:.4f}" if actual_price is not None else "n/a",
        f"{shortfall:.4f}" if shortfall is not None else "n/a",
    )
```

For this you need to preserve the pre-clamp `intended_size` in a local before the clamp overwrites `size`. Add near the top of the clamp block:

```python
intended_size_pre_clamp = size  # preserve for diagnostic log
```

**Step 4: Run tests**

```
pytest tests/test_bot.py -v
```

**Step 5: Commit**

```bash
git add polypocket/bot.py tests/test_bot.py
git commit -m "feat(live): per-trade IOC_DIAG log for root-cause analysis (issue #9)"
```

---

## Task 7: Add post-fill dust warning

**Files:**
- Modify: `polypocket/executor.py` (inside the `filled` branch of `execute_live_trade`)
- Test: `tests/test_executor.py`

**Step 1: Write the failing test**

```python
def test_execute_live_trade_logs_dust_warning(tmp_db, sample_signal, caplog):
    caplog.set_level(logging.WARNING, logger="polypocket.executor")
    client = MagicMock()
    client.get_usdc_balance.return_value = 100.0
    # Fill below MIN_POSITION_USDC * 0.25 notional.
    # With MIN_POSITION_USDC=5, dust floor is $1.25.
    # filled_size=2.0 @ $0.60 = $1.20 notional → dust.
    client.submit_ioc.return_value = FillResult(
        status="filled", order_id="abc",
        filled_size=2.0, avg_price=0.60, error=None,
    )

    result = execute_live_trade(
        db_path=tmp_db, signal=sample_signal, entry_price=0.61,
        size=7.0, window_slug="w1", token_id="T", condition_id="C",
        client=client,
    )

    assert result.success
    assert any("dust-fill" in r.getMessage() for r in caplog.records)
```

Use whatever `MIN_POSITION_USDC` fixture/monkeypatch pattern `test_executor.py` already uses.

**Step 2: Run failing**

```
pytest tests/test_executor.py -k dust -v
```

**Step 3: Implement**

In `polypocket/executor.py`, inside `execute_live_trade` at the `fill.status == "filled"` branch, just after `update_trade`:

```python
from polypocket.config import MIN_POSITION_USDC

notional = fill.filled_size * (fill.avg_price or 0.0)
if notional < MIN_POSITION_USDC * 0.25:
    log.warning(
        "dust-fill %s: filled=%.4f @ $%.4f = $%.4f < floor=$%.4f",
        window_slug, fill.filled_size, fill.avg_price or 0.0,
        notional, MIN_POSITION_USDC * 0.25,
    )
```

Move the import to the top of the file if it's not there.

**Step 4: Run tests**

```
pytest tests/test_executor.py -v
```

**Step 5: Commit**

```bash
git add polypocket/executor.py tests/test_executor.py
git commit -m "feat(live): warn on dust-fill below MIN_POSITION_USDC/4 (issue #9)"
```

---

## Task 8: Add `GTC_CANCEL_TIMEOUT_S` config (optional — defer if py_clob_client has no timeout knob)

**Files:**
- Modify: `polypocket/config.py`
- Modify: `polypocket/clients/polymarket.py`

This task is optional. `py_clob_client` methods may not expose per-call timeouts. If they don't, skip this task — the `cancel_order` retry loop bounds total wall time already. Note the decision in the commit message if you skip.

If the client's session does expose a timeout, add:

```python
# polypocket/config.py
GTC_CANCEL_TIMEOUT_S = float(os.getenv("GTC_CANCEL_TIMEOUT_S", "2.0"))
```

And thread it through `cancel_order`. Test: `pytest tests/test_polymarket_client.py -v`.

**Commit:**

```bash
git add polypocket/config.py polypocket/clients/polymarket.py
git commit -m "feat(live): GTC_CANCEL_TIMEOUT_S config for cancel call bound"
# or skip with:
git commit --allow-empty -m "chore: skip GTC_CANCEL_TIMEOUT_S — py_clob_client has no per-call timeout"
```

---

## Task 9: Full test pass + lint

**Step 1:** Run entire test suite.

```
pytest tests/ -v
```
Expected: all pass.

**Step 2:** Lint (if the project has a linter wired).

```
ruff check polypocket tests  # or whatever the project uses
```

**Step 3:** Commit any fixups.

---

## Task 10: Live rollout

Not a code task — runtime verification.

**Step 1:** Ensure `.env` still has `FOK_SLIPPAGE_TICKS=6` (leave the stop-gap until IOC is proven).

**Step 2:** Start the bot in live mode. Let it run for ≥ 10 trades (~50 minutes with 5-minute windows).

**Step 3:** Pull metrics:

```
sqlite3 live_trades.db "SELECT status, COUNT(*) FROM trades WHERE id > <cutoff> GROUP BY status"
sqlite3 live_trades.db "SELECT COUNT(*) FROM trades WHERE status='rejected' AND error='gtc-no-fill' AND id > <cutoff>"
```

Definition of done from the design doc:
- Reject rate ≈ 0 (≤ 1 `gtc-no-fill` tolerance over 10 trades).
- No orphan `status='open'` after session end (check with a `WHERE status='open'` query after settlements resolve).
- Zero `cancel-failed` ERROR log lines.
- Zero `429` response lines.

Also scan the `IOC_DIAG` log lines — they should show consistent shortfall patterns if any hypothesis is correct.

**Step 4:** If DoD holds, revert `FOK_SLIPPAGE_TICKS` and delete `submit_fok`.

```bash
# in .env, set FOK_SLIPPAGE_TICKS=3 (or unset, which defaults to 3)
# then in code:
```

Remove `submit_fok` from `polypocket/clients/polymarket.py`, delete its tests, remove `submit_fok` from the `LiveOrderClient` Protocol, remove the probe script if satisfied.

```bash
git commit -m "chore(live): remove deprecated submit_fok path after IOC rollout"
```

**Step 5:** Close issue #9 with a comment referencing the rollout results.

---

## Notes for the implementer

- **Fixture names in tests are placeholders.** Match the style already in `test_polymarket_client.py`, `test_bot.py`, `test_executor.py`. Search for "conftest" and existing fixtures before adding new ones.
- **`MIN_POSITION_USDC` default.** Check `polypocket/config.py` for its current value; the dust-floor constant `× 0.25` is hardcoded to match — no new env var.
- **Commit frequency.** Each task ends in a commit. If a task balloons, split it rather than piling changes into one commit.
- **If the probe (Task 0) fails** (e.g., cancel wipes matched quantity): STOP and surface the finding. The design assumes probe passes. Failure means Task 3's shape changes to a pre-cancel `get_order` read.
- **Don't mock the DB.** `test_executor.py` uses real sqlite via `tmp_db` fixture. Preserve that — the ledger code is small enough that mocking it would just hide bugs. (Matches project norm per feedback memory.)
