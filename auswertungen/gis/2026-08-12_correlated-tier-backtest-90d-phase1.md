# Correlated-tier + stagnant-rotation — 90-Tage Phase-1 Backtest

**Fenster:** 2026-05-14 → 2026-08-12 · **Universum:** 55 Symbole (traded=50, watchlist=31, tier=6)

## Ergebnis in einem Satz

**Experiment schlägt Baseline auf 1h** (-14633.87 vs -15101.83 USDT realisiert). Das ist ein einzelner 90-Tage-Pfad, kein Sweep.

---

## 1. Universum

- Größe: **55**
- Historisch gehandelt: 50
- Aktuelle Watchlist (base + expansion): 31
- correlated_tier Proxies/Members: `BTC/USDT, CRWVG/USDT, ETH/USDT, MVLLG/USDT, NBISG/USDT, SOXLG/USDT`
- Zu wenig Historie / unlisted verworfen: 3 (CAT/USDT, TON/USDT, TRU/USDT)
- correlated_tier mit Teilhistorie behalten (zu jung für volle 90 Tage): CRWVG/USDT 494×1h / 124×4h, MVLLG/USDT 494×1h / 124×4h, NBISG/USDT 828×1h / 207×4h, SOXLG/USDT 828×1h / 207×4h
- Phantom-Testsymbole verworfen: SENSOR15/USDT

Vollständige Liste:

```
AARK/USDT, ADA/USDT, ARIA/USDT, BEAT/USDT, BNB/USDT, BTC/USDT, CRWVG/USDT, DOGE/USDT, ETH/USDT, GAS/USDT, GMRT/USDT, GNC/USDT, GORK/USDT, H/USDT, HIGH/USDT, HMSTR/USDT, HYPE/USDT, JUP/USDT, LAB/USDT, LIKE/USDT, LIT/USDT, LTC/USDT, MAGMA/USDT, MON/USDT, MVLLG/USDT, NBISG/USDT, NEAR/USDT, NUTS/USDT, NYAN/USDT, PEPE/USDT, RAVE/USDT, RIF/USDT, SIREN/USDT, SKYAI/USDT, SOL/USDT, SOXLG/USDT, SPCX/USDT, STG/USDT, SUI/USDT, TRB/USDT, TREE/USDT, TRUMP/USDT, TRX/USDT, TYCOON/USDT, U/USDT, VELVET/USDT, WLD/USDT, XAI/USDT, XPL/USDT, XRP/USDT, ZBT/USDT, ZEC/USDT
```

## 2. Headline-Zahlen

### 1h

- **Baseline** (Flags aus): `n=1308  win%=0.4411  avg=-7.59%  med=-0.81%  pnl=-15101.83 USDT  ret=-15.1%  mdd=-22.1%  sharpe≈-11.06`
- **Experiment** (Flags an): `n=1311  win%=0.4531  avg=-7.17%  med=-0.7%  pnl=-14633.87 USDT  ret=-14.63%  mdd=-22.1%  sharpe≈-10.49`
- **BTC Buy&Hold** (gleiches Fenster, gleiches Startkapital): -20.32% / -20321.23 USDT

| Gruppe | Lauf | n | Win% | Avg % | Med % | PnL USDT |
|--------|------|--:|-----:|------:|------:|---------:|
| crypto_market | baseline | 1235 | 0.4227 | -7.87 | -1.09 | -14523.56 |
| us_stock | baseline | 73 | 0.7534 | -2.93 | 3.49 | -578.27 |
| crypto_market | experiment | 1237 | 0.4325 | -7.77 | -0.97 | -14509.58 |
| us_stock | experiment | 74 | 0.7973 | 2.86 | 3.86 | -124.29 |

### 4h

