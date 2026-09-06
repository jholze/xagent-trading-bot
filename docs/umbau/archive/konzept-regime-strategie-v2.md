> ⚠️ **SUPERSEDED · 2026-09-05** — ersetzt durch [`konzept-regime-strategie-v3.md`](../konzept-regime-strategie-v3.md) (v3 „Ersetzt v2“). Historisch, nicht mehr pflegen.

# Konzeptpapier: Regime-Adaptive Long-Short-Strategie (Pilot-Tenant) — v2

Änderungen gegenüber v1 sind mit **[v2]** markiert.

## 1. Ziel
Der Bot schaltet sich selbst zwischen Long-Bias und Short-Bias um,
basierend auf Marktregime, nicht auf Tagesrauschen.
Läuft erstmal nur auf einem neuen Tenant, alle anderen bleiben unverändert.

## 2. Architektur-Prinzip
- Feature-Flag pro Tenant: `regime_strategy_enabled`, Default `false`
- Nur der Pilot-Tenant bekommt `true`
- Keine Änderung an der globalen Logik, nur ein neuer Codepfad
- Alle Schwellen als Tenant-Config, nicht hartcodiert
- Jeder Indikator einzeln abschaltbar
- Logging für jeden Flip mit Begründung
- **[v2] Fail-Closed:** Bei Fehler, fehlenden Daten, Sidecar-Ausfall oder
  widersprüchlichen Signalen liefert die Engine `flat`. Niemals den letzten
  Bias stillschweigend fortführen. Jeder `flat`-Fallback wird geloggt.

## 3. [v2] Short-Infrastruktur (Voraussetzung, nicht Detail)
Auf Gate.io Spot ist kein Short möglich. Proaktive Shorts (Abschnitt 7)
bedeuten zwingend Margin- oder Futures-Konto. Vor Schritt 1 entscheiden:

| | Cross-Margin (Spot) | Perpetual Futures |
|---|---|---|
| API-Pfad | eigener Margin-Endpoint, Borrow/Repay | eigener Futures-Endpoint, Kontrakte statt Coins |
| Kosten | Leihzins pro Stunde | Funding Rate alle 8h |
| Risiko | Zwangsliquidation bei Margin-Level | Liquidation bei Maintenance-Margin |
| Testbarkeit | kein Sandbox | **Testnet vorhanden** |
| Position-Modell | Coins geliehen und verkauft | Kontrakt mit Größe, Hebel, Entry |

**Empfehlung:** Futures-Konto, Hebel fest auf 1x, Isolated Margin.
Grund: Testnet existiert — damit lässt sich die Order-Response-Behandlung
erstmals real testen, was auf Spot bisher nicht möglich war.

Konsequenzen für den Code:
- Short ist **kein Spiegel von Long**. Eigenes Position-Modell mit
  Liquidationspreis, Funding-Kosten und Margin-Verbrauch.
- Kapital für Shorts muss vorab auf das Futures-Konto transferiert werden;
  „Kapital frei" heißt Futures-Guthaben, nicht Spot-Guthaben.
- Funding-Rate-Kosten gehen in die Positions-PnL ein, sonst lügt der
  Gewinner/Verlierer-Check in Abschnitt 6.
- Stop-Loss muss immer enger als der Liquidationspreis sitzen.

## 4. Indikatoren (gestaffelt)

### 4.0 [v2] Zeiteinheit und Bezugs-Asset
- Regime wird auf **Tageskerzen** bestimmt (200-MA, ADX-Trend) und auf
  **4h-Kerzen** bestätigt (SuperTrend, MACD, Volumen).
- Live-Ticks aus dem Sidecar werden zu 4h-Kerzen aggregiert. Es gibt keine
  Regime-Entscheidung auf Tick-Basis.
- Regime wird auf **BTC als Markt-Proxy** bestimmt, nicht pro Asset.
  Pro Asset gibt es nur einen Eintritts-Filter (eigener SuperTrend gleiche
  Richtung wie Markt-Regime). Grund: Ein Regime pro Asset macht Backtest
  und Kapitalverteilung unkontrollierbar.
