# Walk-Forward lang + DCA-Deckel (2026-11-01)

Harness `python -m backtest.harness.longwf`, code at `5fb073fe`, generated 2026-11-01T10:41:10.980872+00:00 (UTC).

## Method

* Data: public Gate.io spot candles (no keys). 1h is served only for the last 10,000 hours (from about 2025-09-10); before that each 4h bar is split into four synthetic 1h bars (path O-L-H-C on green bars, O-H-L-C on red, volume / 4). Results are shown for the full test period (Mar 2025 - Oct 2026, 20 test months) and for the real-1h part only (Oct 2025 - Oct 2026, 13 test months).
* Universe: 313 USDT pairs (coins the bot traded or watched for breadth, still listed today). Survivorship bias: coins delisted before today are missing, which flatters long-history results.
* Proxy entries P1 (pre-registered, Freqtrade cross-check rule): 24h return > 8 %, 24h quote volume > 2.0x its 6-day baseline, green bar; one signal per coin per 24 h; max 5 entries per Berlin day, first come; 1275 USDT (median recorded entry), tenant default params. Robustness proxy P2: 24h return > 3 %, relvol > 1.5 (broader, closer to recorded entry features).
* Adds: real `should_dca` on 1h bars (rule mode, tenant default DCA config). Stops: real `evaluate_long_hard_stop`. Proxy take-profit: full sell at the close when close >= upper BB(20,2) and >= avg x 1.015 (stands in for the recorded bb_upper/trailing exits). No new lot on a coin while a simulated lot is open.
* Breadth: share of the universe with close above its own 168h mean, from bars closed before the decision; weak < 40 %, strong > 60 %. Label shares over the period: weak 56 %, mid 16 %, strong 27 %.
* Walk-forward: rolling 2 months train / 1 month test. Fixed variants have no parameters to fit, so the train months only feed the WF selector (best variant by return on deployed capital over the 2 train months, applied in the test month; daily returns spliced, an approximation because each variant runs as its own book).
* Statistics: DSR with N = 58 (50 earlier harness trials + 8 new), 5-day block bootstrap CI of the daily excess return on deployed capital, PBO by CSCV (10 blocks) over all 8 variants, Monte-Carlo trade shuffle (2000) of lot PnL for drawdown. Costs: fee 0.2 %/side + class slippage, base and stress.
* Rule 4 (release framework), applied per variant: GO only if >= 2/3 of test months better (return on deployed capital), DSR > 0.5, bootstrap CI low > 0, no guardrail (open lots <= -20 %, max DD, capital in losing lots) > 1 % worse. Otherwise KILL (months or guardrail fail) or OBSERVE (months ok, statistics not).

## Proxy faithfulness P1 (overlap 2026-07-28 - 2026-10-30)

* Entries/day: real all tenants 11.2, real default 5.2, real gainer sources 1.7; proxy raw signals 18.4, proxy after cap 4.9.
* Coins: real 283, proxy 202, both 171, Jaccard 0.54.
* Timing (same coin, +-24 h): recall of real entries 34 % (gainer sources 19 %), precision of proxy entries 12 %.
* Recall by source: auto 14 %, cmc 78 %, entry_sensor_15m 34 %, gainer_live_heat 50 %, gainer_rank_entry 50 %, gainer_relvol 17 %, grid 20 %, lc 38 %.

| pre-entry (median) | real all | real gainer | proxy |
|---|---|---|---|
| 24h return % | 3.8 | 2.2 | 12.1 |
| 72h return % | 4.9 | 1.6 | 16.7 |
| 48h pump vs low % | 9.2 | 7.4 | 19.8 |

| baseline outcome | lots | PnL % | median lot % | lots <= -20 % | lots with adds % | adds/lot | adds/entry (median) | hold d (closed, median) | open at end % |
|---|---|---|---|---|---|---|---|---|---|
| proxy, rule DCA, proxy TP | 405 | -0.36 | 2.7 | 5.7 | 39.0 | 1.00 | 1.22 | 0.67 | 8.6 |
| tape, recorded adds+exits | 1039 | -0.68 | 1.3 | 9.0 | 24.7 | 0.42 | 0.91 | 1.22 | 12.8 |
| tape entries, rule DCA | 1039 | -0.63 | 1.4 | 7.9 | 37.1 | 0.81 | 0.60 | 1.20 | 12.9 |

Weekly cohort PnL % correlation proxy vs tape: 0.65 (n = 13 weeks).

| variant | proxy delta USDT (overlap cohort) | tape delta USDT | same sign |
|---|---|---|---|
| a_max2adds | 920 | -45 | no |
| b_budget2x | 1,178 | 1,434 | yes |
| c_breadth40 | -1,708 | -3,195 | yes |
| d_cool24h | 0 | 0 | yes |
| e_age14_m15 | 5,588 | 3,050 | yes |
| combo_abcde | 1,514 | 2,438 | yes |
| rg_breadth | 6,043 | 18,393 | yes |

## Proxy faithfulness P2 (overlap 2026-07-28 - 2026-10-30)

