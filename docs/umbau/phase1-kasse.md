# Phase 1 — Kasse: Aufgabenliste für Claude Code

**Quelle:** Konzeptpapier v4 (Teil A + Phase 1), Abgleich gegen `staging` @ 3.9.2026
**Ziel der Phase:** Der Bot gibt echte Orders auf, versteht die Antwort der Börse, rechnet Kosten korrekt, und Paper läuft auf demselben Code wie Live.
**Kein neues Feature in dieser Phase.** Jede Aufgabe ist Härtung.

Delegation nach `CLAUDE.md`: **C** = Claude direkt (Urteil nötig, geldrelevant), **G** = an Grok delegierbar (mechanisch, gut spezifiziert), danach Review + `pytest`.

---

## 0. Vorbereitung

### 0.1 Hermes pausieren — C
- `hermes.enabled: false` in `config.json` oder Loop nicht starten.
- `hermes/memory/baseline.json` kopieren nach `baseline.pre-costmodel.json`. Nicht löschen.
- **Abnahme:** Kein Hermes-Zyklus mehr im Log. Baseline-Kopie liegt vor.

### 0.2 Testnet oder Klein-Live entscheiden — Jennsen
- Entweder: `testnet.gate.com` Login, API-Keys, Host `fx-api-testnet.gateio.ws`.
- Oder: Gate Spot live, Tenant mit max. 100 USDT, nur BUY/SELL.
- **Abnahme:** Ein `fetch_balance()` gegen das gewählte Ziel liefert eine Antwort.

---

## 1. Inventare (vor jeder Codeänderung)

### 1a. Exception-Audit — C
Alle `except Exception` und nackte `except:` in diesen Dateien listen:
`execution/`, `risk/`, `services/order_service.py`, `services/portfolio_service.py`, `strategies/positions.py`, `data_manager.py`, `storage/`.