- Beides als Tenant-Config (`regime_timeframe`, `regime_reference_asset`).

### 4.1 Trendfilter (Pflicht)
- 200-Tage-Durchschnitt als Basis
- Preis darüber → Long-Bias, darunter → Short-Bias
- SuperTrend (ATR-Multiplikator 3) auf 4h als Ergänzung:
  Bänder weiten sich bei Volatilität selbst, flippen seltener als EMA-Crossover

### 4.2 Regime-Stärke (Pflicht)
- ADX auf Tageskerzen
- Unter 20 → Seitwärts, Bot tradet nicht
- Über 25 → echter Trend, Richtung wird freigegeben
- 20–25 → Zone wird als „unentschieden" behandelt: bestehender Bias bleibt,
  kein neuer Flip

### 4.3 Volumen-Bestätigung (Pflicht für Flip)
- Durchbruch gilt nur bei überdurchschnittlichem Volumen (über 20-Tage-Mittel)
- Sonst kein Flip

### 4.4 Momentum (Bestätigung)
- MACD unter Nulllinie → Short-Bestätigung, darüber → Long
- RSI als Zusatz, nicht als alleiniger Trigger

### 4.5 On-Chain & Derivate (Kontext, nicht Hauptfilter)
- MVRV Z-Score, Funding Rates
- Extreme Funding Rates signalisieren Überhebelung vor dem Preis-Kippen

### 4.6 [v2] Konfliktauflösung
Die Indikatoren sind in drei Klassen eingeteilt. Die Auflösung ist eine
feste Regel, kein implizites Verhalten:

1. **Pflicht-Bedingungen** (4.1 Trend, 4.2 ADX, 4.3 Volumen):
   Alle müssen für einen Flip dieselbe Richtung zeigen.
   Fehlt eine → kein Flip, bestehender Bias bleibt.
   Ist eine nicht berechenbar (fehlende Daten) → `flat`.
2. **Bestätigungen** (4.4 MACD, 4.5 On-Chain):
   Score-basiert, konfigurierbar. Default: mindestens 1 von 2 muss
   zustimmen. Widerspricht eine aktiv → Flip wird um eine Hysterese-Periode
   verzögert, nicht verhindert.
3. **Veto** (Abschnitt 9): kann einen Flip blockieren, nie auslösen.

Abgeschaltete Indikatoren zählen als „stimmt zu" (neutral), nicht als
„fehlt". Sonst kann man die Engine durch Abschalten in `flat` zwingen.

## 5. Hysterese
- Flip braucht 2–3 Tage Stabilität im neuen Zustand
  (konfigurierbar, gezählt in Tageskerzen)
- Kein tägliches Hin-und-Her
- **[v2]** Hysterese-Zähler wird bei jedem Gegen-Signal auf 0 gesetzt,
  nicht nur dekrementiert

## 6. Partial-Exit beim Flip
**[v2] Klargestellte Regel** (v1 war mehrdeutig):

Beim Flip wird jede offene Position gegen den neuen Bias geprüft:
- **Gewinner** (PnL > 0): sofort geschlossen, Gewinn realisiert
- **Verlierer innerhalb Toleranzband** (PnL zwischen 0 und −3…−5 %):
  sofort geschlossen. Kleiner Verlust, Kapital wird frei.
- **Verlierer außerhalb Toleranzband** (PnL < −5 %): ebenfalls geschlossen.
  Das Toleranzband dient **nicht** dazu, große Verlierer zu halten.

Kurz: Beim Flip wird alles geschlossen, was gegen den neuen Bias steht.
Das Toleranzband bestimmt nur die **Reihenfolge und das Logging**
(Verlierer außerhalb Band werden als „Stop-Loss hätte greifen müssen"
markiert — das ist ein Alarm für die Stop-Loss-Logik, kein Halte-Grund).