* Entries/day: real all tenants 11.2, real default 5.2, real gainer sources 1.7; proxy raw signals 42.7, proxy after cap 5.0.
* Coins: real 283, proxy 219, both 189, Jaccard 0.60.
* Timing (same coin, +-24 h): recall of real entries 48 % (gainer sources 36 %), precision of proxy entries 9 %.
* Recall by source: auto 22 %, cmc 88 %, entry_sensor_15m 41 %, gainer_live_heat 100 %, gainer_rank_entry 62 %, gainer_relvol 34 %, grid 38 %, lc 55 %.

| pre-entry (median) | real all | real gainer | proxy |
|---|---|---|---|
| 24h return % | 3.8 | 2.2 | 5.4 |
| 72h return % | 4.9 | 1.6 | 8.2 |
| 48h pump vs low % | 9.2 | 7.4 | 11.8 |

| baseline outcome | lots | PnL % | median lot % | lots <= -20 % | lots with adds % | adds/lot | adds/entry (median) | hold d (closed, median) | open at end % |
|---|---|---|---|---|---|---|---|---|---|
| proxy, rule DCA, proxy TP | 383 | -0.89 | 2.1 | 4.2 | 41.5 | 1.03 | 1.18 | 1.04 | 8.4 |
| tape, recorded adds+exits | 1039 | -0.68 | 1.3 | 9.0 | 24.7 | 0.42 | 0.91 | 1.22 | 12.8 |
| tape entries, rule DCA | 1039 | -0.63 | 1.4 | 7.9 | 37.1 | 0.81 | 0.60 | 1.20 | 12.9 |

Weekly cohort PnL % correlation proxy vs tape: 0.52 (n = 13 weeks).

| variant | proxy delta USDT (overlap cohort) | tape delta USDT | same sign |
|---|---|---|---|
| a_max2adds | 4,287 | -45 | no |
| b_budget2x | 2,658 | 1,434 | yes |
| c_breadth40 | -1,563 | -3,195 | yes |
| d_cool24h | 0 | 0 | yes |
| e_age14_m15 | 2,570 | 3,050 | yes |
| combo_abcde | 3,538 | 2,438 | yes |
| rg_breadth | 4,806 | 18,393 | yes |

## Results P1, costs base, Mar 2025 - Oct 2026 (20 test months)

| variant | lots | PnL USDT | avg deployed | return on deployed % | months won (ROC/USDT) | DSR | excess bp/day [CI] | lots <= -20 % | max DD % | MC DD p95 USDT | guardrails worse | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 2453 | 11,995 | 98,418 | 12.19 | - | - | - | 3.9 | 43.4 | 26,247 | - | - |
| a_max2adds | 2448 | -11,296 | 70,692 | -15.98 | 5/20 / 7/20 | 0.00 | -3.29 [-7.79, 1.17] | 5.6 | 53.2 | 30,638 | max_dd_pct | **KILL** |
| b_budget2x | 2435 | 1,971 | 86,774 | 2.27 | 9/20 / 6/20 | 0.00 | -1.55 [-4.75, 1.45] | 4.5 | 47.4 | 27,649 | max_dd_pct | **KILL** |
| c_breadth40 | 2401 | -20,969 | 99,877 | -20.99 | 8/20 / 8/20 | 0.00 | -8.84 [-15.18, -3.28] | 4.5 | 55.1 | 39,782 | max_dd_pct | **KILL** |
| d_cool24h | 2451 | 11,864 | 98,415 | 12.05 | 5/20 / 0/20 | 0.01 | -0.00 [-0.01, 0.01] | 3.9 | 43.5 | 27,107 | - | **KILL** |
| e_age14_m15 | 2750 | 1,531 | 42,183 | 3.63 | 11/20 / 10/20 | 0.82 | 20.83 [5.34, 38.87] | 4.6 | 51.5 | 28,375 | max_dd_pct | **KILL** |
| combo_abcde | 2718 | -31,718 | 32,330 | -98.11 | 4/20 / 7/20 | 0.00 | -2.33 [-16.45, 13.03] | 6.5 | 69.0 | 40,742 | max_dd_pct | **KILL** |
| rg_breadth | 629 | -8,607 | 19,745 | -43.59 | 5/20 / 8/20 | 0.00 | -7.20 [-20.93, 8.94] | 2.2 | 45.2 | 15,536 | share_open_le_minus20, max_dd_pct | **KILL** |

PBO (CSCV, 252 splits): 0.03. WF selector: won 10/20 months, DSR 0.91, excess 19.95 bp/day [CI 5.42, 37.65], beats 100 % of 500 random monthly switches. Picks: 2025-03 e_age14_m15, 2025-04 e_age14_m15, 2025-05 e_age14_m15, 2025-06 e_age14_m15, 2025-07 e_age14_m15, 2025-08 e_age14_m15, 2025-09 c_breadth40, 2025-10 e_age14_m15, 2025-11 baseline, 2025-12 baseline, 2026-01 baseline, 2026-02 d_cool24h, 2026-03 d_cool24h, 2026-04 c_breadth40, 2026-05 c_breadth40, 2026-06 combo_abcde, 2026-07 rg_breadth, 2026-08 e_age14_m15, 2026-09 e_age14_m15, 2026-10 e_age14_m15.

## Results P1, costs base, real 1h only, Oct 2025 - Oct 2026 (13 test months)

