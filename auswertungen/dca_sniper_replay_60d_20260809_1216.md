# DCA Sniper / Recovery-Hold Replay (60d)

Generated: `2026-08-09T12:16:12.840205+00:00`

## Screen (hard falls in raster)

| Symbol | DD from high | 60d ret |
|--------|-------------:|--------:|
| LAB/USDT | -99.5% | -98.7% |
| NFP/USDT | -99.0% | -93.0% |
| SIREN/USDT | -96.9% | -94.3% |
| SKYAI/USDT | -95.2% | -30.1% |
| H/USDT | -92.3% | -61.3% |
| VELVET/USDT | -87.9% | 14.3% |
| BEAT/USDT | -87.5% | -33.4% |
| STG/USDT | -83.4% | -60.6% |
| MOVE/USDT | -77.7% | -50.8% |
| HIGH/USDT | -74.1% | -68.5% |
| ALCH/USDT | -70.5% | -53.9% |
| MANTA/USDT | -65.6% | -24.9% |
| BLESS/USDT | -63.4% | 92.9% |
| HMSTR/USDT | -62.9% | 24.1% |
| WLD/USDT | -59.4% | -39.0% |
| SPCX/USDT | -54.5% | -20.0% |
| ACT/USDT | -53.4% | 1.6% |
| BONK/USDT | -53.2% | -43.5% |
| RAVE/USDT | -52.7% | -39.0% |
| PORTAL/USDT | -49.8% | -25.7% |
| BRETT/USDT | -48.9% | -24.4% |
| DEGEN/USDT | -48.6% | -6.4% |
| AI/USDT | -48.2% | -17.6% |
| JTO/USDT | -47.0% | -15.7% |
| EIGEN/USDT | -45.4% | -5.5% |
| ARIA/USDT | -43.9% | 0.5% |
| GIGA/USDT | -43.6% | -17.7% |
| XPL/USDT | -41.5% | 16.0% |
| W/USDT | -41.2% | -5.5% |
| ZEREBRO/USDT | -41.1% | 33.8% |
| FET/USDT | -41.1% | -31.4% |
| TRUMP/USDT | -41.0% | -10.7% |
| NEAR/USDT | -38.7% | -24.9% |
| ZK/USDT | -38.6% | -25.6% |
| SPX/USDT | -38.5% | 6.3% |
| TOSHI/USDT | -37.9% | -11.7% |
| STRK/USDT | -37.0% | -28.7% |
| TAO/USDT | -37.0% | 0.2% |
| AERO/USDT | -36.2% | 27.8% |
| PIXEL/USDT | -35.6% | -12.4% |

## Policy summary

| Policy | n | sum PnL | median | wins | trail exits | hold blocks | DCA USDT |
|--------|--:|--------:|-------:|-----:|------------:|------------:|---------:|
| `A0_beat_stale` | 14 | -1993.3 | -155.6 | 0/14 | 14 | 0 | 0 |
| `A1_legacy_small` | 14 | -6858.1 | -595.7 | 0/14 | 5 | 0 | 4500 |
| `A2_heavy_hold` | 14 | -7662.8 | -657.5 | 0/14 | 6 | 0 | 12254 |
| `A3_heavy_hold_reclaim` | 14 | -8306.8 | -689.2 | 0/14 | 5 | 0 | 11939 |
| `A4_small_hold_reclaim` | 14 | -6824.4 | -596.3 | 0/14 | 5 | 0 | 4500 |
| `A5_reanchor_only` | 14 | 0.0 | 0.0 | 0/14 | 14 | 0 | 0 |

## Ranking vs A0

| Rank | Policy | Score | med Δ | sum PnL | hard SL | hold blocks |
|-----:|--------|------:|------:|--------:|--------:|------------:|
| 1 | `A5_reanchor_only` | 176.99 | 155.63 | 0.0 | 0 | 0 |
| 2 | `A4_small_hold_reclaim` | -475.82 | -409.06 | -6824.44 | 3 | 0 |
| 3 | `A1_legacy_small` | -481.67 | -414.55 | -6858.07 | 3 | 0 |
| 4 | `A2_heavy_hold` | -547.12 | -471.38 | -7662.8 | 3 | 0 |
| 5 | `A3_heavy_hold_reclaim` | -582.42 | -499.78 | -8306.77 | 3 | 0 |

**BEST:** `A5_reanchor_only` (score=176.99)


## Notes

- Entry near 60d high (tough recovery path).
- A0_beat_stale: DCA without peak reanchor (BEAT-class).
- A1_legacy_small: small DCA + reanchor + 12h grace.
- A2_heavy_hold: sniper heavy + hold, no reclaim gate.
- A3_heavy_hold_reclaim: heavy + hold ONLY on 3-bar reclaim (quality sniper).
- A4_small_hold_reclaim: small DCA + hold + reclaim (capital-light).
- A5_reanchor_only: reclaim reanchor without size.
- Hard SL -40% always. Relative ranking only.

## Per coin

### SPCX/USDT

