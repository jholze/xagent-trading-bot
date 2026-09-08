# Exception-Inventar Phase 1 — geldrelevante Dateien (Ticket #311, §1a)

**Stand:** 5. September 2026 · **Basis:** `staging` @ `1b40c69` (identisch mit `umbau/phase1` für diese Dateien) · **Umfang:** 204 `except`-Stellen in `execution/`, `risk/`, `storage/`, `services/order_service.py`, `services/portfolio_service.py`, `services/trading_service.py`, `services/ledger_sync.py`, `strategies/positions.py`, `data_manager.py`
**Vorlage:** `docs/umbau/audit-exceptions-phase1.md` (heuristische Vorklassifizierung) · **Urteil:** vier parallele Bewerter mit fester Rubrik, jede Stelle ±30 Zeilen gelesen inkl. Aufrufer · die gravierendsten Stellen von Claude im Code nachverifiziert (Abschnitt 4)

## 1. Rubrik

| Klasse | Bedeutung | Maßnahme |
|---|---|---|
| **A** | Nur Logging, Telegram, Anzeige, Metriken, optionale Anreicherung. Kein Einfluss auf Größe, `approved`, Positionsbestand, Cash, Ledger. | bleibt |
| **A (Kontext)** | Bei Fehler fällt nur ein optionaler Kontext-Indikator weg, ohne Größe zu erhöhen oder eine Sperre zu umgehen. | bleibt, Vermerk |
| **B** | Programmier-/Invariantenfehler, Weiterlaufen korrumpiert Zustand. | `log ERROR` + `raise` |
| **C** | `try` enthält Deny/Block/Reduce **oder** Ledger-/Position-/Cash-Zugriff, Handler lässt den Aufrufer weiterlaufen als sei nichts passiert. | **fail-closed** |
| **C?** | Wie C, Bewerter unsicher — Begründung in der Tabelle. | einzeln entscheiden |

## 2. Ergebnis

**204 Stellen:** **A** 91 · **B** 1 · **C** 87 · **C?** 25

| Datei | A | B | C | C? | Σ |
|---|---:|---:|---:|---:|---:|
| `data_manager.py` | 26 |  | 27 | 11 | 64 |
| `risk/risk_manager.py` | 25 |  | 24 | 6 | 55 |
| `risk/slot_eviction_runtime.py` | 7 |  | 9 |  | 16 |
| `services/order_service.py` | 12 |  | 1 |  | 13 |
| `storage/ledger_router.py` |  |  | 4 | 6 | 10 |
| `strategies/positions.py` |  | 1 | 7 |  | 8 |
| `storage/order_ledger_v2.py` | 6 |  | 2 |  | 8 |
| `execution/gate_adapter.py` | 2 |  | 2 |  | 4 |
| `storage/grid_plan_store.py` | 1 |  | 2 | 1 | 4 |
| `storage/tenant_meta_store.py` |  |  | 3 | 1 | 4 |
| `services/portfolio_service.py` | 1 |  | 2 |  | 3 |
| `storage/mongo_client.py` | 3 |  |  |  | 3 |
| `risk/moderate_deploy.py` | 2 |  |  |  | 2 |
| `risk/slot_eviction_rag.py` | 2 |  |  |  | 2 |
| `services/trading_service.py` | 1 |  | 1 |  | 2 |
| `storage/tenant_registry.py` | 1 |  | 1 |  | 2 |
| `services/ledger_sync.py` | 2 |  |  |  | 2 |
| `risk/position_capacity.py` |  |  | 1 |  | 1 |
| `risk/slot_eviction.py` |  |  | 1 |  | 1 |

