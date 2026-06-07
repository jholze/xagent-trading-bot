from core.actions import (
    ADD_WATCHLIST,
    BUY,
    BUY_STRONG,
    HOLD,
    IGNORE,
    SELL_FULL,
    SELL_PARTIAL_20,
    SELL_PARTIAL_30,
    SELL_PARTIAL_50,
    is_buy,
    is_sell,
    normalize,
    to_execution_action,
)
from core.config import get_bot_config
from core.models import MarketContext, SignalAnalysis
from data_manager import load_watchlist
from services.market_service import MarketService
from strategies.positions import count_open_positions, get_position
from strategies.registry import get_strategy, resolve_coin_config


class DecisionEngine:
    """Merges technical strategy output with social signals and consensus."""

    SELL_PRIORITY = {
        SELL_FULL: 5,
        SELL_PARTIAL_50: 4,
        SELL_PARTIAL_30: 3,
        SELL_PARTIAL_20: 2,
        HOLD: 0,
    }

    def __init__(self, market_service: MarketService = None):
        self.config = get_bot_config()
        self.market = market_service or MarketService()

    def build_market_context(self, coin: dict, current_price: float) -> MarketContext:
        coin = resolve_coin_config(coin)
        symbol = coin["symbol"]
        tf = coin.get("timeframe", "4h")
        indicators = self.market.fetch_indicators(symbol, tf, current_price)
        pos = get_position(symbol, tf)
        params = coin.get("strategy_params") or self.config.strategy_params(symbol, tf)
        return MarketContext(
            symbol=symbol,
            timeframe=tf,
            current_price=current_price,
            rsi=indicators["rsi"],
            lower_bb=indicators["lower_bb"],
            vol_multiplier=indicators["vol_multiplier"],
            has_position=float(pos["amount"]) > 0,
            average_entry=pos.get("average_entry", 0),
            open_positions=count_open_positions(),
            strategy_params=params,
        )

    def _x_signals_for_coin(self, symbol: str, x_signals: list) -> list:
        base = symbol.split("/")[0]
        return [s for s in (x_signals or []) if getattr(s, "coin", "") == base]

    def _consensus_multiplier(self, coin_signals: list) -> float:
        actionable = [s for s in coin_signals if getattr(s, "action", "HOLD") in ("BUY", "SELL")]
        if len(actionable) >= 3:
            return 1.25
        if len(actionable) >= 2:
            return 1.1
        return 1.0

    def _x_buy_threshold(self, x_signal) -> float:
        trust = getattr(x_signal, "trust_score", 70)
        return max(65.0, 85 - (trust - 70) * 0.5)

    def _merge_buy(self, technical: SignalAnalysis, x_signal, coin_signals: list, market: MarketContext) -> tuple:
        sources = list(technical.sources)
        x_buy = False
        eff = 0.0
        tech_buy = normalize(technical.action) in (BUY, BUY_STRONG) or technical.action == "BUY"

        if x_signal and x_signal.action == "BUY":
            eff = getattr(x_signal, "effective_confidence", x_signal.confidence)
            eff *= self._consensus_multiplier(coin_signals)
            if eff >= self._x_buy_threshold(x_signal):
                x_buy = True
                sources.append("x")

        if not market.has_position and market.open_positions < self.config.max_open_positions:
            if tech_buy and x_buy:
                return BUY_STRONG, sources, max(technical.confidence, eff)
            if tech_buy or x_buy:
                return BUY, sources, max(technical.confidence, eff)
        return HOLD, sources, technical.confidence

    def _merge_sell(self, technical: SignalAnalysis, x_signal, coin_signals: list) -> tuple:
        sources = list(technical.sources)
        candidates = []

        tech_norm = normalize(technical.action)
        if is_sell(technical.action):
            candidates.append((tech_norm, self.SELL_PRIORITY.get(tech_norm, 1), "technical"))

        if x_signal and x_signal.action == "SELL":
            eff = getattr(x_signal, "effective_confidence", x_signal.confidence)
            eff *= self._consensus_multiplier(coin_signals)
            if eff >= 70:
                candidates.append((SELL_PARTIAL_20, 2, "x"))
                sources.append("x")

        if not candidates:
            return HOLD, sources, technical.confidence

        best = max(candidates, key=lambda c: c[1])
        return best[0], sources, max(technical.confidence, getattr(x_signal, "effective_confidence", 0) if x_signal else 0)

    def evaluate(self, coin: dict, current_price: float, x_signals=None) -> SignalAnalysis:
        if not current_price:
            return None

        coin = resolve_coin_config(coin)
        market = self.build_market_context(coin, current_price)
        strategy = get_strategy(coin)
        technical = strategy.analyze(coin, market, x_signals=None)

        coin_x = self._x_signals_for_coin(coin["symbol"], x_signals)
        x_signal = coin_x[0] if coin_x else None

        if market.has_position:
            normalized, sources, confidence = self._merge_sell(technical, x_signal, coin_x)
            if normalized == HOLD and not is_sell(technical.action):
                normalized = normalize(technical.action) if is_sell(technical.action) else HOLD
                sources = list(technical.sources)
                confidence = technical.confidence
        else:
            normalized, sources, confidence = self._merge_buy(technical, x_signal, coin_x, market)
            if normalized == HOLD:
                normalized = normalize(technical.action) if normalize(technical.action) == BUY else HOLD

        if normalized == HOLD and technical.action != "HOLD":
            normalized = normalize(technical.action)
            sources = list(technical.sources)

        execution_action = to_execution_action(normalized)
        rationale_parts = []
        if "technical" in sources:
            rationale_parts.append(f"TA→{technical.action}")
        if "x" in sources and x_signal:
            rationale_parts.append(f"X→{x_signal.action}@{x_signal.account}({x_signal.confidence}%)")
        if normalized == BUY_STRONG:
            rationale_parts.append("strong consensus")

        return SignalAnalysis(
            action=execution_action,
            symbol=technical.symbol,
            timeframe=technical.timeframe,
            rsi=technical.rsi,
            lower_bb=technical.lower_bb,
            vol_multiplier=technical.vol_multiplier,
            ampel_emoji=technical.ampel_emoji,
            ampel_text=technical.ampel_text,
            should_notify=technical.should_notify or execution_action != "HOLD",
            notify_reason=technical.notify_reason if execution_action == "HOLD" else "Decision",
            x_confidence=getattr(x_signal, "confidence", 0) if x_signal else 0,
            sources=sources,
            normalized_action=normalized,
            rationale=" | ".join(rationale_parts) or technical.notify_reason,
            confidence=confidence or technical.x_confidence,
            recommended=execution_action != "HOLD",
        )

    def to_recommendation(self, x_signal, analysis: SignalAnalysis, account: str, tweet_text: str, price: float) -> dict:
        recommendation = {
            "account": account,
            "action": IGNORE,
            "confidence": x_signal.confidence,
            "rationale": analysis.rationale or x_signal.rationale,
            "coin": x_signal.coin,
            "recommended": False,
            "raw_tweet": tweet_text[:200],
            "trust_at_signal": getattr(x_signal, "trust_score", 70),
            "parsed_action": x_signal.action,
            "signal_price": price,
        }

        if x_signal.coin == "UNKNOWN":
            return recommendation

        norm = analysis.normalized_action
        if is_buy(norm) and x_signal.action == "BUY":
            recommendation["action"] = BUY_STRONG if norm == BUY_STRONG else "BUY"
            recommendation["recommended"] = True
        elif is_sell(norm) and x_signal.action == "SELL":
            recommendation["action"] = "SELL"
            recommendation["recommended"] = True
        elif x_signal.coin not in [c["symbol"].split("/")[0] for c in load_watchlist()]:
            recommendation["action"] = ADD_WATCHLIST
            recommendation["recommended"] = True

        return recommendation