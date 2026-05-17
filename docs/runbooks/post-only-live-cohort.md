# Post-only live cohort — runbook

**Last updated:** 2026-05-16 (after Step-9 probe landed; GTD + 60s expiration
fixes shipped in `b58fab2`).

This runbook drives the small-cohort live validation of the post-only
entry path. Target: 50–100 trades at `MIN_POSITION_USDC = $5` to compare
live-vs-paper calibration on the DOWN side (the structural gap from the
2026-05-15 v2-execution-seam diagnostic).

The Step-9 probe is already done. This runbook covers the cohort run +
analysis only.

## Pre-flight

1. **Branch + commits up to date.** `feat/post-only-entries` HEAD should be
   `b58fab2` or later (GTD + expiration-floor fixes from the live probe).
2. **`.env` ready:**
   - `TRADING_MODE=live`
   - `ENTRY_MODE=post_only`
   - `MIN_POSITION_USDC=5.0`, `MAX_POSITION_USDC=5.0` (the cohort sizes
     every trade at the floor — keeps cohort cost bounded at $250–$500
     of capital churn, most of it ROI-neutral)
   - All five CLOB env vars set: `PRIVATE_KEY`, `CLOB_API_KEY`,
     `CLOB_SECRET`, `CLOB_PASSPHRASE`, `PROXY_ADDRESS`
3. **Funder balance check:**
   ```powershell
   python -c "
   import os; os.environ.setdefault('TRADING_MODE','live')
   from dotenv import load_dotenv; load_dotenv('.env', override=True)
   from polypocket import config
   from polypocket.clients.polymarket import PolymarketClient
   c = PolymarketClient(host=config.POLYMARKET_HOST, chain_id=config.CHAIN_ID,
       private_key=os.environ['PRIVATE_KEY'],
       api_creds={'key': os.environ['CLOB_API_KEY'],
                  'secret': os.environ['CLOB_SECRET'],
                  'passphrase': os.environ['CLOB_PASSPHRASE']},
       proxy_address=os.environ['PROXY_ADDRESS'], dry_run=False)
   print('pUSD balance:', c.get_usdc_balance())
   "
   ```
   Need at minimum `2 * MIN_POSITION_USDC` (~$10) for two concurrent trades.
   For the full cohort budget on ~$5/trade and possibly stranded shares,
   $30+ is comfortable.
4. **Existing `live_trades.db`:** the cohort writes to whatever the bot
   uses (defaults to `live_trades.db` in the repo root via
   `LIVE_DB_PATH`). If you want a clean cohort DB, set `--db
   live_trades_post_only_cohort.db` on the start command.

## Start the bot

From a terminal you can leave open for hours (NOT inside Claude's session
— the agent's background tasks don't persist across sessions):

```powershell
python -m polypocket run
```

Or with a cohort-specific DB:

```powershell
python -m polypocket run --db live_trades_post_only_cohort.db
```

Tail the log to confirm the first place lands cleanly:

- Look for `post-only PLACED: btc-updown-5m-... up rest=$0.5X x5.0 exp=...`
  in the bot log — that's the executor confirming a successful place.
- Confirm `/order` records the order via `_post_only_replay.md`-style
  inspection (`scripts/replay_post_only_paper.py`-equivalent for live is
  a follow-up — for now, eyeball the `order_events` table for the
  first few trades).

If the first place returns `error='post-only-would-cross'` repeatedly,
the offset is too aggressive — stop the bot and bump
`POST_ONLY_REST_OFFSET_TICKS` from the default 2 → 3 in `.env` and
restart.

## Halt / stop the cohort

Two mechanisms:

**A. Kill file (graceful):**
```powershell
New-Item -ItemType File .cohort_stop -Force | Out-Null
```
The bot's `_on_book_update` checks `cohort_stop_requested()` at the top
and returns early. New trades stop; in-flight 'placed' orders continue
on their normal cancel-on-tick / window-end / server-expiration path.

**B. Ctrl-C (signal):** in the terminal running the bot. The bot's
`asyncio.CancelledError` handler runs cleanup; any 'placed' rows are
left to the next startup's `find_unsettled_trades` + cancel-reconcile
path (shipped in `000bf15`).