DD -54.51% · 60d ret -20.0%

| Policy | PnL USDT | PnL % | Exit | DCA | Hold blocks | BE+ |
|--------|---------:|------:|------|----:|------------:|----|
| `A0_beat_stale` | -125.7 | -6.3 | trailing_stop | 0 | 0 | no |
| `A1_legacy_small` | -1000.0 | -40.0 | hard_sl | 500 | 0 | no |
| `A2_heavy_hold` | -1349.4 | -40.0 | hard_sl | 1373 | 0 | no |
| `A3_heavy_hold_reclaim` | -1292.4 | -40.0 | hard_sl | 1231 | 0 | no |
| `A4_small_hold_reclaim` | -1000.0 | -40.0 | hard_sl | 500 | 0 | no |
| `A5_reanchor_only` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |

### ACT/USDT

DD -53.38% · 60d ret 1.56%

| Policy | PnL USDT | PnL % | Exit | DCA | Hold blocks | BE+ |
|--------|---------:|------:|------|----:|------------:|----|
| `A0_beat_stale` | -59.0 | -3.0 | trailing_stop | 0 | 0 | no |
| `A1_legacy_small` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |
| `A2_heavy_hold` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |
| `A3_heavy_hold_reclaim` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |
| `A4_small_hold_reclaim` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |
| `A5_reanchor_only` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |

### BONK/USDT

DD -53.21% · 60d ret -43.5%

| Policy | PnL USDT | PnL % | Exit | DCA | Hold blocks | BE+ |
|--------|---------:|------:|------|----:|------------:|----|
| `A0_beat_stale` | -227.3 | -11.4 | trailing_stop | 0 | 0 | no |
| `A1_legacy_small` | -1000.0 | -40.0 | hard_sl | 500 | 0 | no |
| `A2_heavy_hold` | -1355.2 | -40.0 | hard_sl | 1388 | 0 | no |
| `A3_heavy_hold_reclaim` | -1338.2 | -40.0 | hard_sl | 1345 | 0 | no |
| `A4_small_hold_reclaim` | -1000.0 | -40.0 | hard_sl | 500 | 0 | no |
| `A5_reanchor_only` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |

### RAVE/USDT

DD -52.71% · 60d ret -39.0%

| Policy | PnL USDT | PnL % | Exit | DCA | Hold blocks | BE+ |
|--------|---------:|------:|------|----:|------------:|----|
| `A0_beat_stale` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |
| `A1_legacy_small` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |
| `A2_heavy_hold` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |
| `A3_heavy_hold_reclaim` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |
| `A4_small_hold_reclaim` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |
| `A5_reanchor_only` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |

### PORTAL/USDT

DD -49.82% · 60d ret -25.66%

| Policy | PnL USDT | PnL % | Exit | DCA | Hold blocks | BE+ |
|--------|---------:|------:|------|----:|------------:|----|
| `A0_beat_stale` | -93.5 | -4.7 | trailing_stop | 0 | 0 | no |
| `A1_legacy_small` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |
| `A2_heavy_hold` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |
| `A3_heavy_hold_reclaim` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |
| `A4_small_hold_reclaim` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |
| `A5_reanchor_only` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |

### BRETT/USDT

DD -48.93% · 60d ret -24.36%

| Policy | PnL USDT | PnL % | Exit | DCA | Hold blocks | BE+ |
|--------|---------:|------:|------|----:|------------:|----|
| `A0_beat_stale` | -148.6 | -7.4 | trailing_stop | 0 | 0 | no |
| `A1_legacy_small` | -600.7 | -24.0 | eow_mark | 500 | 0 | no |
| `A2_heavy_hold` | -689.2 | -20.4 | eow_mark | 1372 | 0 | no |
| `A3_heavy_hold_reclaim` | -708.3 | -20.9 | eow_mark | 1391 | 0 | no |
| `A4_small_hold_reclaim` | -606.8 | -24.3 | eow_mark | 500 | 0 | no |
| `A5_reanchor_only` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |

### DEGEN/USDT

DD -48.57% · 60d ret -6.41%

| Policy | PnL USDT | PnL % | Exit | DCA | Hold blocks | BE+ |
|--------|---------:|------:|------|----:|------------:|----|
| `A0_beat_stale` | -322.2 | -16.1 | trailing_stop | 0 | 0 | no |
| `A1_legacy_small` | -1000.0 | -40.0 | hard_sl | 500 | 0 | no |
| `A2_heavy_hold` | -1343.9 | -40.0 | hard_sl | 1360 | 0 | no |
| `A3_heavy_hold_reclaim` | -1329.2 | -40.0 | hard_sl | 1323 | 0 | no |
| `A4_small_hold_reclaim` | -1000.0 | -40.0 | hard_sl | 500 | 0 | no |
| `A5_reanchor_only` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |

### JTO/USDT

DD -46.97% · 60d ret -15.71%