| variant | lots | PnL USDT | avg deployed | return on deployed % | months won (ROC/USDT) | DSR | excess bp/day [CI] | lots <= -20 % | max DD % | MC DD p95 USDT | guardrails worse | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 1640 | -31,787 | 120,020 | -26.49 | - | - | - | 4.6 | 43.4 | 36,481 | - | - |
| a_max2adds | 1637 | -38,767 | 83,478 | -46.44 | 2/13 / 6/13 | 0.00 | -3.08 [-8.06, 2.22] | 6.2 | 53.2 | 34,990 | max_dd_pct | **KILL** |
| b_budget2x | 1627 | -36,075 | 104,884 | -34.40 | 5/13 / 4/13 | 0.00 | -2.50 [-6.01, 1.28] | 5.2 | 47.4 | 35,651 | max_dd_pct | **KILL** |
| c_breadth40 | 1620 | -45,859 | 118,294 | -38.77 | 6/13 / 6/13 | 0.00 | -4.58 [-10.36, 0.45] | 5.1 | 55.1 | 40,818 | max_dd_pct | **KILL** |
| d_cool24h | 1638 | -31,919 | 120,016 | -26.60 | 5/13 / 0/13 | 0.01 | -0.00 [-0.02, 0.02] | 4.6 | 43.5 | 37,350 | - | **KILL** |
| e_age14_m15 | 1868 | -30,566 | 48,907 | -62.50 | 6/13 / 8/13 | 0.05 | 4.65 [-9.50, 16.79] | 5.5 | 51.5 | 45,863 | max_dd_pct | **KILL** |
| combo_abcde | 1846 | -39,659 | 36,904 | -107.47 | 2/13 / 7/13 | 0.00 | -11.29 [-25.45, 0.86] | 7.6 | 69.0 | 46,716 | max_dd_pct | **KILL** |
| rg_breadth | 390 | -12,077 | 26,559 | -45.47 | 4/13 / 8/13 | 0.00 | -8.31 [-19.55, 2.63] | 2.8 | 45.2 | 14,768 | share_open_le_minus20, max_dd_pct | **KILL** |

PBO (CSCV, 252 splits): 0.17. WF selector: won 5/13 months, DSR 0.04, excess 3.05 bp/day [CI -7.69, 13.49], beats 94 % of 500 random monthly switches. Picks: 2025-10 e_age14_m15, 2025-11 baseline, 2025-12 baseline, 2026-01 baseline, 2026-02 d_cool24h, 2026-03 d_cool24h, 2026-04 c_breadth40, 2026-05 c_breadth40, 2026-06 combo_abcde, 2026-07 rg_breadth, 2026-08 e_age14_m15, 2026-09 e_age14_m15, 2026-10 e_age14_m15.

### Monthly return on deployed capital % (P1, base)

| month | baseline | a_max2adds | b_budget2x | c_breadth40 | d_cool24h | e_age14_m15 | combo_abcde | rg_breadth |
|---|---|---|---|---|---|---|---|---|
| 2025-03 | 0.81 | 1.37 | 0.59 | -7.14 | 0.81 | 8.90 | -2.55 | -18.04 |
| 2025-04 | 22.82 | 21.12 | 24.11 | 24.05 | 22.82 | 33.99 | 34.25 | 17.98 |
| 2025-05 | 23.58 | 19.73 | 24.39 | 8.02 | 23.58 | 59.71 | 45.48 | 49.38 |
| 2025-06 | 16.55 | 11.68 | 13.09 | -4.87 | 16.55 | 26.88 | -8.18 | 5.85 |
| 2025-07 | 12.03 | 14.83 | 12.11 | 17.14 | 12.03 | 6.56 | -0.44 | 11.05 |
| 2025-08 | 5.70 | 6.00 | 7.64 | 5.38 | 5.70 | 12.45 | 2.68 | -2.43 |
| 2025-09 | 4.47 | -2.31 | 1.05 | 1.52 | 4.47 | -0.06 | -2.72 | 0.34 |
| 2025-10 | -38.83 | -46.30 | -44.47 | -40.84 | -39.06 | -35.27 | -42.22 | -73.29 |
| 2025-11 | -11.94 | -15.60 | -14.29 | -18.60 | -11.94 | -18.47 | -26.61 | -15.69 |
| 2025-12 | -6.36 | -7.48 | -5.56 | -11.75 | -6.36 | -7.82 | -13.77 | -19.45 |
| 2026-01 | -9.12 | -9.60 | -10.45 | -6.64 | -9.12 | -19.34 | -22.64 | -8.31 |
| 2026-02 | -4.92 | -7.67 | -6.60 | -7.49 | -4.92 | -2.08 | -9.49 | -12.29 |
| 2026-03 | -2.40 | -0.12 | -1.32 | 1.34 | -2.40 | -7.18 | -9.05 | -6.78 |
| 2026-04 | 13.71 | 10.61 | 13.89 | 13.46 | 13.71 | 10.84 | 7.62 | 12.11 |
| 2026-05 | 4.91 | 4.51 | 3.41 | 3.19 | 4.91 | 11.85 | 18.07 | 8.53 |
| 2026-06 | -11.62 | -12.79 | -13.25 | -10.62 | -11.62 | -13.80 | -19.20 | -12.31 |
| 2026-07 | 1.90 | -1.23 | 1.24 | 2.37 | 1.90 | 4.00 | 0.69 | -7.27 |
| 2026-08 | 13.16 | 16.49 | 13.45 | 14.44 | 13.16 | 18.49 | 19.50 | 10.16 |
| 2026-09 | 22.06 | 21.27 | 24.91 | 15.32 | 22.06 | 39.54 | 20.70 | 23.40 |
| 2026-10 | -16.55 | -17.66 | -18.45 | -13.32 | -16.55 | -24.80 | -27.74 | -2.42 |

