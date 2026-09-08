# Konzeptpapier v3: Echte Order-Ausführung, Buchhaltung und Regime-Adaptive Long-Short-Strategie

Stand: 4. September 2026
Ersetzt v2. Enthält alle Entscheidungen aus der Diskussion und die Ergebnisse der Jesse-Analyse.

---

## Teil A — Analyse des Jesse-Frameworks

Quelle: github.com/jesse-ai/jesse, Stand heute (605 Python-Dateien, MIT-Lizenz, aktiv gepflegt).
Wichtig vorab: Die Live-Treiber (Verbindung zu echten Börsen) sind **nicht** im Open-Source-Teil, sondern im kostenpflichtigen `jesse-live`. Offen sind die Modelle, die Simulations-Engine, die Strategie-Basisklasse und die Schnittstellen. Das reicht aber, um die Architektur zu verstehen — und genau die Architektur ist das, was wir übernehmen wollen.

### A.1 Was Jesse besser macht als wir — und was wir übernehmen

**1. Ein Codepfad für Backtest, Paper und Live**
Die Order-Ausführung (`order_service.execute_order`) ist eine einzige Funktion. Der Unterschied zwischen Simulation und Live liegt in genau einer Frage: `jh.is_livetrading()`. Im Live-Modus kommen Fee, gefüllte Menge und Kontostand von der Börse; in der Simulation werden sie berechnet. Alles andere — Positions-Update, Ledger-Eintrag, Stop-Loss-Handling — ist identisch.
→ **Übernehmen.** Das ist die Antwort auf „Paper-Modus ist zu falsch": Nicht einen besseren Paper-Modus bauen, sondern den Unterschied zwischen Paper und Live auf die Datenquelle reduzieren.

**2. Order-Modell mit vollständigem Lebenszyklus**
Jesse-Orders haben: `exchange_id` (die ID der Börse, getrennt von der eigenen), `qty` und `filled_qty` getrennt, `remaining_qty`, Status `ACTIVE / QUEUED / PARTIALLY_FILLED / EXECUTED / CANCELED`, ein `reduce_only`-Flag, `submitted_via` (STOP_LOSS / TAKE_PROFIT / normal), `order_exist_in_exchange` und `jesse_submitted` (unterscheidet Bot-Orders von manuell an der Börse platzierten).
→ **Übernehmen.** Insbesondere `filled_qty` getrennt von `qty` und der Status `PARTIALLY_FILLED` — das ist genau die Teilausführungs-Behandlung, die bei uns fehlt.

**3. Status QUEUED**
Wenn die Börse eine Stop-Order ablehnt, weil der Preis zu weit weg ist, wird sie nicht verworfen, sondern in `QUEUED` geparkt und später erneut eingereicht (`resubmit_order`). Die Strategie sieht die Order als „platziert".
→ **Übernehmen.** Verhindert, dass eine Absicherung stillschweigend fehlt.

**4. Börse ist die Wahrheit (Live)**
Im Live-Modus rechnet Jesse Kontostand, verfügbare Margin, Entry-Preis und Liquidationspreis **nicht** selbst. `FuturesExchange.update_from_stream()` und `position_service.update_from_stream()` übernehmen diese Werte aus dem Börsen-Stream. Eigene Berechnungen laufen nur in der Simulation. `charge_fee()` und `add_realized_pnl()` sind im Live-Modus bewusst leer.
→ **Übernehmen.** Das ist die technische Umsetzung von „Ledger auf echte Börsendaten".

**5. Schutz-Orders werden beim Öffnen der Position an die Börse gegeben**
`_on_open_position()` reicht Stop-Loss und Take-Profit sofort als `reduce_only`-Orders ein, sobald die Eröffnungsorder gefüllt ist. Liegt der Stop auf der falschen Seite des Entry-Preises (Markt hat sich bewegt), wird stattdessen sofort per Market geschlossen.
→ **Übernehmen.** Der Stop liegt bei der Börse, nicht nur im Bot. Absturz-sicher.

**6. Position-Modell mit Liquidations- und Bankrottpreis**
`Position.liquidation_price` und `bankruptcy_price` sind Kernfelder. In der Simulation werden sie aus Hebel und Entry berechnet (nur Isolated-Modus), im Live-Modus von der Börse übernommen. Der Backtest prüft pro Kerze, ob die Liquidation getroffen wurde, und schließt dann zum Bankrottpreis (`_check_for_liquidations`).
→ **Übernehmen.** Ohne Liquidations-Simulation kann der Backtest gehebelte Strategien nicht ehrlich bewerten.

**7. Ledger: ClosedTrade aus Orders abgeleitet**
Ein `ClosedTrade` speichert nicht Entry- und Exit-Preis als Zahlen, sondern die Liste der zugehörigen Orders. Entry-Preis, Exit-Preis, Menge, Fee und PnL sind **abgeleitete Eigenschaften** aus den tatsächlich gefüllten Orders (volumengewichtet). Fee = Summe der Order-Fees, die im Live-Modus von der Börse kommen.
→ **Übernehmen.** Der Trade lügt nicht, weil er nichts selbst speichert, was er aus den Orders berechnen kann. Für die Steuer: Jeder Trade ist bis auf die einzelne Börsen-Order rückverfolgbar.

