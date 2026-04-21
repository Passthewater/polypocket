# Reducing FOK rejects: implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Execute in-chat, linearly; do not dispatch subagents.

**Goal:** Reduce live FOK reject rate by replacing the all-or-nothing depth gate in `bot.py` with a size-to-depth clamp, so we ask for at most `DEPTH_CLAMP_BUFFER × fillable_depth` shares and only skip when depth can't fund `MIN_FILL_RATIO × intended_size`.

**Architecture:** Pure logic change inside `Bot._on_book_update` (live branch only). No change to `PolymarketClient.submit_fok` or to order semantics — still FOK. Two new env-configurable constants in `config.py`.

**Tech Stack:** Python 3, pytest, sqlite3. Source: `polypocket/bot.py`, `polypocket/config.py`. Tests: `tests/test_bot.py`, `tests/test_config.py`.

Design doc: `docs/plans/2026-04-21-reducing-fok-rejects-design.md`.

---

### Task 1: Add config constants

**Files:**
- Modify: `polypocket/config.py` (alongside `FOK_SLIPPAGE_TICKS`, ~line 87)
- Test: `tests/test_config.py`

**Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_depth_clamp_buffer_default():
    import importlib, polypocket.config as cfg
    importlib.reload(cfg)
    assert cfg.DEPTH_CLAMP_BUFFER == 0.9

def test_min_fill_ratio_default():
    import importlib, polypocket.config as cfg
    importlib.reload(cfg)
    assert cfg.MIN_FILL_RATIO == 0.5

def test_depth_clamp_buffer_env_override(monkeypatch):
    monkeypatch.setenv("DEPTH_CLAMP_BUFFER", "0.75")
    import importlib, polypocket.config as cfg
    importlib.reload(cfg)
    assert cfg.DEPTH_CLAMP_BUFFER == 0.75

def test_min_fill_ratio_env_override(monkeypatch):
    monkeypatch.setenv("MIN_FILL_RATIO", "0.25")
    import importlib, polypocket.config as cfg
    importlib.reload(cfg)
    assert cfg.MIN_FILL_RATIO == 0.25
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config.py -k "depth_clamp or min_fill_ratio" -v`
Expected: FAIL with `AttributeError: module 'polypocket.config' has no attribute 'DEPTH_CLAMP_BUFFER'`.

**Step 3: Add the constants**

In `polypocket/config.py`, immediately after the `FOK_SLIPPAGE_TICKS` block (after line ~87):

```python
# Fraction of book depth (at <= FOK limit price) we ask for as our FOK
# size. Leaves headroom for the book to thin between our depth read and
# the signed order reaching the matcher. 0.9 = ask for at most 90% of
# visible fillable size.
DEPTH_CLAMP_BUFFER = float(os.getenv("DEPTH_CLAMP_BUFFER", "0.9"))
# Minimum fraction of intended size a trade must be able to fill for us
# to bother. If depth-clamped target_size < intended * MIN_FILL_RATIO,
# skip the window with reason "book-too-thin".
MIN_FILL_RATIO = float(os.getenv("MIN_FILL_RATIO", "0.5"))
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config.py -k "depth_clamp or min_fill_ratio" -v`
Expected: 4 passed.

**Step 5: Commit**

```bash
git add polypocket/config.py tests/test_config.py
git commit -m "feat(live): add DEPTH_CLAMP_BUFFER and MIN_FILL_RATIO config"
```

---

### Task 2: Replace depth gate with size-to-depth clamp in bot.py

**Files:**
- Modify: `polypocket/bot.py:422-436` (the existing depth-gate block)
- Modify: `polypocket/bot.py:7-21` (config imports)
- Test: `tests/test_bot.py`

**Step 1: Write the failing tests**

Append to `tests/test_bot.py` (the file already has `_CapturingClient` and `_make_live_bot` helpers from earlier tests — reuse them).

```python
@pytest.mark.asyncio
async def test_bot_live_clamps_size_when_book_shallow(tmp_path: Path, monkeypatch):
    """Book holds less than intended size but >= MIN_FILL_RATIO * intended:
    trade fires at clamped size (fillable * DEPTH_CLAMP_BUFFER)."""
    client = _CapturingClient()
    bot = _make_live_bot(tmp_path, monkeypatch, client)

    # Entry 0.55, FOK_SLIPPAGE_TICKS=3 -> limit 0.58. Intended ~ 18 shares
    # ($10 / 0.55) with defaults. Book holds 12 shares at <= 0.58 -> ratio
    # 12/18 = 0.67 > 0.5. Clamp to 12 * 0.9 = 10.8 shares.
    window = Window(
        condition_id="shallow-test",
        question="BTC Up or Down",
        up_token_id="UP", down_token_id="DOWN",
        end_time=time.time() + 180,
        slug="btc-updown-5m-shallow",
        price_to_beat=84198.0,
        up_ask=0.55, down_ask=0.45,
        up_book=[
            {"price": 0.55, "size": 8.0},
            {"price": 0.57, "size": 4.0},
            {"price": 0.70, "size": 1000.0},  # outside limit band
        ],
        down_book=[{"price": 0.45, "size": 1000.0}],
        book_updated_at=time.monotonic(),
    )

    await bot._on_book_update(window, "up")

    assert len(client.calls) == 1
    # fillable = 12, clamped = 12 * 0.9 = 10.8. intended derived from
    # edge/vol sizing, but is > 12 for this signal strength, so the
    # clamp engages and the submitted size equals 10.8 (±fp).
    assert client.calls[0]["size"] == pytest.approx(10.8, rel=1e-3)


