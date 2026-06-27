# 15m-Breakout-Trigger — Implementierungsplan

**Status:** Geplant  
**Erstellt:** 2026-06-27  
**Branch-Baseline:** `main` (Mongo-Ledger, Position-Tree)

---

## Ziel

Erweiterung des bestehenden 4h-Trading-Bots um einen parallelen **15m-Breakout-Trigger** für volatile Low-Caps (ca. **5–100M USD** Market Cap), ohne die bestehende 4h-Logik umzubauen.

| Aspekt | Strategie A (`ta_4h`) | Strategie B (`breakout_15m`) |
|--------|----------------------|------------------------------|
| Chart | 4h (bzw. 1h volatile) | 15m |
| Rolle | Filter & Setup | Schneller Einstieg |
| Kapital | Eigener Pool (z.B. 60%) | Eigener Pool (z.B. 40%) |
| Positions-Key | `{symbol}_{tf}_ta_4h` | `{symbol}_15m_breakout_15m` |

**Entscheidungen (bestätigt):**

- **Daten:** Hybrid-Polling (15–30s Exchange-OHLCV für armed Coins, CMC REST für Market Cap) — kein CMC-WebSocket in Phase 1
- **Isolation:** Volle `strategy_id` im Ledger (eigene Lots, Cash, PnL)

---

## Ausgangslage (Codebase)

- **Hauptstrategie:** `strategies/technical_rsi_bb.py` via `strategies/decision_engine.py` auf einem effektiven Timeframe (`4h` oder `1h` für volatile Coins) — `strategies/registry.py` → `resolve_effective_timeframe()`
- **Position-Key heute:** `{symbol}_{timeframe}` in `strategies/positions.py` — kein `strategy_id`, ein gemeinsamer Cash-Pool in `trade_history`
- **OHLCV:** ccxt REST in `services/market_service.py`; `15m` ist in `_TF_HOURS` vorbereitet, aber nirgends konfiguriert
- **CMC:** nur REST (`data/cmc_trending_provider.py`, `data/cmc_volatile_signals.py`); kein WebSocket
- **A/B-Muster:** Shadow-Buckets wie `strategies/time_profit_exit.py` — nur Signal-A/B, keine Kapital-Trennung
- **Multi-Timeframe:** Kein „4h-Filter → 15m-Entry“-Pfad vorhanden

---

## Zielarchitektur

```
┌─────────────────────────────────────────────────────────────┐
│  Hauptzyklus (120s)                                         │
│  Watchlist + CMC REST → Market Cap 5–100M → 4h Strategie A│
│  Bei Buy-Setup → armed_coins setzen                         │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│  Fast Loop (15–30s) — nur armed Coins                       │
│  15m OHLCV + Live-Preis → Breakout-Engine Strategie B       │
│  → TradingService (strategy_id=breakout_15m)                │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│  Mongo Ledger                                               │
│  orders.strategy_id · positions.strategy_id                 │
│  trade_history.portfolios.{strategy_id}                     │
└─────────────────────────────────────────────────────────────┘
```

---

## Phase 1 — Ledger-Fundament: `strategy_id`

Position-Key erweitern: `{symbol}_{timeframe}_{strategy_id}`

| Bereich | Dateien | Änderung |
|---------|---------|----------|
| Position-Key | `strategies/positions.py` | `get_key(symbol, tf, strategy_id="ta_4h")`; Migration bestehend → `ta_4h` |
| Orders | `services/order_service.py`, `storage/mongo_ledger.py` | Feld `strategy_id` + Indizes |
| Ledger-Sync | `services/ledger_sync.py` | `derive_positions_from_orders` gruppiert nach `strategy_id` |
| Portfolio/Cash | `data_manager.py`, `services/portfolio_service.py` | `trade_history.portfolios.{id}.virtual_balance` |
| Risk | `risk/risk_manager.py` | Limits pro Strategie-Pool |
| Execution | `services/trading_service.py`, `core/models.py` | `strategy_id` in `TradeOrder` + Idempotency |
| Reporting | `notifications/telegram_commands/position_display.py`, `notifications/terminal_dashboard.py` | Filter/Gruppierung A vs B |
| Config | `config.json` | Block `strategy_portfolios` |

