# Arena Decision: Gate Top Coins + WS Signals (2026-08-05)

**Teilnehmer (parallel):** Architecture · Strategy/EV · Adversarial Grill · Implementation/Reuse  
**Operator-Ziel:** maximale Profite, immer die Top-Coins auf Gate.io identifizieren; Signale nicht nur Exits über WS.

---

## 1. Konsens der Runde

| These | Architektur | Strategy | Grill | Implementierung |
|-------|-------------|----------|-------|-----------------|
| „Immer alle Top-Coins traden“ = freier Edge | nein | **nein (EV oft ≤0)** | nein | — |
| REST-only 60s zu langsam für Sniper-Branding | **ja** | ja | ja | ja (später WS) |
| Inject ≠ Buy — dediziertes Signal nötig | **ja** | **ja** | **ja** | **ja** |
| RiskManager nie umgehen | **ja** | ja | **ja** | **ja** |
| WS für Exits behalten / nicht kaputt machen | ja | ja | ja | **hart: Hub nicht anfassen in MVP** |
| Wenige Sniper-Slots (3), nicht max_open 36 | ja | **ja** | **ja** | Phase nach Shadow |
| Shadow / Demo vor Live-Sniper | ja | **ja** | **non-negotiable** | **ja** |

**Gemeinsames Zielbild (reformuliert, ehrlich):**

> Nicht „maximale Capture von gestern’s #1“, sondern **positive Expectancy unter harten Ruin-Bounds**: liquide Gate-USDT-Tops **früh genug** erkennen, **eng** handeln, **WS-Trail** raus — und alles messbar killbar.

---

## 2. Konflikt, den die Runde aufgelöst hat

| Pol | Position |
|-----|----------|
| **Architektur** | Ein shared GateTickerHub aus `exit_realtime` erweitern (OPEN ∪ watch) — schnellster TTV, eine Connection |
| **Implementierung** | `exit_realtime/hub.py` **nicht** anfassen im MVP — Live-Exits sind production; Dual-Use = Exit-Lag / false fires |

**Beschluss (beste Synthese):**

```
ENDSTATE (Ziel-Architektur)
  Shared GateTickerHub: ein WSS, Listener Exit + Board/Signal

PATH (sichere Reihenfolge)
  Phase S: pure sticky/score/T1/T2 + shadow would-buy auf REST board
           → null Exit-Risiko, beweist Policy-Logik
  Phase W: Board-WS (watch set) als Feature-Flag
           → entweder (i) vorsichtige Hub-Erweiterung hinter Flag
              oder (ii) dünner zweiter Client nur Board, dann Merge
  Phase B: gainer_sniper BUY → RiskManager (3 slots) Demo
  Phase L: Live nur nach Kill-Kriterien grün
```

User-Wunsch „Signale über WS“ bleibt **Endziel**.  
Erste Wertschöpfung darf **REST-Shadow** sein, sonst riskieren wir die **einzige** funktionierende WS-Schicht (Exits).

---

## 3. Gewählter Weg (ONE path)

### 3.1 Produkt-Policy (Strategy + Grill)

| Knob | Wert |
|------|------|
| Universe | Spot USDT, no 3L/5L, vol ≥ 500k–1M |
| Primary | Sticky Top-**5** (≥2–3 samples) |
| Early edge | Accel Rank ≤**15**, Band **12–40%** 24h |
| Ceiling | Block new entry **>45–55%** |
| BTC-RS | optional on: ≥ +5 pp vs BTC |
| Slots | **3** sniper (hard), risk **0.5–1%** |
| Daily | ≤6 sniper buys; −2.5% sniper DD → pause |
| Exit | WS TTP/trail (sniper overlay enger); rank-decay + 12–24h time stop später |
| Chase | prev-day continuation nur mit chase_guard |

### 3.2 Architektur-Endbild