## Results P2, costs base, Mar 2025 - Oct 2026 (20 test months)

| variant | lots | PnL USDT | avg deployed | return on deployed % | months won (ROC/USDT) | DSR | excess bp/day [CI] | lots <= -20 % | max DD % | MC DD p95 USDT | guardrails worse | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 2501 | -52,470 | 111,009 | -47.27 | - | - | - | 4.3 | 47.8 | 56,877 | - | - |
| a_max2adds | 2528 | -51,742 | 76,421 | -67.71 | 9/20 / 9/20 | 0.02 | 0.53 [-3.05, 4.22] | 5.5 | 61.8 | 54,158 | max_dd_pct | **KILL** |
| b_budget2x | 2503 | -42,650 | 93,318 | -45.70 | 10/20 / 11/20 | 0.35 | 2.15 [-0.08, 4.27] | 4.6 | 50.3 | 48,235 | max_dd_pct | **KILL** |
| c_breadth40 | 2540 | -55,086 | 101,703 | -54.16 | 10/20 / 9/20 | 0.00 | -1.10 [-5.17, 2.57] | 4.0 | 50.3 | 62,156 | max_dd_pct, capital_in_losing_lots | **KILL** |
| d_cool24h | 2494 | -53,018 | 110,977 | -47.77 | 1/20 / 1/20 | 0.00 | -0.06 [-0.16, 0.02] | 4.3 | 48.0 | 57,677 | - | **KILL** |
| e_age14_m15 | 2886 | -58,690 | 48,026 | -122.20 | 6/20 / 13/20 | 0.71 | 17.38 [-0.87, 39.96] | 5.0 | 61.3 | 65,688 | max_dd_pct | **KILL** |
| combo_abcde | 2838 | -41,743 | 35,512 | -117.54 | 7/20 / 12/20 | 0.25 | 10.06 [-5.61, 27.82] | 5.6 | 58.1 | 47,176 | max_dd_pct | **KILL** |
| rg_breadth | 1370 | -48,101 | 46,939 | -102.48 | 4/20 / 11/20 | 0.00 | -10.18 [-17.58, -3.42] | 2.5 | 63.5 | 53,022 | max_dd_pct | **KILL** |

PBO (CSCV, 252 splits): 0.20. WF selector: won 6/20 months, DSR 0.19, excess 7.13 bp/day [CI -6.22, 24.58], beats 73 % of 500 random monthly switches. Picks: 2025-03 e_age14_m15, 2025-04 c_breadth40, 2025-05 c_breadth40, 2025-06 combo_abcde, 2025-07 e_age14_m15, 2025-08 e_age14_m15, 2025-09 c_breadth40, 2025-10 combo_abcde, 2025-11 combo_abcde, 2025-12 e_age14_m15, 2026-01 baseline, 2026-02 c_breadth40, 2026-03 c_breadth40, 2026-04 rg_breadth, 2026-05 c_breadth40, 2026-06 baseline, 2026-07 baseline, 2026-08 baseline, 2026-09 baseline, 2026-10 combo_abcde.

## Results P2, costs base, real 1h only, Oct 2025 - Oct 2026 (13 test months)

| variant | lots | PnL USDT | avg deployed | return on deployed % | months won (ROC/USDT) | DSR | excess bp/day [CI] | lots <= -20 % | max DD % | MC DD p95 USDT | guardrails worse | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 1615 | -58,675 | 123,426 | -47.54 | - | - | - | 4.8 | 47.8 | 49,467 | - | - |
| a_max2adds | 1639 | -54,910 | 84,341 | -65.10 | 5/13 / 6/13 | 0.00 | -2.06 [-6.05, 1.64] | 5.9 | 61.8 | 45,470 | max_dd_pct | **KILL** |
| b_budget2x | 1619 | -52,059 | 103,729 | -50.19 | 5/13 / 7/13 | 0.03 | 0.55 [-1.99, 2.98] | 5.1 | 50.3 | 43,962 | max_dd_pct | **KILL** |
| c_breadth40 | 1644 | -64,972 | 115,990 | -56.02 | 5/13 / 4/13 | 0.00 | -2.61 [-7.23, 1.64] | 4.5 | 50.3 | 54,720 | max_dd_pct, capital_in_losing_lots | **KILL** |
| d_cool24h | 1609 | -59,164 | 123,378 | -47.95 | 1/13 / 1/13 | 0.00 | -0.07 [-0.21, 0.05] | 4.8 | 48.0 | 49,431 | - | **KILL** |
| e_age14_m15 | 1875 | -57,880 | 53,291 | -108.61 | 3/13 / 9/13 | 0.00 | -7.23 [-17.33, 3.31] | 5.7 | 61.3 | 66,065 | max_dd_pct | **KILL** |
| combo_abcde | 1846 | -43,479 | 39,812 | -109.21 | 4/13 / 8/13 | 0.00 | -9.47 [-20.78, 1.38] | 6.7 | 58.1 | 50,317 | max_dd_pct | **KILL** |
| rg_breadth | 849 | -39,831 | 57,852 | -68.85 | 3/13 / 9/13 | 0.00 | -5.91 [-12.57, 0.69] | 2.5 | 63.5 | 34,462 | max_dd_pct | **KILL** |

