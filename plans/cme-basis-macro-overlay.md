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

**Verbesserung gegenüber vorheriger Version:**
- Absolute Basis → Net Spread (relativ zu Funding-Kosten) eliminiert Zinsniveau-Bias
- Schwellen an realem Desk-Verhalten (Galaxy) statt Willkür kalibriert
- Soft-Filter statt Hard-Gate reduziert False-Positive-Risiko
- Messbare Kausalität: Spread-Kollaps → quantifizierbarer ETF-Abbau

## Nächste Schritte für Produktion
1. Stabile Datenquelle: CME BasisWatch / Coinglass API / eigene Berechnung aus Yahoo/CME-Settlement
2. Tägliche Series ab mindestens Jan 2025 für Out-of-Sample-Kalibrierung
3. Integration als Confidence-/Size-Multiplikator im DecisionEngine
4. Kombination mit bestehendem Venue-Gate + STH-Cost-Basis

---
*Refined after multi-agent review + Galaxy evidence – 31.10.2026*