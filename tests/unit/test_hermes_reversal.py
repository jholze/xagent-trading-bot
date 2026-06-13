from core.models import MarketContext
from strategies.technical_rsi_bb import TechnicalRSIStrategy


def test_reversal_buy_triggers_on_rsi_cross_up():
    strategy = TechnicalRSIStrategy()
    sim_state = {"last_rsi": 30.0, "last_ampel": "🟡", "rsi_sell_tiers_done": {}}
    market = MarketContext(
        symbol="TEST/USDT",
        timeframe="4h",
        current_price=100.0,
        rsi=40.0,
        lower_bb=95.0,
        vol_multiplier=1.5,
        has_position=False,
        open_positions=0,
        strategy_params={
            "buy_regime": "reversal",
            "reversal_rsi_cross_low": 32,
            "reversal_rsi_cross_high": 38,
            "reversal_volume_multiplier": 1.2,
        },
        sim_state=sim_state,
    )
    coin = {"symbol": "TEST/USDT", "timeframe": "4h"}
    analysis = strategy.analyze(coin, market)
    assert analysis.action == "BUY"
    assert "reversal" in analysis.sources


def test_dip_regime_ignores_reversal_without_bb_touch():
    strategy = TechnicalRSIStrategy()
    sim_state = {"last_rsi": 30.0, "last_ampel": "🟡", "rsi_sell_tiers_done": {}}
    market = MarketContext(
        symbol="TEST/USDT",
        timeframe="4h",
        current_price=100.0,
        rsi=40.0,
        lower_bb=95.0,
        vol_multiplier=1.5,
        has_position=False,
        open_positions=0,
        strategy_params={"buy_regime": "dip"},
        sim_state=sim_state,
    )
    coin = {"symbol": "TEST/USDT", "timeframe": "4h"}
    analysis = strategy.analyze(coin, market)
    assert analysis.action == "HOLD"


def test_both_regime_accepts_reversal():
    strategy = TechnicalRSIStrategy()
    sim_state = {"last_rsi": 28.0, "last_ampel": "🟡", "rsi_sell_tiers_done": {}}
    market = MarketContext(
        symbol="TEST/USDT",
        timeframe="4h",
        current_price=100.0,
        rsi=39.0,
        lower_bb=98.0,
        vol_multiplier=1.4,
        has_position=False,
        open_positions=0,
        strategy_params={"buy_regime": "both"},
        sim_state=sim_state,
    )
    coin = {"symbol": "TEST/USDT", "timeframe": "4h"}
    analysis = strategy.analyze(coin, market)
    assert analysis.action == "BUY"