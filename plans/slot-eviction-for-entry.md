# Plan: Slot Eviction for High-Conviction Entry

> **Status:** Design ready · Implementation open  
> **GitHub:** [#111](https://github.com/jholze/xagent-trading-bot/issues/111) · Parent epic [#89](https://github.com/jholze/xagent-trading-bot/issues/89) · Capacity sibling [#110](https://github.com/jholze/xagent-trading-bot/issues/110)  
> **Umgebung:** **Staging / demo ledger** ist das Labor — Live-Sells inkl. RAG-gesteuerter Eviction sind **gewollt und erlaubt** (kein Paper-only-Theater). Production-Mainnet separat härten.  
> **Anlass:** Lorenzo / `BANK` — Sensor ready, Cash ok, aber Full-Slots = `max_open_eff` → dead reject. Capacity (#110) macht die Tür **breiter/enger**; Eviction öffnet die Tür **gezielt**, wenn sie trotzdem zu ist.  
> **Nicht ersetzen:** `sell_policy.rotation` (D′), Trail-exclusive, DCA funding (#93), `rotation_urgency` (#94), Cash floor — **komponieren**  
> **Eine Engine:** kein zweites Sell-Gehirn. Eviction **scored** Kandidaten und **emittiert** normale Sells mit `exit_source=slot_evict_for_entry`.

---

## 0. One-liner

**Wenn kein Full-Slot frei ist und ein High-Conviction Entry wartet: Memory-Profile + RAG wählen den Victim und steuern auf Staging den echten Eviction-Sell — unter mehreren Coins im Plus den mit schlechterer Hold-Erwartung. Hard-Gates bleiben (Trail, Cash, block_buys). Fail-open auf Profile wenn RAG tot. Nie Memory hard-block auf Sells.**

---

## 1. Was Eviction ist — und was nicht

| Begriff | Bedeutung | Owner heute |
|---------|-----------|-------------|
| **Capacity** (`max_open_eff`) | *Wie viele* Full-Slots erlaubt sind | #110 · `risk/position_capacity.py` |
| **Rotation tempo** | Früher partial/tail bei **Plus** | #92 · `sell_rotation_policy` |
| **Funding sell** | Gewinner → Cash für DCA | #93 · `dca_portfolio` |
| **Urgency** | Memory/Fusion treibt Tempo bei Plus | #94 / #71 |
| **Slot eviction** (dieses Ticket) | **Platz schaffen für einen konkreten Entry**, wenn free full slots = 0 | **neu** |

```text
Capacity:     "Darf das Buch 28 statt 24 Bags haben?"
Rotation:     "Diese Coin ist im Plus — schneller ernten."
Funding:      "Diese Gewinner finanzieren DCA auf X."
Eviction:     "BANK will rein, 0 free slots — wen werfen wir raus / teilverkaufen?"
```

**Eviction ist kein Stop-Loss-Ersatz** und kein „alles under water killen“.  
**Eviction ist Portfolio-Hygiene unter Zeitdruck eines besseren Setups.**  
**Memory ist der Schiedsrichter unter grünen Bags:** nicht „wer hat am wenigsten Plus“, sondern „wer *würde* laut Book-History / Profile besser weiterlaufen“.

### User-Story (Memory first)

```text
Book full. BANK will rein (Sensor).
Im Plus: COIN_A (+4%), COIN_B (+3%), COIN_C (+6%, trail armed).

Memory:
  COIN_A  prefer · win_rate hoch · size_bias 1.1  → keep_score hoch
  COIN_B  neutral · win_rate schwach · size_bias 0.7 · facts soft  → keep_score niedrig
  COIN_C  trail armed → hard veto (nie victim)

Plan: free COIN_B (partial/full), keep COIN_A, leave COIN_C alone.
Rationale: "memory: B weaker hold than A; entry BANK demand ok"
```

### Staging-Lab (verbindlich)

| | Staging (`xagent_test` / demo) | Production live |
|--|-------------------------------|-----------------|
| Eviction sells | **live, RAG steuert Victim** | später, nach Staging-Evidence |
| `slot_eviction.mode` | **`live`** | start `shadow` oder eng limitiert |
| `rag.apply_to_plan` | **`true`** | erst nach Staging-Review |
| Rate limits | an (nicht unbegrenzt) | strenger |
| Kill-switch | jederzeit `mode=off` / `rag.mode=off` | Pflicht |

Wir **lernen im echten Demo-Ledger**, nicht nur in Logs. Fehler = Demo-PnL, nicht Blindflug auf Mainnet.

---

## 2. Problem-Statement (Lorenzo-Lektion)

1. Sensor 15m: Spike ≥3–5×, Venue ok, Memory nicht soft_block.  
2. Risk: Cash floor ok, Fusion nicht `block_buys`.  
3. `count_open_full_slots() >= max_open_eff` → `code=max_open_positions`.  
4. Im Book: flache Bags, Memory-weak Namen, lange Idle — **niemand wird aktiv freigemacht** nur für den Entry.  
5. Tails zählen oft **nicht** als Full-Slot → viele Bags, trotzdem blockt Full-Cap.

**Zielmetrik:** Anteil „Sensor-ready + free cash + free_slots=0“ Events, die innerhalb ≤1 Cycle entweder  
(a) Entry freigeben (evict succeeded), oder  
(b) sauber loggen *warum nicht* (hard veto / no candidate / shadow only).

---

## 3. Design-Prinzipien (hart)

| # | Prinzip | Konkret |
|---|---------|---------|
| P1 | **Demand-driven** | Eviction nur wenn Entry-Demand (Score ≥ Schwelle) **und** `free_full_slots == 0` |
| P2 | **Eine Sell-Pipeline** | Order = normaler SELL; `source` bleibt channel; `exit_source=slot_evict_for_entry` |
| P3 | **Trail-Gewinner heilig** | Trail armed / high peak-gain → **nie** Eviction-Kandidat |
| P4 | **Prefer free cash first** | Wenn `spendable_new` zu klein für Entry → Eviction allein hilft nicht (Cash-Pfad / #93) |
| P5 | **Prefer reduce-to-tail over full loss dump** | Underwater: zuerst Full-Slot → Tail (partial), nicht hard full liquidate |
| P6 | **No loss-full-evict by default** | Full close at loss nur opt-in + extreme toxic score + caps |
| P7 | **Fail-open soft on score errors** | RAG/LLM tot → Profile-Fallback; wenn auch das fehlt → kein Evict (Entry blocked) |
| P8 | **Staging live by default** | Auf Staging: `mode=live` + RAG steuert Sells. Shadow nur Debug/Kill-Compare. Production: separat härten |
| P9 | **Rate limits** | Max N evictions / hour, cooldown pro Symbol, max 1 concurrent pending (auch Staging) |
| P10 | **Observability first** | Jede Decision: demand, keep_profile/keep_rag, applied victim, exit_source, would_vs_did |
| P11 | **Memory ranks winners** | Unter Class-A: Victim = niedrigster `keep_final` (Profile+RAG), nicht blind kleinstes Plus % |
| P12 | **Entry muss Memory-Vergleich bestehen** | Evict winner only if `entry_keep_edge ≥ margin` vs victim |
| P13 | **RAG steuert Staging-Sells** | `apply_to_plan=true` auf Staging; A/B-Felder bleiben im Log (Profile vs RAG) |
| P14 | **Hard gates vor RAG** | Trail / min-hold / block_buys / cash — RAG darf **keine** Vetos aufweichen |

**Explizit verboten (v1):**

- RAG/LLM **ohne** Hard-Gates (Trail, same-symbol, cash, block_buys)  
- Memory **blockt** den Eviction-Sell (Memory/RAG score’t / rank’t, nie hard-block sell)  
- LLM-Output als einzige Wahrheit ohne Profile-Baseline im Log  
- Eviction bei Fusion `block_buys` / CRASH (kein neuer Risk-on Entry)  
- Eviction des **gleichen** Symbols wie der Entry  
- Eviction in Restart-Warmup (Process age < T, shared with capacity warmup)  
- Doppel-Engine: nicht `dca_portfolio` **und** Eviction denselben Winner im selben Cycle ohne Prioritätsregel

---

## 4. Architektur

```text
                    ┌──────────────────────────────┐
                    │  Entry Demand (sensor/etc.)  │
                    │  score_entry_demand()        │
                    └──────────────┬───────────────┘
                                   │ demand ≥ thr
                                   │ free_full_slots == 0
                                   ▼
                    ┌──────────────────────────────┐
                    │  Slot Eviction Planner       │
                    │  risk/slot_eviction.py       │
                    │  (pure plan + thin wire)     │
                    └──────────────┬───────────────┘
           inputs                  │
  ┌────────┴────────┬──────────────┼──────────────┬─────────────┐
  │ Capacity snap   │ Open full    │ Memory       │ Rotation    │
  │ max_open_eff    │ positions    │ keep_score() │ can_evict?  │
  │ free_slots      │ PnL, idle    │ entry vs bag │ trail arm   │
  │ fusion/cash     │ sold%        │ profiles*    │ urgency     │
  └─────────────────┴──────────────┼──────────────┴─────────────┘
                                   ▼
                    EvictionPlan { mode, victim, action,
                                   entry_symbol, keep_scores,
                                   memory_rationale, scores, vetos }

* Layer A: CoinProfile cache (keep_score).
  Layer B: RAG retrieve (+ optional LLM compare) — experiment stack, see §6.5.
                                   │
          ┌────────────────────────┼────────────────────────┐
          ▼                        ▼                        ▼
   shadow: log only         live: enqueue SELL         no plan:
   cycle digest             exit_source=slot_evict     entry stays
   + Telegram optional      then re-eval entry         max_open reject
```

\* `rotation_urgency` (#94) sobald vorhanden; bis dahin Heuristik aus Memory features + idle + PnL.

### 4.1 Dateien (vorschlag)

| Datei | Rolle |
|-------|--------|
| `risk/slot_eviction.py` | pure: demand score, victim rank, plan, rate-limit helpers |
| `risk/position_capacity.py` | read-only consumer (free slots / eff) |
| `strategies/sell_rotation_policy.py` | reuse `can_rotation_evict`, trail helpers, tail checks |
| `strategies/exit_attribution.py` | register `slot_evict_for_entry` |
| `strategies/sell_sources.py` | optional constant |
| Entry path (`entry_sensor_15m` / decision_engine / risk) | thin: call planner when reject would be max_open |
| `notifications/...` | optional `/capacity` / cycle line |
| `tests/unit/test_slot_eviction.py` | table tests |

### 4.2 Hot-path flow (Staging live + RAG)

```text
1. Sensor wants NEW entry on SYMBOL
2. If free_full_slots > 0 → normal buy path
3. If free_full_slots == 0:
   a. score EntryDemand (musts: venue, cash, not soft_block, not block_buys)
   b. if demand < min → max_open reject
   c. hard-filter book (trail, min_hold, same symbol, …)
   d. keep_profile for entry + Class-A candidates
   e. RAG enrich → keep_rag / keep_final  (apply_to_plan=true on staging)
   f. optional LLM compare if rag.mode=retrieve_llm
   g. pick victim = min keep_final among eligible; swap gate vs entry
   h. if mode=live (staging default):
        - SELL victim (partial/full)  exit_source=slot_evict_for_entry
        - pending_entry intent TTL
   i. if mode=shadow: log only, no sell
4. Next cycle (or same cycle if sync fill + config): entry if slot free
```

**Two-cycle default** (sicherer Ledger): Evict N, Entry N+1.  
Staging darf später `same_cycle_if_filled=true` testen — Config, nicht Pflicht.

---

## 5. Entry Demand Score (wann lohnt Eviction?)

Nur **neue** Entries (nicht DCA auf existing). Quellen priorisiert: Sensor 15m, optional später CMC confluence.

| Signal | Punkte (vorschlag) | Must / Soft |
|--------|--------------------|-------------|
| Source ∈ sensor family | +2 | soft |
| Spike multiple ≥ 3× | +1 | soft |
| Spike multiple ≥ 5× | +2 (statt +1) | soft |
| Venue quality pass | — | **must** |
| Not soft_block / not structure_risk hard | — | **must** |
| Fusion not `block_buys`, regime ≠ CRASH | — | **must** |
| Cash `spendable_new` ≥ planned entry size | — | **must** |
| Capacity enabled + free_slots == 0 | — | **must** (sonst unnötig) |
| LC/CMC bullish confluence | +1 | soft |
| Memory `prefer` / high keep_score on entry | +1…+2 | soft |
| Memory soft_block / toxic on entry | — | **must fail** |
| Restart warmup active | — | **veto** |
| Daily eviction budget exhausted | — | **veto** |

```text
min_score_live   = 4   # z.B. sensor(+2) + spike5×(+2)
min_score_shadow = 3   # mehr Events sehen in Shadow
```

**Must-fails → kein Plan** (kein „fast-evict“).

---

## 6. Victim Ranking (wen freimachen?)

Nur **Full-Slots** (Tails belegen Cap nicht — freimachen bringt nichts).

### 6.0 Zwei-Ebenen-Ranking (wichtig)

```text
Layer 1 — Policy gate:   veto? trail? min hold? rotation-eligible?
Layer 2 — Memory rank:   among survivors, who has lowest keep_score?
Layer 3 — Entry edge:    is BANK (entry) worth the swap vs that victim?
```

**PnL % ist Tie-Breaker, nicht Primärsignal** — wenn beide im Plus, gewinnt Memory.

### 6.1 Hard veto (nie Victim)

| Veto | Warum |
|------|--------|
| Symbol == entry symbol | trivial |
| Trail TP / trail stop **armed** | North Star: winners ride |
| Peak gain ≥ `protect_peak_gain_pct` (z.B. 12%) | fast-trail / winner protect |
| Position age < `min_hold_hours` (z.B. 2–4h) | thrash guard |
| Open order / cooldown sell | execution race |
| Memory `prefer` **and** keep_score ≥ `prefer_keep_floor` | Memory sagt „halten“ — nur überstimmen wenn Entry keep_edge riesig (optional, default: hard keep) |
| Notional > `max_evict_notional_usdt` | nicht den Riesen killen für Sensor-Lot |
| Already scheduled recovery-only path? | optional: recovery coins not full-killed |

### 6.2 Memory `keep_score` (0–1) — Hot-Path, deterministisch

Reine Funktion aus `CoinProfile` (+ optional position features). **Kein LLM.**  
Höher = „sollte im Buch bleiben / eher weiter performen“.

| Input | keep_score ↑ | keep_score ↓ |
|-------|--------------|--------------|
| `entry_bias == prefer` | stark | |
| `entry_bias == soft_block` | | stark |
| `size_bias` hoch (≥1.0) | ja | niedrig (≤0.75) |
| `win_rate` (bei n≥ min_samples) | hoch | niedrig |
| `total_pnl_usdt` / avg sell quality | positiv | negativ history |
| `risk_score` niedrig | ja | hoch |
| `features.structure_risk` / hard_neg | | ja |
| `features` venue thin / sensor-loss heavy | | ja |
| `rotation_urgency` (#94) hoch | | (eher raus bei Plus) |
| Missing profile | **neutral 0.5** (fail-open) | |

```text
keep_score(symbol) ∈ [0, 1]

# Among Class-A candidates (gain≥0, no hard veto):
victim_score = (1 - keep_score) * w_memory
          + idle_term * w_idle
          + flatness_term * w_pnl   # small weight
          + sold_pct_term * w_tail

# Pick max free_score, then require:
keep_score(entry) - keep_score(victim) >= min_entry_keep_edge
# OR if entry is brand-new (no profile): use demand_score proxy
#    entry_proxy_keep = f(demand, prefer, not soft_block)
```

**Interpretation deiner Story:**

| Coins im Plus | keep_score | Entscheidung |
|---------------|------------|--------------|
| A prefer, starke History | 0.82 | **behalten** |
| B schwach, size_bias 0.7 | 0.35 | **Victim** (frei machen) |
| C trail armed | veto | unantastbar |

→ Memory hat „gefragt“ (via Profile-Aggregate, die Reflect/Rebuild aus Trades bauen) — wer besser performen *würde* — und den Schwächeren freigemacht.

### 6.3 Comparative swap gate (Entry vs Victim)

Eviction eines **Gewinners** nur wenn der Tausch sinnvoll ist:

```text
if victim is Class A (green/flat):
    edge = keep_score(entry) - keep_score(victim)
    if edge < min_entry_keep_edge:   # default 0.10 … 0.15
        veto: "memory_swap_not_worth_it"
        # Beispiel: Entry neutral 0.5, Victim prefer 0.8 → kein Swap
```

Underwater Class B/C: Swap-Gate weicher (Slot-Hygiene > Memory-Vergleich), aber Memory toxic boostet Victim-Priorität.

### 6.4 Soft score (höher = eher raus) — nach Memory

| Faktor | Richtung | Gewicht v1 |
|--------|----------|------------|
| **Memory (1 − keep_score)** | primär unter Grünen | **hoch** |
| Idle hours | länger → höher | mittel |
| Unrealized PnL flatness | flach > deep green | **niedrig** (Tie-Break) |
| `sold_percent` high (noch full) | easy tail | mittel |
| No trail / far from arm | + | niedrig |
| DCA recovery heavy bag | **−** | mittel |
| Grid active tight | **−** | niedrig |

### 6.5 RAG-Integration (Staging: **steuert Live-Sells**)

Ziel: Auf Staging **wirklich** sehen, ob Profile+RAG bessere Eviction-Opfer wählt — inkl. ausgeführter Sells im Demo-Ledger.  
Stack: `hermes/memory/rag_retriever.py` · `memory.rag.*` · Mongo `memory_rag_chunks` · Weaviate fail-open · `scripts/probe_ask_rag.py`.

#### 6.5.1 Drei Stufen (Config)

| `rag.mode` | Was läuft | Latenz | Steuert Victim auf Staging? |
|------------|-----------|--------|------------------------------|
| `off` | nur Profile `keep_score` | ~0 | Profile only |
| `retrieve` | Top-K Chunks → **evidence boost** auf keep | niedrig–mittel | **ja** wenn `apply_to_plan=true` |
| `retrieve_llm` | retrieve + strukturierter LLM-Compare | höher | **ja** (LLM pick wenn valid), sonst retrieve/profile |

#### 6.5.1b Staging-Defaults (verbindlich für dieses Ticket)

```text
slot_eviction.mode          = live
slot_eviction.rag.mode      = retrieve          # später retrieve_llm optional
slot_eviction.rag.apply_to_plan = true          # RAG gewinnt Ranking
slot_eviction.rag.log_ab_always = true          # Profile-Victim trotzdem loggen
```

```text
Flow:
  keep_profile  ──┐
                  ├──► keep_final ──► victim ──► LIVE SELL (demo)
  keep_rag      ──┘       ▲
                          │ apply_to_plan=true
```

**A/B bleibt Observability:** jedes Event speichert `profile_victim` vs `applied_victim` (meist = rag).  
So siehst du Discord und Outcome, **ohne** Staging auf Log-only zu drosseln.

**Production (später):** Defaults strenger (`mode=shadow` oder `apply_to_plan=false` bis Review) — nicht Teil des Staging-Lab-Defaults.

#### 6.5.2 Retrieve-Query (kein LLM)

Pro Kandidat (und Entry) eine gezielte Query, filter symbol-aware:

```text
query_hold(symbol) =
  "{symbol}: hold quality trade outcomes wins losses soft_block lessons
   structure_risk sensor entry exit"

hits = RagRetriever.retrieve(query, top_k=rag.top_k, filters={symbol?})
```

**Evidence-Score** aus Hits (pure, testbar):

| Signal in chunk text/meta | keep_boost |
|---------------------------|------------|
| lesson / solid win_rate / prefer language | + |
| gross loss / stop blowup / soft_block / structure_risk | − |
| high retrieval score × recency | stärker gewichten |
| keine Hits | 0 (fail-open, Profile bleibt) |

```text
keep_rag(symbol) = clamp(
  keep_profile(symbol) + evidence_delta(hits) * rag.evidence_weight,
  0, 1
)
```

Ranking/Swap nutzen:

```text
if rag.mode == off OR rag failed:
    keep_final = keep_profile
elif apply_to_plan:
    keep_final = keep_rag          # Staging default
else:
    keep_final = keep_profile      # log keep_rag only
```

#### 6.5.3 LLM-Compare (optional, `retrieve_llm`) — auch live-fähig auf Staging

Nur wenn ≥2 Class-A Kandidaten nach Hard-Veto übrig **und** retrieve gelaufen ist:

```text
SYSTEM: Portfolio slot eviction advisor. Output JSON only. Never invent fills.
USER:
  Entry demand: BANK score=5 spike=5.2x venue=ok
  Candidates (green, eviction-eligible):
    AAA keep_profile=0.82 keep_rag=0.79 pnl=+5% idle=20h hits=[...]
    BBB keep_profile=0.40 keep_rag=0.28 pnl=+4% idle=48h hits=[...]
  Question: which ONE symbol should we free (partial sell) so entry can take the slot?
  Prefer weaker expected continuation. Never pick trail-armed (already filtered).
  Reply JSON:
  {
    "free_symbol": "BBB",
    "keep_symbol": "AAA",
    "confidence": 0.0-1.0,
    "rationale": "…",
    "evidence_chunk_ids": ["…"]
  }
```

**Merge-Regeln (Staging live-safe):**

| Regel | Detail |
|-------|--------|
| Timeout | z.B. 2.5s → drop LLM, use retrieve/profile, **Sell trotzdem möglich** |
| Low confidence | `confidence < min_llm_confidence` → ignore LLM pick, keep retrieve |
| Invalid symbol | not in candidate set → ignore |
| Prefer hard-keep | LLM darf `prefer` Victim nur wenn `allow_llm_override_prefer` |
| Disagreement log | always: `profile_pick` vs `rag_pick` vs `llm_pick` vs `applied` |
| apply_to_plan | Staging **true**: applied = llm if valid else rag else profile |

**Wo der Call läuft (Latenz) — Staging:**

| Option | Wann |
|--------|------|
| **B (Staging default)** | **Sync** im Planner mit Timeout: retrieve (+ optional LLM) vor Sell — du willst den Sell **jetzt** testen |
| **A** | Async enrich + cached compare next cycle — wenn Latency weh tut |
| **C** | Hermes Replay offline — Outcomes / Regret |

Staging v1: **B + C**. Bei p95-Latency-Problemen → A oder `retrieve` without LLM.

#### 6.5.4 Messung trotz Live-Sells (das „bringt das was?“)

Jedes Eviction-Event (auch ausgeführte Sells) loggt:

```json
{
  "entry": "BANK/USDT",
  "profile_victim": "BBB/USDT",
  "rag_victim": "AAA/USDT",
  "llm_victim": null,
  "applied_victim": "AAA/USDT",
  "sell_executed": true,
  "rag_mode": "retrieve",
  "apply_to_plan": true,
  "keep_profile": {"AAA": 0.55, "BBB": 0.50, "BANK": 0.61},
  "keep_rag": {"AAA": 0.30, "BBB": 0.62, "BANK": 0.58},
  "agreement": {"profile_vs_rag": false},
  "hit_counts": {"AAA": 5, "BBB": 2, "BANK": 2},
  "latency_ms": {"retrieve": 45, "llm": 0}
}
```

**Outcome-Labels (Hermes, +24h / +7d) auf Demo-Trades:**

| Label | Definition |
|-------|------------|
| `victim_regret` | Victim stieg nach Evict stark (schlecht freigemacht) |
| `entry_capture` | Entry genommen und ok PnL |
| `entry_flop` | Entry nach Evict stark negativ |
| `counterfactual_profile` | Hätte Profile-Victim weniger Regret gehabt? |

Nach 1–2 Wochen Staging-Live:

```text
Discord-Rate? Bei Discord: RAG-Regret vs counterfactual Profile-Regret?
→ RAG schlechter: apply_to_plan=false oder mode=off
→ RAG besser / gleich: so lassen, Production-Plan ableiten
```

#### 6.5.5 Fail-open & Safety

| Event | Verhalten |
|-------|-----------|
| `memory.rag.enabled=false` | treat as `rag.mode=off` |
| Weaviate down | Mongo retrieve / empty hits |
| Retrieve exception | keep_profile only, log `rag_error` |
| LLM timeout/parse error | ignore LLM |
| No chunks for symbol | evidence_delta=0 |
| RAG says free trail-armed | impossible (pre-filtered); if somehow → reject |

**Nie:** RAG schreibt Ledger, ändert Positions, oder setzt soft_block.

#### 6.5.6 Code-Schnitt RAG

| Baustein | Ort | Rolle |
|----------|-----|--------|
| `RagRetriever.retrieve` | `hermes/memory/rag_retriever.py` | reuse |
| `evidence_delta(hits) -> float` | `risk/slot_eviction_rag.py` (neu) | pure |
| `enrich_keep_with_rag(symbols) -> dict` | same | batch retrieve |
| `llm_compare_hold(entry, cands, hits)` | same / thin llm_client wrapper | optional |
| `compare_picks_ab(...)` | same | profile vs rag vs llm |
| Cache | in-process TTL 60–120s per symbol | avoid N×retrieve spam |
| Probe | `scripts/probe_slot_eviction_rag.py` | offline + staging SSH |
| Replay | `scripts/analyze_slot_eviction_rag.py` | historical would_evict + outcomes |

#### 6.5.7 Config (Staging: RAG steuert Sells)

```json
"risk": {
  "slot_eviction": {
    "mode": "live",
    "rag": {
      "mode": "retrieve",
      "apply_to_plan": true,
      "top_k": 5,
      "evidence_weight": 0.25,
      "max_candidates_for_rag": 5,
      "retrieve_timeout_ms": 800,
      "llm_timeout_ms": 2500,
      "min_llm_confidence": 0.55,
      "allow_llm_override_prefer": false,
      "cache_ttl_sec": 90,
      "async_enrich": false,
      "log_ab_always": true
    }
  }
}
```

Kill-switches (sofort):

| Switch | Effekt |
|--------|--------|
| `slot_eviction.mode=off` | keine Eviction |
| `slot_eviction.mode=shadow` | log only, keine Sells |
| `rag.mode=off` | Profile-only live sells |
| `rag.apply_to_plan=false` | live sells, aber Victim = Profile; RAG nur Log |
| `memory.rag.enabled=false` / `HERMES_RAG=0` | retrieve aus → Profile |

### 6.6 Action selection (was verkaufen?)

| Victim class | Action | Slot-Effekt |
|--------------|--------|-------------|
| **A. Eligible winner / flat** + lowest keep_score + swap edge ok | `SELL_PARTIAL` 30–50% **or** full if small | Full→tail oder free |
| **B. Underwater, not toxic** | **Reduce-to-tail only** | Full-slot free, bag remains |
| **C. Toxic underwater** (low keep + structure_risk + idle) | Partial aggressive; full only if `allow_loss_full_evict` | free |
| **D. No candidate** (all high keep / trail / swap fail) | No plan | entry stays blocked |

**Default live v1:** A (memory-ranked) + B.  
**C full-loss:** `allow_loss_full_evict: false`.

### 6.7 Relation zu `can_rotation_evict` + #94

- Class A: **muss** `can_rotation_evict` / `evict_min_gain_pct` respektieren.  
- `rotation_urgency` fließt in **keep_score↓** (gleiche Quelle wie #94, eine Funktion).  
- Class B: Slot-Demotion, kein full loss dump.  
- Class C: Ausnahme, eng, geloggt.

### 6.8 Code-Schnitt Memory + RAG

| Funktion | Ort | Rolle |
|----------|-----|--------|
| `memory_keep_score(profile, fusion?) -> float` | `risk/slot_eviction.py` oder `intelligence/memory/keep_score.py` | pure Profile baseline |
| `get_coin_profile` | `intelligence/memory/cache.py` | hot cache |
| Entry profile | same | soft_block must fail demand |
| `enrich_keep_with_rag` / `evidence_delta` | `risk/slot_eviction_rag.py` | retrieve + boost |
| `llm_compare_hold` | same | optional structured compare |
| `RagRetriever` | `hermes/memory/rag_retriever.py` | existing stack |

---

## 7. Config-Skizze

```json
"risk": {
  "slot_eviction": {
    "enabled": true,
    "mode": "live",
    "min_entry_score": 4,
    "min_entry_score_shadow": 3,
    "min_victim_score": 2.0,
    "min_hold_hours": 3,
    "protect_peak_gain_pct": 12,
    "max_evict_notional_usdt": 8000,
    "max_evictions_per_hour": 2,
    "max_evictions_per_day": 8,
    "symbol_cooldown_hours": 24,
    "pending_entry_ttl_min": 30,
    "same_cycle_if_filled": false,
    "prefer_reduce_to_tail": true,
    "tail_target_sold_pct": 0.55,
    "tail_target_max_notional_usdt": 800,
    "allow_loss_full_evict": false,
    "loss_full_min_toxic_score": 0.85,
    "memory": {
      "enabled": true,
      "weight": 0.55,
      "min_entry_keep_edge": 0.12,
      "prefer_keep_floor": 0.7,
      "prefer_is_hard_keep": true,
      "missing_profile_keep": 0.5,
      "min_samples_for_win_rate": 3
    },
    "rag": {
      "mode": "retrieve",
      "apply_to_plan": true,
      "top_k": 5,
      "evidence_weight": 0.25,
      "max_candidates_for_rag": 5,
      "retrieve_timeout_ms": 800,
      "llm_timeout_ms": 2500,
      "min_llm_confidence": 0.55,
      "allow_llm_override_prefer": false,
      "cache_ttl_sec": 90,
      "async_enrich": false,
      "log_ab_always": true
    },
    "weights": {
      "memory": 0.55,
      "idle": 0.2,
      "pnl_flat": 0.1,
      "tail_ready": 0.15
    },
    "sources": ["entry_sensor_15m", "vol_spike_15m"],
    "require_capacity_full": true,
    "require_spendable_for_entry": true,
    "skip_if_warmup": true,
    "skip_if_block_buys": true,
    "skip_if_crash": true
  }
}
```

Staging: `mode=live` + `rag.apply_to_plan=true`.  
Kill: `mode=off|shadow`, `rag.mode=off`, `apply_to_plan=false`, `memory.rag.enabled=false`.

---

## 8. Observability

### 8.1 Structured log / cycle digest

```text
slot_eviction demand=BANK score=5 free=0
  keep_profile: BANK=0.61 A=0.82 B=0.35 C=veto(trail)
  keep_rag:     BANK=0.58 A=0.79 B=0.28  (hits A=3 B=7)
  ab: profile_victim=B rag_victim=B llm_victim=- agree=true
  plan: EVICT B SELL_PARTIAL_40 exit=slot_evict_for_entry for BANK
        applied=rag (profile would have picked B too / or Discord)
  mode=live sell_executed=true
```

### 8.2 Ledger / order fields

| Field | Value |
|-------|--------|
| `source` | channel (auto / entry path) |
| `exit_source` | `slot_evict_for_entry` |
| `exit_rationale` | short: `for=BANK keep_v=0.28 keep_e=0.58 edge=0.30 rag=retrieve action=partial_40` |

### 8.3 Reject message upgrade (wenn kein Plan)

```text
Max open positions (28/28 eff) · eviction: no_candidate
  (best victim score 1.1 < 2.0; trail-protected=6)
```

### 8.4 Telegram (optional)

- Cycle line or `/capacity`: `evict shadow would XYZ→BANK`  
- Alert on **live** eviction only (rate-limited)

### 8.5 Metrics (Staging)

| Metric | Ziel |
|--------|------|
| `sensor_ready_slot_full` / week | ↓ |
| `would_evict` events | visible in shadow |
| `live_evict` count | small, controlled |
| `evict_then_entry_fill` rate | track |
| `evict_then_entry_flop` (entry −X% in 24h) | review threshold |
| False victim (trail missed winner) | **0** by design |
| **`profile_vs_rag_agree` rate** | baseline; Abweichungen tracken |
| **`rag_regret` vs `profile_regret`** | RAG gewinnt nur wenn weniger Regret bei Discord |
| retrieve latency p95 | under timeout; fail-open rate low |

---

## 9. Phases (implementierbar, aber ein Ticket)

| Phase | Deliverable | Risk | Exit criterion |
|-------|-------------|------|----------------|
| **E0** | Plan + Issue | none | this doc |
| **E1** | Pure profile keep/rank/plan + unit tests | none | tests green |
| **E1b** | RAG retrieve + evidence + A/B log fields + probe | low | probe hits on staging |
| **E2** | **Wire live on staging:** max_open → plan → **RAG victim → real SELL** + rate limits + `exit_source` | med (demo) | first real evict+entry stories |
| **E2b** | Optional `retrieve_llm` live-gated (timeout, conf) | med | latency + quality ok |
| **E3** | Outcome metrics / Hermes regret + Discord review | low | 1–2 weeks data |
| **E4** | Optional same-cycle fill | med | optional |
| **E5** | Urgency #94 + prod hardening defaults | low | when promoting off staging |

**Kein** separates Epic — Child of #89, sibling of #110.  
**Staging-Default: live sells + RAG apply.** Kein monatelanges Shadow-Gate.

---

## 10. Interaction matrix (wichtig)

| System | Interaction |
|--------|-------------|
| #110 Capacity | Eviction only if still full after `max_open_eff`; wider cap reduces need |
| #90 Cash | Must have spendable for **entry** after/evict; underwater reduce-to-tail frees **slot** not cash |
| #92 Rotation tempo | Share trail/gain guards; eviction **demands** entry, rotation is **tempo** |
| #93 Funding | If funding sell already frees cash/slots same cycle, skip eviction (priority: funding > eviction when both fire) |
| #94 Urgency | Victim score input |
| Grid min-gain | Grid harvest underwater stays blocked; eviction reduce-to-tail is **not** grid harvest |
| Sensor memory gates | Entry demand must pass same soft_block/facts |
| Venue quality | Entry must pass; victim may be thin-venue (higher score) |
| Memory RAG (#72 stack) | Retrieve/LLM enrich keep; fail-open; never ledger |
| Hermes index_on_cycle | Chunk freshness for evidence; no index = weak RAG |

**Priority when multiple sells want the same bag:**

```text
1. Stop / emergency
2. Trail exclusive exits
3. Funding sell (DCA) if portfolio mode live
4. Slot eviction for entry
5. Generic structure sells
```

---

## 11. Worked examples

### 11.1 Lorenzo happy path (RISK_ON, full slots)

- free=0, BANK demand=5 (sensor+5× spike), cash ok  
- Victim XYZ: +1.2% gain, idle 50h, **low keep_score** (weak memory), no trail → Class A partial 40%  
- Shadow: log; Live: sell → next cycle BANK entry  

### 11.1b Two greens — Memory decides

- free=0, BANK wants in  
- `AAA` +5%, keep=0.80 (`prefer`)  
- `BBB` +4%, keep=0.32 (weak win_rate, size_bias 0.7)  
- `CCC` +8% trail armed → veto  
- **Victim = BBB** (not AAA despite similar PnL)  
- Swap: keep(BANK)−keep(BBB) ≥ margin → ok  
- If BANK soft_block: **no plan** (must fail demand)

### 11.1c Both greens strong memory

- AAA keep=0.85, BBB keep=0.78, BANK keep_proxy=0.55  
- edge vs best victim < margin → **no_candidate memory_swap_not_worth_it**  
- Correct: nicht starke Bags opfern für mediocren Entry  

### 11.1d RAG Discord — **Live-Sell folgt RAG** (Staging)

- Profile: AAA 0.55, BBB 0.50 → profile würde BBB freimachen  
- RAG: AAA 0.30 (loss lessons), BBB 0.62 → **applied_victim = AAA**, Sell AAA  
- Log: `profile_victim=BBB rag_victim=AAA applied=AAA sell_executed=true`  
- +7d: Regret messen; bei Dauer-Schaden → `apply_to_plan=false` oder `rag.mode=off`

### 11.1e RAG timeout / empty

- Retrieve fails → keep_final = keep_profile, **Sell trotzdem** (Profile-Victim), log `rag_error`  
- Fail-open, kein totaler Stop der Eviction

### 11.2 RISK_OFF full book

- Capacity already tight (eff≈12–16)  
- Fusion size_mult low; if not block_buys, demand may still pass venue+sensor  
- **Policy choice:** `skip_if_regime in (RISK_OFF, CRASH)` optional — **v1 recommendation: allow only RISK_ON + NEUTRAL** for live eviction (defensive). Shadow may still log everywhere.

### 11.3 All bags trail-protected winners

- No candidate → reject with `eviction: no_candidate trail_protected=N`  
- Correct: don't sabotage book for FOMO  

### 11.4 Cash tight, slots full

- spendable < entry → **no eviction** (P4). Cash/harvest path first.

### 11.5 Underwater zombie, full slot

- Class B reduce-to-tail: frees full slot, keeps recovery option  
- Aligns with North Star without forced loss dump  

---

## 12. Test plan

| Case | Expect |
|------|--------|
| free_slots > 0 | no plan |
| demand below thr | no plan |
| block_buys | veto |
| best victim trail armed | skip, next or no plan |
| Two greens: weak keep vs prefer | victim = weak keep |
| Two greens: both high keep, entry mediocre | no plan (swap edge) |
| Entry soft_block | no plan |
| Class A gain≥0 + memory edge ok | plan partial/full |
| Class B underwater | reduce-to-tail only if prefer_reduce |
| Class C full loss when allow=false | never full |
| memory.enabled false | falls back to idle/pnl weights |
| rag.mode=retrieve, loss chunks on A | keep_rag(A) ↓; **sell A** if apply_to_plan |
| rag apply_to_plan=true (staging default) | applied = rag victim, sell executes |
| rag apply_to_plan=false | sell uses profile; rag only logged |
| retrieve timeout | sell uses profile, rag_error |
| llm invalid / low conf | fall back retrieve/profile, sell still ok |
| rate limit hit | veto, no sell |
| min_hold / trail / same symbol | veto |
| mode=shadow | log only |
| mode=live two-cycle | sell N, entry N+1 |

Replay: extend `hermes/sell_rotation_replay.py` **or** small `scripts/analyze_slot_eviction.py` over demo ledger (would_evict histogram).

---

## 13. Non-goals

| Non-goal | Why |
|----------|-----|
| Replace max_open_eff | Capacity remains first line |
| Loss-harvest all bags | Risk of locking losses for FOMO entries |
| LLM victim pick | latency + nondeterminism |
| Evict for DCA | #93 owns funding |
| Evict tails | no full-slot benefit |
| Memory hard-block sells | forbidden platform-wide |

---

## 14. Success criteria (Definition of Done)

1. E1 + E1b pure/RAG helpers + tests merged.  
2. **Staging live:** ≥1 real `exit_source=slot_evict_for_entry` sell driven by RAG keep_final.  
3. Trail-armed never victim (0).  
4. A/B logs show profile vs applied on every event.  
5. Kill-switches documented and smoke-tested (`mode=shadow` / `rag.mode=off`).  
6. Optional: 1–2 weeks regret metrics; decide prod defaults.  

---

## 15. Open decisions (defaults — Staging Lab)

| # | Question | Staging default |
|---|----------|-----------------|
| D1 | Live only RISK_ON/NEUTRAL? | **Yes** |
| D2 | Same-cycle entry after fill? | **No** (two-cycle; optional later) |
| D3 | Loss full-evict | **Off** |
| D4 | Reduce-to-tail underwater | **On** |
| D5 | Max notional victim | **8000 USDT** |
| D6 | Sources | Sensor-only v1 |
| D7 | Memory ranks Class-A | **Yes** |
| D8 | `prefer` hard keep | **Yes** |
| D9 | min_entry_keep_edge | **0.12** |
| D10 | RAG mode | **`retrieve`** |
| D11 | RAG steuert Live-Sells | **Yes** (`apply_to_plan=true`) |
| D12 | Eviction mode | **`live`** on staging |
| D13 | LLM compare | Optional `retrieve_llm` later |
| D14 | LLM override prefer | **No** |
| D15 | Sync RAG before sell | **Yes** (timeout fail-open) |

---

## 16. Implementation order

1. Plan + #111 (this).  
2. E1 profile keep/rank + tests.  
3. E1b RAG retrieve + evidence + A/B fields + probe.  
4. **E2 wire staging live:** max_open → RAG victim → **real sell** + pending entry + rate limits.  
5. Smoke: kill-switches, trail veto, one BANK-like story.  
6. E3 outcome/regret over days (learn, don’t block shipping).  
7. Optional `retrieve_llm`.  
8. Prod defaults later (stricter).  

**Schnellster Staging-Test:** E1+E1b+E2 mit  
`mode=live`, `rag.mode=retrieve`, `apply_to_plan=true`.

---

## 17. Related docs

- [`intelligent-position-capacity.md`](intelligent-position-capacity.md) — #110  
- [`adaptive-cash-rotation-master.md`](adaptive-cash-rotation-master.md) — #89  
- [`memory-cash-rotation-integration.md`](memory-cash-rotation-integration.md) — #71  
- [`dca-recovery-rotation.md`](dca-recovery-rotation.md) — no loss-evict D′, tail model  
- RAG stack: `hermes/memory/rag_retriever.py`, `intelligence/memory/rag_config.py`, `scripts/probe_ask_rag.py`  
- Code: `risk/position_capacity.py`, `strategies/sell_rotation_policy.py`, `strategies/dca_portfolio.py`, `strategies/exit_attribution.py`
