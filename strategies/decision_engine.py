from core.actions import (
    ADD_WATCHLIST,
    BUY,
    BUY_DCA,
    BUY_STRONG,
    HOLD,
    IGNORE,
    SELL_FULL,
    SELL_PARTIAL_10,
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
from data_manager import is_dry_run_enhanced, load_effective_watchlist
from services.market_service import MarketService
from strategies.market_structure import (
    evaluate_market_structure_buy_boost,
    evaluate_market_structure_sells,
)
from strategies.dca import evaluate_dca_addon
from strategies.trailing_stop import evaluate_trailing_stop
from intelligence.volatility_classifier import volatility_tier
from strategies.positions import count_open_positions, get_position, lock_strategy_tier, update_market_snapshot
from strategies.registry import (
    get_strategy,
    resolve_coin_config,
    resolve_effective_timeframe,
    resolve_strategy_params,
)


class DecisionEngine:
    """Merges technical strategy output with X, CMC, and market-structure signals."""

    SELL_PRIORITY = {
        SELL_FULL: 5,
        SELL_PARTIAL_50: 4,
        SELL_PARTIAL_30: 3,
        SELL_PARTIAL_20: 2,
        SELL_PARTIAL_10: 1,
        HOLD: 0,
    }

    def __init__(self, market_service: MarketService = None):
        self.config = get_bot_config()
        self.market = market_service or MarketService()

    def build_market_context(self, coin: dict, current_price: float) -> MarketContext:
        symbol = coin.get("symbol", "")
        watchlist_tf = coin.get("timeframe", "4h")
        tf = resolve_effective_timeframe(coin)
        from intelligence.strategy_backtest import classify_coin

        if tf == watchlist_tf and classify_coin(symbol, coin.get("strategy_params")) != "large_cap":
            peek = self.market.fetch_indicators(symbol, "4h", current_price)
            atr_peek = float(peek.get("atr_pct", 3.0))
            tf_refined = resolve_effective_timeframe(coin, atr_pct=atr_peek)
            if tf_refined != tf:
                tf = tf_refined
                indicators = self.market.fetch_indicators(symbol, tf, current_price)
                atr_pct = float(indicators.get("atr_pct", 3.0))
            else:
                indicators = peek
                atr_pct = atr_peek
        else:
            indicators = self.market.fetch_indicators(symbol, tf, current_price)
            atr_pct = float(indicators.get("atr_pct", 3.0))

        coin = resolve_coin_config({**coin, "timeframe": tf})
        symbol = coin["symbol"]
        pos = get_position(symbol, tf)
        has_position = float(pos["amount"]) > 0
        frozen = pos.get("strategy_tier") if has_position else None
        va_cfg = self.config.volatile_altcoin_config
        if has_position and not frozen and va_cfg.get("freeze_tier_on_entry", True):
            tier = volatility_tier(coin, atr_pct, va_cfg)
            lock_strategy_tier(symbol, tf, tier)
            frozen = tier
            pos = get_position(symbol, tf)
        params = resolve_strategy_params(
            coin,
            has_position=has_position,
            atr_pct=atr_pct,
            frozen_tier=frozen,
        )
        if has_position:
            update_market_snapshot(symbol, tf, current_price, atr_pct)
            pos = get_position(symbol, tf)
        return MarketContext(
            symbol=symbol,
            timeframe=tf,
            current_price=current_price,
            rsi=indicators["rsi"],
            lower_bb=indicators["lower_bb"],
            middle_bb=indicators.get("middle_bb", indicators["lower_bb"]),
            upper_bb=indicators.get("upper_bb", indicators["lower_bb"]),
            atr_pct=atr_pct,
            vol_multiplier=indicators["vol_multiplier"],
            has_position=has_position,
            average_entry=pos.get("average_entry", 0),
            open_positions=count_open_positions(),
            strategy_params=params,
        )

    def _signals_for_coin(self, symbol: str, signals: list) -> list:
        base = symbol.split("/")[0]
        return [s for s in (signals or []) if getattr(s, "coin", "") == base]

    def _all_coin_social_signals(self, symbol: str, x_signals: list, cmc_signals: list, lc_signals: list = None) -> list:
        return (
            self._signals_for_coin(symbol, x_signals)
            + self._signals_for_coin(symbol, cmc_signals)
            + self._signals_for_coin(symbol, lc_signals)
        )

    def _consensus_multiplier(self, coin_signals: list) -> float:
        actionable = [s for s in coin_signals if getattr(s, "action", "HOLD") in ("BUY", "SELL")]
        sources = {getattr(s, "source", "x") for s in actionable}
        multiplier = 1.0
        if len(actionable) >= 3:
            multiplier = 1.25
        elif len(actionable) >= 2:
            multiplier = 1.1
        if "x" in sources and "cmc" in sources:
            multiplier *= 1.15
        if "lc" in sources and "cmc" in sources:
            multiplier *= 1.12
        if "lc" in sources and "x" in sources:
            multiplier *= 1.10
        return multiplier

    def _x_buy_threshold(self, x_signal) -> float:
        trust = getattr(x_signal, "trust_score", 70)
        return max(65.0, 85 - (trust - 70) * 0.5)

    def _cmc_buy_threshold(self, strategy_params: dict = None) -> float:
        params = strategy_params or {}
        if params.get("cmc_min_confidence") is not None:
            return float(params["cmc_min_confidence"])
        if is_dry_run_enhanced():
            return float(self.config.dry_run_defaults.get("cmc_min_confidence", 55))
        return float(self.config.cmc_config.get("min_confidence", 60))

    def _cmc_sell_threshold(self, strategy_params: dict = None, cmc_signal=None) -> float:
        params = strategy_params or {}
        if params.get("cmc_sell_min_confidence") is not None:
            base = float(params["cmc_sell_min_confidence"])
        elif is_dry_run_enhanced():
            base = float(self.config.dry_run_defaults.get("cmc_sell_min_confidence", 65))
        else:
            base = float(self.config.cmc_config.get("sell_min_confidence", 70))
        if cmc_signal and getattr(cmc_signal, "quotes_fallback", False):
            bonus = float(self.config.cmc_config.get("quotes_fallback_sell_threshold_bonus", 10))
            base += bonus
        return base

    def _cmc_sell_requires_ta(self, strategy_params: dict = None) -> bool:
        params = strategy_params or {}
        if "cmc_sell_requires_ta" in params:
            return bool(params["cmc_sell_requires_ta"])
        if is_dry_run_enhanced():
            return bool(self.config.dry_run_defaults.get("cmc_sell_requires_ta", True))
        return bool(self.config.cmc_config.get("sell_requires_ta", True))

    def _cmc_trust_score(self, cmc_signal, strategy_params: dict = None) -> float:
        params = strategy_params or {}
        if params.get("cmc_trust_score") is not None:
            return float(params["cmc_trust_score"])
        return float(getattr(cmc_signal, "trust_score", 65.0))

    def _lc_buy_threshold(self, strategy_params: dict = None) -> float:
        params = strategy_params or {}
        if params.get("lc_min_confidence") is not None:
            return float(params["lc_min_confidence"])
        if is_dry_run_enhanced():
            return float(self.config.dry_run_defaults.get("lc_min_confidence", 52))
        return float(self.config.lunarcrush_config.get("min_confidence", 58))

    def _lc_sell_threshold(self, strategy_params: dict = None) -> float:
        params = strategy_params or {}
        if params.get("lc_sell_min_confidence") is not None:
            return float(params["lc_sell_min_confidence"])
        if is_dry_run_enhanced():
            return float(self.config.dry_run_defaults.get("lc_sell_min_confidence", 65))
        return float(self.config.lunarcrush_config.get("sell_min_confidence", 68))

    def _lc_sell_requires_ta(self, strategy_params: dict = None) -> bool:
        params = strategy_params or {}
        if "lc_sell_requires_ta" in params:
            return bool(params["lc_sell_requires_ta"])
        return bool(self.config.lunarcrush_config.get("sell_requires_ta", True))

    def _lc_trust_score(self, lc_signal, strategy_params: dict = None) -> float:
        params = strategy_params or {}
        if params.get("lc_trust_score") is not None:
            return float(params["lc_trust_score"])
        return float(getattr(lc_signal, "trust_score", self.config.lunarcrush_config.get("trust_score", 72)))

    def _weighted_social_confidence(self, x_eff: float, cmc_eff: float, lc_eff: float = 0.0) -> float:
        x_w = self.config.x_weight
        c_w = self.config.onchain_weight
        l_w = self.config.lc_weight
        total = 0.0
        weight_sum = 0.0
        if x_eff > 0:
            total += x_eff * x_w
            weight_sum += x_w
        if cmc_eff > 0:
            total += cmc_eff * c_w
            weight_sum += c_w
        if lc_eff > 0:
            total += lc_eff * l_w
            weight_sum += l_w
        if weight_sum <= 0:
            return 0.0
        return total / weight_sum * (x_w + c_w + l_w)

    def _social_buy_count(self, x_buy: bool, cmc_buy: bool, lc_buy: bool) -> int:
        return sum((x_buy, cmc_buy, lc_buy))

    def _merge_buy(
        self,
        technical: SignalAnalysis,
        x_signal,
        cmc_signal,
        coin_signals: list,
        market: MarketContext,
        lc_signal=None,
    ) -> tuple:
        sources = list(technical.sources)
        x_buy = False
        cmc_buy = False
        lc_buy = False
        x_eff = 0.0
        cmc_eff = 0.0
        lc_eff = 0.0
        tech_buy = normalize(technical.action) in (BUY, BUY_STRONG) or technical.action == "BUY"
        consensus = self._consensus_multiplier(coin_signals)

        if x_signal and x_signal.action == "BUY":
            x_eff = getattr(x_signal, "effective_confidence", x_signal.confidence)
            x_eff *= consensus
            if x_eff >= self._x_buy_threshold(x_signal):
                x_buy = True
                sources.append("x")

        strategy_params = market.strategy_params or {}
        if cmc_signal and cmc_signal.action == "BUY":
            trust = self._cmc_trust_score(cmc_signal, strategy_params)
            cmc_eff = float(cmc_signal.confidence) * (trust / 100.0)
            cmc_eff *= consensus
            if cmc_eff >= self._cmc_buy_threshold(strategy_params):
                cmc_buy = True
                sources.append("cmc")

        if lc_signal and lc_signal.action == "BUY":
            trust = self._lc_trust_score(lc_signal, strategy_params)
            lc_eff = float(lc_signal.confidence) * (trust / 100.0)
            lc_eff *= consensus
            if lc_eff >= self._lc_buy_threshold(strategy_params):
                lc_buy = True
                sources.append("lc")

        social_count = self._social_buy_count(x_buy, cmc_buy, lc_buy)
        blended = self._weighted_social_confidence(
            x_eff if x_buy else 0,
            cmc_eff if cmc_buy else 0,
            lc_eff if lc_buy else 0,
        )
        boost = evaluate_market_structure_buy_boost(market, strategy_params, tech_buy, cmc_buy or lc_buy)
        if boost:
            sources.append(boost.source)

        if not market.has_position and market.open_positions < self.config.max_open_positions:
            if boost and (tech_buy or cmc_buy or lc_buy):
                sources.append("multi_source")
                return BUY_STRONG, sources, max(technical.confidence, blended)
            if tech_buy and social_count >= 2:
                sources.append("multi_source")
                return BUY_STRONG, sources, max(technical.confidence, blended)
            if tech_buy and social_count >= 1:
                if social_count >= 2:
                    sources.append("multi_source")
                    return BUY_STRONG, sources, max(technical.confidence, blended)
                return BUY, sources, max(technical.confidence, blended)
            if social_count >= 2:
                sources.append("multi_source")
                return BUY, sources, blended
            if tech_buy:
                return BUY, sources, technical.confidence
            if social_count >= 1:
                return BUY, sources, blended
        return HOLD, sources, technical.confidence

    def _x_stop_loss_triggered(self, x_signal, current_price: float) -> bool:
        stop = getattr(x_signal, "stop_loss", None) if x_signal else None
        return stop is not None and current_price > 0 and current_price <= float(stop)

    def _x_price_target_triggered(self, x_signal, current_price: float) -> bool:
        target = getattr(x_signal, "price_target", None) if x_signal else None
        if target is None or current_price <= 0:
            return False
        tolerance = float(self.config.raw.get("x_backtest", {}).get("target_tolerance_pct", 0.5))
        return current_price >= float(target) * (1 - tolerance / 100.0)

    def _merge_sell(
        self,
        technical: SignalAnalysis,
        x_signal,
        cmc_signal,
        coin_signals: list,
        market: MarketContext = None,
        position: dict = None,
        lc_signal=None,
    ) -> tuple:
        sources = list(technical.sources)
        candidates = []
        structure_rationales = []
        consensus = self._consensus_multiplier(coin_signals)

        tech_norm = normalize(technical.action)
        if is_sell(technical.action):
            pri = self.SELL_PRIORITY.get(tech_norm, 1)
            if "stop_loss" in technical.sources:
                pri = 7
            candidates.append((tech_norm, pri, "technical"))

        if x_signal and self._x_stop_loss_triggered(x_signal, market.current_price if market else 0):
            candidates.append((SELL_FULL, 6, "x_stop_loss"))
            sources.append("x_stop_loss")

        if x_signal and self._x_price_target_triggered(x_signal, market.current_price if market else 0):
            candidates.append((SELL_PARTIAL_30, 5, "x_take_profit"))
            sources.append("x_take_profit")

        if x_signal and x_signal.action == "SELL":
            eff = getattr(x_signal, "effective_confidence", x_signal.confidence) * consensus
            if eff >= 85:
                candidates.append((SELL_PARTIAL_30, 3, "x"))
                sources.append("x")
            elif eff >= 70:
                candidates.append((SELL_PARTIAL_20, 2, "x"))
                sources.append("x")

        strategy_params = (market.strategy_params or {}) if market else {}
        if cmc_signal and cmc_signal.action == "SELL":
            quotes_as_signal = bool(self.config.cmc_config.get("quotes_fallback_as_signal", False))
            if getattr(cmc_signal, "quotes_fallback", False) and not quotes_as_signal:
                pass
            else:
                trust = self._cmc_trust_score(cmc_signal, strategy_params)
                eff = float(cmc_signal.confidence) * (trust / 100.0) * consensus
                requires_ta = self._cmc_sell_requires_ta(strategy_params)
                ta_bearish = is_sell(technical.action)
                volatile_profile = strategy_params.get("strategy_profile") == "volatile_altcoin"
                if eff >= self._cmc_sell_threshold(strategy_params, cmc_signal):
                    if requires_ta and not ta_bearish:
                        pass
                    elif ta_bearish or volatile_profile:
                        candidates.append((SELL_PARTIAL_20, 2, "cmc"))
                        sources.append("cmc")
                    else:
                        candidates.append((SELL_PARTIAL_10, 1, "cmc"))
                        sources.append("cmc")

        if lc_signal and lc_signal.action == "SELL":
            trust = self._lc_trust_score(lc_signal, strategy_params)
            eff = float(lc_signal.confidence) * (trust / 100.0) * consensus
            requires_ta = self._lc_sell_requires_ta(strategy_params)
            ta_bearish = is_sell(technical.action)
            volatile_profile = strategy_params.get("strategy_profile") == "volatile_altcoin"
            if eff >= self._lc_sell_threshold(strategy_params):
                if requires_ta and not ta_bearish:
                    pass
                elif ta_bearish or volatile_profile:
                    candidates.append((SELL_PARTIAL_20, 2, "lc"))
                    sources.append("lc")
                else:
                    candidates.append((SELL_PARTIAL_10, 1, "lc"))
                    sources.append("lc")

        if market and position:
            for cand in evaluate_market_structure_sells(market, strategy_params, position):
                candidates.append((cand.action, cand.priority, cand.source))
                sources.append(cand.source)
                structure_rationales.append(cand.rationale)

            trail = evaluate_trailing_stop(market, position, strategy_params)
            if trail:
                candidates.append((trail.action, trail.priority, trail.source))
                sources.append(trail.source)
                structure_rationales.append(trail.rationale)
                if trail.shadow_only:
                    sources.append("trailing_shadow")

        if not candidates:
            return HOLD, sources, technical.confidence, structure_rationales

        best = max(candidates, key=lambda c: c[1])
        social_conf = 0.0
        if x_signal:
            social_conf = max(social_conf, getattr(x_signal, "effective_confidence", 0))
        if cmc_signal:
            social_conf = max(social_conf, getattr(cmc_signal, "effective_confidence", 0))
        if lc_signal:
            social_conf = max(social_conf, getattr(lc_signal, "effective_confidence", 0))
        return best[0], sources, max(technical.confidence, social_conf), structure_rationales

    def _apply_shadow_mode(
        self,
        normalized: str,
        execution_action: str,
        strategy_params: dict,
        sources: list | None = None,
    ) -> tuple:
        sources = sources or []
        if "trailing_shadow" in sources and is_sell(normalized):
            shadow = execution_action
            return HOLD, "HOLD", shadow

        profile = strategy_params.get("strategy_profile", "")
        if profile not in ("volatile_altcoin", "hermes_baseline+volatile"):
            return normalized, execution_action, ""
        mode = self.config.volatile_altcoin_config.get("mode", "shadow")
        if mode != "shadow":
            return normalized, execution_action, ""
        if normalized == HOLD:
            return normalized, execution_action, ""
        shadow = execution_action
        return HOLD, "HOLD", shadow

    def evaluate_with_market(
        self,
        coin: dict,
        market: MarketContext,
        x_signals=None,
        cmc_signals=None,
        lc_signals=None,
    ) -> SignalAnalysis:
        coin = resolve_coin_config(coin)
        return self._evaluate_internal(coin, market, x_signals, cmc_signals, lc_signals)

    def evaluate(self, coin: dict, current_price: float, x_signals=None, cmc_signals=None, lc_signals=None) -> SignalAnalysis:
        if not current_price:
            return None

        coin = resolve_coin_config(coin)
        market = self.build_market_context(coin, current_price)
        return self._evaluate_internal(coin, market, x_signals, cmc_signals, lc_signals)

    def _evaluate_internal(
        self,
        coin: dict,
        market: MarketContext,
        x_signals=None,
        cmc_signals=None,
        lc_signals=None,
    ) -> SignalAnalysis:
        coin = resolve_coin_config(coin)
        if not market.strategy_params:
            market.strategy_params = resolve_strategy_params(
                coin,
                has_position=market.has_position,
                atr_pct=market.atr_pct,
                frozen_tier=get_position(coin["symbol"], market.timeframe).get("strategy_tier"),
            )
        strategy = get_strategy({**coin, "strategy_params": market.strategy_params})
        technical = strategy.analyze(coin, market, x_signals=None)

        coin_x = self._signals_for_coin(coin["symbol"], x_signals)
        coin_cmc = self._signals_for_coin(coin["symbol"], cmc_signals)
        coin_lc = self._signals_for_coin(coin["symbol"], lc_signals)
        all_social = self._all_coin_social_signals(coin["symbol"], x_signals, cmc_signals, lc_signals)
        x_signal = coin_x[0] if coin_x else None
        cmc_signal = coin_cmc[0] if coin_cmc else None
        lc_signal = coin_lc[0] if coin_lc else None
        position = get_position(coin["symbol"], market.timeframe)
        structure_rationales = []

        dca_usdt = 0.0
        if market.has_position:
            normalized, sources, confidence, structure_rationales = self._merge_sell(
                technical, x_signal, cmc_signal, all_social, market, position, lc_signal
            )
            if normalized == HOLD:
                dca = evaluate_dca_addon(market, position, market.strategy_params)
                if dca:
                    normalized = BUY_DCA
                    sources.append(dca.source)
                    structure_rationales.append(dca.rationale)
                    dca_usdt = dca.usdt_amount
                    if dca.shadow_only:
                        sources.append("dca_shadow")
        else:
            normalized, sources, confidence = self._merge_buy(
                technical, x_signal, cmc_signal, all_social, market, lc_signal
            )
            if normalized == HOLD:
                tech_norm = normalize(technical.action)
                if is_buy(tech_norm):
                    normalized = tech_norm
                    sources = list(technical.sources)

        execution_action = to_execution_action(normalized)
        strategy_params = market.strategy_params or {}
        normalized, execution_action, shadow_action = self._apply_shadow_mode(
            normalized, execution_action, strategy_params, sources
        )
        if "dca_shadow" in sources and normalized == BUY_DCA:
            shadow_action = execution_action
            normalized = HOLD
            execution_action = "HOLD"

        rationale_parts = []
        if "technical" in sources:
            rationale_parts.append(f"TA->{technical.action}")
        rationale_parts.extend(structure_rationales)
        if "take_profit" in sources:
            rationale_parts.append("TA->take_profit")
        if "x_take_profit" in sources:
            rationale_parts.append("X->price_target hit")
        if "x_stop_loss" in sources:
            rationale_parts.append("X->stop_loss hit")
        if "x" in sources and x_signal:
            rationale_parts.append(f"X->{x_signal.action}@{x_signal.account}({x_signal.confidence}%)")
        if "cmc" in sources and cmc_signal:
            rationale_parts.append(f"CMC->{cmc_signal.action}({cmc_signal.confidence}%)")
        if "lc" in sources and lc_signal:
            rationale_parts.append(f"LC->{lc_signal.action}({lc_signal.confidence}%)")
        if "multi_source" in sources:
            social_tags = [t for t in ("x", "cmc", "lc") if t in sources]
            if social_tags:
                rationale_parts.append("+".join(s.upper() for s in social_tags) + " consensus")
            else:
                rationale_parts.append("multi-source consensus")
        if normalized == BUY_STRONG:
            rationale_parts.append("strong consensus")
        if "trailing_stop" in sources:
            rationale_parts.append("Trail->ATR stop")
        if "dca" in sources:
            rationale_parts.append("DCA->accumulation")
        if shadow_action:
            rationale_parts.append(f"shadow->{shadow_action}")

        social_conf = 0.0
        if x_signal:
            social_conf = max(social_conf, getattr(x_signal, "confidence", 0))
        if cmc_signal:
            social_conf = max(social_conf, getattr(cmc_signal, "confidence", 0))
        if lc_signal:
            social_conf = max(social_conf, getattr(lc_signal, "confidence", 0))

        profile = strategy_params.get("strategy_profile", "")
        tier = strategy_params.get("volatility_tier", "")

        analysis = SignalAnalysis(
            action=execution_action,
            symbol=technical.symbol,
            timeframe=technical.timeframe,
            rsi=technical.rsi,
            lower_bb=technical.lower_bb,
            vol_multiplier=technical.vol_multiplier,
            ampel_emoji=technical.ampel_emoji,
            ampel_text=technical.ampel_text,
            should_notify=technical.should_notify or execution_action != "HOLD" or bool(shadow_action),
            notify_reason=technical.notify_reason if execution_action == "HOLD" and not shadow_action else "Decision",
            x_confidence=social_conf,
            sources=sources,
            normalized_action=normalized,
            rationale=" | ".join(rationale_parts) or technical.notify_reason,
            confidence=confidence or social_conf,
            recommended=execution_action != "HOLD",
            upper_bb=market.upper_bb,
            middle_bb=market.middle_bb,
            atr_pct=market.atr_pct,
            volatility_tier=tier,
            strategy_profile=profile,
            shadow_action=shadow_action,
        )
        if dca_usdt > 0:
            analysis.dca_usdt = dca_usdt
        return analysis

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
            "price_target": getattr(x_signal, "price_target", None),
            "stop_loss": getattr(x_signal, "stop_loss", None),
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
        elif x_signal.coin not in [c["symbol"].split("/")[0] for c in load_effective_watchlist()]:
            recommendation["action"] = ADD_WATCHLIST
            recommendation["recommended"] = True

        return recommendation