```
REST seed (2–5 min, später; MVP 60s)
  filter + global rank authority
        │
        ▼
Watch set (OPEN ∪ top liquid/hot) ── cap ~60–80 pairs
        │
        ▼
ONE Gate WSS spot.tickers  (GateTickerHub)
        ├─► ExitListener (trail/TTP) → TradingService SELL
        └─► BoardListener → sticky/score/T1/T2
                └─► signal queue (no orders in WS thread)
                        └─► RiskManager → BUY (sniper only if candidate live)
```

### 3.3 Signal → Order (non-negotiable)

1. Trigger pure (testbar)  
2. Event/log only in shadow  
3. Later: `TradeOrder(source=gainer_sniper|…)`  
4. **Nur** `RiskManager` → `TradingService`  
5. Meta: rank, score, trigger, entry_pct  

### 3.4 Was wir **nicht** bauen

- Parallel-Bot / zweites Ledger  
- Full-market permanent WS  
- Order aus WS-Callback  
- Sniper der nur inject erweitert ohne eigenes Signal  
- „Max profit“ Trail lockern vor n≥30 tagged fills  
- Exit-Hub-Refactor und Live-Sniper im **selben** PR  

---

## 4. PR-Stack (Implementation-Lead, risk-ordered)

| PR | Inhalt | Exit-Risiko | Größe |
|----|--------|-------------|-------|
| **1** | Sticky + sniper_score + T1/T2 pure + unit tests | none | S |
| **2** | `sniper_shadow` → jsonl would-buy/reject; wire nach `run_scan`; default off | none | S |
| **3** | Staging soak 24–48h; metrics; go/no-go | none | S |
| **4** | Board WS (flag): watch OPEN∪top-N; board ticks; **noch kein buy** | low if isolated | M |
| **5** | `mode=sniper` / dedicated BUY + 3-slot RM + inject cap | low (no exit edit) | M |
| **6** | Optional: merge dual stream → shared hub; sniper trail overlay | medium — own PR | M |

MVP-Wert in 1–2 Tagen = **PR1–3**.  
User-WS-Forderung = **PR4** als nächstes hartes Deliverable, nicht optional ewig.

---

## 5. Kill / Go-Live Bar

**Shadow grün wenn:**

- ≥20 would-triggers, 0 leverage  
- Reject-Reasons = vol/ceiling/chase (keine Bugs)  
- Median would-entry rank ≤ 8, pct-Band sinnvoll  

**Demo/Live sniper grün wenn:**

- ≥15–30 round-trips, expectancy ≥0 after fees  
- ≥50% exits via WS trail  
- Sniper slots nie >3  
- 7d sniper expectancy ≤0 → auto `mode=off`  

**Ops halt:**

- WS/board stale >2 min → sniper pause  
- Daily sniper DD −2.5% → pause entries  

---

## 6. Antwort auf „maximale Profile + immer Top Coins“

| Wunsch | Realistische Umsetzung |
|--------|------------------------|
| Immer Top Coins **identifizieren** | Board (REST→WS) Top-N sticky + logs — **ja** |
| Immer Top Coins **kaufen** | **Nein** — nur quality-gated T1/T2; sonst negative EV |
| Max Profit | **Max expectancy unter Caps** — wenige fette Slots + schnelle Exits |
| WS | Exits **jetzt**; Signale **als nächstes** (PR4), nicht erst in 6 Monaten |

---

## 7. Operator-Entscheidung (eine Frage)

Freigabe für Ausführung in dieser Reihenfolge?

**A (empfohlen):** PR1–3 Shadow sofort → PR4 Board-WS → PR5 Demo-Buys  
**B:** PR4 Board-WS parallel/vor Shadow (höheres Risiko, schneller „WS-Feeling“)  
**C:** Nur Config enger (`trade_expand` drosseln) — kein neues Signal (billig, erfüllt Ziel **nicht**)

Arena-Empfehlung: **A**.

---

*End decision memo — multi-agent arena 2026-08-05.*
