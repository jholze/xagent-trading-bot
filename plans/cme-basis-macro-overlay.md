# CME Futures Basis (Cash-and-Carry) als Macro-Overlay / Deleveraging-Indikator

## Ziel
Frühwarnsystem für institutionelles Deleveraging (Cash-and-Carry-Unwind) als Macro-Filter auf bestehende Signale im Trading Bot.

## Hintergrund
Hedgefonds und Banken betreiben Cash-and-Carry:
- Long Spot-ETF
- Short CME Bitcoin Futures
- Verdienen die Basis (Futures-Premium)

Wenn die annualisierte Basis unter ~1.5–2 % über dem 3M-T-Bill-Satz fällt, wird der Trade unattraktiv → Desks verkaufen ETFs (Druck auf Spot) und schließen Futures-Shorts. Das erzeugt Abwärtsdruck ohne neuen Short.

## Indikator-Definition
**CME / Nearest Futures Basis (annualisiert)**
- Formel: `((Futures - Spot) / Spot) * (365 / Tage_bis_Expiry) * 100`
- Vergleich zu 3-Monats-T-Bill-Satz

### Stufen (initial)
| Stufe | Bedingung | Bedeutung | Bot-Aktion |
|-------|-----------|-----------|------------|
| **Grün** | Basis > 3 % über T-Bills | Carry aktiv | Normaler Betrieb |
| **Gelb** | 1.5–3 % über T-Bills | Carry schrumpft | Positionsgrößen reduzieren, engere Stops |
| **Rot** | < 1.5 % über T-Bills | Carry tot | Venue-Gate scharf / Richtung Short / neue Longs nur mit starker Bestätigung |

## Integration
- Neuer Feed: `cme_basis`
- Macro-Overlay / Filter im DecisionEngine / Regime-Modul
- Parallel zu Liq-/Lock-Regeln und Venue-Gate

## 60-Tage-Backtest (ca. 01.09.2026 – 31.10.2026) – Initial Notes
Siehe Analyse unten / im Issue.

### Datenpunkte (Maketo nearest-contract, multi-exchange)
- High: 7.63 % (16. Sep 2026)
- Low: 3.66 % (1. Okt 2026)
- 30. Sep: 5.21 %
- Latest (30. Okt): ~5.5–6.1 %

### 3M T-Bill
- Sep ~3.9–4.0 %
- Okt ~4.1–4.3 %

### Spread über T-Bills
Meist im gelben bis roten Bereich (1–3 % oder darunter). Selten grün.

### ETF-Flows & Preis
- Sep: starke Inflows (~+2.65 Mrd. USD, Peak +999 Mio. am 21. Sep), Preis von ~78k auf ~84k+
- Okt: Mix, große Outflows (u. a. ~-643 Mio. am 30. Okt partial), Preis zurück auf ~77k

**Interpretation:** Basis-Kompression / relativ niedriger Spread in Oktober aligned mit Shift zu Outflows und Preisdruck. Indikator hätte mostly Yellow/Red signalisiert — nützlich als Filter.

## Offene Punkte
- [ ] Datenzugang stabilisieren (Maketo / Coinglass / CME / Yahoo)
- [ ] Schwellen kalibrieren
- [ ] Kombination mit STH Cost Basis (~76k)
- [ ] PR + Issue erstellen

---
*Spec + Initial Backtest Notes aus Voice-Session 31.10.2026*