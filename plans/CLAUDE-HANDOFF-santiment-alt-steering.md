# CLAUDE HANDOFF — Santiment / Alt-Steering Plan

**An:** Claude Code  
**Von:** Grok (xAI) + Jens  
**Datum:** 2026-08-09  
**Branch:** `docs/claude-handoff-santiment-plan`  
**Env:** Staging only (`xagent-test` / Railway `test`) — **kein Production**

---

## Was du tun sollst

1. **Lies vollständig:**
   - `plans/CLAUDE-HANDOFF-santiment-alt-steering.md` (diese Datei)
   - `plans/santiment-alt-steering-validation.md` (Hauptplan)
   - Optional Kontext: `plans/santiment-sidecar-service.md`, `plans/staging-first-learning-and-exits.md`

2. **Deliverables (schreibe zurück in Dateien auf diesem Branch):**

| # | Datei | Inhalt |
|---|--------|--------|
| R1 | `plans/reviews/claude-review-santiment-alt-steering.md` | Kritischer Review des Plans |
| R2 | `plans/reviews/claude-s1-spec.md` | Konkrete Spec für Ticket **S1** (market_bias Tags) |
| R3 | Optional | Nur wenn du echte Lücken findest: kurze Änderungsvorschläge am Hauptplan als Diff-Beschreibung in R1 — **Hauptplan nicht umschreiben**, außer klar begründet |

3. **Nicht implementieren** in diesem Durchlauf (kein Code in `risk/`, `services/`, Config-Deploy).  
   Nur Review + Spec. Implementierung = späteres Ticket nach Jens/Grok-OK.

4. Wenn fertig: kurze Checkliste am Ende von R1:
   - [ ] Plan gelesen
   - [ ] R1 geschrieben
   - [ ] R2 geschrieben
   - [ ] Go/No-Go zu S1 (ready / needs changes)

---

## Kontext (kurz, damit du nicht raten musst)

### Architektur (gut, bleibt)

```text
xagent-santiment (GraphQL API, thrifty lean)
  → POST /api/santiment/ingest
  → santiment_store + santiment_policy
  → market_policy_fusion (+ market oracle)
  → Risk size_mult / sensor_policy / block_buys
```

- Bot pollt Santiment **nicht** selbst.
- Lean default: 4 Metrics (BTC/ETH DAA + Vol), Poll ~30m, abort on 429 (PR #237 u.a. auf staging).

### Aktuelle Marktlage (Staging, Stand Session)

- Fusion oft **RISK_OFF ×0.5**, sensor **shadow**
- Treiber: Santiment on-chain (DAA Δ negativ ≈ −19 % blended → composite ≈ −0.47)
- Oracle oft milder (NEUTRAL ×0.85)
- Käufe: eher Grid + DCA-Sniper; weniger aggressive neue Entries

### Offene Produktfrage

> Reicht BTC/ETH-DAA als globaler Dimmer fürs **Altcoin-Book**?  
> Evidence fehlt (lange 429/empty; keine Regime-Tags pro Trade).  
> Plan: messen (S1 Tags → 14d Scorecard) bevor Policy hart geändert wird.

### Was bereits closed / nicht zurückdrehen

| Thema | Hinweis |
|-------|---------|
| Thrifty lean API | staging live — nicht full profile ohne Budget |
| Position lock / no_dca honor | #235 |
| Stable BB min-gain | #231/#234 |
| DCA sniper small default | bewusst so gelassen |

---

## Review-Fokus (R1)

Bitte strukturiert antworten:

### 1. Plan-Qualität
- Klarheit der Phasen A–E?
- Kill/Go-Kriterien messbar genug?
- Fehlende Risiken?

### 2. S1 (Instrumentierung)
- Reichen die vorgeschlagenen `market_bias`-Felder?
- Wo exakt im Code taggen? (vermutlich `TradingService` / RiskDecision / ledger `request_extra` / risk snapshot)
- Multi-tenant / demo scope Fallstricke?
- Grid-Buys mit taggen? (Plan sagt: Grid separat in Scorecard)

### 3. Scorecard (Phase B)
- KPIs realistisch mit Staging-Demo-Ledger?
- Bias durch Grid-Roundtrips / Sniper-DCA?
- Besser 14d oder 28d?

### 4. Altcoin-Fit
- Ist „BTC DAA → global size“ als Hypothese fair?
- Was **nicht** in Santiment lösen (WQE, gainer, memory)?

### 5. Priorisierung
- Was streichen / verschieben?
- Was muss vor S1 passieren?

Sei **kritisch und konkret** (Dateipfade, Felder, Akzeptanzkriterien). Kein generisches „looks good“.

---

## S1 Spec-Anforderungen (R2)

Schreibe implementierbare Spec:

1. **Ziel:** Jeder BUY (filled + rejected) trägt Fusion-Bias für spätere Auswertung.  
2. **Felder:** mind. regime, size_mult, sensor_policy, block_buys, source, san/oracle split, as_of.  
3. **Code-Orte:** konkrete Module/Funktionen (nach Codebase-Suche).  
4. **Tests:** welche Unit-Tests.  
5. **Nicht:** Config-Threshold-Änderungen, Prod, full Santiment metrics.  
6. **Acceptance:** Beispiel JSON; „wie prüft man auf Staging in 5 Minuten“.  
7. **Effort:** S / M.  
8. **Risiken:** Payload-Größe, fail-open wenn Fusion down.

---

## Constraints

- Staging-first, Demo-Ledger OK zum Experimentieren  
- Fail-open bei externen APIs beibehalten  
- Keine Secrets committen  
- Keine großen Refactors  
- Deutsch oder Englisch OK; strukturierte Markdown-Überschriften  

---

## Nach deiner Arbeit

Jens sagt Grok: „Claude ist fertig“ → Grok liest:

- `plans/reviews/claude-review-santiment-alt-steering.md`
- `plans/reviews/claude-s1-spec.md`

und gibt Feedback / nächsten Implementierungs-PR.

---

## Dateien anlegen

```bash
mkdir -p plans/reviews
# dann R1 + R2 schreiben
```

**Branch nicht nach production mergen.** Review-only branch; Merge der Specs nach staging später optional als docs-only PR.