PBO (CSCV, 252 splits): 0.24. WF selector: won 2/13 months, DSR 0.00, excess -6.12 bp/day [CI -13.79, 0.92], beats 20 % of 500 random monthly switches. Picks: 2025-10 combo_abcde, 2025-11 combo_abcde, 2025-12 e_age14_m15, 2026-01 baseline, 2026-02 c_breadth40, 2026-03 c_breadth40, 2026-04 rg_breadth, 2026-05 c_breadth40, 2026-06 baseline, 2026-07 baseline, 2026-08 baseline, 2026-09 baseline, 2026-10 combo_abcde.

### Monthly return on deployed capital % (P2, base)

| month | baseline | a_max2adds | b_budget2x | c_breadth40 | d_cool24h | e_age14_m15 | combo_abcde | rg_breadth |
|---|---|---|---|---|---|---|---|---|
| 2025-03 | -17.50 | -18.14 | -18.34 | -10.04 | -17.50 | -23.54 | -22.52 | -16.92 |
| 2025-04 | 13.05 | 15.60 | 13.78 | 16.94 | 12.98 | 9.06 | 19.68 | 9.40 |
| 2025-05 | 5.23 | 11.28 | 7.65 | 7.22 | 5.23 | 22.62 | 20.82 | -5.23 |
| 2025-06 | -0.92 | -5.61 | -0.71 | -7.31 | -0.92 | 3.75 | -7.64 | -6.36 |
| 2025-07 | 15.26 | 17.67 | 17.49 | 13.51 | 15.26 | 15.36 | 9.64 | 11.52 |
| 2025-08 | 0.36 | -1.18 | -1.11 | 2.22 | 0.36 | -0.82 | 5.01 | -11.01 |
| 2025-09 | -2.84 | -2.55 | 0.10 | -2.78 | -2.84 | -4.37 | -3.10 | -5.95 |
| 2025-10 | -40.96 | -43.51 | -42.10 | -31.35 | -41.15 | -26.18 | -18.10 | -44.15 |
| 2025-11 | -14.58 | -13.67 | -13.47 | -19.42 | -14.58 | -15.22 | -24.04 | -24.70 |
| 2025-12 | -11.27 | -14.13 | -12.56 | -13.10 | -11.29 | -22.67 | -23.48 | -12.68 |
| 2026-01 | -8.83 | -7.89 | -6.25 | -3.51 | -8.83 | -16.62 | -11.36 | -6.78 |
| 2026-02 | -3.44 | -6.10 | -4.60 | -7.63 | -3.44 | -4.05 | -15.19 | -3.92 |
| 2026-03 | -4.44 | -6.54 | -3.58 | -2.82 | -4.29 | -9.76 | -12.25 | -0.44 |
| 2026-04 | 11.71 | 9.41 | 11.62 | 12.21 | 11.51 | 1.82 | 2.44 | 3.94 |
| 2026-05 | 6.09 | 6.66 | 5.94 | 3.82 | 6.09 | 14.59 | 9.07 | -1.28 |
| 2026-06 | -15.28 | -17.67 | -16.65 | -18.24 | -15.49 | -21.04 | -21.65 | -19.10 |
| 2026-07 | -1.94 | -3.85 | -2.54 | -1.75 | -1.94 | -4.73 | -0.26 | -1.34 |
| 2026-08 | 13.84 | 14.50 | 14.15 | 12.78 | 13.84 | 9.39 | 12.89 | 11.68 |
| 2026-09 | 22.96 | 24.86 | 24.87 | 19.36 | 22.96 | 27.48 | 32.73 | 22.14 |
| 2026-10 | -17.55 | -18.42 | -18.19 | -18.70 | -17.55 | -22.94 | -28.91 | -17.67 |

## Results P1, costs stress, Mar 2025 - Oct 2026 (20 test months)

