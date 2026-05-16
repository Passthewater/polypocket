# Post-only / maker-side entries — implementation

Companion to `2026-05-15-post-only-entries-design.md`. Linear, in-chat
execution — each step is a single concrete action with explicit
acceptance criteria. No subagent dispatch.

## Pre-flight

- Design doc reviewed and approved.
- Bot is stopped before any live-touching steps (Steps 7–10).
- Worktree clean OR carrying only the known pre-existing edits enumerated
  in the previous v2-migration plan (those don't conflict).

Branch:

```powershell
git checkout -b feat/post-only-entries
```

## Step 1 — Config plumbing + conftest._key

In `polypocket/config.py`, append after the existing `IOC_BUFFER_TICKS`
block:

```python
# --- Post-only entry path (#TBD post-only-entries) ---
# Execution mode for live trades. "fak" keeps the current pair-merge taker
# (FAK-via-v2 SDK) path; "post_only" routes through a GTC + post_only
# resting maker order. Paper mode ignores this — paper continues to fill
# at ask instantly. Read at the bot dispatch site, not in the executor.
ENTRY_MODE = os.getenv("ENTRY_MODE", "fak").strip().lower()
# Ticks below the pair-merge clearing price (1 - best_opp_bid) to rest a
# post-only maker order. Default 2: absorbs typical 200–500 ms book drift
# between gate-eval and SDK sign while still capturing ~7 ticks of edge
# over the FAK regime's `pmc + IOC_BUFFER_TICKS`. Tune from cohort data
# after the first 50–100 fills.
POST_ONLY_REST_OFFSET_TICKS = int(os.getenv("POST_ONLY_REST_OFFSET_TICKS", "2"))
# Wall-clock seconds-remaining at which the bot tick cancels a resting
# post-only order. Matches WINDOW_ENTRY_MIN_REMAINING so a post-only fill
# never lands in the dead-band where the gate also refuses new signals.
POST_ONLY_CANCEL_AT_T_REMAINING_S = float(os.getenv("POST_ONLY_CANCEL_AT_T_REMAINING_S", "30"))
# Seconds subtracted from window.end_time when computing the server-side
# `expiration` field. Defense-in-depth against a bot tick that doesn't fire
# its own cancel by POST_ONLY_CANCEL_AT_T_REMAINING_S — server kills the
# order if the bot is silent.
POST_ONLY_EXPIRY_SAFETY_BUFFER_S = float(os.getenv("POST_ONLY_EXPIRY_SAFETY_BUFFER_S", "30"))
```

Add the four names to `snapshot_gate_config()`'s returned dict (alphabetic
or grouped with the existing post-only-adjacent keys; either is fine).

In `tests/conftest.py`, append to the `_key` tuple:

```python
    "ENTRY_MODE",
    "POST_ONLY_REST_OFFSET_TICKS",
    "POST_ONLY_CANCEL_AT_T_REMAINING_S",
    "POST_ONLY_EXPIRY_SAFETY_BUFFER_S",
```

**Acceptance:**

