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
import time
import pandas as pd

import pandas as pd

from core.config import get_bot_config
from core.models import MarketContext, SignalAnalysis
from data_manager import is_dry_run_enhanced, load_effective_watchlist
from logger import log
from services.market_service import MarketService
from strategies.market_structure import (
    evaluate_market_structure_buy_boost,
    evaluate_market_structure_sells,
)
from strategies.dca import evaluate_dca_addon

from strategies.sell_rotation_policy import (
    apply_rotation_sell_filters,
    audit_to_dict,
    policy_shadow_active,
)
from strategies.trailing_stop import evaluate_trailing_stop
from strategies.trailing_take_profit import evaluate_trailing_take_profit
from strategies.time_profit_exit import evaluate_time_profit_exit
from strategies.profit_max_lifetime import evaluate_profit_max_lifetime, sync_profit_armed_at
from intelligence.regime_detector import RegimeDetector
from intelligence.strategy_allocator import StrategyAllocator
from intelligence.volatility_classifier import volatility_tier
from intelligence.regime_detector import RegimeDetector
from intelligence.strategy_allocator import StrategyAllocator
from strategies.positions import (
    count_open_full_slots,
    count_open_positions,
    flush_positions,
    get_position,
    lock_strategy_tier,
    mark_profit_max_lifetime_done,
    mark_trailing_take_profit_step,
    update_market_snapshot,
)
from strategies.registry import (
    get_strategy,
    resolve_coin_config,
    resolve_effective_timeframe,
    resolve_strategy_params,
)
from strategies.entry_sensor_15m import (
    ENTRY_SENSOR_SOURCE,
    consume_pending_sensor_metrics,
    evaluate_entry_sensor_15m,
)
from strategies import watch_15m_state
from strategies.entry_guard import filter_sell_candidates, is_fresh_guarded_entry
from strategies.exit_sensor import evaluate_exit_sensor_sells

_WATCHLIST_CACHE: tuple[float, frozenset[str]] | None = None
_WATCHLIST_TTL_SEC = 60.0


