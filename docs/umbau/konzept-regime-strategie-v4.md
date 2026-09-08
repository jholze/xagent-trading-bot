# Konzeptpapier v4: Abgleich mit der Codebasis und Erweiterung auf sieben Phasen

**Stand:** 4. September 2026
**Basis:** v3 (Jesse-Analyse, vier Phasen, 24 Schritte) + Sprachsitzung vom 4.9. (Kontext-Layer, Protokollierung, Musterdatenbank, autonome Weiterentwicklung)
**Abgeglichen gegen:** `jholze/xagent-trading-bot`, Branch `staging`, Commit vom 3.9.2026 21:19 (PR #297), 838 Python-Dateien, 174.000 Zeilen (davon 50.000 Tests)

Dieses Papier ersetzt v3 nicht, sondern legt sich darüber: Für jeden Schritt steht jetzt, was im Code **schon existiert**, was **umgebaut** werden muss und was **neu** ist. Die Nummerierung der Abschnitte 1–11 aus v3 bleibt gültig und wird hier referenziert.

---

## Teil A — Was der Code-Abgleich ergeben hat

Die Sprach- und Konzeptarbeit hat auf einem Bild des Bots aufgesetzt, das an mehreren Stellen nicht mehr stimmt. Der Bot ist weiter, als das Konzept angenommen hat — und an anderen Stellen anders kaputt, als angenommen.

### A.1 Das Konzept hat unterschätzt, was schon da ist

| Konzept-Baustein | Existiert bereits als | Zustand |
|---|---|---|
| Regime-Engine (Phase 3, 7.x) | **Zwei** Module: `services/market_oracle/regime.py` (Makro-Zustandsmaschine BTC/ETH: `RISK_ON / NEUTRAL / RISK_OFF / CRASH`, Funding, Breadth, Hysterese mit `min_bars_to_flip=2`) und `intelligence/regime_detector.py` (pro Coin: ADX, EMA-200-Steigung, Sentiment-Fusion; Zustände `RANGING / STRONG_UPTREND / STRONG_DOWNTREND / CHOPPY_HIGH_VOL / TRANSITION`) | Beide produktiv verdrahtet: `strategies/decision_engine.py`, `strategies/registry.py`, `services/market_policy_fusion.py`. Das Marktorakel steuert Positionsgröße und Anzahl (`size_mult`, `block_new_entries`, `max_new_buys_per_hour`) — genau das, was Jennsen im Gespräch als „Marktorakel“ beschrieben hat. |
| Futures-Position-Modell (8.1) | `strategies/short_math.py`: Isolated-Liquidationspreis, Liquidations-Puffer, Stop-Preis aus Margin, Funding-Kosten (Paper), Hebel-Clamp, `snapshot()` | Vollständig, aber **nur Paper**. Live ist per `shorts.allow_live=false` hart gesperrt („no Gate futures in v0“). |
| Short-Policy (8.3, 8.4) | `strategies/short_policy.py`: Hebel-Default 2, Cap, Zeit-Cap 4 h (volatile) / 8 h (stable), Trailing, RSI-Cover, `max_margin_pct 20`, `max_open 6`, Coin-Overrides | Vollständig. Weicht in einem Punkt hart vom Konzept ab (siehe A.3). |
| Volatilitätsklassifizierung | `intelligence/volatility_classifier.py`, Tiers `volatile / mid / stable` mit `frozen_tier` in `registry.py` | Produktiv. Basis für den dynamischen Hebel (8.3). |
| Signifikanz-Test (7.8) | `hermes/` — Experimente ändern **einen** Parameter, Backtest beider Varianten, Promotion nur bei klarer Verbesserung | Existiert, aber gegen ein falsches Kostenmodell (A.2). |
| Memory / Sidecar (7.10) | WebSocket-Hubs: `services/dca_sniper/ws_watch.py`, `services/gainer_signal/ws_loop.py`, `services/exit_realtime/hub.py`; Vektor-Memory: `intelligence/memory/` (Weaviate) + `hermes/memory/rag_retriever.py` | Infrastruktur da. Kein TTL-Konzept sichtbar. |
| Backtest-Engine | `hermes/backtester.py`, `hermes/pipeline_backtest.py`, `intelligence/strategy_backtest.py`, `hermes/replay_engine.py`, Rust-Skelett unter `backtest/rust/` mit Paritäts-Harness | Mehrere Engines, kein gemeinsames Kostenmodell. |
| Autonome Weiterentwicklung (Sprachsitzung, „Phase 7“) | **Hermes ist genau das.** `hermes/self_improver.py` lässt Grok Parameter vorschlagen, `experiment.py` testet, `live_evidence.py` liefert Live-Metriken als Promotions-Veto, Ergebnis geht nach `hermes/memory/baseline.json` und wird **sofort vom Live-Bot gelesen** | Existiert. Das Konzept muss es nicht erfinden, sondern einhegen. |
| Mandantenmodell | `core/tenant_context.py` (ContextVar), `storage/tenant_registry.py`, `tenant_keys.py`, `tenant_routing.py` | Phase 0 laut Docstring. Vorhanden. |
| Ledger | `services/order_service.py` (1.271 Zeilen), `storage/order_ledger_v2.py`, `storage/mongo_ledger.py`, `storage/ledger_router.py` mit Scopes `demo / paper / live`, Idempotency-Keys, Dual-Write-Schalter | Mongo als Ziel, JSON-Dateien im Router weiterhin erste Wahl. Migration halb fertig. |
| Externe Daten | LunarCrush, CMC, X, Santiment (`services/dca_sniper/santiment_enrich.py`), RSS-Feeds, Grok-Suche | Mehr Quellen als im Konzept angenommen. Kein gemeinsamer Adapter, kein einheitliches Fallback-Verhalten. |

**Konsequenz:** Phase 3 und Phase 4 sind zu großen Teilen **Umbau statt Neubau**. Die Schritte 10, 15, 17, 18 aus v3 schrumpfen. Dafür kommt ein Schritt hinzu, der im Konzept fehlte: **die zwei Regime-Module zusammenführen oder klar trennen** (Makro-Orakel vs. Coin-Regime).

### A.2 Das Kostenmodell ist doppelt falsch — in zwei Richtungen

v3 ging von „1,5 % pauschal pro Trade“ aus. Der Befund ist präziser und schlimmer:

| Pfad | Was gerechnet wird | Wirkung |
|---|---|---|
| **Backtests** (`hermes/backtester.py`, `pipeline_backtest.py`, `intelligence/strategy_backtest.py`) | `slippage_percent = 1.5` **auf Kauf und Verkauf** → 3 % Round-Trip. Keine Fee. | ~15-fach zu **pessimistisch** gegenüber Gate Spot (~0,2 % Round-Trip + reale Slippage). Strategien wurden zu Unrecht verworfen, Hermes-Promotionen basieren auf verzerrten Vergleichen. |
| **Paper-PnL** (`services/portfolio_service.py`) | `pnl = (price - entry) * amount`. **Keine** Fee, **keine** Slippage. Slippage 1,5 % geht nur in `received` (virtuelles Guthaben) beim Verkauf, nicht beim Kauf, nicht in den PnL. | PnL zu **optimistisch**, Guthaben inkonsistent zum PnL. Zwei Kennzahlen, die sich widersprechen. |
| **Live** (`execution/gate_adapter.py`) | Fee wird aus der ccxt-Antwort gelesen (`_extract_fee`) und ins `record_live_trade` geschrieben — aber der PnL kommt aus `portfolio.execute_sell`, der die Fee **nicht kennt**. | Live-PnL im Ledger ist brutto, Fee steht daneben, wird nirgends verrechnet. |
| **Shorts** (Paper) | `fee_rate 0.001`, `funding_rate_8h 0.0001` als Konstanten. | Immerhin vorhanden, aber nicht aus Börsendaten. |
| **Grid / Entry-Recipe** | `assumed_fee_pct 0.1` | Ein vierter Wert für dieselbe Größe. |

Fünf Stellen, vier verschiedene Annahmen, keine davon aus der Börse. Abschnitt 3.3 aus v3 (Kostenmodell pro Börse und Markt, Maker/Taker getrennt, Slippage separat, Funding aus Historie) bleibt exakt richtig — und **Schritt 5 (alle Backtests wiederholen) ist dringlicher als gedacht**, weil Hermes seit Monaten gegen 3 % Round-Trip optimiert.

### A.3 Konflikte zwischen Konzept und Code

| Thema | Konzept (v3) | Code | Entscheidung |
|---|---|---|---|
| Hebel-Obergrenze | **2x**, dynamisch 1x / 1,5x / 2x | `config.shorts.leverage_cap: 5`, `clamp_leverage(cap=5.0)` hartcodiert als Default | Konzept gilt. Cap auf 2 setzen, Default in `short_math.py` auf 2 ändern, Test dafür schreiben. |
| Fail-Closed | Constraint für alles, was Geld bewegt | `market_oracle_risk_fail_open: true`, `santiment_risk_fail_open: true`, Regime-Docstring „Breadth/funding optional: if missing, fail-open“, `risk_manager` mit vier dokumentierten Fail-Open-Pfaden (Cash-Bias, Slot-Memory, Capacity). 97 Vorkommen von `fail_open` im Code. | Fail-Open bleibt für **Kontext-Indikatoren** akzeptabel (fehlende Breadth → Preisregeln allein). Für **Sizing, Hebel, Stop, Short-Eröffnung** wird es Fail-Closed. Die Trennlinie muss pro Schalter dokumentiert werden. |
| Exception-Behandlung | „stilles Schlucken schließen“ | **370** `except Exception: pass` außerhalb der Tests, 3 nackte `except:` | Nicht alle sind kritisch (Telegram, Logging). Aber jede Stelle in `execution/`, `risk/`, `services/order_service.py`, `services/portfolio_service.py`, `strategies/positions.py` muss einzeln bewertet werden. Das ist Schritt 1 und kein Nebensatz. |
| Short-Haltedauer | `max_short_holding_hours = 72` als Startwert | `time_cap_hours` 4 (volatile) / 8 (stable) | Code ist **strenger** als Konzept. Konzeptwert war aus der Spot-Logik abgeleitet; Codewert ist für reaktive Kurz-Shorts gedacht. Für proaktive Regime-Shorts ist 4–8 h zu kurz. → Zwei getrennte Parameter: `time_cap_hours` (reaktiv, bleibt) und `regime_short_max_hours` (proaktiv, Startwert 72, Backtest entscheidet). |
| Reaktive Short-Quellen | „nur als Folge eines Verkaufs“ | `shorts.auto_sources`: `rsi_sell`, `exit_1h_rsi_rollover`, `oracle_climax_harvest`, `exit_volume_climax` | Stimmt mit Konzept überein. Das Doppelverlust-Risiko aus v3 (Exit und Short auf demselben Signal) ist damit **konkret**: alle vier Quellen sind Exit-Signale. |
| Regime-Zeiteinheit | Tages-Hysterese 3 Tage | Orakel: `min_bars_to_flip=2` auf Zyklus-Basis (Minuten), Detector: pro Kerze | Orakel und Konzept meinen verschiedene Dinge. Orakel = kurzfristiger Risikoschalter. Konzept-Regime = mehrtägiger Bias. Beide behalten, sauber benennen. |

### A.4 Was das Konzept richtig vorausgesehen hat — und was fehlt

Bestätigt (Phase 1 ist genau so nötig wie beschrieben):

- **Nur Market-Orders**, via ccxt `create_market_buy_order / create_market_sell_order`. Keine Limit-, keine Stop-Orders bei der Börse. **Kein `reduce_only`, kein `stopPrice`** im gesamten Code.
- **Keine Teilfüllungs-Logik.** `filled = raw.get("filled") or amount` — fehlt das Feld, gilt die Order als voll gefüllt. `fill_price` fällt auf den Wunschpreis zurück.
- **Kein Timeout-, kein Ablehnungs-Handling** über `except Exception → executed=False` hinaus.
- **Keine Recovery gegen die Börse.** `reconcile_*` im Code gleicht nur Ledger-Dateien untereinander ab. `fetch_open_orders`, `fetch_positions`, `fetch_my_trades` kommen nicht vor.
- **Zwei Codepfade**: `PaperExecutionAdapter` (14 Zeilen, ruft `portfolio.execute_order`) und `GateExecutionAdapter` (311 Zeilen, eigene Buy/Sell-Logik, danach `_sync_local_ledger` in dasselbe Portfolio). Der Paper-Pfad hat nie eine Börsenantwort gesehen.
- **Dry-Run** existiert (`live.dry_run: true`) und schreibt `exchange_order_id="dry_run"` — ein dritter Quasi-Modus zwischen Paper und Live.
- **Order-Statusmodell** kennt praktisch nur `filled` / `rejected`. Kein `partially_filled`, kein `queued`, kein `canceled`.

Fehlt im Konzept, im Code aber relevant:

- **Drei Ledger-Scopes** (`demo / paper / live`) statt zwei. „Demo“ ist der historische Paper-Modus mit JSON, „paper“ der neuere. Phase 2 muss die Scope-Semantik festlegen, bevor irgendwas migriert wird.
- **Mongo-Migration ist halb fertig.** `ledger_router.py` kennt `mongo` als Backend und `ledger_dual_write_enabled`, aber die Datei-Namen (`orders.live.json`, `positions.live.json`) sind weiterhin die Primärreferenz in Doku und Code. Phase 2 muss das abschließen, nicht umgehen.
- **Hermes schreibt live wirksame Parameter** ohne dass das Konzept es erwähnt. Jede Änderung am Kostenmodell (Phase 1) invalidiert `baseline.json`. Das gehört in Schritt 5.

---

## Teil B — Sieben Phasen

Die vier Phasen aus v3 bleiben. Drei kommen dazu, alle aus der Sprachsitzung vom 4.9.:

| Phase | Kurzname | Ergebnis | Art |
|---|---|---|---|
| 1 | Kasse | Echte Orders, korrekte Antworten, korrekte Kosten, ein Codepfad | Bauarbeit |
| 2 | Kassenbuch | Vollständiges, steuertaugliches Ledger aus Börsendaten — **plus Kontext-Achsen als Zusatzfelder** | Bauarbeit |
| 3 | Wetterdienst | Regime-Bias auf Basis der bestehenden zwei Module, zusammengeführt | Umbau |
| 4 | Gegenrichtung | Proaktive Shorts auf Futures, Hebel ≤ 2x, Börsen-Stop | Umbau + Neubau |
| 5 | Kontext-Layer | Externe Datenquellen mit Adapter-Abstraktion, Dune ersetzt Santiment, LLM-Veto mit Werkzeugen | Erweiterung |
| 6 | Musterdatenbank | Kontext-Achsen auswerten — live gesammelt und historisch nachgetragen — Regeln daraus explizit programmieren | Forschung |
| 7 | Autonome Weiterentwicklung | Hermes einhegen: Bänder, Journal, Kostenmodell, Live-Veto | Einhegung von Bestehendem |

Nichts aus Phase 5–7 ändert die Reihenfolge von Phase 1–4. Solange das Kostenmodell 15-fach daneben liegt, optimiert Hermes auf Rauschen und jede Musterauswertung findet Zusammenhänge in einer verzerrten Zahl.

---

## Phase 1 — Kasse (Abgleich zu v3 Abschnitt 1–4)

### Ist-Zustand, präzisiert

| v3-Abschnitt | Ist im Code | Delta |
|---|---|---|
| 2.1 Ein Codepfad | Zwei Adapter, `PaperExecutionAdapter` ohne Börsenkontakt, `GateExecutionAdapter` mit eigener Logik, `dry_run` als drittes Verhalten | Adapter-Basis bleibt (`execution/base.py`), aber Paper muss durch **denselben** Gate-Adapter mit Testnet-Host laufen. `dry_run` wird abgeschafft oder zu „Testnet ohne Ausführung“. |
| 2.2 Order-Modell | `core/models.py: TradeOrder, TradeResult` — flache Dataclasses. Ledger-Order in `order_service` hat `request / risk / execution / timestamps`, Status `filled / rejected` | `qty` und `filled_qty` trennen, `remaining_qty`, Status-Enum `ACTIVE / QUEUED / PARTIALLY_FILLED / EXECUTED / CANCELED`, `reduce_only`, `exchange_order_id` als Pflichtfeld bei Live, `order_exist_in_exchange` |
| 2.3 Response-Behandlung | `filled or amount`, `average or price or order.price`, Exception → `executed=False` | Teilfüllung, Ablehnung mit Code, Timeout mit anschließendem `fetch_order`, Idempotenz gegen Doppelsendung (Idempotency-Key existiert schon im Ledger — an die Börsen-`clientOrderId` koppeln) |
| 3 Kostenmodell | Fünf Stellen, vier Werte (A.2) | Ein Modul `core/costs.py`, gelesen von Backtest, Paper, Live, Short-Math, Grid. Konfiguration pro `exchange × market` wie in v3 3.3. |
| 4 Recovery | Nur Ledger-intern | Komplett neu: Börse abfragen (Balances, offene Orders, Positionen bei Futures), gegen Ledger abgleichen, **Börse gewinnt**, fehlende Stops nachlegen, erst dann Zyklus starten. Ohne Börse kein Tenant-Start. |

### Zusätzliche Schritte für Phase 1

- **1a. Exception-Audit** (ersetzt den Nebensatz in Schritt 1): Alle `except Exception` in `execution/`, `risk/`, `services/order_service.py`, `services/portfolio_service.py`, `strategies/positions.py`, `data_manager.py` listen. Für jede Stelle: (a) bleibt, weil nicht geldrelevant, (b) wird zu Log + Weitergabe, (c) wird zu Fail-Closed. Ergebnis als Tabelle ins Repo.
- **1b. Fail-Open-Inventar**: Alle 97 `fail_open`-Stellen klassifizieren. Kontext-Indikatoren dürfen fail-open bleiben. Alles, was `size_mult`, Hebel, Stop-Abstand, Short-Freigabe oder Kapital berührt, wird fail-closed. Config-Defaults `market_oracle_risk_fail_open` und `santiment_risk_fail_open` werden nicht global umgestellt, sondern die **Verbraucher** entscheiden.
- **5a. Hermes-Baseline invalidieren**: Nach Kostenmodell-Umstellung `hermes/memory/baseline.json` einfrieren, alle Experimente neu laufen lassen, Promotionen aus der 3 %-Ära markieren.

---

## Phase 2 — Kassenbuch (Abgleich zu v3 Abschnitt 5) + Kontext-Protokollierung

### Ist-Zustand

- Ledger-Struktur ist gut: `order_service` hält pro Order `request / risk / execution`, Idempotency-Key, Quelle, Signal. Das ist näher an Jesse's „Trade aus Orders ableiten“ als angenommen.
- **Drei Scopes** statt zwei. Mongo als Backend halb eingeführt.
- Fee-Feld existiert im Live-Pfad, ist aber `null` in allen Demo-/Paper-Orders und wird nirgends in den PnL gerechnet.
- Kein Feld für Funding, Maker/Taker, Kostenmodell-Version, Schließungsgrund als Enum (Signal-String ist frei).

### Was bleibt aus v3

Alle Pflichtfelder aus Abschnitt 5, der CSV-Export pro Tenant und Steuerjahr, `data_source LIVE / PAPER / BACKTEST`, `cost_model_version`. Zusätzlich: **Scope-Semantik festlegen** (`demo` wird zu `paper` migriert oder als historisch eingefroren) und **Mongo-Migration abschließen** — kein Dual-Write dauerhaft.

### Neu: Kontext-Achsen (aus der Sprachsitzung)

Jeder Trade bekommt Zusatzfelder, die **keine Wirkung auf die Entscheidung** haben. Sie werden nach dem Trade befüllt, asynchron über Redis, und blockieren die Ausführung nie.

**In Mongo, am Trade, als Zahlen (grobe Achsen mit wenigen Stufen):**

| Feld | Stufen | Quelle | Verfügbar seit |
|---|---|---|---|
| `ctx_news_risk` | 0–3 | LLM-Einstufung der Nachrichtenlage (Phase 5) | Phase 5 |
| `ctx_macro_event_24h` | 0/1 + Typ | Kalender (Fed, CPI, …) | Phase 2 (statisch pflegbar) |
| `ctx_polymarket_prob` | 0–1 oder null | Polymarket-API, nur wenn ein passendes Ereignis existiert | Phase 5 |
| `ctx_fear_greed` | 0–100 | CMC/Alternative.me | Phase 2 (CMC ist schon angebunden) |
| `ctx_btc_dominance` | Prozent | CMC | Phase 2 |
| `ctx_oracle_state` | `RISK_ON / NEUTRAL / RISK_OFF / CRASH` | **existiert schon** im Marktorakel | Phase 2 — sofort |
| `ctx_coin_regime` | die fünf Detector-Zustände | **existiert schon** im RegimeDetector | Phase 2 — sofort |
| `ctx_volume_rel` | Verhältnis zum 30-Tage-Mittel | eigene Kerzen | Phase 2 — sofort |
| `ctx_whale_flow` | −1 / 0 / +1 | Dune (Phase 5), vorher Santiment falls Slug verifiziert | Phase 5 |
| `ctx_captured_at` | Zeitstempel **getrennt** vom Trade-Zeitstempel | — | Pflicht |
| `ctx_origin` | `live` / `backfilled` | — | Pflicht |

Drei Achsen (`oracle_state`, `coin_regime`, `volume_rel`) kann der Bot **heute** mitschreiben, weil die Werte im Zyklus schon berechnet werden. Damit beginnt die Datensammlung in Phase 2, nicht erst in Phase 5.

**In Weaviate (existiert: `intelligence/memory/vector_weaviate.py`):** Nachrichtenlage als Text mit Einbettung, `trade_id`, `captured_at`. Zweck: Ähnlichkeitssuche „wann war die Lage schon mal so“ — das umgeht das Kombinatorik-Problem, das exakte Musterabfragen haben.

**Regel:** Kein Kontext-Feld ist Eingang in `risk_manager`, `decision_engine` oder `registry`. Wer ein Muster in eine Regel verwandeln will, geht über Phase 6.

---

## Phase 3 — Wetterdienst (Abgleich zu v3 Abschnitt 6–7)

### Die zentrale Änderung: nicht bauen, sondern zusammenführen

v3 hat eine Regime-Engine als neues, gekapseltes Modul geplant. Es gibt schon zwei. Der Schritt heißt jetzt:

**10a. Regime-Architektur festlegen.** Zwei Ebenen, klar benannt:

| Ebene | Modul | Frage | Zeitraster | Hysterese |
|---|---|---|---|---|
| **Markt-Risiko** | `market_oracle` (bleibt) | Darf jetzt überhaupt gekauft werden, und wie groß? | Zyklus (Minuten) | `min_bars_to_flip=2` (bleibt) |
| **Richtungs-Bias** | `regime_detector` (wird erweitert) | Ist dieser Coin in einem Trend, und in welche Richtung? | Kerze (1h/4h) | **Neu:** Tages-Hysterese, Startwert 3 Tage (v3 7.7) |

Der Richtungs-Bias ist das, was v3 mit 7.1–7.4 meinte. Der Detector hat ADX und EMA-200 schon. **Fehlt:** SuperTrend (7.1), Volumen-Bestätigung als harte Bedingung für einen Flip (7.3), die Konfliktauflösung in drei Klassen (7.6), Tages-Hysterese (7.7). Die Sentiment-Fusion im Detector wird zur Klasse „Bestätigung“, nicht „Pflicht“.

### Weitere Schritte, angepasst

- **7.5 On-Chain & Derivate:** Santiment-Slugs in `santiment_enrich.py` sind eine handgepflegte Map (`# Common base → Santiment project slug`). Genau das Problem aus dem Review. Phase 5 löst es mit Dune; bis dahin gilt: unverifizierter Slug → Indikator neutral.
- **7.6 Veto-Layer:** Das Konzept der `filters()`-Liste aus Jesse passt auf `risk_manager._evaluate_impl` — aber diese Funktion ist **700 Zeilen** lang. Veto-Liste als eigene, isoliert testbare Funktion vor `_evaluate_impl`, nicht hinein.
- **7.8 Signifikanz-Test:** Hermes kann das, sobald das Kostenmodell stimmt. Kein neuer Code, sondern Hermes-Experiment „Regime-Signal ohne Order-Ausführung“.
- **7.9 Partial-Exit beim Flip:** Neu. `exit_ladder.py` und `trailing_stop.py` existieren für Gewinnmitnahme, nicht für Regime-Flip. Eigener Exit-Grund `REGIME_FLIP`.
- **7.10 Sidecar & Memory:** Drei WebSocket-Hubs existieren getrennt. Nicht zusammenlegen (Risiko), aber ein gemeinsames Ausfall-Verhalten: Hub tot → Indikator neutral, nicht `flat`. TTL 12 h für Memory-Einträge, die in Entscheidungen einfließen.

---

## Phase 4 — Gegenrichtung (Abgleich zu v3 Abschnitt 8)

### Ist-Zustand: Paper-Shorts sind fertig, Live fehlt komplett

`short_math.py` und `short_policy.py` decken 8.1 (Position-Modell), 8.3 (Hebel-Stufung — als Config, nicht dynamisch), 8.4 (Zeit-Cap, Trailing) ab. Was fehlt, ist die **Börse**:

| v3 | Ist | Delta |
|---|---|---|
| 8.1 Futures-Konto, Kapitaltransfer | Paper rechnet Margin virtuell | ccxt `gate` mit `options.defaultType='swap'`, Testnet-Host, Transfer Spot→Futures mit Tages-Limit, Kontostand aus Börse |
| 8.2 Stop als Börsen-Order | `stop_price()` berechnet, `should_stop_or_liquidate()` prüft **im Bot** | `reduce_only`-Stop bei der Börse sofort nach Öffnung. Bot-Prüfung bleibt als zweite Linie. QUEUED-Status wenn Börse den Stop wegen Preisabstand ablehnt (Jesse-Muster) |
| 8.3 Dynamischer Hebel | Statisch aus Config, Cap 5 | Cap **2**, Stufung 1 / 1,5 / 2 nach `volatility_tier` (existiert), ADX > 25 als Bedingung (Detector liefert ADX) |
| 8.4 Haltedauer | `time_cap_hours` 4 / 8 | Zweiter Parameter `regime_short_max_hours` für proaktive Shorts (A.3) |
| 8.5 Zwei Wege | Reaktiv über `auto_sources` existiert | Proaktiv neu, mit eigenem, kleinerem Sizing (`auto_notional_pct 0.35` ist der reaktive Wert — proaktiv startet niedriger). `linked_trade_id` gegen Doppelverlust. |

### Zusätzlicher Schritt

- **15a. Isolated-Modus bei der Börse setzen und verifizieren.** `short_math.liquidation_price_isolated` rechnet Isolated. Wenn das Gate-Konto auf Cross steht, stimmt kein einziger Stop-Abstand. Beim Recovery (Phase 1, Schritt 6) mitprüfen.

---

## Phase 5 — Externer Kontext-Layer

Wie in der Sprachsitzung skizziert, jetzt gegen den Code:

1. **Datenquellen-Abstraktion.** Es gibt heute LunarCrush, CMC, X, Santiment, RSS, Grok — jede mit eigenem Client, eigenem Cache, eigenem Fehlerverhalten. Eine Schnittstelle `ContextSource` mit `fetch(symbol, asof) → value | None`, Rate-Limit, Cache, `enabled`-Flag pro Quelle pro Tenant. Rückgabe `None` → Verbraucher setzt Indikator **neutral**.
2. **Dune ersetzt Santiment** als On-Chain-Quelle. Die Slug-Map in `santiment_enrich.py` wird stillgelegt.
3. **Fear & Greed, BTC-Dominanz** als Bestätigungs-Indikatoren in die Konfliktauflösung (7.6, Klasse „Bestätigung“).
4. **Polymarket** als Zahl (Wahrscheinlichkeit eines Ereignisses), nur wenn ein Ereignis im Kalender steht.
5. **LLM-Veto mit Werkzeugen.** Der Bot holt die Daten selbst per REST (deterministisch, protokollierbar) und gibt sie als Text ins Prompt — **nicht** das Modell mit MCPs orchestrieren lassen, weil sonst nicht rekonstruierbar ist, worauf ein Veto beruhte. Werkzeug-Ergebnisse: CryptoPanic (Breaking News), Perplexity (Recherche zu gemeldetem Ereignis), LunarCrush (Sentiment-Extrem). Zeitbudget 60 s, Timeout = kein Veto. Nur vor **proaktiven** Shorts. Prompt und Antwort werden am Trade gespeichert.
6. **News-Einstufung 0–3** (`ctx_news_risk`) durch dasselbe LLM, für jedes Signal, unabhängig vom Veto — das ist die Kontext-Achse aus Phase 2.
7. **API-Kosten pro Tenant** ins Ledger als Betriebsausgabe.

Voraussetzung: Phase 4 ist live. Vorher gibt es keinen Verbraucher für ein Short-Veto.

---

## Phase 6 — Musterdatenbank

Drei Bausteine, in dieser Reihenfolge:

1. **Live sammeln** — läuft seit Phase 2 automatisch.
2. **Historisch nachtragen** (Backfill). Für jede Kontext-Achse: Gibt es Point-in-Time-Historie? Wie fein ist der Zeitstempel? Ergebnis pro Quelle dokumentieren, bevor eine Zeile geschrieben wird.

   | Achse | Historie | Zeitstempel | Bewertung |
   |---|---|---|---|
   | Kurse, Volumen | ja | Kerze | unkritisch |
   | Oracle-State, Coin-Regime | rekonstruierbar aus Kerzen (deterministisch) | Kerze | unkritisch, aber nur mit dem Code-Stand von damals — Regime-Logik versionieren |
   | Fear & Greed | ja (Alternative.me) | Tag | brauchbar |
   | Dominanz | ja | Tag | brauchbar |
   | Funding | ja (Gate) | 8 h | brauchbar |
   | Dune On-Chain | ja | Block | gut |
   | News | Archive uneinheitlich | oft nur Datum | **kritisch** — nur mit Minuten-Zeitstempel verwenden, sonst Look-ahead |
   | Polymarket | begrenzt | Ereignis | prüfen |

   Jede nachgetragene Zeile trägt `ctx_origin = backfilled`. Ein Muster, das nur in Backfill-Daten hält und live nicht, war Look-ahead.

3. **Auswerten, von einem Menschen.** Mindestzahl pro Konstellation (Startwert 20) — darunter wird nichts angezeigt. Grobe Achsen mit 3–4 Stufen, nicht 8 Merkmale mit feinen Werten (Kombinatorik gegen wenige Jahre Krypto-Historie). Ein bestätigtes Muster wird zur **explizit programmierten Regel**, geht durch Backtest und Hermes-Signifikanz-Test, und wirkt **nur dämpfend**: Größe runter, Anzahl runter, keine neuen Positionen. Niemals Größe rauf.

Der Bot leitet keine Regel selbst ab. Weaviate-Ähnlichkeitssuche ist ein Werkzeug für die Auswertung, kein Eingang in die Entscheidung.

---

## Phase 7 — Autonome Weiterentwicklung (Hermes einhegen)

Hermes existiert und tut das, was in der Sprachsitzung gewünscht war: Parameter variieren, testen, übernehmen, ohne Klick. Das Konzept muss es nicht bauen, sondern in vier Punkten verändern:

1. **Kostenmodell.** Hermes rechnet 3 % Round-Trip. Nach Phase 1 rechnet es das reale Modell. Alle Promotionen vor diesem Datum werden als `legacy_cost_model` markiert und neu bewertet (Schritt 5a).
2. **Bänder.** Hermes darf heute jeden Parameter in `strategies[]` verändern. Künftig: pro Parameter ein erlaubtes Band in der Config. **Außerhalb der Bänder, und für Hermes unveränderbar:** Hebel-Cap, `max_margin_pct`, `max_open`, Stop-Abstand-Minimum, Liquidations-Puffer, Kapitalgrenzen pro Tenant. Das ist „Memory sammelt, ein Mensch justiert“ aus v3 — in Code gegossen.
3. **Entscheidungs-Journal.** `hermes/memory/` bekommt eine append-only Datei: was geändert, warum, Backtest-Ergebnis, Live-Ergebnis nach n Tagen, und — wichtig — was **verworfen** wurde und warum. Jeder Hermes-Zyklus liest das Journal zuerst. Das verhindert das Pendeln zwischen zwei Werten, das ohne Gedächtnis entsteht.
4. **Live-Veto schärfen.** `live_evidence.py` existiert. Es muss auf dem korrigierten Ledger (Phase 2) rechnen und eine Mindestzahl echter Trades verlangen, bevor eine Promotion live wirksam wird. Paper-Ergebnisse allein reichen nicht mehr, weil der Paper-Pfad (Phase 1) erst dann verlässlich ist, wenn er durch denselben Adapter läuft.

Was Hermes weiterhin nicht darf: die Risikogrenzen anfassen, aus Kontext-Achsen (Phase 6) Regeln ableiten, auf ein Profitziel hin optimieren. Zielgröße bleibt eine risikoadjustierte Kennzahl mit Drawdown-Strafe, nicht Rendite.

---

## Teil C — Implementierungsschritte, angepasst

Die 24 Schritte aus v3 bleiben. Änderungen und Ergänzungen:

| Schritt | Änderung gegenüber v3 |
|---|---|
| 1 | **Erweitert** um 1a (Exception-Audit als Tabelle) und 1b (Fail-Open-Inventar mit Klassifikation) |
| 2 | Adapter-Basis bleibt, `PaperExecutionAdapter` wird zu Gate-Adapter mit Testnet-Host. `dry_run` abschaffen. |
| 3 | Konkret: `core/costs.py`, gelesen von fünf heute getrennten Stellen (A.2) |
| 5 | **Erweitert** um 5a: Hermes-Baseline invalidieren, alle Experimente neu |
| 6 | Recovery inkl. Isolated-Modus-Prüfung (15a) |
| 8 | **Erweitert**: Scope-Semantik `demo/paper/live` festlegen, Mongo-Migration abschließen, Kontext-Achsen als Zusatzfelder (drei sofort befüllbar) |
| 10 | **Umgeschrieben** zu 10a: Zwei-Ebenen-Architektur (Orakel = Markt-Risiko, Detector = Richtungs-Bias). Detector erweitern statt neues Modul. |
| 14 | Veto-Liste **vor** `_evaluate_impl`, nicht hinein |
| 15 | Schrumpft: Paper-Modell existiert. Neu ist nur die Börsenanbindung + 15a |
| 17 | Cap 2 in Config **und** in `short_math.clamp_leverage` Default. Test. |
| 18 | Zweiter Parameter `regime_short_max_hours` |
| 25 | **Neu, Phase 5:** `ContextSource`-Abstraktion, Dune-Adapter, Santiment stilllegen |
| 26 | **Neu, Phase 5:** LLM-Veto mit REST-gesammelten Werkzeugdaten, News-Einstufung 0–3 |
| 27 | **Neu, Phase 6:** Backfill-Prüfung pro Quelle (Tabelle oben), Backfill-Job mit `ctx_origin` |
| 28 | **Neu, Phase 6:** Auswertungs-Werkzeug (Mindestzahl, grobe Achsen, Weaviate-Suche), Regel-Weg über Backtest |
| 29 | **Neu, Phase 7:** Hermes-Bänder in Config, unveränderbare Parameter-Liste, Test dass Hermes sie nicht schreiben kann |
| 30 | **Neu, Phase 7:** Entscheidungs-Journal, Zyklus liest es zuerst |
| 31 | **Neu, Phase 7:** `live_evidence` auf korrigiertem Ledger, Mindestzahl echter Trades vor Promotion |

Abhängigkeiten: 25–26 nach 23. 27–28 nach 8 (Felder) und 25 (Quellen). 29–31 nach 5a; 31 zusätzlich nach 8.

---

## Teil D — Constraints, ergänzt

Alle Constraints aus v3 Abschnitt 10 bleiben. Neu:

- **Kontext-Felder sind Kommentar, nie Eingang.** Kein `ctx_*`-Feld wird von `risk_manager`, `decision_engine`, `registry` oder Hermes gelesen. Test dafür.
- **Zwei Zeitstempel pro Kontextwert:** wann erhoben, wann der Trade war. Fehlt einer, wird der Wert nicht gespeichert.
- **Backfill ist markiert.** `ctx_origin` ist Pflicht. Muster, die nur in Backfill halten, sind ungültig.
- **Hermes-Bänder sind hart.** Parameter außerhalb der Bänder sind für Hermes nicht schreibbar, nicht nur „nicht empfohlen“.
- **Fail-Open ist erlaubt für Kontext, verboten für Geld.** Die Trennlinie steht pro Schalter in der Config-Doku.
- **Ein Kostenmodell.** Kein Modul rechnet Fee oder Slippage lokal. Wer `slippage_percent` oder `fee_rate` direkt liest, ist ein Fehler.

---

## Teil E — Offene Entscheidungen, aktualisiert

Aus v3 offen bleibt:

- Pilot-Tenant komplett Futures oder Spot-Long + Futures-Short? Nach Schritt 5.
- Testnet-Zugang (Schritt 0) ist **weiterhin nicht verifiziert**.

Neu durch den Code-Abgleich:

1. **Was wird aus `demo`?** Historischer Scope mit Monaten an Daten. Einfrieren als Archiv oder in `paper` migrieren? Betrifft, ob die alten Demo-Trades für Phase 6 nutzbar sind (Antwort: nur mit `cost_model_version = legacy`, also eingeschränkt).
2. **Hermes während Phase 1 anhalten?** Solange das Kostenmodell falsch ist, produziert jeder Zyklus Promotionen, die später zurückgenommen werden müssen. Vorschlag: Hermes pausieren, bis Schritt 5a läuft.
3. **Orakel-Hysterese in Zyklen oder Minuten?** `min_bars_to_flip=2` hängt vom `update_interval` (240 s) ab. Wird das Intervall geändert, ändert sich die Hysterese unbemerkt. Auf Zeit umstellen.
4. **Wer pflegt den Ereignis-Kalender** (Fed, CPI) für `ctx_macro_event_24h` — Hand oder Quelle? Bis Phase 5 von Hand.
5. **Grok als Hermes-Ideengeber bleibt?** `self_improver.py` nutzt Grok für Parametervorschläge. Das ist unproblematisch (Vorschlag, nicht Entscheidung), sollte aber im Journal als Quelle stehen.

---

## Teil F — Code-Kritik im Detail

Dieser Teil sammelt jede Kritik am aktuellen Code an einem Ort. Teil A hat die Befunde nach Konzeptbezug sortiert; hier stehen sie nach Schwere, mit Datei und Zeile, damit sie als Arbeitsgrundlage für Review und Umsetzung taugen. Vollständiges Exception-Inventar: `audit-exceptions-phase1.md` (204 Zeilen, geldrelevante Dateien).

### F.1 Gesamturteil

Der Bot ist ein sehr guter Forschungsbot, der noch kein Trading-Bot ist. Die Architektur ist breiter und reifer, als der Entstehungsweg vermuten lässt: Order-Ledger mit Idempotenz, Mandanten, zwei Regime-Ebenen, Short-Mathematik mit Liquidationsrechnung, selbstverbessernder Agent mit Live-Veto, 50.000 Zeilen Tests. Aber die eine Stelle, an der Geld tatsächlich fließt — die Börsenanbindung — ist die dünnste im ganzen System, und die Zahl, auf die ein Jahr Optimierung zielt — der Kostenanteil — ist an fünf Stellen unterschiedlich und überall falsch.

Die Schwächen tragen die Handschrift von Code, den KI geschrieben und KI reviewt hat: Jede Aufgabe für sich sauber gelöst, niemand hatte den Überblick über das Ganze. Das ist kein Vorwurf an die Steuerung, sondern die bekannte Grenze der Arbeitsweise — und der Grund, warum Phase 1 mit Inventaren beginnt, nicht mit Code.

### F.2 Schwere 1 — kann Geld kosten, sobald live gehandelt wird

| # | Befund | Ort | Wirkung |
|---|---|---|---|
| 1 | **Fill wird angenommen, nicht gelesen.** `filled = raw.get("filled") or amount`, `fill_price = average or price or order.price` | `execution/gate_adapter.py` `_execute_buy`, `_execute_sell` | Fehlt ein Feld in der Börsenantwort, bucht der Bot eine volle Füllung zum Wunschpreis. Ledger und Börse driften auseinander, ohne dass es jemand merkt. |
| 2 | **Keine Teilfüllung, keine Timeouts, keine Ablehnungscodes.** Ein `except Exception → executed=False` deckt alles ab | `execution/gate_adapter.py::execute` | Bei Timeout weiß der Bot nicht, ob die Order bei der Börse liegt. Ein Neusenden im nächsten Zyklus verdoppelt die Position. |
| 3 | **Keine Recovery gegen die Börse.** `fetch_open_orders`, `fetch_positions`, `fetch_my_trades` kommen im Code nicht vor. `reconcile_*` gleicht nur Ledger-Dateien untereinander ab | `services/order_service.py:496`, `services/ledger_sync.py` | Nach Absturz oder Neustart gilt das Ledger als Wahrheit. Die Börse kann anders aussehen. |
| 4 | **Keine Stops bei der Börse.** Kein `reduce_only`, kein `stopPrice`, kein `create_order` mit Stop-Typ | gesamter Code | Jeder Stop hängt am Bot-Prozess. Bot down = keine Absicherung. Für Futures (Phase 4) ist das disqualifizierend. |
| 5 | **Balance-Fehler wird zu Null.** `fetch_balance` fehlgeschlagen → `return 0.0` | `execution/gate_adapter.py:64`, `:169` | Beim Kauf zufällig fail-closed (0 < usdt → abgelehnt). Beim Verkauf **fail-open**: `_validate_sell_amount` kappt nur `if exchange_balance > 0` — bei 0.0 wird die Ledger-Menge ungeprüft an die Börse geschickt. |
| 6 | **Ledger fällt still auf In-Memory zurück.** Wenn Mongo nicht erreichbar → `_STORE = MemoryOrderLedgerV2()` | `storage/order_ledger_v2.py:110` | Der Bot handelt weiter, schreibt Orders in den Arbeitsspeicher, und beim nächsten Neustart ist die Historie weg. |
| 7 | **Dual-Write ist fail-open.** Fehler beim v2-Schreiben → `pass`, Kommentar: „legacy blob remains source of truth during migration“ | `services/order_service.py:433` | Zwei Ledger, die stillschweigend auseinanderlaufen. Die Migration kann so nie abgeschlossen werden, weil niemand erfährt, dass sie scheitert. |
| 8 | **Paper-PnL ohne Kosten.** `pnl = (price - entry) * amount` | `services/portfolio_service.py:117` | Jede Paper-Statistik ist zu gut. Zugleich zieht `received` 1,5 % Slippage ab — Guthaben und PnL widersprechen sich. |
| 9 | **Live-Fee wird gelesen, aber nicht gerechnet.** `_extract_fee(raw)` landet in `record_live_trade`, der PnL kommt aus `portfolio.execute_sell` ohne Fee | `execution/gate_adapter.py::_sync_local_ledger` | Live-PnL im Ledger ist brutto. Steuerlich falsch, strategisch irreführend. |
| 10 | **Backtests mit 3 % Round-Trip.** `slippage_percent = 1.5` beidseitig, keine Fee | `hermes/backtester.py:101`, `hermes/pipeline_backtest.py:74`, `intelligence/strategy_backtest.py:103` | ~15-fach zu pessimistisch. Hermes verwirft Strategien, die real funktionieren würden — und findet deshalb „kaum Neues“. |
| 11 | **Hebel-Cap 5 statt 2.** In Config **und** als hartcodierter Default | `config.json shorts.leverage_cap`, `strategies/short_math.py:16 clamp_leverage(cap=5.0)`, `:152 snapshot(cap=5.0)` | Ein Config-Fehler oder ein vergessener Parameter reicht, und der Bot rechnet mit 5x. |
| 12 | **Hermes schreibt live wirksame Parameter** ohne Bänder. `baseline.json` wird vom Live-Bot sofort gelesen, Prioritätsstufe 2 in `registry.py` | `hermes/self_improver.py`, `hermes/memory/`, `strategies/registry.py` | Ein Grok-Vorschlag, der einen fehlerhaften Backtest passiert, ist am nächsten Zyklus live. Keine Grenze für Stop-Abstände, Margin, Positionszahl. |

### F.3 Schwere 2 — verzerrt Entscheidungen oder verbirgt Fehler

| # | Befund | Ort | Wirkung |
|---|---|---|---|
| 13 | **370 `except Exception: pass`** außerhalb der Tests, 3 nackte `except:`. In den geldrelevanten Dateien 204 Stellen, davon 108 stille oder Default-liefernde | siehe `audit-exceptions-phase1.md` | Fehler verschwinden. Der Bot kann monatelang falsch rechnen, ohne dass ein Log-Eintrag entsteht. |
| 14 | **Positions-Update schluckt Fehler in der Mitte.** Peak-Reanchor und Recovery-Hold-Stempel je in eigenem `try/except: pass` | `strategies/positions.py:934`, `:949`, `:689` | Eine Position kann halb aktualisiert sein — Menge stimmt, Peak nicht. Trailing-Stop rechnet dann gegen einen falschen Bezugspunkt. |
| 15 | **Funding-Stunden werden zu Null.** Bei Fehler in der Zeitberechnung `hours = 0.0`, dann `pass` | `services/portfolio_service.py:228`, `:237` | Paper-Short ohne Funding-Kosten — ausgerechnet der Kostenblock, der Shorts teuer macht. |
| 16 | **Tages-Schlüssel fällt auf „heute“ zurück.** Kann der Zeitstempel einer Order nicht geparst werden → `datetime.now()` | `storage/order_ledger_v2.py:183`, `:196` | Alte Orders wandern in die Tagesstatistik von heute. Tageslimits (`max_daily_buys`) rechnen dann falsch. |
| 17 | **97 `fail_open`-Stellen**, zwei davon als Config-Default aktiv, vier dokumentierte Fail-Open-Pfade im Risk-Manager für Cash-Bias, Slot-Memory und Kapazität | `config.json:332,346`, `risk/risk_manager.py:1114,1131,1151`, `services/market_oracle/regime.py` Docstring | Fehlt eine Datenquelle, wird gekauft wie bei gutem Wetter. Für Kontext-Indikatoren vertretbar, für `size_mult` und Kapazität nicht. |
| 18 | **`_evaluate_impl` ist 700 Zeilen lang** (Zeile 98–794) | `risk/risk_manager.py` | Nicht isoliert testbar. Jede neue Veto-Regel macht es schlimmer. Die Veto-Liste aus dem Konzept muss **davor**, nicht hinein. |
| 19 | **Fünf Kostenstellen, vier Werte.** `slippage_percent 1.5`, `fee_rate 0.001`, `assumed_fee_pct 0.1`, Paper ohne alles, Live aus Antwort | siehe A.2 | Kein Modul weiß, was ein Trade kostet. |
| 20 | **Dry-Run als dritter Quasi-Modus.** `live.dry_run: true` schreibt `exchange_order_id="dry_run"` ins Live-Ledger | `execution/gate_adapter.py::execute` | Die Demo-Historie enthält Orders mit `trading_mode: live`, die nie eine Börse gesehen haben. Für Phase 6 (Muster) sind sie nicht von echten unterscheidbar, ohne die ID zu prüfen. |
| 21 | **Order-Status als freie Strings.** Praktisch nur `filled` / `rejected` | `services/order_service.py`, `storage/order_ledger_v2.py` | Kein `partially_filled`, kein `queued`, kein `canceled`. Das Modell kann Teilfüllung nicht einmal darstellen. |
| 22 | **Orakel-Hysterese in Zyklen, nicht in Zeit.** `min_bars_to_flip=2` auf Basis von `update_interval` (240 s) | `services/market_oracle/regime.py:194` | Wer das Intervall ändert, ändert die Hysterese, ohne es zu wissen. |
| 23 | **Alle vier Short-Auslöser sind Exit-Signale.** `rsi_sell`, `exit_1h_rsi_rollover`, `oracle_climax_harvest`, `exit_volume_climax` | `config.json shorts.auto_sources` | Verkauf und Short auf demselben Signal — Doppelverlust, wenn das Signal falsch war. Kein `linked_trade_id`. |
| 24 | **Santiment-Slugs handgepflegt.** Kommentar: „map the frequent bags“ | `services/dca_sniper/santiment_enrich.py:22` | Coin nicht in der Map → kein On-Chain-Wert, und der Verbraucher merkt es nicht (fail-open). |

### F.4 Schwere 3 — Struktur und Schulden

| # | Befund | Ort | Wirkung |
|---|---|---|---|
| 25 | **Zwei Ausführungspfade.** Paper (14 Zeilen) hat nie eine Börsenantwort gesehen; Live (311 Zeilen) hat eigene Buy/Sell-Logik | `execution/paper_adapter.py`, `gate_adapter.py` | Paper testet nicht den Code, der live läuft. |
| 26 | **Drei Ledger-Scopes** `demo / paper / live`, Semantik nirgends festgelegt | `storage/ledger_router.py:13–26` | Monate an Demo-Daten mit altem Kostenmodell und `dry_run`-Orders — Nutzbarkeit für Phase 6 unklar. |
| 27 | **Mongo-Migration halb fertig.** Router kennt `mongo`, Dual-Write existiert, aber `orders.live.json` bleibt Primärreferenz in Code und Doku | `storage/ledger_router.py`, `data_manager.py` (64 Exception-Stellen) | Zwei Wahrheiten. `data_manager.py` ist mit 1.500+ Zeilen und 64 Fängern der unübersichtlichste geldrelevante Baustein. |
| 28 | **Vier Backtest-Engines** ohne gemeinsames Kostenmodell | `hermes/backtester.py`, `hermes/pipeline_backtest.py`, `intelligence/strategy_backtest.py`, `hermes/replay_engine.py`, Rust-Skelett | Ergebnisse sind untereinander nicht vergleichbar. |
| 29 | **Zwei Regime-Module ohne dokumentierte Rollenteilung** | `services/market_oracle/`, `intelligence/regime_detector.py` | Beide produktiv, beide beeinflussen Sizing. Wer was entscheidet, steht nirgends. |
| 30 | **Drei WebSocket-Hubs** mit je eigenem Ausfallverhalten | `services/dca_sniper/ws_watch.py`, `services/gainer_signal/ws_loop.py`, `services/exit_realtime/hub.py` | Hub tot → Verhalten pro Hub verschieden. Kein gemeinsames „Indikator neutral“. |
| 31 | **Sechs externe Datenquellen, sechs Clients.** LunarCrush, CMC, X, Santiment, RSS, Grok | `services/`, `intelligence/`, `data/` | Kein gemeinsamer Cache, kein gemeinsames Rate-Limit, kein einheitliches `None`-Verhalten. |
| 32 | **`time_cap_hours` 4/8 für alle Shorts** | `strategies/short_policy.py` | Für reaktive Kurz-Shorts richtig, für Regime-Shorts (Tage) unbrauchbar. Ein Parameter für zwei Dinge. |
| 33 | **Test-Suite riesig, aber am falschen Ort dicht.** 289 Test-Dateien, 50.000 Zeilen — für Execution gegen gemockte Börsenantworten (Teilfüllung, Timeout, Ablehnung) **kein** Test gefunden | `tests/` | Die Stelle mit dem höchsten Risiko hat die geringste Testabdeckung. |

### F.5 Was gut ist und bleiben soll

Damit die Kritik nicht den Eindruck erweckt, es müsse alles neu:

- **Order-Ledger-Struktur** (`request / risk / execution / timestamps`, Idempotency-Key, Quelle, Signal) ist näher an Jesse's „Trade aus Orders ableiten“ als das Konzept angenommen hat. Bleibt.
- **Marktorakel** als Zustandsmaschine mit Hysterese, Funding, Breadth — richtige Idee, richtiger Ort. Bleibt, wird nur auf Zeit statt Zyklen umgestellt.
- **`short_math.py`** — Isolated-Liquidation, Puffer, Stop-aus-Margin, Funding — korrekt gerechnet, sauber gekapselt, gut testbar. Bleibt, bekommt nur Cap 2 und Börsenanbindung.
- **Hermes-Grundschleife** (ein Parameter ändern, beide Varianten testen, nur bei klarer Verbesserung übernehmen, Live-Metriken als Veto) ist genau richtig. Bleibt, bekommt Kostenmodell, Bänder, Journal.
- **Mandantenmodell** über ContextVar. Bleibt.
- **Volatilitäts-Tiers** mit `frozen_tier`. Bleiben, tragen den dynamischen Hebel.
- **Ledger-Router** mit `_atomic_write` und Rückgabewert, den alle Aufrufer weiterreichen. Richtig gebaut — nur prüft am Ende niemand den Rückgabewert.

### F.6 Wie die Kritik in die Phasen fließt

| Befund # | Phase | Schritt (v4 Teil C) |
|---|---|---|
| 1, 2, 21, 25 | 1 | 2, 3.2, 3.3 |
| 3 | 1 | 6 |
| 4 | 4 | 16 |
| 5, 13, 14, 15, 16 | 1 | 1a |
| 6, 7, 26, 27 | 2 | 8 |
| 8, 9, 10, 19 | 1 | 3 |
| 11 | 1 | 17 (vorgezogen) |
| 12 | 7 | 29, 30, 31 |
| 17 | 1 | 1b, 4 |
| 18 | 3 | 14 |
| 20 | 1 | 2 (`dry_run` entfällt) |
| 22, 29 | 3 | 10a |
| 23, 32 | 4 | 18, 19 |
| 24, 31 | 5 | 25 |
| 28 | 1 | 3 (ein Kostenmodell für alle Engines) |
| 30 | 3 | 13 |
| 33 | 1 | 3.3 (Tests sind Teil der Abnahme) |
