# Plan: Gate Top Coins erkennen & gewinnbringend verkaufen

**Datum:** 2026-08-05  
**Ziel (Operator, klargestellt):** Nicht am Peak kaufen. **Top-Coins identifizieren** und **profitabel verkaufen**.  
**Daten:** `scripts/retrospect_gate_top10d_strategies.py` →  
`auswertungen/gate_top10d_strategies_20260804_221626.json`  
**Fenster:** UTC 2026-07-25 … 2026-08-03 (10 volle Tage)  
**Universum:** liquide Gate USDT Spot, Vol ≥ 500k, ohne 3L/5L; Top-8 pro Tag nach day-return (close/prev_close)

---

## 0. Methodik & Bias (lesen)

| Was die Analyse **kann** | Was sie **nicht** kann |
|--------------------------|------------------------|
| Wer war rückwirkend Top-Gainer? | Live vorhersagen, wer am EOD #1 ist |
| Welche Exit-Styles auf **bekannten** Winners Profit lassen | Unbiased Winrate auf allen Coins |
| Multi-Day Repeaters (ON, COTI, SKYAI, …) | Slippage/partial fills real |
| Capture-Ratio Trail vs Tages-High | Look-ahead-freie Entry-EV |

**Selection bias:** Mix-Leaderboard (z.B. open+hold_eod +26 % avg, 100 % win) gilt **nur auf dem Top-Gainer-Set**. Wer morgens *jeden* Coin kauft, bekommt diese Zahlen **nicht**.

Gültige Fragen:
1. Wenn wir den Winner **früh genug** im Buch haben — wie verkaufen wir gut?  
2. Welche **identifizierbaren** Entry-Signale füllen noch (Accel, Breakout) und wie viel % vom Move bleibt?  
3. Welche Coins **wiederholen** sich (Continuation)?

---

## 1. Tagesboard — Top Coins (beste day-returns)

| Tag | #1 | day% | #2 | day% | #3 | day% |
|-----|----|------|----|------|----|------|
| 07-25 | DEXE | +23 | SYN | +19 | SHIB | +17 |
| 07-26 | UB | +23 | PUMP | +13 | AAVE | +10 |
| 07-27 | **ON** | +49 | COTI | +39 | AKE | +29 |
| 07-28 | **ON** | +84 | BEAT | +29 | DEXE | +13 |
| 07-29 | **COTI** | +57 | UAI | +43 | AEON | +25 |
| 07-30 | KOMA | +81 | SNXXG* | +73 | AXTIG* | +52 |
| 07-31 | RATS | +125 | KOMA | +100 | US | +27 |
| 08-01 | IDOL | +48 | UAI | +44 | BLESS | +40 |
| 08-02 | **BLESS** | +81 | TAKE | +39 | HOME | +14 |
| 08-03 | VIC | +53 | ZRC | +41 | **SKYAI** | +39 |

\* `*G` = Stock-/Tokenized-Namen — liquide, aber anderes Risikoprofil.

**Wiederholer (≥3 Tage in Top-8):** DEXE, AKE, BLESS, TAKE, ON, COTI, IDOL, BEAT, SKYAI  
→ **Continuation / sticky watch** ist empirisch real, nicht nur same-day Oracle.

**Median auf dem Top-Set:** open→high **~24 %**, open→close **~17 %**, „liegen lassen“ High→EOD **~5 %** (Punkte).  
Trail mid captured nur **~33 %** des o→hi (Median) — engere Trails **zu früh**; EOD-Hold auf Winners oft besser *in sample*, live aber riskanter.

---

## 2. Strategie-Mixe (sinnvolle Spanne)

Jeder Mix = **Identify** + **Enter** + **Exit**.  
Fokus: **Erkennen + Verkaufen**, nicht Peak-FOMO.

### Mix A — „Prev-Day Board → Hold/Trail“ (Continuation)

