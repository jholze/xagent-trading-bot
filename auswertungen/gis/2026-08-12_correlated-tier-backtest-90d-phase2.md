# Correlated-tier + stagnant-rotation — 90-Tage Phase-2 Rigor

**Fenster:** 2026-05-14 → 2026-08-12 · Phase-1 Reports unverändert (kein Re-Score der 90d-Headlines).  
**config.json:** nicht geschrieben, Flags bleiben `correlated_tier.enabled=false` und `stagnant_rotation_enabled=false`.

## Ergebnis in einem Satz

**Leave the current config.json defaults off.** The Phase-1 1h us_stock lift does not survive a timing control, is invisible outside a single chop stretch that coincides with a 20–34 day listing window, and the 4h 90-day path still loses badly. This is too noisy / regime-specific to trust.

---

## 0. us_stock sample size (nicht 90 Tage)

CRWVG/MVLLG sind erst seit ~20.5 Tagen gelistet, NBISG/SOXLG seit ~34.5. Jede Zahl, die so tut als wäre das ein volles 90-Tage-Sample, ist falsch.

| Symbol | TF | Bars | First | Last | Effective days | Window coverage |
|--------|----|-----:|-------|------|---------------:|----------------:|
| CRWVG/USDT | 4h | 124 | 2026-07-23T04:00 | 2026-08-12T16:00 | 20.5 | 0.2278 |
| MVLLG/USDT | 4h | 124 | 2026-07-23T04:00 | 2026-08-12T16:00 | 20.5 | 0.2278 |
| NBISG/USDT | 4h | 207 | 2026-07-09T08:00 | 2026-08-12T16:00 | 34.33 | 0.3815 |
| SOXLG/USDT | 4h | 207 | 2026-07-09T08:00 | 2026-08-12T16:00 | 34.33 | 0.3815 |
| CRWVG/USDT | 1h | 494 | 2026-07-23T06:00 | 2026-08-12T19:00 | 20.54 | 0.2282 |
| MVLLG/USDT | 1h | 494 | 2026-07-23T06:00 | 2026-08-12T19:00 | 20.54 | 0.2282 |
| NBISG/USDT | 1h | 828 | 2026-07-09T08:00 | 2026-08-12T19:00 | 34.46 | 0.3829 |
| SOXLG/USDT | 1h | 828 | 2026-07-09T08:00 | 2026-08-12T19:00 | 34.46 | 0.3829 |

## 1. Shuffled-timing control

Gleiche Coins, gleiches Buy-Count-Ziel, gleicher Cost/Capacity-Rahmen — Einstiegszeitpunkt je Symbol zufällig (seed fest). Trennt 'das Signal timed richtig' von 'diese Coins liefen sowieso'.

| TF | Lauf | n | Avg % | PnL USDT | us_stock PnL | us_stock avg % |
|----|------|--:|------:|---------:|-------------:|---------------:|
| 4h | baseline | 492 | -13.26 | -14666.03 | -52.59 | -14.31 |
| 4h | experiment | 483 | -13.08 | -16084.88 | -1472.37 | -9.18 |
| 4h | shuffled | 141 | -6.42 | -8124.71 | -155.28 | 2.86 |
| 4h | BTC buy&hold |  | -22.22 | -22215.78 |  |  |
| 1h | baseline | 1308 | -7.59 | -15101.83 | -578.27 | -2.93 |
| 1h | experiment | 1311 | -7.17 | -14633.87 | -124.29 | 2.86 |
| 1h | shuffled | 191 | -4.22 | -10551.7 | 37.84 | 14.53 |
| 1h | BTC buy&hold |  | -20.32 | -20321.23 |  |  |

- **4h timing-edge** (experiment − shuffled) PnL `-7960.17` USDT / avg `-6.66` pp. seed=42 planned=195 target_buys=195, **filled n=141** (capacity/liquidity dropped most random entries).
- **1h timing-edge** (experiment − shuffled) PnL `-4082.17` USDT / avg `-2.95` pp. seed=42 planned=456 target_buys=456, **filled n=191**.
- Fairer als die PnL-Summe (ungleiche n) ist **avg %**: shuffled 1h −4.22% vs experiment −7.17%; 4h −6.42% vs −13.08%. Zufälliges Timing war im Schnitt besser. us_stock 1h shuffled avg **+14.53%** vs experiment **+2.86%** — die Coins liefen, unser Signal-Timing hat das nicht besser eingefangen als Zufall.

## 2. Regime buckets (post-hoc, 7d BTC-Return)

Schwellen (fest, nicht gefittet): **risk_off** = 7d-BTC < −10%, **chop** = −10% … +5%, **risk_on** = > +5%. Kein neuer Full-Universe-90d-Lauf — Join gegen BTC-Cache + us_stock-only tape.

Kalender (1 Punkt/Tag über das 90d-Fenster): unknown_bucket=6, chop_bucket=69, risk_off_bucket=7, risk_on_bucket=8 (n_days=90).