**8. Filters als Veto-Schicht**
`Strategy.filters()` liefert eine Liste von Funktionen. Bevor `go_long()` / `go_short()` läuft, werden alle Filter geprüft; der erste, der `False` liefert, blockiert den Einstieg und wird geloggt. Filter können nichts auslösen, nur verhindern.
→ **Übernehmen.** Exakt das Muster für unseren Veto-Layer (On-Chain, Heatmaps, LLM).

**9. Signal-Signifikanz-Test**
Jesse hat einen `rule_significance_test`, der eine Strategie ohne Order-Ausführung durchspielt und prüft, ob die Einstiegs-Signale statistisch besser als Zufall sind.
→ **Als Idee übernehmen** für die Validierung der Regime-Engine (Schritt 8).

**10. Isolated statt Cross**
Jesse simuliert Liquidation nur im Isolated-Modus; Cross wird im Backtest als „nie liquidiert" behandelt. Das ist ehrlich: Cross-Liquidation hängt vom gesamten Konto ab und ist nicht seriös simulierbar.
→ **Bestätigt unsere Entscheidung:** Isolated Margin.

### A.2 Was Jesse schlechter macht als unser Plan — nicht übernehmen

**Keine Slippage-Modellierung.** Der Backtest füllt Market-Orders zum Kerzen-Close. Das Wort „slippage" kommt im Code nicht vor.
→ Wir modellieren Slippage separat (Abschnitt 3.3).

**Ein Fee-Satz pro Börse, kein Maker/Taker.** `fee: 0.0005` pauschal. Für Gate Futures sind das 0,05 % — das ist der Taker-Satz, Maker-Orders werden also zu teuer gerechnet.
→ Wir trennen Maker und Taker (Abschnitt 3.3).

**Kein Funding-Rate-Modell im Backtest.** `funding_rate` ist im Live-Modus ein Feld, im Backtest liefert es `0`.
→ Wir rechnen Funding im Backtest ein (Abschnitt 3.3), weil unsere Shorts auf Perpetuals laufen.

**Ein Konto, eine Strategie.** Jesse kennt keine Mandanten. `identifier: 'main'` mit TODO-Kommentar „multi account support".
→ Nicht übertragbar; unser Tenant-Modell bleibt.

**Kein Recovery-Prozess im Open-Source-Teil sichtbar.** Der Resync nach Neustart (`is_initial`, `open_trade`, `p_orders` in `update_from_stream`) deutet an, dass die Live-Treiber beim Start offene Positionen und Orders von der Börse laden — der Code dafür ist aber im geschlossenen Teil.
→ Wir bauen das selbst (Abschnitt 4).

### A.3 Konkrete Übernahmen in einer Tabelle

| Jesse-Muster | Wo es bei uns landet |
|---|---|
| Ein `execute_order`, Datenquelle per Modus | Phase 1, Abschnitt 2.1 |
| Order: `exchange_id`, `filled_qty`, `PARTIALLY_FILLED`, `reduce_only`, `submitted_via` | Phase 1, Abschnitt 2.2 |
| Status `QUEUED` + Resubmit | Phase 1, Abschnitt 2.2 |
| Live-Werte aus dem Stream, nie selbst gerechnet | Phase 1, Abschnitt 2.3 |
| Stop/TP als Börsen-Order beim Öffnen | Phase 4, Abschnitt 8.2 |
| Liquidations- und Bankrottpreis im Position-Modell | Phase 4, Abschnitt 8.1 |
| Liquidations-Check pro Kerze im Backtest | Phase 1, Abschnitt 3.4 |
| ClosedTrade aus Orders abgeleitet | Phase 2, Abschnitt 5 |
| `filters()` als Veto | Phase 3, Abschnitt 7.6 |
| Signifikanz-Test der Signale | Phase 3, Abschnitt 7.8 |

---

## Teil B — Das Konzept

### Warum vier Phasen und diese Reihenfolge

Der ursprüngliche Anlass war die Regime-Strategie. In der Diskussion hat sich aber gezeigt, dass drei Dinge darunter noch nicht stehen: Der Bot hat nie eine echte Order gegen die Börse abgesetzt, der Paper-Modus läuft auf einem eigenen Pfad, und die Kostenrechnung nutzt pauschal 1,5 % pro Trade — das 30-fache der echten Futures-Gebühr.

Solange das so ist, kann keine Strategie sinnvoll bewertet werden: Man weiß nicht, ob sie funktioniert oder ob nur die Rechnung falsch ist. Deshalb zuerst Kasse und Kassenbuch, dann Wetterdienst und Gegenrichtung.

| Phase | In einem Satz |
|---|---|
| 1 — Kasse | Der Bot kann echte Orders aufgeben, versteht die Antwort der Börse, rechnet Kosten korrekt, und Paper läuft auf demselben Code. |
| 2 — Kassenbuch | Jeder Trade ist mit allen Kosten vollständig und steuertauglich protokolliert, aus echten Börsendaten. |
| 3 — Wetterdienst | Der Bot erkennt das Marktregime und stellt sich auf Long- oder Short-Bias ein. |
| 4 — Gegenrichtung | Der Bot eröffnet eigenständig Shorts auf Futures, mit Hebel bis 2x, dynamisch nach Volatilität, mit Absicherung an der Börse. |

---

## Phase 1 — Kasse: Echte Ausführung, Paper-Modus, Kosten

