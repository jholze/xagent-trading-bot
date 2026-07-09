# 15m Entry Sell-Guard — Plan v3 (Backtest-first)

## Branch-Strategie

| Was | Branch |
|-----|--------|
| **Alles Phase 0–2** | `feature/entry-guard-15m` (neu, von `main`) |
| **Prod / Railway** | bleibt auf `main` — kein Merge bis Backtest-Go + dein OK |
| **Nicht nutzen** | `feature/entry-sensor-15m` — veraltet (hinter `main`, divergiert bei `7f89ff9`) |

**Workflow:**
1. Uncommitted WIP (`market_structure` grace) auf Feature-Branch stashen/committen — nicht auf `main` lassen
2. Phase 0 Backtest-Script nur auf Feature-Branch
3. PR/Stack: `feature/entry-guard-15m` → `main` erst nach Report + lokaler Verifikation

---

## Gate: Erst validieren, dann implementieren

**Kein Prod-Code** bis Backtest die beste Variante liefert.

---

## Phase 0 — Backtest & Parameter-Suche (PFLICHT)

### Vorhandene Bausteine nutzen

| Tool | Rolle |
|------|-------|
| [`scripts/backtest_exit_rules_30d.py`](scripts/backtest_exit_rules_30d.py) | Muster: Demo-Ledger → Lots → OHLCV-Replay → PnL-Vergleich |
| [`historical_prices._fetch_ohlcv_range`](historical_prices.py) | 15m + 1h/4h Kerzen von Gate |
| [`hermes/pipeline_backtest.py`](hermes/pipeline_backtest.py) | Bar-by-bar `DecisionEngine` mit `window_metrics_only` |
| [`services/market_service.compute_15m_sensor_metrics`](services/market_service.py) | Dieselben Vol-Metriken wie Live-Entry |
| [`scripts/verify_entry_sensor_15m.py`](scripts/verify_entry_sensor_15m.py) | Harness für Sensor→Engine |
| Mongo `orders` (`source=entry_sensor_15m`) | 29 reale Entry-Events |

### Neues Script: `scripts/backtest_entry_guard_15m.py`

**Input:** Demo-Ledger (Mongo oder `orders.demo.json`), letzte 30–60 Tage, Filter `entry_sensor_15m` BUYs

**Pro Entry-Lot:**
1. `entry_ts`, symbol, tf, entry_price, entry_usdt
2. OHLCV 15m + 4h für `[entry_ts - 4h, entry_ts + 48h]`
3. Bar-by-bar (15m-Schritte): 15m-Metrics + 4h-Indikatoren zum Zeitpunkt
4. Simuliere Sell-Kandidaten (`bb_upper`, `vol_exhaustion`, trailing) **mit und ohne Guard**

**Guard-Varianten (Grid):**

| ID | Beschreibung |
|----|--------------|
| `baseline` | Kein Guard (heutiges Verhalten) |
| `arch_only` | Nur Loop-Guard (kein Sell aus 15m-Pfad) — hypothetisch |
| `time_tier` | min_hold_minutes + min_gain nach Tier (ohne 15m-State) |
| `pump_15m` | 15m continuation/exhaustion State |
| `combo` | arch + pump_15m + tier fallback |

**Parameter-Sweep** (Beispiel):
- `vol_spike_mult`: 1.8, 2.0, 2.5
- `vol_exhaustion_15m_max`: 0.75, 0.85, 0.95
- `mega_pump_gain_pct`: 10, 12, 15
- `min_hold_minutes` (meme/volatile): 30, 45, 60

**Metriken pro Variante:**

| Metrik | Ziel |
|--------|------|
| `whipsaw_count` | Sells <60min nach Entry mit PnL < +2% → **minimieren** |
| `whipsaw_loss_usd` | Summe realisierte Verluste aus Whipsaws → **minimieren** |
| `missed_profit_usd` | Blockierte Sells die >+8% Gain hätten → **nicht zu hoch** |
| `net_pnl_delta` | Simuliert vs Actual economic → **maximieren** |
| `median_hold_before_first_sell` | Soll >30min bei continuation |

**Regression-Fixture:** DOGE 17:13 BUY / 17:15 BB-Sell (aus Ask-History) — muss von Gewinner-Variante geblockt werden.

**Output:** Markdown/JSON Report `auswertungen/entry_guard_backtest_YYYYMMDD.md` mit Ranking.

### Erfolgskriterien (Go für Phase 1)

- Whipsaw-Rate −80% vs baseline auf 15m-Entries
- `net_pnl_delta >= 0` (nicht schlechter als actual)
- `missed_profit_usd` < 15% der Whipsaw-Einsparung
- DOGE-Fixture: blockiert
- Mega-Pump-Szenario (synthetisch): Sell bei +15% nach 35min **erlaubt**

---

## Phase 1 — Implementierung (nur nach Go)

Gewinner-Config aus Phase 0 → Code wie in v2:

1. `strategies/entry_guard.py` (15m pump state)
2. `process_entry_sensor()` Arch-Split
3. Position tagging `entry_source`
4. `_merge_sell` filter
5. Unit tests spiegeln Backtest-Szenarien

---

## Phase 2 — Lokal verifizieren

- `pytest` + Dev-Bot
- **Kein Railway**

---

## Nicht tun

- Kein Prod-Deploy ohne Backtest-Go
- Keine stumpfe 8h-Sell-Sperre
- Stop-Loss immer frei