- `python -c "from polypocket.config import ENTRY_MODE, POST_ONLY_REST_OFFSET_TICKS, POST_ONLY_CANCEL_AT_T_REMAINING_S, POST_ONLY_EXPIRY_SAFETY_BUFFER_S; print(ENTRY_MODE, POST_ONLY_REST_OFFSET_TICKS)"` prints `fak 2`.
- `pytest tests/test_config.py` green (the test that asserts `snapshot_gate_config()` round-trips known keys picks up the new ones with no manual edit; if it fails, the test itself needs the new keys added — that's an in-this-PR edit).

## Step 2 — Ledger schema additions

In `polypocket/ledger.py`, inside the `init_db` idempotent ALTER block,
append:

```python
if "entry_mode" not in existing_cols:
    conn.execute("ALTER TABLE trades ADD COLUMN entry_mode TEXT")
if "rest_price" not in existing_cols:
    conn.execute("ALTER TABLE trades ADD COLUMN rest_price REAL")
```

In `log_trade`'s signature, add:

```python
entry_mode: str | None = None,
rest_price: float | None = None,
```

and append both to the INSERT column list / values tuple. Existing
callers (paper, FAK live) continue to work with the defaults.

**Acceptance:**

- `pytest tests/test_ledger.py` green.
- One new test in `test_ledger.py`: `test_log_trade_persists_entry_mode_and_rest_price` — call `log_trade(..., entry_mode="post_only", rest_price=0.54)` against an in-memory DB, fetch the row, assert both fields land.
- Re-running `init_db` against an existing DB does not error and does not duplicate columns (PRAGMA check). Reuse the existing idempotency tests' pattern.

## Step 3 — Pure helper: `post_only_rest_price`

In `polypocket/clients/polymarket.py`, immediately after `ioc_limit_price`,
add a pure module-level function:

```python
def post_only_rest_price(
    side: str,
    up_bids: list[dict] | None,
    down_bids: list[dict] | None,
    offset_ticks: int,
) -> float | None:
    """Maker rest price for a BUY UP/DOWN on a binary book.

    Sits `offset_ticks` below the pair-merge clearing price
    `pmc = 1 - best_opp_bid`. Returns None when the opposite book has no
    bid (no pair-merge counterparty exists; caller should skip with
    'no-pair-merge-counterparty'). Floored at $0.01 — a rest at $0.00
    is meaningless. Capped at $0.99 to mirror the FAK limit convention,
    though pmc-offset_ticks should never approach $0.99 in practice.

    Conservatively: a non-positive offset_ticks would request a rest at-or-
    above the cross, which post-only would reject. Caller's responsibility
    to keep offset_ticks >= 1; this function trusts the input.
    """
    opp_bids = down_bids if side == "up" else up_bids
    if not opp_bids:
        return None
    best_opp = max(float(b["price"]) for b in opp_bids)
    rest = (1.0 - best_opp) - offset_ticks * 0.01
    return round(max(0.01, min(0.99, rest)), 2)
```

**Acceptance:**

- New tests in `test_polymarket_client.py` mirroring the `test_ioc_limit_price_*` block:
  - `test_post_only_rest_price_up_uses_down_bid` — given down_bids [{price:0.45}], offset=2, returns 0.53.
  - `test_post_only_rest_price_down_uses_up_bid` — symmetric on the other side.
  - `test_post_only_rest_price_no_opp_bid_returns_none` — empty list returns None.
  - `test_post_only_rest_price_clamps_to_one_cent_floor` — extreme opp_bid (e.g. 0.99 with offset=5) doesn't return a sub-cent value.
- Pure function: covered by unit tests only. No mocks needed.

## Step 4 — Protocol extension + `PlaceResult`

In `polypocket/executor.py`:

Add the `PlaceResult` dataclass next to `FillResult`:

```python
@dataclass(frozen=True)
class PlaceResult:
    """Outcome of a post-only place request. Distinct from FillResult,
    which represents terminal fills; a PlaceResult represents the order's
    acceptance into the book (or its rejection)."""
    status: Literal["placed", "rejected", "error"]
    order_id: str | None
    error: str | None
```

Extend `LiveOrderClient` Protocol with:

```python
def submit_post_only(
    self, side: str, size: float, price: float,
    token_id: str, condition_id: str,
    expiration: int,
) -> PlaceResult: ...
```

`expiration` is required (not Optional) at the Protocol level — the
caller always knows the window deadline, and forcing it shifts the
default-handling burden off the SDK wrapper.

**Acceptance:**

- `pytest tests/test_executor.py` green — no new tests yet (those land in Step 6 when the executor function uses it).
- One typing-driven check: any existing stub of `LiveOrderClient` in tests that doesn't implement `submit_post_only` should fail an `isinstance(..., LiveOrderClient)`-style check at test-collection time. Since the existing stubs are duck-typed (Protocol is structural), this won't auto-fail — but a `mypy` or `ruff` sweep would catch missing methods. Skip explicit isinstance assertions; rely on the per-test `MagicMock(spec=LiveOrderClient)` pattern in `test_bot.py` to surface gaps.

## Step 5 — SDK wrapper: `submit_post_only` on `PolymarketClient`

In `polypocket/clients/polymarket.py`:

Add a new error classifier (sibling to `_classify_no_match_error`):

```python
def _classify_post_only_cross_error(exc: Exception) -> tuple[str, str] | None:
    """If `exc` is a v2-server post-only-would-cross rejection, return
    (order_id, label). Verified shape from Phase-3 dry-run probe; until
    then, accept any 400 with 'post' and 'only' tokens in the message.

    Returns ("post-only-would-cross", order_id_or_empty) on match.
    """
    if not isinstance(exc, PolyApiException) or exc.status_code != 400:
        return None
    body = exc.error_msg
    if not isinstance(body, dict):
        return None
    err = (body.get("error") or "").lower()
    if "post" not in err or ("only" not in err and "cross" not in err):
        return None
    order_id = body.get("orderID") or ""
    return order_id, "post-only-would-cross"
```

Add the import line: `OrderArgs` (alias of `OrderArgsV2`):

```python
from py_clob_client_v2 import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    ClobClient,
    MarketOrderArgs,
    OrderArgs,
    OrderPayload,
    OrderType,
    PartialCreateOrderOptions,
    Side,
    SignatureTypeV2,
    TradeParams,
)
```

Add the `submit_post_only` method on `PolymarketClient`:

```python
def submit_post_only(
    self, side, size, price, token_id, condition_id, expiration,
):
    """Post a GTC limit at `price` with post_only=True.

    `size` is in shares (NOT USDC) — this differs from `submit_ioc`/
    `submit_fok` whose `amount` field is USDC budget. `expiration` is a
    Unix-seconds timestamp; the server kills the order at that time if
    it hasn't filled.

    Returns PlaceResult with status="placed" on accepted, "rejected" on
    server-side rejection (e.g. post-only-would-cross), or "error" on
    network/signing failure.
    """
    if self._dry_run:
        log.info(
            "DRY-RUN submit_post_only side=%s size=%.2f price=%.4f exp=%d token=%s cond=%s",
            side, size, price, expiration, token_id, condition_id,
        )
        return PlaceResult(status="placed", order_id="DRY-RUN", error=None)

    # Tick-safe quantization — same defense-in-depth as submit_ioc, kept
    # for consistency. _tick_safe_size operates on (size, price).
    target_size_int = max(1, int(round(size)))
    size_int = _tick_safe_size(target_size_int, price)
    if size_int is None:
        log.error(
            "submit_post_only: no tick-safe size near %d for price=%.4f",
            target_size_int, price,
        )
        return PlaceResult(
            status="rejected", order_id=None, error="tick-size-unfixable",
        )

    args = OrderArgs(
        token_id=token_id,
        price=price,
        size=float(size_int),
        side=Side.BUY,  # mirrors the FAK path's `MarketOrderArgs(side=Side.BUY)`
                        # — OrderArgsV2.side is annotated `str` but the SDK
                        # accepts the IntEnum directly (no validation at the
                        # dataclass layer). str(Side.BUY) would produce
                        # "Side.BUY" and fail at the order builder.
        expiration=int(expiration),
    )
    try:
        resp = self._client.create_and_post_order(
            order_args=args,
            options=PartialCreateOrderOptions(tick_size=TICK_SIZE),
            order_type=OrderType.GTC,
            post_only=True,
        )
    except Exception as exc:
        cross = _classify_post_only_cross_error(exc)
        if cross is not None:
            order_id, label = cross
            return PlaceResult(
                status="rejected", order_id=order_id or None, error=label,
            )
        log.exception("submit_post_only network/signing error")
        return PlaceResult(
            status="error", order_id=None, error=f"network: {exc}",
        )

    # v2 success response: {"success": True, "orderID": "...",
    #   "status": "live" (resting), "makingAmount": ..., "takingAmount": ...}
    # A rest order shouldn't have any tradeIDs or matched amounts at place.
    if not resp.get("success"):
        err = resp.get("errorMsg") or f"status={resp.get('status')!r}"
        # Server-level post-only-cross can also arrive as success=False
        # with errorMsg text (not always as a 400 PolyApiException). Pattern-
        # match the error text the same way _classify does.
        lower = err.lower() if isinstance(err, str) else ""
        if "post" in lower and ("only" in lower or "cross" in lower):
            return PlaceResult(
                status="rejected", order_id=resp.get("orderID") or None,
                error="post-only-would-cross",
            )
        return PlaceResult(
            status="rejected", order_id=resp.get("orderID") or None, error=err,
        )

    order_id = resp.get("orderID")
    if not order_id:
        return PlaceResult(
            status="rejected", order_id=None, error="no-order-id",
        )

    return PlaceResult(status="placed", order_id=order_id, error=None)
```

**Acceptance:**

- New tests in `test_polymarket_client.py`:
  - `test_submit_post_only_placed` — happy path. Mock `create_and_post_order` to return `{"success": True, "orderID": "abc", "status": "live"}`; assert returned `PlaceResult(status="placed", order_id="abc", error=None)`.
  - `test_submit_post_only_passes_v2_order_args` — call args check. Mirror the FAK test: assert `OrderArgs` with the right token_id, price, size, **`side == Side.BUY` (the enum value, not the string "Side.BUY" — guards against `str(Side.BUY)` regression)**, expiration; assert `order_type=OrderType.GTC` and `post_only=True` are passed; assert `tick_size="0.01"`.
  - `test_submit_post_only_would_cross_via_400` — mock `create_and_post_order` to raise a `PolyApiException` with status=400 and a body containing "post-only would cross"; assert `PlaceResult(status="rejected", error="post-only-would-cross")`.
  - `test_submit_post_only_would_cross_via_success_false` — same outcome via `{"success": False, "errorMsg": "post_only_would_cross"}`.
  - `test_submit_post_only_network_error` — raise a generic Exception; assert `status="error"` and error starts with `"network:"`.
  - `test_submit_post_only_tick_safe_unfixable` — patch `_tick_safe_size` to return None; assert `rejected` with `error="tick-size-unfixable"`.

## Step 6 — Executor lifecycle: `execute_live_trade_post_only`

In `polypocket/executor.py`, add a new function after `execute_live_trade`:

```python
def execute_live_trade_post_only(
    db_path: str,
    signal: Signal,
    intended_size: float,
    window_slug: str,
    token_id: str,
    condition_id: str,
    client: LiveOrderClient,
    *,
    up_bids: list[dict] | None,
    down_bids: list[dict] | None,
    offset_ticks: int,
    expiration: int,
    submit_book_age_s_monotonic: float | None = None,
) -> TradeResult:
    """Place a post-only resting maker order at pmc - offset_ticks.

    Behavior:
    - Computes rest_price at call-time against the freshest available
      bids (the caller passes the gate-time bids; the wrapper passes the
      same bids on through but a future enhancement may pass fresher
      ones). Rejects with 'no-pair-merge-counterparty' if no opp bid.
    - Logs trade row at status='placed' with size=intended_size,
      entry_price=rest_price (intended values; overwritten at cancel).
    - Writes `place` and `ack` order_events.
    - On placement reject (post-only-would-cross), trade row goes to
      status='rejected' immediately and returns failure.
    - On successful placement, returns a success TradeResult with the
      order resting. Cancel + reconcile happens in `cancel_post_only_order`
      called by the bot tick or reconciler.

    Place-time pmc recompute: rest_price is computed inside this function
    from the bids passed in. If a future enhancement wants a fresher pmc
    (e.g., a synchronous `client.get_order_book(opposite_token_id)` call
    between gate and place), do it here.
    """
    existing_trade = find_trade_by_window_slug(db_path, window_slug)
    if existing_trade is not None:
        return _window_consumed_result(db_path, window_slug)

    from polypocket.clients.polymarket import post_only_rest_price
    rest_price = post_only_rest_price(signal.side, up_bids, down_bids, offset_ticks)
    if rest_price is None:
        return TradeResult(success=False, error="no-pair-merge-counterparty")

    usdc_needed = rest_price * intended_size
    if client.get_usdc_balance() < usdc_needed:
        return TradeResult(success=False, error="insufficient-balance")

    fee_sh = fee_shares(intended_size, rest_price)
    try:
        trade_id = log_trade(
            db_path=db_path,
            window_slug=window_slug,
            side=signal.side,
            entry_price=rest_price,
            size=intended_size,
            fees=fee_sh,
            model_p_up=signal.model_p_up,
            market_p_up=signal.market_price,
            edge=signal.edge,
            outcome=None,
            pnl=None,
            status="placed",
            signal_reference_price=signal.signal_reference_price,
            signal_reference_source="live",
            entry_mode="post_only",
            rest_price=rest_price,
        )
    except sqlite3.IntegrityError:
        consumed = _window_consumed_result(db_path, window_slug)
        if consumed.trade_id is not None:
            return consumed
        raise

    log_order_event(
        db_path, trade_id, window_slug, "place", time.time(),
        payload={
            "side": signal.side,
            "intended_size": intended_size,
            "rest_price": rest_price,
            "offset_ticks": offset_ticks,
            "expiration": expiration,
            "signal_reference_price": signal.signal_reference_price,
            "book_age_s_monotonic": submit_book_age_s_monotonic,
        },
    )
    place = client.submit_post_only(
        side=signal.side, size=intended_size, price=rest_price,
        token_id=token_id, condition_id=condition_id, expiration=expiration,
    )
    log_order_event(
        db_path, trade_id, window_slug, "ack", time.time(),
        payload={"status": place.status, "error": place.error},
        external_order_id=place.order_id,
    )

    if place.status == "rejected":
        update_trade(
            db_path, trade_id, outcome=None, pnl=None, status="rejected",
            external_order_id=place.order_id, error=place.error,
        )
        log_order_event(
            db_path, trade_id, window_slug, "reject", time.time(),
            payload={"error": place.error},
            external_order_id=place.order_id,
        )
        log.warning(
            "post-only reject: %s %s @%.4f x%.2f: %s",
            window_slug, signal.side, rest_price, intended_size, place.error,
        )
        return TradeResult(success=False, trade_id=trade_id, error=place.error)

    if place.status == "error":
        update_trade(
            db_path, trade_id, outcome=None, pnl=None, status="rejected",
            external_order_id=place.order_id, error=place.error,
        )
        return TradeResult(success=False, trade_id=trade_id, error=place.error)

    # status == "placed"
    update_trade(
        db_path, trade_id, outcome=None, pnl=None, status="placed",
        external_order_id=place.order_id,
    )
    log.info(
        "post-only PLACED: %s %s rest=$%.4f x%.2f exp=%d token=%s order=%s",
        window_slug, signal.side, rest_price, intended_size,
        expiration, token_id, place.order_id,
    )
    return TradeResult(success=True, trade_id=trade_id, pnl=None)
```

Add a companion `cancel_post_only_order` function:

```python
def cancel_post_only_order(
    db_path: str,
    trade: dict,
    client: LiveOrderClient,
    trigger: str,
) -> str:
    """Cancel a resting post-only order and reconcile.

    Reads the post-cancel CLOB state via get_settlement_info (authoritative
    even when cancel races a fill). Updates the trade row:
    - shares_held > 0: status='open' with size=shares_held,
      entry_price=cost/shares — continues to settlement at window close.
    - shares_held == 0: status='rejected' with error='post-only-no-fill'.

    Returns the final local status ('open' or 'rejected'). On client
    errors (cancel failure or settlement-lookup failure), preserves the
    'placed' status and writes a diagnostic event — the next reconciler
    pass will retry.
    """
    trade_id = trade["id"]
    window_slug = trade["window_slug"]
    order_id = trade.get("external_order_id")
    if not order_id:
        log.warning("cancel_post_only_order: no order_id on trade %s", trade_id)
        return trade.get("status", "placed")

    log_order_event(
        db_path, trade_id, window_slug, "cancel", time.time(),
        payload={"trigger": trigger, "phase": "request"},
        external_order_id=order_id,
    )
    cancel_ok = False
    try:
        cancel_ok = client.cancel_order(order_id)
    except Exception as exc:
        log.warning("cancel_post_only_order: cancel_order raised for %s: %s",
                    order_id, exc)

    try:
        info = client.get_settlement_info(order_id)
    except Exception as exc:
        log.exception("cancel_post_only_order: get_settlement_info failed for %s: %s",
                      order_id, exc)
        log_order_event(
            db_path, trade_id, window_slug, "cancel", time.time(),
            payload={"trigger": trigger, "phase": "ack",
                     "cancel_success": cancel_ok,
                     "settlement_lookup_error": str(exc)},
            external_order_id=order_id,
        )
        return trade.get("status", "placed")

    log_order_event(
        db_path, trade_id, window_slug, "cancel", time.time(),
        payload={
            "trigger": trigger, "phase": "ack",
            "cancel_success": cancel_ok,
            "shares_held": info.shares_held,
            "cost_usdc": info.cost_usdc,
        },
        external_order_id=order_id,
    )

    if info.shares_held > 0:
        avg_price = info.cost_usdc / info.shares_held
        update_trade(
            db_path, trade_id, outcome=None, pnl=None, status="open",
            size=info.shares_held, entry_price=avg_price,
        )
        # update_trade's error column is COALESCE-preserved; clear any
        # stale error text from earlier transitions on this row so the
        # promoted-to-open row reads cleanly on post-mortem. Mirrors the
        # pattern in reconcile_recovered_trade's stranded-fill branch.
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "UPDATE trades SET error = NULL WHERE id = ?", (trade_id,),
            )
            conn.commit()
        # Reuse the existing dust-warn line; threshold is the same.
        notional = info.shares_held * avg_price
        if notional < MIN_POSITION_USDC * 0.25:
            log.warning(
                "post-only dust-fill %s: %.4f @ $%.4f = $%.4f < floor=$%.4f",
                window_slug, info.shares_held, avg_price, notional,
                MIN_POSITION_USDC * 0.25,
            )
        log_order_event(
            db_path, trade_id, window_slug, "fill", time.time(),
            payload={"filled_size": info.shares_held, "avg_price": avg_price,
                     "via": "cancel-reconcile"},
            external_order_id=order_id,
        )
        log.info(
            "post-only FILLED via cancel-reconcile: %s %s %.4f @ $%.4f",
            window_slug, trade["side"], info.shares_held, avg_price,
        )
        return "open"

    update_trade(
        db_path, trade_id, outcome=None, pnl=None, status="rejected",
        error="post-only-no-fill",
    )
    log_order_event(
        db_path, trade_id, window_slug, "reject", time.time(),
        payload={"error": "post-only-no-fill"},
        external_order_id=order_id,
    )
    log.info("post-only NO-FILL: %s order=%s trigger=%s",
             window_slug, order_id, trigger)
    return "rejected"
```

Extend `reconcile_recovered_trade` for CLOB status `"live"`:

In the block that reads `clob_status` from `get_order_status`, after the
existing `"matched"` and `"canceled / cancelled / unmatched"` branches,
add:

```python
if clob_status == "live":
    # A post-only order from a previous run is still resting. Window
    # context is gone; cancel-and-reconcile inline.
    log_order_event(
        db_path, trade["id"], trade["window_slug"], "reconcile_live", time.time(),
        payload={"from_status": current_status, "clob_status": clob_status},
        external_order_id=order_id,
    )
    return cancel_post_only_order(db_path, trade, client, trigger="crash-recovery")
```

**Acceptance:**

- `pytest tests/test_executor.py` green plus new tests:
  - `test_execute_live_trade_post_only_placed_happy_path` — mock client.submit_post_only to return `PlaceResult(status="placed", order_id="abc")`; assert trade row at status=placed with rest_price persisted; assert `place` and `ack` events written.
  - `test_execute_live_trade_post_only_no_opp_bid_skips` — pass empty down_bids on a BUY UP; assert `TradeResult(success=False, error="no-pair-merge-counterparty")` and no trade row written.
  - `test_execute_live_trade_post_only_insufficient_balance` — mock get_usdc_balance below need; assert `insufficient-balance` and no trade row.
  - `test_execute_live_trade_post_only_would_cross_rejected` — mock submit_post_only to return `PlaceResult(status="rejected", error="post-only-would-cross")`; assert trade row at status=rejected with the error persisted; assert `place`, `ack`, `reject` events written.
  - `test_cancel_post_only_order_full_fill` — mock get_settlement_info to return shares_held=10, cost_usdc=5.40; assert trade promoted to status=open with size=10, entry_price=0.54; assert `cancel` and `fill` events written.
  - `test_cancel_post_only_order_zero_fill` — mock get_settlement_info shares_held=0; assert status=rejected with error="post-only-no-fill".
  - `test_cancel_post_only_order_cancel_race_partial_fill` — **integration-style**: mock cancel_order returns False (race), get_settlement_info returns shares_held=4, cost_usdc=2.16; assert trade ends at status=open size=4 entry_price=0.54. This is the load-bearing test for the cancel-race failure mode.
  - `test_reconcile_recovered_trade_live_status_triggers_cancel` — recovered trade at status=placed with order_id; mock get_order_status returns `{"status": "live"}`; assert cancel_post_only_order is called (verifying the new branch).

## Step 7 — Bot dispatch + cancel-on-tick

In `polypocket/bot.py`:

Add `ENTRY_MODE`, `POST_ONLY_REST_OFFSET_TICKS`, `POST_ONLY_CANCEL_AT_T_REMAINING_S`, `POST_ONLY_EXPIRY_SAFETY_BUFFER_S` to the top-of-file `from polypocket.config import ...` block.

Import the new executor function:

```python
from polypocket.executor import (
    LiveOrderClient,
    TradeResult,
    cancel_post_only_order,
    execute_live_trade,
    execute_live_trade_post_only,
    ...
)
```

In `Bot._on_book_update`, **before** the existing `recoverable_statuses = {"open"}` block, add `"placed"` to the live-mode set so a resting order survives restart:

```python
recoverable_statuses = {"open"}
if TRADING_MODE == "live":
    recoverable_statuses.add("reserved")
    recoverable_statuses.add("rejected")
    recoverable_statuses.add("placed")  # post-only resting orders
```

Add a cancel-on-tick check. Inside `_on_book_update`, after the existing
"if current window has expired and we're still holding an open trade,
settle it now" block and before the "Live-only stats/signal evaluation"
block, add:

```python
# Post-only cancel-on-tick: if we're holding a resting maker order and the
# window is in its no-trade band, cancel-and-reconcile so the order doesn't
# fill into the dead-zone. Server expiration is the safety net; this is the
# fast-path.
if (
    TRADING_MODE != "paper"
    and self._open_trade is not None
    and self._open_trade.get("status") == "placed"
    and self._current_window is not None
    and (self._current_window.end_time - now) <= POST_ONLY_CANCEL_AT_T_REMAINING_S
    and self.live_order_client is not None
):
    trade_row = find_trade_by_window_slug(self.db_path, self._current_window.slug)
    if trade_row is not None:
        final = cancel_post_only_order(
            self.db_path, trade_row, self.live_order_client,
            trigger="window-close",
        )
        if final == "open":
            # Reconcile produced a partial fill — adopt it for settlement.
            updated = find_trade_by_window_slug(self.db_path, self._current_window.slug)
            self._open_trade = {
                "trade_id": updated["id"],
                "side": updated["side"],
                "entry_price": updated["entry_price"],
                "size": updated["size"],
                "mode": TRADING_MODE,
                "status": "open",
                "external_order_id": updated.get("external_order_id"),
            }
            self.stats["position"] = self._format_position(self._open_trade)
        else:
            # rejected — no fill. Drop the open_trade tracking.
            self._open_trade = None
            self.stats["position"] = None
            self.stats["execution_status"] = "post-only-no-fill"
```

In the live-execution dispatch (the `else:` branch that today calls
`execute_live_trade(...)`), branch on `ENTRY_MODE`:

```python
if ENTRY_MODE == "post_only":
    # Note on settle interaction: if the bot is silent (no book events)
    # between t_remaining=POST_ONLY_CANCEL_AT_T_REMAINING_S and window
    # end, the bot-side cancel never fires. The server-side `expiration`
    # below is the safety net — it kills the order at
    # window.end_time - POST_ONLY_EXPIRY_SAFETY_BUFFER_S. By _settle_trade
    # time, get_settlement_info returns shares_held=0 and PnL settles
    # to 0. A status='placed' row reaching settle is therefore not a
    # bug — it's the bot-silent path with the safety net engaged.
    expiration = int(window.end_time - POST_ONLY_EXPIRY_SAFETY_BUFFER_S)
    result = execute_live_trade_post_only(
        db_path=self.db_path,
        signal=signal,
        intended_size=size,
        window_slug=window.slug,
        token_id=token_id,
        condition_id=window.condition_id,
        client=self.live_order_client,
        up_bids=window.up_bids,
        down_bids=window.down_bids,
        offset_ticks=POST_ONLY_REST_OFFSET_TICKS,
        expiration=expiration,
        submit_book_age_s_monotonic=book_age,
    )
else:
    # existing FAK path
    result = execute_live_trade(
        db_path=self.db_path,
        signal=signal,
        ...,
    )
```

After the existing `if result.success:` block, in the post-only path the
trade is at `status='placed'` not `'open'` — so the `self._open_trade`
dict's `status` field must reflect that. Update the existing block to
read `recorded["status"]` instead of hardcoding `"open"`:

```python
if result.success:
    self._window_traded = True
    ...
    recorded = find_trade_by_window_slug(self.db_path, window.slug)
    self._open_trade = {
        "trade_id": result.trade_id,
        "side": signal.side,
        "entry_price": (recorded.get("entry_price") if recorded else entry_price),
        "size": (recorded.get("size") if recorded else size),
        "mode": TRADING_MODE,
        "status": (recorded.get("status") if recorded else "open"),
        "external_order_id": external_order_id,
    }
```

**Acceptance:**

- `pytest tests/test_bot.py` green plus new tests:
  - `test_bot_dispatches_to_post_only_when_entry_mode_set` — set `ENTRY_MODE=post_only` via monkeypatch; assert the live branch calls `execute_live_trade_post_only`, not `execute_live_trade`.
  - `test_bot_cancels_post_only_at_t_remaining_threshold` — simulate a tick with `_open_trade.status='placed'` and `t_remaining < POST_ONLY_CANCEL_AT_T_REMAINING_S`; assert `cancel_post_only_order` is called once with `trigger="window-close"`.
  - `test_bot_post_only_cancel_promotes_partial_fill` — cancel_post_only_order mock returns "open"; assert `_open_trade.status="open"` and position string reflects the filled size.
  - `test_bot_post_only_cancel_zero_fill_drops_open_trade` — cancel returns "rejected"; assert `_open_trade is None` and stats execution_status reflects no-fill.
  - `test_recoverable_statuses_includes_placed_in_live` — assert that the live-mode recoverable set includes `"placed"` (small structural test).

## Step 8 — Paper-mode bit-identity check

A standalone test asserting nothing in the paper path changed:

`test_paper_path_unchanged_with_post_only_config_set`:

- Configure `ENTRY_MODE=post_only`, `TRADING_MODE=paper`.
- Fire a signal through a Bot instance with a mock binance feed and
  Polymarket window. Assert `execute_paper_trade` is called (not anything
  post-only). Assert no `submit_post_only` call on any client mock.

This is the bit-identity guarantee promised in the design doc. One test.

**Acceptance:**

- `pytest tests/test_bot.py::test_paper_path_unchanged_with_post_only_config_set` green.
- Existing paper-mode tests (`test_bot.py`) all still pass with the new code.

## Step 9 — Live probe script (uncommitted)

Write `scripts/_probe_post_only.py` (gitignored; mirrors the v2-migration
plan's Step 7 pattern):

```python
"""One-shot probe: place a post-only at $0.01 below pmc on a live BTC
window, verify it lands, cancel it, confirm zero fill. NOT committed."""
import os, time
from polypocket.clients.polymarket import PolymarketClient, post_only_rest_price
from polypocket import config

client = PolymarketClient(
    host=config.POLYMARKET_HOST, chain_id=config.CHAIN_ID,
    private_key=os.environ["PRIVATE_KEY"],
    api_creds={"key": os.environ["CLOB_API_KEY"],
               "secret": os.environ["CLOB_SECRET"],
               "passphrase": os.environ["CLOB_PASSPHRASE"]},
    proxy_address=os.environ["PROXY_ADDRESS"],
    dry_run=False,
)

# Manually populate from /events for an active BTC up/down market:
TOKEN_ID = "..."
CONDITION_ID = "..."
DOWN_BIDS = [{"price": 0.45, "size": 100}]  # placeholder; replace with real
UP_BIDS = []

rest_price = post_only_rest_price("up", UP_BIDS, DOWN_BIDS, offset_ticks=2)
print("rest_price:", rest_price)

# Expire in 60 seconds.
expiration = int(time.time()) + 60

place = client.submit_post_only(
    side="up", size=10.0, price=rest_price,
    token_id=TOKEN_ID, condition_id=CONDITION_ID,
    expiration=expiration,
)
print("place result:", place)
if place.status != "placed":
    raise SystemExit(f"place failed: {place.error}")

# Verify via /order
status = client.get_order_status(place.order_id)
print("order status (should show 'live' and rest price):", status)

input("Press enter to cancel.")
ok = client.cancel_order(place.order_id)
print("cancel result:", ok)

info = client.get_settlement_info(place.order_id)
print("post-cancel settlement (should be 0/0):", info)
```

**Acceptance:**

1. `client.submit_post_only` returns `PlaceResult(status="placed", order_id=<truthy>)`.
2. `client.get_order_status(order_id)` shows the order resting at the expected price.
3. After `cancel_order`, `get_settlement_info` returns `shares_held=0.0, cost_usdc=0.0`.
4. Document the exact server response shape for the success path in a one-line code comment near `submit_post_only` if the v2 docs were ambiguous.
5. **Expiration units verification:** place an order with `expiration = int(time.time()) + 60`, then do NOT cancel it. Wait. If the server kills it at ~60 seconds wall-clock (verify via `get_order_status` returning `"cancelled"` or similar), units are Unix-seconds as assumed. If it kills earlier (~60 ms) or never kills, units are milliseconds or something else — fix `POST_ONLY_EXPIRY_SAFETY_BUFFER_S` math at the bot.py dispatch site before proceeding to Step 9.
6. **Bonus probe (separate run):** force a would-cross — place at `pmc + 1` to deliberately cross. Confirm the rejection error shape. Update `_classify_post_only_cross_error` if needed to match the exact text/code.

If any acceptance criterion fails: STOP. Do not flip `ENTRY_MODE=post_only` in live mode.

## Step 10 — Replay script

Write `scripts/replay_post_only_paper.py` (committed) and run it against
current `paper_trades.db`:

```python
"""Post-hoc replay: what would the post-only path have filled, given the
historical paper FAK cohort's decision snapshots and book samples?

Reads from PAPER_DB_PATH (read-only). Emits scripts/_post_only_replay.md.
"""
import sqlite3, json
from contextlib import closing
from polypocket.config import (
    PAPER_DB_PATH, POST_ONLY_REST_OFFSET_TICKS,
    POST_ONLY_CANCEL_AT_T_REMAINING_S,
)
from polypocket.clients.polymarket import post_only_rest_price

def replay(db_path: str = PAPER_DB_PATH) -> dict:
    """For each window with a 'decision' snapshot + non-null bids JSON
    + trade_fired=1, compute the would-be rest_price and walk forward
    through window_book_samples to determine fill outcome.

    Returns aggregate stats: n_placed, n_filled, fill_rate,
    fills_by_offset_bin, calibration_table.
    """
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        decisions = conn.execute("""
            SELECT window_slug, preview_side, model_p_up, up_bids_json,
                   down_bids_json, timestamp
            FROM window_snapshots
            WHERE snapshot_type='decision' AND trade_fired=1
              AND up_bids_json IS NOT NULL AND down_bids_json IS NOT NULL
        """).fetchall()

        # For each decision, compute rest_price; walk samples to find fill.
        ...
        # Join to trades.outcome for the calibration table.
```

Full implementation (~150 LOC) is left for the engineer to write at task
time; this plan's scope is to specify the interface and acceptance:

**Acceptance:**

1. `python scripts/replay_post_only_paper.py` exits 0 against a
   populated `paper_trades.db`.
2. Emits `scripts/_post_only_replay.md` with at least: total decisions
   evaluated, fill rate, mean fill timing (sample-index at fill),
   would-be calibration per `model_p_up` decile.
3. Fill rate is in the plausible band 15–80%. Outside that band, halt
   and revisit `POST_ONLY_REST_OFFSET_TICKS`.
4. Re-running the script is idempotent — no DB writes, deterministic
   output for the same input.

## Step 11 — Commit + PR

Conventional Commits with scope, mirroring the v2-migration pattern:

```
chore(config): add ENTRY_MODE, POST_ONLY_REST_OFFSET_TICKS, *_CANCEL_AT_T_REMAINING_S, *_EXPIRY_SAFETY_BUFFER_S
chore(ledger): add entry_mode + rest_price columns to trades (idempotent)
feat(clients): add submit_post_only and post_only_rest_price to Polymarket v2 wrapper
feat(executor): post-only entry lifecycle and cancel-reconcile path
feat(bot): dispatch on ENTRY_MODE and cancel resting orders at t_remaining ≤ 30s
chore(scripts): add post-only paper replay
docs(plans): post-only entries design + implementation
test: post-only Protocol, executor, bot dispatch, replay
```

PR title: `feat: post-only / maker-side entry path behind ENTRY_MODE flag`.
Body references the diagnostic memory and the design doc. Default is
`ENTRY_MODE=fak`; promotion to `post_only` is a separate, post-validation
ops change (set the env var in the live process), not a code change.

## Step 12 — Memory updates after live cohort lands

Reserved for after a successful Phase-4 live cohort. Out-of-PR
follow-up:

- Update `project_live_v2_execution_gap.md` with the Phase-4 result —
  whether the calibration gap closed under post-only.
- If post-only wins the cohort, write a new memory
  `project_post_only_validated.md` noting `ENTRY_MODE=post_only` as the
  new default and the cohort that drove it.

## Effort estimate

| Step | Time |
|---|---|
| 1 — config + conftest | 15 min |
| 2 — ledger schema | 15 min |
| 3 — post_only_rest_price helper + tests | 30 min |
| 4 — Protocol + PlaceResult | 15 min |
| 5 — submit_post_only + tests | 60–90 min |
| 6 — executor lifecycle + cancel + recovery + tests | 90–120 min |
| 7 — bot dispatch + cancel-on-tick + tests | 60 min |
| 8 — paper bit-identity test | 15 min |
| 9 — live probe | 30 min |
| 10 — replay script | 60–90 min |
| 11 — commit + PR | 20 min |

Total: ~7–9 hours of focused work. The executor lifecycle (Step 6) and
SDK wrapper (Step 5) are the heaviest; bot dispatch (Step 7) is
moderate but the cancel-on-tick logic interacts with existing recovery
flow and warrants care.