### 1. Grundentscheidungen (fest)

- **Eine Börse: Gate.io.** Kein Multi-Exchange-Ziel. Kapital wird nicht zwischen Börsen verschoben. Shorts laufen auf Gate Futures, nicht auf einer anderen Börse.
- **Futures-Konto für Shorts.** USDT-Perpetuals, Isolated Margin. Spot-Logik der bestehenden Tenants bleibt unverändert.
- **Futures-Testnet nutzen.** Basis-URL `fx-api-testnet.gateio.ws`. Login auf `testnet.gate.com` mit den normalen Gate-Zugangsdaten; dort separate API-Keys anlegen. Es gibt keine eigene Registrierung — das war die bisherige Sackgasse.
- **Fee-Konfiguration pro Börse und Markt**, nicht als eine globale Zahl. Grund ist nicht Multi-Exchange, sondern dass Spot und Futures bei Gate unterschiedliche Sätze haben.

### 2. Order-Ausführung

#### 2.1 Ein Codepfad
Es gibt genau eine Funktion, die eine Order ausführt und alle Folgen auslöst (Positions-Update, Ledger, Schutz-Orders). Der einzige Unterschied zwischen Backtest, Paper und Live ist die **Quelle** von drei Werten:

| Wert | Backtest / Paper | Live |
|---|---|---|
| Gefüllte Menge | = bestellte Menge (bzw. simuliert) | aus Order-Response der Börse |
| Fee | aus Fee-Config berechnet | aus Order-Response der Börse |
| Kontostand / Margin | selbst geführt | aus Konto-Stream der Börse |

Alles, was nicht in dieser Tabelle steht, ist in allen Modi identischer Code. Das ist die Definition von „Paper-Modus sauber": Er testet den Live-Code, nicht einen Zwilling davon.

#### 2.2 Order-Modell
Jede Order hat mindestens:
- `id` (eigene) und `exchange_id` (von der Börse) — getrennt
- `qty` (bestellt) und `filled_qty` (tatsächlich) — getrennt; `remaining_qty` abgeleitet
- `status`: `ACTIVE`, `QUEUED`, `PARTIALLY_FILLED`, `EXECUTED`, `CANCELED`, `REJECTED`
- `reduce_only`: darf die Position nur verkleinern, nie umdrehen oder vergrößern
- `submitted_via`: `ENTRY`, `STOP_LOSS`, `TAKE_PROFIT`, `REGIME_FLIP`, `LIQUIDATION`, `RECOVERY`
- `fee` (echter Wert, im Live-Modus von der Börse), `fee_currency`
- `market`: `SPOT` oder `FUTURES`
- `tenant_id`
- `bot_submitted`: unterscheidet Bot-Orders von manuell platzierten
- `exists_on_exchange`: nach Recovery-Abgleich gesetzt

**Status QUEUED:** Lehnt die Börse eine Stop-Order ab, weil der Preis zu weit vom Markt entfernt ist, wird sie geparkt und bei jedem Tick erneut versucht, bis sie akzeptiert wird. Die Position gilt in dieser Zeit als **nicht abgesichert** — das wird geloggt und ist ein Alarm, kein Normalzustand.

#### 2.3 Response-Behandlung (Live)
Die Order-Response der Börse ist die einzige Wahrheit über das, was passiert ist. Zu behandeln:
- **Voll gefüllt:** `filled_qty = qty`, Fee übernehmen, Position aktualisieren.
- **Teilgefüllt:** `filled_qty < qty`, Status `PARTIALLY_FILLED`. Position um `filled_qty` aktualisieren. Rest bleibt aktiv oder wird gecancelt — konfigurierbar pro Order-Typ (Entry: Rest canceln; Stop-Loss: Rest bleibt).
- **Abgelehnt:** Status `REJECTED`, Grund loggen. Keine Positions-Änderung. Bei Entry: kein Retry ohne neue Signalprüfung. Bei Stop-Loss: sofort `QUEUED` und Alarm.
- **Timeout / keine Antwort:** Order-Status per REST nachfragen, **bevor** irgendetwas anderes passiert. Nie annehmen, dass sie nicht durchging.
- **Unbekannter Status:** Fail-Closed — Trading für diesen Tenant pausieren, Alarm.

Kein Pfad darf eine Exception schlucken. Jeder Fehler im Ausführungspfad ist ein Log-Eintrag mit Stacktrace und, bei allem außer Timeout-Retry, ein Trading-Stopp für den Tenant.

### 3. Kostenmodell

#### 3.1 Ist-Zustand
1,5 % pauschal pro Trade, als Slippage bezeichnet, tatsächlich Fee + Slippage vermischt. Gate Futures kostet real ~0,02 % Maker / ~0,05 % Taker, Spot ~0,2 %. Der Bot rechnet also mit dem 30-fachen (Futures) bzw. 7-fachen (Spot) der echten Kosten.

**Konsequenz:** Alle bisherigen Backtests sind zu pessimistisch. Strategien wurden möglicherweise verworfen, die real profitabel wären.

#### 3.2 Wie Gate die Fees abzieht (recherchiert)
- **Spot:** Fee wird von der gehandelten Coin-Menge abgezogen. Kaufe ich 1 ETH, bekomme ich 0,998 ETH. Der Paper-Modus muss das nachbilden, sonst stimmen die Bestände nicht.
- **Futures:** Fee wird von der Position Margin abgezogen, in USDT. Kontrakte, keine Coins.
- Fees fallen **zweimal** an: beim Öffnen und beim Schließen.
- **Funding** alle 8 Stunden auf Perpetuals, separat von der Fee.