Beispiel, Flip Long → Short bei drei Long-Positionen:
- ETH +4 % → geschlossen, Gewinn
- SOL −2 % → geschlossen, im Band
- DOGE −9 % → geschlossen, außerhalb Band, Alarm im Log

Weiterhin:
- Jede Position hat festen Stop-Loss
- Entscheidung hängt nicht am aktuellen Preis
- **[v2]** PnL für Futures-Positionen inkl. gezahlter Funding-Kosten

## 7. Proaktive Short-Eröffnung
- Bisher: Short nur reaktiv, als Folge eines Verkaufs
- Neu: Bot darf Shorts direkt aus dem Regime-Signal eröffnen,
  sobald Kapital frei ist — ohne vorherigen Verkauf
- Verkauf bleibt eine von mehreren Quellen für freies Kapital,
  nicht mehr Voraussetzung
- Altes reaktives Verhalten bleibt bei allen anderen Tenants exakt erhalten
- **[v2]** „Kapital frei" = verfügbares Guthaben auf dem Futures-Konto
  minus reservierte Margin offener Positionen (siehe Abschnitt 3)
- **[v2]** Maximale Short-Exposure pro Tenant als Config
  (`max_short_exposure_pct`), Default 30 % des Tenant-Kapitals

## 8. WebSocket-Sidecar & Memory
- Sidecar liefert nur Daten, nicht die Entscheidung
- Nutzung:
  1. Ticks werden zu 4h-Kerzen aggregiert und speisen SuperTrend/MACD
     **[v2]** (keine Indikator-Berechnung direkt auf Ticks, siehe 4.0)
  2. Sofortige Order-Auslösung beim Flip, ohne Poll-Zyklus-Wartezeit
     — gilt nur für die **Ausführung**, nicht für die Signalbildung
- Memory-Funktion speichert letzten Bias-Zustand:
  aktiver Bias, Zeitpunkt des letzten Flips, Hysterese-Status
  → Neustart überlebt den Kontext, kein Doppel-Flip
- **[v2] TTL für Memory-Zustand:** Ist der gespeicherte Zustand älter als
  `regime_memory_ttl` (Default 12 h), wird das Regime neu bestimmt statt
  übernommen. Bis zur Neubestimmung: `flat`, keine neuen Positionen.
- Sanity-Check: bei Tick-Lücken oder unrealistischen Sprüngen
  Fallback auf REST-API, statt auf kaputte Daten zu shorten
- **[v2]** Fällt auch REST aus → `flat` (Fail-Closed, Abschnitt 2)

## 9. Veto-Layer
- Whale-Tracking & Liquidations-Heatmaps nur als Bestätigung
- Standardmäßig aus, pro Tenant aktivierbar
- Beispiel: Trendfilter sagt Short, aber große Wallets akkumulieren massiv
  → Bot wartet ab
- Heatmaps zeigen nur mögliche Liquidationen, nicht ob sie kommen
  (Genauigkeit ~55–65 %, nie allein entscheiden lassen)
- **[v2]** Veto kann einen Flip nur **verzögern oder blockieren**,
  nie auslösen und nie eine Position öffnen
- **[v2]** Datenquelle vor Aktivierung verifizieren — die Santiment-Slugs
  im Bestand sind noch ungeprüft; ein Veto auf falschen Daten ist
  schlimmer als kein Veto

## 10. Implementierungsstrategie

### [v2] Schritt 0: Short-Infrastruktur entscheiden
- Futures vs. Margin festlegen (Empfehlung: Futures, 1x, Isolated)
- Gate.io Futures-Testnet-Zugang einrichten
- Kapitaltransfer Spot → Futures als eigene Funktion, mit Limit
- Ohne diesen Schritt ist Schritt 5 nicht umsetzbar

### Schritt 1: Codebase analysieren
- Tenant-Modell finden
- Bestehende Indikatoren und Position-Logik lokalisieren
- Prüfen ob SuperTrend, ADX, MACD schon vorhanden sind
- **[v2]** Prüfen, wo Exceptions still geschluckt werden — im neuen
  Codepfad ist das verboten (Fail-Closed)

