# Plan: DCA Sniper Service — Fokus-Recovery, volle Analyse, keine versehentlichen Exits

**Status:** Implemented locally (default off) · **Modus:** Staging Demo first · **Kein Deploy**  
**Datum:** 2026-08-09  
**Code:** `services/dca_sniper/` · `strategies/recovery_hold.py` · bot routes in `bot_http.py`  
**Kontext:** BEAT-Trail auf stale Peak; BLESS versehentlich verkauft (revidiert); User will massive Adds auf **einen** besten Kandidaten; Zeit für tiefe Analyse; Service-Architektur.

---

## 1. One-liner

**Ein DCA-Sniper-Service** wählt vollautomatisch die **besten Recovery-Kandidaten** (Quality-first, oft 1, bei Cash+Analyse auch 2…X), prüft **alle verfügbaren Signale**, führt **individuelle Heavy-Adds** aus (innerhalb Floor/`spendable_dca` + optional Claim/Fund) und hält Focus-Bags im **Recovery-Hold**, bis BE+/Policy — **kein manuelles Approve**.

---

## 2. Ziele / Non-Goals

### Ziele
1. DCA-Logik **aus dem Hot-Path** (Cycle/DE-Sell-Merge) in einen **eigenen Service** (`xagent-dca-sniper`).
2. **Quality-first Focus:** Prefer tief auf die besten; **dynamisch 1…N** Focus-Slots wenn Cash + Analysen es hergeben (nicht starr immer nur 1).
3. **Volle Checkliste** (nicht 3 Indikatoren) — alles was der Stack + sinnvolle APIs hergeben.
4. **Sizing individuell** — kein Fixbetrag: aus Rest-Bag × f, Checklist-Qualität/Score, Profil, Liquidität, **spendable_dca** (siehe §6). Euro-Zahlen im Doc sind **nur Beispiele**.
5. **Exit-Schutz:** Nur **Sniper-Focus / recovery_hold**-Bags — nicht versehentlich per Trail/TTP/BB/Partial; Hard-SL bleibt.
6. Bot bleibt schlank: Sniper entscheidet Heavy-Focus; Bot Risk/Ledger führt aus. Bestehende DCA-Bausteine **reuse**, nicht löschen (§3b).
7. **Vollautomatisch** — kein Telegram-/Operator-Approve für Heavy-DCA.
8. Cash-Floor bleibt; Heavy nur aus freigegebenem Kapital über dem Floor.

### Non-Goals (v1)
- Fix-USDT pro Trade (z.B. immer 2k)  
- Endlos-Martingale / multi-double pro Coin  
- Blind alle roten Bags parallel peanuten  
- Manuelle Freigabe pro Trade  
- LLM als alleiniger Entscheider ohne harte Gates  
- Production Live ohne Staging-Soak  
- On-Chain-Fullsuite für jeden Micro-Meme (nur wenn Datenqualität ok)  
- **Grid-Mechanik ersetzen** — Grid bleibt eigene Strategie (§3c)  
- Bestehenden DCA-Stack hard-delete am Tag 1---

## 3. Architektur

```
┌─────────────────────────────────────────────────────────────┐
│  xagent-test (Bot)                                          │
│  • Cycle / DE: Entries, Grid, Runner-Exits                  │
│  • Hard SL immer · recovery_hold sell gates                 │
│  • Cash policy: spendable_new vs spendable_dca (Floor)      │
│  • Internal API: candidates / cash snapshot / fund-sell /   │
│    dca-execute (token) — final Risk + ledger                │
│  • Optional: price ticks / exit-style WS fan-out            │
└──────────────────────────▲──────────────────────────────────┘
                           │ auto execute (no human gate)
┌──────────────────────────┴──────────────────────────────────┐
│  xagent-dca-sniper (NEU)                                    │
│  FAST PATH (WS / ticks wo sinnvoll)                         │
│  • Preis-Subscribe Focus + shortlist (Exchange WS / Bot hub)│
│  • Dip-Trigger, BE+ promote, SL-Proximity, hold monitors    │
│  SLOW PATH (async deep)                                     │
│  • Rank → #1 · volle Checklist · size · cash plan           │
│  • Optional: fund-from-winner request vor Heavy             │
│  • Decision: WAIT | NEED_CASH | DCA_HEAVY | SKIP | EXHAUSTED│
│  Fully automatic — notify only                              │
└──────────────────────────┬──────────────────────────────────┘
                           │
     Market WS · Memory · Social · Funding · Facts · OHLCV
```

### Service-Grenzen