| | |
|--|--|
| **Identify** | Gestern Top-10 liquider day-return |
| **Enter** | Nächster UTC-Open / erste Stunden (kein Warten auf neuen +80 %) |
| **Exit** | Trail mid/wide **oder** Zeit-Exit 24–36h; chase_guard wenn schon +15–18 % vs prev close |
| **Stärke** | Kein same-day Oracle; ON/COTI/BLESS-Muster |
| **Schwäche** | Viele Prev-Tops mean-reverten |
| **Repo** | `gate_prev_top` + `chase_guard` (existiert) |

### Mix B — **„Live Heat Identify → Trail Sell“** (Same-day, WS-fähig)

| | |
|--|--|
| **Identify** | Board sticky Top-5–10 by 24h% **oder** day-so-far; Vol-Filter |
| **Enter** | Accel: first touch **+8 %** vom Day-Open **oder** Breakout prev high — **nicht** warten bis +50 % |
| **Exit** | **WS** trail mid/wide (arm ~8, trail 8–12); optional rank-decay |
| **Daten** | accel_plus8 fill **91 %** der späteren Tops; avg tradable schwächer als Open, aber **live erkennbar** |
| **Repo** | `gainer_live_heat` + künftig Board-WS |

### Mix C — **„Green Hour + EOD/Trail“** (frühes Intraday-Signal)

| | |
|--|--|
| **Identify** | Erste 1h-Kerze deutlich grün (+1 %+) im liquid universe |
| **Enter** | Close dieser Stunde |
| **Exit** | hold_eod **oder** trail_wide |
| **In-sample** | first_green + hold_eod stark auf Winner-Set; live braucht Universe-Cap |
| **Nutzen** | Früher als reines 24h-Rank |

### Mix D — **„Dip-in-Trend“** (Qualitätseinstieg)

| | |
|--|--|
| **Identify** | Coin bereits im Heat-Board |
| **Enter** | Tief der ersten 4h, solange ≥ ~−2 % vom Open (vwap_dip4h) |
| **Exit** | trail_wide / tp15 |
| **In-sample** | Gute Fills (79 %), starke med PnL mit wide trail |
| **Schwäche** | Verpasst vertikale Opens (RATS/KOMA-Style) |

### Mix E — **„Breakout Prev-High“** (Struktur)

| | |
|--|--|
| **Identify** | Preis nimmt Vortages-High +1 % |
| **Enter** | Breakout-Touch |
| **Exit** | trail_mid/wide |
| **Fill** | 88 % der späteren Tops |
| **Edge** | Klare Regel, weniger reines %-Lag-Label |

### Mix F — **„Sniper Late Avoid“** (Anti-Peak)

| | |
|--|--|
| **Identify** | Top Board, aber **Entry-Ceiling** 24h% / day-so-far **≤ 40–45 %** |
| **Enter** | nur A–E wenn unter Ceiling |
| **Exit** | aggressiver trail |
| **Zweck** | Operator-Ziel: **nicht** Peak kaufen; VIC/ZRC spät meiden wenn schon parabolic |

### Exit-Palette (für alle Mixe)

| Exit | Wann | Capture vs o→hi (in-sample, open entry) |
|------|------|----------------------------------------|
| hold_eod | Trend-Tag, starke Closes | ~79 % med |
| trail_wide (12/12) | Runner, WS | ~39 % med |
| trail_mid (8/8) | Default WS | ~33 % med |
| trail_tight (5/6) | Chop / low-cap | ~25 % med (zu eng auf Monstern) |
| tp15_sl8 | Unsicher, fester Rahmen | oft TP auf Winners, SL auf späten Accel |

**Empfehlung Exit-Default für identifizierte Tops:**  
**trail_wide** oder **hold_eod mit hard time 24h** — tight trail **zerstört** Monster-Tage (RATS/ON).

---

## 3. Empfohlener Strategy-Mix (Portfolio, nicht eine Regel)

| Sleeve | Kapital-Anteil* | Mix | Rolle |
|--------|-----------------|-----|--------|
| **S1 Continuation** | 40 % | A | Prev-day tops, multi-day names |
| **S2 Live Heat** | 40 % | B+F | Same-day identify, no peak chase |
| **S3 Structure** | 20 % | E oder D | Breakout / dip quality |
| **Global** | — | F ceiling + Vol + no leverage | Safety |

