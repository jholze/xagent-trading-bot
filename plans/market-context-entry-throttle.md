# Market Context & Entry Throttle — Ticket / Plan

> **Status:** open (ticket)  
> **Branch (geplant):** `feature/market-context` von `staging`  
> **Deploy:** erst nach Review + explizitem Go (Staging dry-run, dann Prod)  
> **Kontext:** Analyse 2026-07-17 — Bot investiert nach Frischstart / in roten Märkten zu schnell, weil er pro Coin denkt, nicht pro Marktumgebung.

---

## Problem

1. **Restart-Burst:** Nach Deploy/Restart leeren sich In-Memory-Cooldowns; Meta-Seed + Entry-Sensor evaluieren die Watchlist parallel → viele Käufe in kurzer Zeit.
2. **Entry-Sensor opportunistisch:** `mode: active`, Vol-Spike ≥2×, kein Pflicht-EMA-Breakout; kann `HOLD` → `BUY` ohne starkes 4h-TA-Buy.
3. **Regime zu schwach angebunden:** `regime_detector` + `strategy_allocator` laufen pro Coin; `exposure_multiplier` landet in `strategy_params`, **RiskManager sized/blockt damit nicht**. Kein globaler „Markt rot → weniger Entries“-Gate.
4. **Dry-run-Limits aggressiv:** hohe `max_daily_buys`, lockerere RSI/Vol-Defaults — Demo-Durchsatz, kein Anlaufmodus.

**Symptom:** Morgens risk-off / negativer Tape → Bot steckt trotzdem schnell Kapital in lokale Spikes (Meme/Trending).

---

## Ziel

Bot **marktumgebungs-sensibel** steuern:

| Fähigkeit | Nutzen |
|-----------|--------|
| Globale Markt-Ampel | RISK_ON / NEUTRAL / RISK_OFF / CRASH |
| Startup Warm-up | Kein Burst in den ersten N Minuten |
| Gate vor neuen Buys | Sensor + TA-Entries gedrosselt oder geblockt |
| Exposure anbinden | `exposure_multiplier` wirklich in Size |
| Sichtbarkeit | `/market` oder Telegram-Zeile: was der Bot „fühlt“ |

**Nicht-Ziele (dieses Ticket):** Grid-Logik umbauen, Multi-Tenant neu, Live-Gate-Orders, Strategy-Shadow abschalten.

---

## Ist-Stand (Code / Config)

| Baustein | Ort | Heute |
|----------|-----|--------|
| Entry-Sensor 15m | `strategies/entry_sensor_15m.py`, `config.entry_sensor_15m` | `active`, Spike 2.0× |
| Regime pro Coin | `intelligence/regime_detector.py` | enabled |
| Allocator | `intelligence/strategy_allocator.py` | DOWNTREND → defensive + exposure 0.4 |
| Mode-Map | `strategies/trading_modes.py` | DEFENSIVE aus defensive_mode |
| Exposure in Risk | `risk/risk_manager.py` | **nicht** gelesen |
| BTC-Helper | `MarketService.btc_underperformance_ratio` / `btc_relative_return_delta` | DCA / Exit, nicht globaler Entry-Gate |
| Meta-Seed | `services/eval_queue_runtime.seed_meta_producers` | Watchlist-Burst nach Start |
| Cash-Floor / Slots | `risk` + `max_open_positions` | Kapazität, kein Timing |

---

## Lösungsskizze: Market Context Layer

```text
Global Market State (alle 5–15 min cachen)
  ├─ BTC: 4h/1d Trend, 24h-Return, optional ATR
  ├─ Breadth: Anteil Watchlist rot / Median 24h (optional P2)
  ├─ optional Fear&Greed / Total MCap (wenn Quelle stabil)
  └─ State: RISK_ON | NEUTRAL | RISK_OFF | CRASH

→ steuert:
  - Entry-Sensor (shadow / off / strengere Mults)
  - max neue Buys pro Stunde / nach Restart
  - Size-Multiplikator (exposure_mult an Risk)
  - nur DEFENSIVE / keine neuen Entries bei CRASH
  - Warm-up: N Minuten nach Prozessstart nur beobachten
```

---

## Phases

### P0 — Sofort (Config only, kein Code-Zwang)

Manuell an roten Tagen / nach Deploy:

| Knob | Vorschlag RISK_OFF |
|------|--------------------|
| `entry_sensor_15m.mode` | `"shadow"` |
| `entry_sensor_15m.vol_spike_mult` | `2.5`–`3.0` |
| `entry_sensor_15m.require_ema_breakout` | `true` |
| `entry_sensor_15m.block_buy_if_rsi_4h_above` | `60`–`65` |
| `dry_run_defaults.max_daily_buys` / `risk.max_daily_buys` | drosseln |
| `max_usdt_per_trade` | optional runter |

**Done when:** Ops-Runbook 5 Zeilen in diesem Plan oder `DOCUMENTATION.md` (optional).

---

### P1 — Warm-up + BTC/Market-Gate (Kern)

**1. Startup warm-up**

- Config z.B. `market_context.warmup_sec` (default 1800) + `warmup_max_new_buys` (0 oder 1–2).
- Ab `process start` (monotonic): neue **Entries** blocken oder hart limitieren (DCA/Sell unberührt).
- Quelle: Flag in Runtime / `aria_bot` start time; Enforcement in `RiskManager` oder vor Execute in TradingService.
- Multi-tenant: warm-up **prozessglobal** oder pro Tenant — Entscheidung: prozessglobal reicht für Railway-Restart.