#### 3.3 Neues Kostenmodell
Konfiguration pro Börse und Markt:

```
exchanges:
  gate:
    spot:
      maker_fee: 0.002
      taker_fee: 0.002
      fee_deducted_from: base_asset
      slippage_model: fixed_pct
      slippage_pct: 0.001
    futures:
      maker_fee: 0.0002
      taker_fee: 0.0005
      fee_deducted_from: margin
      slippage_model: fixed_pct
      slippage_pct: 0.001
      funding_interval_hours: 8
      funding_source: historical   # Backtest: echte Funding-Historie laden
```

Regeln:
- **Fee** hängt vom Order-Typ ab: Limit-Order, die im Buch wartet = Maker; Market-Order oder Limit, die sofort füllt = Taker. Im Live-Modus kommt die echte Fee von der Börse und überschreibt die Schätzung.
- **Slippage** nur auf Market-Orders. Startwert 0,1 %. Wird im Live-Modus als Differenz zwischen erwartetem und tatsächlichem Fill-Preis **gemessen** und ins Memory geschrieben, damit der Startwert später aus echten Daten kalibriert werden kann.
- **Funding** wird im Backtest aus historischen Funding-Rates berechnet und der Position als Kosten zugeschrieben. Im Live-Modus aus dem Konto-Stream.
- Die Startwerte oben sind ohne VIP-Stufe. Eine VIP-Stufe senkt die Fees; das wird dann als Config-Wert nachgezogen, nicht im Code.

#### 3.4 Backtests wiederholen
Nach Umstellung des Kostenmodells werden alle bestehenden Strategie-Backtests neu gerechnet. Ergebnis wird als Vergleich dokumentiert (alt vs. neu). Erst danach beginnt Phase 3 — es kann sein, dass eine bereits verworfene Variante die bessere Basis ist.

Zusätzlich: Der Backtest prüft ab sofort pro Kerze, ob eine Futures-Position den Liquidationspreis berührt hat, und schließt dann zum Bankrottpreis. Ohne das ist kein gehebelter Backtest ehrlich.

### 4. Recovery beim Start

Bei Spot lag der Coin nach einem Absturz einfach da. Bei Futures läuft eine gehebelte Position weiter. Deshalb eine feste Reihenfolge beim Hochfahren, **vor** jedem Trading:

1. **Börse abfragen:** Alle offenen Positionen und alle aktiven Orders für den Tenant von Gate laden (REST, nicht Stream — der Stream liefert nur Änderungen).
2. **Abgleich mit eigener Datenbank.** Bei jeder Abweichung gewinnt die Börse. Eigene Einträge, die die Börse nicht kennt, werden als `exists_on_exchange = false` markiert und geloggt. Positionen, die die Börse kennt und wir nicht, werden angelegt und als `submitted_via = RECOVERY` markiert.
3. **Schutz prüfen:** Für jede offene Futures-Position prüfen, ob ein Stop-Loss als aktive Order bei der Börse liegt. Fehlt er, **sofort** nachlegen, bevor irgendetwas anderes passiert.
4. **Regime-Zustand prüfen:** Ist der gespeicherte Bias älter als `regime_memory_ttl` (Default 12 h), wird er verworfen. Bis zur Neubestimmung gilt `flat`.
5. **Erst jetzt** darf der Tenant wieder traden.

Schlägt Schritt 1 fehl (Börse nicht erreichbar), startet der Tenant nicht. Kein Trading auf Basis der eigenen Datenbank allein.

---

## Phase 2 — Kassenbuch: Ledger aus echten Börsendaten

### 5. Ledger-Prinzipien

Der Bot hat bereits einen Ledger und eine PnL-Berechnung. Beides bleibt, aber die Datenquelle ändert sich.

**Trade wird aus Orders abgeleitet, nicht gespeichert.** Ein abgeschlossener Trade speichert die Liste seiner Orders (mit `exchange_id`). Entry-Preis, Exit-Preis, Menge, Fee und PnL sind volumengewichtet aus den tatsächlich gefüllten Orders berechnet. Was sich aus den Orders ergibt, wird nicht doppelt gespeichert — sonst kann es auseinanderlaufen.

**Pflichtfelder pro Trade** (für Steuer und Kontrolle):
- Tenant, Asset, Markt (Spot / Futures), Richtung (Long / Short)
- Eröffnet am, geschlossen am (Zeitstempel der tatsächlichen Fills, nicht der Signale), Haltedauer
- Hebel (bei Futures)
- Entry-Preis, Exit-Preis, Menge — abgeleitet
- Fee gesamt, getrennt nach Maker / Taker, mit Währung
- Funding gesamt (nur Futures)
- PnL brutto (vor Kosten), PnL netto (nach Fee und Funding)
- Liste der Börsen-Order-IDs
- Grund der Schließung: `TAKE_PROFIT`, `STOP_LOSS`, `REGIME_FLIP`, `MAX_HOLDING`, `LIQUIDATION`, `MANUAL`
- **Datenquelle:** `LIVE`, `PAPER`, `BACKTEST`. Paper- und Backtest-Trades dürfen nie in eine Steuerauswertung rutschen. Das Feld ist Pflicht und wird bei jeder Auswertung gefiltert.
- **Kostenmodell-Version:** Trades, die mit dem alten 1,5-%-Modell gerechnet wurden, tragen `cost_model = legacy_1_5pct`; neue tragen `cost_model = v3`. Alte und neue Zahlen sind nicht vergleichbar; das Feld verhindert, dass sie stillschweigend gemischt werden.

