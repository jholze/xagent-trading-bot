from strategies.base import BaseStrategy


def get_strategy(coin: dict) -> BaseStrategy:
    from strategies.technical_rsi_bb import TechnicalRSIStrategy

    registry = {"technical_rsi_bb": TechnicalRSIStrategy}
    strategy_class = coin.get("strategy_class", "technical_rsi_bb")
    cls = registry.get(strategy_class, TechnicalRSIStrategy)
    return cls()