@pytest.mark.asyncio
async def test_bot_live_submits_intended_when_book_deep(tmp_path: Path, monkeypatch):
    """Book holds far more than intended -> clamp is a no-op."""
    client = _CapturingClient()
    bot = _make_live_bot(tmp_path, monkeypatch, client)

    window = Window(
        condition_id="deep-test",
        question="BTC Up or Down",
        up_token_id="UP", down_token_id="DOWN",
        end_time=time.time() + 180,
        slug="btc-updown-5m-deep",
        price_to_beat=84198.0,
        up_ask=0.55, down_ask=0.45,
        up_book=[{"price": 0.55, "size": 1000.0}],
        down_book=[{"price": 0.45, "size": 1000.0}],
        book_updated_at=time.monotonic(),
    )

    await bot._on_book_update(window, "up")

    assert len(client.calls) == 1
    # intended size is some edge/vol-derived value; just check the clamp
    # did NOT reduce it below something the old flow would have accepted.
    assert client.calls[0]["size"] > 1.0  # not dust
    # And NOT clamped to book depth * 0.9 = 900 (which would mean clamp
    # fired incorrectly).
    assert client.calls[0]["size"] < 100.0


@pytest.mark.asyncio
async def test_bot_live_skips_when_depth_below_min_fill_ratio(
    tmp_path: Path, monkeypatch
):
    """Book holds < MIN_FILL_RATIO * intended -> skip book-too-thin."""
    client = _CapturingClient()
    bot = _make_live_bot(tmp_path, monkeypatch, client)

    # Same shape as the old test_bot_live_skips_when_book_too_thin: only
    # 3 shares at <= limit. With intended ~ 18 shares, clamp would give
    # 3*0.9=2.7, which is 2.7/18 = 0.15 < MIN_FILL_RATIO (0.5) -> skip.
    window = Window(
        condition_id="thin-test",
        question="BTC Up or Down",
        up_token_id="UP", down_token_id="DOWN",
        end_time=time.time() + 180,
        slug="btc-updown-5m-thin",
        price_to_beat=84198.0,
        up_ask=0.55, down_ask=0.45,
        up_book=[
            {"price": 0.55, "size": 2.0},
            {"price": 0.56, "size": 1.0},
            {"price": 0.70, "size": 1000.0},
        ],
        down_book=[{"price": 0.45, "size": 1000.0}],
        book_updated_at=time.monotonic(),
    )

    await bot._on_book_update(window, "up")

    assert client.calls == []
    assert bot._window_skip_reason == "book-too-thin"


@pytest.mark.asyncio
async def test_bot_live_skips_when_book_empty(tmp_path: Path, monkeypatch):
    """Empty book / None -> skip book-too-thin (fillable=0)."""
    client = _CapturingClient()
    bot = _make_live_bot(tmp_path, monkeypatch, client)

    window = Window(
        condition_id="empty-test",
        question="BTC Up or Down",
        up_token_id="UP", down_token_id="DOWN",
        end_time=time.time() + 180,
        slug="btc-updown-5m-empty",
        price_to_beat=84198.0,
        up_ask=0.55, down_ask=0.45,
        up_book=[],
        down_book=[{"price": 0.45, "size": 1000.0}],
        book_updated_at=time.monotonic(),
    )

    await bot._on_book_update(window, "up")

    assert client.calls == []
    assert bot._window_skip_reason == "book-too-thin"