Ausgangspunkt: `audit-exceptions-phase1.md` (204 Stellen, heuristisch vorklassifiziert) und Konzept v4 Teil F.2/F.3 (Urteil zu #5, #6, #7, #13–16). Zieldatei `docs/audit/exceptions-phase1.md`, pro Stelle eine Zeile:
`Datei:Zeile | was wird gefangen | Klasse A/B/C | Begründung`
- **A** bleibt (Logging, Telegram, Anzeige — nicht geldrelevant)
- **B** wird zu `log(..., "ERROR")` + `raise`
- **C** wird Fail-Closed: Order abgelehnt, Zyklus für dieses Symbol übersprungen

**Abnahme:** Tabelle vollständig, jede Zeile klassifiziert. Gesamtzahl außerhalb Tests heute: 370 + 3.

### 1b. Fail-Open-Inventar — C
Alle 97 `fail_open`-Vorkommen listen in `docs/audit/fail-open-phase1.md`.
Pro Schalter: `Kontext` (darf fail-open bleiben) oder `Geld` (wird fail-closed).
Geld = alles, was `size_mult`, Hebel, Stop-Abstand, Short-Freigabe, Kapital oder `approved` berührt.
Config-Defaults `market_oracle_risk_fail_open` und `santiment_risk_fail_open` **nicht** global umstellen — die Verbraucher entscheiden.

**Abnahme:** Tabelle vollständig. Jeder `Geld`-Schalter hat einen Folge-Task in Abschnitt 4.

---

## 2. Kostenmodell — C (Design), G (Verdrahtung)

### 2.1 `core/costs.py` anlegen — C
```
CostModel(exchange, market) → fee_maker_pct, fee_taker_pct, slippage_pct, funding_source
apply_entry(price, qty, side, order_type) → (net_price, fee_usdt)
apply_exit(...) → (net_price, fee_usdt)
round_trip_pct(order_type) → float
```
Config-Block:
```json
"costs": {
  "gate": {
    "spot":  {"fee_maker_pct": 0.1, "fee_taker_pct": 0.1, "slippage_pct": 0.1},
    "swap":  {"fee_maker_pct": 0.02, "fee_taker_pct": 0.05, "slippage_pct": 0.1, "funding": "exchange_history"}
  }
}
```
Startwerte sind Platzhalter aus der Gate-Fee-Seite — **vor Live gegen das Konto prüfen** (VIP-Stufe, GT-Rabatt).
Version des Modells als Konstante `COST_MODEL_VERSION = "2026-09-v1"`.

### 2.2 Fünf Stellen auf `core/costs.py` umstellen — G, dann C-Review
| Stelle | heute | Änderung |
|---|---|---|
| `hermes/backtester.py:101,144,164` | `slippage_percent` beidseitig | `CostModel.apply_entry/exit` |
| `hermes/pipeline_backtest.py:74,88,142,167` | dito | dito |
| `intelligence/strategy_backtest.py:103,246,251` | dito | dito |
| `services/portfolio_service.py:117` | `pnl = (price-entry)*amount`, keine Fee | PnL netto: Fee auf Kauf und Verkauf abziehen; `received` aus demselben Modell |
| `strategies/short_math.py`, `short_policy.py` | `fee_rate 0.001` Konstante | `CostModel("gate","swap")` |
| `strategies/grid.py`, `grid_limits.py`, `entry_recipe.py`, `grid_plan.py` | `assumed_fee_pct 0.1` | `CostModel.round_trip_pct()` |

`slippage_percent` in `config.json` und `core/config.py:160` **entfernen**. Direkter Zugriff auf `slippage_percent` oder `fee_rate` wird per Test verboten:
`tests/test_costs_single_source.py`: grep über `core strategies services risk hermes intelligence` nach `slippage_percent|fee_rate\b|assumed_fee_pct` → muss leer sein außer in `core/costs.py`.

**Abnahme:** Test grün. `pytest` gesamt grün. Ein Backtest-Lauf vor/nach für ein Symbol dokumentiert (Trades, PnL, Anzahl Promotionen).

### 2.3 Live-Fee in den PnL — C
`execution/gate_adapter.py`: `_extract_fee(raw)` liefert die echte Fee. Sie muss in `portfolio.execute_sell` / `execute_buy` **hinein**, nicht nur ins `record_live_trade`. Signatur erweitern: `fee_usdt: float | None` — wenn gesetzt, überschreibt sie die Modell-Fee.

**Abnahme:** Live-/Testnet-Trade zeigt im Ledger `pnl` netto und `fee` konsistent.

---

## 3. Ein Codepfad — C

### 3.1 Paper durch den Gate-Adapter
`execution/paper_adapter.py` (14 Zeilen) wird zu `GateExecutionAdapter(host=testnet)` oder — falls Testnet nicht geht — `GateExecutionAdapter(mode="shadow")`: baut die Order, ruft `exchange.create_order` **nicht**, aber alles andere (Precision, Limits, Balance-Check) läuft echt.
`factory.py`: `trading_mode` → `live | paper | shadow`. `dry_run` **entfällt**; Orders mit `exchange_order_id="dry_run"` im Ledger bleiben als Historie.

**Abnahme:** `PaperExecutionAdapter` gelöscht. Paper-Order durchläuft `_validate_sell_amount` und `amount_to_precision`.

### 3.2 Order-Modell erweitern — C (Modell), G (Migration)
`core/models.py: TradeOrder`:
- `qty` (gewünscht) und `filled_qty` (tatsächlich) trennen. `remaining_qty` als Property.
- `status: OrderStatus` Enum: `ACTIVE, QUEUED, PARTIALLY_FILLED, EXECUTED, CANCELED, REJECTED`.
- `reduce_only: bool = False`, `client_order_id: str` (= Idempotency-Key), `exchange_order_id`.
- `order_exist_in_exchange: bool` — Pflicht `True` bei Live.

`services/order_service.py`, `storage/order_ledger_v2.py`: Status-Strings auf Enum. Migration alter Einträge: `filled → EXECUTED`, `rejected → REJECTED`.

**Abnahme:** Kein String-Vergleich `== "filled"` mehr außerhalb der Migration.

### 3.3 Response-Behandlung — C
`execution/gate_adapter.py::_execute_buy/_execute_sell`:
- `filled = raw.get("filled")` — **kein** Fallback auf `amount`. Fehlt es → `fetch_order(id)` nachziehen. Fehlt es dann noch → Status `ACTIVE`, Ledger schreiben, Zyklus geht weiter, nächster Zyklus prüft.
- `average` fehlt → aus `fetch_my_trades(symbol, since)` rekonstruieren, sonst `ACTIVE`.
- `filled < qty` → `PARTIALLY_FILLED`, Position nur um `filled_qty`.
- ccxt `InsufficientFunds`, `InvalidOrder`, `RateLimitExceeded`, `NetworkError`, `RequestTimeout` einzeln fangen. Timeout → **erst** `fetch_open_orders` und `fetch_order(client_order_id)`, dann entscheiden. Nie blind neu senden.
- `client_order_id` = Idempotency-Key aus dem Ledger → Gate `text`-Feld (`t-<key>`).

**Abnahme:** Tests mit gemockten ccxt-Antworten für: voll gefüllt, teilgefüllt, `filled` fehlt, Timeout mit später gefundener Order, Ablehnung. Jeder Fall hat einen definierten Ledger-Zustand.

---

## 4. Fail-Closed für Geld — C

Für jede `Geld`-Zeile aus 1b:
- `risk/risk_manager.py` `_market_bias_for_cash`, `_open_book_memory_counts`, `_resolve_position_capacity`: fehlender Eingang → `size_mult = min(1.0, …)`, **nie** > 1.0, und `approved=False` für neue Positionen wenn der Eingang für die Entscheidung tragend ist.
- `services/market_oracle/policy.py`: `fail_open` bleibt für `sensor_policy`, wird `False` für `block_buys_on_crash` und `apply_size_mult` wenn Snapshot älter als 2 × `update_interval`.

**Abnahme:** Test pro Pfad: Eingang `None` → Größe nicht erhöht, Neueröffnung blockiert.

---

## 5. Recovery beim Start — C

Neu: `execution/recovery.py::reconcile_with_exchange(tenant)`, aufgerufen in `aria_bot.py` **vor** dem ersten Zyklus.
1. `fetch_balance()`, `fetch_open_orders()`, bei Swap `fetch_positions()`.
2. Vergleich mit `positions.<scope>` und `orders.<scope>`.
3. Abweichung → **Börse gewinnt**. Ledger anpassen, Abweichung als Event loggen und per Telegram melden.
4. Offene Positionen ohne Stop (ab Phase 4) → Stop nachlegen.
5. Börse nicht erreichbar → Tenant startet **nicht**. Kein Zyklus ohne Abgleich.

`services/ledger_sync.py::reconcile_*` bleibt für Ledger-intern, wird umbenannt zu `sync_ledger_files` (kein Namenskonflikt mit Börsen-Recovery).

**Abnahme:** Test: Ledger sagt Position X, Börse sagt keine → Ledger leer, Event geloggt. Test: Börse down → `start()` wirft, kein Zyklus.

---

## 6. Hebel-Cap — G, dann C-Review

- `config.json: shorts.leverage_cap: 5 → 2`
- `strategies/short_math.py::clamp_leverage(cap=5.0) → cap=2.0`, ebenso `snapshot(cap=5.0)`.
- Test: `clamp_leverage(5) == 2.0`, `resolve_short_params(...)["leverage"] <= 2.0` für jede Tier/Coin-Kombination.

**Abnahme:** Kein Pfad liefert Hebel > 2.

---

## 7. Isolated-Modus prüfen — C

In `reconcile_with_exchange` (Abschnitt 5) bei Swap: `fetch_position_mode` / Margin-Mode pro Symbol lesen. Nicht `isolated` → Tenant startet nicht, Meldung. `short_math.liquidation_price_isolated` ist sonst falsch.

**Abnahme:** Test mit gemocktem `cross` → Start verweigert.

---

## 8. Hermes neu bewerten (5a) — C

Nach 2.2 grün:
- Alle Experimente aus `hermes/memory/` gegen `core/costs.py` wiederholen.
- Jede Promotion vor `COST_MODEL_VERSION` bekommt `cost_model: legacy` im Memory.
- Vergleich dokumentieren: Anzahl Promotionen alt vs. neu, pro Symbol.
- Erst danach Hermes wieder einschalten.

**Abnahme:** `docs/audit/hermes-recost.md` mit dem Vergleich.

---

## Reihenfolge und Abhängigkeiten

```
0.1 ──┐
0.2 ──┼─► 1a, 1b ─► 2.1 ─► 2.2 ─► 2.3 ─► 8
      │               │
      │               └─► 3.1 ─► 3.2 ─► 3.3 ─► 5 ─► 7
      └─► 6 (unabhängig, sofort)
      4 nach 1b, parallel zu 3.x
```

## Was in dieser Phase nicht passiert
- Keine neuen Indikatoren, kein Regime-Umbau, keine Shorts live, keine Kontext-Felder.
- Keine Mongo-Migration (Phase 2).
- Kein Refactoring von `_evaluate_impl` (700 Zeilen) — nur die Fail-Closed-Stellen anfassen.

## Definition of Done für Phase 1
1. Ein Trade (Testnet oder Klein-Live) durchläuft Buy → Teilfüllung simuliert → Sell, und Ledger-PnL stimmt auf den Cent mit der Börsenabrechnung überein.
2. Bot-Neustart mit absichtlich verfälschtem Ledger → Recovery korrigiert, meldet, startet.
3. `pytest` grün, `test_costs_single_source` grün, kein Hebel > 2 möglich.
4. Hermes läuft wieder, auf korrigiertem Kostenmodell, mit dokumentiertem Vorher/Nachher.
