# PnL attribution reference trades (regression artifact)

For each trade, the math is recomputed by hand using the algebra in
`docs/plans/2026-05-11-pnl-attribution-design.md`. `realized_pnl` is taken
directly from `trades.pnl` (not recomputed). These serve as a regression
artifact: if `attribution.py` ever drifts from the design, re-running it
against these trades should reproduce these numbers to `abs_tol=1e-6`.

Picked from `paper_trades.db` on 2026-05-11; provenance `exact` on all three.
All trades selected from rows where `signal_reference_source IN ('exact','live')`
so the gate-reference price is the pair-merge clearing — not the snapshot ask
fallback.

Component formulas (`side`-aligned; for `down` rows, substitute
`model_p_for_side = 1 - model_p_up`):

```
edge_value         = size * (model_p_for_side - signal_reference_price)
slip_value         = size * (signal_reference_price - entry_price)
expected_fee_value = -fee_shares(size, entry_price) * model_p_for_side
luck_value         = realized_pnl - (edge_value + slip_value + expected_fee_value)
```

Fees are recomputed internally by `config.fee_shares(size, entry_price)` from
realized `size`/`entry_price` — `trades.fees` (the intended fee) is not read.

## Trade 1 — favorable-slip UP win (id=777, slug=btc-updown-5m-1777957500)

| field | value |
| --- | --- |
| side | up |
| size | 17.0894 |
| entry_price | 0.26 |
| signal_reference_price | 0.59 |
| model_p_up | 0.786311 |
| outcome | up (won) |
| trades.pnl (authoritative) | +12.409426 |

- model_p_for_side = 0.786311
- fees = fee_shares(17.0894, 0.26) ≈ 0.236302
- edge_value = 17.0894 × (0.786311 − 0.59) = +3.354847
- slip_value = 17.0894 × (0.59 − 0.26) = +5.639505
- expected_fee_value = −0.236302 × 0.786311 = −0.186148
- luck_value = 12.409426 − (3.354847 + 5.639505 − 0.186148) = +3.601223
- **Sum check:** +3.354847 + 5.639505 − 0.186148 + 3.601223 = +12.409427 ✓ (1e-6 OK)

Reading: most PnL came from `slip` (paper-fill at 0.26 vs gate-ref 0.59). Edge
was real (model said 0.786 vs ref 0.59) but smaller in dollar terms than the
slip windfall. `luck` is positive — won at p=0.786, so the residual
`(size − fees) × (1 − 0.786)` ≈ +3.60 is the expected-loss-that-didn't-happen.

## Trade 2 — clean DOWN win (id=68, slug=btc-updown-5m-1777116300)

| field | value |
| --- | --- |
| side | down |
| size | 6.5217 |
| entry_price | 0.46 |
| signal_reference_price | 0.53 |
| model_p_up | 0.375146 |
| outcome | down (won) |
| trades.pnl (authoritative) | +3.405099 |

- model_p_for_side = 1 − 0.375146 = 0.624854
- fees = fee_shares(6.5217, 0.46) ≈ 0.116640
- edge_value = 6.5217 × (0.624854 − 0.53) = +0.618613
- slip_value = 6.5217 × (0.53 − 0.46) = +0.456522
- expected_fee_value = −0.116640 × 0.624854 = −0.072883
- luck_value = 3.405099 − (0.618613 + 0.456522 − 0.072883) = +2.402848
- **Sum check:** +0.618613 + 0.456522 − 0.072883 + 2.402848 = +3.405100 ✓

Reading: edge is small (~$0.62) because the model is only modestly confident
(p_for_side=0.625). Slip is also small (7¢ favorable). Most of the realized
PnL is `luck` — the trade paid full $3.40 but the model expected only ~$1.00,
so the residual lands in `luck`.

## Trade 3 — clean DOWN loss (id=172, slug=btc-updown-5m-1777210500)

| field | value |
| --- | --- |
| side | down |
| size | 6.9106 |
| entry_price | 0.45 |
| signal_reference_price | 0.52 |
| model_p_up | 0.388858 |
| outcome | up (lost) |
| trades.pnl (authoritative) | −3.109790 |

- model_p_for_side = 1 − 0.388858 = 0.611142
- fees = fee_shares(6.9106, 0.45) ≈ 0.124391
- edge_value = 6.9106 × (0.611142 − 0.52) = +0.629847
- slip_value = 6.9106 × (0.52 − 0.45) = +0.483745
- expected_fee_value = −0.124391 × 0.611142 = −0.075261
- luck_value = −3.109790 − (0.629847 + 0.483745 − 0.075261) = −4.148120
- **Sum check:** +0.629847 + 0.483745 − 0.075261 − 4.148120 = −3.109789 ✓

Reading: edge and slip both positive — the model said yes, the fill was
favorable — but the coin landed against us. All the badness is in `luck`
(the post-decision randomness). This is the textbook "bad luck, not bad
model" diagnosis the feature ships to surface.