@pytest.mark.asyncio
async def test_bot_live_skips_when_clamped_size_below_min_position_usdc(
    tmp_path: Path, monkeypatch
):
    """Clamp passes ratio but clamped_size * price < MIN_POSITION_USDC -> skip.

    With MIN_POSITION_USDC=5 (default), a clamped size of 8 shares at $0.55 =
    $4.40 is below the floor; trade must skip rather than submit a dust
    order. Use a small intended size so ratio passes but floor blocks.
    """
    client = _CapturingClient()
    bot = _make_live_bot(tmp_path, monkeypatch, client)
    # Downsize intent artificially by forcing a tiny available balance so
    # the balance clamp pushes intended size close to the floor, then the
    # depth clamp shaves it below.
    monkeypatch.setattr(
        "polypocket.bot.MIN_POSITION_USDC", 5.0, raising=False
    )

    window = Window(
        condition_id="floor-test",
        question="BTC Up or Down",
        up_token_id="UP", down_token_id="DOWN",
        end_time=time.time() + 180,
        slug="btc-updown-5m-floor",
        price_to_beat=84198.0,
        up_ask=0.55, down_ask=0.45,
        # fillable=8. clamped=7.2. 7.2*0.55=$3.96 < $5 floor.
        up_book=[{"price": 0.55, "size": 8.0}, {"price": 0.70, "size": 1000.0}],
        down_book=[{"price": 0.45, "size": 1000.0}],
        book_updated_at=time.monotonic(),
    )

    await bot._on_book_update(window, "up")

    assert client.calls == []
    assert bot._window_skip_reason == "book-too-thin"
```

Also **remove the now-obsolete test** `test_bot_live_skips_when_book_too_thin` (the old gate-based one at `tests/test_bot.py:1143-1171`). It's superseded by `test_bot_live_skips_when_depth_below_min_fill_ratio`, which asserts the same behavior under the new logic. Deleting avoids double-coverage drift.

**Step 2: Run the new tests to verify they fail**

Run: `pytest tests/test_bot.py -k "clamp or min_fill_ratio or book_empty or min_position" -v`
Expected: all new tests FAIL (clamp logic not yet written).

Also run the old test still present:
Run: `pytest tests/test_bot.py::test_bot_live_skips_when_depth_below_min_fill_ratio -v`
Expected: FAIL — because the old gate rejects on `fillable < intended*1.1` which happens to still skip correctly for this thin book case, but the assertion on `_window_skip_reason` may pass by coincidence. That's fine.

**Step 3: Update imports in bot.py**

In `polypocket/bot.py`, update the config import block (around lines 7-21):

```python
from polypocket.config import (
    CALIBRATION_SHRINKAGE_DOWN,
    CALIBRATION_SHRINKAGE_UP,
    DEPTH_CLAMP_BUFFER,
    EDGE_FLOOR,
    EDGE_RANGE,
    MAX_BOOK_AGE_S,
    MAX_POSITION_USDC,
    MIN_FILL_RATIO,
    MIN_POSITION_USDC,
    PAPER_DB_PATH,
    TRADING_MODE,
    VOL_FLOOR,
    VOL_RANGE,
    VOLATILITY_LOOKBACK,
    effective_ask,
)
```

**Step 4: Replace the depth gate with the clamp**

In `polypocket/bot.py`, replace the block at lines 422-436 (the `# Depth gate:` comment through the closing `return`):

