# Documentation audit 2026-11-05 — settings/features/options/Telegram
Source: staging tarball SHA tip via codeload (branch staging). Local path `/tmp/xagent-docs-audit/xagent-trading-bot-staging`.
Docs files scanned: 92
Config files: 1
Telegram commands found: 115
Coverage rows: 1365

## Summary
- Items checked: **1365**
- Undocumented: **370**
- Incorrect (correct=n): **520**
- Possibly stale defaults (correct=maybe): **16**

## Pass 1 — doc → code
Documented slash-commands claimed in docs: 633
Issues (doc claims missing in code scan): 520
- doc claims /a8 but not found in telegram handlers/locales
- doc claims /actions but not found in telegram handlers/locales
- doc claims /adapt but not found in telegram handlers/locales
- doc claims /adds but not found in telegram handlers/locales
- doc claims /adjust but not found in telegram handlers/locales
- doc claims /after but not found in telegram handlers/locales
- doc claims /agent but not found in telegram handlers/locales
- doc claims /agents but not found in telegram handlers/locales
- doc claims /ai_critic but not found in telegram handlers/locales
- doc claims /amplifiers but not found in telegram handlers/locales
- doc claims /analysis but not found in telegram handlers/locales
- doc claims /app but not found in telegram handlers/locales
- doc claims /appetite but not found in telegram handlers/locales
- doc claims /architecture_runtime but not found in telegram handlers/locales
- doc claims /arena but not found in telegram handlers/locales
- doc claims /aria_bot but not found in telegram handlers/locales
- doc claims /aria_log but not found in telegram handlers/locales
- doc claims /armed_state but not found in telegram handlers/locales
- doc claims /audit but not found in telegram handlers/locales
- doc claims /audit_trail but not found in telegram handlers/locales
- doc claims /b1_open_dca_lots but not found in telegram handlers/locales
- doc claims /b92b3f7b but not found in telegram handlers/locales
- doc claims /background_runtime but not found in telegram handlers/locales
- doc claims /backlog_naechster_lauf but not found in telegram handlers/locales
- doc claims /backtest_breakout_15m but not found in telegram handlers/locales
- doc claims /backtester but not found in telegram handlers/locales
- doc claims /backtests but not found in telegram handlers/locales
- doc claims /base but not found in telegram handlers/locales
- doc claims /baseline but not found in telegram handlers/locales
- doc claims /bearish but not found in telegram handlers/locales
- doc claims /befehl but not found in telegram handlers/locales
- doc claims /benchmark but not found in telegram handlers/locales
- doc claims /block but not found in telegram handlers/locales
- doc claims /body but not found in telegram handlers/locales
- doc claims /bot_http but not found in telegram handlers/locales
- doc claims /breakout_15m but not found in telegram handlers/locales
- doc claims /broker but not found in telegram handlers/locales
- doc claims /bt but not found in telegram handlers/locales
- doc claims /build_info but not found in telegram handlers/locales
- doc claims /cache but not found in telegram handlers/locales

## Pass 2 — code → doc (undocumented)
Undocumented items: 370
- code_flag: 16
- config: 331
- telegram: 23

Top undocumented telegram commands:
- `telegram:/churn_replay` @ locales/telegram_messages.json, notifications/telegram_commands/replay_commands.py
- `telegram:/cmcsignals` @ notifications/telegram_commands/cmc_commands.py
- `telegram:/dexsignals` @ notifications/telegram_commands/cmc_commands.py
- `telegram:/gate_status` @ notifications/telegram_commands/gate_commands.py
- `telegram:/gatestatus` @ notifications/telegram_commands/gate_commands.py
- `telegram:/lcscore` @ notifications/telegram_commands/lc_commands.py
- `telegram:/lcsignals` @ notifications/telegram_commands/lc_commands.py
- `telegram:/maxpos` @ notifications/telegram_commands/mode_commands.py
- `telegram:/maxpositions` @ locales/telegram_menu.json, locales/telegram_messages.json, locales/telegram_section_help.json
- `telegram:/myid` @ notifications/telegram_commands/mode_commands.py
- `telegram:/orders_blocked` @ locales/telegram_menu.json, notifications/telegram_commands/order_commands.py
- `telegram:/orders_month` @ locales/telegram_menu.json, notifications/telegram_commands/order_commands.py
- `telegram:/plan_vs` @ notifications/telegram_commands/plan_commands.py
- `telegram:/positions_full` @ notifications/telegram_commands/portfolio_commands.py
- `telegram:/session_cancel` @ notifications/telegram_commands/replay_commands.py
- `telegram:/tradingmode` @ notifications/telegram_commands/mode_commands.py
- `telegram:/wqescores` @ notifications/telegram_commands/watchlist_commands.py
- `telegram:/xaccounts` @ notifications/telegram_commands/x_commands.py
- `telegram:/xhistory` @ notifications/telegram_commands/x_commands.py
- `telegram:/xleaderboard` @ notifications/telegram_commands/x_commands.py
- `telegram:/xlist` @ notifications/telegram_commands/x_commands.py
- `telegram:/xlog` @ notifications/telegram_commands/x_commands.py
- `telegram:/zielplan` @ notifications/telegram_commands/plan_commands.py

