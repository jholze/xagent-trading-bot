from data_manager import get_config, is_dry_run_enhanced as _is_dry_run_enhanced, reload_config
from logger import log


class BotConfig:
    """Typed accessors over config (supports per-tenant via tenant_id)."""

    def __init__(self, raw: dict | None = None, *, tenant_id: str | None = None):
        if raw is not None:
            self._raw = raw
        else:
            if tenant_id:
                self._raw = get_config(tenant_id=tenant_id)
            else:
                self._raw = get_config()
        self._tenant_id = tenant_id

    def refresh(self):
        self._raw = reload_config(tenant_id=getattr(self, "_tenant_id", None))
        return self

    @property
    def raw(self) -> dict:
        return self._raw

    @property
    def virtual_trading(self) -> bool:
        return self._raw.get("virtual_trading", True)

    @property
    def trading_mode(self) -> str:
        mode = self._raw.get("trading_mode")
        if mode == "gate_testnet":
            log(
                "trading_mode gate_testnet is deprecated — use paper or live; treating as paper",
                "WARNING",
            )
            return "paper"
        if mode:
            return mode
        return "paper" if self.virtual_trading else "off"

    @property
    def live_confirmed(self) -> bool:
        return bool(self._raw.get("live_confirmed", False))

    @property
    def live_config(self) -> dict:
        return self._raw.get("live", {})

    @property
    def exchange(self) -> str:
        """Primary exchange for live trading (e.g. 'gate', 'binance')."""
        return self.live_config.get("exchange", "gate")

    @property
    def dry_run_defaults(self) -> dict:
        return self._raw.get("dry_run_defaults", {})

    @property
    def dry_run_enhanced(self) -> bool:
        return self.is_dry_run_enhanced()

    @property
    def simulated_balance_usdt(self) -> float:
        return float(self.live_config.get("simulated_balance_usdt", 5000))

    @property
    def trending_watchlist_config(self) -> dict:
        defaults = {
            "enabled": True,
            "live_enabled": True,
            "max_coins": 15,
            "refresh_hours": 1,
            "exchange_only": True,
            "gate_only": True,  # legacy
            "prune_non_gate": True,
            "prune_base_watchlist": True,
            "max_open_from_trending": 8,
            "source_priority": [
                "trending/latest",
                "trending/gainers-losers",
                "listings/latest",
            ],
        }
        cmc_tw = self.cmc_config.get("trending_watchlist") or {}
        live_tw = self.live_config.get("trending_watchlist") or {}
        return {**defaults, **live_tw, **cmc_tw}

    @property
    def cmc_trending_fusion_config(self) -> dict:
        defaults = {
            "enabled": True,
            "min_confidence_trending": 50,
            "allow_cmc_only_buy_top_n": 8,
            "cmc_only_buy_min_confidence": 58,
            "block_buy_if_rsi_above": 68,
            "require_volatile_atr_tier": True,
            "trending_trade_size_pct": 50,
        }
        raw = self.cmc_config.get("cmc_trending_fusion") or {}
        return {**defaults, **raw}

    def is_dry_run_enhanced(self) -> bool:
        return _is_dry_run_enhanced(self._raw)

    @property
    def paper_config(self) -> dict:
        return self._raw.get("paper", {})

    @property
    def initial_capital_usdt(self) -> float:
        paper = self.paper_config.get("initial_capital_usdt")
        if paper:
            return float(paper)
        return float(self._raw.get("initial_capital_usdt", 5000))

    @property
    def max_daily_trades(self) -> int:
        return int(self._raw.get("max_daily_trades", 5))

    @property
    def trade_cooldown_hours(self) -> float:
        return float(self._raw.get("trade_cooldown_hours", 1.0))

    @property
    def max_usdt_per_trade(self) -> float:
        return float(self._raw.get("max_usdt_per_trade", 150))

    @property
    def max_open_positions(self) -> int:
        return int(self._raw.get("max_open_positions", 5))

    @property
    def slippage_percent(self) -> float:
        return float(self._raw.get("slippage_percent", 1.5))

    @property
    def update_interval(self) -> int:
        return int(self._raw.get("update_interval", 600))

    @property
    def stop_loss_pct(self) -> float:
        return float(self._raw.get("stop_loss_pct", 12.0))

    @property
    def max_position_percent(self) -> float:
        return float(self._raw.get("max_position_percent", 30))

    @property
    def aggression_config(self) -> dict:
        return self._raw.get("aggression", {})

    @property
    def risk_config(self) -> dict:
        return self._raw.get("risk", {})

    @property
    def strategy_backtest_config(self) -> dict:
        return self._raw.get("strategy_backtest", {})

    @property
    def sandbox_config(self) -> dict:
        return self._raw.get("sandbox", {})

    @property
    def sandbox_enabled(self) -> bool:
        return bool(self.sandbox_config.get("enabled", True))

    @property
    def cmc_config(self) -> dict:
        return self._raw.get("cmc", {})

    @property
    def lunarcrush_config(self) -> dict:
        return self._raw.get("lunarcrush", {})

    @property
    def altcoin_social_config(self) -> dict:
        return self._raw.get("altcoin_social", {})

    @property
    def volatile_altcoin_config(self) -> dict:
        return self._raw.get("volatile_altcoin", {})

    @property
    def stable_altcoin_config(self) -> dict:
        return self._raw.get("stable_altcoin", {})

    @property
    def mid_cap_defaults_config(self) -> dict:
        return self._raw.get("mid_cap_defaults", {})

    @property
    def x_weight(self) -> float:
        return float(self._raw.get("x_weight", 0.45))

    @property
    def technical_weight(self) -> float:
        return float(self._raw.get("technical_weight", 0.35))

    @property
    def onchain_weight(self) -> float:
        return float(self._raw.get("onchain_weight", 0.2))

    @property
    def lc_weight(self) -> float:
        return float(self._raw.get("lc_weight", 0.18))

    @property
    def observability_config(self) -> dict:
        return self._raw.get("observability", {})

    @property
    def terminal_dashboard_enabled(self) -> bool:
        return bool(self.observability_config.get("terminal_dashboard", True))

    @property
    def notify_on_cycle(self) -> bool:
        return bool(self.observability_config.get("notify_on_cycle", False))

    @property
    def cycle_notifications_config(self) -> dict:
        defaults = {
            "mode": "delta",
            "send_on_trade": True,
            "send_on_blocked": True,
            "send_on_nav_delta_pct": 0.5,
            "send_on_new_decision": True,
            "hold_explanation_max_per_cycle": 1,
            "hold_explanation_cooldown_hours": 6,
            "digest_merge": True,
            "notify_hermes_rejected": False,
        }
        raw = self.observability_config.get("cycle_notifications", {})
        return {**defaults, **raw}

    @property
    def decisions_audit_enabled(self) -> bool:
        return bool(self.observability_config.get("decisions_audit", True))

    @property
    def telegram_explanations_config(self) -> dict:
        defaults = {
            "enabled": True,
            "verbosity": "verbose",
            "language": "de",
            "show_technical_codes": True,
            "notify_hermes_every_cycle": True,
            "notify_cmc_digest": True,
            "notify_lc_digest": True,
            "notify_x_digest": True,
            "notify_social_hold_explanations": True,
            "notify_blocked_trades": True,
            "cmc_digest_min_confidence": 60,
            "lc_digest_min_confidence": 55,
            "x_digest_min_effective_confidence": 70,
        }
        raw = self.observability_config.get("telegram_explanations", {})
        return {**defaults, **raw}

    @property
    def telegram_command_menu_config(self) -> dict:
        defaults = {
            "enabled": True,
            "button_text": "Menü",
            "reply_keyboard": True,
            "default_language": "de",
        }
        raw = self.observability_config.get("telegram_command_menu", {})
        return {**defaults, **raw}

    @property
    def coin_links_config(self) -> dict:
        defaults = {
            "enabled": True,
            "show_cmc": True,
            "show_gate": True,
            "show_tradingview": True,
            "inline_buttons_on_signals": True,
            "chart_image_on_executed_trades": True,
            "chart_bars": 48,
            "chart_timeframe": "4h",
        }
        raw = self.observability_config.get("coin_links", {})
        return {**defaults, **raw}

    @property
    def hermes_config(self) -> dict:
        return self._raw.get("hermes", {})

    @property
    def hermes_enabled(self) -> bool:
        return bool(self.hermes_config.get("enabled", False))

    @property
    def architecture_config(self) -> dict:
        defaults = {
            "mode": "monolith",
            "redis_url": "redis://127.0.0.1:6379/0",
            "key_prefix": "aria:",
            "notification_mode": "async",
            "notification_rate_limit_sec": 1.0,
            "hermes_external": False,
            "min_hours_after_sell_before_rebuy": 4.0,
            "rebuy_after_stop_loss_hours": 24.0,
            "block_rebuy_if_last_sell_was_stop": True,
            "heartbeat_ttl_sec": 120,
            "heartbeat_warn_enabled": True,
            "use_signal_snapshot": False,
            "price_cache_enabled": True,
            "price_cache_ttl_sec": 120,
            "ohlcv_cache_enabled": True,
            "ohlcv_cache_ttl_sec": {"15m": 60, "1h": 90, "4h": 120},
            "funding_cache_ttl_sec": 300,
            "coin_query_webhook_enabled": True,
            "signal_webhook_enabled": True,
            "signal_webhook_token": "",
            "signal_event_ttl_sec": 3600,
            "signal_webhook_rate_limit_per_min": 10,
            "background_social_enabled": True,
            "background_backtest_enabled": True,
            "background_social_interval_sec": 0,
            "social_snapshot_max_age_sec": 300,
            "dedup_ttl_sec": 86400,
            "trading_engine_mode": "in_process",
            "ledger_lock_enabled": True,
            "ledger_lock_ttl_sec": 30,
            "ledger_lock_wait_sec": 15,
            "trade_intent_queue_enabled": False,
            "trade_intent_async_auto_only": True,
            "eval_queue_enabled": False,
            "eval_worker_poll_sec": 2.0,
            "eval_batch_size": 3,
            "eval_debounce_sec": 45,
            "eval_position_heartbeat_sec": 300,
            "eval_stale_sec": 7200,
            "eval_meta_interval_sec": 300,
            "eval_queue_max_len": 500,
            "ledger_backend": "local",
            "ledger_dual_write": False,
        }
        raw = self._raw.get("architecture", {})
        return {**defaults, **raw}

    @property
    def ledger_backend(self) -> str:
        return str(self.architecture_config.get("ledger_backend", "local"))

    @property
    def ledger_dual_write(self) -> bool:
        return bool(self.architecture_config.get("ledger_dual_write", False))

    @property
    def architecture_mode(self) -> str:
        return str(self.architecture_config.get("mode", "monolith"))

    @property
    def min_hours_after_sell_before_rebuy(self) -> float:
        arch = self.architecture_config
        risk = self.risk_config
        return float(
            arch.get("min_hours_after_sell_before_rebuy")
            or risk.get("min_hours_after_sell_before_rebuy")
            or 4.0
        )

    @property
    def hermes_live_evidence_config(self) -> dict:
        return self.hermes_config.get("live_evidence", {})

    def strategy_params(self, symbol: str, timeframe: str) -> dict:
        for entry in self._raw.get("strategies", []):
            if entry.get("symbol") == symbol and entry.get("timeframe") == timeframe:
                return entry
        return {}

    @property
    def entry_sensor_15m_config(self) -> dict:
        defaults = {
            "enabled": True,
            "mode": "shadow",
            "timeframe": "15m",
            "poll_interval_sec": 20,
            "exchange_only": True,
            "gate_only": True,  # legacy
            "market_cap_min_usd": 5_000_000,
            "watch_ttl_hours": 24,
            "setup_modes": ["buy_signal", "setup_zone", "trending", "watchlist"],
            "vol_spike_mult": 2.0,
            "vol_avg_period": 20,
            "ema_period": 9,
            "require_ema_breakout": False,
            "block_buy_if_rsi_4h_above": 75,
            "fakeout_min_body_atr_ratio": 0.3,
            "cooldown_after_reject_hours": 2,
            "max_watched_coins": 40,
            "min_poll_gap_sec_per_coin": 20,
        }
        raw = self._raw.get("entry_sensor_15m", {})
        merged = {**defaults, **raw}
        if "exchange_only" not in raw and "gate_only" in raw:
            merged["exchange_only"] = raw["gate_only"]
        return merged

    @property
    def entry_sensor_15m_enabled(self) -> bool:
        return bool(self.entry_sensor_15m_config.get("enabled", False))

    # ============================================================
    # NEW: Regime-aware adaptive system configuration (opt-in)
    # ============================================================
    # To enable: set "regime_detector": {"enabled": true, ...} and "strategy_allocator": {"enabled": true}
    # in config.json (or per-tenant). Extends (does not replace) volatility_tier / buy_regime.
    # See intelligence/regime_detector.py, strategy_allocator.py, strategies/grid.py
    # Logs: [Regime] ... will appear; regime info attached to SignalAnalysis for notifiers.
    # Backward compatible: defaults keep legacy behavior.

    @property
    def regime_detector_config(self) -> dict:
        defaults = {
            "enabled": False,  # opt-in (set true to activate adaptive regime switching)
            "tech_weight": 0.62,
            "sentiment_weight": 0.38,
            "cooldown_bars": 6,
            "hysteresis": 0.15,
            "sentiment_sources": ["lunarcrush", "santiment", "x", "fear_greed"],
            "regimes": {
                "RANGING": {"tech_score_range": [-0.4, 0.4]},
                "STRONG_UPTREND": {"tech_score_min": 0.55},
                "STRONG_DOWNTREND": {"tech_score_max": -0.55},
                "CHOPPY_HIGH_VOL": {"volatility_min": 0.7},
                "TRANSITION": {}
            }
        }
        raw = self._raw.get("regime_detector", {})
        return {**defaults, **raw}

    @property
    def strategy_allocator_config(self) -> dict:
        defaults = {
            "enabled": False,  # tied to regime_detector
            "neutral_sentiment_threshold": 0.35,
            "confirm_sentiment_threshold": 0.45,
            "defensive_sentiment_threshold": -0.55,
            "default_grid_weight": 0.6,
            "default_momentum_weight": 0.4
        }
        raw = self._raw.get("strategy_allocator", {})
        return {**defaults, **raw}

    @property
    def grid_config(self) -> dict:
        defaults = {
            "enabled": True,
            "default_spacing_atr_mult": 0.8,
            "re_center_atr_mult": 2.5,
            "fee_aware": True,
            "max_levels": 12,
            "use_limit_orders": True
        }
        raw = self._raw.get("grid", {})
        return {**defaults, **raw}

    @property
    def entry_sensor_15m_mode(self) -> str:
        return str(self.entry_sensor_15m_config.get("mode", "shadow")).strip().lower()

    @property
    def trading_profile(self) -> str | None:
        raw = self._raw.get("trading_profile")
        return str(raw).strip().lower() if raw else None

    @property
    def coin_filters_config(self) -> dict:
        from core.trading_profiles import coin_filters_config
        return coin_filters_config(self._raw)

    @property
    def multi_tenant_config(self) -> dict:
        defaults = {
            "enabled": False,
            "default_tenant": "default",
            "onboarding_enabled": True,
            "require_webhook_secret": True,
        }
        raw = self._raw.get("multi_tenant", {})
        return {**defaults, **raw}

    @property
    def entry_guard_config(self) -> dict:
        defaults = {
            "enabled": True,
            "sources": ["entry_sensor_15m"],
            "fresh_entry_window_minutes": 120,
            "vol_spike_mult": 2.0,
            "vol_exhaustion_15m_max": 0.85,
            "exhaustion_min_gain_pct": 5.0,
            "mega_pump_gain_pct": 12.0,
            "block_loss_sells_minutes": 15,
            "by_tier": {
                "meme": {"min_hold_minutes": 30, "min_gain_structure_pct": 6},
                "volatile": {"min_hold_minutes": 45, "min_gain_structure_pct": 8},
                "normal": {"min_hold_minutes": 60, "min_gain_structure_pct": 10},
                "large_cap": {"min_hold_minutes": 90, "min_gain_structure_pct": 12},
            },
        }
        raw = self._raw.get("entry_guard") or {}
        merged = {**defaults, **raw}
        if raw.get("by_tier"):
            merged["by_tier"] = {**defaults["by_tier"], **raw["by_tier"]}
        return merged

    @property
    def exit_sensor_config(self) -> dict:
        defaults = {
            "enabled": True,
            "mode": "live",
            "min_gain_pct": 7,
            "vol_avg_period": 20,
            "weakness_15m": {
                "enabled": True,
                "ema_period": 20,
                "min_gain_pct": 7,
            },
            "volume_climax": {
                "enabled": True,
                "vol_spike_min": 3.0,
                "upper_wick_min_pct": 55,
                "max_body_atr_ratio": 0.35,
                "near_high_tolerance_pct": 2.0,
                "min_gain_pct": 7,
            },
            "pullback": {
                "enabled": True,
                "min_drop_pct": 3.5,
                "require_vol_above_avg": True,
                "min_gain_pct": 6,
            },
            "btc_rs": {
                "enabled": True,
                "min_underperformance_pct": 2.0,
                "min_gain_pct": 7,
                "timeframe": "4h",
                "periods": 1,
            },
            "rsi_rollover_1h": {
                "enabled": True,
                "peak_rsi_min": 70,
                "current_rsi_max": 60,
                "min_gain_pct": 7,
            },
        }
        raw = self._raw.get("exit_sensor") or {}
        merged = {**defaults, **raw}
        for key in (
            "weakness_15m",
            "volume_climax",
            "pullback",
            "btc_rs",
            "rsi_rollover_1h",
        ):
            if key in raw:
                merged[key] = {**defaults[key], **raw[key]}
        return merged


def get_bot_config(tenant_id: str | None = None) -> BotConfig:
    return BotConfig(tenant_id=tenant_id)