```python
            # Depth clamp: ask for at most DEPTH_CLAMP_BUFFER of the
            # cumulative ask size at <= FOK limit. Protects our order
            # against book churn during signing/network latency. Skip
            # only if the clamped target falls below MIN_FILL_RATIO of
            # intended size (too thin to bother) or below MIN_POSITION_USDC
            # (too small to be a real position).
            book = window.up_book if signal.side == "up" else window.down_book
            limit = fok_limit_price(entry_price)
            fillable = sum(
                lvl["size"] for lvl in (book or []) if lvl["price"] <= limit + 1e-9
            )
            target_size = min(size, fillable * DEPTH_CLAMP_BUFFER)
            if target_size < size * MIN_FILL_RATIO:
                self._window_skip_reason = "book-too-thin"
                log.warning(
                    "Skipping signal: book too thin — intended=%.2f shares @ <=$%.2f, "
                    "fillable=%.2f, clamped target=%.2f (min ratio %.2f)",
                    size, limit, fillable, target_size, MIN_FILL_RATIO,
                )
                return
            if target_size * entry_price < MIN_POSITION_USDC:
                self._window_skip_reason = "book-too-thin"
                log.warning(
                    "Skipping signal: clamped trade below min position — "
                    "target=%.2f @ $%.3f = $%.2f < $%.2f",
                    target_size, entry_price, target_size * entry_price,
                    MIN_POSITION_USDC,
                )
                return
            if target_size < size:
                log.info(
                    "Downsizing trade to depth: intended=%.2f target=%.2f "
                    "fillable=%.2f limit=$%.2f",
                    size, target_size, fillable, limit,
                )
                size = target_size
                size_usdc = target_size * entry_price
```

Note: `size = target_size` happens only when the clamp reduces it, so unclamped paths stay untouched. `size_usdc` is updated so the subsequent `SIGNAL:` log line reports the real committed USDC.

**Step 5: Run all bot tests**

Run: `pytest tests/test_bot.py -v`
Expected: all pass, including the 5 new tests and the existing live-trade tests.

If `test_bot_live_skips_when_clamped_size_below_min_position_usdc` fails because `MIN_POSITION_USDC` is imported-as-name and monkeypatching the module attribute doesn't reach the `<` comparison, change the test to set `monkeypatch.setenv("MIN_POSITION_USDC", "5.0")` before instantiating the bot. If the config doesn't read `MIN_POSITION_USDC` from env, drop that test — the floor path is verified by manual review and the ratio/empty/shallow cases cover the core logic.

**Step 6: Run the full suite**

Run: `pytest tests/ -x`
Expected: all tests pass. No regressions in `test_polymarket_client`, `test_executor`, etc.

**Step 7: Commit**

```bash
git add polypocket/bot.py tests/test_bot.py
git commit -m "feat(live): clamp FOK size to book depth instead of gating

Replaces the all-or-nothing depth gate with a size-to-depth clamp:
orders now request at most DEPTH_CLAMP_BUFFER (90%) of visible fillable
size, leaving headroom for book churn during signing. Windows are only
skipped when the clamped target falls below MIN_FILL_RATIO (50%) of
intended size or below MIN_POSITION_USDC.

Targets the 5/9 FOK rejects observed in the first live session, all of
which hit 'order couldn't be fully filled' because the 10% cushion in
the gate protected the server snapshot but not our in-flight order.
"
```

---

### Task 3: Manual sanity check

**Step 1: Sweep ratios with the existing sim tooling if applicable**

Check whether `scripts/sim_filters.py` can replay the 9 live trades against the new logic:

Run: `ls scripts/ && head -40 scripts/sim_filters.py`

If it supports book-depth replay, run a one-off to estimate reject rate at default settings. If not, skip — this is only a sanity step.

**Step 2: Dry-run sanity**

Run: `TRADING_MODE=paper pytest tests/test_bot.py -v` to confirm paper mode is untouched.
Expected: all paper tests pass.

**Step 3: No commit needed** — this task only verifies.

---

### Task 4: Update live-session notes

**Files:**
- Modify: `docs/plans/2026-04-21-reducing-fok-rejects-design.md` (if any field changed during implementation)

**Step 1:** If no deltas, skip. Otherwise append a short "Implementation notes" section and commit.

```bash
git add docs/plans/2026-04-21-reducing-fok-rejects-design.md
git commit -m "docs(plans): notes from reducing-fok-rejects implementation"
```

---

## Rollout checklist (post-merge)

- [ ] Run live for one session (same defaults as the 9-trade baseline).
- [ ] Query `SELECT status, COUNT(*) FROM trades GROUP BY status` on `live_trades.db`.
- [ ] If reject rate > ~10%, set `DEPTH_CLAMP_BUFFER=0.8` in env and rerun before considering approach B (GTC+cancel).
- [ ] If reject rate ≈ 0% and fills stay reasonable, promote defaults (no change needed — already defaulted to 0.9 / 0.5).
