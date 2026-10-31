# CME Net Carry Spread – Refined Macro-Overlay

## Verbesserte Indikator-Definition

**Primär-Metrik: Net Carry Spread**
```
Net_Carry_Spread = Annualized_CME_FrontMonth_Basis − 3M_TBill_Rate
```
(Annualisierung: `((Futures - Spot) / Spot) * 365 / Days_to_Expiry`)

### Stufen (kalibriert an Galaxy Research 2026)
| Stufe | Net Spread | Bedeutung | Bot-Aktion (soft) |
|-------|------------|-----------|-------------------|
| Grün | > 2.5 pp | Carry attraktiv | Normal / Size +10–20 % |
| Gelb | 0.8 – 2.5 pp | Carry schrumpft | Size −20–40 % |
| Rot | < 0.8 pp | Carry unattraktiv (nach Kosten ~1.5 % oder weniger) | Size −50–70 %, engere Stops, neue Longs nur mit starker Bestätigung |

**Zusatzsignale (optional, bestätigend):**
- 5-Tage-Δ des Net Spreads < −1.0 pp → beschleunigte Kompression
- COT Leveraged Funds Net Short Abbau
- ETF Net Flow 5-Tage-Streak negativ

## Messbares Ergebnis (nachweisbar)

**Galaxy Research (Okt 2026):**
- 30 Tage vor ATH: Basis 7.9 % ann. → Net Spread **+3.9 pp** über T-Bills
- 30 Tage bis 21. Okt: Basis 4.8 % ann. → Net Spread **+0.6 pp**
- Nach Fees/Roll/Margin: ~1.5 pp Netto
- Ergebnis: 9 Hedgefonds/Banken (Millennium, Goldman, Brevan Howard, Tudor etc.) bauten **68.000 BTC (~5.4 Mrd. USD)** ETF-Exposure ab
- CME OI und Leveraged-Funds-Shorts schrumpften signifikant

**60-Tage-Fenster (Sep–Okt 2026):**
- Sep: höhere Basis + starke ETF-Inflows (+2.65 Mrd.) + Preis ~78k → 84k+
- Okt: Kompression auf ~0.6 pp + Outflows + Preisrückgang auf ~77k
- Richtungstreue hoch, auch wenn absolute Levels nicht dramatisch waren

## Acceptance Criteria & Testable Assertions

Diese Kriterien sind so formuliert, dass ein Implementierungs-Agent sie direkt in Unit- und Integration-Tests gießen und validieren kann.

### Unit Tests (pure functions, no network)

**AC-U1: Spread Calculation**
- Given: Futures=82000, Spot=81000, Days_to_Expiry=30, TBill=4.0
- When: compute_net_carry_spread()
- Then: result == (((82000-81000)/81000)*365/30) - 4.0 within 0.01 pp

**AC-U2: Stage Classification**
- Given: net_spread = 3.0 → stage == "green"
- Given: net_spread = 1.5 → stage == "yellow"
- Given: net_spread = 0.5 → stage == "red"
- Boundary: 2.5 exact → green; 0.8 exact → yellow

**AC-U3: Size Multiplier**
- Given: stage="green" → size_multiplier in [1.10, 1.20]
- Given: stage="yellow" → size_multiplier in [0.60, 0.80]
- Given: stage="red" → size_multiplier in [0.30, 0.50]

**AC-U4: Delta Signal**
- Given: 5-day change = -1.2 pp → acceleration_flag == true
- Given: 5-day change = -0.5 pp → acceleration_flag == false

### Integration / Fixture Tests

**AC-I1: Galaxy Window Validation**
- Fixture: pre-ATH window with basis_ann=7.9, tbill=4.0 → net=3.9 → stage="green"
- Fixture: Oct-21 window with basis_ann=4.8, tbill=4.2 → net=0.6 → stage="red"
- Assert: stage transition from green to red occurs when net drops below 0.8

**AC-I2: Soft Filter Application**
- Given: base_size=1.0, stage="red" → effective_size <= 0.50
- Given: stage="red" AND strong_confirmation=true → effective_size still reduced but entry not hard-blocked

**AC-I3: Data Source Interface**
- Mock CME basis source returns None → fallback to last known or neutral (yellow) without crash
- Assert: no live network calls in unit/integration tests

### Historical Validation Metrics (measurable)

**AC-H1: 60-Day Directional Match**
- On Sep high-spread period: average stage == green or yellow, size_multiplier >= 0.8
- On Oct low-spread period: average stage == yellow or red, size_multiplier <= 0.7
- Correlation between stage severity and subsequent 5-day ETF flow sign >= 0.6 (direction only)

**AC-H2: Galaxy Desk Unwind Alignment**
- When net_spread crosses below 1.0 pp and stays there for >=5 days → flag "elevated_unwind_risk" == true
- Documented outcome: 68k BTC reduction aligns with this flag period

### Non-Functional

**AC-NF1: Soft Only**
- Never hard-block an entry solely on red stage; only reduce size / tighten stops
- Combined with existing Venue-Gate and STH-Cost-Basis rules

**AC-NF2: Observability**
- Every decision logs: raw_basis, tbill, net_spread, stage, size_multiplier, reason

## Nächste Schritte für Produktion
1. Stabile Datenquelle: CME BasisWatch / Coinglass API / eigene Berechnung aus Yahoo/CME-Settlement
2. Tägliche Series ab mindestens Jan 2025 für Out-of-Sample-Kalibrierung
3. Integration als Confidence-/Size-Multiplikator im DecisionEngine
4. Kombination mit bestehendem Venue-Gate + STH-Cost-Basis

---
*Expanded with concrete testable acceptance criteria – 31.10.2026*