Top undocumented config keys:
- `config:architecture._fusion_oracle_risk_on_keep_size_doc` files=config.json missing from docs
- `config:architecture.background_social_interval_sec` files=config.json missing from docs
- `config:architecture.block_rebuy_if_last_sell_was_stop` files=config.json missing from docs
- `config:architecture.coin_query_webhook_enabled` files=config.json missing from docs
- `config:architecture.dedup_ttl_sec` files=config.json missing from docs
- `config:architecture.eval_batch_size` files=config.json missing from docs
- `config:architecture.eval_debounce_sec` files=config.json missing from docs
- `config:architecture.eval_meta_interval_sec` files=config.json missing from docs
- `config:architecture.eval_stale_sec` files=config.json missing from docs
- `config:architecture.eval_worker_poll_sec` files=config.json missing from docs
- `config:architecture.funding_cache_ttl_sec` files=config.json missing from docs
- `config:architecture.fusion_oracle_risk_on_keep_size` files=config.json missing from docs
- `config:architecture.fusion_oracle_risk_on_keep_size_tenants` files=config.json missing from docs
- `config:architecture.gate_single_ticker_timeout_s` files=config.json missing from docs
- `config:architecture.gate_ticker_snapshot_ttl_sec` files=config.json missing from docs
- `config:architecture.heartbeat_ttl_sec` files=config.json missing from docs
- `config:architecture.heartbeat_warn_enabled` files=config.json missing from docs
- `config:architecture.key_prefix` files=config.json missing from docs
- `config:architecture.ledger_lock_wait_sec` files=config.json missing from docs
- `config:architecture.market_oracle_apply_sensor_policy` files=config.json missing from docs
- `config:architecture.market_oracle_apply_size_mult` files=config.json missing from docs
- `config:architecture.market_oracle_block_buys_on_crash` files=config.json missing from docs
- `config:architecture.market_oracle_ingest_allow_no_token` files=config.json missing from docs
- `config:architecture.market_oracle_ingest_enabled` files=config.json missing from docs
- `config:architecture.market_oracle_ingest_token` files=config.json missing from docs
- `config:architecture.market_oracle_risk_enabled` files=config.json missing from docs
- `config:architecture.market_oracle_warmup_max_new_buys` files=config.json missing from docs
- `config:architecture.market_oracle_warmup_sec` files=config.json missing from docs
- `config:architecture.notification_rate_limit_sec` files=config.json missing from docs
- `config:architecture.ohlcv_cache_enabled` files=config.json missing from docs
- `config:architecture.ohlcv_cache_ttl_sec` files=config.json missing from docs
- `config:architecture.ohlcv_serve_from_larger` files=config.json missing from docs
- `config:architecture.price_cache_enabled` files=config.json missing from docs
- `config:architecture.price_cache_ttl_sec` files=config.json missing from docs
- `config:architecture.rebuy_after_stop_loss_hours` files=config.json missing from docs
- `config:architecture.redis_url` files=config.json missing from docs
- `config:architecture.santiment_apply_grid_spacing` files=config.json missing from docs
- `config:architecture.santiment_apply_mode_bias` files=config.json missing from docs
- `config:architecture.santiment_apply_sensor_policy` files=config.json missing from docs
- `config:architecture.santiment_apply_size_mult` files=config.json missing from docs
- `config:architecture.santiment_block_buys_on_crash` files=config.json missing from docs
- `config:architecture.santiment_ingest_allow_no_token` files=config.json missing from docs
- `config:architecture.santiment_ingest_enabled` files=config.json missing from docs
- `config:architecture.santiment_ingest_token` files=config.json missing from docs
- `config:architecture.santiment_inject_regime_sentiment` files=config.json missing from docs
- `config:architecture.santiment_risk_enabled` files=config.json missing from docs
- `config:architecture.signal_event_ttl_sec` files=config.json missing from docs
- `config:architecture.signal_webhook_allow_no_token` files=config.json missing from docs
- `config:architecture.signal_webhook_enabled` files=config.json missing from docs
- `config:architecture.signal_webhook_rate_limit_per_min` files=config.json missing from docs

## Pass 3 — spot-check 15 random items
| item | documented | correct | code_ok | note |
|---|---|---|---|---|
| `config:grid.sell_policy.underwater_max_slice` | n | n/a | True | values=['0.12'] |
| `config:volatile_altcoin.vol_exhaustion_rsi_min` | n | n/a | True | values=['58'] |
| `telegram:/wqescores` | n | n/a | True | command found in code/locales, not in docs |
| `config:hermes.live_evidence.enabled` | y | y | True | values=['True'] |
| `config:volatile_altcoin.exit_ladder.tiers` | y | y | True | values=[] |
| `config:gainer_signal` | y | y | True | values=[] |
| `config:risk.position_capacity.cash_mode_adj` | n | n/a | True | values=[] |
| `config:volatile_altcoin.profit_max_lifetime.mode` | y | y | True | values=["'live'"] |
| `config:volatile_altcoin.tier_scoring.range_24h_min_pct` | n | n/a | True | values=['12.0'] |
| `config:hermes.promotion.veto_window_min` | n | n/a | True | values=['10'] |
| `config:risk.position_capacity.memory_toxic_per_n` | n | n/a | True | values=['3'] |
| `config:observability.position_snapshots_every_n_cycles` | n | n/a | True | values=['6'] |
| `config:hermes.rotation` | y | y | True | values=["'signal_activity'"] |
| `config:volatile_altcoin.trailing_take_profit.max_steps` | n | n/a | True | values=['1'] |
| `telegram:/orders_month` | n | n/a | True | command found in code/locales, not in docs |

Spot-check failures/concerns: 0/15