| variant | lots | PnL USDT | avg deployed | return on deployed % | months won (ROC/USDT) | DSR | excess bp/day [CI] | lots <= -20 % | max DD % | MC DD p95 USDT | guardrails worse | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 2446 | 10,332 | 98,987 | 10.44 | - | - | - | 3.9 | 43.4 | 26,801 | - | - |
| a_max2adds | 2441 | -12,959 | 71,261 | -18.18 | 5/20 / 7/20 | 0.00 | -3.42 [-7.80, 0.98] | 5.6 | 53.5 | 31,487 | max_dd_pct | **KILL** |
| b_budget2x | 2428 | 308 | 87,344 | 0.35 | 8/20 / 6/20 | 0.00 | -1.60 [-4.73, 1.33] | 4.5 | 47.4 | 28,537 | max_dd_pct | **KILL** |
| c_breadth40 | 2397 | -22,709 | 100,483 | -22.60 | 9/20 / 8/20 | 0.00 | -8.78 [-15.02, -3.24] | 4.5 | 55.7 | 40,816 | max_dd_pct | **KILL** |
| d_cool24h | 2444 | 10,201 | 98,984 | 10.31 | 7/20 / 0/20 | 0.01 | -0.00 [-0.02, 0.01] | 3.9 | 43.5 | 27,151 | - | **KILL** |
| e_age14_m15 | 2749 | -547 | 42,303 | -1.29 | 11/20 / 10/20 | 0.80 | 20.42 [4.87, 38.51] | 4.6 | 51.9 | 29,113 | max_dd_pct | **KILL** |
| combo_abcde | 2717 | -34,390 | 32,418 | -106.08 | 4/20 / 7/20 | 0.00 | -2.98 [-17.22, 12.36] | 6.6 | 70.4 | 42,770 | max_dd_pct | **KILL** |
| rg_breadth | 627 | -9,085 | 20,159 | -45.07 | 5/20 / 8/20 | 0.00 | -10.65 [-22.52, 3.32] | 2.2 | 45.1 | 15,683 | share_open_le_minus20, max_dd_pct | **KILL** |

PBO (CSCV, 252 splits): 0.03. WF selector: won 10/20 months, DSR 0.91, excess 19.03 bp/day [CI 4.64, 36.36], beats 100 % of 500 random monthly switches. Picks: 2025-03 e_age14_m15, 2025-04 e_age14_m15, 2025-05 e_age14_m15, 2025-06 e_age14_m15, 2025-07 e_age14_m15, 2025-08 e_age14_m15, 2025-09 c_breadth40, 2025-10 e_age14_m15, 2025-11 baseline, 2025-12 baseline, 2026-01 d_cool24h, 2026-02 d_cool24h, 2026-03 d_cool24h, 2026-04 c_breadth40, 2026-05 c_breadth40, 2026-06 combo_abcde, 2026-07 rg_breadth, 2026-08 c_breadth40, 2026-09 e_age14_m15, 2026-10 e_age14_m15.

## Results P1, costs stress, real 1h only, Oct 2025 - Oct 2026 (13 test months)

| variant | lots | PnL USDT | avg deployed | return on deployed % | months won (ROC/USDT) | DSR | excess bp/day [CI] | lots <= -20 % | max DD % | MC DD p95 USDT | guardrails worse | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 1636 | -31,918 | 120,165 | -26.56 | - | - | - | 4.6 | 43.4 | 37,149 | - | - |
| a_max2adds | 1633 | -38,898 | 83,623 | -46.52 | 2/13 / 6/13 | 0.00 | -3.05 [-8.05, 2.23] | 6.2 | 53.5 | 35,370 | max_dd_pct | **KILL** |
| b_budget2x | 1623 | -36,205 | 105,029 | -34.47 | 5/13 / 4/13 | 0.00 | -2.45 [-5.94, 1.27] | 5.2 | 47.4 | 35,484 | max_dd_pct | **KILL** |
| c_breadth40 | 1619 | -46,624 | 118,409 | -39.38 | 6/13 / 6/13 | 0.00 | -4.74 [-10.52, 0.18] | 5.1 | 55.7 | 40,731 | max_dd_pct | **KILL** |
| d_cool24h | 1634 | -32,049 | 120,161 | -26.67 | 6/13 / 0/13 | 0.01 | -0.00 [-0.03, 0.02] | 4.7 | 43.5 | 36,861 | - | **KILL** |
| e_age14_m15 | 1867 | -31,070 | 48,993 | -63.42 | 6/13 / 8/13 | 0.04 | 4.16 [-10.20, 16.44] | 5.5 | 51.9 | 46,031 | max_dd_pct | **KILL** |
| combo_abcde | 1845 | -40,107 | 36,952 | -108.54 | 2/13 / 7/13 | 0.00 | -11.84 [-25.92, 0.25] | 7.6 | 70.4 | 47,318 | max_dd_pct | **KILL** |
| rg_breadth | 389 | -12,098 | 26,829 | -45.09 | 4/13 / 8/13 | 0.00 | -8.56 [-19.77, 2.51] | 2.8 | 45.1 | 14,660 | share_open_le_minus20, max_dd_pct | **KILL** |

PBO (CSCV, 252 splits): 0.19. WF selector: won 5/13 months, DSR 0.02, excess 1.79 bp/day [CI -8.37, 11.51], beats 91 % of 500 random monthly switches. Picks: 2025-10 e_age14_m15, 2025-11 baseline, 2025-12 baseline, 2026-01 d_cool24h, 2026-02 d_cool24h, 2026-03 d_cool24h, 2026-04 c_breadth40, 2026-05 c_breadth40, 2026-06 combo_abcde, 2026-07 rg_breadth, 2026-08 c_breadth40, 2026-09 e_age14_m15, 2026-10 e_age14_m15.

## Results P2, costs stress, Mar 2025 - Oct 2026 (20 test months)