**Export:** CSV pro Tenant und Steuerjahr, alle Felder, nur `LIVE`-Trades. Das ist die Übergabe an den Steuerberater.

**Hinweis, kein Rat:** Futures und Spot werden in Deutschland steuerlich unterschiedlich behandelt (Termingeschäfte vs. private Veräußerung). Das Ledger muss beides sauber trennen; die steuerliche Bewertung ist Sache des Steuerberaters, nicht des Bots.

---

## Phase 3 — Wetterdienst: Regime-Engine

### 6. Architektur-Prinzip

- Feature-Flag pro Tenant: `regime_strategy_enabled`, Default `false`. Nur der Pilot-Tenant bekommt `true`.
- Keine Änderung an der globalen Logik. Neuer Codepfad, streng gekapselt.
- Alle Schwellen als Tenant-Config, nicht hartcodiert.
- Jeder Indikator einzeln abschaltbar. **Abgeschaltet zählt als „stimmt zu" (neutral), nicht als „fehlt"** — sonst kann man die Engine durch Abschalten in `flat` zwingen.
- Logging für jeden Flip mit Begründungs-Struktur: welcher Indikator hat was gesagt.
- **Fail-Closed:** Bei Fehler, fehlenden Daten, Sidecar-Ausfall oder widersprüchlichen Signalen liefert die Engine `flat`. Niemals den letzten Bias stillschweigend fortführen. Jeder Fallback wird geloggt.

### 7. Regime-Bestimmung

#### 7.0 Zeiteinheit und Bezugs-Asset
- Regime auf **Tageskerzen** (200-MA, ADX), Bestätigung auf **4h-Kerzen** (SuperTrend, MACD, Volumen).
- Live-Ticks aus dem Sidecar werden zu 4h-Kerzen aggregiert. **Keine Regime-Entscheidung auf Tick-Basis.**
- Regime auf **BTC als Markt-Proxy**, nicht pro Asset. Pro Asset nur ein Eintritts-Filter (eigener SuperTrend in Richtung des Markt-Regimes).
- Config: `regime_timeframe`, `regime_reference_asset`.

#### 7.1 Trendfilter (Pflicht)
- 200-Tage-Durchschnitt: Preis darüber → Long-Bias, darunter → Short-Bias.
- SuperTrend (ATR × 3) auf 4h als Ergänzung.

#### 7.2 Regime-Stärke (Pflicht)
- ADX auf Tageskerzen. Unter 20 → Seitwärts, kein Trading. Über 25 → Trend freigegeben. 20–25 → bestehender Bias bleibt, kein neuer Flip.

#### 7.3 Volumen-Bestätigung (Pflicht für Flip)
- Durchbruch nur bei Volumen über 20-Tage-Mittel. Sonst kein Flip.

#### 7.4 Momentum (Bestätigung)
- MACD unter Nulllinie → Short-Bestätigung, darüber → Long. RSI als Zusatz.

#### 7.5 On-Chain & Derivate (Kontext)
- MVRV Z-Score, Funding Rates. Extreme Funding = Überhebelung.

#### 7.6 Konfliktauflösung und Veto-Layer
Drei Klassen, feste Regel:

1. **Pflicht** (7.1, 7.2, 7.3): Alle müssen für einen Flip dieselbe Richtung zeigen. Fehlt eine → kein Flip. Nicht berechenbar → `flat`.
2. **Bestätigung** (7.4, 7.5): Score. Default: mindestens 1 von 2. Aktiver Widerspruch → Flip um eine Hysterese-Periode verzögert, nicht verhindert.
3. **Veto:** kann einen Flip oder eine Positions-Eröffnung nur **verzögern oder blockieren**, nie auslösen. Umgesetzt als Liste von Filter-Funktionen (Jesse-Muster): Die Entscheidung ist gefallen, dann laufen alle Filter; der erste, der blockiert, wird mit Namen und Begründung geloggt.

Veto-Quellen, alle standardmäßig aus, pro Tenant aktivierbar:
- **Whale-Tracking / Liquidations-Heatmaps.** Nur Bestätigung. Genauigkeit ~55–65 %, nie allein entscheiden lassen. Datenquelle vor Aktivierung verifizieren — die Santiment-Slugs im Bestand sind ungeprüft.
- **LLM-Veto.** Der Bot darf vor einer proaktiven Short-Eröffnung ein Prompt mit den bereits berechneten Indikatorwerten an eine KI-API senden. Die Antwort darf die Eröffnung **nur blockieren**, nie auslösen und nie die Größe erhöhen. Begründung wird geloggt. Warum kein Auslöser: nicht deterministisch (zwei Antworten auf dieselbe Lage), nicht backtestbar, kein Zugang zu Daten, die die Indikatoren nicht schon haben. Blockieren ist harmlos, Eröffnen nicht.