**Migration:** `scripts/migrate_strategy_id.py` — alle Orders/Positions → `ta_4h`, Cash 100% → `ta_4h`.

**Tests:** Erweiterung `tests/unit/test_mongo_backend.py`, neu `test_strategy_portfolios.py`.

---

## Phase 2 — Market-Cap-Filter (5–100M USD)

Neues Modul `data/cmc_market_cap.py`:

- `GET /v1/cryptocurrency/quotes/latest` (batch, TTL 15 min)
- `is_eligible_low_cap(symbol)` für $5M–$100M
- Cache: `market_cap_usd`, `market_cap_tier` auf Watchlist-Coins

Integration: `services/dry_run_watchlist.py`, `intelligence/volatility_classifier.py`

Config: `breakout_15m.market_cap_min_usd` / `market_cap_max_usd`

---

## Phase 3 — 4h-Armed-Gate

Neues Modul `strategies/armed_state.py` — persistiert `armed_coins` (Mongo/JSON):

**Arming-Bedingung:**

1. Market Cap 5–100M (Phase 2)
2. Strategie A: `BUY` / `BUY_STRONG` auf 4h (oder `setup_zone`: volatile tier + RSI in Kaufzone)
3. Keine offene 15m-Position (`breakout_15m`) für dasselbe Symbol
4. Optional: ATR-Tier `volatile`

**Disarming:** TTL (z.B. 24h), 4h-Sell von A, oder manuell.

Hook: `services/signal_orchestrator.py` nach 4h-`analyze()` → `arm_coin(symbol)`.

---

## Phase 4 — 15m-Breakout-Strategie (Strategie B)

Neue Dateien:

- `strategies/breakout_15m.py` — Strategie-Klasse
- `strategies/decision_engine_15m.py` — schlanker Engine ohne Social-Merge
- Registrierung in `strategies/registry.py`

**Indikatoren** (Erweiterung `services/market_service.py`):

- `ema9` (talib.EMA period=9)
- `vol_avg_20` — 20-Perioden-Volumen-Durchschnitt
- `swing_low(n=5)` — Minimum der letzten 3–5 Kerzen-Lows
- Optional: RSI(14), HH/HL-Bestätigung

**Entry:**

```
close > ema9  (Breakout dieser Kerze)
AND close_prev <= ema9_prev
AND volume > vol_spike_mult * vol_avg_20   (default 1.8–2.5x)
AND coin is armed
AND strategy_id == "breakout_15m"
```

**Fakeout-Filter:**

- Mindest-Kerzenkörper vs. ATR (`min_body_atr_ratio`)
- Up-Volume bestätigt Close-Richtung
- Cooldown nach Stop-Out (z.B. 2h)
- Kein Entry wenn 4h-RSI > 75

**Stop-Loss (einmalig beim Entry):**

- `stop = clamp(swing_low_5, entry - atr_mult*ATR, stop_min_pct..stop_max_pct)`
- In Position: `stop_price`, `stop_pct`

**Exit:**

- Primär: `close < ema9` auf 15m → `SELL_FULL`
- Zeit-Stop: 48h → `SELL_FULL`
- Optional später: TP 1:2 / 1:3 RR, Trailing-EMA

---

## Phase 5 — Fast-Polling-Loop (Hybrid-Datenversorgung)

Neuer Service `services/fast_trigger_loop.py`:

- Daemon-Thread, Intervall **15–30s** (`breakout_15m.poll_interval_sec`)
- Nur `armed_coins` (0–15 Symbole)
- Pro Coin: `fetch_ohlcv(symbol, "15m", limit=50)` + Live-Preis aus `price_fetcher.py`
- `Breakout15mEngine.evaluate()` → `TradingService.execute_order(strategy_id="breakout_15m")`
- Rate-Limit via ccxt `enableRateLimit`

Start in `aria_bot.py` parallel zu `price_loop` (120s).

**Kein CMC-WebSocket in Phase 1** — optional Phase 8 bei Startup+-Plan.

---

## Phase 6 — Performance-Vergleich A vs B