Die Heuristik lag in **beide** Richtungen daneben: viele `A?` („nur Log") sind C, weil der Default oder ein ungeprüftes `False` ein leeres Ledger bzw. eine fremde Config als echten Wert weitergibt; viele `C?` sind A, weil der Handler *selbst* das Deny ist oder der Fehlerpfad restriktiv wirkt. Spalte „Heuristik abw.?" in Abschnitt 6.

## 3. Querschnittsbefunde

1. **„Leer statt Fehler"** — `except: return {}` / `[]` / `None` beim Laden heißt für den Aufrufer „keine Orders / keine Positionen / keine Config". `data_manager.py` (Orders, Positions, Live-History, Config), `strategies/positions.py:387`, `storage/tenant_meta_store.py`, `storage/grid_plan_store.py`. Der Bot handelt auf leerem Buch, kauft doppelt, verwirft Positionen als orphan, fährt mit Operator-Config.
2. **`save_*` gibt `False` zurück, kein Aufrufer prüft es** — `OrderService._save` (6×, inkl. Fill), `record_trade`/`record_live_trade`, `ledger_sync.py`, `remove_coin`/`add_coin`. Ungeprüftes `return False` ≡ `pass`. Fix muss Aufrufer mitziehen oder Save werfen lassen.
3. **Permissive Defaults vor dem `try`** — `block_buys=False`, `regime="NEUTRAL"`, `soft_block=False`, `global_mult=1.0`, `fraction=1.0`: bei Fehler bleiben sie stehen und wirken als Entscheidung. `risk_manager.py`, `slot_eviction_runtime.py`.
4. **Deny im `try`** — zehn Guards in `risk_manager._evaluate_impl` (`:270 :305 :330 :350 :380 :423 :481 :488 :517 :536`) + `strategies/decision_engine.py:1646/1661`: Exception überspringt das Deny, Kontrollfluss fällt zu `approved=True` (`:783`).
5. **`storage/ledger_router.py` ist produktiv nicht verdrahtet** — nur `resolve_ledger_backend`, `ledger_dual_write_enabled`, `*_SCOPE_FILES` werden importiert; seine C-Stellen sind latent. Die zeichengleichen Kopien in `data_manager.py:1518/1533/1612/1627` sind akut.

## 4. Von Claude im Code nachverifiziert

| Stelle | Befund | Status |
|---|---|---|
| `risk/risk_manager.py:1366-1381` `_dynamic_size` | Oracle-Ausfall → `global_regime=None` → `size_boost_default` **1.35** (`moderate_deploy.enabled: true`) → Order **35 % größer statt null** | ✅ |
| `risk/risk_manager.py:1569` | `except: fraction = 1.0` → Teil-TP wird Vollliquidation | ✅ |
| `risk/risk_manager.py:1919` `_trade_cooldown_blocked` | unparsbarer Timestamp → `return False, ""` → alle Cooldowns aus | ✅ |
| `risk/risk_manager.py:433/469` | `wqe_mode(raw)` ohne Zuweisung, `mode=mode` → `NameError` verschluckt → `log_buy_block` läuft für WQE-Blocks nie. **Echter Bug** | ✅ |
| `strategies/positions.py:377-390` `load_positions` | `store.clear()` **vor** `try`; Exception → leerer Store → nächster `flush_positions` überschreibt Dokument mit `{}` inkl. Locks. **Schwerster Fund** | ✅ |
| `data_manager.py:1584` `_reject_demo_mongo_orders_downgrade` | Guard fällt auf `False` = „kein Downgrade" → lässt den Wipe durch, den er verhindern soll | ✅ |
| `data_manager.py:1332` `load_live_trade_history` | Mongo-Fehler → leere Historie → `record_live_trade` schreibt Ein-Trade-Historie zurück | ✅ |
| `risk/slot_eviction_runtime.py:478-502` | permissive Defaults vor `try`; Ausfall → 4 von 6 Must-Fail-Gates weg | ✅ |
| `risk/slot_eviction_runtime.py:543` | `plan_for_blocked_entry` ohne `prices=` → `gain_pct = 0.0` für jeden Kandidaten | ✅ |
| `risk/slot_eviction_runtime.py:392-400` | `mark_price` nie geschrieben, `svc.market.get_price` existiert nicht, `or 1.0` | ✅ |
| `services/order_service.py:433` `_dual_write_v2` | `except: pass` ohne Log | ✅ |
| `storage/tenant_registry.py:44-49` `_decrypt` | `(InvalidToken, Exception)` → `""` → leere Credentials | ✅ |

## 5. Arbeitsliste für #299 — alle C und C? (112)

| # | Stelle | Funktion | Kl. | Fix |
|---|---|---|---|---|
| 2 | `execution/gate_adapter.py:115` | `execute` | C | Netzwerk-/Timeout-Fehler (`ccxt.NetworkError`, `RequestTimeout`, `ExchangeNotAvailable`) und Fehler nach dem Exchange-Call vom deterministischen Reject trennen; Status `unknown`/`needs_reconcile` + log ERROR + Reconcile (`fetch_my_trades`/`fetch_order`) erzwingen, bevor weiter gehandelt wird. |
| 3 | `execution/gate_adapter.py:169` | `_fetch_base_balance` | C | log ERROR; `None` zurückgeben und im Aufrufer den Sell abbrechen bzw. retryen, statt „Fehler" und „Guthaben 0" auf denselben Wert abzubilden. |
| 7 | `risk/position_capacity.py:355` | `count_open_book_memory_signals` | C (niedrig) | Fehler loggen (rate-limited) und nicht auflösbare Positionen konservativ zählen (wie `soft_block`) bzw. ein `incomplete`-Flag zurückgeben, damit zumindest das positive `prefer`-Adjustment entfällt. |
| 14 | `risk/risk_manager.py:270` | `_evaluate_impl` | C | log ERROR + `approved=False` (`code="stablecoin_check_error"`). |
| 15 | `risk/risk_manager.py:285` | `_evaluate_impl` | C? | fail-closed `hop_short = True` + log ERROR (Lot nicht adoptieren). |
| 16 | `risk/risk_manager.py:305` | `_evaluate_impl` | C | log ERROR + `approved=False`. |
| 17 | `risk/risk_manager.py:330` | `_evaluate_impl` | C | log ERROR + `approved=False`. |
| 18 | `risk/risk_manager.py:350` | `_evaluate_impl` | C | log ERROR + `approved=False`. |
| 20 | `risk/risk_manager.py:380` | `_evaluate_impl` | C | log ERROR + `approved=False` (Fusion-Ausfall ≠ „kein Block"). |
| 22 | `risk/risk_manager.py:423` | `_evaluate_impl` | C | log ERROR + `approved=False`. |
| 23 | `risk/risk_manager.py:438` | `_evaluate_impl` | C? | log ERROR; nicht auflösbarer Tenant → Buy ablehnen statt Fremd-Store lesen. |
| 25 | `risk/risk_manager.py:481` | `_evaluate_impl` | C | log ERROR + `approved=False`. |
| 26 | `risk/risk_manager.py:488` | `_evaluate_impl` | C | log ERROR + `approved=False`. |
| 27 | `risk/risk_manager.py:517` | `_evaluate_impl` | C | log ERROR + `approved=False`. |
| 28 | `risk/risk_manager.py:536` | `_evaluate_impl` | C | log ERROR + `approved=False`. |
| 29 | `risk/risk_manager.py:564` | `_evaluate_impl` | C | log ERROR + Suffix „eviction_error"; Ausführungsfehler nie stumm schlucken. |
| 30 | `risk/risk_manager.py:605` | `_evaluate_impl` | C? | log ERROR + konservativeren der beiden Beträge nehmen (bzw. DCA überspringen). |
| 36 | `risk/risk_manager.py:920` | `_sensor_reentry_cooloff_blocked` | C | log ERROR + `RiskDecision(approved=False, code="sensor_reentry_check_error")` zurückgeben. |
| 37 | `risk/risk_manager.py:1119` | `_market_bias_for_cash` | C | log ERROR + konservative Werte (`size_mult` 0-nah bzw. `block_buys=True`) oder Exception an den Aufrufer (§1b: `market_oracle_risk_fail_open`). |
| 38 | `risk/risk_manager.py:1127` | `_process_uptime_sec` | C | log ERROR + 0.0 zurückgeben (= „gerade gestartet", fail-closed). |
| 39 | `risk/risk_manager.py:1142` | `_open_book_memory_counts` | C | log ERROR + Kapazität auf Basis/`min_floor` klemmen statt neutral zählen. |
| 40 | `risk/risk_manager.py:1186` | `_resolve_position_capacity` | C? | log ERROR; `_base_usdt_cap()` ist ein reiner Config-Read, ein Fehler dort gehört gemeldet. |
| 42 | `risk/risk_manager.py:1381` | `_dynamic_size` | C | log ERROR + `approved=False` bzw. `global_mult = 0.0` (Fusion-Ausfall = kein Deployment). |
| 43 | `risk/risk_manager.py:1406` | `_dynamic_size` | C | log ERROR + `coin_bias = min(1.0, …)` bzw. Buy ablehnen. |
| 44 | `risk/risk_manager.py:1428` | `_dynamic_size` | C | log ERROR + Multiplikatoren konservativ (< 1.0) belassen oder Entry verschieben. |
| 48 | `risk/risk_manager.py:1569` | `_fill_sell_amount_from_open_lot` | C | log ERROR + `order` unverändert zurückgeben, damit „No amount to sell" greift. |
| 49 | `risk/risk_manager.py:1644` | `_resolve_sell_order` | C? | log ERROR + im Zweifel den Teilverkauf unverändert lassen (`return order`). |
| 50 | `risk/risk_manager.py:1685` | `_partial_sell_blocked` | C? | log ERROR + strengere der beiden Param-Sätze verwenden. |
| 57 | `risk/risk_manager.py:1909` | `_trade_cooldown_blocked` | C | log ERROR + bei Parse-Fehler blockieren (`return True, "cooldown_ts_unreadable"`). |
| 58 | `risk/risk_manager.py:1919` | `_trade_cooldown_blocked` | C | log ERROR + `return True, "trade_cooldown_ts_unreadable"`. |
| 59 | `risk/risk_manager.py:2018` | `_rebuy_after_sell_blocked` | C | log ERROR + konservativstes Regime (`CRASH`) annehmen. |
| 60 | `risk/risk_manager.py:2025` | `_rebuy_after_sell_blocked` | C | log ERROR + Memory-Ausfall mit dem strengsten Multiplikator behandeln. |
| 62 | `risk/risk_manager.py:2180` | `_iter_daily_filled_orders` | C | log ERROR + Order mitzählen (fail-closed) statt verwerfen. |
| 63 | `risk/slot_eviction.py:554` | `plan_slot_eviction` | C | Bei Exception `continue` (= als gesperrt behandeln) + log ERROR; zusätzlich `attach_lock_from_ledger` wie im Sell-Pfad aufrufen. Restschutz: der nachgelagerte `auto_sell_blocked` fängt die Default-Modi (`no_auto_sell`+`no_evict`), **nicht** aber ein reines `no_evict`-Lock. |
| 66 | `risk/slot_eviction_runtime.py:112` | `_hours_since` | C | Bei Parse-Fehler `0.0` (oder `None` mit expliziter Behandlung) + log WARNING zurückgeben; auch der `if not iso_ts: return 999.0`-Zweig gehört auf einen konservativen Wert. |
| 68 | `risk/slot_eviction_runtime.py:174` | `build_victim_candidates` | C (niedrig–mittel) | log + „Profil nicht verfügbar" schützend behandeln (Kandidat überspringen) statt als neutrales Profil; in der Praxis von #78 maskiert, weil `_gp` bereits schluckt. |
| 70 | `risk/slot_eviction_runtime.py:233` | `build_victim_candidates` | C | `memory_keep_score(None, risk_config=…)` (Config-Default) verwenden statt der Literalzahl — oder den Plan bei fehlendem Entry-Profil ganz verwerfen. |
| 72 | `risk/slot_eviction_runtime.py:399` | `execute_eviction_sell` | C | `or 1.0` entfernen, echte Preisquelle verwenden (`price_fetcher`/`prices`-Dict), bei fehlendem gültigem Preis die Eviction abbrechen + log ERROR. Aggravierend: `plan_for_blocked_entry` wird ohne `prices=` aufgerufen, deshalb ist auch `gain_pct` aller Kandidaten 0.0. |
| 73 | `risk/slot_eviction_runtime.py:420` | `execute_eviction_sell` | C | log ERROR mit Traceback; Fehler vor dem Order-Call von Fehlern danach trennen und letztere als `unknown/needs_reconcile` melden statt als `ok=False`. |
| 75 | `risk/slot_eviction_runtime.py:486` | `try_slot_eviction_on_max_open` | C | log ERROR und Eviction abbrechen (`return None, ""`), wenn der Markt-Bias unbekannt ist — unbekanntes Regime darf nicht als NEUTRAL gelten. |
| 76 | `risk/slot_eviction_runtime.py:502` | `try_slot_eviction_on_max_open` | C | log ERROR und Eviction abbrechen, wenn das Coin-Memory nicht erreichbar ist. |
| 77 | `risk/slot_eviction_runtime.py:524` | `try_slot_eviction_on_max_open` | C (niedrig) | log WARNING und „Uptime unbekannt" als `warmup=True` behandeln. |
| 78 | `risk/slot_eviction_runtime.py:532` | `_gp` | C | Fehler einmalig loggen und den Eviction-Plan verwerfen, wenn die Profilquelle ausfällt; `get_profile` muss „Ausfall" von „kein Profil" unterscheidbar melden. |
| 86 | `services/order_service.py:433` | `_dual_write_v2` | C | Mindestens `log(..., "ERROR")` + Zähler; sobald `ORDER_LEDGER_V2_READS=1`, muss ein fehlgeschlagener v2-Write den Store als *degraded* markieren, damit Tages-Reads auf den Blob zurückfallen statt einen unvollständigen Tag zu melden. |
| 96 | `services/portfolio_service.py:228` | `execute_cover` | C (niedrig) | log WARNING; den Cover **nicht** abbrechen (ein blockierter Cover wäre der schlimmere Fehler), stattdessen konservative Fallback-Dauer verwenden und den PnL als approximativ markieren. |
| 97 | `services/portfolio_service.py:237` | `execute_cover` | C (niedrig) | Mindestens log ERROR; Funding-Fehler im Trade-Record als Flag hinterlegen statt stillschweigend `fund = 0` anzunehmen. |
| 98 | `strategies/positions.py:387` | `load_positions` | C | Fail-closed: Exception an den Aufrufer weiterreichen (oder alten Store-Inhalt nicht verwerfen) und den Handelszyklus für diesen Tenant/Scope aussetzen; `store.clear()` erst nach erfolgreichem Load anwenden. |
| 99 | `strategies/positions.py:472` | `_do_save_positions` | C | Log auf ERROR; bei fehlgeschlagenem Read den Flush abbrechen statt ein aus einem Fehl-Read abgeleitetes Dokument zu schreiben. |
| 100 | `strategies/positions.py:476` | `_do_save_positions` | C | `_do_save_positions`/`flush_positions` auf `bool` umstellen, Fehler an den Aufrufer melden und im Trade-Pfad darauf reagieren (Retry/Alarm/Handelspause). |
| 102 | `strategies/positions.py:918` | `update_position` | C | Fehler melden statt Default einfrieren: `dca_max_rounds` nicht setzen, wenn die Tier-Parameter nicht aufgelöst werden konnten, und den Fehler loggen (ERROR). |
| 103 | `strategies/positions.py:934` | `update_position` | C | Log ERROR; Position als "peak nicht re-anchored" markieren, damit der Trail-Stop-Pfad das erkennt, statt auf einem stale Peak zu handeln. |
| 104 | `strategies/positions.py:949` | `update_position` | C | Log ERROR + Marker auf der Position; Exit-Logik muss den fehlenden Epoch/Hold erkennen. |
| 105 | `strategies/positions.py:1064` | `update_position` | C | Fail-closed: ohne aufgelöste Strategy-Params keine Mengen-/Tier-/Ladder-Fortschreibung — Fehler an den Aufrufer melden statt Defaults in die Positionsbuchhaltung zu schreiben. |
| 108 | `data_manager.py:137` | `_should_use_mongo_for_tenant_config` | C | log ERROR + Exception propagieren bzw. Tri-State; niemals „Speichern OK" melden, wenn das Backend unbekannt ist |
| 112 | `data_manager.py:234` | `load_watchlist` | C? | zusammen mit #108 fixen bzw. ersatzlos entfernen |
| 113 | `data_manager.py:242` | `load_watchlist` | C? | log ERROR; Fehlerfall vom „leer"-Fall trennen und den Zyklus für den Tenant überspringen |
| 114 | `data_manager.py:260` | `load_watchlist` | C | log ERROR + raise (bzw. Sentinel), damit die RMW-Aufrufer abbrechen |
| 115 | `data_manager.py:273` | `save_watchlist` | C? | zusammen mit #108 fixen |
| 116 | `data_manager.py:280` | `save_watchlist` | C | log ERROR; Aufrufer muss den Bool prüfen und die Telegram-Erfolgsmeldung unterdrücken |
| 117 | `data_manager.py:288` | `save_watchlist` | C | Aufrufer fail-closed machen (Fehlermeldung statt ✅) |
| 118 | `data_manager.py:329` | `save_dry_run_expansion` | C | log ERROR + Rückgabewert an den Aufrufern auswerten |
| 121 | `data_manager.py:407` | `save_dry_run_overlay` | C | log ERROR + Aufrufer fail-closed |
| 123 | `data_manager.py:428` | `save_cmc_trending_overlay` | C | log ERROR + Aufrufer fail-closed |
| 124 | `data_manager.py:610` | `build_merged_watchlist_coins` | C | log ERROR; in `enforce` den Zyklus/Coin überspringen statt ungefiltert weiterzugeben |
| 125 | `data_manager.py:628` | `load_effective_watchlist` | C? | log ERROR statt DEBUG; Cap auch im Fallback anwenden |
| 126 | `data_manager.py:664` | `load_trade_watchlist` | C | log ERROR; bei aktivem Split lieber leere Trade-Liste (Zyklus überspringen) als das volle Observe-Set |
| 127 | `data_manager.py:690` | `_load_default_config_from_disk` | C (hoch) | log ERROR + raise; der Bot darf ohne lesbare Basis-Config nicht handeln |
| 128 | `data_manager.py:720` | `_load_tenant_config_body` | C (hoch) | log ERROR + raise bzw. Tenant-Zyklus abbrechen; niemals still auf die Operator-Config zurückfallen |
| 129 | `data_manager.py:738` | `load_config` | C? | zusammen mit #108 fixen |
| 130 | `data_manager.py:796` | `save_config` | C? | zusammen mit #108 fixen |
| 131 | `data_manager.py:807` | `save_config` | C | log ERROR; Aufrufer (Onboarding) muss abbrechen statt „Bot ist aktiv" zu melden |
| 132 | `data_manager.py:816` | `save_config` | C | eigenes log ERROR; verbleibende Aufrufer auf Bool-Prüfung umstellen |
| 133 | `data_manager.py:827` | `load_x_accounts` | C? | log ERROR + raise, damit die RMW-Pfade abbrechen |
| 138 | `data_manager.py:949` | `_load_trade_history_json` | C (hoch) | log ERROR + raise; Aufrufer muss den Zyklus abbrechen statt auf Phantom-Cash zu handeln |
| 139 | `data_manager.py:959` | `_save_trade_history_json` | C | log ERROR; `record_trade`/`record_live_trade` müssen den Fehler melden/werfen |
| 140 | `data_manager.py:975` | `load_trade_history_document` | C? | Refuse-Guard auf **jeden** Scope ausweiten, dessen konfiguriertes Backend `mongo` ist |
| 142 | `data_manager.py:1045` | `save_trade_history_document` | C | Aufrufer fail-closed machen (raise bzw. Zyklus abbrechen) |
| 143 | `data_manager.py:1298` | `_reconcile_live_trade_sources` | C? | log ERROR; bei Fehler `changed=False` zurückgeben, damit nichts Halbfertiges gespeichert wird |
| 144 | `data_manager.py:1313` | `_load_live_trade_history_json` | C (hoch) | log ERROR + raise |
| 145 | `data_manager.py:1332` | `load_live_trade_history` | C (hoch) | log ERROR + raise (analog `_should_refuse_demo_json_fallback`); niemals eine leere Historie zurückschreiben |
| 147 | `data_manager.py:1528` | `_load_orders_json` | C (hoch) | log ERROR + raise; leeres Buch nur bei tatsächlich fehlender Datei |
| 148 | `data_manager.py:1540` | `_save_orders_json` | C (hoch) | log ERROR; `OrderService._save` muss bei `False` werfen bzw. der Aufrufer den Zyklus abbrechen |
| 149 | `data_manager.py:1552` | `load_orders` | C (hoch) | Refuse-Guard auf jeden mongo-konfigurierten Scope ausweiten |
| 150 | `data_manager.py:1584` | `_reject_demo_mongo_orders_downgrade` | C (hoch) | log ERROR + `return True` (fail-closed): im Zweifel den Schreibvorgang blocken |
| 151 | `data_manager.py:1602` | `save_orders` | C | Aufrufer fail-closed machen (raise / Zyklus abbrechen) |
| 152 | `data_manager.py:1622` | `_load_positions_json` | C | log ERROR + raise; Save-Pfad muss bei unlesbarem Positions-Doc abbrechen statt Locks zu verlieren |
| 153 | `data_manager.py:1634` | `_save_positions_json` | C | log ERROR; Aufrufer müssen den Fehler eskalieren, nicht nur loggen |
| 154 | `data_manager.py:1651` | `load_positions_document` | C | Refuse-Guard auf jeden mongo-konfigurierten Scope ausweiten |
| 155 | `data_manager.py:1678` | `save_positions_document` | C | Aufrufer fail-closed machen |
| 156 | `data_manager.py:1696` | `load_strategy_backtest_results` | C? | log ERROR + raise; bei unlesbarer Datei kein Auto-Tuning-Job starten |
| 157 | `data_manager.py:1707` | `save_strategy_backtest_results` | C? | log ERROR + Bool im Worker auswerten |
| 170 | `storage/grid_plan_store.py:67` | `load_grid_plans_document` | C | Fehler melden statt `empty`: eigenes Sentinel/Exception für "Load fehlgeschlagen"; `save_grid_plan` darf nach einem gescheiterten Load nicht speichern; Log auf ERROR. |
| 171 | `storage/grid_plan_store.py:96` | `save_grid_plans_document` | C | Rückgabewert in `_persist_plan` auswerten (Retry/Alarm, Plan nicht als persistiert markieren); Log auf ERROR. |
| 172 | `storage/grid_plan_store.py:122` | `load_grid_plan` | C? | Mindestens loggen; "Read fehlgeschlagen" von "nicht vorhanden" unterscheiden, damit der Grid-Aufbau nicht auf einem Lesefehler neu zentriert. |
| 174 | `storage/ledger_router.py:101` | `_atomic_write` | C | Exception durchreichen (der Logger sitzt schon in `atomic_write_json`) oder hier ERROR loggen **und** sicherstellen, dass jeder `save_*`-Aufrufer auf `False` reagiert. |
| 175 | `storage/ledger_router.py:137` | `load_orders` (Json)` | C | Lesefehler melden statt leeres Dokument; "Datei fehlt" (legitim leer) und "Datei kaputt" strikt trennen. |
| 176 | `storage/ledger_router.py:156` | `load_positions` (Json)` | C | Wie #175: Fehler melden, "fehlt" ≠ "kaputt". |
| 177 | `storage/ledger_router.py:182` | `load_trade_history` (Json)` | C | Fehler melden; ein Startguthaben darf niemals aus einem Exception-Handler stammen. |
| 178 | `storage/ledger_router.py:229` | `load_orders` (DualWrite)` | C? | Fallback nur mit Plausibilitätsprüfung (Datei existiert, Dokument nicht leer, Alter unter Schwelle), sonst fail-closed. |
| 179 | `storage/ledger_router.py:237` | `save_orders` (DualWrite)` | C? | Bei Verdrahtung: `False` muss den Trade-/Flush-Pfad abbrechen; alternativ raisen, damit niemand den Status ignorieren kann. |
| 180 | `storage/ledger_router.py:245` | `load_positions` (DualWrite)` | C? | Wie #178. |
| 181 | `storage/ledger_router.py:253` | `save_positions` (DualWrite)` | C? | Wie #179. |
| 182 | `storage/ledger_router.py:261` | `load_trade_history` (DualWrite)` | C? | Wie #178, zusätzlich: Cash-Dokument niemals aus einem Default konstruieren. |
| 183 | `storage/ledger_router.py:269` | `save_trade_history` (DualWrite)` | C? | Wie #179. |
| 187 | `storage/order_ledger_v2.py:91` | `get_order_ledger_v2` | C | ERROR loggen; ohne Indizes den Store nicht als "mongo" ausliefern (oder `degraded=True` markieren). Hinweis: der Handler toleriert auch bewusst `assert_safe_dev_db_mutation` unter pytest — diesen Fall gezielt abfangen statt `except Exception`. |
| 188 | `storage/order_ledger_v2.py:110` | `get_order_ledger_v2` | C | ERROR loggen, Fallback als degradiert markieren und v2-Reads für diesen Prozess hart abschalten; periodischen Reconnect-Versuch statt Pin auf Prozesslebenszeit. |
| 195 | `storage/tenant_meta_store.py:37` | `load_tenant_config_body` | C | Fail-closed: Lesefehler propagieren; ein Tenant ohne gelesene Config darf keinen Zyklus/keine Order fahren — "nicht gespeichert" (`None`) und "Read fehlgeschlagen" strikt trennen. |
| 196 | `storage/tenant_meta_store.py:61` | `save_tenant_config` | C? | Log auf ERROR; alle `save_config`-Aufrufer auf Auswertung prüfen, insbesondere Onboarding und Telegram-Settings. |
| 197 | `storage/tenant_meta_store.py:83` | `load_tenant_watchlist` | C | Fail-closed: Lesefehler propagieren statt `[]`; `load_watchlist` darf bei einem Fehler nicht auf die Default-Datei zurückfallen. |
| 198 | `storage/tenant_meta_store.py:99` | `save_tenant_watchlist` | C | Log auf ERROR; Rückgabewert in allen `save_watchlist`-Aufrufern auswerten und die Erfolgsmeldung erst nach bestätigtem Write senden. |
| 200 | `storage/tenant_registry.py:220` | `find_tenant_by_owner_chat_id` | C | Lesefehler von "nicht gefunden" trennen (Exception oder Sentinel); `link_tenant_owner_chat` muss bei einem Lookup-Fehler die Verknüpfung verweigern. |
| 204 | `services/trading_service.py:283` | `_execute_order_locked` | C (niedrig) | log ERROR statt DEBUG und die verwaiste Ledger-Order auf `failed` setzen; **nicht** raisen, der SELL ist bereits ausgeführt. |

## 6. Vollständige Tabelle

| # | Stelle | Funktion | Klasse | Begründung | Fix | Heuristik abw.? |
|---|---|---|---|---|---|---|
| 1 | `execution/gate_adapter.py:64` | `_fetch_usdt_balance` | A | `0.0` führt im einzigen Order-Aufrufer (`_execute_buy`: `if balance < usdt: return executed=False`) zur Ablehnung, ist also fail-closed (Self-DoS), sonst nur Telegram-Anzeige. | log ERROR statt WARNING; besser `None` zurückgeben, damit „Balance unbekannt" ≠ „kein Guthaben"; Zeile 68 (`self._last_api_error = ""`) ist toter Code nach `return` → Fehlerhinweis wird nie gelöscht. | ja: `C?` → **A** |
| 2 | `execution/gate_adapter.py:115` | `execute` | C | Der `try` umschließt sowohl `create_market_buy/sell_order` als auch `_sync_local_ledger`, aber jeder Fehler wird als `executed=False` gebucht → `link_execution_result` setzt Ledger-Status `"failed"`, obwohl die Order bei `RequestTimeout` (oder bei einem Fehler *nach* dem Fill) an der Börse ausgeführt sein kann. | Netzwerk-/Timeout-Fehler (`ccxt.NetworkError`, `RequestTimeout`, `ExchangeNotAvailable`) und Fehler nach dem Exchange-Call vom deterministischen Reject trennen; Status `unknown`/`needs_reconcile` + log ERROR + Reconcile (`fetch_my_trades`/`fetch_order`) erzwingen, bevor weiter gehandelt wird. | ja: `A?` → **C** |
| 3 | `execution/gate_adapter.py:169` | `_fetch_base_balance` | C | `0.0` ist nicht von „keine Coins an der Börse" unterscheidbar, dadurch greift im Aufrufer die Reduce-Sperre `if exchange_balance > 0 and amount > exchange_balance: amount = exchange_balance` nicht mehr und der ungeprüfte Ledger-Betrag geht an die Börse. | log ERROR; `None` zurückgeben und im Aufrufer den Sell abbrechen bzw. retryen, statt „Fehler" und „Guthaben 0" auf denselben Wert abzubilden. | nein (`C?`) |
| 4 | `execution/gate_adapter.py:200` | `_validate_sell_amount` | A | Ausgelassen wird nur der lokale `min_amount`/`min_cost`-Vorabcheck, den die Börse selbst erzwingt; der Handler kann die Menge nicht erhöhen, und der Pfad (SELL) reduziert Exposure. | log ERROR statt WARNING und Grund in die `TradeResult.message` durchreichen; beachten: `amount_to_precision` im Handler scheitert bei fehlgeschlagenem `load_markets` meist ebenfalls → landet dann in #2. | nein (`A?`) |
| 5 | `risk/moderate_deploy.py:131` | `size_boost_for_regime` | A | `1.0` ist der Boden des Wertebereichs (`if boost < 1.0: boost = 1.0`) und der Aufrufer wendet nur `if md_boost > 1.0: total *= md_boost` an → der Fehlerpfad kann nie eine größere Size erzeugen als der Erfolgsfall. | Nur Beobachtbarkeit: log WARNING, damit ein dauerhaft kaputtes Config-/Fusion-Setup das Feature nicht stumm abschaltet. | ja: `C?` → **A** |
| 6 | `risk/moderate_deploy.py:148` | `effective_max_total_multiplier` | A | Der Erfolgspfad liefert `max(base_max, md_max) >= base_max`, `base_max` im Handler ist also die *engste* mögliche Obergrenze. | log WARNING; sonst unverändert lassen. | ja: `C?` → **A** |
| 7 | `risk/position_capacity.py:355` | `count_open_book_memory_signals` | C (niedrig) | `continue` verwirft die Position aus den `soft_block`/`toxic`-Zählern, die in `_memory_adj` negative Slot-Adjustments (bis −4) liefern → `max_open_eff` bleibt höher und ein Buy, der an „Max open positions" gescheitert wäre, wird freigegeben. | Fehler loggen (rate-limited) und nicht auflösbare Positionen konservativ zählen (wie `soft_block`) bzw. ein `incomplete`-Flag zurückgeben, damit zumindest das positive `prefer`-Adjustment entfällt. | nein (`C?`) |
| 8 | `risk/risk_manager.py:94` | `evaluate` | A | Umschließt nur `log_risk_reject` (durables risk_rejects.jsonl); die Entscheidung ist bereits gefallen und wird unverändert zurückgegeben. | `log(..., "WARN")` statt stillem `pass`. | ja (C? → A) |
| 9 | `risk/risk_manager.py:146` | `_evaluate_impl` | A | Handler gibt selbst `approved=False, code="side_check_error"` zurück — bereits fail-closed. | zusätzlich `log(..., "ERROR")`, sonst unverändert. | ja (C? → A) |
| 10 | `risk/risk_manager.py:179` | `_evaluate_impl` | A | Loggt ERROR und endet in `approved=False, code="position_lock_check_error"` — fail-closed, Vorbildmuster für alle C-Stellen. | keiner. | nein |
| 11 | `risk/risk_manager.py:187` | `_evaluate_impl` | A | Verschachtelter Handler nur um `from logger import log` / `log(...)`; das Deny folgt danach unbedingt. | keiner. | ja (C? → A) |
| 12 | `risk/risk_manager.py:238` | `_evaluate_impl` | A | Gleiches Muster wie 179 für den DCA-Position-Lock: ERROR-Log + `approved=False`. | keiner. | nein |
| 13 | `risk/risk_manager.py:246` | `_evaluate_impl` | A | Nur der Logging-Aufruf innerhalb des Fail-Closed-Handlers. | keiner. | ja (C? → A) |
| 14 | `risk/risk_manager.py:270` | `_evaluate_impl` | C | `try` enthält das Stablecoin-Deny (`code="stablecoin_blocked"`, `size_multiplier=0.0`); `pass` lässt den Buy durchfallen. | log ERROR + `approved=False` (`code="stablecoin_check_error"`). | nein |
| 15 | `risk/risk_manager.py:285` | `_evaluate_impl` | C? | Fallback `hop_short = False` adoptiert eine evtl. Short-Lot als Long (`has_position=True`) und überspringt damit **alle** `not has_position`-Guards; praktisch durch den One-Way-Rail (Z. 118–146) abgeschirmt. | fail-closed `hop_short = True` + log ERROR (Lot nicht adoptieren). | – (Heuristik `?`) |
| 16 | `risk/risk_manager.py:305` | `_evaluate_impl` | C | `try` enthält das Deny `code="correlated_tier_selloff"`; `pass` → Buy während Tier-Selloff erlaubt. | log ERROR + `approved=False`. | nein |
| 17 | `risk/risk_manager.py:330` | `_evaluate_impl` | C | `try` enthält das Deny `code="universe_trade_cap"`; `pass` → Buy außerhalb des Trade-Universums erlaubt. | log ERROR + `approved=False`. | nein |
| 18 | `risk/risk_manager.py:350` | `_evaluate_impl` | C | `try` enthält das Deny `code="gainer_chase_guard"`; `pass` → Chase-Guard fällt aus. | log ERROR + `approved=False`. | nein |
| 19 | `risk/risk_manager.py:368` | `_evaluate_impl` | A | Nur `note_buy_blocked(...)` (Observability); das `market_block`-Deny steht hinter dem Handler und feuert weiterhin. | keiner. | ja (C? → A) |
| 20 | `risk/risk_manager.py:380` | `_evaluate_impl` | C | `try` enthält das Deny `code="market_block"` (CRASH/Warmup-Sperre); `pass` → globale Buy-Sperre wird stillschweigend aufgehoben. | log ERROR + `approved=False` (Fusion-Ausfall ≠ „kein Block"). | nein |
| 21 | `risk/risk_manager.py:411` | `_evaluate_impl` | A | Nur die TTL-Parse von `soft_block_until`; bei Fehler bleibt `block_this = True`, die Fehlerrichtung ist restriktiv. | log WARN, damit ein kaputter Timestamp nicht ewig blockt. | ja (C? → A) |
| 22 | `risk/risk_manager.py:423` | `_evaluate_impl` | C | `try` enthält das Deny `code="coin_memory_soft_block"`; `pass` → Memory-Sperre fällt aus. | log ERROR + `approved=False`. | nein |
| 23 | `risk/risk_manager.py:438` | `_evaluate_impl` | C? | Fallback `tid = "default"` lässt den WQE-Gate stillschweigend den Quality-Store eines anderen Tenants lesen; Richtung meist restriktiv (fehlende Scores blocken in `enforce`), aber nicht garantiert. | log ERROR; nicht auflösbarer Tenant → Buy ablehnen statt Fremd-Store lesen. | – (Heuristik `?`) |
| 24 | `risk/risk_manager.py:473` | `_evaluate_impl` | A | Nur WQE-Metrik/Event-Log; das Deny folgt unbedingt danach — **verschluckt aber den NameError `mode` (Z. 469 ist nie definiert), d. h. `log_buy_block` läuft nie**. | `mode = wqe_mode(raw)` in Z. 433 zuweisen; Handler auf log WARN umstellen. | ja (C? → A) |
| 25 | `risk/risk_manager.py:481` | `_evaluate_impl` | C | `try` enthält das Deny `code="watchlist_quality"`; `pass` → Quality-Gate fällt komplett aus. | log ERROR + `approved=False`. | nein |
| 26 | `risk/risk_manager.py:488` | `_evaluate_impl` | C | `try` gibt die Deny-Decision aus `_sensor_reentry_cooloff_blocked` zurück (`return cool`); `pass` → Re-Entry-Cooloff fällt aus (doppelt fail-open mit #36). | log ERROR + `approved=False`. | nein |
| 27 | `risk/risk_manager.py:517` | `_evaluate_impl` | C | `try` enthält das Deny `code="venue_liquidity_block"`; `pass` → Buy in dünnen Markt möglich. | log ERROR + `approved=False`. | nein |
| 28 | `risk/risk_manager.py:536` | `_evaluate_impl` | C | `try` enthält das Deny `code="macro_calendar_block"`; `pass` → Makro-Hardblock fällt aus. | log ERROR + `approved=False`. | nein |
| 29 | `risk/risk_manager.py:564` | `_evaluate_impl` | C | `try` ruft `try_slot_eviction_on_max_open`, das über `execute_eviction_sell` einen **echten Verkauf** auslöst; ein Fehler danach wird verschluckt, die Reject-Message verliert den Eviction-Status. | log ERROR + Suffix „eviction_error"; Ausführungsfehler nie stumm schlucken. | nein |
| 30 | `risk/risk_manager.py:605` | `_evaluate_impl` | C? | `try` ersetzt `params` durch die tier-eingefrorenen Strategy-Params für die DCA-Größe; bei Fehler wird still generisch gesized (`dca_cfg["fixed_usdt"]` kann steigen), gedeckelt nur durch `ticket_cap`. | log ERROR + konservativeren der beiden Beträge nehmen (bzw. DCA überspringen). | nein |
| 31 | `risk/risk_manager.py:653` | `_evaluate_impl` | A | `cash_pct = None` lässt nur den Cash-Rich-Extra-Boost weg; `size_boost_for_regime` liefert immer ≥ 1.0, die Größe wird dadurch kleiner, nie größer. | optional log DEBUG. | – (Heuristik `?`) |
| 32 | `risk/risk_manager.py:665` | `_evaluate_impl` | A | Der `try` **erhöht** nur (`md_boost > 1.0`); bei Fehler bleibt `sized` auf Basisgröße — konservativ. | optional log WARN. | ja (C? → A) |
| 33 | `risk/risk_manager.py:676` | `_evaluate_impl` | A | `sensor_cfg = {}` → `ignore_aggression_boost` defaultet auf `True`, d. h. Sensor-Größe bleibt gelockt (keine Inflation). | optional log WARN; `max_usdt_absolute` entfällt dann allerdings ebenfalls. | – (Heuristik `?`) |
| 34 | `risk/risk_manager.py:852` | `status_summary` | A | Rein Anzeige: Telegram `/risk`, `/positions`, Morning-Briefing, Sniper-`snapshot_cash` (nur Cash-Keys); das echte Gate ruft `_resolve_position_capacity` direkt in `_evaluate_impl`. | log WARN, damit ein Capacity-Fehler nicht als „deaktiviert" erscheint. | – (Heuristik `?`) |
| 35 | `risk/risk_manager.py:907` | `_sensor_reentry_cooloff_blocked` | A | Nur Parse-Fallback für `last_loss_at`; `start` bleibt `loss_dt`, der Cooloff wird weiterhin ausgewertet (Richtung restriktiv). | log WARN. | ja (C? → A) |
| 36 | `risk/risk_manager.py:920` | `_sensor_reentry_cooloff_blocked` | C | `return None` heißt „nicht blockiert", obwohl der `try` das Deny `code="sensor_reentry_cooloff"` enthält. | log ERROR + `RiskDecision(approved=False, code="sensor_reentry_check_error")` zurückgeben. | nein |
| 37 | `risk/risk_manager.py:1119` | `_market_bias_for_cash` | C | Neutraler Fallback verwirft `block_buys`, setzt `size_mult` auf 1.0 und `regime` auf `None` → in `_resolve_position_capacity` entfällt `regime_adj` CRASH **−12 Slots**, in `_evaluate_cash_policy` steigen Floor/Size. | log ERROR + konservative Werte (`size_mult` 0-nah bzw. `block_buys=True`) oder Exception an den Aufrufer (§1b: `market_oracle_risk_fail_open`). | nein |
| 38 | `risk/risk_manager.py:1127` | `_process_uptime_sec` | C | `None` führt in `_warmup_adj` zu `return 0`, d. h. der Restart-Warmup-Abzug (`restart_warmup_adj = −6` Slots) entfällt genau dann, wenn der Store gerade nicht antwortet. | log ERROR + 0.0 zurückgeben (= „gerade gestartet", fail-closed). | nein |
| 39 | `risk/risk_manager.py:1142` | `_open_book_memory_counts` | C | `(0,0,0)` neutralisiert `_memory_adj`, d. h. die Kapazitätsverengung für offene soft_block/toxic-Lots (bis `memory_adj_cap` ±4) entfällt. | log ERROR + Kapazität auf Basis/`min_floor` klemmen statt neutral zählen. | nein |
| 40 | `risk/risk_manager.py:1186` | `_resolve_position_capacity` | C? | Ohne `avg_entry_usdt` liefert `_cash_spendable_adj` `afford_adj = 0`, die Verengung `cash_low_afford_adj = −2` entfällt (milde Kapazitätserhöhung). | log ERROR; `_base_usdt_cap()` ist ein reiner Config-Read, ein Fehler dort gehört gemeldet. | nein |
| 41 | `risk/risk_manager.py:1379` | `_dynamic_size` | A | Nur `note_size_cut(...)` (Observability); `global_mult` ist zu dem Zeitpunkt schon gesetzt. | keiner. | ja (C? → A) |
| 42 | `risk/risk_manager.py:1381` | `_dynamic_size` | C | Bei Fehler bleibt `global_mult = 1.0` (CRASH-Nullung `total = 0.0` entfällt) **und** `global_regime = None` → `size_boost_for_regime` greift auf `size_boost_default = 1.35` zurück, die Order wird also 35 % **größer** statt kleiner. | log ERROR + `approved=False` bzw. `global_mult = 0.0` (Fusion-Ausfall = kein Deployment). | nein |
| 43 | `risk/risk_manager.py:1406` | `_dynamic_size` | C | `coin_bias = 1.0` verwirft den Memory-Size-Bias, der laut `CoinProfile` bis auf **0.5** clamped — Positionsgröße bis zu 2× über Soll auf genau den Coins, die das Memory abgestraft hat. | log ERROR + `coin_bias = min(1.0, …)` bzw. Buy ablehnen. | – (Heuristik `?`) |
| 44 | `risk/risk_manager.py:1428` | `_dynamic_size` | C | `calendar_mult`/`session_mult`/`pm_mult` bleiben 1.0 → die Makro-De-Risking-Multiplikatoren fallen genau vor High-Impact-Events weg (volle Größe). | log ERROR + Multiplikatoren konservativ (< 1.0) belassen oder Entry verschieben. | nein |
| 45 | `risk/risk_manager.py:1459` | `_dynamic_size` | A | Wie #31: `cash_pct = None` streicht nur den Cash-Rich-Extra-Mult, Boost wird kleiner. | optional log DEBUG. | – (Heuristik `?`) |
| 46 | `risk/risk_manager.py:1472` | `_dynamic_size` | A | `md_boost = 1.0` heißt „kein Boost"; `total` wird danach ohnehin auf `max_mult` geklemmt. | log WARN; Nebenwirkung: `factors["moderate_deploy_mult"]` meldet 1.0, obwohl `total` den Boost evtl. schon enthält (Reporting-Inkonsistenz). | – (Heuristik `?`) |
| 47 | `risk/risk_manager.py:1556` | `_fill_sell_amount_from_open_lot` | A | `return order` lässt `amount = 0`, der Aufrufer antwortet mit `approved=False, "No amount to sell"` — fail-closed. | optional log WARN. | ja (C? → A) |
| 48 | `risk/risk_manager.py:1569` | `_fill_sell_amount_from_open_lot` | C | Fallback `fraction = 1.0` verkauft bei einem Fehler in `sell_fraction_for_signal` das **gesamte** Lot statt der vorgesehenen Teilmenge (z. B. SELL_20 → 100 %). | log ERROR + `order` unverändert zurückgeben, damit „No amount to sell" greift. | – (Heuristik `?`) |
| 49 | `risk/risk_manager.py:1644` | `_resolve_sell_order` | C? | `sparams = None` → `rotation_config` entscheidet mit generischer statt tier-eingefrorener Config, ob der Teilverkauf zum Full-Close hochgestuft wird (Ordergröße ändert sich). | log ERROR + im Zweifel den Teilverkauf unverändert lassen (`return order`). | nein |
| 50 | `risk/risk_manager.py:1685` | `_partial_sell_blocked` | C? | Params-Fallback kann `ladder_enabled(params)` kippen und damit die Anwendbarkeit des Partial-Sell-Guards verändern (Guard greift/greift nicht). | log ERROR + strengere der beiden Param-Sätze verwenden. | nein |
| 51 | `risk/risk_manager.py:1692` | `_partial_sell_blocked` | A | Der `try` enthält `return False, ""` (Guard **überspringen**); bei Fehler laufen die Limit-Checks weiter — Fehlerrichtung restriktiv. | log WARN (Ladder-Positionen werden sonst still von Legacy-Limits geblockt). | ja (C? → A) |
| 52 | `risk/risk_manager.py:1787` | `_evaluate_short_or_cover` | A | Handler gibt selbst `approved=False, code="shorts_slots"` zurück — bereits fail-closed. | zusätzlich log ERROR. | ja (C? → A) |
| 53 | `risk/risk_manager.py:1805` | `_evaluate_short_or_cover` | A | `mcap = None` läuft in `if mcap is None or float(mcap) < min_mcap: → approved=False` — fail-closed (Auto-Pfad). | log WARN, um „nicht verfügbar" von „kaputt" zu trennen. | – (Heuristik `?`) |
| 54 | `risk/risk_manager.py:1820` | `_evaluate_short_or_cover` | A | Manual-Zweig: der Deny feuert bewusst nur bei bekanntem, zu kleinem mcap; der Except-Pfad ist identisch zum regulären `None`-Pfad, es wird also kein zusätzlicher Deny übersprungen (Vermerk: Kontext). | log WARN. | – (Heuristik `?`) |
| 55 | `risk/risk_manager.py:1836` | `_evaluate_short_or_cover` | A | Handler gibt `approved=False, code="short_margin"` zurück — fail-closed. | zusätzlich log ERROR. | ja (C? → A) |
| 56 | `risk/risk_manager.py:1852` | `_evaluate_short_or_cover` | A | `nav = 0.0` läuft direkt in `if nav <= 0: → approved=False ("NAV unknown")` — fail-closed. | log ERROR ergänzen. | – (Heuristik `?`) |
| 57 | `risk/risk_manager.py:1909` | `_trade_cooldown_blocked` | C | Der `try` enthält das blockierende `return True, "CMC sell cooldown …"`; ein unparsebares `last_cmc_sell_at` hebt den CMC-Sell-Cooldown auf. | log ERROR + bei Parse-Fehler blockieren (`return True, "cooldown_ts_unreadable"`). | nein |
| 58 | `risk/risk_manager.py:1919` | `_trade_cooldown_blocked` | C | Unparsebares `last_trade_at` → `return False, ""` deaktiviert **das gesamte Cooldown-Gate**: Rebuy-after-Sell, DCA-Intervall und Trending-Position-Cap werden danach gar nicht mehr aufgerufen. | log ERROR + `return True, "trade_cooldown_ts_unreadable"`. | nein |
| 59 | `risk/risk_manager.py:2018` | `_rebuy_after_sell_blocked` | C | `regime = None` → `_regime_key` mappt auf `NEUTRAL`, der Rebuy-Cooldown fällt von CRASH **6.0 h auf 2.0 h**. | log ERROR + konservativstes Regime (`CRASH`) annehmen. | nein |
| 60 | `risk/risk_manager.py:2025` | `_rebuy_after_sell_blocked` | C | `profile = None` → `missing_profile_mult = 1.0` statt `soft_block_mult 1.5` / `low_wr_mult 1.4` / Gross-Loss-Cooloff (12 h): der Rebuy auf den schlechtesten Coins wird zu früh freigegeben. | log ERROR + Memory-Ausfall mit dem strengsten Multiplikator behandeln. | nein |
| 61 | `risk/risk_manager.py:2057` | `_rebuy_after_sell_blocked` | A | Nur der DEBUG-Log-Aufruf (`cfg["log"]`). | keiner. | ja (C? → A) |
| 62 | `risk/risk_manager.py:2180` | `_iter_daily_filled_orders` | C | Ein Order mit unparsebarem Timestamp wird per `continue` still übersprungen und zählt damit nicht in `max_daily_buys` / `max_daily_sells` / `max_daily_dca_usdt` — die Tageslimits zählen zu niedrig. | log ERROR + Order mitzählen (fail-closed) statt verwerfen. | nein |
| 63 | `risk/slot_eviction.py:554` | `plan_slot_eviction` | C | Der `try` enthält die Position-Lock-Prüfung (`eviction_blocked` → `continue` = nicht evictbar); bei `pass` bleibt ein gesperrtes Lot im Opferpool und kann zwangsverkauft werden — `risk_manager.py:179` macht denselben Check fail-closed (`approved=False`). | Bei Exception `continue` (= als gesperrt behandeln) + log ERROR; zusätzlich `attach_lock_from_ledger` wie im Sell-Pfad aufrufen. Restschutz: der nachgelagerte `auto_sell_blocked` fängt die Default-Modi (`no_auto_sell`+`no_evict`), **nicht** aber ein reines `no_evict`-Lock. | nein (`C?`) |
| 64 | `risk/slot_eviction_rag.py:63` | `enrich_keeps_with_rag` | A (Kontext) | `err=True` wird an `apply_rag_keep` übergeben, das dann exakt `keep_rag == keep_profile` liefert — die RAG-Anreicherung wird sauber neutralisiert und der Fehler im Ergebnis-Dict (`error`) ausgewiesen. | Nur log WARNING ergänzen; Verhalten ist korrekt fail-neutral. | n/a (`?`) → A |
| 65 | `risk/slot_eviction_rag.py:89` | `_retrieve` | A (Kontext) | `[]` ergibt `evidence_delta_from_hits([]) == 0.0` → `keep_rag == keep_profile`, also derselbe neutrale Effekt wie #64; nur die Unterscheidbarkeit „Ausfall vs. keine Treffer" geht verloren. | log WARNING; sauberer wäre, die Exception durchzulassen, damit der Aufrufer `err=True` setzt (identisches Ergebnis, bessere Telemetrie). | ja: `C?` → **A** |
| 66 | `risk/slot_eviction_runtime.py:112` | `_hours_since` | C | `999.0` ist in beiden Verwendungen der *permissivste* Wert: als `age_hours` hebelt es das `min_hold`-Veto aus, als `idle_hours` maximiert es `idle_term` im `free_score` → ein Lot mit unparsbarem Timestamp wird zum bestbewerteten Eviction-Opfer. | Bei Parse-Fehler `0.0` (oder `None` mit expliziter Behandlung) + log WARNING zurückgeben; auch der `if not iso_ts: return 999.0`-Zweig gehört auf einen konservativen Wert. | nein (`C?`) |
| 67 | `risk/slot_eviction_runtime.py:166` | `build_victim_candidates` | A | Der `try` umschließt nur `float(notional)`; `position_notional_usdt` liefert immer einen `float`, der Handler ist praktisch unerreichbar und der Fallback `amount * price` ist die kanonische Formel. | Optional entfernen oder loggen; keine Verhaltensänderung nötig. | n/a (`?`) → A |
| 68 | `risk/slot_eviction_runtime.py:174` | `build_victim_candidates` | C (niedrig–mittel) | `prof = None` degradiert das Coin-Profil stumm auf „fehlt" → `memory_keep_score` = 0.5 neutral, `prefer=False` (das `prefer_hard_keep`-Veto entfällt) und die Klasse-C-Eskalation für `structure_risk`/`hard_negative` fällt weg. | log + „Profil nicht verfügbar" schützend behandeln (Kandidat überspringen) statt als neutrales Profil; in der Praxis von #78 maskiert, weil `_gp` bereits schluckt. | n/a (`?`) → C |
| 69 | `risk/slot_eviction_runtime.py:195` | `build_victim_candidates` | A (Kontext) | Der Fallback `trail_armed = peak_g >= protect_peak_gain_pct` erhält das Schutz-Veto auf einer *niedrigeren* Schwelle als der Erfolgspfad (12 % statt 15 %); Schutz geht nur für Lots verloren, die `trail_replacement_armed` unterhalb der Peak-Schwelle armen würde. | log WARNING; Fallback beibehalten. | n/a (`?`) → A |
| 70 | `risk/slot_eviction_runtime.py:233` | `build_victim_candidates` | C | Das hartkodierte `0.55` wird als synthetischer `ENTRY`-Kandidat (Zeile 278–306) zu `entry_keep` im Swap-Gate `edge = entry_keep − victim_keep < min_edge` → es liegt über dem konfigurierten `missing_profile_keep` (0.5) und weit über einem echten `soft_block`-Score (~0.22), sodass ein Profil-Fehler das Veto `memory_swap_not_worth_it` in eine freigegebene Eviction umkippt. | `memory_keep_score(None, risk_config=…)` (Config-Default) verwenden statt der Literalzahl — oder den Plan bei fehlendem Entry-Profil ganz verwerfen. | n/a (`?`) → C |
| 71 | `risk/slot_eviction_runtime.py:353` | `plan_for_blocked_entry` | A | `cands = []` führt in `plan_slot_eviction` zum Veto `no_candidate` → keine Eviction, also fail-closed. | log ERROR, damit ein dauerhaft kaputter Book-Scan nicht stumm bleibt. | n/a (`?`) → A |
| 72 | `risk/slot_eviction_runtime.py:399` | `execute_eviction_sell` | C | Der `except: pass` verdeckt, dass `MarketService` **kein** `get_price` besitzt (AttributeError bei *jedem* Aufruf) → die Live-Preis-Auffrischung ist dauerhaft tot und die SELL-Order behält `pos["mark_price"]` (wird nirgends geschrieben) `or average_entry or 0` **`or 1.0`** (Zeile 392), also Entry-Preis oder $1.00. | `or 1.0` entfernen, echte Preisquelle verwenden (`price_fetcher`/`prices`-Dict), bei fehlendem gültigem Preis die Eviction abbrechen + log ERROR. Aggravierend: `plan_for_blocked_entry` wird ohne `prices=` aufgerufen, deshalb ist auch `gain_pct` aller Kandidaten 0.0. | nein (`C?`) |
| 73 | `risk/slot_eviction_runtime.py:420` | `execute_eviction_sell` | C | Der `try` umfasst den kompletten SELL (`svc.execute_order`) plus `note_eviction_executed`/`set_pending_entry`; eine Exception *nach* der Order liefert `{"ok": False}`, und der Aufrufer hängt nur einen Text-Suffix „sell_failed" an → ein tatsächlich verkauftes Lot gilt als „nicht evictiert", Rate-Limit-/Pending-Buchhaltung bleibt halbfertig. | log ERROR mit Traceback; Fehler vor dem Order-Call von Fehlern danach trennen und letztere als `unknown/needs_reconcile` melden statt als `ok=False`. | nein (`C?`) |
| 74 | `risk/slot_eviction_runtime.py:450` | `resolve_spendable_ok_for_entry` | A | `return False` lässt das Must-Gate `spendable` in `score_entry_demand` scheitern → keine Eviction; der Kommentar dokumentiert die Absicht, das ist echtes Fail-Closed. | Nur log WARNING ergänzen. | ja: `C?` → **A** |
| 75 | `risk/slot_eviction_runtime.py:486` | `try_slot_eviction_on_max_open` | C | Bei Fusion-Fehler bleiben `block_buys=False` und `regime="NEUTRAL"` → die Must-Gates `block_buys` und `crash` in `score_entry_demand` werden stumm übersprungen, sodass im CRASH-/Block-Buys-Regime eine Position verkauft und ein Entry freigegeben wird. | log ERROR und Eviction abbrechen (`return None, ""`), wenn der Markt-Bias unbekannt ist — unbekanntes Regime darf nicht als NEUTRAL gelten. | nein (`C?`) |
| 76 | `risk/slot_eviction_runtime.py:502` | `try_slot_eviction_on_max_open` | C | Bei Memory-Fehler bleiben `soft_block=False` und `structure_risk=False` → beide Must-Gates entfallen und es wird ein Slot freigeräumt (Zwangsverkauf) für einen Entry, den das Memory gerade als `soft_block`/`structure_risk` markiert. | log ERROR und Eviction abbrechen, wenn das Coin-Memory nicht erreichbar ist. | nein (`C?`) |
| 77 | `risk/slot_eviction_runtime.py:524` | `try_slot_eviction_on_max_open` | C (niedrig) | `warmup=False` umgeht das `skip_if_warmup`-Veto direkt nach einem Neustart, wenn der Positionsstand noch unvollständig sein kann; Wirkung begrenzt, weil der Block nur bei `restart_warmup_min > 0` greift. | log WARNING und „Uptime unbekannt" als `warmup=True` behandeln. | nein (`C?`) |
| 78 | `risk/slot_eviction_runtime.py:532` | `_gp` | C | `return None` verwandelt jeden Memory-Ausfall in „neutrales Profil 0.5" für sämtliche Opfer *und* den Entry — `prefer`-Schutz, Klasse-C-Eskalation und der Memory-Anteil des Swap-Gates fallen still weg; zusätzlich maskiert es die Handler in 174 und 233. | Fehler einmalig loggen und den Eviction-Plan verwerfen, wenn die Profilquelle ausfällt; `get_profile` muss „Ausfall" von „kein Profil" unterscheidbar melden. | nein (`C?`) |
| 79 | `risk/slot_eviction_runtime.py:540` | `try_slot_eviction_on_max_open` (spike)` | A | `spike = 0.0` senkt `demand.score` (Spike gibt nur additive Boni: +2 ab 5.0, +1 ab 3.0) → `min_entry_score` wird schwerer erreicht, der Fehlerpfad ist konservativ. | log DEBUG/WARNING; sonst unverändert. | n/a (`?`) → A |
| 80 | `risk/slot_eviction_runtime.py:570` | `try_slot_eviction_on_max_open` | A | Umschließt ausschließlich die INFO-Logzeile für den Live-Plan. | Keiner (ggf. `except Exception: pass` um Logging ganz entfernen). | n/a (`?`) → A |
| 81 | `risk/slot_eviction_runtime.py:592` | `try_slot_eviction_on_max_open` | A | Umschließt ausschließlich die INFO-Logzeile für den Shadow-Plan. | Keiner. | n/a (`?`) → A |
| 82 | `services/order_service.py:123` | `_parse_ts` | A | Reiner Parser-Kontrakt: `None` = "nicht parsebar", keine Ledger-Operation im `try`. | Belassen; separat prüfen, dass `order_in_window` (Zeile 203) ein `None` nicht stillschweigend als "außerhalb des Fensters" verwirft. | ja (C?→A) |
| 83 | `services/order_service.py:143` | `_as_display_naive` | A | Kontext: fällt `display_tz()` aus, wird nur ohne TZ-Konversion weitergerechnet, kein leerer Zustand. | Belassen; einmalig `WARNING` loggen, damit ein dauerhaft kaputtes `core.time_utils` sichtbar wird. | nein |
| 84 | `services/order_service.py:158` | `_as_display_naive` | A | Kontext: `astimezone`-Fehler ⇒ naive Ortszeit statt Display-TZ, max. Tagesgrenzen-Offset. | Belassen, Log ergänzen. | ja (C?→A) |
| 85 | `services/order_service.py:171` | `_display_now_naive` | A | Kontext: Fallback auf Prozess-Uhr statt Display-TZ, kein leerer Zustand. | Belassen, Log ergänzen. | ja (C?→A) |
| 86 | `services/order_service.py:433` | `_dual_write_v2` | C | `store.upsert_order(record)` ist ein Ledger-Write; `except: pass` **ohne Log** — ein `DuplicateKeyError` lässt die Order dauerhaft im v2-Store fehlen, alle 4 Aufrufer (`create_from_request`, `update_status`, `expire_stale_pending`, `link_execution_result`) laufen ahnungslos weiter. | Mindestens `log(..., "ERROR")` + Zähler; sobald `ORDER_LEDGER_V2_READS=1`, muss ein fehlgeschlagener v2-Write den Store als *degraded* markieren, damit Tages-Reads auf den Blob zurückfallen statt einen unvollständigen Tag zu melden. | ja (?→C) — bestätigt Ticket-Befund |
| 87 | `services/order_service.py:456` | `get_by_id` | A | Der `except: pass` fällt auf `self._find(self._load(), …)` = den autoritativen Legacy-Blob zurück; kein "leer statt Fehler". | Belassen, aber `WARNING` loggen, damit ein dauerhaft kaputter v2-Lesepfad nicht unsichtbar bleibt. | ja (C?→A) |
| 88 | `services/order_service.py:472` | `get_by_display_seq` | A | Identisch zu #87: Fallback auf den Legacy-Blob, Ergebnis ist nicht "leer", sondern die SoT-Antwort. | Belassen, Log ergänzen. | ja (C?→A) |
| 89 | `services/order_service.py:675` | `list_day_filled_all` | A | `use_v2 = False` fällt auf das Legacy-Tagesfenster (SoT) zurück; die Liste trägt zwar den Gainer-Tageskauf-Cap (`services/gainer_signal/bot_http.py:156`), erhält aber die vollständige Blob-Antwort. | Log ergänzen; **Migrationsrisiko notieren**: sobald `ORDER_LEDGER_V2_BACKFILL_COMPLETE=1` und der Blob gekürzt wird, liefert derselbe Pfad einen leeren Tag ⇒ dann fail-closed nötig. | ja (?→A) |
| 90 | `services/order_service.py:749` | `list_month_filled_all` | A | `pass` fällt auf `list_month_filled` (Blob) zurück; reiner Anzeigepfad (`order_commands.py:199`). | Log ergänzen. | ja (C?→A) |
| 91 | `services/order_service.py:806` | `list_blocked_day_all` | A | `pass` fällt auf `list_blocked_orders` (Blob) zurück; Consumer sind Telegram-Views. | Log ergänzen. | ja (C?→A) |
| 92 | `services/order_service.py:916` | `stats_day_filled` | A | `pass` fällt auf `_stats_filled_window` (Blob) zurück; Consumer `order_commands.py:89` = Anzeige. | Log ergänzen. | ja (C?→A) |
| 93 | `services/order_service.py:960` | `stats_day_filled_fast` | A | Liefert im Fehlerfall Null-Stats, aber ausschließlich für die "Heute"-Zeile in `/positions` und `daily_portfolio` — die Degradation ist im Docstring dokumentiert. | Belassen; Anzeige sollte "n/v" statt "0" schreiben, damit Null nicht als echter Tageswert gelesen wird. | ja (C?→A) |
| 94 | `services/order_service.py:1079` | `link_execution_result` | A | Kontext: `stamp_venue_for_fill` ist Venue-Telemetrie fürs Memory-Learning und wird ehrlich als `{"capture": "missing"}` markiert. | Belassen; Log auf DEBUG ergänzen. | nein |
| 95 | `services/portfolio_service.py:30` | `_default_entry_source` | A | Der Fallback `s.startswith("gainer_") or s == "gate_prev_top"` ist semantisch identisch zu `is_gainer_source` (jedes Mitglied von `GAINER_SOURCES` erfüllt eine der beiden Bedingungen), das Tagging bleibt also korrekt. | Duplizierte Logik markieren: kommt eine Quelle ohne `gainer_`-Präfix in `GAINER_SOURCES`, driftet der Fallback und die Gainer-Slot-Caps unterzählen — besser `ImportError` gezielt fangen und loggen. | n/a (`?`) → A |
| 96 | `services/portfolio_service.py:228` | `execute_cover` | C (niedrig) | `hours = 0.0` bei unparsbarem `entry_at` macht `funding_cost_usdt(...) == 0`, sodass der per `record_trade` geschriebene und im `TradeResult` gemeldete realisierte PnL um die gesamten Funding-Kosten zu gut ausgewiesen wird. | log WARNING; den Cover **nicht** abbrechen (ein blockierter Cover wäre der schlimmere Fehler), stattdessen konservative Fallback-Dauer verwenden und den PnL als approximativ markieren. | n/a (`?`) → C |
| 97 | `services/portfolio_service.py:237` | `execute_cover` | C (niedrig) | Der nackte `except: pass` umschließt den kompletten Funding-Block (`resolve_short_params`, `funding_cost_usdt`) → dieselbe PnL-Überzeichnung im Ledger, komplett ohne Logzeile. | Mindestens log ERROR; Funding-Fehler im Trade-Record als Flag hinterlegen statt stillschweigend `fund = 0` anzunehmen. | nein (`C?`) |
| 98 | `strategies/positions.py:387` | `load_positions` | C | `store.clear()` steht **vor** dem `try`: schlägt der Load fehl (in Multi-Tenant lässt `data_manager.load_positions_document` bewusst durch-raisen), bleibt der In-Memory-Store leer, die Funktion gibt `{}` zurück und der Aufrufer (`bootstrap_positions`/`activate_tenant_positions`) hält das für "keine Positionen". | Fail-closed: Exception an den Aufrufer weiterreichen (oder alten Store-Inhalt nicht verwerfen) und den Handelszyklus für diesen Tenant/Scope aussetzen; `store.clear()` erst nach erfolgreichem Load anwenden. | ja (A?→C) — **schwerste Stelle im Slice** |
| 99 | `strategies/positions.py:472` | `_do_save_positions` | C | Der `try` liest das bestehende Positions-Dokument, um out-of-process gesetzte `lock`-Objekte zu erhalten; scheitert der Read, wird auf DEBUG geloggt und trotzdem geschrieben — ein aktiver Position-Lock (blockt in `risk/risk_manager.py:161/224` Sell und DCA) kann dadurch überschrieben werden. | Log auf ERROR; bei fehlgeschlagenem Read den Flush abbrechen statt ein aus einem Fehl-Read abgeleitetes Dokument zu schreiben. | ja (A?→C) |
| 100 | `strategies/positions.py:476` | `_do_save_positions` | C | Umschließt den kompletten Read-Modify-Write über zwei Stores; Funktion gibt `None` zurück, `flush_positions` reicht nichts weiter — ein gescheiterter Positions-Save ist für jeden Aufrufer (u.a. `update_position` nach jedem Trade) unsichtbar. | `_do_save_positions`/`flush_positions` auf `bool` umstellen, Fehler an den Aufrufer melden und im Trade-Pfad darauf reagieren (Retry/Alarm/Handelspause). | ja (A?→C) |
| 101 | `strategies/positions.py:689` | `bind_buy_timeframe` | B | `is_short(pos)` auf einem wohlgeformten Positions-Dict darf nicht werfen; das Verschlucken kippt die Long/Short-Routing-Entscheidung und bindet einen BUY an das TF eines Short-Lots. | Log ERROR und konservativ `return pref` (unbekannte Seite = wie Short behandeln, kein TF-Hop) statt `pass`. | ja (C?→B) |
| 102 | `strategies/positions.py:918` | `update_position` | C | `params = None` ⇒ `dca_config(None) == {}` ⇒ `pos["dca_max_rounds"] = 3` (Default) wird in die Position persistiert und steuert danach, wie viele DCA-Nachkäufe (= Exposure) erlaubt sind. | Fehler melden statt Default einfrieren: `dca_max_rounds` nicht setzen, wenn die Tier-Parameter nicht aufgelöst werden konnten, und den Fehler loggen (ERROR). | ja (?→C) |
| 103 | `strategies/positions.py:934` | `update_position` | C | `reanchor_recent_high_after_dca` re-basiert nach einem DCA den Trail-Peak; scheitert es still, rechnet der WS-Trail-Stop mit dem Vor-Dump-Hoch weiter ⇒ falsche Exit-Entscheidung. | Log ERROR; Position als "peak nicht re-anchored" markieren, damit der Trail-Stop-Pfad das erkennt, statt auf einem stale Peak zu handeln. | nein |
| 104 | `strategies/positions.py:949` | `update_position` | C | Gleiche Familie: `stamp_peak_epoch_on_dca` / `set_recovery_hold` steuern Peak-Clamp und Recovery-Hold (blockt Exits); `pass` lässt die Position mit stale Peak / ohne Hold weiterlaufen. | Log ERROR + Marker auf der Position; Exit-Logik muss den fehlenden Epoch/Hold erkennen. | nein |
| 105 | `strategies/positions.py:1064` | `update_position` | C | Im SELL-Zweig: `strategy_params = None` ⇒ (a) `sell_fraction_for_signal(..., None)` errechnet bei `amount_traded <= 0` eine Default-Verkaufsquote und schreibt damit den **Restbestand** der Position, (b) `take_profit_tiers` wird leer, (c) `advance_ladder_step` wird komplett übersprungen. | Fail-closed: ohne aufgelöste Strategy-Params keine Mengen-/Tier-/Ladder-Fortschreibung — Fehler an den Aufrufer melden statt Defaults in die Positionsbuchhaltung zu schreiben. | ja (?→C) |
| 106 | `data_manager.py:92` | `get_data_file` | A | Kopie der Realdatei in die `.demo.json`-Variante schlägt fehl → Demo startet mit leerem Buch statt Kopie, geloggt, kein Live-/Order-Pfad betroffen. | ggf. auf ERROR heben, sonst belassen | nein |
| 107 | `data_manager.py:111` | `get_data_file` | A | Identisch zu #106 (Pfad-Variante mit `data/`-Ordner), reiner Demo-Bootstrap-Seed. | ggf. auf ERROR heben | nein |
| 108 | `data_manager.py:137` | `_should_use_mongo_for_tenant_config` | C | Jeder Fehler → `False` → für einen Tenant liefert `load_config` still die Operator-Default-Config (fremde Risiko-Limits) und `save_config`/`save_watchlist` geben **`True` zurück, ohne zu schreiben** (Zeilen 277/810). | log ERROR + Exception propagieren bzw. Tri-State; niemals „Speichern OK" melden, wenn das Backend unbekannt ist | nein (C? → C) |
| 109 | `data_manager.py:165` | `atomic_write_json` | A | Der Handler räumt fd/tmp auf, loggt ERROR und macht `raise` (Z. 176–177) — bereits fail-closed. | keiner | ja (`?` → A) |
| 110 | `data_manager.py:169` | `atomic_write_json` | A | Innerer `os.close(fd)`-Cleanup im ERROR-Pfad; der eigentliche Fehler wird weiterhin geloggt und geworfen. | keiner | **ja (C? → A)** |
| 111 | `data_manager.py:174` | `atomic_write_json` | A | Innerer `os.remove(tmp)`-Cleanup im ERROR-Pfad; Originalfehler wird weiterhin geworfen. | keiner | **ja (C? → A)** |
| 112 | `data_manager.py:234` | `load_watchlist` | C? | Gleiche Fail-Open-Klasse wie #108 (Tenant fällt still auf die Default-JSON-Watchlist zurück), praktisch aber unerreichbar, weil `_should_use_mongo_for_tenant_config` intern schon alles schluckt. | zusammen mit #108 fixen bzw. ersatzlos entfernen | ja (`?` → C?) |
| 113 | `data_manager.py:242` | `load_watchlist` | C? | Bei Mongo-Fehler bekommt der Tenant still die Watchlist des **Default-Tenants** aus JSON → fremdes Handelsuniversum; unsicher, weil der Leer-Fallback ohnehin so designt ist und der Fehler dadurch nicht von „Tenant hat keine Watchlist" unterscheidbar ist. | log ERROR; Fehlerfall vom „leer"-Fall trennen und den Zyklus für den Tenant überspringen | **ja (A? → C?)** |
| 114 | `data_manager.py:260` | `load_watchlist` | C | `return []` bei korrupter `watchlist.json`; `add_coin` (Z. 311) und `save_full_coin` (Z. 672) machen load→append→save und **überschreiben die Watchlist-Datei mit einem einzigen Coin**. | log ERROR + raise (bzw. Sentinel), damit die RMW-Aufrufer abbrechen | nein |
| 115 | `data_manager.py:273` | `save_watchlist` | C? | Wie #112; bei Fehler `use_mongo=False` → `return True` (Z. 277) **ohne Schreibvorgang**; praktisch unerreichbar. | zusammen mit #108 fixen | ja (`?` → C?) |
| 116 | `data_manager.py:280` | `save_watchlist` | C | Tenant-Watchlist-Save schlägt fehl → `return False`, aber `add_coin`/`remove_coin`/`save_full_coin`/`onboarding_commands.py:436` ignorieren den Bool und melden dem Nutzer Erfolg. | log ERROR; Aufrufer muss den Bool prüfen und die Telegram-Erfolgsmeldung unterdrücken | **ja (A? → C)** |
| 117 | `data_manager.py:288` | `save_watchlist` | C | JSON-Schreibfehler → `return False`, von allen Aufrufern ignoriert; `remove_coin` antwortet trotzdem „✅ entfernt", während der Bot den Coin weiter handelt. | Aufrufer fail-closed machen (Fehlermeldung statt ✅) | nein |
| 118 | `data_manager.py:329` | `save_dry_run_expansion` | C | `except: return False` ganz ohne Log; `remove_coin` (Z. 349) und `prune_non_gate_watchlist_sources` (Z. 540) ignorieren den Rückgabewert → entfernter/geprunter Coin bleibt im Trade-Universum. | log ERROR + Rückgabewert an den Aufrufern auswerten | nein |
| 119 | `data_manager.py:385` | `load_dry_run_expansion` | A (Kontext) | Leere Expansion schrumpft nur das Observe/Trade-Universum (konservativ, keine Größenerhöhung); Overlay wird ohnehin je Zyklus von `services/dry_run_watchlist.py` neu erzeugt. | log ERROR; RMW in `prune_non_gate_watchlist_sources` (Z. 538) verliert Nicht-`coins`-Schlüssel | nein |
| 120 | `data_manager.py:397` | `load_dry_run_overlay` | A (Kontext) | Wie #119 — Overlay ist ein regenerierbarer Kontext-Zusatz, Ausfall verkleinert das Universum. | log ERROR | nein |
| 121 | `data_manager.py:407` | `save_dry_run_overlay` | C | `except: return False` ohne Log; `remove_coin` (Z. 358) ignoriert es und meldet „✅ entfernt", der Coin bleibt aber im Overlay und damit handelbar. | log ERROR + Aufrufer fail-closed | nein |
| 122 | `data_manager.py:418` | `load_cmc_trending_overlay` | A (Kontext) | Wie #119/#120 — regenerierbares Trending-Overlay, Ausfall verkleinert das Universum. | log ERROR | nein |
| 123 | `data_manager.py:428` | `save_cmc_trending_overlay` | C | Wie #121, Aufrufer `remove_coin` (Z. 367) meldet Erfolg trotz verlorenem Schreibvorgang. | log ERROR + Aufrufer fail-closed | nein |
| 124 | `data_manager.py:610` | `build_merged_watchlist_coins` | C | Im WQE-Modus `enforce` entfernt `apply_wqe_to_watchlist` bewusst Coins; bei Fehler wird nur DEBUG geloggt und die **ungefilterte** Liste weiterverwendet → Qualitäts-Sperre umgangen. | log ERROR; in `enforce` den Zyklus/Coin überspringen statt ungefiltert weiterzugeben | **ja (A? → C)** |
| 125 | `data_manager.py:628` | `load_effective_watchlist` | C? | Fällt bei Fehler des Universe-Split-Loaders auf das **ungekappte** Merge zurück (`observe_max` entfällt); Observe-Universum ist primär Scan/Memory, speist aber über #126 den Trade-Pfad. | log ERROR statt DEBUG; Cap auch im Fallback anwenden | ja (A? → C?) |
| 126 | `data_manager.py:664` | `load_trade_watchlist` | C | Bei Fehler wird das komplette Observe-Universum als handelbar zurückgegeben — `trade_max_coins`/`select_trade_universe` (services/universe/split.py) werden umgangen, also mehr Coins kaufbar als konfiguriert. | log ERROR; bei aktivem Split lieber leere Trade-Liste (Zyklus überspringen) als das volle Observe-Set | **ja (A? → C)** |
| 127 | `data_manager.py:690` | `_load_default_config_from_disk` | C (hoch) | Unlesbare/kaputte `config.json` → still **hartkodierte Defaults** (`max_usdt_per_trade: 150`, `max_open_positions: 5`, `stop_loss_pct: 12.0`, `strategies: []`) für die gesamte Basis-Config → direkte Änderung von Order-Größe und Risiko-Limits. | log ERROR + raise; der Bot darf ohne lesbare Basis-Config nicht handeln | **ja (A? → C)** |
| 128 | `data_manager.py:720` | `_load_tenant_config_body` | C (hoch) | `return None` → `apply_effective_config(default_cfg, None)` → der Tenant läuft still mit der **Operator-Default-Config** (fremde `max_usdt_per_trade`, `max_open_positions`, `trading_mode`, Strategien). | log ERROR + raise bzw. Tenant-Zyklus abbrechen; niemals still auf die Operator-Config zurückfallen | **ja (A? → C)** |
| 129 | `data_manager.py:738` | `load_config` | C? | Gleiche Klasse wie #108/#128 (Tenant bekommt die Default-Config), praktisch unerreichbar. | zusammen mit #108 fixen | ja (`?` → C?) |
| 130 | `data_manager.py:796` | `save_config` | C? | Gleiche Klasse wie #108; führt zu `return True` (Z. 800) ohne Schreibvorgang; praktisch unerreichbar. | zusammen mit #108 fixen | ja (`?` → C?) |
| 131 | `data_manager.py:807` | `save_config` | C | Tenant-Config-Save scheitert → `return False`, aber `onboarding_commands.py:435` und `grid_plan_store.py:155` ignorieren es → neu onboardeter Tenant läuft mit Operator-Limits weiter. | log ERROR; Aufrufer (Onboarding) muss abbrechen statt „Bot ist aktiv" zu melden | nein |
| 132 | `data_manager.py:816` | `save_config` | C | `config.json`-Schreibfehler → `return False`; `strategy_auto_tuner`/`registry` prüfen den Bool, `grid_plan_store` und `onboarding` nicht → still nicht persistierte Trading-Parameter. | eigenes log ERROR; verbleibende Aufrufer auf Bool-Prüfung umstellen | nein |
| 133 | `data_manager.py:827` | `load_x_accounts` | C? | `return []` → `add_x_account` (x_commands.py:29) macht load→append→save und **überschreibt `x_accounts.json` mit einem Eintrag**, alle `trust_score`-Werte fallen auf den 70-Default zurück; Auswirkung ist reine Sentiment-Gewichtung. | log ERROR + raise, damit die RMW-Pfade abbrechen | nein |
| 134 | `data_manager.py:837` | `save_x_accounts` | A | Beide mutierenden Telegram-Pfade prüfen den Bool (`x_commands.py:38/136`), der Trust-Score-Updater ignoriert ihn, rechnet aber jeden Lauf neu — reine Sentiment-Metadaten. | log ERROR ergänzen (aktuell komplett stumm) | **ja (C? → A)** |
| 135 | `data_manager.py:848` | `load_x_posts` | A | Post-Archiv für die X-Accuracy-Auswertung; kein Einfluss auf Order-Größe, Freigabe oder Ledger. | log ERROR | nein |
| 136 | `data_manager.py:858` | `save_x_posts` | A | Gleicher Analytics-Store; Aufrufer (`x_analyzer.py:345`, `accuracy_tracker.py:79`) verlieren nur Archivdaten. | log ERROR ergänzen (aktuell stumm) | **ja (C? → A)** |
| 137 | `data_manager.py:897` | `load_demo_data` | A | `load_demo_data()` hat keinen Produktionsaufrufer (nur Tests) und ist demo-gated; Fehler hinterlässt aber eine Demo-`trade_history` mit `open_positions: 3` bei leerem Positions-Store. | log ERROR + früh returnen statt die inkonsistente Trade-History zu schreiben | nein |
| 138 | `data_manager.py:949` | `_load_trade_history_json` | C (hoch) | Korrupte `trade_history.json` → `_default_trade_history` = **volles Startkapital, `realized_pnl` 0, `open_positions` 0, `trades` []**; `risk_manager._available_usdt` sized darauf und `record_trade` (load→append→save) überschreibt die Historie mit einem einzigen Trade. | log ERROR + raise; Aufrufer muss den Zyklus abbrechen statt auf Phantom-Cash zu handeln | **ja (A? → C)** |
| 139 | `data_manager.py:959` | `_save_trade_history_json` | C | `except: return False` ohne eigenes Log; `record_trade` (Z. 1388) und `record_live_trade` (Z. 1352) ignorieren den Bool → ausgeführter Trade fehlt still im Ledger. | log ERROR; `record_trade`/`record_live_trade` müssen den Fehler melden/werfen | nein |
| 140 | `data_manager.py:975` | `load_trade_history_document` | C? | Refuse+raise greift nur für Demo-Mongo und Multi-Tenant; bei Single-Tenant-`paper`/`live` mit Mongo-Backend wird auf `_load_trade_history_json` zurückgefallen, obwohl `_ledger_writes_json` dort `False` ist → die JSON ist veraltet/leer und der Kontostand springt auf das Startkapital. | Refuse-Guard auf **jeden** Scope ausweiten, dessen konfiguriertes Backend `mongo` ist | **ja (A? → C?)** |
| 141 | `data_manager.py:1027` | `reconcile_demo_trade_history_on_startup` | A | Ein fehlerhafter Tenant bricht zwar den ganzen Startup-Vorlauf ab, `load_trade_history_document` rekonziliert aber bei jedem späteren Lesen ohnehin lazy. | log ERROR; try in die Schleife ziehen, damit ein Tenant nicht alle blockiert | nein |
| 142 | `data_manager.py:1045` | `save_trade_history_document` | C | Handler loggt korrekt ERROR und gibt `False` zurück, aber `record_trade`, `record_live_trade` und `load_trade_history_document` (Z. 1017) werfen den Bool weg → stiller Verlust von Cash-/Historie-Schreibvorgängen. | Aufrufer fail-closed machen (raise bzw. Zyklus abbrechen) | **ja (A? → C)** |
| 143 | `data_manager.py:1298` | `_reconcile_live_trade_sources` | C? | Nacktes `except: pass` um einen **Orders-Ledger-Schreibvorgang** (`svc.reconcile_legacy_sources()` → `OrderService._save`): Fehler bleibt komplett unsichtbar, und ein Abbruch mitten in der Schleife kann `changed=True` zurückgeben, sodass eine halb-rekonziliierte Historie persistiert wird. | log ERROR; bei Fehler `changed=False` zurückgeben, damit nichts Halbfertiges gespeichert wird | nein |
| 144 | `data_manager.py:1313` | `_load_live_trade_history_json` | C (hoch) | Unlesbare `live_trade_history.json` → leere Historie; `_ensure_live_virtual_balance` rechnet daraus `virtual_balance` = volles Startkapital (Sizing-Grundlage im Live-Dry-Run) und `record_live_trade` schreibt die Ein-Trade-Historie zurück. | log ERROR + raise | **ja (A? → C)** |
| 145 | `data_manager.py:1332` | `load_live_trade_history` | C (hoch) | Mongo-Lesefehler → leere Live-Historie **ohne jeden Refuse-Guard**; `record_live_trade` macht load→append→save und ersetzt damit die komplette Live-Trade-Historie in Mongo durch einen einzigen Trade (zusätzlich kann `reconciled=True` direkt in Z. 1345 zurückschreiben). | log ERROR + raise (analog `_should_refuse_demo_json_fallback`); niemals eine leere Historie zurückschreiben | **ja (A? → C)** |
| 146 | `data_manager.py:1383` | `record_trade` | A | Das echte `max_open_positions`-Gate nutzt `count_open_positions()` live (`decision_engine.py:542`); der hier geschätzte Zähler ist ein Anzeigewert und wird bei simuliertem Handel von `_reconcile_scoped_trade_history` aus den Orders überschrieben. | log ERROR (schluckt sonst auch echte Import-/Programmierfehler — alternativ B) | ja (`?` → A) |
| 147 | `data_manager.py:1528` | `_load_orders_json` | C (hoch) | `_empty_orders` bei korrupter `orders.*.json`: `resolve_sim_cash_balance` replayt 0 Fills → Cash = volles Startkapital für `risk_manager._available_usdt`, `_reconcile_scoped_trade_history` schreibt `virtual_balance`/`open_positions: 0` zurück, und `OrderService._load`→`_save` überschreibt die Orders-Datei. | log ERROR + raise; leeres Buch nur bei tatsächlich fehlender Datei | **ja (A? → C)** |
| 148 | `data_manager.py:1540` | `_save_orders_json` | C (hoch) | `except: return False` ohne eigenes Log, und `OrderService._save` verwirft den Bool an **allen sechs** Aufrufstellen — inkl. `update_status(..., "filled")` → ein ausgeführter Fill landet nie im Ledger. | log ERROR; `OrderService._save` muss bei `False` werfen bzw. der Aufrufer den Zyklus abbrechen | nein |
| 149 | `data_manager.py:1552` | `load_orders` | C (hoch) | Refuse+raise nur für Demo-Mongo/Multi-Tenant; Single-Tenant `paper`/`live` mit Mongo-Backend fällt auf `_load_orders_json` zurück, obwohl die JSON dort gar nicht gepflegt wird (`_ledger_writes_json == False`) → **leeres Orderbuch** = volles Cash, keine Positionen. | Refuse-Guard auf jeden mongo-konfigurierten Scope ausweiten | **ja (A? → C)** |
| 150 | `data_manager.py:1584` | `_reject_demo_mongo_orders_downgrade` | C (hoch) | Der Handler umschließt die **Schutzprüfung selbst**: scheitert der Vorab-Read des bestehenden Mongo-Buchs, wird nur WARNING geloggt und `False` = „kein Downgrade, schreiben erlaubt" zurückgegeben — genau der Wipe, den der Guard verhindern soll, geht durch. | log ERROR + `return True` (fail-closed): im Zweifel den Schreibvorgang blocken | **ja (A? → C)** |
| 151 | `data_manager.py:1602` | `save_orders` | C | Handler loggt ERROR und setzt `ok=False`, aber `OrderService._save` und `ledger_sync.py:385` ignorieren den Bool → stiller Verlust von Orders-Schreibvorgängen. | Aufrufer fail-closed machen (raise / Zyklus abbrechen) | **ja (A? → C)** |
| 152 | `data_manager.py:1622` | `_load_positions_json` | C | `_empty_positions` → `_preserve_locks_from_existing_doc` (positions.py:470) bekommt ein leeres Doc und rettet **keine Locks** mehr, ein Flush löscht damit ops-gesetzte Positions-Locks; `attach_lock_from_ledger` findet ebenfalls keinen Lock → gesperrte Position wird verkaufbar. | log ERROR + raise; Save-Pfad muss bei unlesbarem Positions-Doc abbrechen statt Locks zu verlieren | **ja (A? → C)** |
| 153 | `data_manager.py:1634` | `_save_positions_json` | C | `except: return False`; `positions.py:474` loggt nur ERROR und läuft weiter, `ledger_sync.py:168` ignoriert den Bool → Positions-Cache inkl. Locks wird still nicht persistiert. | log ERROR; Aufrufer müssen den Fehler eskalieren, nicht nur loggen | nein |
| 154 | `data_manager.py:1651` | `load_positions_document` | C | Wie #149 für Positionen: außerhalb Demo/Multi-Tenant stiller Fallback auf ein veraltetes/leeres JSON-Positions-Doc → Locks und Cache-Felder (avg entry, Ladder-State) verschwinden. | Refuse-Guard auf jeden mongo-konfigurierten Scope ausweiten | **ja (A? → C)** |
| 155 | `data_manager.py:1678` | `save_positions_document` | C | Handler meldet ERROR/`False` korrekt, aber `ledger_sync.py:168` ignoriert ihn und `positions.py:474` loggt nur — der Bot handelt mit einem Positions-Doc weiter, das nie geschrieben wurde. | Aufrufer fail-closed machen | **ja (A? → C)** |
| 156 | `data_manager.py:1696` | `load_strategy_backtest_results` | C? | `{"coins": {}}` → `previous.get("locked")` ist `False`, also wird eine vom Operator **gesperrte** Strategie wieder auto-getunt, und `strategy_auto_tuner.apply` schreibt neue RSI-/Volumen-Parameter per `save_config` in die Live-Config. | log ERROR + raise; bei unlesbarer Datei kein Auto-Tuning-Job starten | **ja (A? → C?)** |
| 157 | `data_manager.py:1707` | `save_strategy_backtest_results` | C? | `except: return False` ohne Log; `strategy_backtest_worker.py:182` ignoriert den Bool → `locked`-Flag und Review-Zeitplan gehen verloren. | log ERROR + Bool im Worker auswerten | nein |
| 158 | `data_manager.py:1754` | `load_cmc_posts` | A | Social-Post-Log für Memory/Analytics; die trade-relevante Dedup läuft über `bus.dedup.try_claim_id` (`_claim_post`), nicht über diese Datei. | log ERROR | nein |
| 159 | `data_manager.py:1764` | `save_cmc_posts` | A | Gleicher Store; verlorener Schreibvorgang kostet nur Memory-/Analytics-Einträge, keine Order-Entscheidung. | log ERROR ergänzen (aktuell stumm) | **ja (C? → A)** |
| 160 | `data_manager.py:1792` | `log_cmc_post` | A | `except: pass` um `append_social_feed` — optionale Memory-Anreicherung, laut Rubrik explizit A. | log DEBUG/WARNING statt stummem `pass` | **ja (C? → A)** |
| 161 | `data_manager.py:1813` | `load_lc_signals` | A | LunarCrush-Signal-Log für Memory/Analytics, keine Order-/Ledger-Wirkung. | log ERROR | nein |
| 162 | `data_manager.py:1826` | `save_lc_signals` | A | Wie #159. | log ERROR ergänzen | **ja (C? → A)** |
| 163 | `data_manager.py:1854` | `log_lc_signal` | A | Wie #160 — Memory-Feed-Anreicherung. | log DEBUG/WARNING statt `pass` | **ja (C? → A)** |
| 164 | `data_manager.py:1866` | `load_paper_strategies` | A | Paper-Sandbox-Hypothesen; RMW-Verlust bleibt im Sandbox-Subsystem, kein Live-Order-Pfad. | log ERROR | nein |
| 165 | `data_manager.py:1876` | `save_paper_strategies` | A | Sandbox-Store, kein Einfluss auf Größe/Freigabe/Ledger. | log ERROR ergänzen | **ja (C? → A)** |
| 166 | `data_manager.py:1887` | `load_paper_sandbox_history` | A | Sandbox-Portfolios (Forschung), kein Live-Pfad. | log ERROR | nein |
| 167 | `data_manager.py:1897` | `save_paper_sandbox_history` | A | Wie #165. | log ERROR ergänzen | **ja (C? → A)** |
| 168 | `data_manager.py:1910` | `load_translations` | A | Reine Anzeige/i18n. | keiner | nein |
| 169 | `data_manager.py:1920` | `get_system_lang` | A | Reine Anzeige/i18n. | keiner | nein |
| 170 | `storage/grid_plan_store.py:67` | `load_grid_plans_document` | C | "Leer statt Fehler" auf DEBUG-Level: bei Mongo-Fehler `plans: {}` ⇒ (a) `GridStrategy._load_or_init_plan` baut ein **neues Grid um den aktuellen Preis** (neue Buy/Sell-Level), (b) `save_grid_plan` ist ein Read-Modify-Write und **löscht beim nächsten Save alle übrigen Pläne** des Tenants. | Fehler melden statt `empty`: eigenes Sentinel/Exception für "Load fehlgeschlagen"; `save_grid_plan` darf nach einem gescheiterten Load nicht speichern; Log auf ERROR. | ja (A?→C) |
| 171 | `storage/grid_plan_store.py:96` | `save_grid_plans_document` | C | `return False`, das niemand liest: `save_grid_plan` reicht `ok` durch, aber `GridStrategy._persist_plan` (strategies/grid.py:140) ignoriert den Rückgabewert und behält den Plan im RAM, als sei er persistiert. | Rückgabewert in `_persist_plan` auswerten (Retry/Alarm, Plan nicht als persistiert markieren); Log auf ERROR. | nein |
| 172 | `storage/grid_plan_store.py:122` | `load_grid_plan` | C? | Legacy-Fallback über `config.grid_states`; ein Fehler wird als "kein Plan" (`None`) ausgegeben — ununterscheidbar vom Normalfall, Folge ist erneut ein Grid-Re-Center. Geringere Schwere als #170, weil es nur die zweite Fallback-Stufe ist. | Mindestens loggen; "Read fehlgeschlagen" von "nicht vorhanden" unterscheiden, damit der Grid-Aufbau nicht auf einem Lesefehler neu zentriert. | nein |
| 173 | `storage/grid_plan_store.py:156` | `save_grid_plan` | A | Nur der Legacy-Spiegel in `config.grid_states` für die `/grid`-Anzeige; der autoritative Mongo-Write ist bereits erfolgt und wird über `ok` gemeldet. | Belassen; Hinweis: ein veralteter Spiegel kann später über den Legacy-Fallback (#172) einen alten Plan ausliefern — Log auf WARNING heben. | nein |
| 174 | `storage/ledger_router.py:101` | `_atomic_write` | C | `except Exception: return False` **ohne Log** unterdrückt aktiv den Fehler, den `data_manager.atomic_write_json` bereits mit `ERROR` loggt **und** re-raist — ein Ledger-Write (Orders/Positions/Trade-History) scheitert damit lautlos. | Exception durchreichen (der Logger sitzt schon in `atomic_write_json`) oder hier ERROR loggen **und** sicherstellen, dass jeder `save_*`-Aufrufer auf `False` reagiert. | nein — bestätigt Ticket-Befund |
| 175 | `storage/ledger_router.py:137` | `load_orders` (Json)` | C | "Leer statt Fehler": ein defektes/unlesbares `orders.*.json` wird zu `{"orders": []}` ⇒ der Aufrufer handelt auf einem leeren Orderbuch (Tages-Caps, Idempotenz, Positions-Ableitung). | Lesefehler melden statt leeres Dokument; "Datei fehlt" (legitim leer) und "Datei kaputt" strikt trennen. | ja (A?→C) |
| 176 | `storage/ledger_router.py:156` | `load_positions` (Json)` | C | Identisch für `positions.*.json`: `{"positions": {}}` heißt für den Aufrufer "keine Positionen" ⇒ Doppelkäufe, Orphan-Pruning (`prune_orphan_position_cache`). | Wie #175: Fehler melden, "fehlt" ≠ "kaputt". | ja (A?→C) |
| 177 | `storage/ledger_router.py:182` | `load_trade_history` (Json)` | C | Schlimmste Variante: `_empty_trade_history` **erfindet Cash** — `virtual_balance: 5000.0`, `realized_pnl: 0.0` — aus einem I/O-Fehler. | Fehler melden; ein Startguthaben darf niemals aus einem Exception-Handler stammen. | ja (A?→C) |
| 178 | `storage/ledger_router.py:229` | `load_orders` (DualWrite)` | C? | Fallback von Mongo (laut Docstring "authoritative") auf JSON ohne Existenz-/Frische-Prüfung; fehlt der JSON-Spiegel, liefert #175 ein leeres Orderbuch — und wegen #174 kann ein früherer JSON-Write lautlos gescheitert sein. | Fallback nur mit Plausibilitätsprüfung (Datei existiert, Dokument nicht leer, Alter unter Schwelle), sonst fail-closed. | ja (A?→C?) |
| 179 | `storage/ledger_router.py:237` | `save_orders` (DualWrite)` | C? | Handler-Hälfte ist korrekt (ERROR-Log + `ok = False`), aber der JSON-Write wird nicht zurückgerollt und es existiert **kein produktiver Aufrufer**, der `ok` auswertet ⇒ die beiden Backends divergieren still. | Bei Verdrahtung: `False` muss den Trade-/Flush-Pfad abbrechen; alternativ raisen, damit niemand den Status ignorieren kann. | ja (A?→C?) |
| 180 | `storage/ledger_router.py:245` | `load_positions` (DualWrite)` | C? | Wie #178 für Positionen: stiller Fallback auf einen ggf. leeren/veralteten JSON-Spiegel. | Wie #178. | ja (A?→C?) |
| 181 | `storage/ledger_router.py:253` | `save_positions` (DualWrite)` | C? | Wie #179 für Positionen. | Wie #179. | ja (A?→C?) |
| 182 | `storage/ledger_router.py:261` | `load_trade_history` (DualWrite)` | C? | Wie #178; der JSON-Fallback kann über #177 ein erfundenes Startguthaben liefern. | Wie #178, zusätzlich: Cash-Dokument niemals aus einem Default konstruieren. | ja (A?→C?) |
| 183 | `storage/ledger_router.py:269` | `save_trade_history` (DualWrite)` | C? | Wie #179 für die Trade-History. | Wie #179. | ja (A?→C?) |
| 184 | `storage/mongo_client.py:206` | `get_client` | A | Der `try` schließt nur den **alten** Client bei URI-Wechsel; ein Fehler bedeutet eine geleakte Verbindung, der neue Client wird unabhängig davon erzeugt. | Belassen; DEBUG-Log ergänzen. | ja (C?→A) |
| 185 | `storage/mongo_client.py:240` | `get_database` | A | Kein Swallow: sauberer Retry-once, im zweiten Durchlauf `raise` — der Fehler erreicht den Aufrufer. | Belassen. | nein |
| 186 | `storage/mongo_client.py:291` | `close_client` | A | Teardown-Pfad; ein `close()`-Fehler ist irrelevant, die Referenz wird ohnehin verworfen. | Belassen; DEBUG-Log ergänzen. | ja (C?→A) |
| 187 | `storage/order_ledger_v2.py:91` | `get_order_ledger_v2` | C | Bei `ORDER_LEDGER_V2_BACKEND=mongo` schlägt `ensure_indexes()` still fehl und der Store wird **ohne seine Unique-Indizes** ausgeliefert (`(tenant,scope,display_seq)` unique, `idempotency_key` unique sparse) — der Duplikat-Schutz fehlt für die Prozesslaufzeit, ohne Log. | ERROR loggen; ohne Indizes den Store nicht als "mongo" ausliefern (oder `degraded=True` markieren). Hinweis: der Handler toleriert auch bewusst `assert_safe_dev_db_mutation` unter pytest — diesen Fall gezielt abfangen statt `except Exception`. | nein — bestätigt Ticket-Befund |
| 188 | `storage/order_ledger_v2.py:110` | `get_order_ledger_v2` | C | Auto-Backend: schlägt `MongoOrderLedgerV2()`/`ensure_indexes()` fehl, wird der Prozess **ohne jedes Log** für seine gesamte Laufzeit auf einen flüchtigen `MemoryOrderLedgerV2` gepinnt — Orders landen nur im RAM; mit `ORDER_LEDGER_V2_READS=1` liefert der Tages-Index dann ein leeres Buch. | ERROR loggen, Fallback als degradiert markieren und v2-Reads für diesen Prozess hart abschalten; periodischen Reconnect-Versuch statt Pin auf Prozesslebenszeit. | ja (?→C) — bestätigt Ticket-Befund |
| 189 | `storage/order_ledger_v2.py:138` | `_parse_to_display_naive` | A | Parser-Kontrakt (`None` = nicht parsebar), keine Ledger-Operation; die riskante Entscheidung trifft erst der Aufrufer (#192). | Belassen; siehe #192. | ja (C?→A) |
| 190 | `storage/order_ledger_v2.py:144` | `_parse_to_display_naive` | A | Kontext: ohne `display_tz` wird ohne TZ-Konversion weitergerechnet. | Belassen, Log ergänzen. | nein |
| 191 | `storage/order_ledger_v2.py:158` | `_parse_to_display_naive` | A | Kontext: `astimezone`-Fehler ⇒ naive Ortszeit, max. Tagesgrenzen-Offset. | Belassen, Log ergänzen. | ja (C?→A) |
| 192 | `storage/order_ledger_v2.py:183` | `display_day_key_for_order` | A | Kontext: Fallback von `now_display()` auf die Prozess-Uhr, Abweichung höchstens ein Kalendertag. | Belassen, aber loggen; **separat prüfen** (nicht dieser Handler): dass eine Order *ohne parsebaren Zeitstempel* auf **heute** gebucht wird, verfälscht Tages-Stats und Tages-Caps — besser explizit als `day_key: unknown` führen. | ja (C?→A) |
| 193 | `storage/order_ledger_v2.py:196` | `display_day_key_now` | A | Kontext: wie #192, Fallback auf die Prozess-Uhr. | Belassen, Log ergänzen. | ja (C?→A) |
| 194 | `storage/order_ledger_v2.py:269` | `_token` | A | Defensiver Normalisierer für `str/Enum/int`; `""` führt zum explizit gezählten `unknown_side`, also kein still unterschlagener Zustand (und `str(value)` wirft praktisch nie). | Belassen. | ja (C?→A) |
| 195 | `storage/tenant_meta_store.py:37` | `load_tenant_config_body` | C | `return None` ⇒ `load_config(tid)` liefert `apply_effective_config(default_cfg, None)`, d.h. der Satelliten-Tenant läuft still auf der **Operator-config.json** (trading_mode, `max_usdt_per_trade`, Risiko-Limits, Profil) statt auf seiner eigenen. | Fail-closed: Lesefehler propagieren; ein Tenant ohne gelesene Config darf keinen Zyklus/keine Order fahren — "nicht gespeichert" (`None`) und "Read fehlgeschlagen" strikt trennen. | ja (A?→C) |
| 196 | `storage/tenant_meta_store.py:61` | `save_tenant_config` | C? | `return False` erreicht `data_manager.save_config`; einige Aufrufer prüfen (`strategies/registry.py:523/556`, `services/strategy_auto_tuner.py:71`), andere nicht (`onboarding_commands.py:435`) ⇒ eine verlorene Limit-/Modus-Änderung kann unbemerkt bleiben. | Log auf ERROR; alle `save_config`-Aufrufer auf Auswertung prüfen, insbesondere Onboarding und Telegram-Settings. | ja (A?→C?) |
| 197 | `storage/tenant_meta_store.py:83` | `load_tenant_watchlist` | C | "Leer statt Fehler": `[]` ⇒ `data_manager.load_watchlist` fällt auf die **Default-/Operator-Watchlist-Datei** durch; der Tenant handelt fremde Symbole, und ein anschließendes `remove_coin`/`add_coin` schreibt diese fremde Liste als **seine** Watchlist nach Mongo. | Fail-closed: Lesefehler propagieren statt `[]`; `load_watchlist` darf bei einem Fehler nicht auf die Default-Datei zurückfallen. | ja (A?→C) |
| 198 | `storage/tenant_meta_store.py:99` | `save_tenant_watchlist` | C | `return False`, das niemand liest: `add_coin` (dm:320), `remove_coin` (dm:340), `save_full_coin` (dm:675) und der Gate-Prune (dm:537) ignorieren den Rückgabewert und melden dem Nutzer trotzdem "✅ … wurde entfernt" — das entfernte Coin bleibt im Handelsuniversum. | Log auf ERROR; Rückgabewert in allen `save_watchlist`-Aufrufern auswerten und die Erfolgsmeldung erst nach bestätigtem Write senden. | ja (A?→C) |
| 199 | `storage/tenant_registry.py:202` | `link_tenant_owner_chat` | A | Der Write-Fehler wird ehrlich als `(False, "Verknüpfung fehlgeschlagen.")` an den Aufrufer und damit an den Telegram-Nutzer gemeldet; kein Ledger-/Cash-/Positions-Zustand betroffen. | Belassen; Log auf ERROR heben. | ja (C?→A) |
| 200 | `storage/tenant_registry.py:220` | `find_tenant_by_owner_chat_id` | C | `return None` ist ununterscheidbar von "keine Bindung": im Routing (`core/tenant_routing.py:96`) fällt es zwar fail-closed aus (Chat wird abgewiesen), aber in `link_tenant_owner_chat:178` **umgeht** ein Fehler-`None` die Exklusivitätsprüfung ("oldest binding wins") ⇒ ein fremder Tenant kann den Chat übernehmen und damit dessen Order-Bestätigungen empfangen. | Lesefehler von "nicht gefunden" trennen (Exception oder Sentinel); `link_tenant_owner_chat` muss bei einem Lookup-Fehler die Verknüpfung verweigern. | nein |
| 201 | `services/ledger_sync.py:36` | `migrate_legacy_positions` | A | Einmalige Legacy-Kopie (`positions.json` → `positions.paper.json`), geloggt; die Positionsmengen werden ohnehin aus dem Order-Ledger rekonstruiert, es entsteht kein falsch-echter Zustand. | Belassen; grenzwertig: existiert die Legacy-Datei und schlägt die Kopie fehl, wäre ERROR + Boot-Abbruch sauberer. (Praktisch weitgehend tot seit dem `data/`-Umzug.) | nein |
| 202 | `services/ledger_sync.py:428` | `sync_positions_on_startup` | A | Kontext: nur der `recent_high`-Backfill entfällt; der Peak wird vom Preiszyklus (`update_market_snapshot`) wieder aufgebaut, und ein zu niedriger Peak lässt den Trail-Stop später statt früher feuern. | Belassen; Log auf ERROR heben, damit ein dauerhaft fehlender Backfill sichtbar ist. | nein |
| 203 | `services/trading_service.py:276` | `_execute_order_locked` | A | Betrifft ausschließlich den Telegram-Positions-Snapshot nach einem Fill. | Keiner. | nein (`A?`) |
| 204 | `services/trading_service.py:283` | `_execute_order_locked` | C (niedrig) | Der `try` umschließt `_maybe_auto_short_after_sell`, das über `_execute_order_locked` eine vollständige SHORT-Order fährt (Risk-Eval, Ledger-Create/Update, Adapter-Execute) — eine Exception wird auf **DEBUG** verschluckt, sodass eine im Ledger auf `"executing"` hängengebliebene Order unsichtbar bleibt. | log ERROR statt DEBUG und die verwaiste Ledger-Order auf `failed` setzen; **nicht** raisen, der SELL ist bereits ausgeführt. | ja: `A?` → **C** |

## Anhang — Zusammenfassungen der Bewerter (verbatim)

### Slice 1

## Zusammenfassung

**Verteilung:** 26 × A · 0 × B · 27 × C (fest) · 11 × C? = 64
(C? = #112, #113, #115, #125, #129, #130, #133, #140, #143, #156, #157)

Kein Handler in diesem Slice ist ein reiner B-Fall; am nächsten kommen #143 (nacktes `pass` um einen Ledger-Write) und #146 (schluckt Import-/Programmierfehler), beide aber mit Zustands- bzw. Anzeigebezug, deshalb C? / A.

### Die 5 gravierendsten C-Stellen

1. **#145 `load_live_trade_history` (Z. 1332)** — Ein einzelner transienter Mongo-Lesefehler liefert eine **leere** Live-Trade-Historie ohne Refuse-Guard. Der nächste `record_live_trade` macht load→append→save und ersetzt damit die **komplette Live-Trade-Historie in Mongo durch einen einzigen Trade**; `virtual_balance` wird gleichzeitig auf das volle Startkapital zurückgerechnet.
2. **#147 / #149 `_load_orders_json` + `load_orders` (Z. 1528 / 1552)** — Korrupte `orders.*.json` bzw. ein Mongo-Ausfall bei mongo-konfiguriertem `paper`/`live` (dort wird die JSON gar nicht gepflegt) ergeben ein **leeres Orderbuch**. `resolve_sim_cash_balance` replayt 0 Fills → `risk_manager._available_usdt` sieht volles Startkapital und keine offenen Positionen → der Bot kauft die Watchlist ein zweites Mal, während `_reconcile_scoped_trade_history` den Phantom-Kontostand auch noch zurückschreibt.
3. **#150 `_reject_demo_mongo_orders_downgrade` (Z. 1584)** — Der Handler umschließt die Schutzprüfung selbst: scheitert der Vorab-Read des bestehenden Buchs, wird `False` = „kein Downgrade" gemeldet und **genau der Wipe zugelassen, den der Guard verhindern soll** (voller Demo-Ledger wird durch ein fast leeres Memory-Abbild überschrieben).
4. **#127 / #128 `_load_default_config_from_disk` + `_load_tenant_config_body` (Z. 690 / 720)** — Eine unlesbare `config.json` ersetzt die gesamte Basis-Config still durch **hartkodierte Defaults** (`max_usdt_per_trade: 150`, `max_open_positions: 5`, `stop_loss_pct: 12.0`); ein Mongo-Fehler lässt einen Tenant still mit der **Operator-Config** (fremden Positionsgrößen und Limits) handeln. Beide ändern Order-Sizing direkt, ohne dass irgendwo ERROR erscheint.
5. **#148 `_save_orders_json` (Z. 1540)** — Schreibfehler → `return False` ohne eigenes Log, und `OrderService._save` verwirft den Bool an allen sechs Aufrufstellen, u. a. bei `update_status(..., "filled")`. Ein real ausgeführter Fill landet dann nie im Ledger; Cash-Replay, Positionen und PnL laufen dauerhaft auseinander.

Ehrenvolle Erwähnung: **#152 `_load_positions_json`** — leeres Positions-Doc → `_preserve_locks_from_existing_doc` rettet keine Locks mehr, ein Flush löscht damit einen ops-gesetzten Positions-Lock, und `attach_lock_from_ledger` findet ihn ebenfalls nicht → eine gesperrte Position wird wieder verkaufbar.

### Widerspruch zur Heuristik (24 Stellen)

**A? → C (Heuristik zu milde, 15):** #116, #124, #126, #127, #128, #138, #142, #144, #145, #147, #149, #150, #151, #152, #154, #155 — durchweg Load-/Save-Stellen, die die Heuristik wegen des `log(...)`-Aufrufs als „nur Log" eingestuft hat, deren Default/`False` aber ein leeres Ledger bzw. eine fremde Config als echten Wert weitergibt oder deren Rückgabewert kein Aufrufer prüft.
*(Zusätzlich A? → C? bei #113, #125, #140, #156.)*

**C? → A (Heuristik zu streng, 9):** #110, #111 (Cleanup-Handler innerhalb eines Pfads, der bereits ERROR loggt und `raise`t), #134, #136, #159, #160, #162, #163, #165, #167 (Sentiment-/Memory-/Sandbox-Stores ohne Wirkung auf Größe, Freigabe, Positionsbestand, Cash oder Ledger).

**`?` aufgelöst:** #109 → A, #112/#115/#129/#130 → C? (gleiche Fail-Open-Klasse wie #108, praktisch aber unerreichbar, weil `_should_use_mongo_for_tenant_config` intern bereits alles schluckt — beim Fix von #108 mit entfernen), #146 → A.

### Querschnittsbefund

Ein struktureller Fehler zieht sich durch die ganze Datei: **`save_*` gibt `False` zurück, und der Aufrufer wirft den Bool weg.** Betroffen sind `OrderService._save` (6 Stellen, inkl. Fill-Persistierung), `record_trade`/`record_live_trade`, `ledger_sync.py:168/385`, `remove_coin`/`add_coin` und `onboarding_commands.py:435`. Ein Fix nur in `data_manager.py` reicht hier nicht — entweder müssen die Save-Funktionen bei Fehlschlag werfen, oder Phase 1 muss die Aufrufer in Slice-übergreifenden Tickets mitziehen. Zweiter Querschnitt: der Refuse-Guard `_should_refuse_demo_json_fallback` deckt nur `demo` + Multi-Tenant ab, obwohl `_ledger_writes_json` für **jedes** mongo-konfigurierte Scope `False` ist — der JSON-Fallback liest dort systematisch ein totes Buch (#140, #149, #154).

---

### Slice 2

## Zusammenfassung

**Anzahl:** A = 25 · B = 0 · C = 30 (davon 24 klar `C`, 6 als `C?` markiert: #15, #23, #30, #40, #49, #50).

### C-Stellen in `_evaluate_impl` (übersprungener Guard)

| Zeile | Übersprungener Guard / Code |
|---|---|
| 270 | `stablecoin_blocked` (permanenter Stablecoin-Buy-Rail) |
| 285 | *alle* `not has_position`-Guards (Short-Lot wird als Long adoptiert) — `C?` |
| 305 | `correlated_tier_selloff` |
| 330 | `universe_trade_cap` (Universe-Split, observe-only) |
| 350 | `gainer_chase_guard` (Issue #162) |
| 380 | `market_block` (globale Fusion-/Oracle-Buy-Sperre, CRASH/Warmup) |
| 423 | `coin_memory_soft_block` |
| 438 | WQE-Tenant-Kontext (falscher Quality-Store) — `C?` |
| 481 | `watchlist_quality` (WQE-Gate) |
| 488 | `sensor_reentry_cooloff` (Deny kommt als Variable `cool` zurück) |
| 517 | `venue_liquidity_block` |
| 536 | `macro_calendar_block` |
| 564 | kein Deny, aber `execute_eviction_sell` — echter Verkauf, Fehler verschluckt |
| 605 | DCA-Sizing über tier-eingefrorene Params — `C?` |

**Abgleich mit dem AST-Lauf:** ein Muster, das literal `return RiskDecision(approved=False, …)` im `try` sucht, findet genau 9 (270, 305, 330, 350, 380, 423, 481, 517, 536). Die 10. Stelle ist **Z. 488**, wo das Deny als Variable (`return cool`) zurückkommt — die verpasst ein reiner AST-Match. Dazu kommen 564 (Ledger-/Execution-Write statt Deny), 285, 438 und 605.

### Die gravierendsten Stellen

1. **`:1381` `_dynamic_size` (globale Fusion-Bias)** — Fällt `get_global_market_bias` aus, bleibt `global_mult = 1.0` (die CRASH-Nullung `total = 0.0` entfällt) *und* `global_regime` bleibt `None`, wodurch `size_boost_for_regime` auf `size_boost_default = 1.35` fällt. **Ein Oracle-/Fusion-Ausfall macht die Order nicht kleiner, sondern 35 % größer** — und zwar mitten im Crash, weil dann auch #20 (`:380`) den Buy nicht mehr blockt. Das ist das gefährlichste Paar in der Datei.
2. **`:1919` `_trade_cooldown_blocked`** — Ein einziger unlesbarer `last_trade_at`-Timestamp in einer Position lässt `return False, ""` feuern und schaltet für dieses Symbol **sämtliche** Cooldowns ab (Rebuy-after-Sell, DCA-Intervall, Trending-Cap). Der Bot kann dieselbe Position im Minutentakt nachkaufen, ohne dass irgendwo ein Log entsteht.
3. **`:564` `_evaluate_impl` (Slot-Eviction)** — Im `try` steckt `execute_eviction_sell`, also ein realer Verkauf einer bestehenden Position. Scheitert etwas nach der Order-Abgabe, wird der Fehler verschluckt und die Ablehnungsmeldung sagt nur „max_open_positions": eine Position ist verkauft, niemand erfährt davon, und der Buy, für den evakuiert wurde, kommt trotzdem nicht zustande.
4. **`:1119` `_market_bias_for_cash`** — Der „fail-open to neutral"-Fallback verwirft `block_buys`, setzt `size_mult = 1.0` und `regime = None`. In `_resolve_position_capacity` entfällt damit `regime_adj["CRASH"] = −12` Slots, in `_evaluate_cash_policy` steigen Spendable/Size. Ein stiller Store-Ausfall öffnet also im Crash 12 zusätzliche Positions-Slots.
5. **`:1569` `_fill_sell_amount_from_open_lot`** — `fraction = 1.0` verkauft bei einem Fehler in `sell_fraction_for_signal` das ganze Lot statt der geplanten 10–30 %. Ein Teil-TP wird still zur Vollliquidation; der Nutzer sieht nur eine ausgeführte „SELL_20".

### Widerspruch zur Heuristik

**Heuristik `C?` → Urteil `A`** (15 Stellen, alle geprüft, keine davon fail-open):
- **Reine Logging-/Telemetrie-Hüllen:** #8 (`:94`), #11 (`:187`), #13 (`:246`), #19 (`:368`), #24 (`:473`), #41 (`:1379`), #61 (`:2057`).
- **Bereits fail-closed** (der Handler *ist* das Deny): #9 (`:146`), #52 (`:1787`), #55 (`:1836`), #47 (`:1556`, führt in „No amount to sell").
- **Fehlerrichtung restriktiv** (bei Exception wird mehr blockiert, nicht weniger): #21 (`:411`), #35 (`:907`), #51 (`:1692`).
- **Nur ein Size-*Boost* fällt weg** (Größe wird kleiner): #32 (`:665`).

**Heuristik `?` → Urteil `C`** (2 Stellen, echte Fail-Opens, die die Vorschätzung nicht gesehen hat):
- #43 (`:1406`) — `coin_bias = 1.0` statt bis zu 0.5 → bis zu doppelte Positionsgröße auf memory-abgestraften Coins.
- #48 (`:1569`) — `fraction = 1.0` → Vollverkauf statt Teilverkauf.

**Zusätzlicher Fund (kein Handler-Thema, aber in Slice 2 aufgefallen):** `risk/risk_manager.py:469` übergibt `mode=mode` an `log_buy_block`, aber `mode` wird in `_evaluate_impl` nie zugewiesen (Z. 433 ruft `wqe_mode(raw)` ohne Zuweisung). Der resultierende `NameError` wird vom Handler in Z. 473 geschluckt — **`log_buy_block` läuft für WQE-Blocks also nie**, und `note_buy_blocked(reason)` in derselben Zeile davor ist der einzige Grund, warum überhaupt etwas gezählt wird. Das Deny selbst ist nicht betroffen.

---

### Slice 3

## Zusammenfassung

**Zählung:** A = 15 · B = 0 · C = 16 (davon 6 gravierend, 5 mittel, 5 niedrig)

- **A (15):** 1, 4, 5, 6, 64, 65, 67, 69, 71, 74, 79, 80, 81, 95, 203
- **B (0):** —
- **C (16):** 2, 3, 7, 63, 66, 68, 70, 72, 73, 75, 76, 77, 78, 96, 97, 204

### Die gravierendsten C-Stellen

1. **`execution/gate_adapter.py:115` (`execute`) — Order-Zustand „unbekannt" wird als „nicht passiert" gebucht.**
   Gate quittiert `create_market_buy_order` mit `RequestTimeout`, die Order füllt aber an der Börse; der Handler liefert `executed=False`, `link_execution_result` schreibt Ledger-Status `"failed"`, `positions.live.json` bleibt leer → eine reale Position ohne Stop, ohne Trail, ohne Exit-Management, und die nächste Balance-Prüfung rechnet mit Geld, das bereits ausgegeben ist. Derselbe `try` umschließt auch `_sync_local_ledger`, d.h. ein Fehler beim Ledger-Schreiben *nach* dem Fill erzeugt exakt dieselbe Divergenz.

2. **`risk/slot_eviction_runtime.py:399` (+ `or 1.0` in Zeile 392) — Eviction-SELL mit Fantasiepreis.**
   `svc.market.get_price()` existiert auf `MarketService` gar nicht, der `except: pass` verdeckt die AttributeError bei jedem einzelnen Aufruf; `pos["mark_price"]` wird nirgends geschrieben, also ist der Orderpreis immer der **Entry-Preis** — und wenn `average_entry` fehlt, **1.0**. Damit gehen `usdt_amount = sell_amt * 1.0` in die Risk-Checks und (im virtuellen/Enhanced-Pfad) ein Verkauf zu $1.00 in den Trade-Record.

3. **`risk/slot_eviction.py:554` (`plan_slot_eviction`) — Position-Lock fällt offen.**
   Wirft `eviction_blocked`/`get_position` (fehlendes Lot, defektes `lock`-Dokument, Import-Fehler), bleibt das gesperrte Lot im Opferpool und kann als Eviction-Opfer zwangsverkauft werden. Der identische Check in `risk_manager.py:179` antwortet auf denselben Fehler mit `approved=False`. Der nachgelagerte `auto_sell_blocked` deckt nur die Default-Modi ab — ein reines `no_evict`-Lock rutscht durch.

4. **`risk/slot_eviction_runtime.py:486` und `:502` — beide Must-Gate-Blöcke fallen offen.**
   Ist `market_policy_fusion` (486) oder das Coin-Memory (502) kurz nicht erreichbar, gelten `block_buys=False`, `regime="NEUTRAL"`, `soft_block=False`, `structure_risk=False`. In `score_entry_demand` sind das vier von sechs Must-Fail-Gates: der Bot verkauft im CRASH-Regime eine gehaltene Position, um Platz für einen Entry zu machen, den sein eigenes Memory als `soft_block`/`structure_risk` führt.

5. **`risk/slot_eviction_runtime.py:112` (`_hours_since`) — `999.0` ist der maximal opferfreundliche Wert.**
   Ein Lot mit unparsbarem `first_buy_at` gilt als 999 h alt (→ `min_hold`-Veto weg) *und* als 999 h idle (→ `idle_term` im `free_score` maximal): genau die Position mit dem kaputtesten Datensatz wird zuerst verkauft.

6. **`risk/slot_eviction_runtime.py:532` (`_gp`) — Memory-Ausfall wird zu „neutralem Profil".**
   Ein einziger `return None` setzt für alle Opfer *und* den Entry `keep = 0.5`, entfernt den `prefer_hard_keep`-Schutz, die Klasse-C-Eskalation und den Memory-Anteil des Swap-Gates — und maskiert dabei die Handler in 174 und 233, sodass die Eviction weiterläuft, als sei das Memory bloß leer.

### Widerspruch zur Heuristik (7 Stellen)

| # | Heuristik | Urteil | Warum |
|---|---|---|---|
| 1 | `C?` | **A** | `return 0.0` lässt `_execute_buy` ablehnen — fail-closed, nicht fail-open (nur schlechte Fehlermeldung + toter Code in Zeile 68). |
| 2 | `A?` | **C** | Nicht „nur Log": der Handler bucht einen möglicherweise ausgeführten Live-Trade als `failed` und deckt auch den Ledger-Schreibpfad ab. |
| 5 | `C?` | **A** | `1.0` ist der Boden des Wertebereichs (`if boost < 1.0: boost = 1.0`) und der Aufrufer wirkt nur bei `> 1.0` — der Fehlerpfad kann keine Size vergrößern. |
| 6 | `C?` | **A** | Erfolgspfad liefert `max(base_max, md_max) ≥ base_max`; `base_max` ist die engste Obergrenze. |
| 65 | `C?` | **A** | `[]` → `evidence_delta_from_hits([]) == 0.0` → `keep_rag == keep_profile`; wirkungsneutral. |
| 74 | `C?` | **A** | `return False` lässt das `spendable`-Must-Gate scheitern → keine Eviction; explizit als Fail-Closed kommentiert. |
| 204 | `A?` | **C** | Der `try` fährt über `_maybe_auto_short_after_sell` eine komplette SHORT-Order inkl. Ledger-Writes; das Verschlucken auf **DEBUG** macht eine auf `executing` hängende Order unsichtbar. |

Zusätzlich als **C** eingestuft wurden die von der Heuristik unklassifizierten (`?`) Stellen **68, 70, 96, 97**; als **A** aufgelöst wurden **64, 67, 69, 71, 79, 80, 81, 95**.

### Randnotizen (kein Handler, aber im selben Fehlerbild)

- `risk/slot_eviction_runtime.py:392`: `price = float(pos.get("mark_price") or pos.get("average_entry") or 0) or 1.0` — `mark_price` wird im gesamten Positions-Store nie geschrieben; der `or 1.0` ist die eigentliche Ursache zu #72.
- `try_slot_eviction_on_max_open` ruft `plan_for_blocked_entry` **ohne** `prices=` auf, deshalb ist in `build_victim_candidates` immer `price == entry` und damit `gain_pct == 0.0` für jeden Kandidaten — das Ranking (`class_a`, `flat`-Term im `free_score`) arbeitet auf Scheinwerten.
- `execution/gate_adapter.py:68`: `self._last_api_error = ""` steht nach einem `return` und ist unerreichbar → ein einmaliger `INVALID_KEY` bleibt dauerhaft im Telegram-Hinweis stehen.
- `risk/slot_eviction.py:554` prüft im Gegensatz zu `risk_manager.py:167` **nicht** `attach_lock_from_ledger` — ein nur im Order-Ledger hinterlegtes Lock ist im Eviction-Vorfilter unsichtbar.

---

### Slice 4

## Zusammenfassung

**Verteilung:** A = 25 · B = 1 · C/C? = 28 (davon 8 als `C?` markiert: #172, #178–#183, #196).

### Die gravierendsten C-Stellen

1. **`strategies/positions.py:387` `load_positions` (#98) — schwerste Stelle im Slice.**
   `store.clear()` steht *vor* dem `try`. `data_manager.load_positions_document` raist in Multi-Tenant/Demo **bewusst** ("refusing JSON fallback"), um fail-closed zu sein — dieser Handler verwandelt genau das in einen stillen leeren Positionsbestand. *Failure-Szenario:* Mongo-Aussetzer beim Tenant-Bootstrap ⇒ RAM-Store leer ⇒ der Bot hält sich für positionslos, kauft gehaltene Symbole erneut, verkauft/stoppt bestehende Lots nicht — und der nächste `flush_positions` (läuft nach **jedem** Trade) ersetzt via `replace_one` das persistierte Positions-Dokument durch `{}`: peak_amount, sold_percent, recent_high, RSI-Tiers, DCA-Zähler, entry_source und **Locks** sind weg.

2. **`storage/tenant_meta_store.py:37` `load_tenant_config_body` (#195).**
   *Failure-Szenario:* Ein Mongo-Fehler beim Laden der Tenant-Config ⇒ `apply_effective_config(default_cfg, None)` ⇒ der Satelliten-Tenant fährt mit der **Operator-config.json** (inkl. `trading_mode`, `max_usdt_per_trade`, Risiko-Limits) weiter, ohne dass irgendetwas außer einer WARNING-Zeile darauf hinweist.

3. **`storage/order_ledger_v2.py:110` `get_order_ledger_v2` (#188) + `services/order_service.py:433` `_dual_write_v2` (#86).**
   *Failure-Szenario:* Mongo ist beim ersten Store-Zugriff kurz weg ⇒ der Prozess ist für seine **gesamte Laufzeit** auf einen flüchtigen `MemoryOrderLedgerV2` gepinnt (kein Log), und jeder fehlgeschlagene v2-Write wird von `_dual_write_v2` ebenfalls ohne Log verschluckt. Solange `ORDER_LEDGER_V2_READS=0` ist, deckt der Legacy-Blob das ab — mit aktivierten v2-Reads liefert der Tages-Index ein leeres Buch, und der Gainer-Tageskauf-Cap (`services/gainer_signal/bot_http.py:156` → `list_day_filled_all`) zählt gegen 0.

4. **`storage/tenant_meta_store.py:83/99` Watchlist (#197/#198).**
   *Failure-Szenario:* Lesefehler ⇒ leere Tenant-Watchlist ⇒ `data_manager.load_watchlist` fällt auf die **Operator-Watchlist** zurück ⇒ der Tenant handelt fremde Symbole; ein anschließendes `/remove` schreibt diese fremde Liste als seine eigene nach Mongo — und meldet dem Nutzer trotz `return False` "✅ … wurde entfernt".

5. **`storage/grid_plan_store.py:67` `load_grid_plans_document` (#170).**
   *Failure-Szenario:* Mongo-Aussetzer (Log nur auf DEBUG) ⇒ `plans: {}` ⇒ `GridStrategy._load_or_init_plan` baut ein **neues Grid um den aktuellen Preis** (frische Buy/Sell-Level auf einer laufenden Position), und der nächste `save_grid_plan` (Read-Modify-Write) **löscht alle übrigen Grid-Pläne** des Tenants.

### „Leer statt Fehler" — alle Stellen dieser Klasse

| # | Stelle | Leerer Wert | Wird gelesen als |
|---|---|---|---|
| 98 | `strategies/positions.py:387` | `{}` + geleerter RAM-Store | "keine offenen Positionen" |
| 170 | `storage/grid_plan_store.py:67` | `plans: {}` | "kein Grid-Plan" ⇒ Re-Center + Löschen der übrigen Pläne |
| 172 | `storage/grid_plan_store.py:122` | `None` | "kein Legacy-Plan" |
| 175 | `storage/ledger_router.py:137` | `{"orders": []}` | "keine Orders" |
| 176 | `storage/ledger_router.py:156` | `{"positions": {}}` | "keine Positionen" |
| 177 | `storage/ledger_router.py:182` | `{"virtual_balance": 5000.0, "realized_pnl": 0.0, "trades": []}` | **erfundenes Startguthaben** |
| 178/180/182 | `storage/ledger_router.py:229/245/261` | JSON-Fallback ohne Existenz-/Frische-Check | kann über #175–#177 leer/veraltet sein |
| 188 | `storage/order_ledger_v2.py:110` | flüchtiger Memory-Store | "Tages-Orderbuch leer" (bei aktiven v2-Reads) |
| 195 | `storage/tenant_meta_store.py:37` | `None` | "Tenant hat keine eigene Config" ⇒ Operator-Config |
| 197 | `storage/tenant_meta_store.py:83` | `[]` | "Tenant hat keine Watchlist" ⇒ Operator-Watchlist |
| 200 | `storage/tenant_registry.py:220` | `None` | "Chat ist an keinen Tenant gebunden" |
| 93 | `services/order_service.py:960` | Null-Stats | nur Anzeige (bewusst, dokumentiert) — trotzdem sollte die UI "n/v" statt "0" zeigen |

### Abweichungen von der Heuristik

**Heuristik zu mild (A?/? → C), 21 Stellen:** #86, #98, #99, #100, #102, #105, #170, #175, #176, #177, #178, #179, #180, #181, #182, #183, #188, #195, #196, #197, #198. Dazu #101 als einzige Umstufung `C? → B`.
Gemeinsames Muster: die Heuristik hat „nur Log" gesehen, aber ein `log(...)` ohne Rückmeldung an den Aufrufer ist bei einem Ledger-Read/Write genau die C-Situation — und ein `return False`, das kein Aufrufer prüft (#171, #179–#183, #196, #198), ist funktional ein `pass`.

**Heuristik zu streng (C?/? → A), 18 Stellen:** #82, #84, #85, #87, #88, #89, #90, #91, #92, #93, #184, #186, #189, #191, #192, #193, #194, #199.
Gemeinsames Muster: entweder ein **echter Fallback auf die Source of Truth** (v2-Read scheitert ⇒ Legacy-Blob: #87–#92), reine **TZ-/Parser-Kontrakte** (#82–#85, #189–#194), **Teardown/Ressourcen** (#184, #186) oder ein **ehrlich gemeldeter Fehler** (#199).

### Zwei Befunde außerhalb der zugewiesenen Zeilen (für den Ticket-Owner)

- **`data_manager.py:1518/1612` `_load_orders_json` / `_load_positions_json` und `:1533/1627` `_save_*_json`** sind **zeichengleiche Kopien** von #174–#177 — und im Gegensatz zu `storage/ledger_router.py` liegen sie im **produktiven** Pfad (`load_orders` → `_load_orders_json`, `load_positions_document` → `_load_positions_json`). Der Fix muss an beiden Stellen landen; Priorität hat `data_manager.py`.
- **`storage/ledger_router.py` ist derzeit nicht verdrahtet:** produktiv importiert wird aus dem Modul nur `resolve_ledger_backend`, `ledger_dual_write_enabled` und die `*_SCOPE_FILES`-Konstanten; `resolve_store` / `JsonLedgerStore` / `DualWriteLedgerStore` werden ausschließlich aus Tests aufgerufen. Die 10 C-Zeilen dort sind also *latente* Risiken (relevant bei Verdrahtung), nicht akut — im Gegensatz zu den identischen Mustern in `data_manager.py`.
- Bestätigt (nicht in dieser Slice-Zeilenliste, aber im selben File): **`storage/tenant_registry.py:44-49` `_decrypt`** fängt `(InvalidToken, Exception)` — der zweite Eintrag macht den ersten bedeutungslos — und liefert `""`; `get_gate_credentials` gibt damit still leere Exchange-Credentials zurück (Klasse C: fail-closed, Entschlüsselungsfehler muss den Handel für diesen Tenant stoppen, nicht in einen leeren API-Key münden).
- Angrenzend: **`services/order_service.py` `create_from_request` / `update_status`** ignorieren den Rückgabewert von `self._save(data)` — ein fehlgeschlagener Ledger-Write der Order ist dort unabhängig von den hier klassifizierten Handlern unsichtbar.