| Policy | PnL USDT | PnL % | Exit | DCA | Hold blocks | BE+ |
|--------|---------:|------:|------|----:|------------:|----|
| `A0_beat_stale` | -207.2 | -10.4 | trailing_stop | 0 | 0 | no |
| `A1_legacy_small` | -791.8 | -31.7 | eow_mark | 500 | 0 | no |
| `A2_heavy_hold` | -875.2 | -27.2 | eow_mark | 1218 | 0 | no |
| `A3_heavy_hold_reclaim` | -909.8 | -28.0 | eow_mark | 1253 | 0 | no |
| `A4_small_hold_reclaim` | -804.0 | -32.2 | eow_mark | 500 | 0 | no |
| `A5_reanchor_only` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |

### EIGEN/USDT

DD -45.35% · 60d ret -5.48%

| Policy | PnL USDT | PnL % | Exit | DCA | Hold blocks | BE+ |
|--------|---------:|------:|------|----:|------------:|----|
| `A0_beat_stale` | -197.8 | -9.9 | trailing_stop | 0 | 0 | no |
| `A1_legacy_small` | -551.6 | -22.1 | eow_mark | 500 | 0 | no |
| `A2_heavy_hold` | -629.6 | -18.6 | eow_mark | 1390 | 0 | no |
| `A3_heavy_hold_reclaim` | -507.8 | -15.5 | eow_mark | 1268 | 0 | no |
| `A4_small_hold_reclaim` | -507.8 | -20.3 | eow_mark | 500 | 0 | no |
| `A5_reanchor_only` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |

### ARIA/USDT

DD -43.9% · 60d ret 0.48%

| Policy | PnL USDT | PnL % | Exit | DCA | Hold blocks | BE+ |
|--------|---------:|------:|------|----:|------------:|----|
| `A0_beat_stale` | -49.4 | -2.5 | trailing_stop | 0 | 0 | no |
| `A1_legacy_small` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |
| `A2_heavy_hold` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |
| `A3_heavy_hold_reclaim` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |
| `A4_small_hold_reclaim` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |
| `A5_reanchor_only` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |

### GIGA/USDT

DD -43.65% · 60d ret -17.71%

| Policy | PnL USDT | PnL % | Exit | DCA | Hold blocks | BE+ |
|--------|---------:|------:|------|----:|------------:|----|
| `A0_beat_stale` | -223.9 | -11.2 | trailing_stop | 0 | 0 | no |
| `A1_legacy_small` | -624.9 | -25.0 | eow_mark | 500 | 0 | no |
| `A2_heavy_hold` | -734.8 | -21.7 | eow_mark | 1393 | 0 | no |
| `A3_heavy_hold_reclaim` | -716.1 | -21.2 | eow_mark | 1374 | 0 | no |
| `A4_small_hold_reclaim` | -618.9 | -24.8 | eow_mark | 500 | 0 | no |
| `A5_reanchor_only` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |

### XPL/USDT

DD -41.49% · 60d ret 16.04%

| Policy | PnL USDT | PnL % | Exit | DCA | Hold blocks | BE+ |
|--------|---------:|------:|------|----:|------------:|----|
| `A0_beat_stale` | -176.2 | -8.8 | trailing_stop | 0 | 0 | no |
| `A1_legacy_small` | -698.4 | -27.9 | eow_mark | 500 | 0 | no |
| `A2_heavy_hold` | 0.0 | 0.0 | trailing_stop | 1370 | 0 | yes |
| `A3_heavy_hold_reclaim` | -835.0 | -24.7 | eow_mark | 1379 | 0 | yes |
| `A4_small_hold_reclaim` | -701.2 | -28.1 | eow_mark | 500 | 0 | no |
| `A5_reanchor_only` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |

### W/USDT

DD -41.18% · 60d ret -5.51%

| Policy | PnL USDT | PnL % | Exit | DCA | Hold blocks | BE+ |
|--------|---------:|------:|------|----:|------------:|----|
| `A0_beat_stale` | -162.7 | -8.1 | trailing_stop | 0 | 0 | no |
| `A1_legacy_small` | -590.8 | -23.6 | eow_mark | 500 | 0 | no |
| `A2_heavy_hold` | -685.5 | -20.2 | eow_mark | 1391 | 0 | no |
| `A3_heavy_hold_reclaim` | -670.0 | -19.9 | eow_mark | 1375 | 0 | no |
| `A4_small_hold_reclaim` | -585.8 | -23.4 | eow_mark | 500 | 0 | no |
| `A5_reanchor_only` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |

### ZEREBRO/USDT

DD -41.14% · 60d ret 33.81%

| Policy | PnL USDT | PnL % | Exit | DCA | Hold blocks | BE+ |
|--------|---------:|------:|------|----:|------------:|----|
| `A0_beat_stale` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |
| `A1_legacy_small` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |
| `A2_heavy_hold` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |
| `A3_heavy_hold_reclaim` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |
| `A4_small_hold_reclaim` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |
| `A5_reanchor_only` | 0.0 | 0.0 | trailing_stop | 0 | 0 | no |