| Verantwortung | Sniper | Bot |
|---------------|--------|-----|
| Kandidaten finden & ranken | ✅ | Snapshot + Preise |
| Volle Analyse | ✅ | — |
| DCA Size + Cash-Plan | ✅ request | Risk final + floor |
| Fund-from-winner (Profit realisieren) | ✅ request + Analyse | ✅ sell + ledger (reuse rotation policy) |
| DCA ausführen | Request | ✅ execute + ledger |
| Recovery-Hold Flag | Request | ✅ persist |
| Trail/TTP/BB blocken | Policy | ✅ fire path |
| Hard SL / BE+ promote | Monitor (WS) | ✅ enforce |
| Grid | exclude heavy | grid bleibt Bot |

### Deployment (Staging)
- Railway Service: `xagent-dca-sniper`  
- Same repo, start: `python -m services.dca_sniper`  
- Env: `DCA_SNIPER_TOKEN`, `BOT_INTERNAL_URL`, `MONGO_URL` (read), market APIs, optional WS URLs  
- Kill: `DCA_SNIPER_ENABLED=0` oder Service stoppen → Bot DCA-Cycle optional legacy off

---

## 3a. Dynamischer Cash + „Gewinner → DCA-Cash“ (Operator-Idee)

**Bewertung: gut und stack-aligned** — nicht neu erfinden, sondern sniper-gesteuert **schärfen**.

### Was schon existiert