| variant | lots | PnL USDT | avg deployed | return on deployed % | months won (ROC/USDT) | DSR | excess bp/day [CI] | lots <= -20 % | max DD % | MC DD p95 USDT | guardrails worse | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 2500 | -53,796 | 111,059 | -48.44 | - | - | - | 4.3 | 48.0 | 58,078 | - | - |
| a_max2adds | 2527 | -53,069 | 76,471 | -69.40 | 9/20 / 9/20 | 0.02 | 0.39 [-3.16, 4.06] | 5.5 | 62.1 | 55,454 | max_dd_pct | **KILL** |
| b_budget2x | 2502 | -43,976 | 93,368 | -47.10 | 10/20 / 11/20 | 0.33 | 2.09 [-0.12, 4.21] | 4.6 | 50.5 | 49,030 | max_dd_pct | **KILL** |
| c_breadth40 | 2534 | -56,479 | 102,161 | -55.28 | 10/20 / 9/20 | 0.00 | -1.14 [-5.22, 2.49] | 4.0 | 50.2 | 63,377 | max_dd_pct, capital_in_losing_lots | **KILL** |
| d_cool24h | 2494 | -54,317 | 111,030 | -48.92 | 3/20 / 1/20 | 0.00 | -0.06 [-0.16, 0.02] | 4.3 | 48.2 | 58,150 | - | **KILL** |
| e_age14_m15 | 2885 | -60,699 | 48,127 | -126.12 | 5/20 / 13/20 | 0.56 | 14.40 [-2.05, 34.54] | 5.0 | 63.2 | 67,217 | max_dd_pct | **KILL** |
| combo_abcde | 2837 | -44,695 | 35,634 | -125.43 | 7/20 / 12/20 | 0.17 | 8.21 [-6.90, 25.16] | 5.7 | 60.8 | 49,712 | max_dd_pct | **KILL** |
| rg_breadth | 1370 | -48,798 | 47,036 | -103.75 | 4/20 / 11/20 | 0.00 | -10.33 [-17.82, -3.55] | 2.6 | 64.0 | 53,689 | max_dd_pct | **KILL** |

PBO (CSCV, 252 splits): 0.21. WF selector: won 5/20 months, DSR 0.10, excess 4.97 bp/day [CI -6.47, 19.89], beats 68 % of 500 random monthly switches. Picks: 2025-03 e_age14_m15, 2025-04 c_breadth40, 2025-05 c_breadth40, 2025-06 combo_abcde, 2025-07 e_age14_m15, 2025-08 e_age14_m15, 2025-09 c_breadth40, 2025-10 combo_abcde, 2025-11 combo_abcde, 2025-12 e_age14_m15, 2026-01 baseline, 2026-02 c_breadth40, 2026-03 c_breadth40, 2026-04 rg_breadth, 2026-05 c_breadth40, 2026-06 baseline, 2026-07 baseline, 2026-08 baseline, 2026-09 baseline, 2026-10 combo_abcde.

## Results P2, costs stress, real 1h only, Oct 2025 - Oct 2026 (13 test months)

| variant | lots | PnL USDT | avg deployed | return on deployed % | months won (ROC/USDT) | DSR | excess bp/day [CI] | lots <= -20 % | max DD % | MC DD p95 USDT | guardrails worse | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 1614 | -59,440 | 123,484 | -48.14 | - | - | - | 4.8 | 48.0 | 49,924 | - | - |
| a_max2adds | 1638 | -55,675 | 84,399 | -65.97 | 5/13 / 6/13 | 0.00 | -2.13 [-6.12, 1.58] | 5.9 | 62.1 | 46,122 | max_dd_pct | **KILL** |
| b_budget2x | 1618 | -52,824 | 103,787 | -50.90 | 5/13 / 7/13 | 0.03 | 0.51 [-2.02, 2.94] | 5.1 | 50.5 | 44,490 | max_dd_pct | **KILL** |
| c_breadth40 | 1640 | -66,319 | 116,344 | -57.00 | 5/13 / 4/13 | 0.00 | -2.75 [-7.36, 1.47] | 4.5 | 50.2 | 55,800 | max_dd_pct, capital_in_losing_lots | **KILL** |
| d_cool24h | 1609 | -59,901 | 123,441 | -48.53 | 3/13 / 1/13 | 0.00 | -0.07 [-0.21, 0.04] | 4.8 | 48.2 | 49,666 | - | **KILL** |
| e_age14_m15 | 1874 | -59,202 | 53,421 | -110.82 | 3/13 / 9/13 | 0.00 | -7.54 [-17.56, 2.88] | 5.7 | 63.2 | 66,974 | max_dd_pct | **KILL** |
| combo_abcde | 1845 | -46,009 | 39,978 | -115.09 | 4/13 / 8/13 | 0.00 | -10.58 [-21.66, 0.13] | 6.8 | 60.8 | 52,787 | max_dd_pct | **KILL** |
| rg_breadth | 849 | -40,155 | 57,993 | -69.24 | 3/13 / 9/13 | 0.00 | -5.82 [-12.53, 0.85] | 2.6 | 64.0 | 34,767 | max_dd_pct | **KILL** |

PBO (CSCV, 252 splits): 0.18. WF selector: won 2/13 months, DSR 0.00, excess -6.28 bp/day [CI -13.93, 0.71], beats 20 % of 500 random monthly switches. Picks: 2025-10 combo_abcde, 2025-11 combo_abcde, 2025-12 e_age14_m15, 2026-01 baseline, 2026-02 c_breadth40, 2026-03 c_breadth40, 2026-04 rg_breadth, 2026-05 c_breadth40, 2026-06 baseline, 2026-07 baseline, 2026-08 baseline, 2026-09 baseline, 2026-10 combo_abcde.

