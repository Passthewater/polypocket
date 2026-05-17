# FAK paper calibration — Phase 1 report

**DB:** `C:\Users\Matt\polypocket\paper_trades.db`
**Cutoff:** `2026-04-24 00:00:00`
**p-column:** `model_p_up_v2`
**Generated:** 2026-05-17

## Top-line

| metric | value |
|---|---:|
| n_settled | 611 |
| win_rate | 72.3% |
| mean_p_pred | 0.755 |
| gap | -3.1pt |
| Brier | 0.2027 |

## By confidence bin

| bin | n | mean p_pred | hit rate | gap | note |
|---|---:|---:|---:|---:|---|
| 0.60–0.65 | 85 | 0.633 | 0.718 | +8.5pt |  |
| 0.65–0.70 | 68 | 0.690 | 0.721 | +3.1pt |  |
| 0.70–0.75 | 24 | 0.725 | 0.708 | -1.7pt |  |
| 0.75–0.80 | 121 | 0.769 | 0.777 | +0.8pt |  |
| 0.80–0.85 | 125 | 0.814 | 0.712 | -10.2pt |  |
| 0.85–0.90 | 131 | 0.878 | 0.740 | -13.8pt |  |
| 0.90–0.95 | 15 | 0.925 | 0.867 | -5.9pt | small |
| 0.95–1.00 | 12 | 0.984 | 0.750 | -23.4pt | small |

## By side

| side | n_settled | win_rate | mean p_pred | gap |
|---|---:|---:|---:|---:|
| up | 196 | 71.4% | 0.715 | -0.1pt |
| down | 415 | 72.8% | 0.774 | -4.6pt |

## By confidence bin x UP

| bin | n | mean p_pred | hit rate | gap | note |
|---|---:|---:|---:|---:|---|
| 0.75-0.80 | 58 | 0.774 | 0.862 | +8.8pt |  |
| 0.80-0.85 | 102 | 0.811 | 0.706 | -10.5pt |  |
| 0.85-0.90 | 6 | 0.867 | 0.833 | -3.4pt | small |
| 0.90-0.95 | 4 | 0.940 | 1.000 | +6.0pt | small |

## By confidence bin x DOWN

| bin | n | mean p_pred | hit rate | gap | note |
|---|---:|---:|---:|---:|---|
| 0.60-0.65 | 85 | 0.633 | 0.718 | +8.5pt |  |
| 0.65-0.70 | 68 | 0.690 | 0.721 | +3.1pt |  |
| 0.70-0.75 | 24 | 0.725 | 0.708 | -1.7pt |  |
| 0.75-0.80 | 63 | 0.764 | 0.698 | -6.5pt |  |
| 0.80-0.85 | 23 | 0.827 | 0.739 | -8.8pt |  |
| 0.85-0.90 | 125 | 0.879 | 0.736 | -14.3pt |  |
| 0.90-0.95 | 11 | 0.920 | 0.818 | -10.2pt | small |
| 0.95-1.00 | 12 | 0.984 | 0.750 | -23.4pt | small |

## UTC-band slice (19:40–02:25)

Cross-check against `[[project_live_v2_execution_gap]]` Brier 0.1167.

| metric | value |
|---|---:|
| n_settled | 169 |
| win_rate | 77.5% |
| mean_p_pred | 0.774 |
| gap | +0.2pt |
| Brier | 0.1711 |

## Gate verdict

| criterion | value | result |
|---|---|---|
| n_settled ≥ 500 | 611 | PASS |
| every n≥20 bin gap ∈ [−10pt, +10pt] | 2/6 large bins outside ±10pt | FAIL |
| no n≥20 bin gap < −15pt | no large bin below −15pt | PASS |
| DOWN overall gap ∈ [−7pt, +7pt] | -4.6pt | PASS |

**Overall GATE: FAIL**

> Note: GATE FAIL is expected on existing data — the 0.80–0.85 bin (gap −10.2pt n=125) and 0.85–0.90 bin (gap −13.8pt n=131) are already outside the strict ±10pt rule. This is a known carry-over risk pre-committed by the design (§Phase 1 reframe). The GATE is informational; the actual blockers checked by the design are the DOWN-side per-bin regression and the overall DOWN gap. Neither fires on current data (DOWN gap = -4.6pt).