| Baustein | Heute |
|----------|--------|
| `risk/cash_policy.py` | `spendable_new` ≠ `spendable_dca`, Mode DEPLOY/STEADY/HARVEST, DCA-Buffer, Floor-Haircut |
| `dca_portfolio.find_funding_sell` | Stale-Winner / Tail / Ladder-Terminal → Sell **um DCA zu finanzieren** |
| `run_portfolio_dca_pass` | Fund-Sell dann BUY_DCA im selben Pass |
| Adaptive-Cash Master (#89/#90) | Floor/Spendable folgt Regime — Phase 2–4 (Rotation-Tempo) teils noch offen |

### Zielbild für den Sniper

```text
Starker Dip + Checklist YES
        │
        ▼
  Cash-Plan: need = size(heavy)
        │
        ├─ spendable_dca ≥ need  →  DCA_HEAVY sofort (auto)
        │
        ├─ spendable_dca < need
        │     aber free cash / policy erlaubt Shift
        │     → optional **DCA-Priority Claim**:
        │       kurzfristig mehr von free cash dem DCA-Puffer zuweisen
        │       (nie unter hard floor; never break cash_floor)
        │
        └─ immer noch zu wenig
              → rank profitable bags (sauber analysiert)
              → FUND_SELL (partial/full nach Policy)
              → dann DCA_HEAVY
              → Profit mitnehmen + Recovery finanzieren
```

### Regeln (damit es nicht „alles liquidieren für einen Dip“ wird)

1. **Floor unantastbar** — dynamisch nur oberhalb Floor / innerhalb cash_policy Modes.  
2. **DCA-Priority Claim (soft)** — wenn Focus YES Heavy und Score ≥ threshold:  
   - darf `spendable_dca` kurzzeitig aus freiem Cash **auffüllen** (Cap: z.B. ≤ X % equity oder 1× heavy max),  
   - **nicht** endlos Entry-Budget plattmachen (Cap pro Tag / pro Focus).  
3. **Fund-from-winner nur mit Analyse** (kein blindes Sell-Everything):  
   - reuse `can_rotation_evict` / rotation + Memory/soft_block  
   - Prefer: stale winner, ladder-done, tail idle, gain ≥ min  
   - **Nie** recovery_hold / sniper_focus-Bag verkaufen um andere zu füttern  
   - **Nie** pure grid-level inventory blind full-exit (Grid exclude / nur policy-safe slices)  
   - Max 1 fund-sell pro Focus-Versuch; Partial erlaubt wenn genug USDT  
4. **Order of capital:** (1) existing spendable_dca → (2) soft claim free cash → (3) fund-sell winners → (4) WAIT  
5. **HARVEST/RISK_OFF:** Policy kann Heavy skippen oder Size drosseln (bestehende `dca_policy`) — Sniper gehorcht.  
6. **Auto, kein Approve** — aber volle Audit-Logs (why cash claim / why fund-sell).

### Warum das gut ist

- Cash wird **Werkzeug**, nicht toter Parkplatz (passt zu Adaptive-Cash North Star: „immer DCA-fähig“).  
- Profit-Rotation **mit** Analyse = Fiete mitnehmen **und** Recovery-Kapital, statt Zombie-Winner + tote Rote.  
- Serial Focus verhindert, dass 10 Dips gleichzeitig Cash und Winner-Sells fressen.

### Risiken / Guardrails

| Risiko | Mitigation |
|--------|------------|
| Winner zu früh verkauft, läuft weiter | min_gain + trail not just armed; memory prefer hold; partial first |
| Entry-Hunger stirbt weil alles in DCA | daily DCA cap + claim caps + dynamic N focus + min_cash_after |
| Cascade: sell winner → dip more → panic | fund-sell nur bei confirmed heavy YES; cooldown |
| Doppel-Sells mit Cycle rotation | eine Authority: Sniper-fund requests taggen; Bot dedupe |

### Phasen-Einordnung

| Phase | Lieferumfang Cash |
|-------|-------------------|
| P1 | Size only aus aktuellem `spendable_dca` (Policy as-is) |
| P2 | Soft DCA-priority claim (Config) wenn Heavy ready |
| P2–P3 | Fund-from-winner API reuse `find_funding_sell` + analyse gate + auto chain execute |

---

## 3d. Performance: WebSockets wo sinnvoll

**Prinzip:** Fast path = WS/ticks; Slow path = deep analysis. Nicht alles im 15‑Min-Poll.

| Signal | Transport | Ziel-Latenz |
|--------|-----------|-------------|
| Preis Focus / Shortlist | **Exchange WS** oder Bot price hub (wie exit-radar) | sub-sekund–wenige s |
| Dip-Trigger (loss band / cascade drop) | WS mark → wake analyzer | schnell |
| BE+ promote / Hard-SL proximity | WS mark vs avg/SL | schnell |
| recovery_hold monitor | WS + state | schnell |
| Full checklist (TA multi-TF, funding, social, memory, facts) | async HTTP/cache, **nicht** tick-hot | Sekunden–Minuten ok |
| Rank universe (alle roten Bags) | periodisch 1–5 min + on WS wake | mittel |
| Execute / fund-sell | Bot internal HTTP (auth) — Ausführung bleibt Bot | nach Decision |

### Architektur-Skizze WS

```text
Exchange WS (or Bot exit-realtime hub)
        │ ticks for watchlist
        ▼
 Sniper Fast Loop  →  trigger events (DIP, BE_PLUS, SL_NEAR)
        │
        ▼
 Deep Worker (queue, max 1 heavy analysis concurrent)
        │
        ▼
 Cash plan → optional fund-sell → dca-execute
```

### Patterns aus dem Repo (reuse)

- `services/exit_realtime/*` — watch/fire HTTP + hub (Vorbild Fan-out)  
- Gainer internal routes — token’d bot callbacks  
- **Nicht** den ganzen Cycle auf WS legen; nur sniper-relevante Symbole

### Config-Ideen

```text
dca_sniper.ws_enabled = true
dca_sniper.ws_symbols = focus + top_k shortlist
dca_sniper.poll_fallback_sec = 60   # wenn WS down
dca_sniper.deep_analysis_cooldown_sec = ...
```

**Kill:** WS off → pure poll (degraded, still correct).
---

## 3b. Bestehender DCA-Stack — **reuse, nicht über den Haufen**

Heute schon live (Config Staging-Richtung `volatile_altcoin.dca`):

| Modul | Rolle heute | Sniper-Relation |
|-------|-------------|-----------------|
| `strategies/dca.py` | Hard gates (loss band, rounds, interval, SL-proximity, remainder) + Scoring (ATR/RSI/funding/BTC/BB) | **Reuse** als Basis-Hard-Gates + Score-Kern; Sniper **erweitert** Checklist (Memory, facts, social, multi-TF, portfolio) |
| `strategies/dca_sizing.py` | Adaptive size: `fixed_usdt` Anchor + `notional_ratio` (z.B. 0.3) + score/loss/round mult + min/max caps | **Reuse & schärfen** für Heavy: höhere f / bag-relative Prefer, weiter cap by `spendable_dca` — **kein fester 2k** |
| `strategies/dca_policy.py` | cash_mode HARVEST skip, deploy boost, fusion, calendar, soft_block | **Reuse** vor Execute (Policy-Skip gilt auch Sniper) |
| `strategies/dca_portfolio.py` | Rank Targets, max_buys/cycle, optional fund-sell | Portfolio-Pass bleibt für **Light/Legacy** bis Sniper Authority; Focus-Heavy **nur** Sniper |
| `strategies/dca_scheduled.py` | Wochen-Split DCA | **Default off** — unberührt; nicht Sniper-Scope |
| Grace / reanchor | `grace_hours_after_dca`, `pause_trail_*`, `reanchor_peak_on_dca` | **Behalten**; P0 `recovery_hold` ist **stärker/länger** nur für Focus-Bags |
| Risk | `spendable_dca`, cash floor, daily DCA caps | **Immer final** — Sniper umgeht Risk nie |

### Authority-Modell (kein Big-Bang)

```
DCA_SNIPER_ENABLED=0  →  alles wie heute (Cycle/DE + portfolio DCA)
DCA_SNIPER_ENABLED=1  →  Heavy/Focus nur Sniper;
                         Cycle auto-DCA: AUS oder nur non-focus light (Config)
                         scheduled: weiterhin eigenes Flag (default off)
                         execute path: weiter RiskManager + ledger
```

**Wichtig:** Wir werfen `evaluate_dca_addon` / Policy / Sizing **nicht weg**. Sniper orchestriert tiefer + serial; Bot-Module bleiben Shared Library.

### Was Sniper **neu** bringt (Delta)

1. **Quality-first Focus 1…N** — dynamisch nach Cash + Analyse, nicht starr 1 und nicht blind parallel peanuts  
2. **Deep async checklist** (Minuten ok; nicht nur Cycle-Indikatoren)  
3. **`recovery_hold` / sniper_focus** über Grace hinaus (gegen BEAT/BLESS-Klasse)  
4. **Heavy sizing Prefer** bag-relativ mit Fakten-Score — nicht nur `fixed_usdt`±mult  

---

## 3c. Grid-Positionen — **getrennt behandeln**

Grid (`strategies/grid.py`, pure/hybrid tiers) ist **eigene Ökonomie**: Level-Sells, Re-Center, slice TP im Band. DCA-Nachkauf auf Grid-Rest ist **nicht dasselbe** wie Momentum/Runner-Recovery.

| Frage | Entscheidung v1 |
|-------|-----------------|
| Sniper Heavy auf **pure_grid** / `strategy_profile=grid`? | **Nein (exclude)** — Grid hat eigene Level-Buys; Heavy + recovery_hold würde Grid-TP/BB killen und die Grid-Logik entkernen |
| Hybrid / volatile mit Grid-Anteil? | Default **exclude** wenn `strategy_profile==grid` oder active grid_plan; optional later „soft recovery“ ohne hold |
| recovery_hold blockt „BB / grid TP“? | **Nur** wenn `recovery_hold` gesetzt — und Hold wird **nicht** auf Grid-Focus gesetzt |
| DCA legacy auf Grid-Bags? | Heute: DCA hat **keinen** harten Grid-Exclude (kann theoretisch feuern). Sniper ändert das nicht global; optional später: `dca.skip_grid_profiles=true` |
| Was bleibt für Grid im Minus? | Grid rebuy levels + Risk; **kein** Sniper-Hold |

→ Sniper-Kandidaten-Filter: **skip if grid profile / pure_grid class** (explizit in candidates + rank).

---

## 4. Produktlogik: Quality-first Focus (1…N, cash- & analyse-gesteuert)

**Nicht** „immer nur einer“. **Auch nicht** „alle Roten kaufen“.  
Default-Denke: erst die Besten tief; **wenn Cash und Analysen es hergeben → zweiter, dritter, … bis Cap**.

```
alle roten Bags mit DCA-Potential
        │
        ▼
   score & rank (schnell vorfiltern) → ordered list #1, #2, …
        │
        ▼
   N_eff = dynamic_focus_slots(cash, scores, config)
        │  z.B. 1 wenn knapper Cash / nur #1 stark
        │      2…N wenn spendable groß + mehrere YES Heavy
        ▼
   for candidate in top while open_focus < N_eff:
        deep analysis ──► NEIN → skip
        │ JA
        ▼
     size_i = individual(rest, score, cash_left, caps)
     if size_i < min_meaningful or cash_left zu dünn → stop adding
        │
        ▼
     set recovery_hold / sniper_focus
     execute DCA (auto)
     cash_left -= size_i
        │
        ▼
   each focus holds until: BE+ | hard SL | timeout → clear that slot
```

### Wie `N_eff` entsteht (Konzept)

| Input | Wirkung |
|-------|---------|
| Checklist-Qualität der Top-k | nur Coins mit Heavy-YES zählen |
| `spendable_dca` (+ soft claim + optional fund) | wie viele full sizes noch passen |
| `max_focus_slots` (Config, z.B. 3–4) | harter Deckel |
| `min_cash_after_focus` / Floor | nie Floor knacken; Reserve lassen |
| BTC/RISK_OFF / HARVEST policy | N runter oder 0 |
| Schon offene Focus-Holds | belegte Slots zählen gegen N_eff |

**Beispiele (nur Logik, keine Fix-€):**
- 1 sehr starker Dip, knappes free cash → **N=1**, dicker Add  
- 3 starke Dips, viel spendable, alle YES → **N=3**, jeweils individuelle Size (nicht 1/3 peanut willkürlich)  
- 8 schwache Roten → **N=0** WAIT, nicht streuen  

**Regel:** Neue Heavies nur solange Slot + Cash + Analyse JA.  
Legacy Cycle-DCA Heavy: **aus** wenn Sniper enabled (eine Authority); Light optional Config.
---

## 5. Volle Analyse-Checkliste (Deep Pass)

Nicht „3 Indikatoren“ — **Schichten**. Hard-Veto bricht ab. Soft-Scores summieren.

### 5.1 Position & Bag (Hard / Soft)

| ID | Check | Hard? | Pass-Idee |
|----|--------|-------|-----------|
| P1 | Unrealized in DCA-Band | Hard | z.B. −8 % … −40 % (profilabhängig) |
| P2 | Rounds left / heavy not used | Hard | max rounds; heavy_used flag |
| P3 | Interval since last buy | Hard | ≥ 6–12h (profil) |
| P4 | Not within hard-SL proximity | Hard | z.B. >10–12 % über SL |
| P5 | Remainder notional ≥ floor | Hard | lohnt Heavy (z.B. ≥ €800) |
| P6 | Time in red | Soft | zu lang ohne Struktur → score− |

### 5.2 Marktstruktur & TA (Kern)

| ID | Check | Hard? | Pass-Idee |
|----|--------|-------|-----------|
| T1 | Structure: dump flacht / höheres Tief | **Hard** | kein freier Fall |
| T2 | RSI: oversold **oder** reclaim | Soft→Hard für Heavy | nicht „mid RSI + Dump“ |
| T3 | BB / %b nahe low oder Reclaim mid | Soft | |
| T4 | Volume climax dann Abklingen | Soft/Hard Heavy | 3–5× avg dann ruhiger |
| T5 | ATR-distance: Dip meaningful | Hard | ≥ min_atr_mult |
| T6 | BTC regime | Soft/Hard | BTC crash → skip Heavy |
| T7 | Multi-TF align (1h + 4h) | Soft | 4h nicht total broken |

### 5.3 Derivate (wenn Markt existiert)

| ID | Check | Hard? | Pass-Idee |
|----|--------|-------|-----------|
| D1 | Funding nicht extrem long | Soft | |
| D2 | Funding neg + OI down = flush better | Soft+ | |
| D3 | Price↓ + OI↑ = new shorts | Soft veto | |
| D4 | Illiquid / no perp | Skip layer | kein Fake-Grün |

### 5.4 On-Chain / Token Facts (nur bei Datenqualität)

| ID | Check | Hard? | Pass-Idee |
|----|--------|-------|-----------|
| O1 | Unlock / vesting nahe | **Hard veto** | coin_facts |
| O2 | hard_negative / structure_risk | **Hard veto** | |
| O3 | Exchange inflow spike | Soft veto | wenn API |
| O4 | Active addresses collapse | Soft | |
| O5 | Whale to exchange | Soft veto | |
| O6 | No reliable on-chain | **Skip** | score neutral, nicht grün lügen |

### 5.5 Social / Sentiment

| ID | Check | Hard? | Pass-Idee |
|----|--------|-------|-----------|
| S1 | Santiment / fusion block_buys, CRASH | **Hard** | |
| S2 | Galaxy / LC not free-fall only | Soft | |
| S3 | CMC interest still exists | Soft | |
| S4 | X narrative: unlock/rug dominant | Soft veto | |
| S5 | Social **alone never YES** | Policy | |

### 5.6 Memory

| ID | Check | Hard? | Pass-Idee |
|----|--------|-------|-----------|
| M1 | entry_bias soft_block | **Hard** or tiny-only | |
| M2 | Past DCA on symbol often failed | Soft/Hard | |
| M3 | Low samples | Soft | kein Auto-Heavy nur wegen Memory-Ja |
| M4 | Prefer + good recovery history | Soft boost | |

### 5.7 Portfolio & Liquidity

| ID | Check | Hard? | Pass-Idee |
|----|--------|-------|-----------|
| C1 | spendable_dca / cash floor | **Hard** | |
| C2 | Concurrent focus ≤ N_eff ≤ max_focus_slots | **Hard** | dynamisch aus Cash+Qualität |
| C3 | Recovery budget % equity | **Hard** | z.B. ≤15–20 % |
| C4 | Max bag % after add (profil) | **Hard** | |
| C5 | Order ≤ k% 24h quote vol | **Hard** | |
| C6 | Book drawdown throttle | Hard/Soft | |

### 5.8 Scoring & Entscheidung

```
if any Hard fail → SKIP (next candidate or WAIT)
else score = weighted sum(T,D,O,S,M)
if score >= heavy_threshold AND open_focus < N_eff AND cash_left ok → DCA_HEAVY
elif score >= light_threshold → optional small (v1: skip light, only heavy or nothing)
else → WAIT / next candidate
# next candidates same loop until N_eff or cash/quality stop
```

**v1 Empfehlung:** Sniper macht **Heavy oder nichts** pro Coin (kein Nibble) — aber **mehrere** Heavies parallel erlaubt, wenn `N_eff` und Cash es tragen.

---

## 6. Sizing — **individuell, datengetrieben, nie Fixbetrag**

> **€2k im Beispiel ist KEINE Regel.** Der Sniper (über `dca_sizing` + Policy + Checklist) entscheidet pro Coin, was nach einem massiven Dip sinnvoll ist.

### Entscheidungs-Inputs (Priorität)

1. **Fakten / Checklist-Qualität** — Score/Confidence: schwach → kein Heavy (oder WAIT); stark → f hoch  
2. **Bag / Rest-Notional** — Add skaliert mit Rest (große Bag ≠ gleicher €-Betrag wie kleine)  
3. **Profil** (major / mid / meme) — Max Bag %, Max single, Rounds  
4. **Cash** — `spendable_dca` (über Cash-Floor), daily room, optional recovery budget % equity  
5. **Liquidität** — Order ≤ k% 24h quote volume / Book  

### Cash-Floor
- Floor (~12 % NAV) **unantastbar**  
- Heavy nur aus **`spendable_dca`**  
- Daily caps (`max_daily_dca_usdt`) bleiben  

### Bestehende Sizing-Bausteine (bereits im Bot)

Config-Richtung heute u.a.:
- `fixed_usdt` = **Anchor**, nicht Final (z.B. 1000)  
- `sizing.notional_ratio` ≈ 0.3, `base_mode: max` → `max(anchor, rest×ratio)`  
- score/loss/round Mults, `min_usdt` / `max_usdt` (z.B. 500–2800)  
- `dca_policy` mult (deploy/harvest/soft_block)  
- Cap: `min(..., spendable_dca)`  

Sniper-Heavy **baut darauf auf**, schraubt Prefer auf bag-relativ höher, wenn Checklist „YES Heavy“ — ohne Hardcode 2000.

### Formel (Konzept)

```
f = f_profile * f_score(checklist) * f_policy   # f_score aus Daten, nicht fix

raw = rest_marktwert * f

add = min(
  raw,
  max_bag_equity - bag_now,
  max_single_add_profile,
  spendable_dca,       # cash floor respektiert
  daily_dca_room,
  liq_cap
)
# wenn add < min_meaningful → SKIP (kein Peanut-Forced)
```

### Illustrative Caps @ ~€100k Equity (Defaults, **tunable**)

| Profil | f-Range × Rest | Max Bag % Eq | Max single (Cap) | Heavy rounds |
|--------|----------------|--------------|------------------|--------------|
| Major | 0.5–0.7 | 8 % | Cap z.B. 3k | 2 + selten Heavy |
| Volatile mid | 0.7–1.0 | 6 % | Cap z.B. 3k | 2–3 + 1 Heavy |
| Meme/thin | 0.8–1.0 | 4 % | Cap z.B. 2.5–3k | 2 + **1 Heavy max** |
| Memory soft_block | 0 / tiny | — | 0 | 0 |
| **Grid profile** | — | — | — | **Sniper skip** |

**Nur Beispielrechnung** (−50 %, Entry-Bag 4k → Rest ~2k, f≈1, Cash ok):  
→ Add **kann** ~2k sein — aber bei Rest 800, schlechtem Score oder knapperem spendable auch 400 oder **0 (WAIT)**.  
Nie: „immer 2k weil Plan sagt 2k“.
---

## 7. Exit-Schutz (damit Focus nicht „BLESS-mäßig“ stirbt)

### Position Flags (Bot ledger)
- `sniper_focus: bool`
- `recovery_hold: bool`  
- `dca_heavy_used: bool`
- `recovery_entered_at`, `last_dca_at`
- `peak_epoch_high` (seit letztem DCA)

### Während `recovery_hold` / `sniper_focus` (nur diese Bags)
| Quelle | Erlaubt? |
|--------|----------|
| Hard full SL | ✅ |
| Trailing stop | ❌ |
| Trailing TP | ❌ |
| Partial SL | ❌ |
| BB upper / runner TP | ❌ |
| Grid level TP | n/a — Grid-Bags bekommen **kein** recovery_hold (siehe §3c) |
| Social sell | ❌ |
| Manual /sell | ✅ |
| Sniper-triggered exit plan later | ✅ (Phase 2) |

Normale Grid-/Runner-Positionen **ohne** Hold: Exit-Logik **unverändert** (inkl. Grid-Sells).
### Promote aus Hold (v1 simpel, auto)
- Mark ≥ **Avg × (1 + be_buffer)** **+2 %** → `recovery_hold=false`, `exit_state=RUNNER` (auto)  
- Timeout 14d ohne Progress → Alert + clear focus (kein panisches Full-Trail)  
- Hard-SL jederzeit

### Peak
- Nach jedem Sniper-DCA: `peak_epoch_high = max(fill, avg)`  
- Trail nur mit Epoch-Peak, nie Lifetime pre-DCA (BEAT)

---

## 8. Service API (Skizze)

### Sniper → Bot
`POST /internal/dca-sniper/execute`  
```json
{
  "symbol": "BLESS/USDT",
  "timeframe": "1h",
  "usdt": 2000,
  "reason_code": "DCA_HEAVY",
  "set_recovery_hold": true,
  "analysis_id": "uuid",
  "score": 8.2,
  "checklist": { "T1": true, "O1": true, ... }
}
```

### Bot → Sniper
`GET /internal/dca-sniper/candidates` — offene Bags: symbol, avg, amount, uPnL%, dca_rounds, flags, last_dca_at

### Sniper internal
- `GET /health`, `GET /status` (focus, last decisions, queue)
- Loop interval: z.B. 5–15 min (nicht Tick-Hot-Path)

---

## 9. Phasen

| Phase | Lieferumfang | Done when |
|-------|--------------|-----------|
| **P0** | Bot: `recovery_hold` enforce + peak_epoch on DCA; Kill flags | 0 Trail-Full auf hold-Bags |
| **P1** | Service skeleton + candidates + rank + deep checklist + Heavy size + execute aus **current** spendable_dca; poll + **WS price wake** for focus/shortlist | 1 Focus-DCA staging; WS or poll fallback |
| **P2** | Social/facts layers; **soft DCA cash claim**; Telegram notify-only | Checklist + cash priority live |
| **P3** | **Fund-from-winner** chain (analyse + rotation reuse) + BE+ promote WS; cycle heavy DCA off | Serial pipeline + funded heavies |
| **P4** | Metrics: focus win rate, time-to-BE, fund-sell→dca success, accidental sell = 0, WS wake latency | Report |
---

## 10. Kill / Rollback

| Kill | Wirkung |
|------|---------|
| `DCA_SNIPER_ENABLED=0` | Service idle; optional Bot legacy DCA config |
| `RECOVERY_HOLD_ENFORCE=0` | alte Exit-Welt (nur Notfall) |
| `max_focus_slots=0` | keine Heavies |
| Service stop | keine neuen Sniper-DCAs; Hold-Flags bleiben bis clear |

---

## 11. Success Metrics (Staging)

| Metric | Ziel 7–14d |
|--------|------------|
| Auto-Full-Sell auf `recovery_hold` | **0** (außer Hard-SL) |
| Focus concurrent | ≤ `max_focus_slots` und ≤ Cash/Qualität (`N_eff`) |
| Median Add / Rest-Notional bei Heavy | ≥ 0.5 |
| Sniper decisions logged with full checklist | 100 % |
| Time-to-BE+ after Heavy (wenn recovery) | tracken |
| Cash floor breaches by Sniper | 0 |

---

## 12. Risiken (kurz)

| Risiko | Mitigation |
|--------|------------|
| Falscher #1 bindet Cash | strenge Checklist + timeout + hard SL |
| Service down, keine DCA | bewusste Pause besser als falsche Parallel-DCA |
| Doppelte DCA Authority | Cycle-DCA aus wenn Sniper on |
| On-Chain noise | skip layer wenn quality low |
| Redeploy kills Hermes peaks | peak_epoch im **Mongo position** doc, nicht nur File |

---

## 13. Ticket-Schnitt (Umsetzung später)

1. **Bot P0:** recovery_hold + sell gates + peak_epoch on any DCA fill  
2. **Bot API:** candidates + execute internal  
3. **Service:** scaffold Railway + loop + rank  
4. **Service:** full analyzer modules (TA, funding, memory, facts, social, portfolio)  
5. **Service:** size engine (profile table)  
6. **Wire:** disable cycle auto-DCA when sniper enabled  
7. **Observability:** status endpoint + logs + optional TG  
8. **Staging soak** + report  

---

## 14. Operator one-liner

> **Quality-first DCA Sniper: 1…N Focus je nach Cash+Analyse, individuelle Heavy-Adds, Hold bis geheilt, Floor tabu — kein Approve, kein Peanut-Spray, kein versehentlicher Trail-Kill.**

---

## 15. Festgelegt (Operator 2026-08-09)

| Thema | Entscheidung |
|--------|----------------|
| Approve | **Kein manuelles Approve** — vollautomatisch; optional Notify-only (TG info, never gate) |
| Focus slots | **Dynamisch 1…N** (Quality + Cash); Cap `max_focus_slots` (Config, z.B. 3–4); nicht starr 1 |
| Bestehendes DCA | **Reuse** hard gates, scoring core, `dca_sizing`, `dca_policy`, Risk — Sniper orchestriert Heavy/Focus; kein Delete des Stacks |
| Cycle-DCA wenn Sniper on | Heavy/Focus **nur** Sniper; Cycle light default **aus** (Config-Flag, kill-reversible) |
| Scheduled DCA | Unberührt (default off) |
| Sizing | **Individuell** (Fakten + Bag + Cash + Caps) — **kein Fix-USDT** |
| Grid | **Kein** Sniper-Heavy / **kein** recovery_hold auf pure/active grid profiles |
| Cash / vCash Floor | Floor bleibt; Heavy primär **spendable_dca**; optional soft claim + fund-from-winner (§3a) |
| Fund-from-winner | Ja, analysiert, max 1 pro Focus, reuse rotation policy — Profit + DCA-Cash |
| Transport | **WS** für Preise/Trigger/BE+; deep analysis async; poll fallback |
| BE+ Buffer | **+2 %** über Avg (Default Spec) |
| Timeout Recovery | **Alert + clear focus** nach 14d ohne Progress; kein panisches Full-Trail; Hard-SL bleibt |

---


## 16. GitHub Tickets (live)

| Issue | Title | Scope |
|-------|-------|--------|
| **#222** | Epic: DCA Sniper Service + Recovery Hold | Umbrella, decisions locked |
| **#223** | P0: recovery_hold + peak_epoch + block auto sells | Bot sell-path gates first |
| **#224** | P1: bot internal candidates + execute API; disable cycle DCA | Bot API + authority |
| **#225** | P1–P2: xagent-dca-sniper service (auto focus heavy) | Service rank/analyze/size/auto-exec |
| **#226** | P2: observability + BE+ promote + metrics | Notify-only TG, promote, soak metrics |

**Implement order:** #223 → #224 → #225 → #226 (Epic #222 tracking).

**Non-negotiables in every ticket:**
- Fully automatic — **no human approve gate**
- Size only via **spendable_dca**; **cash floor never breached**
- Staging Demo first; no production deploy without explicit user ask

---

## 17. Super Goal-Mode Prompt (canonical — copy-paste / agent start)

```
# GOAL
Ship Epic #222 DCA Sniper + Recovery Hold fully automatic on STAGING.
Spec SSOT: plans/dca-sniper-service.md (all sections, esp. §3a cash, §3b reuse, §3c grid, §3d WS, §4 dynamic N, §6 sizing, §7 hold, §15 locked).

# TICKETS (order; close with evidence comments)
1) #223 P0 — recovery_hold + peak_epoch_high + block auto sells (cycle + exit WS); Hard SL always; BE+ promote +2%
2) #224 P1 — GET/POST internal candidates+execute (token); set hold on fill; disable cycle heavy DCA when sniper on; Risk always final
3) #225 P1–P2 — services/dca_sniper: WS price wake + poll fallback; rank top-k; full checklist; individual size; auto exec NO approve; N_eff=1…max_focus_slots from cash+quality; soft cash claim; grid exclude
4) #226 P2–P3 — fund-from-winner (rotation reuse, analysed); observability; metrics; TG notify-only

# PRODUCT RULES (non-negotiable)
- FULLY AUTOMATIC — no human approve gate anywhere (TG notify only)
- Sizing INDIVIDUAL from checklist quality + bag rest + profile + spendable_dca + liq — NEVER fixed €2k
- Cash order: spendable_dca → soft claim free cash ABOVE floor → fund-from-winner → WAIT; floor NEVER breached
- Focus slots DYNAMIC 1…N (quality + cash); hard cap max_focus_slots; peanut-spray forbidden; multi-focus OK when justified
- REUSE dca.py gates/scoring, dca_sizing, dca_policy, cash_policy, find_funding_sell — extend, do not delete stack
- recovery_hold bags: block trail/TTP/partial/BB/social auto sells; allow hard SL + manual; no fund-sell of hold bags
- Grid pure/active profiles: NO sniper heavy, NO recovery_hold
- WS for price/triggers/BE+; deep analysis async; poll fallback if WS down
- BE+ clear hold at mark ≥ avg×1.02; 14d no progress → alert + clear focus (no panic trail)
- Staging only; no production deploy unless operator explicitly asks

# SUCCESS
- 0 accidental auto full-sells on recovery_hold except Hard SL
- Concurrent focus ≤ N_eff ≤ max_focus_slots
- Sizes vary with data; 0 floor breaches; fund→dca audited
- Grid without hold unchanged; checklist logged 100%; WS+fallback works

# ENGINEERING
- Minimal surface; mirror exit_realtime / gainer internal token patterns
- Unit tests per ticket; run targeted pytest; short commits; update GH issues
- Do not churn unrelated entry/gainer work
```

### Agent can self-start
This section is enough to run Goal Mode without further product Q&A. Implement #223 first.

---

*Ende Spec. Tickets #222–#226. Super Goal = §17.*
