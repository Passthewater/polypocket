---
project_name: 'polypocket'
user_name: 'matt'
date: '2026-04-23'
sections_completed:
  ['technology_stack', 'language_rules', 'framework_rules', 'testing_rules', 'quality_rules', 'workflow_rules', 'anti_patterns']
status: 'complete'
rule_count: 58
optimized_for_llm: true
---

# Project Context for AI Agents

_This file contains critical rules and patterns that AI agents must follow when implementing code in this project. Focus on unobvious details that agents might otherwise miss._

**What polypocket is:** a directional 5-minute BTC prediction bot for Polymarket's binary Up/Down markets. Three async feeds (Binance spot via `ccxt.pro`, Polymarket `/events` REST, CLOB book WS) converge into a Brownian-motion signal model; paper and live trades persist to separate SQLite ledgers.

---

## Technology Stack & Versions

**Runtime:** Python ≥3.11 (uses PEP 604 `int | None` union syntax throughout).

**Core dependencies** (from `pyproject.toml`):
- `ccxt>=4.0.0` — Binance WebSocket price feed (uses `ccxt.pro`).
- `py-clob-client==0.19.0` — Polymarket CLOB REST. **Pinned exact** — the client's signing/creds contract has changed across minor versions; do not bump without a scripted re-derivation of CLOB creds.
- `websockets>=12.0` — raw Polymarket book WS (not via py-clob-client).
- `scipy>=1.11.0`, `numpy>=1.24.0` — signal model (`scipy.stats.norm.cdf`, rolling vol).
- `textual>=3.0.0` — runtime TUI; some keybinds mutate `polypocket.config` at runtime.
- `aiohttp>=3.9.0` — Polymarket `/events`, `/trades`, `/order` REST.
- `python-dotenv>=1.0.0` — `.env` loading at config import (see testing rules).
- **Stdlib:** `sqlite3` (ledger), `asyncio` (orchestration), `logging`.

**Dev dependencies:** `pytest`, `pytest-asyncio`. No lockfile — `pyproject.toml` is authoritative.

**Build:** `setuptools>=68.0`. No `uv.lock`, no `poetry.lock`, no `requirements.txt`.

**Lint/format:** `ruff` cache present (`.ruff_cache/`), no explicit config file — defaults apply.

**No TypeScript / Node / frontend bundler.** TUI is Python (textual).

---

## Critical Implementation Rules

### Language-Specific Rules (Python)

- **Python 3.11+ syntax:** use PEP 604 unions (`int | None`) and modern typing, not `Optional[int]` or `Union[...]`.
- **Async everywhere in the hot path:** `bot.py` is an asyncio orchestrator. Feeds, CLOB client, executor all run on the same loop. No blocking I/O (`requests`, sync network reads) inside the run loop — use `aiohttp` or offload with `asyncio.to_thread`.
- **Dataclasses for value objects, `Protocol` for executable seams:**
  - `@dataclass(frozen=True)` for results that cross boundaries (`FillResult`, `SettlementInfo`, `QuoteSnapshot`, `Signal`).
  - `typing.Protocol` for anything tests stub — see `LiveOrderClient` in `executor.py`. Adding a method requires updating the Protocol **and** every impl/stub; signatures must match exactly.
- **Module docstring on every file:** one-line `"""..."""` as line 1.
- **Logging:** `import logging; log = logging.getLogger(__name__)` at module top. Never `print()` from library code. Scripts under `scripts/` may print to stdout.
- **Config = module-level constants, env-overridable:** tunable values live in `polypocket/config.py` as `X = int(os.getenv("X", "default"))` (or `float(...)`). Never call `os.getenv` from hot-path code — import the constant.
- **Prefer `from polypocket.config import X`** over `import polypocket.config as cfg` — runtime TUI mutation targets the imported names the caller holds.
- **SQLite discipline:** wrap with `contextlib.closing(sqlite3.connect(path))`, explicit `conn.execute("BEGIN")` / `conn.commit()`, `CREATE TABLE IF NOT EXISTS` (idempotent). Do not hold a connection across `await`.
- **Imports ordered:** stdlib → third-party → `polypocket.*`, blank-line separated. **No relative imports** — always `from polypocket.executor import ...`.

### Framework-Specific Rules

