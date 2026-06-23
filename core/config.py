from data_manager import get_config, reload_config
from logger import log


class BotConfig:
    """Typed accessors over config.json."""

    def __init__(self, raw: dict | None = None):
        self._raw = raw if raw is not None else get_config()

    def refresh(self):
        self._raw = reload_config()
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
            "refresh_hours": 4,
            "gate_only": True,
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
        if self.trading_mode != "live":
            return False
        if not self.live_config.get("dry_run", True):
            return False
        return bool(self.live_config.get("dry_run_enhanced", False))

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
        }
        raw = self._raw.get("architecture", {})
        return {**defaults, **raw}

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


def get_bot_config() -> BotConfig:
    return BotConfig()