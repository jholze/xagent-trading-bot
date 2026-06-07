from core.actions import normalize, to_execution_action
from core.config import get_bot_config
from core.models import MarketContext, SignalAnalysis
from strategies.base import BaseStrategy
from strategies.positions import count_open_positions, get_position


def get_ampel_color(rsi, vol_multiplier, price, lower_bb):
    if rsi is None or vol_multiplier is None:
        return "🟡", "Neutral"
    if vol_multiplier >= 1.7 and rsi <= 48:
        return "🟢", "Stark Bullish"
    if rsi <= 42 and price <= lower_bb * 1.015:
        return "🟢", "Bullish (Tief)"
    if rsi >= 68:
        return "🔴", "Bearish"
    if vol_multiplier <= 0.6:
        return "🔴", "Schwaches Volumen"
    return "🟡", "Neutral"


class TechnicalRSIStrategy(BaseStrategy):
    """Pure technical analysis — social signals merged by DecisionEngine."""

    name = "technical_rsi_bb"

    def analyze(self, coin: dict, market: MarketContext, x_signals=None) -> SignalAnalysis:
        symbol = coin["symbol"]
        tf = self.get_timeframe(coin)
        config = get_bot_config()
        params = market.strategy_params or config.strategy_params(symbol, tf)

        rsi_buy_low = params.get("rsi_buy_low", 28)
        rsi_buy_high = params.get("rsi_buy_high", 48)
        volume_multiplier_min = params.get("volume_multiplier", 1.2)
        stop_loss_pct = params.get("stop_loss_pct", config.stop_loss_pct)
        partial_stop = stop_loss_pct * 0.67
        rsi_sell_30 = params.get("rsi_sell_30", 70)
        rsi_sell_20 = params.get("rsi_sell_20", 80)

        ampel_emoji, ampel_text = get_ampel_color(
            market.rsi, market.vol_multiplier, market.current_price, market.lower_bb
        )

        action = "HOLD"
        sources = ["technical"]

        if not market.has_position and market.open_positions < config.max_open_positions:
            if (
                market.current_price <= market.lower_bb * 1.01
                and rsi_buy_low <= market.rsi <= rsi_buy_high
                and market.vol_multiplier >= volume_multiplier_min
            ):
                action = "BUY"
        else:
            entry = market.average_entry
            if entry > 0:
                loss_pct = (market.current_price / entry - 1) * -100
                if loss_pct > stop_loss_pct:
                    action = "SELL_STOP_FULL"
                    sources.append("stop_loss")
                elif loss_pct > partial_stop:
                    action = "SELL_STOP_PARTIAL"
                    sources.append("stop_loss")
            if market.rsi >= rsi_sell_20:
                action = "SELL_20"
            elif market.rsi >= rsi_sell_30:
                action = "SELL_30"

        pos = get_position(symbol, tf)
        last_ampel = pos.get("last_ampel", "🟡")
        last_rsi_old = pos.get("last_rsi", 45.0)
        should_notify = action != "HOLD" or (
            market.has_position and (ampel_emoji != last_ampel or abs(market.rsi - last_rsi_old) > 15)
        )
        notify_reason = "Signal" if action != "HOLD" else "Position Ampel change" if market.has_position else "No position"
        normalized = normalize(action)

        return SignalAnalysis(
            action=to_execution_action(normalized),
            symbol=symbol,
            timeframe=tf,
            rsi=market.rsi,
            lower_bb=market.lower_bb,
            vol_multiplier=market.vol_multiplier,
            ampel_emoji=ampel_emoji,
            ampel_text=ampel_text,
            should_notify=should_notify,
            notify_reason=notify_reason,
            sources=sources,
            normalized_action=normalized,
            rationale=f"TA: RSI={market.rsi:.1f} Vol={market.vol_multiplier:.2f}x",
            confidence=50.0,
        )