| TF | Bucket | base n | exp n | base us_stock PnL | exp us_stock PnL | Δ PnL |
|----|--------|-------:|------:|------------------:|-----------------:|------:|
| 4h | chop_bucket | 31 | 23 | -699.32 | -1405.38 | -706.06 |
| 4h | risk_off / risk_on | 0 | 0 | — | — | kein us_stock-Sample |
| 1h | chop_bucket | 85 | 91 | -464.92 | 58.5 | 523.42 |
| 1h | risk_off / risk_on | 0 | 0 | — | — | kein us_stock-Sample |

Der 1h-hilft / 4h-schadet-Effekt sitzt **ausschließlich in chop**, und chop ist das einzige Regime, das mit dem Listing-Fenster (ab 9. bzw. 23. Juli) überlappt. risk_off (7 Tage) und risk_on (8 Tage) im 90-Tage-Kalender liegen *vor* den Listings. Das ist kein Cross-Regime-Test — es ist ein einzelner Stretch. Dieselbe Warnung, die das Team bei anderen Features gezogen hat: ein einzelner Stretch ist kein Promotion-Grund.

## 3. Parameter-Sweep (1h, one-at-a-time)

| Point | n | Δ PnL vs *its* baseline | us_stock Δ | stagnant n | peak_open |
|-------|--:|------------------------:|-----------:|-----------:|----------:|
| Phase-1 current (trail 3.5 / fc 12, max_open=36) | 1311 | +467.96 | +453.98 | 0 | 36 |
| trail_pct=2.5 | 1311 | +468.98 | +454.81 | 0 | 36 |
| trail_pct=5.0 | 1311 | +474.91 | +460.75 | 0 | 36 |
| full_close_gain_pct=10 | 1312 | +446.22 | +433.10 | 0 | 36 |
| full_close_gain_pct=15 | 1313 | +603.28 | +578.02 | 0 | 36 |
| tight_maxopen18_slack2 | 411 | +375.19 | +13.27 | 0 | 18 |
| tight_maxopen18_slack8 | 411 | +375.19 | +13.27 | 0 | 18 |
| tight_maxopen18_slack8_gain6_idle12 | 383 | +2469.75 | −59.49 | **1** | 18 |

- Overlay-Knobs (trail 2.5/5.0, full_close 10/15) bewegen 1h us_stock Δ kaum gegenüber dem Phase-1-Punkt (~+454). `full_close_gain_pct=15` ist die einzige leichte Verbesserung (~+578), in der Größenordnung immer noch ein 20–34-Tage-Sample.
- `stagnant_rotation` hat in Phase 1 nie gefeuert (`max_open=36`, slack=2, und der Sim-Clock hat `peak_at` jede Bar neu gestempelt). Phase-2 tight-book mit `peak_stamp=on_progress`: slack=8 allein bei 8%/24h reicht **nicht**. Erst `gain=6 / idle=12 / slack=8 / max_open=18` feuert **einmal**. Gesamtes Buch-PnL gegen diese Tight-Baseline +2469 USDT (Rotation räumt crypto_market), us_stock selbst −59. Ein Fire ist kein Tuning-Ergebnis.
- `tight_maxopen12_…` wurde nicht mehr gelaufen (stagnant war beobachtet; Sweep-Cap).

## 4. Walk-forward folds (Hermes-Konvention)

Nur Fold 2 hat ein echtes us_stock-Sample (Listing). Fold 0: Tokens existieren nicht. Fold 1: NBISG/SOXLG gerade gelistet, Overlay ändert den Pfad nicht (Δ=0). **folds-won 1/2 ist also 1/1 echte Folds** — das ist kein Generalisierungs-Check, nur der Listing-Stretch selbst.

Auffällig gegen Phase 1: im frischen 4h-Fold 2 (13.07.–12.08.) schlägt Experiment Baseline auf us_stock (+482 vs −144). Derselbe Overlay auf dem *durchlaufenden* 90-Tage-4h-Pfad macht us_stock −1472 vs −53. Pfadabhängigkeit (Cash/Slots aus Mai–Juli, andere Entry-Menge) — kein stabiles 4h-Signal.

### 4h — folds won **1/2** (nur Folds mit us_stock-Sample; total folds=3)

| Fold | Window | us_stock n (b/e) | base PnL | exp PnL | Δ | beat? |
|-----:|--------|-----------------:|---------:|--------:|--:|:-----:|
| 0 | 2026-05-14 → 2026-06-13 | 0/0 | 0.0 | 0.0 | 0.0 | no |
| 1 | 2026-06-13 → 2026-07-13 | 2/2 | -249.76 | -249.76 | 0.0 | no |
| 2 | 2026-07-13 → 2026-08-12 | 22/22 | -144.22 | 481.61 | 625.83 | yes |

### 1h — folds won **1/2** (nur Folds mit us_stock-Sample; total folds=3)

| Fold | Window | us_stock n (b/e) | base PnL | exp PnL | Δ | beat? |
|-----:|--------|-----------------:|---------:|--------:|--:|:-----:|
| 0 | 2026-05-14 → 2026-06-13 | 0/0 | 0.0 | 0.0 | 0.0 | no |
| 1 | 2026-06-13 → 2026-07-13 | 9/9 | 5.63 | 5.63 | 0.0 | no |
| 2 | 2026-07-13 → 2026-08-12 | 52/55 | -1105.12 | -943.92 | 161.2 | yes |

