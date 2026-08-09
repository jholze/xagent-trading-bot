# Plan: Santiment / Global Bias — Altcoin-Steering validieren & schärfen

| | |
|--|--|
| **Status** | Plan (Staging-first) |
| **Stand** | 2026-08-09 |
| **Env** | Railway **test** / `xagent-test` + `xagent-santiment` |
| **Production** | erst nach messbarem Staging-Nutzen |
| **Related** | `plans/santiment-sidecar-service.md` · `plans/staging-first-learning-and-exits.md` · PR #237 thrifty lean |

---

## 0. Leitplanke

> **Staging = Demo.** Direkt messen und iterieren.  
> Kein monatelanges Shadow-Ritual — aber **eine** messbare Frage pro Phase.  
> Architecture (Sidecar → Ingest → Fusion → Risk) **beibehalten**.

**Aktuell closed / live on staging (nicht neu aufmachen):**

| Thema | PR / Stand |
|-------|------------|
| Thrifty lean API (4 Metriken, 30m, 429-abort) | #237 + follow-ups auf `staging` |
| Position lock honor `no_dca` | #235 |
| Stable BB micro-sell floor | #231 + #234 |
| Branch-Arbeit dazu | **zu** — weiter nur `staging` |

---

## 1. Problemstellung

### Was wir haben

- Globaler Markt-Bias aus **BTC/ETH On-Chain (lean: DAA + Vol)** + Oracle-Fusion  
- Wirkung: `size_mult`, `sensor_policy` (active/shadow/block), optional `block_buys`  
- Gilt **book-weit** (auch Alts) — **nicht** altcoin-sektorspezifisch  
- Lange Zeit oft **wirkungslos** (API 429 / empty features → neutral fail-open)  
- Seit thrifty + Reset: wieder **echte** Regime (z. B. RISK_OFF ×0.5 wegen DAA−19 %)

### Was wir **nicht** haben

1. **Evidence:** Hat RISK_OFF/ON Alt-PnL / Drawdown / Churn verbessert?  
2. **Audit pro Trade:** `fusion.regime`, `size_mult`, `sensor_policy` am Order  
3. **Counterfactual:** „hätte ohne Santiment-Size besser performt?“  
4. **Alt-Proxy:** nur BTC/ETH-DAA → schwache Aussage über Meme/L2-Rotation  

### Kernfrage (eine)

> **Ist der globale Santiment-Dimmer (Size/Sensor) für unser Staging-Alt-Book nützlich genug, um ihn so zu lassen — oder schadet er (zu wenig Risk-on, zu oft shadow)?**

---

## 2. Zielbild (90 Tage Staging)

```text
                    ┌─ Oracle ────────────┐
 Santiment lean ──►│ Fusion (strict min) │──► size_mult + sensor
                    └─ Memory (soft) ─────┘
                              │
                    Order tags + day scorecard
                              │
                    Kill / tune / promote rules
```

**In Scope**

- Observability (Regime pro Trade + Tageskarte)  
- 14–28d Staging-Scorecard mit/ohne Dimmer  
- Optional sparsame Feature-Erweiterung (nur wenn Evidence schwach)  
- Klare Kill-/Keep-Kriterien  

**Out of Scope (dieser Plan)**

- Per-coin Santiment scores  
- Sidecar handelt / Ledger anfasst  
- Production-Default ändern  
- Volles SanAPI-full-Profil (teuer) ohne Budget-Plan  

---

## 3. Ist-Architektur (SSOT — beibehalten)

| Layer | Rolle | Status |
|-------|--------|--------|
| `xagent-santiment` | GraphQL, thrifty lean, push | live, sparsam |
| Bot ingest + store | Snapshot | live |
| `santiment_policy` | size/sensor/block | live, fail-open |
| `market_policy_fusion` | Santiment + Oracle | live, strengere Bias |
| Risk / Entry-Sensor | wendet Fusion an | live |

**Bewertung Architektur:** gut (Producer/Consumer).  
**Offene Lücke:** Product-Evidence + Alt-Fit, nicht Rewrite.

---

## 4. Phasen (Staging-first)

### Phase A — Instrumentierung (1–2 Tage) ⭐ zuerst

**Ziel:** Jede relevante Entscheidung ist später auswertbar.