#### 7.7 Hysterese
- Flip braucht 2–3 Tage Stabilität (Default 3, konfigurierbar, gezählt in Tageskerzen).
- Zähler wird bei Gegen-Signal auf 0 gesetzt, nicht dekrementiert.
- Zu häufiges Flippen ist bei einem ungetesteten System der teurere Fehler; der Backtest entscheidet den Wert.

#### 7.8 Validierung der Signale
Vor dem Backtest der vollen Strategie: Signifikanz-Test der Regime-Signale ohne Order-Ausführung (Jesse-Muster). Frage: Ist die Richtung nach einem Flip statistisch besser als Zufall? Wenn nein, ist der Rest egal.

### 7.9 Partial-Exit beim Flip
Beim Flip wird jede Position gegen den neuen Bias geprüft. **Alles Gegenläufige wird geschlossen.** Das Toleranzband (Default 5 %) steuert nur Logging: Verlierer außerhalb des Bands werden als „Stop-Loss hätte greifen müssen" markiert — Alarm für die Stop-Logik, kein Halte-Grund.

Beispiel, Flip Long → Short bei drei Long-Positionen:
- ETH +4 % → geschlossen, Gewinn
- SOL −2 % → geschlossen, im Band
- DOGE −9 % → geschlossen, außerhalb Band, Alarm im Log

PnL für Futures-Positionen inklusive gezahltem Funding.

### 7.10 WebSocket-Sidecar & Memory
- Sidecar liefert Daten, nicht Entscheidungen.
- Ticks → 4h-Kerzen → Indikatoren. Sofortige Order-Auslösung beim Flip gilt nur für die **Ausführung**, nicht für die Signalbildung.
- Memory speichert: aktiver Bias, Zeitpunkt des letzten Flips, Hysterese-Zähler. TTL 12 h (Abschnitt 4, Schritt 4).
- Sanity-Check: Tick-Lücken oder unrealistische Sprünge → Fallback auf REST. Fällt auch REST aus → `flat`.
- **Memory als Datensammlung für spätere Kalibrierung** (nicht für Selbst-Justierung): gemessene Slippage pro Order, realisierter Stop-Abstand, tatsächliche Volatilität pro Klasse, minimaler Abstand zwischen Kurs und Liquidationspreis pro Position. Das Memory macht sichtbar, ob eine Klasse zu konservativ eingestellt ist. **Ändern tut ein Mensch — einmal, bewusst.** Ein System, das seine eigene Risikogrenze aus den eigenen Gewinnen nachjustiert, hat in einer Trendphase genau ein Signal: mehr Hebel. Und dann kommt der Regimewechsel.

---

## Phase 4 — Gegenrichtung: Proaktive Shorts auf Futures

### 8. Short-Infrastruktur

#### 8.1 Position-Modell Futures
Short ist **kein Spiegel von Long**. Eigenes Position-Modell mit:
- Kontraktgröße, Hebel, Entry-Preis (volumengewichtet)
- **Liquidationspreis** und **Bankrottpreis** — im Backtest berechnet (Isolated: `entry × (1 ± 1/Hebel ∓ Maintenance-Puffer)`), im Live-Modus von der Börse übernommen
- Verbrauchte Margin, unrealisierter PnL, kumuliertes Funding
- Mark-Preis (Live) getrennt vom letzten Handelspreis

„Kapital frei" = verfügbares Guthaben auf dem Futures-Konto minus reservierte Margin. Kapitaltransfer Spot → Futures ist eine eigene Funktion mit Limit (`max_futures_allocation_pct`, Default 30 % des Tenant-Kapitals).

#### 8.2 Absicherung statt DCA
Bei Spot verbilligt DCA den Einstand. Bei Futures gibt es **kein DCA** — eine Position pro Signal, und stattdessen Verlustbegrenzung:
- **Harter Stop-Loss pro Position**, als `reduce_only`-Order **bei der Börse**, eingereicht sofort nachdem die Eröffnungsorder gefüllt ist. Nicht nur in der Bot-Logik. Grund: Wenn der Bot abstürzt, ist die Position trotzdem abgesichert.
- **Stop muss immer enger als der Liquidationspreis liegen.** Mindestabstand: Liquidation doppelt so weit weg wie der Stop. Das ist eine harte Regel, die den maximalen Hebel begrenzt (8.3).
- Liegt der Stop beim Einreichen bereits auf der falschen Seite des Entry-Preises (Markt hat sich bewegt), wird die Position sofort per Market geschlossen statt einen sinnlosen Stop zu setzen.
- Optional: **Trailing-Stop**, zieht bei Gewinn nach. Config pro Tenant.
- Wird eine Stop-Order von der Börse abgelehnt (Preis zu weit), gilt sie als `QUEUED` und wird bei jedem Tick erneut versucht. Solange ist die Position unabgesichert — Alarm.

#### 8.3 Dynamischer Hebel
Der Bot setzt den Hebel automatisch pro Position, ohne manuellen Eingriff. Grundlage ist die **bestehende Volatilitätsklassifizierung** des Bots — keine zweite, parallele.

| Volatilitätsklasse (bestehend) | Hebel | Zusatzbedingung |
|---|---|---|
| hoch | 1x | — |
| mittel | 1,5x | ADX > 25 |
| niedrig | 2x | ADX > 25 |

