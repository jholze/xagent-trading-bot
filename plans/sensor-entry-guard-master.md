# Master-Plan: Sensor-Entry-Guard (BDX-Klasse) — Strategy × Venue × Memory × Grid

> **Status:** V1+M1+M2 **implemented** in repo (2026-07-18) — deploy optional; Reflect M3 optional follow-up  
> **Canonical doc:** diese Datei (ersetzt/merged die früheren Split-Pläne)  
> **Anlass:** Staging default **BDX/USDT** 2026-07-16→18: `entry_sensor_15m` BUY bei TA-HOLD, ~$3.1k + DCA, Full-Exit **−52 % / −$1768**  
> **Branch (später):** `feature/sensor-entry-guard` von `staging`  
> **Deploy:** erst nach Review + Staging-Soak; kein Auto-Deploy  
> **Supersedes:** `prevent-sensor-entry-blowups.md`, `sensor-strategy-memory-integration.md` (Stubs verweisen hierher)  
> **Verwandt:** `15m-entry-sell-guard.md`, `market-context-entry-throttle.md`, `grid-mode-abc.md`, Memory OPS (#30/#42/#53), Epic #6 (Social ≠ dieses Problem), #65 Agents (später)

---

## 0. Executive Summary

| Frage | Antwort |
|-------|---------|
| Warum BDX? | **15m-Vol-Sensor** hat BUY erzwungen trotz **4h HOLD**; Gate nur „Preis > 0“; **dünne Venue** (~$2k/24h quote vol, extremer Spread); Full-Size × aggression; Memory fail-open bei n=1 |
| Social/CMC/LC/X? | **Nicht** die Ursache; #6-Änderungen stoppen diesen Pfad **nicht** |
| Profit-Pfad? | Nach Entry max **~+1.3 %** kurz; nach DCA **nie** wieder ≥ Ø-Entry — Exit-Feintuning half nicht |
| Ziel | Weniger und kleinere Sensor-Blowups; **lernen** (source + venue); Grid unangetastet |
| Jetzt gebaut? | **Nein** — nur Pläne + unrelated Config (LC/X park, CMC fallback) |

**Leitprinzip:** Strategy bewertet Setup · Venue blockt dünne Bücher live · Memory lernt source/venue · Risk erzwingt Gates · **kein** Ledger-Write aus Memory · Sells nie durch Memory/Venue blocken.

---

## 1. Problem (präzise)

### 1.1 Ledger-Fakten BDX (default / demo / Railway test)

| | Zeit UTC | Detail |
|--|----------|--------|
| Buy1 | 16.07. 15:50 | **0.25617**, 12 198.93, **$3125**, source **`entry_sensor_15m`**, mult 1.25 |
| Buy2 | 17.07. 02:54 | **0.2383**, 1 152.58, **$275**, source **dca** |
| Ø Entry | | **0.25463**, cost **$3399.66** |
| Sell | 18.07. 11:42 | **0.1222**, full, **SELL_FULL** auto, pnl **−1768.11 (−52 %)** |
| Gate Venue (Stichprobe) | | quote_vol_24h ~**$2118**, spread bid/ask **~30 %+** |
| Memory Profile | | `entry_bias=neutral`, `size_bias=1.0`, *few samples n=1 fail-open* |
| bot_decisions | | Muster 14.07.: `TA->HOLD \| 15m vol spike … \| 15m->vol entry` (16.07. Decision-Log lückenhaft, Order eindeutig) |

### 1.2 Kernfehlerklasse

```text
15m Kerzen-Spike  +  TA HOLD Override  +  Full-Size
  +  kein Gate 24h-Vol/Spread  +  Memory n≥3 soft_block
        →  schneller Kapitaleinsatz auf dünnem Book  →  Full-Loss
```

Nicht: „Social kaputt“. Sondern: **Sensor zu freizügig + keine Venue-Qualität + Memory lernt Big-Loss zu spät**.

### 1.3 Heute im Code (Ist)

| Check | Verhalten |
|-------|-----------|
| Sensor `mode: active`, `vol_spike_mult: 2.0` | HOLD → BUY möglich |
| Gate tradeable | Preis > 0 |
| Market cap min | oft $5M global (Beldex groß → **pass**) |
| Grid slice | nur `trading_mode == "GRID"` (22–35 %); HYBRID/MOMENTUM **full** |
| Memory soft_block | erst **n≥3** Sells in rebuild |
| Risk | soft_block + size_bias angeschlossen, greift BDX n=1 **nicht** |

---

## 2. Ziele / Nicht-Ziele / Erfolgskriterien

### 2.1 Ziele

1. **Proaktiv:** dünne Gate-Paare und lockere HOLD→BUY-Overrides stoppen/verkleinern  
2. **Retrospektiv:** Memory lernt **source** + **venue@fill** + gross-loss → soft_block/cooloff  
3. **Grid-safe:** Grid-Level-Engine unangetastet; soft_block default nur Sensor-Family  
4. **Messbar** und kill-switchbar (Config)

### 2.2 Erfolgskriterien

| Metrik | Heute | Ziel |
|--------|-------|------|
| Sensor-BUY bei TA=HOLD ohne Confluence (MOMENTUM) | erlaubt | **block** (oder shadow) |
| Sensor-BUY bei thin venue (vol/spread) | erlaubt | **block** |
| Max Sensor-Erstkauf | ~full × 1.25 | mode-aware slice + hard cap (z. B. ≤$1000) |
| Re-Entry nach Gross-Loss (Sensor) | sofort | soft_block TTL + cooloff |
| Fill ohne Venue-Snapshot | immer | **nie** (oder `capture=missing`) |
| Profile nach thin loss | generisch fail-open | `features.venue` + Lesson `thin_venue` |

### 2.3 Nicht-Ziele

- Exit-TP-Optimierung als Haupthebel (BDX: kaum Profit-Pfad)  
- Social CMC/LC/X reaktivieren  
- Decision Agents (#65) jetzt  
- Parallele process_coin / Ledger-Umbau  
- Sensor komplett killen (außer Ops Kill-Switch shadow)  
- MCap allein erhöhen (BDX global liquid genug)

---

## 3. Wirkungsanalyse (Hebel-Ranking)

| Hebel | BDX-Effekt | Aufwand | Hinweis |
|-------|------------|---------|---------|
| **V Venue Quality** (24h quote vol + spread + book) | **sehr hoch** (hätte Entry allein gestoppt) | mittel | orthogonal zu TA |
| **A Confluence** HOLD→BUY | **sehr hoch** | mittel | mode-aware |
| **B Sensor Size-Cap / slice** | hoch (Schaden) | niedrig | GRID schon Slice; HYBRID-Bug |
| **C Memory gross-loss + cooloff** | hoch (Re-Entry) | mittel | n=1 erlaubt |
| **M Venue learning** | Erklärbarkeit + TTL-Boost | mittel | **mit V1 stamp** |
| **D Strengere Spike-Defaults** | mittel–hoch | niedrig | Config |
| **E Global market block** | mittel | prüfen Lücke | CRASH schon da |
| **F Sensor shadow** | total | trivial | Kill-Switch |

**Kern-Kombi:** **V + A + B + C + M(venue)** — in dieser Reihenfolge umsetzen.

---

## 4. Architektur: Rollen und Defense-in-Depth

### 4.1 Drei Rollen (+ Venue)

```text
┌─────────────────────────────────────────────────────────────────┐
│ Strategy vol_spike_15m (ex entry_sensor override)               │
│ Input: 15m metrics, 4h TA, trading_mode, profile snapshot       │
│ Output: BUY|HOLD, size_hint, confidence, reasons[]              │
│ Darf: Setup; liest Memory; schreibt kein soft_block             │
└────────────────────────────┬────────────────────────────────────┘
                             │
         ┌───────────────────┼───────────────────┐
         ▼                   ▼                   ▼
   Venue Quality        Memory (Hermes)      Global Bias
   live ticker          rebuild/reflect      Oracle/San/Macro
   hard block thin      soft_block/lessons   block_buys CRASH
         │                   │                   │
         └───────────────────┼───────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│ Risk Guardian                                                   │
│ venue_ok · soft_block_scope · cooloff · size_hint × size_bias   │
│ × sensor_cap × global/calendar/session/pm · cash/slots          │
│ Sells: nie durch venue/memory blocken                           │
└────────────────────────────┬────────────────────────────────────┘
                             ▼
                         Execute
                             │
              stamp venue @ fill → orders metadata
                             │
              Hermes rebuild → memory_* (RO ledger)
```

### 4.2 Defense-in-Depth (Spike → Order)

```text
15m Vol-Spike
    → [D] Spike/EMA thresholds
    → [V] Venue quality (quote_vol, spread, book)
    → [A] Confluence / hold_override_by_mode
    → [E] Global market block
    → BUY intent + size_hint
    → [B] Size cap (mode + absolute)
    → [C] Memory soft_block / cooloff
    → Risk final
    → Execute + venue stamp
```

Jeder Layer kill-switchbar; fail-safe eher blocken als full-size kaufen.

### 4.3 Zwei Entry-Familien (nicht vermischen)

| | **A Grid Level** | **B Vol-Spike Sensor** |
|--|------------------|-------------------------|
| Source | `grid` | `entry_sensor_15m` / `vol_spike_15m` |
| Logik | GridStrategy levels | Override / strategy module |
| Confluence A | **nein** | **ja** |
| Size B | Grid level size | mode slice + cap |
| Venue V | new entries optional | **ja** |
| soft_block default | erlaubt bei `sensor_only` | blockiert |

---

## 5. Strategy `vol_spike_15m`

### 5.1 Vertrag

| Feld | Bedeutung |
|------|-----------|
| `stance` | BUY \| HOLD |
| `size_hint` | 0–1 Fraktion `max_usdt_per_trade` |
| `confidence` | Spike + Confluence |
| `reasons[]` | erklärbar |
| Reads | CoinProfile (soft_block → HOLD-Spiegel optional) |
| Writes | nichts in memory_* |

Legacy: `entry_sensor_15m` Config mappt 1:1 (kein Big-Bang-Rename nötig).

### 5.2 Mode-Matrix (Target)

| Mode | Sensor | size_hint | hold_override |
|------|--------|-----------|---------------|
| **GRID** | Add-on slice (wie heute `grid_slice`) | stable **0.22** / volatile **0.35** | `slice_only` (nie full) |
| **HYBRID** | medium slice | **0.40–0.55** | `allow_with_conditions` |
| **MOMENTUM** | streng | **≤0.30–0.35** + hard cap | **`block`** bei purem TA-HOLD |
| **DEFENSIVE** | **off** | 0 | n/a |

**Ist-Bug:** `entry_sensor_buy_usdt_frac` kennt HYBRID, Code wendet Slice **nur** bei `trading_mode == "GRID"` an → HYBRID/MOMENTUM Full-Size (BDX-Klasse).

### 5.3 hold_override Modes

| Mode | Verhalten |
|------|-----------|
| `block` | HOLD + Sensor → kein Buy |
| `shadow` | nur Log/Shadow |
| `allow_with_conditions` | ≥ N Bedingungen (RSI-Band, EMA breakout, höherer Spike, …) |
| `slice_only` | Buy erlaubt, aber nur size_hint (GRID) |
| `legacy` | altes Verhalten (Kill-Switch A/B) |
| `off` | Sensor aus (DEFENSIVE) |

Conditions-Skizze (wenn allow_with_conditions):

```json
"conditions": {
  "rsi_4h_max": 45,
  "rsi_4h_min": 20,
  "vol_spike_mult_override": 3.5,
  "require_ema_breakout_15m": true,
  "btc_not_underperforming": true
}
```

### 5.4 Size (Sensor-Family)

```text
usdt = min(
  max_usdt_per_trade × size_hint_by_mode,
  max_usdt_absolute,          // z.B. 1000
  risk_approved_after_mults
)
ignore_aggression_boost: true   // kein 1.25 auf reinen Sensor
```

DCA: separates Limit (nicht dieses Cap zerstören).

### 5.5 Strengere Defaults (Config)

| Key | Alt | Neu Staging |
|-----|-----|-------------|
| `vol_spike_mult` | 2.0 | **3.0** |
| `block_buy_if_rsi_4h_above` | 75 | **65** |
| `require_ema_breakout` | false | true nur in conditions-mode |

### 5.6 Grid-eigene Pfade (nicht ersetzen)

- GridStrategy levels, spacing (ATR + Santiment `grid_spacing_mult`)  
- Green-only sells in pure GRID  
- DEFENSIVE: no new grid buys + partial reduce  
- Merge-Policy: **ein** Buy-Intent pro Cycle wenn Grid + Sensor gleichzeitig (kein Doppel)

### 5.7 Global Market

Oracle/Santiment `block_buys` auf CRASH — **prüfen**, dass `process_entry_sensor` immer durch Risk läuft (Bugfix falls Bypass).

### 5.8 Kill-Switch (sofort Ops)

```json
"entry_sensor_15m": { "mode": "shadow" }
```

---

## 6. Venue Quality (Gate 24h-Volumen & Liquidität)

### 6.1 Lücke

Heute: gelistet (Preis > 0) + optional MCap.  
**Nicht:** Gate `quote_volume`, Spread, Book-Tiefe.

BDX: große MCap, **dünnes Gate-Book** → 15m-Spike ≠ Kapital.

### 6.2 Hard Gates (Staging-Vorschlag)

| Check | Quelle | Default |
|-------|--------|---------|
| Min 24h quote volume | ticker `quote_volume` | **≥ $50 000** USDT |
| Max spread | (ask−bid)/mid | **≤ 1.5 %** |
| Min top-of-book | bid/ask size × price | **≥ $200**/Seite |
| Vol vs order | quote_vol ≥ k × planned_usdt | **k ≥ 20** |

Apply to: `entry_sensor_15m`, `vol_spike_15m`, optional `grid_new_entry`.  
**Sells:** nie blocken.

### 6.3 Implementierung

| Stück | Ort |
|-------|-----|
| Bulk tickers + metrics | `price_fetcher` / `services/venue_quality.py` |
| Cache TTL 60–120s | RAM + optional Redis |
| `passes_venue_quality()` | pure, unit-testbar |
| Config SoT | `risk.venue_quality` |
| Consumers | Sensor loop, Strategy, Risk `venue_liquidity_block`, optional Grid new entry |

**on_fetch_error:** Sensor **`block_sensor`** (empfohlen); Grid-Level optional warn once.

### 6.4 BDX Counterfactual Venue

| Check | → |
|-------|---|
| ~$2k quote vol | **block** |
| ~30 % spread | **block** |
| $3k vs Tagesvol | **block** |

---

## 7. Memory-Integration

### 7.1 Warum Memory BDX verpasst hat

```text
rebuild: soft_block nur n_sells >= 3  →  n=1 fail-open
reflect: ebenfalls min_samples >= 3
TradeMemory.source vorhanden  →  features.by_source FEHLT
venue am Entry  →  nicht gestempelt, nicht gelernt
Social: nie soft_block allein  →  korrekt, half hier nicht
```

### 7.2 TradeMemory / Profile Features

**by_source** (rebuild):

```json
"by_source": {
  "entry_sensor_15m": { "buys": 1, "sells_linked": 1, "pnl_usdt": -1768.1, "avg_pnl_pct": -52.0 },
  "dca": { "buys": 1 },
  "grid": { "..." : "..." }
}
```

Buy→Sell Lot-Matching: PnL dem dominanten Entry-Source zuordnen.

**Gross-loss soft_block** (`memory.gross_loss`):

| Bedingung | Wirkung |
|-----------|---------|
| Ein Sell `pnl_pct ≤ -25%` **oder** `pnl_usdt ≤ -500` | soft_block, size_bias≤0.5, **ohne** n=3 |
| TTL | z. B. 14d in `soft_block_until` |
| scope default | **`sensor_only`** (Grid-Level weiter erlaubt) |
| optional all_new_entries | toxisches Symbol |

**Re-entry cooloff (Risk):** nach Full-Exit Loss z. B. **168h** (auch wenn Hermes lag).

### 7.3 Memory lernt Volumen/Liquidität (Pflicht)

Live-Gate allein reicht nicht für Lernen.

#### Fill-time Snapshot (Bot, mit V1)

Auf `order.execution.venue` / später `TradeMemory.metadata.venue`:

| Feld | Beispiel |
|------|----------|
| `quote_volume_24h_usdt` | 2118 |
| `spread_pct` | 30.2 |
| `top_book_usdt` | … |
| `volume_to_order_mult` | 0.68 |
| `venue_ok` / `reasons[]` | false / thin_volume |
| `exchange` | gate |
| fail | `{ "capture": "missing" }` |

#### Profile `features.venue` (rebuild)

```json
"venue": {
  "last_entry_quote_vol_24h": 2118,
  "last_entry_spread_pct": 30.2,
  "entries_thin_30d": 1,
  "entries_thick_30d": 0,
  "pnl_when_thin_usdt": -1768.1,
  "pnl_when_thick_usdt": 0,
  "thin_loss_rate": 1.0
}
```

Thin-Definition: **dieselben Thresholds** wie `risk.venue_quality` (eine SoT).

#### Reflect Lessons

| Bedingung | Tags |
|-----------|------|
| Loss + venue thin @ entry | `thin_venue`, optional `sensor_blowup` |
| ≥2 thin entries / 30d | `thin_venue`, `repeat` |
| Gross-loss + thin | soft_block TTL boost (z. B. +7d) |

#### Memory darf nicht

- Social allein soft_block  
- Live-Venue ersetzen  
- Orders/positions schreiben  
- Sells blocken  

**V1 ohne Fill-Stamp = unvollständig** für Memory-Volumen-Lernen.

### 7.4 Hot-Path-Sequenz (Ziel)

```text
1. Regime → trading_mode
2. Primary: GridStrategy (GRID|HYBRID) oder TA
3. vol_spike_15m.evaluate(mode, metrics, profile, venue_ok)
4. Merge: max ein Buy-Intent
5. Risk: venue · soft_block_scope · cooloff · size
6. Fill + venue stamp
7. Hermes: rebuild profiles/lessons
```

Fail-open Memory: size_bias=1.0 — deshalb **V+A+B Pflicht** unabhängig von Hermes.

### 7.5 Datenfluss

```text
xagent-test                              xagent-hermes
Strategy + Venue + Risk
  → order (+ execution.venue stamp)
                                         rebuild RO orders
                                           → TradeMemory (+ venue meta)
                                           → CoinProfile by_source + features.venue
                                           → Lessons thin_venue / sensor_blowup
Bot cache ← CoinProfile (TTL ~60s)
```

---

## 8. Mapping BDX → Layer

| Problem | Strategy | Venue | Memory | Risk |
|---------|----------|-------|--------|------|
| HOLD + 2× Spike Full BUY | hold_override MOMENTUM=block | — | — | — |
| Thin Gate book | prefilter | **hard block** | learn thin@fill | venue_liquidity_block |
| $3k size | size_hint | vol≥20×order | — | cap + no aggression |
| n=1 fail-open re-entry | — | — | gross_loss soft_block | cooloff |
| DCA-Spam rejected | — | — | lesson optional | DCA rules (separat) |
| Grid nach Loss | — | — | scope sensor_only | grid buys ok |

**Replay nach Umsetzung:** Entry 16.07. stirbt an Venue und/oder hold_override; falls durch: Size klein; nach Exit: soft_block Sensor-Reentry.

---

## 9. Unified Config (Ziel Staging)

```json
"entry_sensor_15m": {
  "enabled": true,
  "mode": "active",
  "vol_spike_mult": 3.0,
  "block_buy_if_rsi_4h_above": 65,
  "require_ema_breakout": false,
  "hold_override_by_mode": {
    "GRID": "slice_only",
    "HYBRID": "allow_with_conditions",
    "MOMENTUM": "block",
    "DEFENSIVE": "off"
  },
  "size_hint_by_mode": {
    "GRID": { "stable": 0.22, "volatile": 0.35 },
    "HYBRID": { "stable": 0.40, "volatile": 0.55 },
    "MOMENTUM": { "default": 0.30, "max": 0.35 }
  },
  "max_usdt_absolute": 1000,
  "ignore_aggression_boost": true
},
"risk": {
  "venue_quality": {
    "enabled": true,
    "exchange": "gate",
    "min_quote_volume_24h_usdt": 50000,
    "max_spread_pct": 1.5,
    "min_top_book_usdt_per_side": 200,
    "min_volume_to_order_multiple": 20,
    "apply_to": ["entry_sensor_15m", "vol_spike_15m", "grid_new_entry"],
    "cache_ttl_sec": 90,
    "on_fetch_error": "block_sensor"
  },
  "sensor_entry": {
    "ignore_aggression_boost": true,
    "reentry_cooloff_hours_after_gross_loss": 168
  },
  "grid_entry": {
    "respect_soft_block_scope": true
  }
},
"memory": {
  "profile_by_source": true,
  "gross_loss": {
    "enabled": true,
    "min_loss_pct": 25,
    "min_loss_usdt": 500,
    "soft_block_ttl_hours": 336,
    "size_bias_cap": 0.5,
    "soft_block_scope": "sensor_only",
    "apply_to_sources": ["entry_sensor_15m", "vol_spike_15m"]
  },
  "venue_learning": {
    "enabled": true,
    "stamp_on_fill": true,
    "thin_uses_risk_venue_thresholds": true,
    "lesson_on_thin_loss": true,
    "soft_block_ttl_boost_hours_if_thin": 168,
    "aggregate_window_days": 30
  }
}
```

---

## 10. Implementierungsphasen (was wir umsetzen)

### Empfohlene Reihenfolge

```text
P0  V1   Venue live + Risk/Sensor block + Fill-Stamp
P0  M1   hold_override_by_mode + size_hint (HYBRID slice fix) + hard cap
P1  M2   Memory by_source + features.venue + gross_loss soft_block + cooloff
P1  V2b  optional Grid new-entry venue
P2  M3   Reflect thin_venue / sensor_blowup + TTL boost
P3  M0   Counterfactual report (kann parallel zu P0 laufen)
P4  M4   Agents #65 (nicht Teil dieses Guards)
```

### Phase V1 — Venue + Stamp (P0, BDX-Killer)

| Arbeit | Deliverable |
|--------|-------------|
| `venue_quality` module + cache | pure evaluate + Gate bulk |
| Sensor + Risk block | code `venue_liquidity_block` |
| Fill stamp `execution.venue` | orders metadata |
| Config defaults | staging thresholds |
| Unit tests | thin/thick, sell never blocked |

**DoD:** BDX-like fixture → no BUY; BTC-like → pass; every sensor fill has venue or `capture=missing`.

### Phase M1 — Strategy/Sensor Size & Confluence (P0)

| Arbeit | Deliverable |
|--------|-------------|
| hold_override_by_mode | MOMENTUM block TA-HOLD |
| size_hint all modes + HYBRID fix | no full-size HYBRID bug |
| max_usdt_absolute + no aggression | Risk/orchestrator |
| Unit tests | HOLD+spike block; GRID slice; HYBRID medium |

**DoD:** Unit grün; 48h soak: no sensor full-size MOMENTUM buys.

### Phase M2/V2 — Memory lernen (P1)

| Arbeit | Deliverable |
|--------|-------------|
| rebuild by_source + venue aggregates | CoinProfile features |
| gross_loss soft_block n=1, scope sensor_only | rebuild + Risk TTL |
| reentry cooloff | Risk |
| TradeMemory.metadata.venue from orders | rebuild |
| Unit tests | −52% → soft_block; sensor_only allows grid |

### Phase M3/V3 — Lessons (P2)

| Arbeit | Deliverable |
|--------|-------------|
| Reflect thin_venue / sensor_blowup | lessons |
| TTL boost thin+gross | config |
| Weaviate optional | embed |

### Phase M0/V0 — Report (optional parallel)

Script: sensor fills 30–60d × venue counterfactual × hold_override × size.  
Deliverable: `auswertungen/sensor_entry_blowup_report_YYYYMMDD.md` + JSON.

### Phase F — Ops Kill-Switch

Document: `mode: shadow` in runbook.

---

## 11. Tickets (Vorschlag)

| ID | Titel | Phase | Prio |
|----|-------|-------|------|
| SEG-0 | Counterfactual report sensor fills × venue | M0 | P2 |
| SEG-1 | Venue quality + fill stamp | V1 | **P0** |
| SEG-2 | hold_override_by_mode + size matrix + HYBRID slice | M1 | **P0** |
| SEG-3 | Memory by_source + venue features + gross_loss soft_block | M2 | P1 |
| SEG-4 | Reflect thin_venue / sensor_blowup | M3 | P2 |
| SEG-5 | Grid new-entry venue (optional) | V2b | P2 |

Epic-Label: Entry-Sensor / Risk — **nicht** Epic #6 Social.

---

## 12. Tests (gesamt)

| Bereich | Assert |
|---------|--------|
| Venue | thin → fail; thick → pass; sell never blocked |
| Strategy | MOMENTUM HOLD+spike → HOLD; GRID → slice |
| HYBRID | size medium not full |
| Risk | soft_block sensor_only: sensor reject, grid allow |
| Risk | size ≤ absolute cap without aggression |
| Memory rebuild | 1 sell −52% → soft_block; venue thin aggregates |
| Reflect | thin loss → lesson thin_venue |
| Integration | BDX counterfactual prevented |
| Regression | Social no soft_block alone; DCA/sell paths ok |

---

## 13. Rollout & Rollback

1. Implement V1+M1 auf Branch → Review  
2. Staging deploy + 48–72h soak  
3. M2 Memory → soak  
4. Prod nur nach Staging-OK  

**Rollback:** venue `enabled: false`; hold_override `legacy`; memory gross_loss off; sensor `mode: shadow` als Notbremse.

---

## 14. Risiken

| Risiko | Mitigation |
|--------|------------|
| soft_block zu hart | scope sensor_only; TTL; kill-switch |
| Venue zu streng | thresholds Config; Report M0 |
| API fail blocks all | on_fetch_error policy; monitor |
| Strategy vs Risk double semantics | Risk SoT for hard blocks |
| Hermes lag | V+A+B independent of memory |
| Doppel-Buy Grid+Sensor | merge policy one intent |

---

## 15. Offene Defaults (vor Code abnicken)

1. Venue: **$50k** / **1.5 %** spread?  
2. MOMENTUM hold_override **block**?  
3. Size hard cap **$1000**?  
4. Gross-loss **−25 % / −$500**, scope **sensor_only**?  
5. Ticker fail: Sensor **block**?  
6. HYBRID slice-fix in M1 mitnehmen? (**ja**)

---

## 16. Abgrenzung

| Thema | In diesem Plan | Außerhalb |
|-------|----------------|-----------|
| Sensor/Venue/Memory guard | ja | |
| Grid engine rewrite | nein | grid-mode-abc |
| 15m sell guard exits | nein | 15m-entry-sell-guard |
| Market context warm-up burst | teilweise E | market-context-entry-throttle |
| Decision Agents | vorbereitet (tags) | #65 |
| Social signals | nein | Epic #6 |

---

## 17. Einzeiler

> **Live:** Gate-Liquidität + mode-aware Sensor-Confluence/Size stoppen BDX-Käufe.  
> **Memory:** source + venue@fill lernen und Sensor-Reentries nach Big-Loss/thin drosseln.  
> **Grid** bleibt eigene Engine. **Noch nicht implementiert** — nächster Code: **V1 + M1**.

---

## Appendix A — Merge-Herkunft

| Früheres Doc | Inhalt jetzt |
|--------------|--------------|
| `prevent-sensor-entry-blowups.md` | §1–3 Hebel A–F, Phasen, Kill-Switch, Nicht-Ziele |
| `sensor-strategy-memory-integration.md` | Rollen, Memory E, Grid 4b, Venue 4c, Config, Agents |

---

## Appendix B — Sofort ohne Code

```json
"entry_sensor_15m": { "mode": "shadow" }
```

Bis V1+M1 live — einzige harte Garantie heute.
