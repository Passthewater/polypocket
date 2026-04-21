# Live Trading MVP — Design

**Date:** 2026-04-21
**Issue:** #3 — Live trading not wired up — hard blockers before real-money run
**Scope:** MVP path to a supervised $20 sanity test. Defers full acceptance criteria to follow-up issues.

## Scope

This design covers **blockers #1, #2, #3, #6** from issue #3:

- #1 — Concrete `LiveOrderClient` implementation against Polymarket CLOB.
- #2 — `__main__.py` constructs the client when `TRADING_MODE=live`, with `--db` flag.
- #3 — Fill verification in `execute_live_trade`; rejected orders don't leave an open position.
- #6 — Pre-submit USDC balance check.

**Deferred to follow-up issues** (filed at commit time):

- **A** — Live PnL / payout reconciliation (`settle_live_trade` queries Polymarket) → #4
- **B** — Startup order-status reconciliation against CLOB → #5
- **C** — `RiskManager` consumes live PnL alongside paper PnL → #6
- **D** — Integration test matrix (fill / reject / partial-fill / restart-mid-order) → #7
- **E** — Remove `LIVE_MAX_TRADES_PER_SESSION` stopgap once A+C land → #8

Rationale for the split: shipping #1-3 + #6 in a reviewable chunk is lower-risk than a sprawling PR that also wires reconciliation. The first live run is manually supervised either way.

## Account / auth context

Polymarket account was created via Google OAuth and funded by Interac e-transfer. This is the **L2 proxy-wallet** path: Polymarket manages a Gnosis Safe proxy wallet on Polygon that holds USDC, with a user-owned EOA as its signer. Implementation must use `py-clob-client`'s L2 signing (signature type `POLY_PROXY` or `POLY_GNOSIS_SAFE`). Balance and order queries target the proxy address, not the signer EOA.

Required env vars (live mode):

- `PRIVATE_KEY` — signer EOA private key (from Polymarket "Export private key").
- `CLOB_API_KEY` / `CLOB_SECRET` / `CLOB_PASSPHRASE` — L2 API creds, derived once from `PRIVATE_KEY` via helper script.
- `PROXY_ADDRESS` — Polymarket proxy wallet address (funder).

## Architecture

```
polypocket/
  clients/
    __init__.py
    polymarket.py        # NEW — PolymarketClient, FillResult
  executor.py            # CHANGED — execute_live_trade consumes FillResult + balance pre-check
  __main__.py            # CHANGED — wires client for live, --db flag, --dry-run
  config.py              # CHANGED — LIVE_DB_PATH, LIVE_MAX_TRADES_PER_SESSION
scripts/
  derive_clob_creds.py   # NEW — one-shot L2 cred derivation helper
tests/
  test_polymarket_client.py  # NEW
  test_executor.py           # CHANGED — new live-path cases
  test_bot.py                # CHANGED — live-mode construction
.env.example             # CHANGED — add CLOB_API_KEY / SECRET / PASSPHRASE / PROXY_ADDRESS
```

**Boundary rule:** only `clients/polymarket.py` and `scripts/derive_clob_creds.py` import `py-clob-client`. `executor.py`, `bot.py`, and tests see only our own `PolymarketClient` class and `FillResult` dataclass. The existing `LiveOrderClient` Protocol in `executor.py` stays as the structural type for mocking; its signature updates to match.

**Live DB default:** `__main__.py` defaults to `live_trades.db` when `TRADING_MODE=live` and `paper_trades.db` otherwise. `--db PATH` overrides.

## Components

### `polypocket/clients/polymarket.py`

```python
@dataclass(frozen=True)
class FillResult:
    status: Literal["filled", "rejected", "error"]
    order_id: str | None
    filled_size: float
    avg_price: float | None
    error: str | None

class PolymarketClient:
    def __init__(self, host, chain_id, private_key, api_creds, proxy_address, dry_run=False): ...
    def submit_fok(self, side, price, size, token_id, client_order_id) -> FillResult: ...
    def get_usdc_balance(self) -> float: ...
    def get_order_status(self, order_id) -> dict: ...  # for follow-up B
```

- `submit_fok` builds `OrderArgs(price, size, side, token_id)` with `OrderType.FOK`, POSTs, and on `success=True` calls `get_order(order_id)` once to read confirmed status and filled size.
- On `dry_run=True`, signs but does not POST; returns `FillResult(status="filled", order_id="DRY-RUN", filled_size=size, avg_price=price, error=None)` so the downstream flow is exercised end-to-end.
- `get_usdc_balance` queries the proxy wallet, not the signer EOA.

### `polypocket/executor.py` — updated `execute_live_trade`

New signature:

```python
def execute_live_trade(db_path, signal, entry_price, size, window_slug,
                       token_id: str, client: LiveOrderClient) -> TradeResult: ...
```

Flow:

1. `client.get_usdc_balance() >= entry_price * size + fee_buffer` — else `TradeResult(success=False, error="insufficient-balance")`, no DB row.
2. `log_trade(..., status="reserved")`.
3. `fill = client.submit_fok(...)`.
4. `fill.status == "filled"` → `update_trade(status="open", external_order_id=fill.order_id)`, return success.
5. `fill.status in ("rejected", "error")` → `update_trade(status="rejected", error=fill.error)`, return `TradeResult(success=False, error=fill.error)`.

The `LiveOrderClient` Protocol updates to match `PolymarketClient.submit_fok`.

### `polypocket/__main__.py` — `run` command

```
python -m polypocket run [--db PATH] [--dry-run]
```

- Default db: `live_trades.db` if `TRADING_MODE=live`, else `paper_trades.db`.
- Live mode: construct `PolymarketClient` from env, pass to `Bot(live_order_client=...)`. Honor `--dry-run`.
- Paper mode: flags ignored.

