# Ledger-I/O — Fehler propagieren statt Zustand erfinden (#318, Phase 1 Tier 2)

**Stand:** 5. September 2026 · **Hängt ab von:** #299 Tier 1a (Schalter `risk.fail_closed_guards`) · **Quelle:** `docs/audit/exceptions-phase1.md` §3 Punkte 1–2, §5

## 1. Prinzip

Ein fehlgeschlagener Ledger-Lese- oder Schreibzugriff darf **nie** aussehen wie „nichts da". Heute liefert die Persistenzschicht bei Fehler leere Container (`{}`, `[]`, `None`) oder ein `False`, das kein Aufrufer prüft — der Bot handelt dann auf leerem Buch, kauft doppelt, verwirft Positionen als orphan oder fährt mit fremder Config. Ziel: **Fehler → Ausnahme → Handelszyklus „Zustand unbekannt" → keine Order, kein Flush, Operator-Meldung, Retry.**

## 2. Zwei Mechanismen

### 2.1 `storage/errors.py` (neu, klein)
```python
class LedgerUnavailable(RuntimeError):
    """Ledger read/write failed. Carries scope, tenant_id, op ('load_orders', …), cause."""
class LedgerWriteFailed(LedgerUnavailable): ...
```

### 2.2 Load-Funktionen werfen, Save-Funktionen werfen
- **Load** (`data_manager.load_orders :1544`, `load_positions_document :1638`, `load_trade_history_document :963`, `load_live_trade_history :1322`, `_load_orders_json :1528`, `_load_positions_json`, `_load_default_config_from_disk :690`, `_load_tenant_config_body :720`; `storage/tenant_meta_store.py :37/:83/:99`; `storage/grid_plan_store.py :67`): bei Mongo-Exception, JSON-Parse-Fehler oder verweigertem Fallback → `raise LedgerUnavailable(...)`. **Nie** `{}`/`[]`/`None` als Ersatz. Eine *legitim leere* Sammlung (neuer Tenant, noch keine Orders) bleibt natürlich `{"orders": []}` — der Unterschied ist: gelesen-und-leer vs. nicht-lesbar.
- **Save** (`save_orders :1589`, `save_positions_document :1664`, `save_trade_history_document :1032`, `_save_orders_json :1540`, `record_trade`, `record_live_trade`, `storage/ledger_router._atomic_write :96`): bei Fehler `raise LedgerWriteFailed(...)` statt `return False`. Rückgabetyp bleibt `bool` (immer `True`) — Aufrufer mit `if not ok:` funktionieren weiter, Aufrufer ohne Prüfung bekommen jetzt die Ausnahme.
- **`OrderService._save` :268** und seine sechs Aufrufer (`:345 create_from_request`, `:390`, `:420 update_status`, `:491`, `:509`, `:1052 link_execution_result`): `_save` propagiert; `create_from_request` darf **keinen** Adapter-Call auslösen, wenn der Record nicht durabel ist (Review C5).
- **`_dual_write_v2` :433**: bleibt fail-open (v2 ist Shadow), aber `log(…, "WARNING")` statt stummem `pass`.
- **`storage/order_ledger_v2.py :110`**: Index-Fehler → `LedgerUnavailable` statt lebenslangem Memory-Store — oder, wenn v2 Shadow bleiben soll, WARNING + `degraded`-Flag, das `stats_day_filled_fast` (liest v2 **unbedingt**, Review) fail-closed macht.

## 3. „Zustand unbekannt" im Zyklus