Regeln:
- **Absolute Obergrenze 2x.** Config-Wert `max_leverage = 2`, den der Bot lesen, aber nicht verändern kann. Alles darüber ist ausgeschlossen, egal was die Signale sagen.
- Bei ADX ≤ 25 gilt immer 1x, unabhängig von der Klasse.
- Der Hebel wird zusätzlich durch 8.2 begrenzt: Ergibt die Stop-Rechnung, dass die Liquidation bei 2x zu nah am Stop läge, wird der Hebel reduziert, nicht der Stop geweitet.
- Hebel hängt **nur an Volatilität und Trendqualität**, nicht an Signalstärke. Sonst wird aus „ich bin sicher" automatisch mehr Risiko.
- Effektiv ist das Positionsgrößen-Logik: Risiko pro Position = Abstand zum Stop × Größe. Der Hebel bestimmt darüber hinaus nur den Liquidationspreis — deshalb bleibt er niedrig.

#### 8.4 Take-Profit und Haltedauer
- Take-Profit in Prozent wird aus der Spot-Logik übernommen (pro Klasse und pro Coin), **aber durch den Hebel geteilt**: Bei 2x bewegt sich der Kurs nur halb so weit, bis dieselbe Rendite auf die Margin erreicht ist. Spot-Prozentwerte direkt zu übernehmen hieße, bei 2x doppelt so früh Gewinn zu nehmen.
- **Maximale Haltedauer härter als bei Spot** und **ohne die Überschreibung bei guter Performance**. Bei Spot kostet Halten nichts, bei Futures läuft Funding weiter. Shorts sollen so kurz wie möglich sein — Ziel ist die Gegenbewegung nach einem Ausstieg, nicht eine Wochen-Position. Default `max_short_holding_hours = 72`, Config pro Tenant.
- Da Shorts kurz gehalten werden, ist Funding klein. Das erlaubt Longs und Shorts im selben Futures-Konto (ein Kapitalmodell, keine Transfers) — offen, siehe Abschnitt 11.

#### 8.5 Zwei Wege zur Short-Eröffnung
- **Reaktiv (Bestand, bleibt exakt erhalten):** Ein Spot-Verkauf mit Ausstiegs-Signal löst einen Short auf denselben Coin aus. Das Verkaufssignal ist der Rückhalt.
- **Proaktiv (neu, nur Pilot-Tenant):** Im Short-Regime darf der Bot Shorts direkt aus dem Regime-Signal eröffnen, ohne vorherigen Verkauf. Einzige Voraussetzungen: Regime = Short, Kapital auf dem Futures-Konto frei, kein Veto.
- **Proaktive Shorts haben eigene Sizing-Regeln**, kleiner als reaktive: Sie haben kein Verkaufssignal als zweite Bestätigung, nur das Regime. Default: 50 % der Größe, die ein reaktiver Short auf denselben Coin bekäme. Config `proactive_short_size_factor`.
- **Spot-Futures-Brücke:** Der reaktive Fall verknüpft eine Spot-Verkaufs-Order mit einer Futures-Eröffnungs-Order zu einem logischen Vorgang. Das Ledger speichert beide mit einem gemeinsamen `linked_trade_id`, damit „Verkauf plus Short" als eine Entscheidung auswertbar ist. Zwei Konten, zwei Positions-Modelle, eine Begründung.
- **Exposure-Limit:** `max_short_exposure_pct`, Default 30 % des Tenant-Kapitals, über beide Wege zusammen.

---

## 9. Implementierungsschritte

| # | Schritt | Phase | Abhängig von |
|---|---|---|---|
| 0 | Futures-Testnet-Zugang: Login mit Gate-Account auf testnet.gate.com, API-Keys, Host `fx-api-testnet.gateio.ws` in Config | 1 | — |
| 1 | Codebase analysieren: Tenant-Modell, bestehende Indikatoren, Volatilitätsklassifizierung, Order-Pfad, Paper-Pfad, Ledger, Stellen mit stillem Exception-Schlucken | 1 | — |
| 2 | Order-Modell erweitern (2.2), Ausführung auf einen Codepfad zusammenführen (2.1), Response-Behandlung (2.3) | 1 | 1 |
| 3 | Kostenmodell (3.3): Fee-Config pro Börse/Markt, Maker/Taker, Slippage separat, Funding-Historie laden | 1 | 1 |
| 4 | Liquidations-Check im Backtest (3.4) | 1 | 3 |
| 5 | **Alle bestehenden Backtests wiederholen, Vergleich dokumentieren** | 1 | 3, 4 |
| 6 | Recovery beim Start (4) | 1 | 2 |
| 7 | Integrationstest gegen Futures-Testnet: Market/Limit/Stop, Teilausführung, Ablehnung, Timeout, Absturz mitten im Trade + Recovery | 1 | 0, 2, 6 |
| 8 | Ledger umstellen (5): Trade aus Orders ableiten, Pflichtfelder, Datenquelle, Kostenmodell-Version, CSV-Export | 2 | 2, 3 |
| 9 | Feature-Flag `regime_strategy_enabled`, Migration, nur Pilot-Tenant | 3 | — |
| 10 | Regime-Engine (7.0–7.7) als gekapseltes Modul; Konfliktauflösung als eigene, isoliert testbare Funktion | 3 | 9 |
| 11 | Signifikanz-Test der Regime-Signale (7.8) | 3 | 10 |
| 12 | Partial-Exit (7.9) | 3 | 10 |
| 13 | Sidecar-Aggregation, Memory mit TTL, Sanity-Check (7.10) | 3 | 10 |
| 14 | Veto-Layer inkl. LLM-Veto (7.6), Datenquellen vorher verifizieren | 3 | 10 |
| 15 | Futures-Position-Modell (8.1), Kapitaltransfer mit Limit | 4 | 2, 6 |
| 16 | Stop-Loss als Börsen-Order, QUEUED-Handling, Trailing (8.2) | 4 | 15 |
| 17 | Dynamischer Hebel an bestehende Volatilitätsklassen (8.3) | 4 | 15, 16 |
| 18 | Take-Profit / Haltedauer für Futures (8.4) | 4 | 15 |
| 19 | Proaktive Short-Eröffnung, Sizing, Spot-Futures-Brücke, Exposure-Limit (8.5) | 4 | 10, 15–18 |
| 20 | Unit-Tests: Konfliktauflösung, alle Fail-Closed-Pfade, Hebel-Grenze, Stop-vs-Liquidation | 3–4 | alle |
| 21 | Backtest Pilot-Tenant mit neuem Kostenmodell, Funding-Historie, Liquidations-Check | 3–4 | 5, 10–19 |
| 22 | Paper-Trading Pilot-Tenant gegen Testnet | 3–4 | 21 |
| 23 | Live mit kleinem Kapital, 2x-Freigabe erst nach Testnet-Nachweis der Stop-Logik | 4 | 22 |
| 24 | Ausweitung auf weitere Tenants erst wenn Metriken stimmen | — | 23 |