| Deliverable | Details |
|-------------|---------|
| **A1 Order-Tags** | Bei BUY (filled + rejected): `market_regime`, `fusion_size_mult`, `sensor_policy`, `fusion_source` (santiment/oracle/…) in `request_extra` oder risk snapshot |
| **A2 Cycle/JSONL** | Bereits fusion in cycle summary? Sicherstellen: 1 Zeile/Tag oder cycle mit regime + mult |
| **A3 Health** | Schon ok; optional: `scores.daa_d` in line kürzer |
| **A4 Snapshot-Archive** | Optional Mongo `santiment_snapshots` last 30d (kein Hot-Path) |

**Done:** 48h Staging mit Tags auf ≥50 Buys/Rejects.

**Kill:** Tags fehlen → Scorecard unmöglich.

---

### Phase B — 14-Tage Baseline Scorecard (parallel zu A, Auswertung ab Tag 14)

**Ziel:** „Hat der Dimmer geholfen?“ — staging ledger only.

| KPI | Definition | Richtung „gut“ |
|-----|------------|----------------|
| **New-entry PnL 24h/72h** | unrealized+realized nach Entry, bucket by `fusion_size_mult` | RISK_OFF-Entries nicht schlechter bei ×0.5 (Schutz) |
| **Max DD open book** | peak-to-trough NAV staging | niedriger an RISK_OFF-Tagen |
| **Entry count** | new buys / day by regime | RISK_OFF < NEUTRAL < RISK_ON |
| **Churn** | sells within 6h of buy (grid separate) | nicht explodieren |
| **Coverage** | % Zeit `metrics_ok≥1` vs fail-open empty | >80 % nach thrifty |

**Buckets**

- `regime ∈ {RISK_ON, NEUTRAL, RISK_OFF, CRASH}`  
- `size_mult ∈ {≤0.5, 0.5–0.85, ≥0.85}`  
- Entry family: `grid` | `gainer` | `sensor` | `other` (Grid separat werten!)

**Methode**

1. Script `scripts/scorecard_fusion_regime_14d.py` → JSON unter `auswertungen/fusion/`  
2. Markdown Summary: „an RISK_OFF-Tagen weniger Entries? schlechtere 72h-PnL wenn trotzdem full size?“  
3. Counterfactual **light**: store `would_size_mult_if_neutral=1.0` only in log (no second book)

**Done:** 1 Scorecard-MD mit Zahlen, nicht nur Bauchgefühl.

**Kill-Kriterien (Beispiele, vorab festlegen)**

| Ergebnis | Entscheidung |
|----------|--------------|
| RISK_OFF-Tage: Entries ähnlich viele, PnL schlechter | Dimmer zu schwach oder ignoriert |
| RISK_OFF: deutlich weniger/kleinere Entries, 72h-PnL der verbleibenden besser/ neutral, DD runter | **Keep** |
| RISK_OFF: fast keine Entries, verpasste gute Alts-Tage (Oracle NEUTRAL, große Upside) | **Tune** Schwellen milder |
| `metrics_ok=0` >20 % Zeit | API/Thrift fix first, nicht Regime-Logik |

---

### Phase C — Policy-Tuning (nur nach B, 1 Knob)

**Nur eine Änderung pro Experiment** (staging-first).

| Option | Wann | Change |
|--------|------|--------|
| **C1 Milder RISK_OFF** | zu wenig Trades, Oracle oft NEUTRAL | `composite ≤ −0.35` → `−0.45`; size 0.5→0.65 |
| **C2 Strenger** | RISK_OFF-Entries immer noch bad | size 0.5→0.35; sensor shadow stay |
| **C3 Fusion** | Santiment dominiert zu hart | Fusion: weighted avg statt strict min (nur Size) |
| **C4 Apply flags** | A/B | 7d: `santiment_apply_size_mult=false` vs true (Oracle only) |

**Nicht** in C: full metric profile freischalten ohne Budget.

**Done:** 7d Soak + Mini-Scorecard vs Phase B baseline.

---

### Phase D — Optionale Feature-Erweiterung (nur wenn B sagt „zu dünn“)

Quota-Budget nach thrifty (~6k calls/Monat bei lean 4×/30m).

