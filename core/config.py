from data_manager import get_config, reload_config


class BotConfig:
    """Typed accessors over config.json."""

    def __init__(self):
        self._raw = get_config()

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

    def strategy_params(self, symbol: str, timeframe: str) -> dict:
        for entry in self._raw.get("strategies", []):
            if entry.get("symbol") == symbol and entry.get("timeframe") == timeframe:
                return entry
        return {}


def get_bot_config() -> BotConfig:
    return BotConfig()