### Startup validation (live mode)

Before `Bot.run()`, `__main__.py` validates:

- `PRIVATE_KEY`, `CLOB_API_KEY`, `CLOB_SECRET`, `CLOB_PASSPHRASE`, `PROXY_ADDRESS` non-empty.
- `client.get_usdc_balance()` succeeds (proves creds + network + proxy address).
- Balance ≥ `MIN_POSITION_USDC`.

Any failure → log and `sys.exit(1)` before the event loop starts.

### `scripts/derive_clob_creds.py`

~20 lines. Reads `PRIVATE_KEY` from `.env`, calls `ClobClient.create_or_derive_api_creds()`, prints the three values ready to paste into `.env`. One-shot. Never invoked by the bot.

### Ledger schema additions

Idempotent `ALTER TABLE` in `init_db`:

- `trades.external_order_id TEXT`
- `trades.error TEXT`

Both nullable. Existing paper-mode rows remain valid.

### Token-id threading

`submit_fok` needs the Polymarket CLOB `token_id` for the YES/NO outcome. `signal.side` is `"up"`/`"down"`; `Window` dataclass needs `up_token_id` / `down_token_id` fields (verify — implementation plan includes a verification step and adds them to the feed layer if missing). `bot.py` resolves side → token_id and forwards it through `execute_live_trade` to `submit_fok`.

## Data flow

**Happy path:**

```
signal fires
  → bot._execute_trade()
  → resolve side → token_id
  → execute_live_trade(..., token_id, client)
      → client.get_usdc_balance()            [pre-check]
      → log_trade(status="reserved")
      → client.submit_fok(...)
          → py-clob-client POST + one get_order() read
          → FillResult(status="filled", order_id, ...)
      → update_trade(status="open", external_order_id=...)
      → TradeResult(success=True)
  → bot sets _open_trade, _window_traded=True
```

**Reject path:** `FillResult.status="rejected"` → `update_trade(status="rejected", error=...)` → `_open_trade` stays None; window remains retryable.

**Balance-insufficient:** Pre-check fails → no DB row → error surfaced. Bot marks window non-retryable for the session to avoid hammering the balance endpoint.

**Network error:** `py-clob-client` raises → caught in `PolymarketClient.submit_fok`, returned as `FillResult(status="error")`. Treated like reject. No retry in MVP.

**Settlement (unchanged for MVP):** `settle_live_trade` writes `pnl=None, status="settled"` on Chainlink resolution. Real payout reconciliation = follow-up #A.

**Startup with `reserved` row:** existing bot recovery logic at `bot.py:155-175` unchanged. Manual operator reconciliation in MVP. Automated = follow-up #B.

**Dry-run:** synthetic `FillResult` without POST. Trade row still written to `live_trades.db` with `external_order_id="DRY-RUN"` for visual distinguishability. Balance check runs for real (proves creds).

## Error handling

**Exception taxonomy in `PolymarketClient.submit_fok`:**

- Network / HTTP → `FillResult(status="error", error="network: ...")`.
- Auth / signing → `FillResult(status="error", error="auth: ...")` (should be impossible after startup validation).
- Unexpected → re-raised; top-level bot handler logs and exits.

**Risk-manager weakness (MVP-only):** `RiskManager.check()` reads `get_daily_pnl` which returns 0 for live rows (`pnl=None`). `MAX_DAILY_LOSS` is effectively disabled for live runs until follow-up #C. Compensating controls:

- `MAX_CONSECUTIVE_LOSSES` tracking continues to work via `record_loss` on observed Chainlink outcomes — same inference paper uses today.
- New hard cap `LIVE_MAX_TRADES_PER_SESSION` (default 10) in `config.py`, enforced in the bot, bounds downside.

**Logging:** every `submit_fok` call logs structured fields — `window_slug, side, price, size, token_id, client_order_id, result_status, order_id, fill_price, error`.

## Testing

### Unit tests (in CI)

- `tests/test_polymarket_client.py` (new). Mock `py_clob_client.ClobClient`. Cases: filled, rejected, network exception, dry-run, balance query targets proxy.
- `tests/test_executor.py` (extended). `execute_live_trade`: insufficient balance, fill, reject, duplicate-window.
- `tests/test_bot.py` (extended). Live mode without client → `RuntimeError`; with client → first signal calls `client.submit_fok` with expected args.

### Manual verification (not in CI, documented in release notes)

1. `python scripts/derive_clob_creds.py` → paste output into `.env`.
2. `TRADING_MODE=live python -m polypocket run --dry-run` → confirm startup validation passes, balance reads, first signal logs a `DRY-RUN` order row.
3. `TRADING_MODE=live python -m polypocket run` with `MAX_POSITION_USDC=5.0` and `LIVE_MAX_TRADES_PER_SESSION=3` — supervised first real run. Hand-monitor.

### Out of scope (→ follow-up #D)

Integration test matrix covering fill / reject / partial-fill / restart-mid-order against a fully-mocked CLOB.

## Follow-up issues filed at commit time

- **A — Live PnL reconciliation** (#4). `settle_live_trade` queries Polymarket for actual payout / fees / shares; writes real `pnl`.
- **B — Startup order reconciliation** (#5). On recovering `reserved`/`open` rows, call `client.get_order_status(external_order_id)` and resolve before resuming.
- **C — RiskManager consumes live PnL** (#6). Unify `MAX_DAILY_LOSS` gate across paper + live.
- **D — Integration test matrix** (#7). Mocked-CLOB coverage: fill, reject, partial-fill, restart-mid-order.
- **E — Remove `LIVE_MAX_TRADES_PER_SESSION` stopgap** (#8) once A + C land.

Each references issue #3 and links back to this doc.