\* Anteil der **Sniper/Gainer-Slots** (z.B. 3–5), nicht des gesamten 36er-Buchs.

**Identifikation (shared):**

1. REST/WS Board liquider Paare  
2. Sticky rank + day-so-far %  
3. Multi-day streak flag (ON/COTI-Klasse)  
4. Blacklist Hebel; optional `*G` separate risk flag  

**Verkauf (shared):**

1. **WS** trail (wide default für tagged gainer)  
2. Time stop 24–36h  
3. Optional: rank fällt aus Top-15 + flat → raus  

---

## 4. Umsetzung im Bot (Plan)

### Phase 0 — Done (diese Analyse)

- [x] 10d Board + Mix-Matrix Script  
- [x] JSON Report  
- [x] Bias dokumentiert  

### Phase 1 — Identify (Shadow, 3–5 Tage)

1. Board logger pro Cycle/Scan: top-15 live + day-so-far wenn möglich  
2. Tag: `identified_gainer` ohne Buy-Zwang  
3. Metrics: wie oft identified → später EOD top-8 overlap (hit-rate)  
4. **Erfolg:** Identify-Hit auf EOD Top-8 ≥ 40–50 % (nicht 100 %)

### Phase 2 — Exit first on identified (wenn schon long)

1. Positionen mit gainer-tag: trail_wide overlay + max hold 24–36h  
2. WS exit_realtime behalten  
3. **Erfolg:** weniger Giveback High→Exit auf tagged lots  

### Phase 3 — Controlled enter (Demo)

1. Mix A prev_top (existiert) + Mix B accel/breakout **als Signal**, nicht nur inject  
2. Ceiling F; 3 slots; RiskManager  
3. Shadow would-buy 48h → Demo  
4. **Erfolg:** median entry nicht in top parabolic; expectancy ≥0 after fees  

### Phase 4 — WS board (Latenz)

1. Watch-Set WS für Identify-Latenz  
2. REST nur Seed  
3. Exit-Hub nicht im selben PR brechen  

### Nicht in Scope

- Oracle „kauf am Open alle späteren Tops“  
- Full-market WS  
- Max-open 36 mit allen Heats  

---

## 5. Messgrößen (Dashboard)

| Metric | Zielrichtung |
|--------|--------------|
| Identify hit-rate vs EOD top-8 | ↑ |
| Median entry day-so-far % / 24h% | im Band 8–35, nicht 60+ |
| Capture ratio (exit vs post-entry peak) | ↑ Richtung 0.4–0.6 |
| Giveback peak→exit | ↓ |
| % exits via WS | ≥ 50 % |
| Multi-day repeater capture | track |
| Sniper/gainer expectancy after fees | ≥ 0 |

---

## 6. Kill-Kriterien

- Identify hit-rate &lt; 25 % über 7d → Board-Logik falsch  
- Median entry &gt; 45 % day-move → Peak-FOMO trotz F  
- Expectancy gainer-sleeve &lt; 0 über ≥15 RT → mode off  
- Leverage fill &gt; 0 → filter bug  

---

## 7. Kurzfazit

1. **Top Coins der 10 Tage** sind klar gelistet; viele **Wiederholer** (ON, COTI, BLESS, SKYAI, …).  
2. **Verkaufen:** auf echten Winners oft **weiter trail / EOD** besser als tight trail; WS-Exit bleibt zentral.  
3. **Kaufen/Erkennen:** live nur **Continuation + Accel/Breakout + Ceiling** — nicht „EOD Top kaufen“.  
4. **Mix-Portfolio S1/S2/S3** statt einer Magic-Rule.  
5. Nächster Bau-Schritt: **Identify-Shadow + Exit-Overlay**, dann erst Entry-Signal.

---

## 8. Artefakte

| File | Role |
|------|------|
| `scripts/retrospect_gate_top10d_strategies.py` | Reproduzierbare Analyse |
| `auswertungen/gate_top10d_strategies_*.json` | Rohdaten + alle Mix-PnLs |
| Dieses Plan-Doc | Operator-Plan |

Re-run:

```bash
python3.13 scripts/retrospect_gate_top10d_strategies.py --days 10 --top 8 --scan 180
```
