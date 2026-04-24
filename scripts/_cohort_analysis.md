# Buffer=8 cohort analysis

**Generated:** 2026-04-24T02:11:45Z
**Cohort start:** 2026-04-23T18:07:56
**Config:** IOC_BUFFER_TICKS=8, SIGNAL_CUSHION_TICKS=11 (all other knobs frozen)

## Slip distribution

| metric | buffer=15 baseline | buffer=8 cohort | delta |
|---|---|---|---|
| n | 59 | 19 | — |
| median (ticks) | 11.6 | 6 | -5.6 |
| mean (ticks) | 10.5 | 6.79 | — |
| min / max | — | 2 / 15 | — |
| p25 / p75 | — | 4 / 8 | — |

## Reject rate

- Attempts: 36 (fills 19 + rejects 17)
- Reject rate: 47.2%

## Cohort PnL

- Total: -15.20
- Avg/trade: -0.800

## Verdict

**SHIP**

Median slip is at or below the 6-tick threshold. Update `polypocket/config.py` defaults (`IOC_BUFFER_TICKS=8`, `SIGNAL_CUSHION_TICKS` = cohort median slip), refresh the calibration comment blocks, and run a follow-up gate-only sweep on the combined 79-fill corpus.