| Ort | heute | neu |
|---|---|---|
| `strategies/positions.load_positions :377-390` | `store.clear()` vor dem `try`; Exception → leerer Store | `clear()` **nach** erfolgreichem Load. Bei `LedgerUnavailable`: Store unverändert, Modul-Flag `_positions_state[tenant,scope] = "unknown"`, ERROR einmal. |
| `strategies/positions.flush_positions :482` | schreibt immer | verweigert bei `"unknown"` (ERROR einmal) — **kein `replace_one` mit `{}`**. Erst ein erfolgreicher Load setzt `"known"`. |
| `services/trading_service.execute_order` | — | `LedgerUnavailable` → `RiskDecision(approved=False, code="ledger_unavailable")` für **alle** Ordertypen, Operator-Meldung einmal pro Episode. |
| `aria_bot._run_tenant_price_cycle` | — | `positions_state == "unknown"` → Trading für diesen Tenant in diesem Zyklus überspringen, Log + Meldung; Telegram-Lesebefehle bleiben. **`aria_bot.py` ist Guardrail — Änderung minimal (ein Check am Zyklusanfang), zeilenweise auditieren.** |
| `data_manager._reject_demo_mongo_orders_downgrade :1584` | Exception → `False` (= „kein Downgrade") | Exception → `True` (blockieren) + ERROR |
| `services/ledger_sync.prune_orphan_position_cache :163-171` | leeres `order_snap` → alle Positionen orphan → Doc leer geschrieben | läuft nur, wenn `load_orders` **erfolgreich** und `orders` nicht leer war; sonst überspringen + WARNING |

**Bewusster Trade-off:** Auch SELL wird bei nicht erreichbarem Ledger verweigert — ein Stop, der wegen Mongo-Hänger nicht feuert, ist schlecht, aber ein Verkauf, der nicht verbucht wird, ist schlimmer (Position unsichtbar, Recovery fehlt bis #314). Operator sieht die Meldung sofort. In Phase 2 (Kassenbuch) kann SELL mit Börsen-Recovery (#314) wieder erlaubt werden.

## 4. Config-Fehler sind Startfehler
- `_load_default_config_from_disk :690`: unlesbare `config.json` → **`raise`**, Bot startet nicht. Der hartkodierte Fallback (`max_usdt_per_trade: 150`, `stop_loss_pct: 12`, `max_open_positions: 5`) wird gelöscht — er ändert Order-Sizing still.
- `_load_tenant_config_body :720`: Mongo-Fehler → `LedgerUnavailable` → Tenant-Zyklus übersprungen, **nicht** Operator-Config.
- `tenant_meta_store :83/:99`: Tenant ohne Watchlist → **leere** Watchlist (legitim leer), nicht Operator-Watchlist; Lesefehler → `LedgerUnavailable`.

## 5. Fallback-Verweigerung generalisieren
`_should_refuse_demo_json_fallback(scope, cfg)` :1429 deckt nur `demo` ab. Neu `_should_refuse_json_fallback(scope, cfg)`: verweigern, wenn `not _ledger_writes_json(scope, cfg)` — d. h. immer, wenn Mongo für dieses Scope der Writer ist (dort ist die JSON ein totes Buch). `_demo_json_fallback_enabled()` bleibt als expliziter Notausgang.

## 6. Tests (`tests/unit/test_ledger_io_errors.py`)
1. `load_positions_document` wirft → RAM-Store unverändert, `flush_positions` verweigert, **kein** `replace_one`.
2. `load_orders` wirft → `execute_order` liefert `code="ledger_unavailable"` für BUY **und** SELL; `notify_operator` einmal.
3. `save_orders` wirft → `update_status("filled")` propagiert, `create_from_request` ruft den Adapter nicht.
4. Downgrade-Guard: Vorab-Read wirft → `True`.
5. `prune_orphan_position_cache` mit fehlgeschlagenem `load_orders` → keine Änderung am Positions-Doc.
6. Config-Read schlägt fehl → Start wirft; kein Fallback-Dict.
7. Tenant ohne eigene Config → `LedgerUnavailable`, nicht Operator-Config.
8. Legitim leer: neuer Tenant, Mongo erreichbar, keine Orders → `{"orders": []}` **ohne** Ausnahme (Negativtest gegen Überkorrektur).
9. `_should_refuse_json_fallback("live", mongo-cfg)` → True; `("paper", local-cfg)` → False.

## 7. Nicht hier
Redis-Lock / Zwei-Writer-Lease (#306) · Börsen-Recovery (#314) · `storage/ledger_router.py` (nicht verdrahtet; seine 10 Stellen analog nachziehen, wenn er verdrahtet wird).