### Schritt 2: Feature-Flag einführen
- Migration für `regime_strategy_enabled`, Default `false`
- Nur Pilot-Tenant aktivieren

### Schritt 3: Regime-Engine bauen
- Neues Modul, berechnet Indikatoren, gibt Bias aus: long / short / flat
- Streng gekapselt, keine Seiteneffekte auf andere Tenants
- **[v2]** Konfliktauflösung aus 4.6 als eigene, isoliert testbare Funktion
- **[v2]** Jeder Rückgabewert trägt eine Begründungs-Struktur mit
  (welcher Indikator hat was gesagt), nicht nur den Bias

### Schritt 4: Partial-Exit-Logik
- Beim Flip Positionen nach Regel aus Abschnitt 6 schließen
- Toleranzband konfigurierbar pro Tenant

### Schritt 5: Proaktive Short-Eröffnung
- Short-Eröffnung aus Verkaufs-Logik herauslösen
- Eigene Regelkette, Futures-Kapital-Verfügbarkeit als Voraussetzung
- Exposure-Limit aus Abschnitt 7

### Schritt 6: WebSocket-Integration & Memory
- Tick-Aggregation zu 4h-Kerzen
- Memory-Persistenz für Bias-Zustand mit TTL
- Sanity-Check mit REST-Fallback, dahinter `flat`

### Schritt 7: Veto-Layer
- On-Chain- und Heatmap-Daten als optionale Bestätigung
- Standardmäßig aus
- Datenquellen vorher verifizieren

### Schritt 8: Tests
- Unit-Tests für Regime-Logik, insbesondere Konfliktauflösung und alle
  Fail-Closed-Pfade
- Integrationstest mit simulierten Daten
- **[v2]** Integrationstest gegen Gate.io Futures-Testnet: echte
  Order-Responses, Teilausführungen, Ablehnungen
- Backtest auf historischen Daten für Pilot-Tenant
- **[v2] Backtest-Fallen:**
  - Look-ahead-Bias bei On-Chain-Daten (MVRV wird nachträglich revidiert,
    nur Point-in-Time-Daten verwenden oder 4.5 im Backtest deaktivieren)
  - Funding-Rate-Historie einrechnen, sonst sind Shorts im Backtest
    systematisch zu profitabel
  - Hysterese im Backtest exakt wie live zählen (Tageskerzen, Reset bei
    Gegen-Signal)

### Schritt 9: Rollout
- Paper-Trading auf Pilot-Tenant
- Live mit kleinem Kapital
- Erst auf weitere Tenants ausweiten, wenn Metriken stimmen
- **[v2] Voraussetzung:** Die bekannten kritischen Punkte aus dem
  Staging-Review (fail-open-Pfade, stilles Exception-Schlucken in den
  Entscheidungspfaden) müssen geschlossen sein, bevor der Pilot live geht.
  Die Regime-Strategie baut auf `risk_manager` und `decision_engine` auf —
  ist deren Review nicht fertig, ist der Pilot nicht freigabefähig.

## 11. Constraints
- Keine Änderung an bestehenden Tenants
- Jeder Indikator einzeln abschaltbar (abgeschaltet = neutral, nicht fehlend)
- Alle Schwellen als Tenant-Config
- Logging für jeden Flip mit Begründung
- **[v2]** Fail-Closed: Fehler oder Datenlücke → `flat`, niemals
  Fortführung des letzten Bias
- **[v2]** Keine Regime-Entscheidung auf Tick-Basis
- **[v2]** Stop-Loss immer enger als Liquidationspreis
- **[v2]** Kein Short ohne Exposure-Limit

## 12. [v2] Offene Entscheidungen
- Futures oder Margin (Empfehlung steht, Entscheidung offen)
- Toleranzband: 3 % oder 5 % als Default für den Pilot
- Hysterese: 2 oder 3 Tage als Default
- Soll der Pilot-Tenant überhaupt Spot-Longs halten, oder komplett auf
  Futures laufen (Long und Short im selben Konto, einfacheres Kapitalmodell)?
