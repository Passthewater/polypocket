# Signal-reference backfill log

**Date:** 2026-05-11
**SIGNAL_CUSHION_TICKS at backfill time:** 8
**Task 0 baseline (paper):**
  MAX(trades.id) = 1376
  COUNT(settled trades) = 1329
  bids-JSON-populated decision rows = 1374 / 4997

## Paper DB
| source | count |
| --- | --- |
| exact | 1375 |
| approximate | 2 |
| missing | 0 |
| skipped (already tagged) | 0 |

## Live DB
| source | count |
| --- | --- |
| exact | 130 |
| approximate | 87 |
| missing | 0 |
| skipped (already tagged) | 0 |

## Notes
- Paper trades come overwhelmingly from windows where bids were populated at
  decision time, so the design's predicted ~70% approximate share applies to
  the full decision corpus (4997 rows), not the firing subset (~1377 trades).
  Approximate trades on paper are just 2 / 1377 (0.15%).
- Live ledger has ~40% approximate rows — these predate the bids-JSON capture
  fix and use ask-fallback. Excluded from headline aggregates per design.
- A re-run is a no-op (skipped count == row count).
- Task 10 Step 4's forward-soak boundary is MAX(trades.id) = 1376.
