# Exception-Inventar Phase 1 (geldrelevante Dateien)

**Stand:** staging @ 3.9.2026 · **Umfang:** 204 Stellen in execution/, risk/, storage/, order_service, portfolio_service, positions, data_manager, ledger_sync, trading_service

Vorläufige Klasse (heuristisch, aus dem Handler-Rumpf):
- **A?** nur Log — vermutlich unkritisch, bleibt
- **B** wirft weiter — in Ordnung
- **C?** schluckt still oder liefert Default — **muss einzeln bewertet werden**; in Sizing/Ledger/Execution-Pfaden Kandidat für Fail-Closed
- **?** unklar

Das Fragezeichen heißt: automatische Einstufung, kein Urteil. Die Spalte *Entscheidung* ist für den Review.

| # | Datei:Zeile | Funktion | Klasse | Hinweis | Handler (erste Zeile) | Entscheidung |
|---|---|---|---|---|---|---|
| 1 | `execution/gate_adapter.py:64` | `_fetch_usdt_balance` | C? | liefert Default | `self._last_api_error = str(e) log(f"Gate balance fetch failed: {e}", "WARNING") return 0.0` | |
| 2 | `execution/gate_adapter.py:115` | `execute` | A? | nur Log | `log(f"Gate execution failed for {order.symbol}: {e}", "ERROR") return TradeResult( execute` | |
| 3 | `execution/gate_adapter.py:169` | `_fetch_base_balance` | C? | liefert Default | `log(f"Gate {base} balance fetch failed: {e}", "WARNING") return 0.0` | |
| 4 | `execution/gate_adapter.py:200` | `_validate_sell_amount` | A? | nur Log | `log(f"Gate market limits check failed for {order.symbol}: {e}", "WARNING") amount = float(` | |
| 5 | `risk/moderate_deploy.py:131` | `size_boost_for_regime` | C? | liefert Default | `return 1.0` | |
| 6 | `risk/moderate_deploy.py:148` | `effective_max_total_multiplier` | C? | liefert Default | `return float(base_max)` | |
| 7 | `risk/position_capacity.py:355` | `count_open_book_memory_signals` | C? | schluckt still | `continue` | |
| 8 | `risk/risk_manager.py:94` | `evaluate` | C? | schluckt still | `pass` | |
| 9 | `risk/risk_manager.py:146` | `_evaluate_impl` | C? | liefert Default | `return RiskDecision( approved=False, message=f"side_check_error: {exc}"[:200], code="side_` | |
| 10 | `risk/risk_manager.py:179` | `_evaluate_impl` | A? | nur Log | `try: from logger import log log( f"position_lock sell check error {order.symbol}: {exc}",` | |
| 11 | `risk/risk_manager.py:187` | `_evaluate_impl` | C? | schluckt still | `pass` | |
| 12 | `risk/risk_manager.py:238` | `_evaluate_impl` | A? | nur Log | `try: from logger import log log( f"position_lock dca check error {order.symbol}: {exc}",` | |
| 13 | `risk/risk_manager.py:246` | `_evaluate_impl` | C? | schluckt still | `pass` | |
| 14 | `risk/risk_manager.py:270` | `_evaluate_impl` | C? | schluckt still | `pass` | |
| 15 | `risk/risk_manager.py:285` | `_evaluate_impl` | ? |  | `hop_short = False` | |
| 16 | `risk/risk_manager.py:305` | `_evaluate_impl` | C? | schluckt still | `pass` | |
| 17 | `risk/risk_manager.py:330` | `_evaluate_impl` | C? | schluckt still | `pass` | |
| 18 | `risk/risk_manager.py:350` | `_evaluate_impl` | C? | schluckt still | `pass` | |
| 19 | `risk/risk_manager.py:368` | `_evaluate_impl` | C? | schluckt still | `pass` | |
| 20 | `risk/risk_manager.py:380` | `_evaluate_impl` | C? | schluckt still | `pass` | |
| 21 | `risk/risk_manager.py:411` | `_evaluate_impl` | C? | schluckt still | `pass` | |
| 22 | `risk/risk_manager.py:423` | `_evaluate_impl` | C? | schluckt still | `pass` | |
| 23 | `risk/risk_manager.py:438` | `_evaluate_impl` | ? |  | `tid = "default"` | |
| 24 | `risk/risk_manager.py:473` | `_evaluate_impl` | C? | schluckt still | `pass` | |
| 25 | `risk/risk_manager.py:481` | `_evaluate_impl` | C? | schluckt still | `pass` | |
| 26 | `risk/risk_manager.py:488` | `_evaluate_impl` | C? | schluckt still | `pass` | |
| 27 | `risk/risk_manager.py:517` | `_evaluate_impl` | C? | schluckt still | `pass` | |
| 28 | `risk/risk_manager.py:536` | `_evaluate_impl` | C? | schluckt still | `pass` | |
| 29 | `risk/risk_manager.py:564` | `_evaluate_impl` | C? | schluckt still | `pass` | |
| 30 | `risk/risk_manager.py:605` | `_evaluate_impl` | C? | schluckt still | `pass` | |
| 31 | `risk/risk_manager.py:653` | `_evaluate_impl` | ? |  | `cash_pct = None` | |
| 32 | `risk/risk_manager.py:665` | `_evaluate_impl` | C? | schluckt still | `pass` | |
| 33 | `risk/risk_manager.py:676` | `_evaluate_impl` | ? |  | `sensor_cfg = {}` | |
| 34 | `risk/risk_manager.py:852` | `status_summary` | ? |  | `out["max_open_eff"] = self.config.max_open_positions out["position_capacity_enabled"] = Fa` | |
| 35 | `risk/risk_manager.py:907` | `_sensor_reentry_cooloff_blocked` | C? | schluckt still | `pass` | |
| 36 | `risk/risk_manager.py:920` | `_sensor_reentry_cooloff_blocked` | C? | liefert Default | `return None` | |
| 37 | `risk/risk_manager.py:1119` | `_market_bias_for_cash` | C? | liefert Default | `return {"size_mult": 1.0, "block_buys": False, "regime": None}` | |
| 38 | `risk/risk_manager.py:1127` | `_process_uptime_sec` | C? | liefert Default | `return None` | |
| 39 | `risk/risk_manager.py:1142` | `_open_book_memory_counts` | C? | liefert Default | `return 0, 0, 0` | |
| 40 | `risk/risk_manager.py:1186` | `_resolve_position_capacity` | C? | schluckt still | `pass` | |
| 41 | `risk/risk_manager.py:1379` | `_dynamic_size` | C? | schluckt still | `pass` | |
| 42 | `risk/risk_manager.py:1381` | `_dynamic_size` | C? | schluckt still | `pass` | |
| 43 | `risk/risk_manager.py:1406` | `_dynamic_size` | ? |  | `coin_bias = 1.0 social_summary = ""` | |
| 44 | `risk/risk_manager.py:1428` | `_dynamic_size` | C? | schluckt still | `pass` | |
| 45 | `risk/risk_manager.py:1459` | `_dynamic_size` | ? |  | `cash_pct = None` | |
| 46 | `risk/risk_manager.py:1472` | `_dynamic_size` | ? |  | `md_boost = 1.0` | |
| 47 | `risk/risk_manager.py:1556` | `_fill_sell_amount_from_open_lot` | C? | liefert Default | `return order` | |
| 48 | `risk/risk_manager.py:1569` | `_fill_sell_amount_from_open_lot` | ? |  | `fraction = 1.0` | |
| 49 | `risk/risk_manager.py:1644` | `_resolve_sell_order` | C? | schluckt still | `pass` | |
| 50 | `risk/risk_manager.py:1685` | `_partial_sell_blocked` | C? | schluckt still | `pass` | |
| 51 | `risk/risk_manager.py:1692` | `_partial_sell_blocked` | C? | schluckt still | `pass` | |
| 52 | `risk/risk_manager.py:1787` | `_evaluate_short_or_cover` | C? | liefert Default | `return RiskDecision( approved=False, message=f"short book check failed: {exc}"[:200], code` | |
| 53 | `risk/risk_manager.py:1805` | `_evaluate_short_or_cover` | ? |  | `mcap = None` | |
| 54 | `risk/risk_manager.py:1820` | `_evaluate_short_or_cover` | ? |  | `mcap = None` | |
| 55 | `risk/risk_manager.py:1836` | `_evaluate_short_or_cover` | C? | liefert Default | `return RiskDecision( approved=False, message=f"short cash unknown: {exc}"[:200], code="sho` | |
| 56 | `risk/risk_manager.py:1852` | `_evaluate_short_or_cover` | ? |  | `nav = 0.0` | |
| 57 | `risk/risk_manager.py:1909` | `_trade_cooldown_blocked` | C? | schluckt still | `pass` | |
| 58 | `risk/risk_manager.py:1919` | `_trade_cooldown_blocked` | C? | liefert Default | `return False, ""` | |
| 59 | `risk/risk_manager.py:2018` | `_rebuy_after_sell_blocked` | C? | schluckt still | `pass` | |
| 60 | `risk/risk_manager.py:2025` | `_rebuy_after_sell_blocked` | C? | schluckt still | `pass` | |
| 61 | `risk/risk_manager.py:2057` | `_rebuy_after_sell_blocked` | C? | schluckt still | `pass` | |
| 62 | `risk/risk_manager.py:2180` | `_iter_daily_filled_orders` | C? | schluckt still | `continue` | |
| 63 | `risk/slot_eviction.py:554` | `plan_slot_eviction` | C? | schluckt still | `pass` | |
| 64 | `risk/slot_eviction_rag.py:63` | `enrich_keeps_with_rag` | ? |  | `hits = [] err = True` | |
| 65 | `risk/slot_eviction_rag.py:89` | `_retrieve` | C? | liefert Default | `return []` | |
| 66 | `risk/slot_eviction_runtime.py:112` | `_hours_since` | C? | liefert Default | `return 999.0` | |
| 67 | `risk/slot_eviction_runtime.py:166` | `build_victim_candidates` | ? |  | `notional = amount * price` | |
| 68 | `risk/slot_eviction_runtime.py:174` | `build_victim_candidates` | ? |  | `prof = None` | |
| 69 | `risk/slot_eviction_runtime.py:195` | `build_victim_candidates` | ? |  | `trail_armed = peak_g >= float(cfg.get("protect_peak_gain_pct", 12) or 12)` | |
| 70 | `risk/slot_eviction_runtime.py:233` | `build_victim_candidates` | ? |  | `keep_p[entry_symbol] = 0.55` | |
| 71 | `risk/slot_eviction_runtime.py:353` | `plan_for_blocked_entry` | ? |  | `cands = []` | |
| 72 | `risk/slot_eviction_runtime.py:399` | `execute_eviction_sell` | C? | schluckt still | `pass` | |
| 73 | `risk/slot_eviction_runtime.py:420` | `execute_eviction_sell` | C? | liefert Default | `return {"ok": False, "message": str(e)}` | |
| 74 | `risk/slot_eviction_runtime.py:450` | `resolve_spendable_ok_for_entry` | C? | liefert Default | `# Cannot verify spendable → do not free a slot for an unfundable entry return False` | |
| 75 | `risk/slot_eviction_runtime.py:486` | `try_slot_eviction_on_max_open` | C? | schluckt still | `pass` | |
| 76 | `risk/slot_eviction_runtime.py:502` | `try_slot_eviction_on_max_open` | C? | schluckt still | `pass` | |
| 77 | `risk/slot_eviction_runtime.py:524` | `try_slot_eviction_on_max_open` | C? | schluckt still | `pass` | |
| 78 | `risk/slot_eviction_runtime.py:532` | `_gp` | C? | liefert Default | `return None` | |
| 79 | `risk/slot_eviction_runtime.py:540` | `_gp` | ? |  | `spike = 0.0` | |
| 80 | `risk/slot_eviction_runtime.py:570` | `_gp` | C? | schluckt still | `pass` | |
| 81 | `risk/slot_eviction_runtime.py:592` | `_gp` | C? | schluckt still | `pass` | |
| 82 | `services/order_service.py:123` | `_parse_ts` | C? | liefert Default | `return None` | |
| 83 | `services/order_service.py:143` | `_as_display_naive` | ? |  | `target = None` | |
| 84 | `services/order_service.py:158` | `_as_display_naive` | C? | liefert Default | `return dt.replace(tzinfo=None)` | |
| 85 | `services/order_service.py:171` | `_display_now_naive` | C? | liefert Default | `return _as_display_naive(datetime.now())` | |
| 86 | `services/order_service.py:433` | `_dual_write_v2` | ? |  | `# Fail-open: legacy blob remains source of truth during migration. pass` | |
| 87 | `services/order_service.py:456` | `get_by_id` | C? | schluckt still | `pass` | |
| 88 | `services/order_service.py:472` | `get_by_display_seq` | C? | schluckt still | `pass` | |
| 89 | `services/order_service.py:675` | `list_day_filled_all` | ? |  | `use_v2 = False` | |
| 90 | `services/order_service.py:749` | `list_month_filled_all` | C? | schluckt still | `pass` | |
| 91 | `services/order_service.py:806` | `list_blocked_day_all` | C? | schluckt still | `pass` | |
| 92 | `services/order_service.py:916` | `stats_day_filled` | C? | schluckt still | `pass` | |
| 93 | `services/order_service.py:960` | `stats_day_filled_fast` | C? | schluckt still | `pass` | |
| 94 | `services/order_service.py:1079` | `link_execution_result` | ? |  | `execution["venue"] = {"capture": "missing"}` | |
| 95 | `services/portfolio_service.py:30` | `_default_entry_source` | ? |  | `if s.startswith("gainer_") or s == "gate_prev_top": return s` | |
| 96 | `services/portfolio_service.py:228` | `execute_cover` | ? |  | `hours = 0.0` | |
| 97 | `services/portfolio_service.py:237` | `execute_cover` | C? | schluckt still | `pass` | |
| 98 | `strategies/positions.py:387` | `load_positions` | A? | nur Log | `log(f"Failed to load positions ({target}): {e}", "ERROR")` | |
| 99 | `strategies/positions.py:472` | `_do_save_positions` | A? | nur Log | `log(f"lock preserve on save skip ({target}): {e}", "DEBUG")` | |
| 100 | `strategies/positions.py:476` | `_do_save_positions` | A? | nur Log | `log(f"Failed to save positions ({target}): {e}", "ERROR")` | |
| 101 | `strategies/positions.py:689` | `bind_buy_timeframe` | C? | schluckt still | `pass` | |
| 102 | `strategies/positions.py:918` | `update_position` | ? |  | `params = None` | |
| 103 | `strategies/positions.py:934` | `update_position` | C? | schluckt still | `pass` | |
| 104 | `strategies/positions.py:949` | `update_position` | C? | schluckt still | `pass` | |
| 105 | `strategies/positions.py:1064` | `update_position` | ? |  | `strategy_params = None` | |
| 106 | `data_manager.py:92` | `get_data_file` | A? | nur Log | `log(f"Could not copy {base_name} to {demo_path}: {e}", "WARNING")` | |
| 107 | `data_manager.py:111` | `get_data_file` | A? | nur Log | `log(f"Could not copy {src} to {demo_path}: {e}", "WARNING")` | |
| 108 | `data_manager.py:137` | `_should_use_mongo_for_tenant_config` | C? | liefert Default | `return False  # safe fallback` | |
| 109 | `data_manager.py:165` | `atomic_write_json` | ? |  | `if fd is not None: try: os.close(fd) except Exception: pass` | |
| 110 | `data_manager.py:169` | `atomic_write_json` | C? | schluckt still | `pass` | |
| 111 | `data_manager.py:174` | `atomic_write_json` | C? | schluckt still | `pass` | |
| 112 | `data_manager.py:234` | `load_watchlist` | ? |  | `use_mongo = False` | |
| 113 | `data_manager.py:242` | `load_watchlist` | A? | nur Log | `log(f"Failed tenant_meta_store load_tenant_watchlist for {tid}: {e}", "WARNING")` | |
| 114 | `data_manager.py:260` | `load_watchlist` | C? | liefert Default | `log(f"Failed to load watchlist from {path}: {e}", "WARNING") return []` | |
| 115 | `data_manager.py:273` | `save_watchlist` | ? |  | `use_mongo = False` | |
| 116 | `data_manager.py:280` | `save_watchlist` | A? | nur Log | `log(f"Failed tenant_meta_store save_tenant_watchlist for {tid}: {e}", "WARNING") return Fa` | |
| 117 | `data_manager.py:288` | `save_watchlist` | C? | liefert Default | `log(f"Failed to save watchlist: {e}", "ERROR") return False` | |
| 118 | `data_manager.py:329` | `save_dry_run_expansion` | C? | liefert Default | `return False` | |
| 119 | `data_manager.py:385` | `load_dry_run_expansion` | A? | nur Log | `log(f"Failed to load {path}: {e}", "WARNING") return {"coins": []}` | |
| 120 | `data_manager.py:397` | `load_dry_run_overlay` | A? | nur Log | `log(f"Failed to load {path}: {e}", "WARNING") return {"refreshed_at": "", "source": "", "c` | |
| 121 | `data_manager.py:407` | `save_dry_run_overlay` | C? | liefert Default | `return False` | |
| 122 | `data_manager.py:418` | `load_cmc_trending_overlay` | A? | nur Log | `log(f"Failed to load {path}: {e}", "WARNING") return {"refreshed_at": "", "source": "", "c` | |
| 123 | `data_manager.py:428` | `save_cmc_trending_overlay` | C? | liefert Default | `return False` | |
| 124 | `data_manager.py:610` | `build_merged_watchlist_coins` | A? | nur Log | `log(f"WQE watchlist transform skipped: {e}", "DEBUG")` | |
| 125 | `data_manager.py:628` | `load_effective_watchlist` | A? | nur Log | `log(f"observe universe failed, fallback merge: {e}", "DEBUG")` | |
| 126 | `data_manager.py:664` | `load_trade_watchlist` | A? | nur Log | `log(f"trade universe failed, fallback effective: {e}", "DEBUG") return list(observe_coins)` | |
| 127 | `data_manager.py:690` | `_load_default_config_from_disk` | A? | nur Log | `log(f"Failed to load config.json for default, using hardcoded defaults: {e}", "WARNING") r` | |
| 128 | `data_manager.py:720` | `_load_tenant_config_body` | A? | nur Log | `log(f"Failed tenant_meta_store load_tenant_config_body for {tid}: {e}", "WARNING") return ` | |
| 129 | `data_manager.py:738` | `load_config` | ? |  | `use_mongo = False` | |
| 130 | `data_manager.py:796` | `save_config` | ? |  | `use_mongo = False` | |
| 131 | `data_manager.py:807` | `save_config` | C? | liefert Default | `log(f"Failed tenant_meta_store save_tenant_config for {tid}: {e}", "WARNING") return False` | |
| 132 | `data_manager.py:816` | `save_config` | C? | liefert Default | `return False` | |
| 133 | `data_manager.py:827` | `load_x_accounts` | C? | liefert Default | `log(f"Failed to load {path}: {e}", "WARNING") return []` | |
| 134 | `data_manager.py:837` | `save_x_accounts` | C? | liefert Default | `return False` | |
| 135 | `data_manager.py:848` | `load_x_posts` | A? | nur Log | `log(f"Failed to load {path}: {e}", "WARNING") return {"posts": []}` | |
| 136 | `data_manager.py:858` | `save_x_posts` | C? | liefert Default | `return False` | |
| 137 | `data_manager.py:897` | `load_demo_data` | A? | nur Log | `log(f"Could not seed demo positions: {e}", "WARNING")` | |
| 138 | `data_manager.py:949` | `_load_trade_history_json` | A? | nur Log | `log(f"Failed to load {path}: {e}", "WARNING") return _default_trade_history(scope, config)` | |
| 139 | `data_manager.py:959` | `_save_trade_history_json` | C? | liefert Default | `return False` | |
| 140 | `data_manager.py:975` | `load_trade_history_document` | A? | nur Log | `if _should_refuse_demo_json_fallback(scope, cfg) or multi_tenant_enabled(): log( f"Mongo t` | |
| 141 | `data_manager.py:1027` | `reconcile_demo_trade_history_on_startup` | A? | nur Log | `log(f"Multi-tenant demo trade_history reconcile skipped: {e}", "WARNING")` | |
| 142 | `data_manager.py:1045` | `save_trade_history_document` | A? | nur Log | `log(f"Mongo trade_history save failed ({scope}): {e}", "ERROR") ok = False` | |
| 143 | `data_manager.py:1298` | `_reconcile_live_trade_sources` | C? | schluckt still | `pass` | |
| 144 | `data_manager.py:1313` | `_load_live_trade_history_json` | A? | nur Log | `log(f"Failed to load {path}: {e}", "WARNING") return {"trades": [], "total_pnl": 0.0, "rea` | |
| 145 | `data_manager.py:1332` | `load_live_trade_history` | A? | nur Log | `log(f"Mongo live trade_history load failed: {e}", "WARNING") history = {"trades": [], "tot` | |
| 146 | `data_manager.py:1383` | `record_trade` | ? |  | `if typ in ("BUY", "SHORT"): history["open_positions"] = history.get("open_positions", 0) +` | |
| 147 | `data_manager.py:1528` | `_load_orders_json` | A? | nur Log | `log(f"Failed to load {path}: {e}", "WARNING") return _empty_orders(scope)` | |
| 148 | `data_manager.py:1540` | `_save_orders_json` | C? | liefert Default | `return False` | |
| 149 | `data_manager.py:1552` | `load_orders` | A? | nur Log | `if _should_refuse_demo_json_fallback(scope, cfg) or multi_tenant_enabled(): log( f"Mongo o` | |
| 150 | `data_manager.py:1584` | `_reject_demo_mongo_orders_downgrade` | A? | nur Log | `log(f"Demo orders downgrade guard failed: {e}", "WARNING")` | |
| 151 | `data_manager.py:1602` | `save_orders` | A? | nur Log | `log(f"Mongo orders save failed ({scope}): {e}", "ERROR") ok = False` | |
| 152 | `data_manager.py:1622` | `_load_positions_json` | A? | nur Log | `log(f"Failed to load {path}: {e}", "WARNING") return _empty_positions(scope)` | |
| 153 | `data_manager.py:1634` | `_save_positions_json` | C? | liefert Default | `return False` | |
| 154 | `data_manager.py:1651` | `load_positions_document` | A? | nur Log | `if _should_refuse_demo_json_fallback(target, cfg) or multi_tenant_enabled(): log( f"Mongo ` | |
| 155 | `data_manager.py:1678` | `save_positions_document` | A? | nur Log | `log(f"Mongo positions save failed ({target}): {e}", "ERROR") ok = False` | |
| 156 | `data_manager.py:1696` | `load_strategy_backtest_results` | A? | nur Log | `log(f"Failed to load {path}: {e}", "WARNING") return {"coins": {}}` | |
| 157 | `data_manager.py:1707` | `save_strategy_backtest_results` | C? | liefert Default | `return False` | |
| 158 | `data_manager.py:1754` | `load_cmc_posts` | A? | nur Log | `log(f"Failed to load {path}: {e}", "WARNING") return {"posts": []}` | |
| 159 | `data_manager.py:1764` | `save_cmc_posts` | C? | liefert Default | `return False` | |
| 160 | `data_manager.py:1792` | `log_cmc_post` | C? | schluckt still | `pass` | |
| 161 | `data_manager.py:1813` | `load_lc_signals` | A? | nur Log | `log(f"Failed to load {path}: {e}", "WARNING") return {"signals": []}` | |
| 162 | `data_manager.py:1826` | `save_lc_signals` | C? | liefert Default | `return False` | |
| 163 | `data_manager.py:1854` | `log_lc_signal` | C? | schluckt still | `pass` | |
| 164 | `data_manager.py:1866` | `load_paper_strategies` | A? | nur Log | `log(f"Failed to load {path}: {e}", "WARNING") return {"hypotheses": []}` | |
| 165 | `data_manager.py:1876` | `save_paper_strategies` | C? | liefert Default | `return False` | |
| 166 | `data_manager.py:1887` | `load_paper_sandbox_history` | A? | nur Log | `log(f"Failed to load {path}: {e}", "WARNING") return {"portfolios": {}}` | |
| 167 | `data_manager.py:1897` | `save_paper_sandbox_history` | C? | liefert Default | `return False` | |
| 168 | `data_manager.py:1910` | `load_translations` | A? | nur Log | `log(f"Failed to load translation files: {e}", "WARNING") TRANSLATIONS = {"en": {}, "de": {` | |
| 169 | `data_manager.py:1920` | `get_system_lang` | A? | nur Log | `log(f"Failed to detect system language: {e}", "WARNING") return "en"` | |
| 170 | `storage/grid_plan_store.py:67` | `load_grid_plans_document` | A? | nur Log | `log(f"grid_plan_store load failed ({tid}/{sc}): {e}", "DEBUG") return empty` | |
| 171 | `storage/grid_plan_store.py:96` | `save_grid_plans_document` | C? | liefert Default | `log(f"grid_plan_store save failed ({tid}/{sc}): {e}", "WARNING") return False` | |
| 172 | `storage/grid_plan_store.py:122` | `load_grid_plan` | C? | schluckt still | `pass` | |
| 173 | `storage/grid_plan_store.py:156` | `save_grid_plan` | A? | nur Log | `log(f"grid_plan_store legacy config mirror skipped: {e}", "DEBUG")` | |
| 174 | `storage/ledger_router.py:101` | `_atomic_write` | C? | liefert Default | `return False` | |
| 175 | `storage/ledger_router.py:137` | `load_orders` | A? | nur Log | `log(f"Failed to load {path}: {e}", "WARNING") return {"ledger_scope": scope, "orders": [],` | |
| 176 | `storage/ledger_router.py:156` | `load_positions` | A? | nur Log | `log(f"Failed to load {path}: {e}", "WARNING") return {"ledger_scope": scope, "positions": ` | |
| 177 | `storage/ledger_router.py:182` | `load_trade_history` | A? | nur Log | `log(f"Failed to load {path}: {e}", "WARNING") return self._empty_trade_history(scope)` | |
| 178 | `storage/ledger_router.py:229` | `load_orders` | A? | nur Log | `log(f"Mongo orders load failed ({scope}), falling back to JSON: {e}", "WARNING") return se` | |
| 179 | `storage/ledger_router.py:237` | `save_orders` | A? | nur Log | `log(f"Mongo orders save failed ({scope}): {e}", "ERROR") ok = False` | |
| 180 | `storage/ledger_router.py:245` | `load_positions` | A? | nur Log | `log(f"Mongo positions load failed ({scope}), falling back to JSON: {e}", "WARNING") return` | |
| 181 | `storage/ledger_router.py:253` | `save_positions` | A? | nur Log | `log(f"Mongo positions save failed ({scope}): {e}", "ERROR") ok = False` | |
| 182 | `storage/ledger_router.py:261` | `load_trade_history` | A? | nur Log | `log(f"Mongo trade_history load failed ({scope}), falling back to JSON: {e}", "WARNING") re` | |
| 183 | `storage/ledger_router.py:269` | `save_trade_history` | A? | nur Log | `log(f"Mongo trade_history save failed ({scope}): {e}", "ERROR") ok = False` | |
| 184 | `storage/mongo_client.py:206` | `get_client` | C? | schluckt still | `pass` | |
| 185 | `storage/mongo_client.py:240` | `get_database` | ? |  | `last_err = e if attempt == 0: # Retry once. Only drop the singleton when it is already clo` | |
| 186 | `storage/mongo_client.py:291` | `close_client` | C? | schluckt still | `pass` | |
| 187 | `storage/order_ledger_v2.py:91` | `get_order_ledger_v2` | C? | schluckt still | `pass` | |
| 188 | `storage/order_ledger_v2.py:110` | `get_order_ledger_v2` | ? |  | `_STORE = MemoryOrderLedgerV2()` | |
| 189 | `storage/order_ledger_v2.py:138` | `_parse_to_display_naive` | C? | liefert Default | `return None` | |
| 190 | `storage/order_ledger_v2.py:144` | `_parse_to_display_naive` | ? |  | `target = None` | |
| 191 | `storage/order_ledger_v2.py:158` | `_parse_to_display_naive` | C? | liefert Default | `return dt.replace(tzinfo=None)` | |
| 192 | `storage/order_ledger_v2.py:183` | `display_day_key_for_order` | C? | liefert Default | `return datetime.now().strftime("%Y-%m-%d")` | |
| 193 | `storage/order_ledger_v2.py:196` | `display_day_key_now` | C? | liefert Default | `return datetime.now().strftime("%Y-%m-%d")` | |
| 194 | `storage/order_ledger_v2.py:269` | `_token` | C? | liefert Default | `return ""` | |
| 195 | `storage/tenant_meta_store.py:37` | `load_tenant_config_body` | A? | nur Log | `log(f"tenant_meta_store: failed load_tenant_config_body for {tid}: {e}", "WARNING")` | |
| 196 | `storage/tenant_meta_store.py:61` | `save_tenant_config` | A? | nur Log | `log(f"tenant_meta_store: failed save_tenant_config for {tid}: {e}", "WARNING") return Fals` | |
| 197 | `storage/tenant_meta_store.py:83` | `load_tenant_watchlist` | A? | nur Log | `log(f"tenant_meta_store: failed load_tenant_watchlist for {tid}: {e}", "WARNING")` | |
| 198 | `storage/tenant_meta_store.py:99` | `save_tenant_watchlist` | A? | nur Log | `log(f"tenant_meta_store: failed save_tenant_watchlist for {tid}: {e}", "WARNING") return F` | |
| 199 | `storage/tenant_registry.py:202` | `link_tenant_owner_chat` | C? | liefert Default | `log(f"tenant_registry: link_owner failed for {tid}: {e}", "WARNING") return False, "Verknü` | |
| 200 | `storage/tenant_registry.py:220` | `find_tenant_by_owner_chat_id` | C? | liefert Default | `log(f"tenant_registry: find by chat_id failed: {e}", "WARNING") return None` | |
| 201 | `services/ledger_sync.py:36` | `migrate_legacy_positions` | A? | nur Log | `log(f"Legacy positions migration failed: {e}", "WARNING")` | |
| 202 | `services/ledger_sync.py:428` | `sync_positions_on_startup` | A? | nur Log | `log(f"recent_high reconcile skipped for scope={scope}: {exc}", "WARNING")` | |
| 203 | `services/trading_service.py:276` | `_execute_order_locked` | A? | nur Log | `log(f"Positions snapshot failed: {e}", "WARNING")` | |
| 204 | `services/trading_service.py:283` | `_execute_order_locked` | A? | nur Log | `log(f"auto-short after sell skip: {e}", "DEBUG")` | |

## Verteilung pro Datei

- `data_manager.py`: 64
- `risk/risk_manager.py`: 55
- `risk/slot_eviction_runtime.py`: 16
- `services/order_service.py`: 13
- `storage/ledger_router.py`: 10
- `strategies/positions.py`: 8
- `storage/order_ledger_v2.py`: 8
- `execution/gate_adapter.py`: 4
- `storage/grid_plan_store.py`: 4
- `storage/tenant_meta_store.py`: 4
- `services/portfolio_service.py`: 3
- `storage/mongo_client.py`: 3
- `risk/moderate_deploy.py`: 2
- `risk/slot_eviction_rag.py`: 2
- `storage/tenant_registry.py`: 2
- `services/ledger_sync.py`: 2
- `services/trading_service.py`: 2
- `risk/position_capacity.py`: 1
- `risk/slot_eviction.py`: 1