## 5. Verdict

**Keep `sell_policy.correlated_tier.enabled=false` and `stagnant_rotation_enabled=false`.** Do not adjust trail_pct / full_close_gain_pct in config.json either.

Why, without softening it:

1. **Timing control fails.** Avg-% of the shuffled pass beats the experiment on both timeframes. us_stock 1h random entries (+14.5% avg) beat the real signal (+2.9% avg). These coins moved; our entries were not the reason.
2. **No cross-regime evidence.** us_stock only exists in the last 20–34 days, which sit entirely inside `chop_bucket`. The 1h-helps / 4h-hurts pattern is one stretch, not a regime-robust effect. risk_off and risk_on never saw these tokens.
3. **Walk-forward is 1/1, not 3/3.** Fold 0 has zero us_stock. Fold 1 does not diverge. Only fold 2 (the listing window) shows an experiment win — and even there 4h-fresh-fold vs 4h-full-90d disagree in sign. Path artifact.
4. **stagnant_rotation is still a ghost under production knobs.** It fired once, only after we shrank the book to 18, raised slack to 8, and eased gain/idle to 6%/12h. That one fire helped the *crypto* book, not us_stock (−59). Production is max_open=36 / slack=2 / 8%/24h.
5. **Sweep of the overlay knobs is flat.** trail 2.5 / 3.5 / 5.0 and full_close 10 / 12 are interchangeable. full_close=15 is a slightly prettier 1h number on the same short sample — not a reason to ship.

Phase 1’s 1h us_stock print (−2.93% → +2.86% avg) was real on that path and is still the best-looking single number. Phase 2 says it is a short-sample chop-window result that does not generalize and whose timing does not beat chance. Leave the flags off until these four tokens have a full-window history and a shuffled/walk-forward pass that actually has more than one scorable fold.

## 6. Was wurde begrenzt / warum

- Sweep cap = 12 simulation passes, 1h only, one dimension at a time (not a cartesian grid). **Used 8.** Last tight point (`max_open=12`) skipped because stagnant had already fired once.
- Walk-forward = 30d folds / 30d step → 3 non-overlapping folds, both timeframes. Not a dense overlapping set.
- Shuffled pass uses `decision_fn` (no DecisionEngine) so it stays cheap. Hold lengths come from Phase-1 BUY→SELL signal pairs.
- Regime P&L uses a us_stock-only replay (4 members + BTC/ETH) rather than a second full 52-coin 90d harvest. Phase-1 JSON had stripped the trade tape.
- Phase 1 90d two-pass was not re-run; headlines above come from the existing Phase-1 JSON.
- Sweep ran on 1h only; 4h overlay/stagnant grid was not repeated.
- Wall time for the whole Phase-2 run: **~11 minutes** (cache hit, no network).

## 7. Limitations

- Phase 1 headlines stand. This file does not re-score the 90-day 1h/4h two-pass.
- Shuffled-timing reuses the Phase-1 cost model and capacity knobs. Entries are random valid bars per symbol (seeded); exits use that symbol's paired BUY→SELL signal hold, not DecisionEngine — this isolates entry timing from the overlay.
- Phase 1 JSON stripped the trade tape (strip_trades). Per-trade P&L-by-regime is recovered from a cheap us_stock-only replay (4 members + BTC/ETH proxies) on cached OHLCV, plus a full-universe walk-forward time-slice. That us_stock-only tape does not compete for the 36-slot book, so fill counts can differ from Phase 1; the overlay path is the same.
- Regime label = 7-day rolling BTC close-to-close return: < -10% risk_off_bucket, -10%..+5% chop_bucket, > +5% risk_on_bucket. Thresholds are stated, not fitted.
- us_stock tokens are recently listed (CRWVG/MVLLG ~20.5d, NBISG/SOXLG ~34.5d inside the 90d window). Early folds have no us_stock sample — folds-won is reported among folds that actually traded the group.
- Parameter sweep is one dimension at a time, 1h only, capped. Overlay-knob points keep Phase-1 peak_at stamping (every_bar) so they stay comparable to Phase 1. Tight-book points stamp peak_at only on a genuine new high so stagnant idle can accumulate.
- Walk-forward follows hermes/validation.py rolling_folds: half-open [start, start+fold_days), step_days forward. Default 30d/30d → 3 non-overlapping folds.
- config.json is never written. All flag/knob changes are in-memory deep copies.

## 8. Dateien

- `auswertungen/gis/correlated_tier_backtest_90d_phase2_4h_20260812_200917.json`
- `auswertungen/gis/correlated_tier_backtest_90d_phase2_1h_20260812_200917.json`
- `auswertungen/gis/2026-08-12_correlated-tier-backtest-90d-phase2.md`
- Phase 1 (unverändert): `auswertungen/gis/correlated_tier_backtest_90d_1h_20260812_193319.json`, `…_4h_20260812_193405.json`, `auswertungen/gis/2026-08-12_correlated-tier-backtest-90d-phase1.md`