## Random on/off baselines (P1, base)

Random weak/mid/strong labels in blocks of 3 h with the empirical shares, 20 seeds per variant.

| variant | real excess bp/day | random median | real beats random % | real PnL USDT | random median USDT | real beats random (USDT) % |
|---|---|---|---|---|---|---|
| c_breadth40 | -8.84 | -1.96 | 0 | -20,969 | 10,435 | 0 |
| rg_breadth | -7.20 | -9.87 | 65 | -8,607 | -7,958 | 40 |

## Real tape (W1-W3 entries, recorded adds/exits), costs base

| variant | lots | PnL USDT | PnL % | W1 % | W2 % | W3 % | windows won | lots <= -20 % | adds skipped | time stops | filtered |
|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 1039 | -16,889 | -0.68 | 1.76 | -0.03 | -10.57 | -/3 | 9.0 | 0 | 0 | 0 |
| a_max2adds | 1039 | -16,934 | -0.69 | 1.85 | -0.13 | -10.67 | 1/3 | 9.0 | 27 | 0 | 0 |
| b_budget2x | 1039 | -15,455 | -0.63 | 1.90 | -0.01 | -10.77 | 2/3 | 9.0 | 21 | 0 | 0 |
| c_breadth40 | 1039 | -20,084 | -0.85 | 1.52 | -0.35 | -10.99 | 0/3 | 9.4 | 189 | 0 | 0 |
| d_cool24h | 1039 | -16,889 | -0.68 | 1.76 | -0.03 | -10.57 | 0/3 | 9.0 | 0 | 0 | 0 |
| e_age14_m15 | 1039 | -13,840 | -0.56 | 1.12 | 0.24 | -8.08 | 2/3 | 4.1 | 0 | 116 | 0 |
| combo_abcde | 1039 | -14,451 | -0.63 | 1.05 | 0.05 | -8.40 | 2/3 | 4.8 | 195 | 117 | 0 |
| rg_breadth | 728 | 1,503 | 0.09 | 1.82 | 0.51 | -6.36 | 3/3 | 3.7 | 0 | 0 | 311 |

### Per signal source, PnL USDT (tape, base)

| source | lots | baseline | a_max2adds | b_budget2x | c_breadth40 | d_cool24h | e_age14_m15 | combo_abcde | rg_breadth |
|---|---|---|---|---|---|---|---|---|---|
| auto | 111 | 3,101 | 3,591 | 4,371 | 2,196 | 3,101 | 6,360 | 5,851 | 7,080 |
| cmc | 203 | 4,109 | 2,707 | 3,852 | 1,504 | 4,109 | 370 | 25 | -443 |
| entry_sensor_15m | 154 | -17,466 | -17,930 | -18,028 | -16,221 | -17,466 | -13,892 | -13,066 | -8,422 |
| gainer_live_heat | 2 | 394 | 394 | 394 | 394 | 394 | 394 | 394 | 0 |
| gainer_rank_entry | 7 | -754 | -959 | -754 | -754 | -754 | -754 | -959 | 0 |
| gainer_relvol | 148 | -4,436 | -3,995 | -4,255 | -4,167 | -4,436 | -4,803 | -4,026 | -2,238 |
| grid | 310 | 1,501 | 2,324 | 2,122 | 780 | 1,501 | 3,545 | 2,983 | 609 |
| lc | 104 | -3,336 | -3,066 | -3,157 | -3,815 | -3,336 | -5,060 | -5,654 | 4,918 |

## Real tape (W1-W3 entries, recorded adds/exits), costs stress

| variant | lots | PnL USDT | PnL % | W1 % | W2 % | W3 % | windows won | lots <= -20 % | adds skipped | time stops | filtered |
|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 1039 | -19,450 | -0.78 | 1.67 | -0.14 | -10.69 | -/3 | 9.0 | 0 | 0 | 0 |
| a_max2adds | 1039 | -19,486 | -0.79 | 1.76 | -0.25 | -10.79 | 1/3 | 9.0 | 27 | 0 | 0 |
| b_budget2x | 1039 | -17,994 | -0.74 | 1.80 | -0.12 | -10.89 | 2/3 | 9.0 | 21 | 0 | 0 |
| c_breadth40 | 1039 | -22,525 | -0.96 | 1.43 | -0.47 | -11.11 | 0/3 | 9.4 | 189 | 0 | 0 |
| d_cool24h | 1039 | -19,450 | -0.78 | 1.67 | -0.14 | -10.69 | 0/3 | 9.0 | 0 | 0 | 0 |
| e_age14_m15 | 1039 | -16,284 | -0.66 | 1.03 | 0.13 | -8.19 | 2/3 | 4.1 | 0 | 116 | 0 |
| combo_abcde | 1039 | -16,791 | -0.73 | 0.96 | -0.07 | -8.51 | 2/3 | 4.9 | 195 | 117 | 0 |
| rg_breadth | 728 | -468 | -0.03 | 1.70 | 0.38 | -6.47 | 3/3 | 3.7 | 0 | 0 | 311 |