| Metrik | Quelle |
|--------|--------|
| Realized PnL | `trade_history.portfolios.{id}.realized_pnl` |
| Unrealized | Positions × Mark pro `strategy_id` |
| Win rate / avg hold | Orders-Replay |
| Max drawdown | `services/strategy_performance.py` (neu) |

Telegram: `/strategies` oder Erweiterung `/positions`  
Daily: Side-by-Side in `notifications/daily_portfolio.py`

---

## Phase 7 — VELVET-Validierung (14–20 Tage)

Script `scripts/backtest_breakout_15m.py`:

1. OHLCV: `VELVET/USDT` 15m + 4h, 20 Tage (ccxt)
2. Replay: 4h-Arming + 15m-Entry/Exit/Stop
3. Output: Timestamps Entry, Stop, EMA-Exit, 48h-Exit
4. Vergleich: nur 4h-Strategie A im gleichen Zeitraum

Tests: `tests/unit/test_breakout_15m.py` (synthetische Breakout/Fakeout-Szenarien)

**Beispiel-Move:** VELVET 10.–12. Juni — früherer Entry vs. Fakeout-Rate dokumentieren.

---

## Config-Entwurf

```json
"strategy_portfolios": {
  "ta_4h":        { "initial_capital_pct": 0.60, "max_open_positions": 20 },
  "breakout_15m": { "initial_capital_pct": 0.40, "max_open_positions": 10 }
},
"breakout_15m": {
  "enabled": true,
  "mode": "shadow",
  "timeframe": "15m",
  "poll_interval_sec": 20,
  "market_cap_min_usd": 5000000,
  "market_cap_max_usd": 100000000,
  "arming_mode": "buy_signal",
  "arming_ttl_hours": 24,
  "vol_spike_mult": 2.0,
  "ema_period": 9,
  "vol_avg_period": 20,
  "stop_swing_lookback": 5,
  "stop_min_pct": 15,
  "stop_max_pct": 35,
  "time_stop_hours": 48,
  "fakeout_min_body_atr_ratio": 0.3,
  "cooldown_after_stop_hours": 2
}
```

**Rollout:** `mode: "shadow"` → 1 Woche Signale loggen → `mode: "active"`.

---

## PR-Stack (Implementierungsreihenfolge)

| PR | Inhalt | Risiko |
|----|--------|--------|
| PR1 | `strategy_id` end-to-end + Migration | Hoch |
| PR2 | CMC Market Cap 5–100M | Niedrig |
| PR3 | Armed-State + 4h-Hook | Mittel |
| PR4 | `breakout_15m` Strategie + Stops/Exits | Mittel |
| PR5 | Fast Loop 15–30s | Mittel (API-Rate) |
| PR6 | Performance Reporting A vs B | Niedrig |
| PR7 | VELVET-Backtest + Unit-Tests | Niedrig |

Abhängigkeiten: PR1 → PR2 → PR3 → PR4 → PR5 → PR6; PR4 → PR7 parallel möglich.

---

## Design-Entscheidungen

1. **Gleicher Coin, zwei Strategien:** Erlaubt — getrennte Lots und Cash-Pools.
2. **4h-Logik unverändert:** Strategie A bleibt in `DecisionEngine`; B hat eigenen Engine.
3. **Stop 15–35%:** Clamp um ATR-basierten Swing-Low, nicht fix.
4. **Fakeouts:** Volumen + Kerzenkörper + Cooldown; kein Entry ohne Armed-State.
5. **15m-Reaktionszeit:** 20s Poll reicht (Kerze 15 min; forming candle + Live-Preis).

---

## Erfolgskriterien

- Strategie A verhält sich identisch zu heute (Regression grün)
- Strategie B tradet nur armed + Market-Cap-eligible Coins
- `/positions` und Daily Report zeigen getrennte PnL
- VELVET-Backtest dokumentiert Entry/Exit vs. 4h-Baseline
- `assert_safe_demo_mongo_db()` bleibt aktiv (Demo → `xagent_test` only)

---

## Referenzen

- Bestehende Strategie: `strategies/technical_rsi_bb.py`
- A/B-Muster: `strategies/time_profit_exit.py`
- OHLCV: `services/market_service.py`
- Positionen: `strategies/positions.py`
- Railway (separat): `docs/RAILWAY_PLAN.md`