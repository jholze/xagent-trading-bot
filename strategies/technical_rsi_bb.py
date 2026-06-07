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
    name = "technical_rsi_bb"

    def analyze(self, coin: dict, market: MarketContext, x_signals=None) -> SignalAnalysis:
        symbol = coin["symbol"]
        tf = self.get_timeframe(coin)
        config = get_bot_config()
        params = market.strategy_params or config.strategy_params(symbol, tf)

        x_signal = next((s for s in (x_signals or []) if s.coin == symbol.split("/")[0]), None)
        x_score = x_signal.score if x_signal else 0.0
        x_confidence = x_signal.confidence if x_signal else 0

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
        x_boost = x_score > 0.6

        if not market.has_position and market.open_positions < config.max_open_positions:
            buy_threshold = rsi_buy_high if x_boost else rsi_buy_high - 3
            technical_buy = (
                market.current_price <= market.lower_bb * 1.01
                and rsi_buy_low <= market.rsi <= buy_threshold
                and market.vol_multiplier >= volume_multiplier_min
            )
            x_conf_threshold = getattr(x_signal, "effective_confidence", x_confidence) if x_signal else 0
            min_x_buy = 75
            if x_signal and hasattr(x_signal, "trust_score"):
                min_x_buy = max(65, 85 - (x_signal.trust_score - 70) * 0.5)
            x_buy = x_signal and x_signal.action == "BUY" and x_conf_threshold >= min_x_buy
            if technical_buy or x_buy:
                action = "BUY"
                if x_buy:
                    sources.append("x")
        else:
            entry = market.average_entry
            if entry > 0:
                loss_pct = (market.current_price / entry - 1) * -100
                if loss_pct > stop_loss_pct or (x_signal and x_signal.action == "SELL" and x_confidence >= 80):
                    action = "SELL_STOP_FULL"
                    sources.append("stop_loss")
                elif loss_pct > partial_stop:
                    action = "SELL_STOP_PARTIAL"
                    sources.append("stop_loss")
            if market.rsi >= rsi_sell_20 or (x_signal and x_signal.action == "SELL" and x_confidence >= 70):
                action = "SELL_20"
                sources.append("x" if x_signal else "technical")
            elif market.rsi >= rsi_sell_30:
                action = "SELL_30"
                sources.append("technical")

        pos = get_position(symbol, tf)
        last_ampel = pos.get("last_ampel", "🟡")
        last_rsi_old = pos.get("last_rsi", 45.0)
        should_notify = action != "HOLD" or (
            market.has_position and (ampel_emoji != last_ampel or abs(market.rsi - last_rsi_old) > 15)
        )
        notify_reason = "Signal" if action != "HOLD" else "Position Ampel change" if market.has_position else "No position"

        return SignalAnalysis(
            action=action,
            symbol=symbol,
            timeframe=tf,
            rsi=market.rsi,
            lower_bb=market.lower_bb,
            vol_multiplier=market.vol_multiplier,
            ampel_emoji=ampel_emoji,
            ampel_text=ampel_text,
            should_notify=should_notify,
            notify_reason=notify_reason,
            x_confidence=x_confidence,
            sources=sources,
        )