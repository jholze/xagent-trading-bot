# Next wave: Adaptive Cash + Intelligent DCA + DecisionPacket

> **Status:** Plan (updated 2026-07-19) — **ticket-aligned**  
> **Nach:** Epic [#72](https://github.com/jholze/xagent-trading-bot/issues/72) RAG/Bus **CLOSED**  
> **Vor:** Gate-News (#86/#87), Memory-UI (#88)  
> **Related docs:** [`epic-dca-agent.md`](epic-dca-agent.md) · [`adaptive-cash-rotation-master.md`](adaptive-cash-rotation-master.md) · [`epic-agent-bus-rag.md`](epic-agent-bus-rag.md)

### Work from tickets (source of truth)

| Track | Epic | Next open tickets | Done |
|-------|------|-------------------|------|
| Adaptive Cash | [#89](https://github.com/jholze/xagent-trading-bot/issues/89) | [#91](https://github.com/jholze/xagent-trading-bot/issues/91) soak → [#92](https://github.com/jholze/xagent-trading-bot/issues/92)–[#94](https://github.com/jholze/xagent-trading-bot/issues/94) | [#90](https://github.com/jholze/xagent-trading-bot/issues/90) P0–P1 |
| DCA Agent | [#79](https://github.com/jholze/xagent-trading-bot/issues/79) | [#95](https://github.com/jholze/xagent-trading-bot/issues/95) D1 → [#96](https://github.com/jholze/xagent-trading-bot/issues/96)–[#97](https://github.com/jholze/xagent-trading-bot/issues/97) | — |
| Decision Agents | [#65](https://github.com/jholze/xagent-trading-bot/issues/65) | [#82](https://github.com/jholze/xagent-trading-bot/issues/82) A0 (parallel, thin) | — |
| Ops floor $0 | [#67](https://github.com/jholze/xagent-trading-bot/issues/67) | close via [#91](https://github.com/jholze/xagent-trading-bot/issues/91) | code path #90 |

---

## 0. Product north star (User)

| Priorität | Ziel |
|-----------|------|
| **1** | **Intelligentes DCA** — nicht blind scorend; Regime + Memory + Tech → `size_mult` / `skip` / `reason_codes` |
| **2** | **Adaptive Cash** — starker Markt: Cash **freigeben** (Floor↓, spendable↑); schwacher Markt: Cash **auffüllen** (Floor↑, Rotation ernten) |
| **3** | **Upside mitnehmen, Risiko drosseln** — DEPLOY wenn Daten RISK_ON; HARVEST wenn RISK_OFF / DD |
| **4** | **Grok nur advisory** — `/ask` + Hermes Reflect; **nie** Live-DCA-Entscheider |

```text
Starker Markt  →  DEPLOY: Floor niedrig, Entries+DCA freier, Rotation nur Zombies
Neutral        →  STEADY: mittlere Reserve, DCA dosiert
Schwacher Markt →  HARVEST: Floor hoch, Entries klein/block, DCA strenger, Gewinne realisieren
```

---

## 1. #72 Integration (Audit 2026-07-18)

### 1.1 Was #72 liefert vs. DCA-Live

| #72 Child | Deliverable | Live-DCA (`strategies/dca.py`) | Advisory (`/ask`) |
|-----------|-------------|-------------------------------|-------------------|
| C1 #73 | RagRetriever + `memory_rag_chunks` | **nicht verdrahtet** | ja |
| C2 #74 | Index cycle + Hermes RAG | kein DCA-Event-Index | indirekt (Memory wächst) |
| C3 #75 | Bus RagQuery/Result | **off** (`use_bus: false`) | nein |
| C4 #76 | `/ask` + Discovery | **kein** Auto-Order | **ja** (`dca_advice_rag`) |
| C5 #77 | Weaviate MemoryRagChunk 384d | nicht in evaluate | ja (Bot `WEAVIATE_URL`) |
| C6 #78 | Docs / kill-switches | n/a | n/a |
| C7 #80 | pluggable LLM | nicht im DCA-Cycle | Grok-Fallback |
| C8 #81 | Fusion→RAG | Flag **off**; Fusion **nicht** in dca.py | Lessons (z.B. RISK_OFF) |

**Fazit:** #72 North-star (natürliche DCA-**Fragen**) ist erfüllt. Live-DCA + Cash sind **#79 + Cash Policy** — sie **konsumieren** #72 (read-only Context/Index), öffnen #72 nicht neu.

### 1.2 Ist-Pfad heute

```text
DecisionEngine → evaluate_dca_addon (hard gates + optional score)
              → Risk (starrer cash_floor, auch für DCA)
              → Exec

/ask → MemoryStore + RAG → Grok (advisory only)
```

| Quelle | In Live-DCA? |
|--------|--------------|
| Loss-Band, Interval, Rounds, Score (RSI/ATR/…) | ✅ |
| Fusion size_mult / Regime | ❌ |
| CoinProfile / RAG | ❌ (soft_block nur New Entry) |
| spendable_new ≠ spendable_dca | ❌ |
| DcaDecisionEvent → Memory | ❌ |

### 1.3 Wie #72 in der Welle genutzt wird

| Baustein | Nutzung in dieser Welle |
|----------|-------------------------|
| C1 Retriever | D2: optional top-k im `DcaContext` (fail-open); D4: index |
| C2 Index | D4: Decision/Outcome chunks |
| C3 Bus | **nicht** Blocker; optional später publish PolicyResult |
| C4 `/ask` | Polish: Policy-Snapshot im Prompt (Alignment Advice ↔ Live) |
| C5 Weaviate | gleiche Retrieve-API |
| C7 LLM | nur Reflect / `/ask` |
| C8 | Hot-Path liest **Fusion live**; Index optional später |

---

## 2. Zielarchitektur (Cash + DCA + #72)

```text
  Fusion / Macro / Memory / optional RAG (#72 C1)
                    │
                    ▼
  ┌─────────────────────────────────────┐
  │  Cash Policy                        │  Mode: DEPLOY | STEADY | HARVEST
  │  → floor_pct_eff                    │
  │  → spendable_new                    │  frische Entries
  │  → spendable_dca                    │  Nachkäufe (eigener Puffer)
  │  → urgency: free | hold | harvest   │
  └──────────────────┬──────────────────┘
                     │
         ┌───────────┴───────────┐
         ▼                       ▼
  New Entry Risk           Hard gates + Score (bestehend)
         │                       │
         │                       ▼
         │                 DcaPolicy (mult/skip/reason_codes)
         │                       │
         └───────────┬───────────┘
                     ▼
              Risk + Exec (single-writer, kein Grok)
                     │
                     ▼
           DcaDecisionEvent → Memory/RAG index (#72 C1–C2)
                     │
           /ask + Hermes Reflect (Grok advisory)
```

**Cash freigeben** = Floor senken **und** bei Bedarf Rotation/Funding-Sells (USDT realisieren).  
**Auffüllen** = Floor hoch + Entries drosseln + DCA skip/mult↓ + Gewinne ernten.

---

## 3. Strang A — Adaptive Cash (+ #67)

Details: [`adaptive-cash-rotation-master.md`](adaptive-cash-rotation-master.md).

### 3.1 Modes (v1)

| Mode | Wann (Fusion) | Floor | Spendable |
|------|---------------|-------|-----------|
| **DEPLOY** | size_mult ≥ 1.0 / RISK_ON | niedrig (min-Cap) | new + dca frei |
| **STEADY** | NEUTRAL / mid mult | mittel | normal |
| **HARVEST** | size_mult &lt; 0.7 / RISK_OFF / block_buys / DD | hoch | new klein/0; **dca_buffer** bleibt |

### 3.2 Dual spendable

```text
spendable_new  = cash - floor_abs - dca_reserved
spendable_dca  = dca_buffer  (Floor frisst DCA nicht komplett)
```

### 3.3 Phase 0 — #67 Ops (sofort)

| Schritt | Done when |
|---------|-----------|
| Staging: free &gt; min_trade **oder** DCA nicht permanent floor-blocked | 1 observed BUY/DCA |
| Prod-Floor-Defaults nicht verwässern | Env/Config-Trennung |

### 3.4 Phase 1 — Cash Controller v1 (Haupt-Hebel für User-Vision)

- `floor_pct_eff` aus Fusion + DD  
- `spendable_new` / `spendable_dca`  
- Shadow-Log 24–48h, dann enable flag  
- **Acceptance:** DEPLOY senkt Floor; HARVEST erhöht free cash über Zeit; DCA hat Budget wenn New=0  

---

## 4. Strang B — Intelligent DCA (#79)

Details: [`epic-dca-agent.md`](epic-dca-agent.md).

### 4.1 Pipeline (nach Hard-Gates)

```text
should_dca / scoring (behalten)
  → DcaPolicy(context) → size_mult | skip | reason_codes
  → usdt = base * mult  capped by spendable_dca
  → Risk → Exec
```

### 4.2 Context (D2) — #72-konsumierend

| Input | Pflicht v1 | Quelle |
|-------|------------|--------|
| fusion regime + size_mult | ja | live Fusion (nicht nur RAG) |
| cash_mode / spendable_dca | ja | Cash Policy |
| coin profile (size_bias, win_rate) | ja | MemoryStore |
| calendar / session / PM | ja | Macro Memory |
| tech score breakdown | ja | bestehendes Scoring |
| RAG hits | optional | RagRetriever fail-open |
| equity DD | ja | Risk/equity |

### 4.3 Policy defaults (skizze — freeze in D1)

| Signal | Effect |
|--------|--------|
| Cash DEPLOY / size_mult ≥ 1.0 | mult 1.2–1.5 |
| STEADY | mult 1.0 |
| HARVEST / size_mult &lt; 0.7 | mult 0.3–0.5 **or skip** |
| High-impact calendar | mult ≤ 0.5 or skip |
| Profile schwach / viele Loss-Lessons | mult↓ or skip |
| RSI oversold + score high (in DEPLOY/STEADY) | mult 1.3–1.8 (cap) |
| Missing fusion | mult 1.0 fail-open |
| **Skip beats size** | immer |

Caps: `policy_mult ∈ [0, max_policy_mult]` (default max 2.0).  
**Kein** soft_block hard-kill auf DCA v1 (Recovery); höchstens mult↓.

### 4.4 Children

| ID | Title | Depends |
|----|--------|---------|
| **D1** | Spec Context + factor table + reason_codes | — |
| **D2** | DcaContextBuilder (read-only, fail-open) | D1, #72 done |
| **D3** | Wire mult/skip + tests + optional shadow | D2, Cash Phase0/1 empfohlen |
| **D4** | DcaDecisionEvent → Memory/RAG | D3 |
| **D5** | Reflect (rules ± Hermes Grok) | D4 |
| **D6** | Telegram/audit reasons | D3 |
| **D4b** | `/ask` zeigt Live-Policy-Snapshot | D3, C4 |
| **D7** | scheduled DCA (optional later) | D3 stable |

### 4.5 Verbesserungen vs. reines Score-DCA (Checkliste)

- [ ] Regime steuert Size/Skip (Alignment mit `/ask` RISK_OFF-Story)
- [ ] Cash-Mode steuert Budget, nicht nur Tech-Score
- [ ] reason_codes in Log/Memory (lernbar für #72 Index)
- [ ] Advice (`/ask`) und Live-Policy können denselben Context lesen
- [ ] Unit tests: RISK_OFF → skip/mult↓; missing fusion → fail-open

---

## 5. Strang C — DecisionPacket A0 (#82)

Parallel, **nur Vertrag** — siehe Epic #65.

- `AgentStance` + `DecisionPacket` v1 freeze  
- Merge: any_veto blocks, size_mode min  
- LLM hot-path **off**  
- Bridge: `DcaPolicyResult` → `AgentStance(agent="dca")` trivial mappbar  
- Deliverable: `plans/decision-packet-v1.md`  
- **Kein** Runtime in dieser Welle  

---

## 6. Reihenfolge

```text
Tag 0–1   #67 Phase 0 + D1 Spec + A0 Spec (parallel)
Tag 1–4   Cash Policy Phase 1 (shadow → enable)   ← User-Vision Cash
Tag 2–5   D2 ContextBuilder
Tag 4–8   D3 Wire Policy + tests + shadow
Tag 8+    D4 Events/Index · D4b /ask · D5/D6 · optional A1
```

| Work | Blocked by | Blocks |
|------|------------|--------|
| #67 P0 | — | realistische Staging-Tests |
| Cash P1 | #67 empfohlen | glaubwürdige DEPLOY/HARVEST |
| #79 D1 | — | D2/D3 |
| #79 D2 | D1, #72 ✅ | D3 |
| #79 D3 | D2, Cash P0/P1 | D4+ |
| #82 A0 | — | #65 A1+ |
| #86/#87/#88 | — | nach D3 |

---

## 7. Acceptance (Welle done)

- [ ] Staging: nicht permanent `free $0` (#67)
- [ ] Cash: `floor_pct_eff` / Mode im Log; `spendable_dca` getrennt nutzbar
- [ ] D1 Spec reviewbar
- [ ] D3: Unit tests beweisen skip/mult; optional Staging-Log `reason_codes`
- [ ] Kein Grok in `evaluate_dca` / Policy-Modul
- [ ] Policy-Module importieren keine Order-Writer
- [ ] A0 Packet freeze (doc only)
- [ ] Dokumentiert: was #72 liefert vs. was Live-DCA konsumiert

---

## 8. Explizit später

- #65 A1–A3 Orchestrator/Critic/Wire  
- DCA D7 scheduled  
- Gate watchlist/news Agents  
- Memory visualization  
- Loss-Evict (bleibt verboten)  
- Bus-Pflicht (C3) vor Policy  

---

## 9. Erste Todos

1. #67 Staging cash/floor  
2. Cash Policy v1 Spec-Freeze (Modes + Formeln) → implement Phase 1  
3. #79 D1 Policy-Tabelle final  
4. #82 `decision-packet-v1.md`  
5. D2/D3 PRs (flag shadow first)