## Full coverage table (abbreviated)
| item | documented | correct | code location | note |
|---|---|---|---|---|
| `config:accuracy` | y | y | config.json | defaults=[] |
| `config:aggression` | y | y | config.json | defaults=[] |
| `config:altcoin_social` | y | y | config.json | defaults=[] |
| `config:architecture` | y | y | config.json | defaults=[] |
| `config:architecture._fusion_oracle_risk_on_keep_size_doc` | n | n/a | config.json | missing from docs |
| `config:architecture.background_backtest_enabled` | y | y | config.json | defaults=['True'] |
| `config:architecture.background_social_enabled` | y | y | config.json | defaults=['True'] |
| `config:architecture.background_social_interval_sec` | n | n/a | config.json | missing from docs |
| `config:architecture.block_rebuy_if_last_sell_was_stop` | n | n/a | config.json | missing from docs |
| `config:architecture.coin_query_webhook_enabled` | n | n/a | config.json | missing from docs |
| `config:architecture.dedup_ttl_sec` | n | n/a | config.json | missing from docs |
| `config:architecture.eval_batch_size` | n | n/a | config.json | missing from docs |
| `config:architecture.eval_debounce_sec` | n | n/a | config.json | missing from docs |
| `config:architecture.eval_meta_interval_sec` | n | n/a | config.json | missing from docs |
| `config:architecture.eval_position_heartbeat_sec` | y | y | config.json | defaults=['300'] |
| `config:architecture.eval_queue_enabled` | y | y | config.json | defaults=['True'] |
| `config:architecture.eval_queue_max_len` | y | y | config.json | defaults=['500'] |
| `config:architecture.eval_stale_sec` | n | n/a | config.json | missing from docs |
| `config:architecture.eval_worker_poll_sec` | n | n/a | config.json | missing from docs |
| `config:architecture.funding_cache_ttl_sec` | n | n/a | config.json | missing from docs |
| `config:architecture.fusion_oracle_risk_on_keep_size` | n | n/a | config.json | missing from docs |
| `config:architecture.fusion_oracle_risk_on_keep_size_tenants` | n | n/a | config.json | missing from docs |
| `config:architecture.gate_single_ticker_timeout_s` | n | n/a | config.json | missing from docs |
| `config:architecture.gate_ticker_snapshot_ttl_sec` | n | n/a | config.json | missing from docs |
| `config:architecture.heartbeat_ttl_sec` | n | n/a | config.json | missing from docs |
| `config:architecture.heartbeat_warn_enabled` | n | n/a | config.json | missing from docs |
| `config:architecture.hermes_external` | y | y | config.json | defaults=['True'] |
| `config:architecture.key_prefix` | n | n/a | config.json | missing from docs |
| `config:architecture.lease_renew_sec` | y | y | config.json | defaults=['10'] |
| `config:architecture.lease_ttl_sec` | y | y | config.json | defaults=['30'] |
| `config:architecture.ledger_backend` | y | y | config.json | defaults=["'mongo'"] |
| `config:architecture.ledger_dual_write` | y | y | config.json | defaults=['False'] |
| `config:architecture.ledger_lock_enabled` | y | y | config.json | defaults=['True'] |
| `config:architecture.ledger_lock_fail_closed` | y | y | config.json | defaults=['True'] |
| `config:architecture.ledger_lock_ttl_sec` | y | y | config.json | defaults=['60'] |
| `config:architecture.ledger_lock_wait_sec` | n | n/a | config.json | missing from docs |
| `config:architecture.market_oracle_apply_sensor_policy` | n | n/a | config.json | missing from docs |
| `config:architecture.market_oracle_apply_size_mult` | n | n/a | config.json | missing from docs |
| `config:architecture.market_oracle_block_buys_on_crash` | n | n/a | config.json | missing from docs |
| `config:architecture.market_oracle_ingest_allow_no_token` | n | n/a | config.json | missing from docs |
| `config:architecture.market_oracle_ingest_enabled` | n | n/a | config.json | missing from docs |
| `config:architecture.market_oracle_ingest_token` | n | n/a | config.json | missing from docs |
| `config:architecture.market_oracle_risk_enabled` | n | n/a | config.json | missing from docs |
| `config:architecture.market_oracle_risk_fail_open` | y | y | config.json | defaults=['True'] |
| `config:architecture.market_oracle_warmup_max_new_buys` | n | n/a | config.json | missing from docs |
| `config:architecture.market_oracle_warmup_sec` | n | n/a | config.json | missing from docs |
| `config:architecture.min_hours_after_sell_before_rebuy` | y | y | config.json | defaults=['4.0'] |
| `config:architecture.mode` | y | y | config.json | defaults=["'monolith'"] |
| `config:architecture.mongodb` | y | y | config.json | defaults=[] |
| `config:architecture.mongodb.db_name` | y | y | config.json | defaults=["'xagent'"] |
| `config:architecture.mongodb.uri` | y | maybe | config.json | doc mentions key; values in configs=["'mongodb://127.0.0.1:27017'"] |
| `config:architecture.notification_mode` | y | y | config.json | defaults=["'async'"] |
| `config:architecture.notification_rate_limit_sec` | n | n/a | config.json | missing from docs |
| `config:architecture.ohlcv_cache_enabled` | n | n/a | config.json | missing from docs |
| `config:architecture.ohlcv_cache_ttl_sec` | n | n/a | config.json | missing from docs |
| `config:architecture.ohlcv_cache_ttl_sec.15m` | y | y | config.json | defaults=['60'] |
| `config:architecture.ohlcv_cache_ttl_sec.1h` | y | y | config.json | defaults=['90'] |
| `config:architecture.ohlcv_cache_ttl_sec.4h` | y | y | config.json | defaults=['120'] |
| `config:architecture.ohlcv_serve_from_larger` | n | n/a | config.json | missing from docs |
| `config:architecture.price_cache_enabled` | n | n/a | config.json | missing from docs |
| `config:architecture.price_cache_ttl_sec` | n | n/a | config.json | missing from docs |
| `config:architecture.rebuy_after_stop_loss_hours` | n | n/a | config.json | missing from docs |
| `config:architecture.redis_url` | n | n/a | config.json | missing from docs |
| `config:architecture.santiment_apply_grid_spacing` | n | n/a | config.json | missing from docs |
| `config:architecture.santiment_apply_mode_bias` | n | n/a | config.json | missing from docs |
| `config:architecture.santiment_apply_sensor_policy` | n | n/a | config.json | missing from docs |
| `config:architecture.santiment_apply_size_mult` | n | n/a | config.json | missing from docs |
| `config:architecture.santiment_block_buys_on_crash` | n | n/a | config.json | missing from docs |
| `config:architecture.santiment_ingest_allow_no_token` | n | n/a | config.json | missing from docs |
| `config:architecture.santiment_ingest_enabled` | n | n/a | config.json | missing from docs |
| `config:architecture.santiment_ingest_token` | n | n/a | config.json | missing from docs |
| `config:architecture.santiment_inject_regime_sentiment` | n | n/a | config.json | missing from docs |
| `config:architecture.santiment_risk_enabled` | n | n/a | config.json | missing from docs |
| `config:architecture.santiment_risk_fail_open` | y | y | config.json | defaults=['True'] |
| `config:architecture.signal_event_ttl_sec` | n | n/a | config.json | missing from docs |
| `config:architecture.signal_webhook_allow_no_token` | n | n/a | config.json | missing from docs |
| `config:architecture.signal_webhook_enabled` | n | n/a | config.json | missing from docs |
| `config:architecture.signal_webhook_rate_limit_per_min` | n | n/a | config.json | missing from docs |
| `config:architecture.signal_webhook_token` | n | n/a | config.json | missing from docs |
| `config:architecture.single_writer_lease_enabled` | y | y | config.json | defaults=['True'] |
| `config:architecture.social_snapshot_max_age_sec` | n | n/a | config.json | missing from docs |
| `config:architecture.stale_price_max_age_sec` | n | n/a | config.json | missing from docs |
| `config:architecture.trade_intent_async_auto_only` | n | n/a | config.json | missing from docs |
| `config:architecture.trade_intent_queue_enabled` | n | n/a | config.json | missing from docs |
| `config:architecture.trading_engine_mode` | y | y | config.json | defaults=["'in_process'"] |
| `config:architecture.use_signal_snapshot` | n | n/a | config.json | missing from docs |
| `config:cmc` | y | y | config.json | defaults=[] |
| `config:costs` | y | y | config.json | defaults=[] |
| `config:dca_sniper` | y | y | config.json | defaults=[] |
| `config:debug` | y | y | config.json | defaults=['False'] |
| `config:demo` | y | y | config.json | defaults=[] |
| `config:desk` | y | y | config.json | defaults=[] |
| `config:dry_run_defaults` | y | y | config.json | defaults=[] |
| `config:entry_guard` | y | y | config.json | defaults=[] |
| `config:entry_sensor_15m` | y | y | config.json | defaults=[] |
| `config:exit_realtime` | y | y | config.json | defaults=[] |
| `config:exit_rotation` | n | n/a | config.json | missing from docs |
| `config:exit_sensor` | n | n/a | config.json | missing from docs |
| `config:gainer_entry` | y | y | config.json | defaults=[] |
| `config:gainer_relvol_shadow` | n | n/a | config.json | missing from docs |
| `config:gainer_signal` | y | y | config.json | defaults=[] |
| `config:gainer_universe` | y | y | config.json | defaults=[] |
| `config:grid` | y | y | config.json | defaults=[] |
| `config:grid.bar_lookback` | n | n/a | config.json | missing from docs |
| `config:grid.default_spacing_atr_mult` | n | n/a | config.json | missing from docs |
| `config:grid.enabled` | y | y | config.json | defaults=['True'] |
| `config:grid.fee_aware` | y | y | config.json | defaults=['True'] |
| `config:grid.hybrid_tiers` | n | n/a | config.json | missing from docs |
| `config:grid.level_timeframe` | n | n/a | config.json | missing from docs |
| `config:grid.max_levels` | n | n/a | config.json | missing from docs |
| `config:grid.meme_spacing_atr_mult` | n | n/a | config.json | missing from docs |
| `config:grid.no_pure_grid_classes` | n | n/a | config.json | missing from docs |
| `config:grid.pure_grid_classes` | n | n/a | config.json | missing from docs |
| `config:grid.pure_grid_tiers` | n | n/a | config.json | missing from docs |
| `config:grid.re_center_atr_mult` | n | n/a | config.json | missing from docs |
| `config:grid.sell_policy` | y | y | config.json | defaults=[] |
| `config:grid.sell_policy._min_sell_gain_pct_doc` | n | n/a | config.json | missing from docs |
| `config:grid.sell_policy.allow_defensive_underwater` | n | n/a | config.json | missing from docs |
| `config:grid.sell_policy.block_recenter_below_entry` | n | n/a | config.json | missing from docs |
| `config:grid.sell_policy.enabled` | y | y | config.json | defaults=['True'] |
| `config:grid.sell_policy.green_buffer_pct` | n | n/a | config.json | missing from docs |
| `config:grid.sell_policy.green_only_modes` | n | n/a | config.json | missing from docs |
| `config:grid.sell_policy.memory_enabled` | y | y | config.json | defaults=['True'] |
| `config:grid.sell_policy.memory_flow_only_min_gain_pct` | n | n/a | config.json | missing from docs |
| `config:grid.sell_policy.memory_hard_neg_min_gain_pct` | n | n/a | config.json | missing from docs |
| `config:grid.sell_policy.memory_runner_hold_below_gain_pct` | n | n/a | config.json | missing from docs |
| `config:grid.sell_policy.memory_structure_min_gain_pct` | n | n/a | config.json | missing from docs |
| `config:grid.sell_policy.min_sell_gain_pct` | n | n/a | config.json | missing from docs |
| `config:grid.sell_policy.re_center_max_drawdown_pct` | n | n/a | config.json | missing from docs |
| `config:grid.sell_policy.require_sell_above_center` | n | n/a | config.json | missing from docs |
| `config:grid.sell_policy.soft_underwater_modes` | n | n/a | config.json | missing from docs |
| `config:grid.sell_policy.underwater_max_slice` | n | n/a | config.json | missing from docs |
| `config:grid.stable_spacing_atr_mult` | n | n/a | config.json | missing from docs |
| `config:grid.use_bar_range_hits` | n | n/a | config.json | missing from docs |
| `config:grid.use_limit_orders` | n | n/a | config.json | missing from docs |
| `config:grid.volatile_re_center_atr_mult` | n | n/a | config.json | missing from docs |
| `config:grid.volatile_spacing_atr_mult` | n | n/a | config.json | missing from docs |
| `config:grid_states` | y | y | config.json | defaults=[] |
| `config:hermes` | y | y | config.json | defaults=[] |
| `config:hermes.backtest_days` | y | y | config.json | defaults=['45'] |
| `config:hermes.backtest_mode` | y | y | config.json | defaults=["'pipeline'"] |
| `config:hermes.cmc_replay_ttl_hours` | n | n/a | config.json | missing from docs |
| `config:hermes.cycle_interval_sec` | y | y | config.json | defaults=['1800'] |
| `config:hermes.enabled` | y | y | config.json | defaults=['False'] |
| `config:hermes.failure_criteria` | y | y | config.json | defaults=[] |
| `config:hermes.failure_criteria.drawdown_delta_max` | y | y | config.json | defaults=['5'] |
| `config:hermes.failure_criteria.sharpe_delta_max` | y | y | config.json | defaults=['-0.2'] |
| `config:hermes.health` | y | y | config.json | defaults=[] |
| `config:hermes.health.inconclusive_alert_pct` | y | y | config.json | defaults=['50'] |
| `config:hermes.health.inconclusive_window` | y | y | config.json | defaults=['20'] |
| `config:hermes.initial_capital_usdt` | y | maybe | config.json | doc mentions key; values in configs=['20000'] |
| `config:hermes.live_evidence` | y | y | config.json | defaults=[] |
| `config:hermes.live_evidence.counterfactual_enabled` | y | y | config.json | defaults=['True'] |
| `config:hermes.live_evidence.dual_exit_params_only` | n | n/a | config.json | missing from docs |
| `config:hermes.live_evidence.enabled` | y | y | config.json | defaults=['True'] |
| `config:hermes.live_evidence.include_manual_trades` | y | y | config.json | defaults=['True'] |
| `config:hermes.live_evidence.live_blocklist` | y | y | config.json | defaults=[] |
| `config:hermes.live_evidence.live_max_loss_usdt` | y | y | config.json | defaults=['200'] |
| `config:hermes.live_evidence.lookback_days` | y | y | config.json | defaults=['7'] |
| `config:hermes.live_evidence.min_counterfactual_sells` | y | y | config.json | defaults=['1'] |
| `config:hermes.live_evidence.min_live_pnl_delta_usdt` | y | y | config.json | defaults=['100'] |
| `config:hermes.live_evidence.min_live_sell_trades` | y | y | config.json | defaults=['2'] |
| `config:hermes.live_evidence.min_live_trades` | y | y | config.json | defaults=['3'] |
| `config:hermes.live_evidence.mode` | y | y | config.json | defaults=["'dual'"] |
| `config:hermes.live_evidence.notify_on_live_veto` | y | y | config.json | defaults=['True'] |
| `config:hermes.live_evidence.require_cf_seeded` | n | n/a | config.json | missing from docs |
| `config:hermes.notify_on_promotion` | y | y | config.json | defaults=['True'] |
| `config:hermes.primary_metric` | n | n/a | config.json | missing from docs |
| `config:hermes.promotion` | y | y | config.json | defaults=[] |
| `config:hermes.promotion.max_promotions_per_day` | n | n/a | config.json | missing from docs |
| `config:hermes.promotion.post_apply_min_trades` | n | n/a | config.json | missing from docs |
| `config:hermes.promotion.post_apply_validation_hours` | n | n/a | config.json | missing from docs |
| `config:hermes.promotion.post_apply_win_rate_gap_pp` | n | n/a | config.json | missing from docs |
| `config:hermes.promotion.veto_window_min` | n | n/a | config.json | missing from docs |
| `config:hermes.rotation` | y | y | config.json | defaults=["'signal_activity'"] |
| `config:hermes.rotation_hours` | n | n/a | config.json | missing from docs |
| `config:hermes.skills` | y | y | config.json | defaults=[] |
| `config:hermes.skills.confidence_ema_alpha` | n | n/a | config.json | missing from docs |
| `config:hermes.skills.max_per_variable` | y | y | config.json | defaults=['5'] |
| `config:hermes.skills.min_confidence` | y | y | config.json | defaults=['0.25'] |
| `config:hermes.success_criteria` | y | y | config.json | defaults=[] |
| `config:hermes.success_criteria.max_drawdown_pct` | y | y | config.json | defaults=['15'] |
| `config:hermes.success_criteria.min_opportunity_score` | y | y | config.json | defaults=['0.2'] |
| `config:hermes.success_criteria.min_sharpe` | y | y | config.json | defaults=['0.5'] |
| `config:hermes.success_criteria.min_trades` | y | y | config.json | defaults=['3'] |
| `config:hermes.success_criteria.min_win_rate` | y | y | config.json | defaults=['45'] |
| `config:hermes.symbols` | y | y | config.json | defaults=[] |
| `config:hermes.symbols_cmc_top_n` | n | n/a | config.json | missing from docs |
| `config:hermes.symbols_include_positions` | n | n/a | config.json | missing from docs |
| `config:hermes.symbols_max` | y | y | config.json | defaults=['8'] |
| `config:hermes.symbols_mode` | y | y | config.json | defaults=["'hybrid'"] |
| `config:hermes.symbols_pin` | y | y | config.json | defaults=[] |
| `config:hermes.symbols_refresh_hours` | n | n/a | config.json | missing from docs |
| `config:hermes.sync_to_config` | y | y | config.json | defaults=['True'] |
| `config:hermes.timeframes` | y | y | config.json | defaults=[] |
| `config:hermes.tunable_params` | y | y | config.json | defaults=[] |
| `config:hermes.usdt_per_trade` | y | y | config.json | defaults=['1000'] |
| `config:hermes.validation` | y | y | config.json | defaults=[] |
| `config:hermes.validation.backtest_days` | y | y | config.json | defaults=['45'] |
| `config:hermes.validation.fold_days` | y | y | config.json | defaults=['5'] |
| `config:hermes.validation.holdout_dd_tolerance_pct` | y | y | config.json | defaults=['2.0'] |
| `config:hermes.validation.holdout_folds` | y | y | config.json | defaults=['2'] |
| `config:hermes.validation.min_bars_per_fold` | y | y | config.json | defaults=['30'] |
| `config:hermes.validation.min_folds_won_ratio` | y | y | config.json | defaults=['0.55'] |
| `config:hermes.validation.min_opportunity_delta` | n | n/a | config.json | missing from docs |
| `config:hermes.validation.min_total_trades` | y | y | config.json | defaults=['30'] |
| `config:hermes.validation.min_trades_aggregate` | y | y | config.json | defaults=['3'] |
| `config:hermes.validation.min_trades_per_fold` | y | y | config.json | defaults=['1'] |
| `config:hermes.validation.min_win_probability` | y | y | config.json | defaults=['0.95'] |
| `config:hermes.validation.mode` | y | y | config.json | defaults=["'walk_forward'"] |
| `config:hermes.validation.step_days` | y | y | config.json | defaults=['3'] |
| `config:initial_capital_usdt` | y | y | config.json | defaults=['100000'] |
| `config:lc_weight` | n | n/a | config.json | missing from docs |
| `config:live` | y | y | config.json | defaults=[] |
| `config:live.api_key_env` | n | n/a | config.json | missing from docs |
| `config:live.api_secret_env` | n | n/a | config.json | missing from docs |
| `config:live.dry_run` | y | y | config.json | defaults=['True'] |
| `config:live.dry_run_enhanced` | y | y | config.json | defaults=['True'] |
| `config:live.exchange` | y | y | config.json | defaults=["'gate'"] |
| `config:live.execution` | y | y | config.json | defaults=["'shadow'"] |
| `config:live.max_usdt_per_trade` | y | y | config.json | defaults=['4500'] |
| `config:live.require_min_trust_score` | n | n/a | config.json | missing from docs |
| `config:live.simulated_balance_usdt` | y | y | config.json | defaults=['100000'] |
| `config:live.trending_watchlist` | y | y | config.json | defaults=[] |
| `config:live.trending_watchlist.enabled` | y | y | config.json | defaults=['True'] |
| `config:live.trending_watchlist.exchange_only` | n | n/a | config.json | missing from docs |
| `config:live.trending_watchlist.exclude_symbols` | y | y | config.json | defaults=[] |
| `config:live.trending_watchlist.gate_only` | n | n/a | config.json | missing from docs |
| `config:live.trending_watchlist.max_coins` | y | y | config.json | defaults=['15'] |
| `config:live.trending_watchlist.refresh_hours` | n | n/a | config.json | missing from docs |
| `config:live.trending_watchlist.strategy_profile` | y | maybe | config.json | doc mentions key; values in configs=["'dry_run_meme'"] |
| `config:live_confirmed` | y | y | config.json | defaults=['True'] |
| `config:lunarcrush` | y | y | config.json | defaults=[] |
| `config:max_daily_trades` | y | y | config.json | defaults=['50'] |
| `config:max_open_positions` | y | y | config.json | defaults=['36'] |
| `config:max_position_percent` | y | y | config.json | defaults=['30'] |
| `config:max_usdt_per_trade` | y | y | config.json | defaults=['4500'] |
| `config:mcp` | y | y | config.json | defaults=[] |
| `config:memory` | y | y | config.json | defaults=[] |
| `config:mid_cap_defaults` | n | n/a | config.json | missing from docs |
| `config:min_x_confidence` | n | n/a | config.json | missing from docs |
| `config:multi_tenant` | y | y | config.json | defaults=[] |
| `config:observability` | y | y | config.json | defaults=[] |
| `config:observability.ask_bridge` | y | y | config.json | defaults=[] |
| `config:observability.ask_bridge.agent_inbox_path` | n | n/a | config.json | missing from docs |
| `config:observability.ask_bridge.auto_respond_enabled` | n | n/a | config.json | missing from docs |
| `config:observability.ask_bridge.cursor_priority_sec` | n | n/a | config.json | missing from docs |
| `config:observability.ask_bridge.cursor_renotify_sec` | n | n/a | config.json | missing from docs |
| `config:observability.ask_bridge.cursor_timeout_sec` | n | n/a | config.json | missing from docs |
| `config:observability.ask_bridge.enabled` | y | y | config.json | defaults=['True'] |
| `config:observability.ask_bridge.grok_fallback_enabled` | n | n/a | config.json | missing from docs |
| `config:observability.ask_bridge.headless_dispatch_delay_sec` | n | n/a | config.json | missing from docs |
| `config:observability.ask_bridge.headless_dispatch_enabled` | y | y | config.json | defaults=['False'] |
| `config:observability.ask_bridge.max_question_length` | n | n/a | config.json | missing from docs |
| `config:observability.ask_bridge.notify_log_path` | n | n/a | config.json | missing from docs |
| `config:observability.ask_bridge.pending_notify_path` | n | n/a | config.json | missing from docs |
| `config:observability.ask_bridge.poll_interval_sec` | y | y | config.json | defaults=['3'] |
| `config:observability.ask_bridge.queue_path` | n | n/a | config.json | missing from docs |
| `config:observability.ask_bridge.rate_limit_per_hour` | n | n/a | config.json | missing from docs |
| `config:observability.ask_bridge.response_mode` | y | maybe | config.json | doc mentions key; values in configs=["'grok_fallback'"] |
| `config:observability.coin_links` | y | y | config.json | defaults=[] |
| `config:observability.coin_links.chart_bars` | n | n/a | config.json | missing from docs |
| `config:observability.coin_links.chart_image_on_executed_trades` | n | n/a | config.json | missing from docs |
| `config:observability.coin_links.chart_timeframe` | n | n/a | config.json | missing from docs |
| `config:observability.coin_links.enabled` | y | y | config.json | defaults=['True'] |
| `config:observability.coin_links.inline_buttons_on_signals` | n | n/a | config.json | missing from docs |
| `config:observability.coin_links.show_cmc` | n | n/a | config.json | missing from docs |
| `config:observability.coin_links.show_gate` | n | n/a | config.json | missing from docs |
| `config:observability.coin_links.show_tradingview` | n | n/a | config.json | missing from docs |
| `config:observability.cycle_failure_alert_after` | n | n/a | config.json | missing from docs |
| `config:observability.cycle_notifications` | y | y | config.json | defaults=[] |
| `config:observability.cycle_notifications.digest_merge` | n | n/a | config.json | missing from docs |
| `config:observability.cycle_notifications.heartbeat_sec` | y | y | config.json | defaults=['3600'] |
| `config:observability.cycle_notifications.hold_explanation_cooldown_hours` | n | n/a | config.json | missing from docs |
| `config:observability.cycle_notifications.hold_explanation_max_per_cycle` | n | n/a | config.json | missing from docs |
| `config:observability.cycle_notifications.min_interval_sec` | n | n/a | config.json | missing from docs |
| `config:observability.cycle_notifications.mode` | y | y | config.json | defaults=["'delta'"] |
| `config:observability.cycle_notifications.notify_hermes_rejected` | n | n/a | config.json | missing from docs |
| `config:observability.cycle_notifications.send_on_blocked` | n | n/a | config.json | missing from docs |
| `config:observability.cycle_notifications.send_on_nav_delta_pct` | n | n/a | config.json | missing from docs |
| `config:observability.cycle_notifications.send_on_new_decision` | n | n/a | config.json | missing from docs |
| `config:observability.cycle_notifications.send_on_trade` | n | n/a | config.json | missing from docs |
| `config:observability.cycle_notifications.social_digest_min_interval_sec` | n | n/a | config.json | missing from docs |
| `config:observability.cycle_notifications.summary_style` | n | n/a | config.json | missing from docs |
| `config:observability.cycle_watchdog` | y | y | config.json | defaults=[] |
| `config:observability.cycle_watchdog._doc` | y | maybe | config.json | doc mentions key; values in configs=["'Start value N=60 = ceil(7200 s / update_interval 120 s), abou |
| `config:observability.cycle_watchdog.auto_silent_cycles` | y | y | config.json | defaults=['60'] |
| `config:observability.cycle_watchdog.enabled` | y | y | config.json | defaults=['True'] |
| `config:observability.cycle_watchdog.fill_silent_cycles` | y | y | config.json | defaults=['60'] |
| `config:observability.daily_report_telegram` | y | y | config.json | defaults=['True'] |
| `config:observability.decisions_audit` | y | y | config.json | defaults=['True'] |
| `config:observability.decisions_log_max_bytes` | n | n/a | config.json | missing from docs |
| `config:observability.decisions_log_rotate_keep` | n | n/a | config.json | missing from docs |
| `config:observability.decisions_mongo_sync` | n | n/a | config.json | missing from docs |
| `config:observability.display_timezone` | y | y | config.json | defaults=["'Europe/Berlin'"] |
| `config:observability.json_logs` | y | y | config.json | defaults=['False'] |
| `config:observability.market_context_notify_min_sec` | n | n/a | config.json | missing from docs |
| `config:observability.market_context_notify_on_boot` | n | n/a | config.json | missing from docs |
| `config:observability.market_context_state_notify` | n | n/a | config.json | missing from docs |
| `config:observability.morning_briefing_enabled` | y | y | config.json | defaults=['True'] |
| `config:observability.morning_briefing_hour` | y | y | config.json | defaults=['8'] |
| `config:observability.notify_on_cycle` | y | y | config.json | defaults=['True'] |
| `config:observability.operator_timezone` | y | y | config.json | defaults=["'Europe/Berlin'"] |
| `config:observability.position_snapshots_enabled` | n | n/a | config.json | missing from docs |
| `config:observability.position_snapshots_every_n_cycles` | n | n/a | config.json | missing from docs |
| `config:observability.position_snapshots_include_exit_sensor` | n | n/a | config.json | missing from docs |
| `config:observability.position_snapshots_mongo_sync` | n | n/a | config.json | missing from docs |
| `config:observability.positions_max_events_per_coin` | n | n/a | config.json | missing from docs |
| `config:observability.positions_show_trade_tree` | n | n/a | config.json | missing from docs |
| `config:observability.stack_compare_enabled` | n | n/a | config.json | missing from docs |
| `config:observability.telegram_command_menu` | y | y | config.json | defaults=[] |
| `config:observability.telegram_command_menu.default_language` | y | y | config.json | defaults=["'de'"] |
| `config:observability.telegram_command_menu.enabled` | y | y | config.json | defaults=['True'] |
| `config:observability.telegram_command_menu.force_language` | n | n/a | config.json | missing from docs |
| `config:observability.telegram_command_menu.reply_keyboard` | y | y | config.json | defaults=['True'] |
| `config:observability.telegram_explanations` | y | y | config.json | defaults=[] |
| `config:observability.telegram_explanations.cmc_digest_min_confidence` | y | y | config.json | defaults=['60'] |
| `config:observability.telegram_explanations.enabled` | y | y | config.json | defaults=['True'] |
| `config:observability.telegram_explanations.language` | y | y | config.json | defaults=["'de'"] |
| `config:observability.telegram_explanations.lc_digest_min_confidence` | n | n/a | config.json | missing from docs |
| `config:observability.telegram_explanations.notify_blocked_trades` | y | y | config.json | defaults=['False'] |
| `config:observability.telegram_explanations.notify_cmc_digest` | y | y | config.json | defaults=['True'] |
| `config:observability.telegram_explanations.notify_hermes_every_cycle` | y | y | config.json | defaults=['False'] |
| `config:observability.telegram_explanations.notify_lc_digest` | n | n/a | config.json | missing from docs |
| `config:observability.telegram_explanations.notify_social_hold_explanations` | y | y | config.json | defaults=['False'] |
| `config:observability.telegram_explanations.notify_x_digest` | y | y | config.json | defaults=['True'] |
| `config:observability.telegram_explanations.show_technical_codes` | y | y | config.json | defaults=['True'] |
| `config:observability.telegram_explanations.verbosity` | y | y | config.json | defaults=["'verbose'"] |
| `config:observability.telegram_explanations.x_digest_min_effective_confidence` | y | y | config.json | defaults=['70'] |
| `config:observability.telegram_startup_notify` | n | n/a | config.json | missing from docs |
| `config:observability.terminal_dashboard` | y | y | config.json | defaults=['True'] |
| `config:onchain_weight` | y | y | config.json | defaults=['0.25'] |
| `config:paper` | y | y | config.json | defaults=[] |
| `config:portfolio_plan` | y | y | config.json | defaults=[] |
| `config:recovery_hold` | y | y | config.json | defaults=[] |
| `config:regime_detector` | y | y | config.json | defaults=[] |
| `config:regime_detector.cooldown_bars` | y | y | config.json | defaults=['6'] |
| `config:regime_detector.dynamic_social` | n | n/a | config.json | missing from docs |
| `config:regime_detector.dynamic_social._doc` | y | maybe | config.json | doc mentions key; values in configs=["'Raise CMC/Santiment/Lunar weight only when ≥2 agree (not alwa |
| `config:regime_detector.dynamic_social.bear_sentiment_weight` | n | n/a | config.json | missing from docs |
| `config:regime_detector.dynamic_social.block_on_climax_harvest` | n | n/a | config.json | missing from docs |
| `config:regime_detector.dynamic_social.block_on_fusion_risk_off` | n | n/a | config.json | missing from docs |
| `config:regime_detector.dynamic_social.bull_sentiment_weight` | n | n/a | config.json | missing from docs |
| `config:regime_detector.dynamic_social.cmc_chorus_trust_mult` | n | n/a | config.json | missing from docs |
| `config:regime_detector.dynamic_social.enabled` | y | y | config.json | defaults=['True'] |
| `config:regime_detector.dynamic_social.min_sources` | n | n/a | config.json | missing from docs |
| `config:regime_detector.dynamic_social.quotes_bull_min_conf` | n | n/a | config.json | missing from docs |
| `config:regime_detector.enabled` | y | y | config.json | defaults=['True'] |
| `config:regime_detector.hysteresis` | y | y | config.json | defaults=['0.15'] |
| `config:regime_detector.sentiment_sources` | n | n/a | config.json | missing from docs |
| `config:regime_detector.sentiment_weight` | y | y | config.json | defaults=['0.38'] |
| `config:regime_detector.tech_weight` | y | y | config.json | defaults=['0.62'] |
| `config:risk` | y | y | config.json | defaults=[] |
| `config:risk._block_stablecoin_buys_doc` | n | n/a | config.json | missing from docs |
| `config:risk.atr_reference_pct` | y | y | config.json | defaults=['3.0'] |
| `config:risk.block_partial_sell_if_sold_percent_above` | y | y | config.json | defaults=['0.99'] |
| `config:risk.block_stablecoin_buys` | n | n/a | config.json | missing from docs |
| `config:risk.cash_floor_basis` | n | n/a | config.json | missing from docs |
| `config:risk.cash_floor_pct` | n | n/a | config.json | missing from docs |
| `config:risk.cash_policy` | y | y | config.json | defaults=[] |
| `config:risk.cash_policy._size_mult_deploy_doc` | n | n/a | config.json | missing from docs |
| `config:risk.cash_policy.dca_buffer_pct_equity` | n | n/a | config.json | missing from docs |
| `config:risk.cash_policy.dca_buffer_usdt` | n | n/a | config.json | missing from docs |
| `config:risk.cash_policy.dca_floor_haircut` | n | n/a | config.json | missing from docs |
| `config:risk.cash_policy.drawdown_delta` | y | y | config.json | defaults=['4'] |
| `config:risk.cash_policy.enabled` | y | y | config.json | defaults=['True'] |
| `config:risk.cash_policy.floor_basis` | n | n/a | config.json | missing from docs |
| `config:risk.cash_policy.floor_pct_base` | n | n/a | config.json | missing from docs |
| `config:risk.cash_policy.floor_pct_max` | n | n/a | config.json | missing from docs |
| `config:risk.cash_policy.floor_pct_min` | n | n/a | config.json | missing from docs |
| `config:risk.cash_policy.harvest_dca_buffer_mult` | n | n/a | config.json | missing from docs |
| `config:risk.cash_policy.link_fusion_size_mult` | n | n/a | config.json | missing from docs |
| `config:risk.cash_policy.mode` | y | y | config.json | defaults=["'adaptive'"] |
| `config:risk.cash_policy.recovery_floor_delta` | n | n/a | config.json | missing from docs |
| `config:risk.cash_policy.regime_deploy_delta` | n | n/a | config.json | missing from docs |
| `config:risk.cash_policy.regime_harvest_delta` | n | n/a | config.json | missing from docs |
| `config:risk.cash_policy.size_mult_deploy` | y | y | config.json | defaults=['0.8'] |
| `config:risk.cash_policy.size_mult_harvest` | y | y | config.json | defaults=['0.7'] |
| `config:risk.dca_reserve_pct` | n | n/a | config.json | missing from docs |
| `config:risk.drawdown_size_multiplier` | y | y | config.json | defaults=['0.75'] |
| `config:risk.drawdown_throttle_pct` | y | y | config.json | defaults=['22.0'] |
| `config:risk.dust_sweep_max_position_usdt` | n | n/a | config.json | missing from docs |
| `config:risk.dust_sweep_min_remainder_usdt` | y | y | config.json | defaults=['100'] |
| `config:risk.dust_sweep_sold_percent_min` | n | n/a | config.json | missing from docs |
| `config:risk.fail_closed_guards` | y | y | config.json | defaults=["'deny'"] |
| `config:risk.liquidity_guard` | n | n/a | config.json | missing from docs |
| `config:risk.liquidity_guard._doc` | y | maybe | config.json | doc mentions key; values in configs=["'Hard buy lock (#641, size cap #671, floors #689). No off-swit |
| `config:risk.liquidity_guard.depth_to_order_multiple` | n | n/a | config.json | missing from docs |
| `config:risk.liquidity_guard.depth_to_order_multiple_min` | n | n/a | config.json | missing from docs |
| `config:risk.liquidity_guard.depth_window_pct` | n | n/a | config.json | missing from docs |
| `config:risk.liquidity_guard.min_quote_volume_24h_usdt` | n | n/a | config.json | missing from docs |
| `config:risk.liquidity_guard.order_book_cache_ttl_sec` | n | n/a | config.json | missing from docs |
| `config:risk.max_daily_buys` | y | y | config.json | defaults=['50'] |
| `config:risk.max_daily_dca_buys` | n | n/a | config.json | missing from docs |
| `config:risk.max_daily_dca_usdt` | y | maybe | config.json | doc mentions key; values in configs=['20000'] |
| `config:risk.max_daily_sells` | y | y | config.json | defaults=['0'] |
| `config:risk.min_position_usdt_for_partial_sell` | y | y | config.json | defaults=['500'] |
| `config:risk.min_sell_notional_usdt` | n | n/a | config.json | missing from docs |
| `config:risk.min_size_multiplier` | y | y | config.json | defaults=['0.25'] |
| `config:risk.min_trade_usdt` | y | y | config.json | defaults=['100.0'] |

… truncated 965 more rows …

## README defaults vs config.json
- `update_interval`: README claims `600`, config.json has `120` → MISMATCH
- `max_usdt_per_trade`: README claims `25`, config.json has `4500` → MISMATCH
- `max_daily_trades`: README claims `8`, config.json has `50` → MISMATCH
- `trade_cooldown_hours`: README claims `1.0`, config.json has `0.35` → MISMATCH
- `notify_on_cycle`: README claims `True`, config.json has `None` → MISMATCH
- `observability.telegram_explanations.enabled`: README claims `True`, config.json has `True` → OK
- `observability.telegram_explanations.verbosity`: README claims `verbose`, config.json has `verbose` → OK
- `live.dry_run`: README claims `True`, config.json has `True` → OK
- `live.dry_run_enhanced`: README claims `False`, config.json has `True` → MISMATCH
- `strategy_backtest.auto_run`: README claims `True`, config.json has `True` → OK

## AGENTS.md checklist
- contains `Hindsight`: NO
- contains `before any research`: NO
- contains `docs map`: NO
- contains `Documentation map`: NO
- contains `llms.txt`: NO
- contains `settings reference`: NO
- contains `owner-approved`: yes

## Stale markers
- README last update line: ['Letzte Aktualisierung: 23. Juni 2026']
- README claims "466+ Unit-Tests" — verify later against pytest count


## Refined findings (pass cleanup 2026-11-05)

False-positive note: the first automated pass treated every `/word` in all docs (including GitHub URLs) as a Telegram command, inflating "wrong" counts. Refined Telegram scan uses README + DOCUMENTATION(+.en) vs `notifications/telegram_commands/` + locales.

### Pass numbers (refined)

| Pass | Method | Result |
|---|---|---|
| 1 doc→code | Slash-commands in README/DOCUMENTATION vs handlers | **54** documented commands; **9** claimed in docs but not found by the coarse handler grep (several exist under dedicated modules, e.g. `/xai_login`, `/diag` — verify individually); many README **defaults wrong** vs `config.json` |
| 2 code→doc | Handlers/locales → docs | **~27** real-ish commands present in code/menus and missing or thin in human docs (incl. `/lock`, `/unlock`, `/pause`, `/resume`, `/maxpositions`, `/config`, …) plus noise tokens from locale strings |
| 3 spot-check | 15 random coverage rows end-to-end | **0/15** structural failures; defaults mismatches confirmed separately |

### Top 5 findings

1. **No agent-facing settings/features/options reference** under `docs/` (Diátaxis reference missing). Agents rely on README tables that are stale.
2. **README "Wichtige Defaults" contradicts committed `config.json`** on staging: `update_interval` 600 vs 120; `max_usdt_per_trade` 25 vs 4500; `max_daily_trades` 8 vs 50; `trade_cooldown_hours` 1.0 vs 0.35; `live.dry_run_enhanced` false vs true; `notify_on_cycle` claimed true but key absent at top level.
3. **Telegram command coverage gaps**: operator/control commands (`/lock`, `/unlock`, `/pause`, `/resume`, `/maxpositions`, `/diag`, `/xai_login`, …) unevenly documented vs `notifications/telegram_commands/`.
4. **AGENTS.md lacks docs map + mandatory Hindsight consult rule** for research/analysis/tickets (Jens 05.11.2026).
5. **Hindsight bank `coding-agent::trading_bot` is stale vs AGENTS.md (2026-11-04)**: still describes lead as "Grok Super Heavy", `devops`-only merges, `rebuild/*` / `direct-staging`, CLAUDE.md ticket-closing checklist; missing Kay merge rule 6, DeepSeek review sessions, `owner-approved`, docs+Hindsight consult rule. Access path: Omnigent MCP for trading-bot has **no** Hindsight tools (29 session tools only); Hindsight is the local HTTP API at `http://127.0.0.1:8888` (bank healthy, 3385 facts) — agents must use Hindsight tools/API, not Omnigent MCP alone.

### Research principles applied (sources)

- AGENTS.md as compact routing index, not encyclopedia — https://agents.md/ ; https://aaif.io/blog/writing-an-effective-agents-md ; https://developers.redhat.com/articles/2026/07/27/standardize-project-context-agentsmd-and-agent-skills
- llms.txt as small link index for agents — https://llmstxt.org/
- Diátaxis: tutorial / how-to / reference / explanation; single source of truth — https://diataxis.fr/ ; https://github.blog/developer-skills/documentation-done-right-a-developers-guide/
- Generate reference from code/schema + CI drift gate — e.g. https://github.com/doc-detective/doc-detective/blob/main/.github/workflows/docs-schema-refs.yml ; Artel freshness pattern https://artel.run/docs/documentation/
- OpenAI Codex AGENTS.md discovery — https://learn.chatgpt.com/docs/agent-configuration/agents-md



## Hindsight reconciliation (read-only, 2026-11-05)

Accessed via Hindsight HTTP API `http://127.0.0.1:8888` on Jens' Mac (bank `coding-agent::trading_bot`, healthy, ~3385 facts). Omnigent MCP tools for trading-bot account: **no** Hindsight/memory tools (29 session/project tools only).

### Contradictions vs AGENTS.md (2026-11-04)

| Memory claim (knowledge pages) | Current docs / AGENTS.md |
|---|---|
| Lead is "Grok Super Heavy"; lead must not merge | Lead bot is **Kay**; Kay may squash-merge paper-only PRs to `staging` under rule 6 |
| Only `devops` merges with `gh pr merge`; staging only for `release/*` or `direct-staging` | Topic PRs target `staging`; Kay/Jens merge rules in AGENTS.md; no `rebuild/*` |
| Ticket closing checklist in CLAUDE.md | `CLAUDE.md` is pointer-only; AGENTS.md is SoT |
| Missing DeepSeek review session naming, `owner-approved`, interim Opus rate-limit | Present in AGENTS.md mandatory rules |
| No settings/features reference pointer | Added in this change set |

No Hindsight write performed (prefer read-only unless a trivial correction is clearly safe; process correction belongs in docs first).