**Polymarket CLOB (`py-clob-client` + raw WS):**
- **`priceToBeat` is the resolution baseline, NOT Binance.** All displacement maths (`d = (P_now − P_open) / P_open`) must use `eventMetadata.priceToBeat` from the Polymarket `/events` payload. Binance is the fast *current* feed only. Using a Binance-derived open models the wrong target variable.
- **Pair-merge pricing for binary books:** a BUY UP clears against the DOWN side — implied entry price is `1 − best_down_bid`, not `up_ask`. `SIGNAL_CUSHION_TICKS` gates on the pair-merge price; `IOC_BUFFER_TICKS` is the live-order taker buffer. They are not interchangeable.
- **CLOB creds derived once, not signed per call.** `PRIVATE_KEY` + `PROXY_ADDRESS` feed `scripts/derive_clob_creds.py` → `CLOB_API_KEY/SECRET/PASSPHRASE`. Never re-derive live.
- **`sig_type=1` (Google-OAuth accounts):** the funder address is **not** the EOA and **not** the Profile→Deposit proxy. Allowances are pre-maxed by Polymarket — do not issue new approvals.
- **`/order` and `/trades` race:** `/order` can return a null body while the fill is real. Harden any new call paths with the same retry/reconciliation pattern already in `clients/polymarket.py` and the stranded-fill sweep in `executor.py`.
- **Book freshness gate:** a signal is unsafe to act on if the last book event is older than `MAX_BOOK_AGE_S`. New code paths that consume the book must check staleness.

**asyncio / bot orchestrator:**
- **One window = one decision.** `bot.py` iterates 5-minute windows; entry decisions gate on `WINDOW_ENTRY_MIN_ELAPSED` and `WINDOW_ENTRY_MIN_REMAINING`. Do not place trades outside that band.
- **Kill-file pattern:** `.cohort_stop` at repo root halts trading on the next tick. Anchored to repo root (parent of `polypocket/`) so systemd/cwd-independent. Honor it.
- **Paper vs live are separate code paths:** `execute_paper_trade` / `settle_paper_trade` vs `execute_live_trade` / `settle_live_trade`, separate DB files (`PAPER_DB_PATH`, `LIVE_DB_PATH`), gated by `TRADING_MODE`. Don't cross the streams.

**Textual TUI:**
- Keybinds may mutate `polypocket.config` at runtime. Treat config constants as live references, not snapshots.

### Testing Rules

- **Runner:** `pytest` + `pytest-asyncio`. 234 tests across 19 files live in `tests/`.
- **Test env isolation is mandatory.** `tests/conftest.py` stubs `dotenv.load_dotenv` to a no-op **before** `polypocket.config` imports, then `pop`s a named env-key list. **Any new env-backed constant added to `config.py` MUST be added to the `_key` tuple in `tests/conftest.py`** — otherwise a developer's `.env` will leak into CI.
- **Mirror the module layout:** one test file per source module, named `test_<module>.py`. Test functions are `def test_...` or `async def test_...` (marked with `@pytest.mark.asyncio`).
- **Test the Protocol, not the class.** `LiveOrderClient` is a `Protocol`; stubs in `test_executor.py` / `test_bot.py` implement it structurally. New executor code should accept the Protocol, not the concrete `PolymarketClient`.
- **Integration test for live-adjacent paths:** anything touching the CLOB has a focused test in `test_polymarket_client.py` (47 cases). Don't ship CLOB changes without one.
- **No real network / no real DB in tests.** Use in-memory SQLite (`":memory:"`) or tmp paths; stub feeds and clients.

### Code Quality & Style Rules

