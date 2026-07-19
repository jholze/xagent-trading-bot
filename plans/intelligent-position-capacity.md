# Plan: Intelligent Position Capacity (`max_open` als System, nicht als Starre 24)

> **Status:** Proposal / research + design  
> **GitHub:** [#110](https://github.com/jholze/xagent-trading-bot/issues/110) (child of #89)  
> **Anlass:** Lorenzo (BANK) mehrfach Sensor-Ready, aber `Max open positions (24)` — starrer Cap seit 17.07.2026 (40→24 mit Cash-Floor)  
> **Verwandt:** [#89 Adaptive Cash](https://github.com/jholze/xagent-trading-bot/issues/89) · [adaptive-cash-rotation-master.md](adaptive-cash-rotation-master.md) · [market-context-entry-throttle.md](market-context-entry-throttle.md) · Sensor Memory Gates · Grid min-gain  
> **Nicht ersetzen:** Venue-Guard, soft_block, Cash-Floor-Grundidee — **komponieren**

---

## 0. One-liner

**Statt einer festen Zahl: pro Cycle ein intelligentes `max_open_eff` aus Kapazität (Cash) × Markt-Regime (Fusion) × Memory-Qualität × Setup-Priorität — plus optional Slot-Freigabe für High-Conviction-Entries.**

---

## 1. Ist-Zustand (kurz)

| Heute | Verhalten |
|-------|-----------|
| `config.max_open_positions = 24` | **starr** (Commit `74e1cc0`: 40→24 + cash floor 18 %) |
| Risk | `count_open_full_slots() >= max` → reject neue Entries |
| Tails | zählen oft **nicht** als Full-Slot → **>24 Bags** möglich, neue Buys trotzdem blocked |
| Fusion `size_mult` / `block_buys` | skaliert **Size**, ändert **nicht** den Slot-Cap |
| Memory soft_block / facts | pro Coin Entry — **kein** Portfolio-Capacity |
| Cash floor | blockt bei knapper Free-Cash, unabhängig von Slot-Score |

**Lücke Lorenzo:** Setup war da (Watchlist + 15m-Spikes ≥3×), **Kapazität war nein** (Slots voll). Intelligent = *bei gutem Setup + risk-on Platz schaffen oder reservieren*, bei risk-off *enger* werden.

---

## 2. Research-Findings (Industrie / Quant)

### 2.1 Freqtrade (Open-Source Crypto Bot)

- `max_open_trades` ist **required**, oft **fest** in Config/Strategy.
- Mit `stake_amount: unlimited` wird Equity **durch max_open_trades geteilt** → Slot-Anzahl steuert implizit Positionsgröße.
- Pairlist begrenzt zusätzlich (nur 1 Trade pro Pair).
- **Takeaway:** Industry-default ist starr; „intelligent“ liegt oft in **Pairlist + Stake**, nicht in dynamischem Cap. Für uns lohnt **beides**: adaptive Cap **und** adaptive Size (haben wir teils).

### 2.2 Kelly / Risk-Budget

- Kelly: Größe ∝ Edge / Odds — bei uns Edge unscharf → **Fractional Kelly** / Caps (half-Kelly) üblich.
- Portfolio-Kelly: Summe der Positionsrisiken ≤ Budget → impliziert **Obergrenze an gleichzeitigen Wetten**.
- **Takeaway:** Cap sollte an **Risk-Budget pro Trade × Equity** hängen, nicht an einer magischen 24.

### 2.3 Regime-adaptive Systeme

- Risk-on: mehr Exposure / mehr parallele Ideen.  
- Risk-off / Crash: Exposure runter, oft **weniger** neue Concurrent Positions.  
- QuantInsti u.a.: Regime-Switching für Sizing und Aktivität — **nicht** nur pro Coin.

### 2.4 Diversifikation vs. Konzentration

- Zu viele kleine Bags → Kapitalsplit, Slot-Zombies, Lorenzo-Miss.  
- Zu wenige → Idiosynkrasie.  
- Faustregel aus Praxis: **8–20** liquide Concurrent Ideas oft genug; darüber sinkt Marginalnutzen.

### 2.5 Bot-Ops (3Commas / Retail Guides)

- Caps + Daily Trade Limits + Drawdown-Stop.  
- **Takeaway:** Safety rails behalten (min/max Cap), Intelligenz **innerhalb** des Korridors.

---

## 3. Design-Prinzipien (für uns)

1. **Korridor, nicht Chaos:** `min_open_floor` … `max_open_ceiling` (z.B. 12…40).  
2. **Fail-open soft:** fehlende Fusion/Memory → neutrales Cap (z.B. 24), nie crashen.  
3. **Eine Stelle:** `resolve_max_open_eff()` im Risk-Pfad (wie Fusion size_mult einmal).  
4. **Full slots only:** weiter `count_open_full_slots` (Tails freilassen).  
5. **Sells nie durch Capacity blocken.**  
6. **Observability:** Reject-Code `max_open_positions` + `max_open_eff` + `rationale` im Order/Risk.  
7. **Shadow first:** eine Woche nur loggen „hätte Cap X erlaubt“, dann live.

---

## 4. Zielarchitektur

```text
                    ┌──────────────────────────────┐
                    │  Capacity Controller (neu)   │
                    │  resolve_max_open_eff()      │
                    └──────────────┬───────────────┘
           inputs                  │
  ┌────────┴────────┬──────────────┼──────────────┬─────────────┐
  │ Fusion regime   │ Cash/Equity  │ Memory book  │ Burst/Warm  │
  │ size_mult       │ spendable    │ soft_blocks  │ restart     │
  │ block_buys      │ floor_eff    │ fact toxicity │             │
  └─────────────────┴──────────────┼──────────────┴─────────────┘
                                   ▼
                    max_open_eff ∈ [min, max]
                                   │
          ┌────────────────────────┼────────────────────────┐
          ▼                        ▼                        ▼
   Risk: new entry?         Optional: Slot         Sensor priority
   full_slots >= eff        eviction candidate     queue / reserve
   → reject or allow        (Phase 2)              (Phase 2)
```

### 4.1 Formel (v1 — erklärbar, testbar)

```text
base        = config.max_open_positions          # z.B. 24 „neutral“
cash_term   = f(spendable / avg_entry_usdt)      # wie viele Full-Sizes passen?
regime_term = g(fusion.regime, size_mult)          # RISK_ON +2..+6, RISK_OFF −4..−8, CRASH → min
memory_term = h(soft_block_count, toxic_share)     # viele schlechte Namen → enger
burst_term  = −warmup_slots if process_age < T     # Restart-Burst dämpfen

max_open_eff = clamp(
    round(base + regime_adj + cash_adj + memory_adj + burst_adj),
    min_open_floor,   # z.B. 12
    max_open_ceiling  # z.B. 36
)

# Hard overrides
if fusion.block_buys or CRASH:  max_open_eff = min(max_open_eff, min_open_floor)
if cash_floor blocks new entries: no new slots (eff irrelevant)
```

**Beispiel Lorenzo-Tag (risk-on, Cash ok, aber 24 full):**  
- `regime_adj = +4` → eff 28, **oder**  
- Phase 2: **evict** schwächsten Zombie → Slot frei bei eff 24.  

Beides ist „intelligent“; Eviction ist produktnäher an eurem North Star (Rotation).

---

## 5. Input-Signale (was wir schon haben)

| Signal | Quelle | Nutzung für Capacity |
|--------|--------|----------------------|
| `get_global_market_bias()` | Oracle + Santiment fusion | regime, size_mult, block_buys, sensor_policy |
| Cash floor / cash_policy | Risk / #90 | cash_adj: wenig spendable → **enger** Cap |
| `count_open_full_slots` / tails | positions | Ist-Zustand |
| Memory `soft_block` / size_bias | coin profiles | Portfolio „toxisch“ → enger; viele prefer → leicht weiter |
| Coin facts structure_risk / hard_neg | memory events | Entry-Qualität (schon Sensor); Capacity: Anteil toxischer Names |
| Venue quality | risk | pro Coin, nicht Cap — bleibt Entry-Gate |
| Macro calendar | get_risk_multipliers | block_new_entries → Cap irrelevant / floor |
| DCA harvest/deploy modes | cash_policy | HARVEST → enger; DEPLOY → weiter |
| Restart age | process start | Warm-up: temp niedriger Cap (market-context plan) |

**Nicht doppelt bestrafen:** Fusion size_mult *und* Cap-Crush *und* soft_block — **Caps + Audit-Log** der Faktoren.

---

## 6. Phases

### Phase 0 — Spec + Shadow Metrics (0.5–1 Tag)

- [ ] `CapacitySnapshot` dataclass: `{max_open_eff, full_slots, free_slots, factors{}, as_of}`  
- [ ] Pure `resolve_max_open_eff(inputs) -> CapacitySnapshot` + Unit Tests  
- [ ] Log pro Cycle / Health: `max_open_eff=20 (base24 regime-4 cash0 mem0)`  
- [ ] **Kein** Live-Gate noch  

### Phase 1 — Risk liest `max_open_eff` (1–2 Tage)

- [ ] RiskManager: `max_open_positions` → `resolve_max_open_eff()`  
- [ ] Reject message: `Max open positions reached (20/24 eff, regime=RISK_OFF)`  
- [ ] Config:

```json
"risk": {
  "position_capacity": {
    "enabled": true,
    "mode": "live",
    "base": 24,
    "min_floor": 12,
    "max_ceiling": 36,
    "regime_adj": {
      "RISK_ON": 6,
      "NEUTRAL": 0,
      "RISK_OFF": -6,
      "CRASH": -12,
      "WARMUP": -8
    },
    "link_fusion_size_mult": true,
    "cash_tight_threshold_usdt": 2000,
    "cash_tight_adj": -4,
    "memory_soft_block_per_5": -1,
    "restart_warmup_min": 15,
    "restart_warmup_adj": -6
  }
}
```

- [ ] Staging soak: Reject-Rate, Free-Slots-Histogramm, keine Ledger-Korruption  

### Phase 2 — Intelligent Slot Freeing (der Lorenzo-Hebel)

**Ausgelagert in eigenes Ticket + Plan:**

- Issue: [#111 Slot eviction for high-conviction entry](https://github.com/jholze/xagent-trading-bot/issues/111)  
- Plan: [`slot-eviction-for-entry.md`](slot-eviction-for-entry.md)

Capacity (#110) steuert **wie viele** Slots; #111 steuert **wen freimachen**, wenn trotzdem full + High-Conviction Entry wartet.

### Phase 3 — Priority Queue (optional)

- Sensor-Hits in Queue mit Score  
- Wenn Slot frei → höchster Score zuerst (statt „wer zuerst pollt“)  
- Verhindert, dass schwache Coins Lorenzo den letzten Slot wegschnappen  

### Phase 4 — Ops / Telegram

- `/capacity` oder Zeile in `/status`:  
  `Slots 22/28 eff (base24 +4 risk_on −0 cash) · free 6 · tails 7`  
- Reject-Explain: User sieht *warum* 24 vs 28  

---

## 7. Was wir **nicht** tun (Non-Goals v1)

| Non-Goal | Warum |
|----------|--------|
| Volle Kelly-Optimierung | Edge unkalibriert |
| Grok entscheidet Cap pro Cycle | Latenz, Nicht-Determinismus |
| Unlimited max_open | Cash/Risk |
| Eviction von Gewinnern mit Trail armed | widerspricht Trail-Policy |
| Memory blockt Sells | verboten |

---

## 8. Success Metrics

| Metrik | Heute (Lorenzo-Lektion) | Ziel |
|--------|-------------------------|------|
| Sensor-Ready + Slot-Full Rejects / Woche | hoch (BANK 5×) | ↓ oder Eviction greift |
| Captured setups mit spike≥5× und free cash | verpasst | ≥ Anteil in Shadow-Log |
| Open full slots in RISK_OFF | oft zu voll | Cap enger, weniger neue Bags |
| Median free slots RISK_ON | 0 | >0 öfter |
| False eviction (gute Coin raus, Entry flopt) | — | Shadow-Review, Cap |

---

## 9. Mapping zu bestehenden Tickets

| Ticket / Plan | Beziehung |
|---------------|-----------|
| #89 / adaptive-cash | Cash-Controller + Rotation — **Capacity ist Schwester-Modul** (Slots) |
| #92 Rotation tempo | Eviction-Kandidaten teilen |
| market-context-entry-throttle | Warm-up / burst → `restart_warmup_adj` |
| Sensor memory gates | pro Coin Qualität — Capacity nutzt **Portfolio-Aggregat** |
| Grid min-gain | unberührt (Exit-Qualität) |

**Empfehlung Ticket:** neues Issue  
`Intelligent position capacity (dynamic max_open_eff + optional slot eviction)`  
Label: `area:risk` · Child of #89 optional.

---

## 10. Implementierungs-Schnitt (Code-Skizze)

| Datei | Rolle |
|-------|--------|
| `risk/position_capacity.py` | pure `resolve_max_open_eff` |
| `risk/risk_manager.py` | use eff instead of raw max |
| `services/market_policy_fusion.py` | read-only consumer |
| `intelligence/memory/cache.py` | soft_block count helper |
| `notifications/.../mode_commands.py` | optional status line |
| `tests/unit/test_position_capacity.py` | regime/cash/memory table tests |

---

## 11. Empfohlene Reihenfolge (nach User-Priorität)

1. **Phase 0+1** — intelligentes Cap (sichtbar, sicher)  
2. **Phase 2 Shadow** — „hätte Lorenzo Slot freigemacht“  
3. **Phase 2 Live** — nur wenn Shadow ok  
4. **Phase 3** Queue — wenn Poll-Reihenfolge unfair wird  

Parallel: Deploy der schon gebauten Sensor-Memory-Gates + Grid min-gain, sonst messen wir alte Welt.

---

## 12. Entscheidungspunkte für dich

| # | Frage | Vorschlag |
|---|--------|-----------|
| D1 | Base weiter 24? | Ja, neutral center |
| D2 | Ceiling? | 32–36 (nicht zurück auf 40 ohne Cash) |
| D3 | Floor? | 12–16 in CRASH/RISK_OFF |
| D4 | Eviction für Sensor-Spikes? | Shadow zuerst, Score≥3 |
| D5 | An #89 hängen? | Ja, als Capacity-Child |

---

## 13. Quellen (Research)

- Freqtrade Configuration: `max_open_trades`, unlimited stake splits balance by open trades  
- Kelly criterion (Wikipedia + crypto risk blogs): fractional Kelly, portfolio risk budget  
- Regime-adaptive trading (QuantInsti etc.): scale activity/sizing with regime  
- Bestehende Repo-Pläne: adaptive-cash-rotation, market-context-entry-throttle, DCA policy fusion  

---

## 14. Nächster konkreter Schritt

Nach Freigabe der D1–D5: **Issue anlegen + Phase 0 pure function + Shadow-Log in Risk** (1 PR, kein Eviction noch).  
Dann Staging: bei nächstem BANK-ähnlichen Spike im Log sehen: `eff=28 free=0 would_evict=…`.