**2. Global Market State**

- Modul z.B. `intelligence/market_context.py` (pure + cache).
- Inputs (Minimum):
  - BTC 24h % und/oder 4h Trend (reuse MarketService / OHLCV).
  - Optional: median 24h der aktiven Watchlist.
- Output: `MarketState` dataclass + `size_mult`, `block_new_entries: bool`, `sensor_mode_override`, `rationale`.
- Config `market_context`:

```json
"market_context": {
  "enabled": true,
  "refresh_sec": 300,
  "warmup_sec": 1800,
  "warmup_max_new_buys": 0,
  "btc_risk_off_24h_pct": -3.0,
  "btc_crash_24h_pct": -6.0,
  "risk_off_size_mult": 0.35,
  "risk_off_block_sensor_entries": true,
  "crash_block_all_new_entries": true
}
```

**3. Enforcement-Punkte**

| Pfad | Verhalten RISK_OFF | CRASH |
|------|--------------------|-------|
| Entry-Sensor → BUY | shadow oder block | block |
| TA/Social BUY (kein Sensor) | size × mult | block optional |
| DCA / Recovery | size × mult oder unverändert (Policy) | Policy |
| Sells / Stops | **nie** blocken | nie blocken |

**4. Tests**

- Unit: state mapping (BTC −1 / −4 / −8 → NEUTRAL / RISK_OFF / CRASH).
- Unit: warm-up blocks first N minutes.
- Unit: sells still approved under CRASH.
- Optional: DecisionEngine smoke mit mocked market_context.

**Done when:** Staging dry-run: nach Redeploy 30 min keine Sensor-Burst-Käufe; bei simuliertem RISK_OFF kleinere/keine neuen Entries; Sells laufen.

---

### P2 — Exposure anbinden + Breadth

1. **`exposure_multiplier` in Risk sizing**  
   - Order size × `min(regime_exposure, market_context.size_mult)`.  
   - Quelle: `strategy_params` am TradeOrder oder RiskManager liest MarketState.
2. **Breadth**  
   - z.B. wenn >70 % der Watchlist 24h < −5 % → mind. RISK_OFF.  
3. **Soft-Cap Buys/Stunde** nach Warm-up (zusätzlich zu daily max).

**Done when:** Unit tests für Size-Pfad; Logzeile `[MarketContext] state=… size_mult=…`.

---

### P3 — Sichtbarkeit

- Telegram: `/market` oder Zeile im Morning Briefing / Cycle-Digest.
- Felder: state, BTC 24h, breadth, warm-up remaining, sensor override.
- Optional: `status_summary` / Ask-Bridge.

**Done when:** Operator sieht Ampel ohne Log-Grep.

---

## Akzeptanzkriterien (gesamt)

- [ ] Nach Restart: kein unkontrollierter Multi-Buy-Burst in Warm-up-Fenster.
- [ ] RISK_OFF: spürbar weniger neue Entries und/oder kleinere Size; Sensor nicht „frei jagen“.
- [ ] CRASH: keine neuen Entries (Config); Sells/Stops unberührt.
- [ ] Cash-Floor, max_open, Grid, Multi-Tenant unverändert in Semantik.
- [ ] Kein Default, der Live-Echtgeld ohne dry_run freischaltet.
- [ ] Unit tests grün; Staging-Verifikation dokumentiert (kurz in PR).

---

## Risiken / Offene Fragen

| Frage | Default-Vorschlag |
|-------|-------------------|
| DCA unter RISK_OFF? | Size runter, nicht hart blocken (Positionen retten) |
| Warm-up pro Tenant? | Nein, prozessglobal |
| Sensor nur blocken vs. alle Entries? | P1: Sensor + optionale TA; CRASH: alle neuen Entries |
| Fear&Greed Pflicht? | Nein in P1 (externe Abhängigkeit) |
| Konflikt mit DEFENSIVE Grid-Sells? | Sells nie blocken |

---

## Verwandte Pläne / Code

- [`plans/grid-mode-abc.md`](grid-mode-abc.md) — Regime → Mode (DEFENSIVE); dieses Ticket ergänzt **globale** Steuerung.
- [`plans/entry-queue-fsm.md`](entry-queue-fsm.md) — 15m-Queue / Seed.
- [`plans/15m-entry-sell-guard.md`](15m-entry-sell-guard.md) — Exit-Whipsaw (orthogonal: Entry-Timing vs. früher Sell).
- `intelligence/regime_detector.py`, `strategy_allocator.py`
- `strategies/entry_sensor_15m.py`, `services/entry_sensor_loop.py`
- `services/eval_queue_runtime.py` (`seed_meta_producers`)
- `risk/risk_manager.py`

---

## Umsetzungshinweise (kurz)

1. Kleine PRs: (a) `MarketState` + Cache + Tests, (b) Warm-up + Gate im Risk/Execute-Pfad, (c) Sensor-Override, (d) `/market`.
2. Feature-Flag `market_context.enabled` default **true** auf Staging nach Go, optional false auf main bis Ready.
3. Logging: eine klare Zeile pro State-Wechsel, nicht pro Coin-Eval.

---

## Ticket-Metadaten

| Feld | Wert |
|------|------|
| Titel | Market Context Layer: Warm-up + BTC/Market-Gate + Exposure |
| Typ | feature / risk-control |
| Priorität | P1 (Staging-Schmerz: Restart + rote Märkte) |
| Owner | — |
| Erstellt | 2026-07-17 |
| Quelle | Operator-Feedback: schneller Deploy-Invest in negativem Tape |
