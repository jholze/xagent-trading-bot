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
        return self.live_config.get("trending_watchlist", {})

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
    def hermes_live_evidence_config(self) -> dict:
        return self.hermes_config.get("live_evidence", {})

    def strategy_params(self, symbol: str, timeframe: str) -> dict:
        for entry in self._raw.get("strategies", []):
            if entry.get("symbol") == symbol and entry.get("timeframe") == timeframe:
                return entry
        return {}


def get_bot_config() -> BotConfig:
    return BotConfig()