**Freigabe-Bedingung für Schritt 23:** Die bekannten kritischen Punkte aus dem Staging-Review (fail-open-Pfade, stilles Exception-Schlucken in `risk_manager` und `decision_engine`) sind geschlossen. Die Regime-Strategie baut auf beiden auf; ist deren Review nicht fertig, ist der Pilot nicht freigabefähig.

**Backtest-Fallen (Schritt 21):**
- Look-ahead-Bias bei On-Chain-Daten (MVRV wird nachträglich revidiert): nur Point-in-Time-Daten oder 7.5 im Backtest deaktivieren.
- Funding-Historie einrechnen, sonst sind Shorts systematisch zu profitabel.
- Hysterese exakt wie live zählen.
- Liquidations-Check aktiv.

---

## 10. Constraints (alle Phasen)

- Keine Änderung an bestehenden Tenants.
- Eine Börse. Kein Kapital-Shuffling zwischen Börsen.
- Ein Codepfad für Backtest, Paper, Live; Unterschied nur in der Datenquelle.
- Börse ist die Wahrheit. Live-Werte werden nie selbst gerechnet.
- Fail-Closed: Fehler oder Datenlücke → `flat` bzw. Trading-Stopp für den Tenant. Kein Pfad schluckt Exceptions.
- Kein Trading vor abgeschlossenem Recovery.
- Jeder Indikator einzeln abschaltbar; abgeschaltet = neutral.
- Alle Schwellen als Tenant-Config.
- Keine Regime-Entscheidung auf Tick-Basis.
- Kein DCA auf Futures. Eine Position pro Signal.
- Stop-Loss liegt bei der Börse, immer enger als der Liquidationspreis.
- Hebel maximal 2x, nur an Volatilität und ADX gekoppelt, Grenze vom Bot nicht veränderbar.
- Kein Short ohne Exposure-Limit.
- LLM und On-Chain-Daten nur als Veto.
- Memory sammelt, ein Mensch justiert.
- Logging für jeden Flip, jede Ablehnung, jeden Fallback, jeden Veto-Treffer, mit Begründung.

---

## 11. Offene Entscheidungen

Getroffen in der Diskussion:
- Futures, Isolated, Hebel dynamisch 1x–2x, Obergrenze 2x ✔
- Kein DCA auf Futures, Absicherung über Börsen-Stop ✔
- LLM nur als Veto ✔
- Eine Börse ✔
- Fee-Config pro Börse und Markt ✔
- Shorts kurz halten, Take-Profit durch Hebel geteilt ✔
- Toleranzband 5 % als Startwert ✔
- Hysterese 3 Tage als Startwert für den Backtest ✔

Noch offen:
1. **Longs und Shorts im selben Futures-Konto?** Vorteil: ein Kapitalmodell, keine Transfers. Nachteil: Funding auch auf Longs; Spot-Longs der Bestandslogik müssten unangetastet bleiben. Vorschlag: Pilot-Tenant komplett auf Futures, Bestands-Tenants bleiben Spot. Entscheidung nach Schritt 5 (wenn klar ist, was die korrigierten Kosten mit den Long-Ergebnissen machen).
2. **Slippage-Startwert 0,1 %** — wird aus gemessenen Live-Daten kalibriert. Bis dahin Annahme.
3. **`max_short_holding_hours` 72** — Startwert, Backtest entscheidet.
4. **Testnet-Zugang tatsächlich prüfen** (Schritt 0). Falls es wider Erwarten doch nicht geht, fällt das stärkste Argument für Futures — dann wäre Cross-Margin auf Spot die Alternative, mit deutlich schlechterer Testbarkeit.