- **Baseline** (Flags aus): `n=492  win%=0.3374  avg=-13.26%  med=-9.08%  pnl=-14666.03 USDT  ret=-14.67%  mdd=-19.45%  sharpe≈-10.47`
- **Experiment** (Flags an): `n=483  win%=0.3333  avg=-13.08%  med=-9.08%  pnl=-16084.88 USDT  ret=-16.08%  mdd=-20.88%  sharpe≈-10.26`
- **BTC Buy&Hold** (gleiches Fenster, gleiches Startkapital): -22.22% / -22215.78 USDT

| Gruppe | Lauf | n | Win% | Avg % | Med % | PnL USDT |
|--------|------|--:|-----:|------:|------:|---------:|
| crypto_market | baseline | 465 | 0.3312 | -13.2 | -9.08 | -14613.45 |
| us_stock | baseline | 27 | 0.4444 | -14.31 | -1.23 | -52.59 |
| crypto_market | experiment | 464 | 0.3297 | -13.24 | -9.61 | -14612.52 |
| us_stock | experiment | 19 | 0.4211 | -9.18 | -2.36 | -1472.37 |

`crypto_market` is nearly identical across passes (the group has no trail overlay knobs). The delta lives in `us_stock`: on 1h the tighter overlay turns avg −2.93% into +2.86% and cuts group PnL from −578 to −124; on 4h the same overlay cuts n 27→19 and group PnL from −53 to −1.472. No `stagnant_rotation` exits appeared — `max_open=36` with slack=2 almost never bound.

## 3. Limitations / Approximations

- Survivorship: universe is today's watchlist plus symbols that appear in surviving ledger snapshots; coins delisted and fully dropped from every snapshot are missing.
- Causality is enforced by the engine (signal at close t, fill at open t+1). End-of-window leftover positions are marked out at the last close (not a next open) so P&L is fully realized; those exits are tagged exit=end_of_window.
- Cost model matches scripts/backtest_volume_ignition_60d.py (fee_rt=0.002 + 25 bps slip + 2% participation), NOT config.slippage_percent=1.5 (that live buffer is not a fill model).
- Position sizing is a simplified ticket: min(max_usdt_per_trade, cash-cash_floor, participation*qvol). Full risk/risk_manager.py (moderate_deploy, venue_quality, adaptive cash_policy, slot eviction) is not wired in.
- entry_sensor_15m, exit_sensor and dca_sniper are disabled in BOTH in-memory copies: this engine has no 15m history and no live WS sniper. Cycle DCA (evaluate_dca_addon) remains available. The persisted config.json is unchanged.
- Social/CMC/LunarCrush/Santiment signals are not replayed (no historical feed). DecisionEngine therefore sees technical + rotation + correlated-tier overlay only.
- Correlated-tier selloff uses the real GroupDrawdownTracker on proxy OHLCV. Live windows are 10–15 minutes; we only have 1h/4h bars, so each bar is sampled as four synthetic ticks (o/h/l/c). Intra-bar path is an approximation, not tick tape.
- correlated_tier_selloff_active normally reads a Redis flag. Redis is not replayed; the historical tracker result is injected for the experiment pass (and is a no-op when enabled=false).
- Simulated clock: position peak_at/last_trade_at are stamped with bar time; sell_rotation_policy._hours_since is patched to that clock so stagnant_idle_hours is measured in simulated time, not wall-clock.
- The isolated in-memory position book is the real strategies.positions store with flush_positions no-op'd so the operator ledger is never written.
- us_stock proxies (CRWVG/NBISG/SOXLG/MVLLG) are recently listed: a 90-day since= fetch returns empty from Gate, so we fall back to the newest bars and keep them as 'tier-partial' even below the 80% coverage cut. They do not span the full window.
- No shuffled-timing control and no walk-forward / regime buckets — those are Phase 2.

## 4. Dateien

- `auswertungen/gis/correlated_tier_backtest_90d_1h_20260812_193319.json`
- `auswertungen/gis/correlated_tier_backtest_90d_4h_20260812_193405.json`