- **Naming:** `snake_case` for functions/modules/vars; `PascalCase` for classes and dataclasses; `UPPER_SNAKE` for module-level constants.
- **File layout:** package code in `polypocket/`, subpackages `polypocket/clients/` and `polypocket/feeds/`. Operational/one-off tooling in `scripts/`. Never put runtime code under `scripts/`.
- **Comment discipline for tuning constants:** every numerical threshold in `config.py` carries the *why* — cohort sample size (`n=218`), issue number (`#11`, `#12`, `#13`, `#14`), date of calibration (`2026-04-23`), and PnL/slip impact. **Preserve this context when editing**; do not strip comments to "clean up." When adding a new tuning constant, follow the same format.
- **No `print()` in committed library code.** Use `log.debug/info/warning/error`.
- **Paths:** anchor repo-root paths to `Path(__file__).resolve().parent.parent` (see `bot.py`'s `COHORT_STOP_FILE`), never `Path.cwd()` — the bot runs under systemd and scripts from arbitrary cwds.
- **Currency math:** size in USDC is `float`; share count derives from size/price. Fees follow `size * FEE_RATE * p * (1 − p)` (peaks at p=0.50). Use `config.fee_shares()` and `config.effective_ask()`; don't redo the algebra inline.

### Development Workflow Rules

- **Planning convention:** new features get paired docs under `docs/plans/YYYY-MM-DD-<slug>-design.md` + `docs/plans/YYYY-MM-DD-<slug>-implementation.md`. Plans are written for in-chat linear execution (no parallel subagent dispatch).
- **Commit style — Conventional Commits with scope:**
  - `feat(<scope>): ...` — new behavior. Scopes seen in history: `executor`, `signal`, `client`, `bot`.
  - `chore(<scope>): ...` — ops/tooling, e.g. `chore(scripts): ...`.
  - `fix(<scope>): ...`, `docs: ...`.
  - Body references issue numbers (`#11`, `#13`) where applicable.
- **Branches:** default working branch is `main` (single-author repo; no long-lived feature branches in history).
- **Databases are not source of truth for review:** `*.db` files are git-ignored. `*.bak.db` snapshots at decision boundaries (e.g. `paper_trades.pre-feefix.bak.db`) are kept as versioned reference points — don't delete them without confirming.
- **No secrets committed:** `.env` is gitignored; only `.env.example` is tracked. `PRIVATE_KEY`, `CLOB_SECRET`, `CLOB_PASSPHRASE` must never appear in code, tests, or logs.

### Critical Don't-Miss Rules

**Anti-patterns (do NOT do these):**
- **Do not use `up_ask` as the UP entry price.** Use `1 − best_down_bid` (pair-merge). See signal gate logic in `signal.py`.
- **Do not bypass `MAX_ENTRY_PRICE` (0.70) or `MAX_EDGE_THRESHOLD_UP` (0.25).** Both are empirically calibrated on live PnL; raising them without a new cohort is curve-fitting backward.
- **Do not add a new `os.getenv` at the call site.** Thread it through `polypocket/config.py` so the TUI and tests see it.
- **Do not bump `py-clob-client` past `0.19.0`** without re-running `scripts/derive_clob_creds.py` in a staging flow and re-validating the sig_type=1 path.
- **Do not add new env-backed config constants without updating `tests/conftest.py`'s `_key` tuple** — this is the single most common silent bug vector.
- **Do not trust a null `/order` response.** Always reconcile against `/trades` — the stranded-fill sweep in `executor.py` exists because this race is real.
- **Do not commit DB files or `.env`.** Both are gitignored; re-adding requires a deliberate override.
- **Do not place trades outside the entry window** (`WINDOW_ENTRY_MIN_ELAPSED` ≤ elapsed ≤ window − `WINDOW_ENTRY_MIN_REMAINING`). The latency-arb thesis breaks outside that band.

**Edge cases worth handling:**
- **Stale book:** reject the signal if `now − last_book_event > MAX_BOOK_AGE_S` (default 3.0s).
- **Thin book:** depth-clamp the target size to `DEPTH_CLAMP_BUFFER * visible_fillable` and skip the window with reason `"book-too-thin"` if the clamped target < `intended * MIN_FILL_RATIO`.
- **Cohort kill-file:** check `.cohort_stop` on every tick; halt cleanly, do not crash.
- **Resolution race:** settlement may arrive before the executor marks the trade settled — `reconcile_recovered_trade` handles this; new settlement paths must too.
- **DOWN-side shrinkage:** `CALIBRATION_SHRINKAGE_DOWN=0.50` ≠ `_UP=1.00`. Asymmetry is intentional (see `config.py` comment). Do not symmetrize.

**Security rules:**
- **Funder ≠ EOA ≠ Proxy** for sig_type=1 accounts. Never conflate in auth flows.
- **Never log private keys, CLOB secrets, or passphrases.** Redact before `log.debug`.
- **Allowances are pre-maxed; do not issue approvals from this codebase.**

**Performance gotchas:**
- **Don't re-fetch `priceToBeat` per tick** — it's fixed per window. Cache at window open.
- **Rolling vol has a lookback of 50 windows (~4h).** Re-computing from scratch each tick is wasteful; the `observer.compute_realized_vol` path is incremental — respect it.
- **`scipy.stats.norm.cdf` is cheap but not free** — one call per signal eval, not per book update.

---

## Usage Guidelines

**For AI Agents:**
- Read this file before implementing any code in this repo.
- Follow ALL rules exactly as documented; when in doubt, prefer the more restrictive option.
- When adding env-backed config constants, update `tests/conftest.py`'s `_key` tuple in the same change.
- When editing tuning constants in `config.py`, preserve the rationale comment (cohort n, issue #, PnL impact).
- Propose updates to this file if a new pattern emerges that agents would otherwise miss.

**For Humans:**
- Keep this file lean and focused on agent-actionable rules.
- Update when the tech stack or a load-bearing constant changes.
- Review after each epic / major calibration run; remove rules that have become obvious.

**Last Updated:** 2026-04-23