Don't forget to remove `.cohort_stop` after the cohort:
```powershell
Remove-Item .cohort_stop -ErrorAction SilentlyContinue
```

## Per-trade halt criteria

Stop the cohort early if any of:

- **Server consistently rejects placement.** Three consecutive
  `error='post-only-would-cross'` rejects in adjacent windows suggests
  the offset is misaligned with current book dynamics.
- **Server returns an error shape `_classify_post_only_cross_error`
  does not recognize.** Surface as a network: error in the trade row —
  investigate before continuing.
- **Cancel-race partial fill drift.** If multiple trades land with
  `entry_mode='post_only'` but `size` much smaller than intended (dust
  fills via the cancel-reconcile path), the offset may be too
  aggressive — consider raising it.
- **PnL drift exceeds the existing cohort guard** (`MAX_DAILY_LOSS = $15`
  per `polypocket/config.py`). The bot self-halts.

## Mid-cohort progress check

While the bot runs, inspect progress without disturbing it:

```powershell
python -c "
import sqlite3
db = 'live_trades.db'  # or your --db override
with sqlite3.connect(db) as c:
    c.row_factory = sqlite3.Row
    rows = c.execute(
        \"SELECT status, entry_mode, COUNT(*) n FROM trades GROUP BY status, entry_mode\"
    ).fetchall()
    for r in rows: print(dict(r))
"
```

Look for: `entry_mode='post_only'` rows with `status` in
`{placed, open, settled, rejected}`.

**Fill rate sanity check** mid-cohort:
- `placed → settled` (via open) = fills.
- `placed → rejected` with `error='post-only-no-fill'` = the resting
  order timed out without a counterparty crossing.
- Post-hoc paper replay (`scripts/_post_only_replay.md`) reported 28.9%
  fill rate at offset=2 — live should land at-or-above that since live
  observes intra-30s fills the replay misses.

## Cohort analysis (after halt)

Once n=50–100 has accumulated:

```powershell
python -m polypocket.scripts.model_health  # or equivalent
# (live calibration split by entry_mode is a planned follow-up; for
# now, the cohort_id can be derived from window_slug timestamp +
# trade.entry_mode='post_only')
```

The key questions for the GO/NO-GO decision on flipping `ENTRY_MODE`
default:

1. **DOWN-side calibration gap closure.** Diagnostic showed live FAK
   was -11.7pt UNDER paper on DOWN. Did post-only close to within ±3pt?
2. **Fill rate vs replay.** Live should be at-or-above the
   28.9% replay number. A live rate materially *below* the replay
   means our presence on the book changes other actors' behavior.
3. **Realized entry vs rest_price.** Should be approximately zero
   (`entry_price ≈ rest_price` since post-only fills only at rest).
4. **No adverse-selection blowup.** Pre-flight expectation is some
   adverse selection (the design's chief acknowledged trade-off). If
   the cohort PnL is materially worse than the FAK cohort at the same
   model+gate config, that's a flag for the v2 enhancement
   (cancel-and-repost on book-moves).

## Rollback

If the cohort surfaces a blocker (any halt criterion above, or the
final analysis fails GO):

1. Set `ENTRY_MODE=fak` back in `.env`.
2. Restart the bot — FAK path is bit-identical to pre-PR (guarded by
   `test_paper_path_unchanged_with_post_only_config_set`).
3. Open a follow-up issue with the cohort findings + decision-quality
   data attached.

## Server-side constraints to remember

From the Step-9 probe on 2026-05-16:

- **`expiration` units are Unix seconds**, NOT milliseconds.
- **`expiration` minimum is `now + 60s`.** The bot floors via
  `POLYMARKET_MIN_EXPIRATION_BUFFER_S = 65`.
- **`OrderType.GTC` + non-zero `expiration` is rejected.** Use
  `OrderType.GTD` when expiration > 0 — `submit_post_only` handles
  this automatically.
- **Post-only-would-cross error shape:** HTTP 400 with
  `{"error": "invalid post-only order: order crosses book"}`.
- **/order status field is uppercase:** `LIVE`, `MATCHED`, `CANCELED`.
  The reconciler lowercases via `.strip().lower()` before matching.
