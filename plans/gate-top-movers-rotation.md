# Design: Gate Top-Movers + mehr Rotation

**Status:** Draft (kein Code)  
**Ziel (klar):** Jeden Tag Top-Mover schnell erkennen und **mehr rotieren** — kleine Gewinne realisieren, Slots freimachen, Capital nicht in toten Holds parken.  
**Scope:** Nur Bot-Logik (Scanner → Universe → bestehende Buy/Risk/Exit). Kein Frontend, kein Parallel-Bot.

---

## 1. Problem

| Heute | Folge |
|-------|--------|
| Trade-Universe ≈ feste Liste + etwas Discovery | Echte **Tages-Top-Mover** oft **nicht** trade-fähig |
| Exit eher „lang halten“ (hohe Trail-Arms / langes Lifetime) | Wenige Closes, unrealized Bleed, wenig Rotation |
| Backtest (10d) | Nur WL: schwach; **WL ∪ gestern Gate-Top-10**: spürbar besser; same-day Oracle noch besser, aber Look-ahead |

**Kernhebel:** (1) Universe um **gestern’s** Gate-Tops erweitern, (2) Exits **früher** (rot_mid-Spirit).

---

## 2. Nicht-Ziele

- Kein zweiter Order-Loop / eigenes Positions-Ledger  
- Kein `place_buy` am RiskManager vorbei  
- Kein same-day Oracle als Live-Default („wir wissen schon morgens, wer *heute* #1 wird“)  
- Keine Web-UI  
- Kein globales `max_open=3` das das Hauptbuch ersetzt  
- Keine hardcodierte Coin-Liste als alleiniges Universe  

---

## 3. Architektur (einfalten in bestehenden Bot)

```
┌─────────────────────────────────────────────────────────────┐
│  GainerScanner (neu, read-only)                             │
│  tickers → filter → live_top / daily_board / streaks        │
│  → hot_candidates / continuation (Tags, Scores)             │
└──────────────────────────┬──────────────────────────────────┘
                           │ shadow log + optional trade inject
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Universe split (bestehend erweitern)                       │
│  observe = broad (Memory/WQE darf Movers sehen)             │
│  trade   = open ∪ base ∪ discovery ∪ gate_prev_top ∪ …      │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Bestehend: process_coin → signals → RiskManager → orders   │
│  Exit: TTP / trail / lifetime (rot_mid knobs) + optional    │
│        strengeres max_hold nur für source=gainer_*          │
└─────────────────────────────────────────────────────────────┘
```

**Prinzip:** Scanner liefert **Kandidaten und Universe**. Handeln tut **nur** die bestehende Pipeline.

---

## 4. Scanner (was er liefert)

### 4.0 Scanner-Frequenz vs. bestehende Signale (wichtig)

**Ja: Scanner darf / soll häufig laufen. Nein: er darf Grid/RSI/Sensor/Fusion/Risk nicht ersetzen oder „kaputt“ priorisieren.**

| Schicht | Frequenz (Richtwert) | Darf sie … |
|---------|----------------------|------------|
| **Ticker-Scan** (live ranks) | oft: z.B. **jeder Bot-Cycle** oder alle **30–60s**, cached | nur **Liste + Scores** updaten |
| **Daily board** (1d candles) | seltener: z.B. **alle 10–15 min** (rate-limit) | gestern’s Tops / Streaks refreshen |
| **Bestehende Signale** | unverändert im Cycle | Entry/Exit wie heute entscheiden |

**Regeln (Fail-safe):**

1. Scanner = **Input für Universe + Logs**, kein eigener Order-Pfad.  
2. Coins, die schon auf Base/Open/Discovery sind: **weiterhin volle bestehende Signal-Pipeline** (kein „nur noch Gainer-Logik“).  
3. Expand-Coins: **dieselbe** `process_coin` / Decision / Risk-Kette — nur **neu sichtbar** für Buys.  
4. Wenn Scanner down/leer/timeout: Universe **fail-open** = wie bisher (Base + Open + altes Discovery), **keine** leere Trade-Liste.  
5. Ranking des Scanners **überschreibt nicht** Exit-Policies, Fusion size_mult, entry_guard, partial-sell-guards.  
6. Optional später: Expand-only Tag für Metrics — nie „disable all other signals on this symbol“.

Kurz: **Scanner = oft gucken, wer heiß ist. Signale = weiter der bestehende Bot.**

### 4.1 Live (häufig, rate-limit-freundlich)

- Gate spot tickers (bestehender Exchange/Market-Wrapper)  
- Filter: `*/USDT`, min 24h Quote-Volume, keine Leverage-Suffixe (`3L/3S/…`)  
- Stock-Tokens **erlaubt**; manuelle `blacklist_bases` optional  
- **live_top:** breites Ranking nach 24h % / Momentum-Proxy (Observe + Universe-Kandidaten)  
- Cache: wenn letzter Scan &lt; `poll_sec`, **Reuse** — kein doppelter Gate-Hammer pro Cycle-Substep  

### 4.2 Daily board (seltener, batched candles)

- Top liquiden Paare (z.B. 150–250 by volume)  
- 1d Candles, Rank pro UTC-Tag: close/prev_close  
- **daily_top_max** + **min_day_ret_pct**: „alle“ starken Mover bis Cap  
- History **daily_history_days** (10)  
- **streaks:** mehrfach oben in den letzten Tagen → Continuation  

### 4.3 Was **trade-fähig** wird (Phase 1b)

**Operator-Wunsch:** möglichst **alle** Tages-Top-Mover, nicht künstlich bei 10 abschneiden.  
**Praktisch:** „alle“, die den Quality-Filter bestehen (Volume, Blacklist nur Leverage/Stable), Rank 1…K mit hohem K.

| Quelle | Wann eligible | Look-ahead? |
|--------|----------------|-------------|
| **gate_prev_top** | Gestern’s Daily-Ranking: **alle** mit day_ret ≥ Schwellwert **oder** bis Cap `daily_top_max` (hoch, z.B. 50–100) | Nein |
| **continuation / streak** | Mehrfach in den oberen Rängen + heute nicht extrem gejagt | Nein |
| live_top / hot same-day | Observe/Logging; Trade erst wenn in prev/continuation (kein Oracle) | — |

**Caps (technisch, nicht „nur Top 10“-Philosophie):**

- `daily_top_max` (z.B. 80): Schutz vor 500 Junk-Pairs am ruhigen Tag  
- `min_day_ret_pct` (z.B. 3–5%): unter dem Ranken viele „Tops“ mit +0.5% Müll  
- `trade_max_coins` / bestehende Universe-Caps: Bot kann nicht unendlich viele gleichzeitig scannen — Expand füllt Trade-Liste bis Cap, priorisiert nach Rank/Score  

Stock-Tokens (**erlaubt**, Operator ok): keine Keyword-Blacklist für Inc./ETF als Default; nur Leverage-Suffixe + Stables.

Persist: Overlay/State, `source`, `rank`, `day_ret`, `eligible_until`.

---

## 5. Rotation / Exit

**Zielbild:** mehr Closes, kleinere realisierte Wins, freie Slots.

| Knopf | Richtung (rot_mid Start) | Bemerkung |
|-------|--------------------------|-----------|
| trail arm / min gain | ~10% / ~6% | Früher sichern als base ~15/10 |
| profit max lifetime | ~48h | Weniger „ewig im Plus sitzen“ |
| Optional gainer-lots only | max_hold ~24h | Spec-Idee, nur getaggte Expand-Entries |

**Nicht** parallel: festes SL6/TP12 als zweites Exit-System fürs ganze Buch.  
Hauptbuch: bestehende Sell-Policies, Staging-Defaults Richtung mid.

---

## 6. Config (Keys — in `config.json` / tenant, nicht parallele Wahrheit)

```yaml
# Skizze — Namen final bei Implementierung an Repo-Stil anpassen
gainer_universe:
  enabled: true
  mode: shadow | trade_expand     # shadow = log only
  quote: USDT
  universe_top_by_volume: 250
  min_volume_usdt_24h: 1_000_000
  blacklist_suffixes: [3L, 3S, 5L, 5S]   # nur Hebel-Tokens
  blacklist_bases: []                   # manuell bei Bedarf
  # stock tokens (NVDAX, *G, …): ERLAUBT — keine name-keyword-blacklist default
  live_top_n: 50                        # observe/logging
  daily_history_days: 10
  daily_top_max: 80                     # „alle“ starken Tages-Mover bis Cap
  min_day_ret_pct: 3                    # darunter nicht als „top mover“ injecten
  daily_min_volume: 300_000
  prev_top_ttl_hours: 36
  enable_continuation: true
  streak_min_days_in_top20: 2
  continuation_max_chase_pct_today: 15

# Exit-Experiment (Staging)
exit_rotation:
  profile: base | rot_mid | rot_agg   # Start Staging: rot_mid
```

**Kein „Sleeve“ / Phase-3-Sonder-Bot** — ein Buch, eine Pipeline (siehe §8).

Secrets: weiter `.env` / Railway (`GATE_*`, Demo-Flags). Nie hardcoden.

---

## 7. Safety

- Staging/Demo zuerst; `trade_expand` nicht ungebremst Prod  
- Alle Buys durch **RiskManager** (cash floor, capacity, universe cap, min notional)  
- Rate limits: Daily-Candles batch + sleep; Ticker nicht unnötig spammen  
- Fail-open: Scanner down → Universe wie bisher (kein leeres Trade-Set)  
- Kill-switch: Expand-Gruppe klar negativ / Gesamt-Demo deutlich schlechter → `mode: shadow` oder off  
- Idempotente Orders wie heute (client ids), kein Retry-Market-Dump  

---

## 8. Phasen & Deliverables

| Phase | Deliverable | Done wenn |
|-------|-------------|-----------|
| **0** | Dieses Doc + Ticket | Go von Operator |
| **1a** | Scanner + daily board (Leverage-Blacklist only), **shadow logs** | Logs: alle starken Tages-Ranks, live tops; keine Trade-Änderung |
| **1b** | `prev` (+ continuation) → **trade universe**, breit bis Cap/TTL | Viele Expand-Coins trade-fähig; Orders nur über bestehende Path |
| **1c** | **rot_mid** Exit Staging (default+henry demo) | Mehr Closes / kleinere realisierte Wins messbar |
| **2** | 7d Soak + Report | Metrics; Go/No-Go (N/Cap/min_day_ret feintunen) |

**Kein Phase-3-Sleeve.** Frühe Entries kommen über Universe + bestehende Buy-Logik + Rotation — nicht über ein zweites Mini-Konto im Bot.

**Tests (minimal):**  
- Mock tickers → Filter (Leverage raus, Stock drin)  
- Daily board: Rank/Streak korrekt  
- Universe: gestern’s Mover erscheinen in trade (viele, bis Cap), nach TTL weg  

Operator-Start: Demo + `gainer_universe.mode=shadow`.

---

## 9. Soak-Metriken (7 Tage Staging)

| Metrik | Richtung |
|--------|----------|
| # Closes / Woche | ↑ vs. Vorwoche |
| Share realisierte Wins „klein“ | ↑ (Rotation sichtbar) |
| Expand-Buys > 0 | ja |
| Expand-Gruppe realized PnL | tracken (nicht nur Gesamt) |
| Hit: gestern’s Top in trade ±1d gehandelt | ↑ vs. pure WL (~5% → Richtung 20–60% im Backtest) |
| Occupancy / free slots nach Exits | Rotation, nicht Dauer-Vollblock ohne Turnover |
| Skip reasons | volume, blacklist, chase, risk, capacity |

**Kill:** Expand-PnL klar negativ über 7d **oder** Gesamt-Demo spürbar schlechter **oder** reiner Volume-Spam ohne Edge.  
**Go:** behalten; Caps/`min_day_ret` feintunen (breiter vs. enger).

---

## 10. Mapping Grok-Build-Spec → dieses Design

| Spec | Hier |
|------|------|
| `gainer_scanner.py` | Phase 1a Modul (read-only) |
| daily leaderboard / streaks | 1a + inject 1b (**breit**, nicht nur Top-10) |
| `gainer_strategy` eigener Loop + Orders | **Nein** — nur Universe + bestehende Pipeline |
| TRADE max_open 3 / size 50 / „Sleeve“ | **Entfällt** — ein Buch |
| SL6/TP12 | **Nein**; Exit = rot_mid / bestehende Policies |
| dry_run / daily loss | Demo + bestehendes Risk |
| Stock-Token-Blacklist | **Aus** (Operator: stock tokens ok) |

---

## 11. Ein-Satz für Henry

**Der Bot soll möglichst alle starken Coins von gestern auf die Einkaufsliste bekommen (auch Stock-Tokens) und schneller kleine Gewinne mitnehmen — erst zuschauen, dann Staging.**

---

## 12. Entschieden (Operator)

| Thema | Entscheidung |
|--------|----------------|
| Wie viele Top-Coins? | **Möglichst alle** starken Tages-Mover (Filter + hoher Cap), nicht „nur 10“ |
| Stock-Tokens | **Erlaubt** |
| Phase-3 „Sleeve“ | **Entfällt** / nicht Teil des Plans |

---

## 13. Bestehende Shadow-Features (Stand Config) vs. dieser Plan

| Feature | Status | Kollision? |
|--------|--------|------------|
| **WQE** `watchlist_quality.mode=shadow` (+ AI shadow) | scorct, ändert Trade-Liste noch nicht | **Nein** — parallel ok; bei später soft/enforce: Expand-Tags nicht tot-scoren |
| **DCA policy** `shadow: true` | Sizing-Policy nur Audit | **Nein** |
| **DCA scheduled** | enabled false / shadow | **Nein** |
| **Fusion/Oracle RISK_OFF** → oft `sensor_policy: shadow` | weniger Sensor-Buys, kein harter Block | **Indirekt** — Expand sichtbar, Buys gedrosselt (ok) |
| **sell_policy** active + `shadow_log_decisions` | Sells live + Logs | **Nein** |
| **Slot eviction** **live** | kann evicten | **Beobachten** bei mehr Expand-Kandidaten |
| **Exit TTP/lifetime/trail** | live | rot_mid ändert Knöpfe — Rollback = Config revert |
| **entry_sensor_15m** | active | unangetastet |

**Rollback-Prinzip:** Alles hinter `gainer_universe.enabled` / `mode` und `exit_rotation.profile`; aus = altes Verhalten.

---

## 14. Arena-Ergebnis + Implementierung (done)

**Arena:** A minimal inject · B full module · C CMC-reuse.  
**Winner:** B (full `gainer_universe`) + A’s surgical hooks; **C rejected** (CMC ≠ Gate daily).

**Shipped (feature-flagged):**
- `services/gainer_universe/*` — scanner, inject, runtime, store, filters  
- Wire: `aria_bot` refresh, `load_observe_universe` / `load_trade_universe`  
- `services/exit_rotation.py` — runtime overlay on TTP + profit_max_lifetime  
- `config.json`: `gainer_universe.mode=trade_expand`, `exit_rotation.profile=rot_mid`  
- Tests: `tests/unit/test_gainer_universe.py`

### Rollback (ohne Code-Revert)

```json
"gainer_universe": { "enabled": false }
// oder "mode": "off" | "shadow"

"exit_rotation": { "enabled": false }
// oder "profile": "base"
```

## 15. Soak (nächster Ops-Schritt)

1. Deploy staging, logs: `gainer_universe refresh … eligible=`  
2. 7d: expand buys, rot_mid closes, kill if expand PnL klar negativ  
3. Optional: `mode=shadow` zuerst, dann `trade_expand`