| Stufe | Calls/Poll | Inhalt | Budget-Impact |
|-------|------------|--------|---------------|
| lean (jetzt) | 4 | DAA+Vol BTC/ETH | SSOT |
| **lean+dev** | 6 | +dev_activity | +50 % |
| **lean+social** | 6 | +social if fresh | +50 %, 1d lag risk |
| full | 10–12 | +leverage | teuer, nur wenn Credits klar |

**Regel:** Max **eine** Stufe hoch, 14d messen, sonst zurück.

**Altcoin-spezifisch eher woanders (parallel, nicht Santiment):**

- WQE / memory soft_block  
- Gainer gate  
- path_stats bias  

Santiment bleibt **BTC/ETH global dimmer**.

---

### Phase E — Production gate (später)

Nur wenn Staging:

1. Coverage >80 %  
2. Scorecard „Keep“ oder getuntes „Keep“  
3. Kill-switch dokumentiert  
4. Kein 429-Burn 14d  

Dann: gleiche thrifty Defaults, Flags offline-fähig.

---

## 5. Observability Spec (A1 — konkret)

Mindestfelder am BUY-Risk-Snapshot / `request_extra`:

```json
{
  "market_bias": {
    "regime": "RISK_OFF",
    "size_mult": 0.5,
    "sensor_policy": "shadow",
    "block_buys": false,
    "source": "santiment+oracle",
    "santiment_regime": "RISK_OFF",
    "oracle_state": "NEUTRAL",
    "santiment_size_mult": 0.5,
    "oracle_size_mult": 0.85,
    "as_of_san": "ISO",
    "as_of_oracle": "ISO"
  }
}
```

Reject-Codes mit gleichem Block (wenn market_block).

---

## 6. Betriebs-SSOT (Santiment API)

| Knob | Staging default (nach #237) |
|------|------------------------------|
| Profile | `lean` |
| Poll | `1800` s |
| Abort on 429 | yes |
| Inter-request delay | ~0.35 s |
| Full metrics | nur bewusst |

**Quota-Check:** nach Reset Health `ok≥1`; bei 429 Backoff, nicht 10× retry.

**Key:** nur Railway Sidecar; Reset max 1×/90d Santiment Account.

---

## 7. Ticket-Schnitt (klein, sequentiell)

| # | Ticket | Effort | Abhängigkeit |
|---|--------|--------|--------------|
| **S1** | Order/risk tags `market_bias` | S | — |
| **S2** | Scorecard script 14d + MD template | S–M | S1 ≥2d data |
| **S3** | 14d read-out + Go/No-Go | S | S2 |
| **S4** | Optional 1× threshold tune | S | S3 |
| **S5** | Optional lean+dev 14d | S | S3 says thin |

Kein Big-Bang-PR.

---

## 8. Risiken

| Risiko | Mitigation |
|--------|------------|
| Tags fehlen still | S1 DoD strikt |
| Grid-Noise verzerrt Scorecard | Grid-Bucket trennen |
| 429 erneut | thrifty + Backoff; Scorecard Coverage-KPI |
| Overfit 14d | 28d prefer; keine Prod-Promote |
| Fusion strict min zu hart | C3 nur nach Evidence |

---

## 9. Go / No-Go nach 14–28 Tagen Staging

| Go (behalten + ggf. mild tune) | No-Go (Dimmer abschwächen) |
|--------------------------------|----------------------------|
| RISK_OFF: weniger/kleinere new entries | RISK_OFF ignoriert (gleiche Entry-Rate) |
| DD an stress days niedriger oder flat | DD gleich + verpasste gute Tage dominant |
| Coverage high, 429 rare | oft empty/neutral fail-open |
| Team kann Scorecard lesen | nur Bauchgefühl |

**No-Go Default:** `santiment_apply_size_mult=false` 7d (Oracle only), nicht Architecture kill.

---

## 10. Nächster konkreter Schritt

1. ~~Thrifty + Reset~~ **done on staging**  
2. **S1** Ticket: `market_bias` auf BUY risk snapshot (small PR)  
3. 14d laufen lassen, parallel normal staging trading  
4. **S2/S3** Scorecard → entscheiden Keep / C1–C4  

---

## 11. Ein-Satz-Ziel

> **Globalen BTC/ETH-Dimmer messbar machen, in 2–4 Wochen Staging entscheiden ob er Alts schützt oder nur bremst — Architecture bleibt, Policy folgt Zahlen.**