def _active_watchlist_symbols() -> frozenset[str]:
    global _WATCHLIST_CACHE
    now = time.time()
    if _WATCHLIST_CACHE and now - _WATCHLIST_CACHE[0] < _WATCHLIST_TTL_SEC:
        return _WATCHLIST_CACHE[1]
    symbols = frozenset(
        c.get("symbol")
        for c in load_effective_watchlist()
        if c.get("active", True) and c.get("symbol")
    )
    _WATCHLIST_CACHE = (now, symbols)
    return symbols


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
        self._tenant_regime_detector: RegimeDetector | None = None
        self._tenant_strategy_allocator: StrategyAllocator | None = None

    def begin_tenant_cycle(self) -> None:
        """Prepare per-tenant cycle caches and reusable regime collaborators."""
        global _WATCHLIST_CACHE
        _WATCHLIST_CACHE = None
        self.config.refresh()
        self.market.begin_cycle()
        raw = self.config.raw
        rd_cfg = raw.get("regime_detector") or {}
        alloc_cfg = raw.get("strategy_allocator") or {}
        self._tenant_regime_detector = (
            RegimeDetector(self.config.regime_detector_config)
            if rd_cfg.get("enabled", False)
            else None
        )
        self._tenant_strategy_allocator = (
            StrategyAllocator() if alloc_cfg.get("enabled", False) else None
        )
        if self._tenant_regime_detector is not None:
            for timeframe, limit in (("4h", 300), ("1h", 300), ("15m", 50)):
                self.market.prefetch_btc_ohlcv(timeframe, limit)
        try:
            from strategies.oracle_climax import begin_cycle

            begin_cycle(raw)
        except Exception as exc:
            log(f"oracle_climax begin_cycle: {exc}", "DEBUG")

    def _oracle_climax_state(self):
        from strategies.oracle_climax import ClimaxDecision, MODE_IDLE, current_climax, oracle_climax_config

        try:
            return current_climax(self.config.raw)
        except Exception as exc:
            log(f"oracle_climax resolve: {exc}", "DEBUG")
            return ClimaxDecision(MODE_IDLE, ("error",), {}), oracle_climax_config({})

    def _entry_sensor_cfg(self) -> dict:
        return self.config.entry_sensor_15m_config

    def _exit_sensor_cfg(self) -> dict:
        return self.config.exit_sensor_config

    def _in_setup_zone(self, market: MarketContext, strategy_params: dict) -> bool:
        modes = self._entry_sensor_cfg().get("setup_modes") or []
        if "setup_zone" not in modes:
            return False
        rsi_low = float(strategy_params.get("rsi_buy_low", 25))
        rsi_high = float(strategy_params.get("rsi_buy_high", 55))
        profile = str(strategy_params.get("strategy_profile", ""))
        tier = str(strategy_params.get("volatility_tier", market.strategy_params.get("volatility_tier", "")))
        volatile = tier == "volatile" or "volatile" in profile
        if not volatile:
            return False
        return rsi_low <= float(market.rsi) <= rsi_high

    def _sync_watch_15m_state(
        self,
        symbol: str,
        market: MarketContext,
        technical: SignalAnalysis,
        normalized: str,
        position: dict,
    ) -> None:
        cfg = self._entry_sensor_cfg()
        if not cfg.get("enabled", True):
            return
        from intelligence.strategy_backtest import classify_coin

        if classify_coin(symbol, market.strategy_params) == "large_cap":
            watch_15m_state.clear_watch(symbol)
            return
        if market.has_position:
            watch_15m_state.clear_watch(symbol)
            return
        if is_sell(normalized) or is_sell(technical.action):
            watch_15m_state.clear_watch(symbol)
            return

        tech_norm = normalize(technical.action)
        tech_buy = is_buy(tech_norm)
        setup = self._in_setup_zone(market, market.strategy_params or {})
        modes = cfg.get("setup_modes") or []
        trending = "trending" in modes and "cmc_trending" in (technical.sources or [])
        on_watchlist = "watchlist" in modes and symbol in _active_watchlist_symbols()
        should_watch = (
            (tech_buy and "buy_signal" in modes)
            or setup
            or trending
            or is_buy(normalized)
            or on_watchlist
        )
        if not should_watch or market.has_position:
            return
        if watch_15m_state.max_watched_reached(int(cfg.get("max_watched_coins", 15))):
            return
        from data.cmc_market_cap import passes_market_cap_filter, resolve_market_cap_usd
        from price_fetcher import passes_exchange_filter

        mcap = resolve_market_cap_usd(symbol, market.strategy_params or {})
        mcap_ok, _ = passes_market_cap_filter(mcap, cfg)
        if not mcap_ok:
            return
        ex_ok, _ = passes_exchange_filter(symbol, cfg)
        if not ex_ok:
            return
        reason = (
            "buy_signal"
            if tech_buy
            else "setup_zone"
            if setup
            else "trending"
            if trending
            else "watchlist"
        )
        watch_15m_state.set_watch(
            symbol,
            market.timeframe,
            reason=reason,
            ttl_hours=float(cfg.get("watch_ttl_hours", 24)),
            rsi_4h=float(market.rsi),
            tech_buy=tech_buy,
        )

    def _apply_entry_sensor_buy(
        self,
        normalized: str,
        sources: list,
        confidence: float,
        symbol: str,
        market: MarketContext,
        technical: SignalAnalysis,
    ) -> tuple:
        cfg = self._entry_sensor_cfg()
        if not cfg.get("enabled", True) or market.has_position:
            return normalized, sources, confidence, "", ""
        if is_sell(normalized) or normalized == BUY_DCA:
            return normalized, sources, confidence, "", ""

        if not watch_15m_state.is_watched(symbol):
            return normalized, sources, confidence, "", ""

        metrics = consume_pending_sensor_metrics(symbol)
        if metrics is None:
            metrics = self.market.fetch_15m_sensor_metrics(symbol, cfg)
        tech_norm = normalize(technical.action)
        from data.cmc_market_cap import resolve_market_cap_usd
        from price_fetcher import is_gate_tradeable, is_listed_on_exchange
        from core.config import get_bot_config

        ex = get_bot_config().exchange
        exchange_tradeable = None
        if cfg.get("gate_only", cfg.get("exchange_only", True)):
            if (ex or "gate").lower() == "gate":
                exchange_tradeable = is_gate_tradeable(symbol)
            else:
                exchange_tradeable = is_listed_on_exchange(symbol, ex)

        venue_ok = None
        venue_reason = None
        try:
            from services.venue_quality import check_venue_for_buy, venue_quality_config

            vcfg = venue_quality_config(self.config.raw)
            if vcfg.get("enabled", True):
                planned = float(get_bot_config().max_usdt_per_trade or 500) * 0.35
                vres = check_venue_for_buy(
                    symbol,
                    source=ENTRY_SENSOR_SOURCE,
                    planned_usdt=planned,
                    config_raw=self.config.raw,
                )
                venue_ok = bool(vres.ok)
                if not vres.ok:
                    venue_reason = "; ".join(vres.reasons) or "venue_quality_block"
        except Exception:
            venue_ok = None

        # Memory: coin facts + profile (fail-open → empty / neutral)
        memory_flags = None
        memory_entry_bias = "neutral"
        try:
            from intelligence.memory.cache import get_entry_bias
            from intelligence.memory.coin_facts import summarize_facts_for_symbol

            memory_entry_bias = get_entry_bias(symbol) or "neutral"
            if cfg.get("memory_enabled", True):
                memory_flags = summarize_facts_for_symbol(
                    symbol, config_raw=self.config.raw
                )
        except Exception:
            memory_flags = None
            memory_entry_bias = "neutral"

        sensor = evaluate_entry_sensor_15m(
            watched=True,
            metrics=metrics,
            cfg=cfg,
            rsi_4h=float(market.rsi),
            hours_since_reject=watch_15m_state.hours_since_sensor_reject(symbol),
            tech_already_buy=is_buy(tech_norm),
            market_cap_usd=resolve_market_cap_usd(symbol, market.strategy_params or {}),
            gate_tradeable=exchange_tradeable,
            venue_ok=venue_ok,
            venue_reason=venue_reason,
            memory_flags=memory_flags,
            memory_entry_bias=memory_entry_bias,
        )

        if sensor is None or not sensor.triggered:
            return normalized, sources, confidence, "", ""

        out_sources = list(sources)
        out_sources.append(ENTRY_SENSOR_SOURCE)
        new_conf = confidence + sensor.confidence_boost
        rationale_extra = sensor.rationale

        if sensor.shadow_only:
            out_sources.append("entry_sensor_shadow")
            if normalized == HOLD:
                return HOLD, out_sources, new_conf, rationale_extra, sensor.action
            return normalized, out_sources, new_conf, rationale_extra, sensor.action

        # Mode-aware size + hold_override (sensor-entry-guard M1)
        trading_mode = str((market.strategy_params or {}).get("trading_mode") or "")
        vol_tier = str(
            (market.strategy_params or {}).get("volatility_tier")
            or getattr(technical, "volatility_tier", "")
            or ""
        )
        from strategies.sensor_entry_policy import (
            apply_sensor_hold_policy,
            resolve_sensor_usdt,
        )
        from strategies.trading_modes import MODE_DEFENSIVE, MODE_GRID, MODE_HYBRID, MODE_MOMENTUM

        if trading_mode == MODE_DEFENSIVE:
            return normalized, sources, confidence, "sensor off in DEFENSIVE", ""

        action, block_reason = apply_sensor_hold_policy(
            tech_normalized=normalized,
            trading_mode=trading_mode,
            cfg=cfg,
            sensor_action=sensor.action,
            tech_already_buy=is_buy(tech_norm),
        )
        if action is None:
            return normalized, sources, confidence, block_reason, ""

        try:
            base = float(get_bot_config().max_usdt_per_trade or 500)
            usdt = resolve_sensor_usdt(
                trading_mode,
                volatility_tier=vol_tier,
                max_usdt_per_trade=base,
                cfg=cfg,
            )
            try:
                sm = float(getattr(sensor, "size_mult", 1.0) or 1.0)
            except (TypeError, ValueError):
                sm = 1.0
            if sm < 0.999:
                usdt = round(usdt * max(0.25, min(1.0, sm)), 2)
            if usdt > 0:
                technical.dca_usdt = usdt
        except Exception:
            pass

        if trading_mode == MODE_GRID:
            out_sources.append("grid_slice")
        elif trading_mode == MODE_HYBRID:
            out_sources.append("hybrid_slice")
        elif trading_mode == MODE_MOMENTUM or not trading_mode:
            out_sources.append("sensor_size_cap")
        if getattr(technical, "dca_usdt", 0):
            rationale_extra = (
                f"{rationale_extra} | sensor_usdt={getattr(technical, 'dca_usdt', 0)}"
            ).strip(" |")

        if normalized == HOLD or is_buy(normalized):
            return BUY, out_sources, new_conf, rationale_extra, ""
        if is_buy(normalized) and sensor.action == BUY_STRONG:
            return BUY, out_sources, new_conf, rationale_extra, ""
        return normalized, out_sources, new_conf, rationale_extra, ""

    def _build_social_context_for_regime(self, symbol: str, x_signals, cmc_signals, lc_signals) -> dict:
        ctx = {}
        for signals, key in [(x_signals, "x"), (cmc_signals, "cmc"), (lc_signals, "lc")]:
            coin_signals = self._signals_for_coin(symbol, signals) if signals else []
            if coin_signals:
                s = coin_signals[0]
                if hasattr(s, "sentiment"):
                    ctx[f"{key}_sentiment"] = getattr(s, "sentiment", 50)
                    # RegimeDetector expects lunarcrush_sentiment key
                    if key == "lc":
                        ctx["lunarcrush_sentiment"] = getattr(s, "sentiment", 50)
                if hasattr(s, "confidence"):
                    ctx[f"{key}_confidence"] = getattr(s, "confidence", 50)
                    if key == "x":
                        ctx["x_confidence"] = getattr(s, "confidence", 50)
                        # map conf to soft x_sentiment when no explicit field
                        if "x_sentiment" not in ctx:
                            ctx["x_sentiment"] = (float(getattr(s, "confidence", 50)) - 50) / 50.0
        # P1/P3: global Santiment sidecar → soft sentiment for RegimeDetector
        try:
            from services.market_policy_fusion import get_global_market_bias, inject_global_sentiment
            from services.santiment_policy import santiment_risk_config

            if santiment_risk_config(self.config.raw).get("inject_regime_sentiment", True):
                bias = get_global_market_bias(self.config.raw)
                ctx = inject_global_sentiment(ctx, bias)
        except Exception:
            pass
        return ctx

    def _ohlcv_limit_for_decision(self) -> int:
        """PR-P2c: when regime is on, fetch enough bars once for indicators + detector."""
        if (self.config.raw.get("regime_detector") or {}).get("enabled", False):
            return 300
        return 100

    def build_market_context(self, coin: dict, current_price: float) -> MarketContext:
        symbol = coin.get("symbol", "")
        watchlist_tf = coin.get("timeframe", "4h")
        tf = resolve_effective_timeframe(coin)
        from intelligence.strategy_backtest import classify_coin

        ohlcv_limit = self._ohlcv_limit_for_decision()
        ohlcv_df = None

        if tf == watchlist_tf and classify_coin(symbol, coin.get("strategy_params")) != "large_cap":
            peek_df, peek = self.market.fetch_ohlcv_and_indicators(
                symbol, "4h", current_price, limit=ohlcv_limit,
            )
            atr_peek = float(peek.get("atr_pct", 3.0))
            tf_refined = resolve_effective_timeframe(
                coin,
                atr_pct=atr_peek,
                range_24h_pct=peek.get("range_24h_pct"),
                change_24h_pct=peek.get("change_24h_pct"),
            )
            if tf_refined != tf:
                tf = tf_refined
                ohlcv_df, indicators = self.market.fetch_ohlcv_and_indicators(
                    symbol, tf, current_price, limit=ohlcv_limit,
                )
                atr_pct = float(indicators.get("atr_pct", 3.0))
            else:
                indicators = peek
                atr_pct = atr_peek
                ohlcv_df = peek_df
        else:
            ohlcv_df, indicators = self.market.fetch_ohlcv_and_indicators(
                symbol, tf, current_price, limit=ohlcv_limit,
            )
            atr_pct = float(indicators.get("atr_pct", 3.0))

        range_24h_pct = indicators.get("range_24h_pct")
        change_24h_pct = indicators.get("change_24h_pct")

        coin = resolve_coin_config({**coin, "timeframe": tf})
        symbol = coin["symbol"]
        pos = get_position(symbol, tf)
        has_position = float(pos["amount"]) > 0
        if not has_position:
            # Defense: lot may sit on another TF than resolve_effective_timeframe
            # returned (e.g. store race / legacy key). Bind analysis to the real lot.
            from strategies.positions import find_open_position_for_symbol

            found = find_open_position_for_symbol(symbol, preferred_timeframe=tf)
            if found:
                tf, pos = found
                has_position = True
                coin = resolve_coin_config({**coin, "timeframe": tf})
        frozen = pos.get("strategy_tier") if has_position else None
        va_cfg = self.config.volatile_altcoin_config
        if has_position and not frozen and va_cfg.get("freeze_tier_on_entry", True):
            tier = volatility_tier(
                coin, atr_pct, va_cfg,
                range_24h_pct=range_24h_pct,
                change_24h_pct=change_24h_pct,
            )
            lock_strategy_tier(symbol, tf, tier)
            frozen = tier
            pos = get_position(symbol, tf)
        params = resolve_strategy_params(
            coin,
            has_position=has_position,
            atr_pct=atr_pct,
            frozen_tier=frozen,
            range_24h_pct=range_24h_pct,
            change_24h_pct=change_24h_pct,
        )
        funding_rate_pct = None
        btc_underperf_ratio = None
        if has_position:
            update_market_snapshot(symbol, tf, current_price, atr_pct)
            pos = get_position(symbol, tf)
            dca_cfg = dict((params or {}).get("dca") or {})
            scoring_cfg = dict(dca_cfg.get("scoring") or {})
            if dca_cfg.get("enabled") and scoring_cfg.get("enabled"):
                funding_rate_pct = self.market.fetch_funding_rate(symbol)
                lookback = float(scoring_cfg.get("btc_lookback_hours", 8))
                btc_underperf_ratio = self.market.btc_underperformance_ratio(
                    symbol, tf, lookback_hours=lookback
                )
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
            funding_rate_pct=funding_rate_pct,
            btc_underperf_ratio=btc_underperf_ratio,
            has_position=has_position,
            average_entry=pos.get("average_entry", 0),
            open_positions=count_open_positions(),
            strategy_params=params,
            ohlcv_df=ohlcv_df,
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

    def _cmc_buy_threshold(self, strategy_params: dict = None, cmc_signal=None) -> float:
        fusion = self.config.cmc_trending_fusion_config
        if (
            fusion.get("enabled")
            and cmc_signal
            and getattr(cmc_signal, "signal_tier", "") == "trending"
        ):
            return float(fusion.get("min_confidence_trending", 50))
        params = strategy_params or {}
        if params.get("cmc_min_confidence") is not None:
            return float(params["cmc_min_confidence"])
        if is_dry_run_enhanced():
            return float(self.config.dry_run_defaults.get("cmc_min_confidence", 55))
        return float(self.config.cmc_config.get("min_confidence", 60))

    def _cmc_trending_only_buy(self, cmc_signal, market: MarketContext, strategy_params: dict) -> bool:
        fusion = self.config.cmc_trending_fusion_config
        if not fusion.get("enabled") or not cmc_signal or cmc_signal.action != "BUY":
            return False
        if getattr(cmc_signal, "signal_tier", "") != "trending":
            return False
        if fusion.get("require_volatile_atr_tier", True):
            profile = strategy_params.get("strategy_profile", "")
            tier = strategy_params.get("volatility_tier", "")
            if tier != "volatile" and profile not in ("volatile_altcoin", "hermes_baseline+volatile"):
                return False
        rsi_cap = float(fusion.get("block_buy_if_rsi_above", 68))
        if market.rsi and market.rsi > rsi_cap:
            return False
        top_n = int(fusion.get("allow_cmc_only_buy_top_n", 8))
        rank = int(getattr(cmc_signal, "trending_rank", 0) or 0)
        if rank <= 0 or rank > top_n:
            return False
        min_conf = float(fusion.get("cmc_only_buy_min_confidence", 58))
        if float(cmc_signal.confidence) < min_conf:
            return False
        # W5: optional WQE gate for CMC-only (soft/enforce)
        try:
            from services.watchlist_quality.universe import cmc_only_buy_allowed

            sym = getattr(cmc_signal, "symbol", None) or strategy_params.get("symbol")
            if sym:
                ok, reason = cmc_only_buy_allowed(
                    str(sym),
                    trending_rank=rank,
                    config=getattr(self.config, "raw", None),
                )
                if not ok:
                    log(
                        f"WQE blocked CMC-only buy {sym}: {reason}",
                        "INFO",
                    )
                    return False
        except Exception:
            pass
        return True

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
        coin: dict | None = None,
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
            if cmc_eff >= self._cmc_buy_threshold(strategy_params, cmc_signal):
                cmc_buy = True
                sources.append("cmc")
            elif self._cmc_trending_only_buy(cmc_signal, market, strategy_params):
                cmc_buy = True
                sources.append("cmc")
                sources.append("cmc_trending")

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
            coin_source = (coin or {}).get("source") or ""
            if coin_source in ("cmc_trending", "dry_run_expansion"):
                from price_fetcher import passes_exchange_filter
                sensor_cfg = self._entry_sensor_cfg()
                ex = get_bot_config().exchange
                ex_ok, _ = passes_exchange_filter(
                    (coin or {}).get("symbol", market.symbol),
                    sensor_cfg,
                    exchange=ex,
                )
                if not ex_ok:
                    return HOLD, sources, technical.confidence

            if boost and (tech_buy or cmc_buy or lc_buy):
                sources.append("multi_source")
                return BUY_STRONG, sources, max(technical.confidence, blended)
            if tech_buy and social_count >= 2:
                sources.append("multi_source")
                return BUY_STRONG, sources, max(technical.confidence, blended)
            if tech_buy and social_count >= 1:
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
        sell_source = ""
        sell_policy_audit = {}
        consensus = self._consensus_multiplier(coin_signals)

        tech_norm = normalize(technical.action)
        if is_sell(technical.action):
            pri = self.SELL_PRIORITY.get(tech_norm, 1)
            # Label hard/partial stop distinctly so recovery_hold can allow full SL only
            tech_src = "technical"
            if "stop_loss" in (technical.sources or []):
                pri = 7
                act_u = str(technical.action or "").upper()
                if "PARTIAL" in act_u or tech_norm in (
                    SELL_PARTIAL_10,
                    SELL_PARTIAL_20,
                    SELL_PARTIAL_30,
                    SELL_PARTIAL_50,
                ):
                    tech_src = "partial_stop"
                else:
                    tech_src = "stop_loss"
            skip_partial = False
            if tech_src == "partial_stop":
                try:
                    from strategies.dca import should_pause_partial_stop

                    skip_partial = should_pause_partial_stop(
                        position,
                        getattr(market, "strategy_params", None) if market else None,
                    )
                except Exception:
                    skip_partial = False
            if not skip_partial:
                candidates.append((tech_norm, pri, tech_src))
            elif position:
                structure_rationales.append(
                    "partial_stop paused (DCA rounds still open)"
                )

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

        climax_dec = None
        climax_cfg = None
        if market and position:
            climax_dec, climax_cfg = self._oracle_climax_state()
            ta_bearish = is_sell(technical.action)
            for cand in evaluate_market_structure_sells(
                market, strategy_params, position, ta_bearish=ta_bearish,
            ):
                candidates.append((cand.action, cand.priority, cand.source))
                sources.append(cand.source)
                structure_rationales.append(cand.rationale)

            escfg = self._exit_sensor_cfg()
            if escfg.get("enabled", True):
                try:
                    metrics_15m = self.market.fetch_exit_metrics_15m(market.symbol, escfg)
                    metrics_1h = self.market.fetch_exit_metrics_1h(market.symbol)
                    bcfg = escfg.get("btc_rs") or {}
                    btc_delta = None
                    if bcfg.get("enabled", True):
                        btc_delta = self.market.btc_relative_return_delta(
                            market.symbol,
                            timeframe=str(bcfg.get("timeframe", "4h")),
                            periods=int(bcfg.get("periods", 1)),
                        )
                    for cand in evaluate_exit_sensor_sells(
                        market,
                        position,
                        escfg,
                        metrics_15m=metrics_15m,
                        metrics_1h=metrics_1h,
                        btc_rs_delta=btc_delta,
                    ):
                        candidates.append((cand.action, cand.priority, cand.source))
                        sources.append(cand.source)
                        structure_rationales.append(cand.rationale)
                        if cand.shadow_only:
                            sources.append("exit_sensor_shadow")
                except Exception as exc:
                    log(f"exit_sensor: metrics failed {market.symbol}: {exc}", "WARNING")

            if sync_profit_armed_at(market, position, strategy_params):
                flush_positions()

            trail_tp = evaluate_trailing_take_profit(
                market,
                position,
                strategy_params,
                climax_decision=climax_dec,
                config_raw=self.config.raw,
            )
            if trail_tp:
                candidates.append((trail_tp.action, trail_tp.priority, trail_tp.source))
                sources.append(trail_tp.source)
                structure_rationales.append(trail_tp.rationale)
                if trail_tp.shadow_only:
                    sources.append("trailing_take_profit_shadow")

            life = evaluate_profit_max_lifetime(market, position, strategy_params)
            if life:
                candidates.append((life.action, life.priority, life.source))
                sources.append(life.source)
                structure_rationales.append(life.rationale)
                if life.shadow_only:
                    sources.append("profit_max_lifetime_shadow")

            trail = evaluate_trailing_stop(market, position, strategy_params)
            if trail:
                candidates.append((trail.action, trail.priority, trail.source))
                sources.append(trail.source)
                structure_rationales.append(trail.rationale)
                if trail.shadow_only:
                    sources.append("trailing_shadow")

            tpe = evaluate_time_profit_exit(market, position, strategy_params)
            if tpe:
                candidates.append((tpe.action, tpe.priority, tpe.source))
                sources.append(tpe.source)
                structure_rationales.append(tpe.rationale)
                if tpe.shadow_only:
                    sources.append("time_profit_shadow")

        if market and position and climax_dec is not None:
            try:
                from strategies.oracle_climax import (
                    HARVEST_SOURCE,
                    MODE_GRIND,
                    MODE_HARVEST,
                    filter_grind_candidates,
                    harvest_candidate,
                    position_blocked_from_harvest,
                )

                if climax_dec.mode == MODE_GRIND and candidates:
                    candidates, blocked = filter_grind_candidates(candidates, climax_dec)
                    if blocked:
                        structure_rationales.append(
                            "oracle_climax grind blocked: "
                            + ", ".join(sorted(set(blocked)))
                        )
                if climax_dec.mode == MODE_HARVEST:
                    gain_pct = (
                        (market.current_price / market.average_entry - 1) * 100
                        if market.average_entry > 0
                        else 0.0
                    )
                    locked = position_blocked_from_harvest(position, self.config.raw)
                    extra = harvest_candidate(
                        gain_pct=gain_pct,
                        decision=climax_dec,
                        cfg=climax_cfg or {},
                        locked=locked,
                    )
                    if extra:
                        candidates.append(extra)
                        sources.append(HARVEST_SOURCE)
                        structure_rationales.append(
                            f"oracle_climax harvest gain={gain_pct:.1f}%"
                        )
            except Exception as exc:
                log(f"oracle_climax overlay failed: {exc}", "DEBUG")

        if market and position:
            try:
                open_full_slots = count_open_full_slots(self.config.raw)
            except Exception:
                open_full_slots = 0
            try:
                eff_cap = int(self.config.max_open_positions)
            except Exception:
                eff_cap = 0
        else:
            open_full_slots = 0
            eff_cap = 0

        if market and position and candidates:
            candidates, policy_audit = apply_rotation_sell_filters(
                candidates,
                market,
                position,
                strategy_params,
                self.config.raw,
                strategy_profile=getattr(technical, "strategy_profile", None),
                sell_sources=sources,
                open_full_slots=open_full_slots,
                eff_cap=eff_cap,
            )
            sell_policy_audit = audit_to_dict(policy_audit)
            if policy_audit.trail_exclusive_blocked:
                structure_rationales.append(
                    "Trail-exclusive blocked: " + ", ".join(policy_audit.trail_exclusive_blocked)
                )
        elif market and position:
            # Stagnant must be able to originate a sell with no other candidates.
            # Do not run the full extras loop here — that would also let
            # already-on tail_idle/ladder_terminal fire standalone.
            try:
                from strategies.sell_rotation_policy import (
                    evaluate_stagnant_rotation_close,
                    rotation_config,
                )

                rot_cfg = rotation_config(self.config.raw, strategy_params)
                extra = evaluate_stagnant_rotation_close(
                    market,
                    position,
                    rot_cfg,
                    open_full_slots=open_full_slots,
                    eff_cap=eff_cap,
                )
                if extra:
                    candidates.append((extra.action, extra.priority, extra.source))
                    sources.append(extra.source)
                    structure_rationales.append(extra.rationale)
            except Exception:
                pass

        # Recovery hold (#223): drop trail/TTP/partial/BB/social while focus recovering
        if market and position and candidates:
            try:
                from strategies.recovery_hold import (
                    filter_sell_candidates_for_recovery_hold,
                    maybe_promote_recovery_hold,
                )

                if maybe_promote_recovery_hold(
                    position,
                    float(market.current_price or 0),
                    strategy_params=strategy_params,
                    config_raw=self.config.raw,
                ):
                    try:
                        flush_positions()
                    except Exception:
                        pass
                    structure_rationales.append("recovery_hold cleared BE+")
                candidates, rh_blocked = filter_sell_candidates_for_recovery_hold(
                    candidates,
                    position,
                    strategy_params=strategy_params,
                    config_raw=self.config.raw,
                )
                if rh_blocked:
                    sources.append("recovery_hold_block")
                    structure_rationales.append(
                        "recovery_hold blocked: " + ", ".join(sorted(set(rh_blocked)))
                    )
            except Exception as exc:
                log(f"recovery_hold filter failed: {exc}", "DEBUG")

        if not candidates:
            return HOLD, sources, technical.confidence, structure_rationales, sell_source, sell_policy_audit

        if market and position:
            gain_pct = (
                (market.current_price / market.average_entry - 1) * 100
                if market.average_entry > 0 else 0.0
            )
            metrics_15m = None
            if is_fresh_guarded_entry(position):
                try:
                    metrics_15m = self.market.fetch_15m_sensor_metrics(
                        market.symbol, self._entry_sensor_cfg(),
                    )
                except Exception as exc:
                    log(
                        f"entry_guard: 15m metrics fetch failed {market.symbol}: {exc}",
                        "WARNING",
                    )
                    metrics_15m = None
                if not metrics_15m:
                    stored_ratio = float(position.get("entry_15m_vol_ratio") or 0)
                    if stored_ratio > 0:
                        stored_momentum = position.get("entry_15m_momentum")
                        metrics_15m = {
                            "volume_spike_ratio": stored_ratio,
                            "price_momentum": (
                                bool(stored_momentum)
                                if stored_momentum is not None
                                else False
                            ),
                        }
            candidates, blocked = filter_sell_candidates(
                candidates,
                position=position,
                strategy_params=strategy_params,
                gain_pct=gain_pct,
                ta_bearish=is_sell(technical.action),
                metrics_15m=metrics_15m,
            )
            structure_rationales.extend(blocked)

        if not candidates:
            return HOLD, sources, technical.confidence, structure_rationales, sell_source, sell_policy_audit

        best = max(candidates, key=lambda c: c[1])
        sell_source = best[2]
        social_conf = 0.0
        if x_signal:
            social_conf = max(social_conf, getattr(x_signal, "effective_confidence", 0))
        if cmc_signal:
            social_conf = max(social_conf, getattr(cmc_signal, "effective_confidence", 0))
        if lc_signal:
            social_conf = max(social_conf, getattr(lc_signal, "effective_confidence", 0))
        return best[0], sources, max(technical.confidence, social_conf), structure_rationales, sell_source, sell_policy_audit

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
        if "trailing_take_profit_shadow" in sources and is_sell(normalized):
            shadow = execution_action
            return HOLD, "HOLD", shadow
        if "profit_max_lifetime_shadow" in sources and is_sell(normalized):
            shadow = execution_action
            return HOLD, "HOLD", shadow
        if "time_profit_shadow" in sources and is_sell(normalized):
            shadow = execution_action
            return HOLD, "HOLD", shadow
        if "exit_sensor_shadow" in sources and is_sell(normalized):
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

        regime_result = None
        allocation = None
        try:
            if (self.config.raw.get("regime_detector") or {}).get("enabled", False):
                # PR-P2c: reuse frame from build_market_context (same limit=300 when regime on)
                ohlcv_df = getattr(market, "ohlcv_df", None)
                if ohlcv_df is None or (hasattr(ohlcv_df, "empty") and ohlcv_df.empty):
                    ohlcv_df = self.market.fetch_ohlcv(
                        coin["symbol"], market.timeframe, limit=300,
                    )
                if ohlcv_df is None:
                    ohlcv_df = pd.DataFrame()
                detector = self._tenant_regime_detector
                if detector is None:
                    detector = RegimeDetector(self.config.regime_detector_config)
                    self._tenant_regime_detector = detector
                regime_result = detector.detect(
                    coin=coin,
                    ohlcv_df=ohlcv_df,
                    current_price=market.current_price,
                    atr_pct=market.atr_pct,
                    social_context=self._build_social_context_for_regime(
                        coin["symbol"], x_signals, cmc_signals, lc_signals
                    ),
                )
                allocator = self._tenant_strategy_allocator
                if allocator is None and (self.config.raw.get("strategy_allocator") or {}).get("enabled", False):
                    allocator = StrategyAllocator()
                    self._tenant_strategy_allocator = allocator
                if allocator is not None:
                    allocation = allocator.allocate(
                        regime_result=regime_result,
                        coin=coin,
                        has_position=market.has_position,
                    )
        except Exception as e:
            log(f"[Regime] Detector/Allocator failed for {coin.get('symbol')}: {e}", "WARNING")

        if regime_result:
            market.regime = regime_result
        if allocation:
            market.allocation = allocation

        if regime_result:
            log(
                f"[Regime] {coin.get('symbol', '?')} regime={regime_result.primary_regime} "
                f"conf={regime_result.confidence} sent={regime_result.sentiment_score:.2f} "
                f"tier={regime_result.volatility_tier}",
                "INFO",
            )
            if allocation:
                log(
                    f"[Regime] allocation weights={allocation.strategy_weights} "
                    f"exposure_mult={allocation.exposure_multiplier} rationale={allocation.rationale}",
                    "INFO",
                )

        if allocation:
            market.strategy_params.setdefault("allocation", {
                "strategy_weights": allocation.strategy_weights,
                "exposure_multiplier": allocation.exposure_multiplier,
            })
            if allocation.defensive_mode:
                market.strategy_params["regime_defensive"] = True
                market.strategy_params["exposure_multiplier"] = allocation.exposure_multiplier
            try:
                from intelligence.strategy_backtest import classify_coin
                from strategies.trading_modes import resolve_trading_mode

                force_g = (
                    market.strategy_params.get("strategy_class") == "grid"
                    or coin.get("strategy_class") == "grid"
                )
                vol_tier = str(
                    market.strategy_params.get("volatility_tier")
                    or getattr(market, "volatility_tier", "")
                    or ""
                )
                cclass = classify_coin(coin.get("symbol", ""), market.strategy_params)
                mode = resolve_trading_mode(
                    allocation,
                    force_grid=force_g,
                    volatility_tier=vol_tier,
                    coin_class=cclass,
                )
                # P1: global Santiment mode bias (never forces DEFENSIVE inventory dump)
                try:
                    from services.market_policy_fusion import (
                        apply_global_mode_bias,
                        get_global_market_bias,
                    )
                    from services.santiment_policy import santiment_risk_config

                    if santiment_risk_config(self.config.raw).get("apply_mode_bias", True):
                        bias = get_global_market_bias(self.config.raw)
                        mode = apply_global_mode_bias(mode, bias, force_grid=force_g)
                        if bias.get("active"):
                            market.strategy_params["santiment_regime"] = bias.get("regime")
                            market.strategy_params["global_market_bias"] = {
                                "regime": bias.get("regime"),
                                "sentiment": bias.get("sentiment"),
                                "grid_spacing_mult": bias.get("grid_spacing_mult"),
                            }
                except Exception:
                    pass
                market.strategy_params["trading_mode"] = mode
                market.strategy_params["coin_class"] = cclass
            except Exception:
                pass

        # resolve_strategy_params returns a fresh dict — preserve cycle mode keys
        # so sensor hold_override / size_hint see GRID|HYBRID|MOMENTUM (sensor-entry-guard).
        _cycle_keys = (
            "trading_mode",
            "coin_class",
            "santiment_regime",
            "global_market_bias",
            "regime_defensive",
            "exposure_multiplier",
            "allocation",
            # Preserve explicit cycle/test DCA + exit overlays after re-resolve
            "dca",
            "recovery_hold",
            "trailing_stop",
            "trailing_take_profit",
        )
        _preserved_cycle = {
            k: market.strategy_params.get(k)
            for k in _cycle_keys
            if market.strategy_params.get(k) is not None
        }

        if regime_result or allocation:
            market.strategy_params = resolve_strategy_params(
                coin,
                has_position=market.has_position,
                atr_pct=market.atr_pct,
                frozen_tier=get_position(coin["symbol"], market.timeframe).get("strategy_tier"),
                regime_result=regime_result,
                allocation=allocation,
            ) or market.strategy_params

        for _k, _v in _preserved_cycle.items():
            market.strategy_params[_k] = _v

        strategy = get_strategy({**coin, "strategy_params": market.strategy_params})
        technical = strategy.analyze(coin, market, x_signals=None)

        if regime_result:
            technical.regime = regime_result.primary_regime
            technical.regime_confidence = regime_result.confidence
            technical.sentiment_score = regime_result.sentiment_score
            if regime_result.primary_regime and not technical.rationale:
                technical.rationale = f"regime={regime_result.primary_regime}"
            elif regime_result.primary_regime:
                technical.rationale = f"{technical.rationale} | regime={regime_result.primary_regime}"
        if allocation:
            technical.allocation = {
                "strategy_weights": allocation.strategy_weights,
                "exposure_multiplier": allocation.exposure_multiplier,
                "defensive_mode": allocation.defensive_mode,
            }

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
        sell_source = ""
        sell_policy_audit: dict = {}
        sensor_shadow = ""
        if market.has_position:
            normalized, sources, confidence, structure_rationales, sell_source, sell_policy_audit = self._merge_sell(
                technical, x_signal, cmc_signal, all_social, market, position, lc_signal
            )
            if normalized == HOLD:
                # Epic #222: sniper owns heavy DCA when enabled
                _sniper_blocks_cycle_dca = False
                try:
                    from services.dca_sniper.config import (
                        dca_sniper_config,
                        dca_sniper_enabled,
                    )

                    _sc = dca_sniper_config(self.config.raw)
                    _sniper_blocks_cycle_dca = bool(
                        dca_sniper_enabled(self.config.raw)
                        and _sc.get("disable_cycle_dca_when_enabled", True)
                    )
                except Exception:
                    _sniper_blocks_cycle_dca = False
                dca = None
                if not _sniper_blocks_cycle_dca:
                    dca = evaluate_dca_addon(market, position, market.strategy_params)
                elif position and (
                    position.get("recovery_hold") or position.get("sniper_focus")
                ):
                    sources.append("dca_sniper_focus_hold")
                # #102: optional scheduled calendar DCA when dip path yields nothing
                # (skipped when dca_sniper owns cycle DCA authority)
                if dca is None and not _sniper_blocks_cycle_dca:
                    try:
                        from strategies.dca_scheduled import (
                            equal_share_allocations,
                            evaluate_scheduled_dca_addon,
                            is_symbol_schedule_due,
                            open_symbols_for_schedule,
                            scheduled_config,
                            scheduled_enabled,
                        )

                        if scheduled_enabled(market.strategy_params, config_raw=self.config.raw):
                            scfg = scheduled_config(
                                dict((market.strategy_params or {}).get("dca") or {}),
                                config_raw=self.config.raw,
                            )
                            sym = coin.get("symbol") or market.symbol
                            # Per-symbol cadence: first fire must not block other open coins
                            # in the same cycle (equal-share of total_usdt across universe).
                            if is_symbol_schedule_due(
                                sym,
                                timeframe=market.timeframe,
                                config=scfg,
                                last_run=position.get("last_scheduled_dca_at"),
                            ):
                                open_syms = open_symbols_for_schedule(include_symbol=sym)
                                shares = equal_share_allocations(open_syms, config=scfg)
                                alloc = float(shares.get(sym) or 0)
                                if alloc > 0:
                                    dca = evaluate_scheduled_dca_addon(
                                        market,
                                        position,
                                        market.strategy_params,
                                        allocated_usdt=alloc,
                                        config_raw=self.config.raw,
                                    )
                    except Exception:
                        pass
                if dca:
                    from strategies.dca_portfolio import should_defer_per_coin_dca

                    defer = (
                        not dca.shadow_only
                        and should_defer_per_coin_dca(market.strategy_params, self.config.raw)
                        and str(dca.source or "") != "dca_scheduled"
                    )
                    if defer:
                        sources.append(dca.source)
                        sources.append("dca_portfolio_deferred")
                        structure_rationales.append(
                            f"[portfolio] {dca.rationale} (${dca.usdt_amount:.0f})"
                        )
                    else:
                        normalized = BUY_DCA
                        sources.append(dca.source)
                        structure_rationales.append(dca.rationale)
                        dca_usdt = dca.usdt_amount
                        if str(dca.source or "") == "dca_scheduled":
                            # Stamp only this symbol after it fires (shadow or live).
                            try:
                                from strategies.dca_scheduled import stamp_last_scheduled_dca as _stamp_sched

                                _stamp_sched(
                                    coin.get("symbol") or market.symbol,
                                    market.timeframe,
                                )
                            except Exception:
                                pass
                        if dca.shadow_only:
                            shadow_tag = (
                                "dca_scheduled_shadow"
                                if str(dca.source or "") == "dca_scheduled"
                                else (
                                    "dca_recovery_shadow"
                                    if dca.source == "dca_recovery"
                                    else "dca_shadow"
                                )
                            )
                            sources.append(shadow_tag)
        else:
            normalized, sources, confidence = self._merge_buy(
                technical, x_signal, cmc_signal, all_social, market, lc_signal, coin=coin
            )
            if normalized == HOLD:
                tech_norm = normalize(technical.action)
                if is_buy(tech_norm):
                    normalized = tech_norm
                    sources = list(technical.sources)
            sensor_rationale = ""
            sensor_shadow = ""
            normalized, sources, confidence, sensor_rationale, sensor_shadow = self._apply_entry_sensor_buy(
                normalized,
                sources,
                confidence,
                coin["symbol"],
                market,
                technical,
            )
            if sensor_rationale:
                structure_rationales.append(sensor_rationale)

        self._sync_watch_15m_state(
            coin["symbol"], market, technical, normalized, position
        )

        if not market.has_position and is_buy(normalized):
            from core.coin_eligibility import passes_coin_filters

            filter_ok, filter_reason = passes_coin_filters(
                coin, market, self.config.raw, context="buy"
            )
            if not filter_ok:
                log(
                    f"[Filter] SKIP {coin['symbol']}: {filter_reason}",
                    "INFO",
                )
                structure_rationales.append(f"[Filter] {filter_reason}")
                normalized = HOLD
                sources.append("coin_filter_blocked")

        # Position lock: suppress auto sells (manual still via TradingService + risk)
        if market.has_position and is_sell(normalized):
            try:
                from strategies.position_lock import auto_sell_blocked

                locked, lock_msg = auto_sell_blocked(
                    position, "auto", config=self.config.raw
                )
                if locked:
                    structure_rationales.append(f"[Lock] {lock_msg}")
                    sources.append("position_locked")
                    normalized = HOLD
            except Exception:
                pass

        # Position lock: suppress DCA add-ons
        if market.has_position and normalized == BUY_DCA:
            try:
                from strategies.position_lock import dca_blocked

                locked, lock_msg = dca_blocked(position, config=self.config.raw)
                if locked:
                    structure_rationales.append(f"[Lock] {lock_msg}")
                    sources.append("position_locked")
                    normalized = HOLD
            except Exception:
                pass

        execution_action = to_execution_action(normalized)
        strategy_params = market.strategy_params or {}
        normalized, execution_action, shadow_action = self._apply_shadow_mode(
            normalized, execution_action, strategy_params, sources
        )
        if "entry_sensor_shadow" in sources and sensor_shadow and not shadow_action:
            shadow_action = sensor_shadow
        if (
            "dca_shadow" in sources
            or "dca_recovery_shadow" in sources
            or "dca_scheduled_shadow" in sources
        ) and normalized == BUY_DCA:
            shadow_action = execution_action
            normalized = HOLD
            execution_action = "HOLD"
        stop_sources = {"x_stop_loss", "stop_loss", "technical"}
        is_stop_sell = (
            "STOP" in (normalized or "").upper()
            or bool(stop_sources.intersection(sources))
        )
        if policy_shadow_active(self.config.raw) and is_sell(normalized) and not is_stop_sell:
            if not shadow_action:
                shadow_action = execution_action
            sources.append("sell_policy_shadow")
            normalized = HOLD
            execution_action = "HOLD"
        if sell_source == "time_profit_exit":
            from strategies.positions import mark_time_profit_exit_done

            mark_time_profit_exit_done(coin["symbol"], market.timeframe)
        if sell_source == "trailing_take_profit":
            mark_trailing_take_profit_step(coin["symbol"], market.timeframe, market.current_price)
        if sell_source == "profit_max_lifetime":
            mark_profit_max_lifetime_done(coin["symbol"], market.timeframe)

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
        if "trailing_take_profit" in sources:
            rationale_parts.append("Trail->take profit")
        if "profit_max_lifetime" in sources:
            rationale_parts.append("Life->max profit")
        if "time_profit_exit" in sources:
            rationale_parts.append("Time->profit exit")
        if "dca" in sources:
            rationale_parts.append("DCA->accumulation")
        if ENTRY_SENSOR_SOURCE in sources:
            rationale_parts.append("15m->vol entry")
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
            sell_policy_audit=sell_policy_audit,
            sell_source=str(sell_source or ""),
        )
        if dca_usdt > 0:
            analysis.dca_usdt = dca_usdt
        elif float(getattr(technical, "dca_usdt", 0) or 0) > 0:
            analysis.dca_usdt = float(technical.dca_usdt)

        if getattr(technical, "regime", None):
            analysis.regime = technical.regime
            analysis.regime_confidence = getattr(technical, "regime_confidence", 0.0)
            analysis.sentiment_score = getattr(technical, "sentiment_score", 0.0)
        if getattr(technical, "allocation", None):
            analysis.allocation = technical.allocation

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
