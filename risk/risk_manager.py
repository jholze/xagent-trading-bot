from datetime import datetime, timedelta

from core.config import BotConfig, get_bot_config
from core.models import RiskDecision, TradeOrder
from data_manager import (
    is_demo_mode,
    is_dry_run_enhanced,
    is_live_dry_run,
    load_live_trade_history,
    load_trade_history,
    resolve_ledger_backend,
    simulated_balance_usdt,
    uses_exchange_ledger,
)
from services.gate_balance import fetch_portfolio_equity, fetch_usdt_balance
from services.market_service import MarketService
from services.portfolio_service import PortfolioService
from strategies.positions import (
    count_open_full_slots,
    count_open_positions,
    find_open_position_for_symbol,
    get_position,
    list_active_positions,
    sell_fraction_for_signal,
)


def _is_emergency_sell(signal: str) -> bool:
    signal = signal or ""
    return signal in ("SELL_STOP_FULL", "SELL_STOP_PARTIAL", "SELL_FULL") or "STOP" in signal


def _is_stop_loss_sell(signal: str) -> bool:
    signal = signal or ""
    return signal in ("SELL_STOP_FULL", "SELL_STOP_PARTIAL") or "STOP" in signal


def _is_partial_sell(signal: str) -> bool:
    signal = signal or ""
    if _is_emergency_sell(signal):
        return False
    if "FULL" in signal:
        return False
    return "PARTIAL" in signal or signal in ("SELL", "SELL_10", "SELL_20", "SELL_30", "SELL_TP")


class RiskManager:
    """Central gate for trade sizing and portfolio limits."""

    def __init__(
        self,
        config: BotConfig = None,
        portfolio: PortfolioService = None,
        market_service: MarketService = None,
    ):
        self.config = config or get_bot_config()
        self.portfolio = portfolio or PortfolioService(self.config)
        self.market = market_service or MarketService()

    def evaluate(
        self,
        order: TradeOrder,
        timeframe: str = "4h",
        source: str = "auto",
        trust_score: float = None,
        confidence: float = None,
        indicators: dict = None,
    ) -> RiskDecision:
        decision = self._evaluate_impl(
            order,
            timeframe=timeframe,
            source=source,
            trust_score=trust_score,
            confidence=confidence,
            indicators=indicators,
        )
        # R15: durable risk_rejects.jsonl for all BUY denies (fail-open)
        try:
            if (
                str(getattr(order, "type", "") or "").upper() == "BUY"
                and not getattr(decision, "approved", True)
            ):
                from services.watchlist_quality.soak_log import log_risk_reject

                raw = self.config.raw if hasattr(self.config, "raw") else None
                log_risk_reject(
                    symbol=getattr(order, "symbol", "") or "",
                    side="BUY",
                    source=str(source or ""),
                    code=getattr(decision, "code", "") or "",
                    message=getattr(decision, "message", "") or "",
                    config=raw,
                )
        except Exception:
            pass
        return decision

    def _evaluate_impl(
        self,
        order: TradeOrder,
        timeframe: str = "4h",
        source: str = "auto",
        trust_score: float = None,
        confidence: float = None,
        indicators: dict = None,
    ) -> RiskDecision:
        from core.test_symbols import is_phantom_test_symbol

        cfg = self.config.raw if hasattr(self.config, "raw") else self.config
        if (
            is_demo_mode()
            and resolve_ledger_backend("demo", cfg) == "mongo"
            and is_phantom_test_symbol(order.symbol)
        ):
            return RiskDecision(
                approved=False,
                message=f"Phantom test symbol blocked: {order.symbol}",
                code="phantom_symbol",
            )

        if order.type == "SELL":
            blocked, reason = self._trade_cooldown_blocked(order, timeframe, source=source)
            if blocked:
                return RiskDecision(approved=False, message=reason, code="trade_cooldown")
            # Position lock: block auto exits (exit_ws/trail/TA/grid); manual can still sell
            try:
                from strategies.position_lock import auto_sell_blocked, log_lock_block

                pos = get_position(order.symbol, timeframe)
                raw_cfg = self.config.raw if hasattr(self.config, "raw") else None
                sell_src = source or getattr(order, "source", None) or ""
                locked, lock_msg = auto_sell_blocked(pos, sell_src, config=raw_cfg)
                if locked:
                    log_lock_block(order.symbol, lock_msg, source=str(sell_src))
                    return RiskDecision(
                        approved=False,
                        message=lock_msg,
                        code="position_locked",
                    )
            except Exception:
                pass
            order = self._resolve_sell_order(order, timeframe, source)
            if order.amount <= 0:
                order = self._fill_sell_amount_from_open_lot(order, timeframe)
            if order.amount <= 0:
                return RiskDecision(approved=False, message="No amount to sell", code="no_amount")
            partial_block, partial_reason = self._partial_sell_blocked(order, timeframe, source)
            if partial_block:
                return RiskDecision(approved=False, message=partial_reason, code="partial_sell_guard")
            max_daily_sells = self._effective_max_daily_sells()
            daily_sells = self._daily_sells_count()
            if max_daily_sells > 0 and daily_sells >= max_daily_sells:
                return RiskDecision(
                    approved=False,
                    message=f"Daily sell limit reached ({daily_sells}/{max_daily_sells})",
                    code="max_daily_sells",
                )
            return RiskDecision(approved=True, order=order, message="Sell approved")

        if order.price <= 0:
            return RiskDecision(approved=False, message="Invalid price")

        blocked, reason = self._trade_cooldown_blocked(order, timeframe, source=source)
        if blocked:
            return RiskDecision(approved=False, message=reason, code="trade_cooldown")

        # Position lock no_dca: block all BUY_DCA paths (cycle, recovery, sniper)
        # Defense-in-depth — sniper/bot_http and DE also check, Risk is final rail.
        # Fail-closed: lock-check errors must not approve adds on locked lots.
        if self._is_dca_buy(source, order):
            try:
                from strategies.position_lock import dca_blocked, log_lock_block

                pos = get_position(order.symbol, timeframe)
                raw_cfg = self.config.raw if hasattr(self.config, "raw") else None
                locked, lock_msg = dca_blocked(pos, config=raw_cfg)
                if locked:
                    log_lock_block(
                        order.symbol, lock_msg, source=str(source or "dca")
                    )
                    return RiskDecision(
                        approved=False,
                        message=lock_msg,
                        code="position_locked",
                    )
            except Exception as exc:
                try:
                    from logger import log

                    log(
                        f"position_lock dca check error {order.symbol}: {exc}",
                        "ERROR",
                    )
                except Exception:
                    pass
                return RiskDecision(
                    approved=False,
                    message=f"position_lock_check_error: {exc}"[:200],
                    code="position_lock_check_error",
                )

        # Permanent stablecoin buy rail (all buy paths: TA, grid, gainer, DCA, …)
        try:
            from core.stablecoins import (
                is_stablecoin_symbol,
                stablecoin_block_reason,
                stablecoin_buys_blocked,
            )

            raw_cfg = self.config.raw if hasattr(self.config, "raw") else None
            if stablecoin_buys_blocked(raw_cfg) and is_stablecoin_symbol(order.symbol):
                return RiskDecision(
                    approved=False,
                    message=stablecoin_block_reason(order.symbol),
                    code="stablecoin_blocked",
                    size_multiplier=0.0,
                )
        except Exception:
            pass

        pos = get_position(order.symbol, timeframe)
        has_position = float(pos.get("amount", 0)) > 0

        # Global market bias (oracle + santiment): block new buys on CRASH / warmup / size 0.
        if not has_position:
            # Universe split: new BUYs only on trade-eligible set (observe is broader)
            try:
                from services.universe.split import is_trade_eligible, universe_split_enabled

                raw_cfg = self.config.raw if hasattr(self.config, "raw") else None
                if universe_split_enabled(raw_cfg) and not self._is_dca_buy(source, order):
                    if not is_trade_eligible(
                        order.symbol,
                        config=raw_cfg,
                    ):
                        return RiskDecision(
                            approved=False,
                            message=(
                                f"Outside trade universe (observe-only): {order.symbol}"
                            ),
                            code="universe_trade_cap",
                            size_multiplier=0.0,
                        )
            except Exception:
                pass

            # Issue #162: prev-day gainer chase guard (new entries only, not DCA add)
            if not self._is_dca_buy(source, order):
                try:
                    from services.gainer_universe.chase_guard import check_gainer_chase_guard

                    raw_cfg = self.config.raw if hasattr(self.config, "raw") else None
                    px = float(getattr(order, "price", 0) or 0)
                    blocked, gmsg = check_gainer_chase_guard(
                        order.symbol, px, config=raw_cfg
                    )
                    if blocked:
                        return RiskDecision(
                            approved=False,
                            message=gmsg,
                            code="gainer_chase_guard",
                            size_multiplier=0.0,
                        )
                except Exception:
                    pass

            try:
                from services.market_policy_fusion import get_global_market_bias

                bias = get_global_market_bias(
                    self.config.raw if hasattr(self.config, "raw") else None
                )
                if bias.get("block_buys"):
                    try:
                        from services.market_context_observability import note_buy_blocked

                        note_buy_blocked(
                            regime=bias.get("regime"),
                            source=bias.get("source"),
                            rationale=str(bias.get("rationale") or ""),
                        )
                    except Exception:
                        pass
                    return RiskDecision(
                        approved=False,
                        message=(
                            f"Market {bias.get('regime') or 'block'} "
                            f"[{bias.get('source') or 'global'}]: "
                            f"no new entries ({bias.get('rationale') or 'policy'})"
                        ),
                        code="market_block",
                        size_multiplier=float(bias.get("size_mult") or 0.0),
                    )
            except Exception:
                pass

            # Coin memory soft_block: skip *new* entries only (DCA / existing pos allowed)
            if not has_position and not self._is_dca_buy(source, order):
                try:
                    from intelligence.memory.cache import get_entry_bias, get_coin_profile
                    from strategies.sensor_entry_policy import is_sensor_source

                    if get_entry_bias(order.symbol) == "soft_block":
                        prof = get_coin_profile(order.symbol)
                        feats = (prof.features if prof else None) or {}
                        # Legacy soft_block (no scope in features) blocks all new entries.
                        # Gross-loss sensor_only: only block sensor-family sources.
                        scope = str(feats.get("soft_block_scope") or "").lower()
                        block_this = True
                        if scope == "sensor_only" and not is_sensor_source(source):
                            block_this = False
                        # TTL soft_block_until in features
                        if block_this and isinstance(feats, dict):
                            until = feats.get("soft_block_until")
                            if until:
                                try:
                                    from datetime import datetime, timezone

                                    u = str(until).replace("Z", "+00:00")
                                    until_dt = datetime.fromisoformat(u)
                                    if until_dt.tzinfo is None:
                                        until_dt = until_dt.replace(tzinfo=timezone.utc)
                                    if datetime.now(timezone.utc) > until_dt:
                                        block_this = False
                                except Exception:
                                    pass
                        if block_this:
                            return RiskDecision(
                                approved=False,
                                message=(
                                    f"Coin memory soft_block {order.symbol}: "
                                    f"{(prof.rationale if prof else 'weak history')}"
                                ),
                                code="coin_memory_soft_block",
                                size_multiplier=0.0,
                            )
                except Exception:
                    pass
                # WQE-R1: quality gate for all new entries (soft/enforce); DCA/open exempt
                if not has_position and not self._is_dca_buy(source, order):
                    try:
                        from services.watchlist_quality.config import wqe_mode
                        from services.watchlist_quality.enforce import buy_allowed
                        from services.watchlist_quality.store import load_quality_scores

                        raw = self.config.raw if hasattr(self.config, "raw") else None
                        if wqe_mode(raw) in ("soft", "enforce"):
                            try:
                                from core.tenant_context import current_tenant_id

                                tid = current_tenant_id() or "default"
                            except Exception:
                                tid = "default"
                            data = load_quality_scores(tenant_id=tid)
                            scored = None
                            for c in data.get("coins") or []:
                                if isinstance(c, dict) and c.get("symbol") == order.symbol:
                                    scored = c
                                    break
                            ok, reason = buy_allowed(
                                order.symbol,
                                scored_row=scored,
                                config=raw,
                                source=str(source or ""),
                                is_new_add=True,
                                has_open_position=False,
                            )
                            if not ok:
                                try:
                                    from services.watchlist_quality.metrics import note_buy_blocked
                                    from services.watchlist_quality.event_log import log_buy_block

                                    note_buy_blocked(reason)
                                    q = None
                                    if scored:
                                        q = scored.get("quality_shadow_ai")
                                        if q is None:
                                            q = scored.get("quality_score")
                                    log_buy_block(
                                        order.symbol,
                                        reason,
                                        source=str(source or ""),
                                        mode=mode,
                                        quality_score=q,
                                        config=raw,
                                    )
                                except Exception:
                                    pass
                                return RiskDecision(
                                    approved=False,
                                    message=f"WQE block {order.symbol}: {reason}",
                                    code="watchlist_quality",
                                    size_multiplier=0.0,
                                )
                    except Exception:
                        pass
                # Re-entry cooloff after gross loss (sensor-entry-guard)
                try:
                    cool = self._sensor_reentry_cooloff_blocked(order, source)
                    if cool:
                        return cool
                except Exception:
                    pass
                # Venue quality hard gate (buys only; sells never use this)
                try:
                    from services.venue_quality import check_venue_for_buy, source_applies_venue, venue_quality_config

                    vcfg = venue_quality_config(
                        self.config.raw if hasattr(self.config, "raw") else None
                    )
                    if vcfg.get("enabled", True) and source_applies_venue(source, vcfg):
                        planned = float(order.usdt_amount or 0) or float(
                            self.config.max_usdt_per_trade or 0
                        )
                        vres = check_venue_for_buy(
                            order.symbol,
                            source=source,
                            planned_usdt=planned,
                            config_raw=self.config.raw if hasattr(self.config, "raw") else None,
                        )
                        if not vres.ok:
                            return RiskDecision(
                                approved=False,
                                message=(
                                    f"Venue quality block {order.symbol}: "
                                    + ("; ".join(vres.reasons) or "thin market")
                                ),
                                code="venue_liquidity_block",
                                size_multiplier=0.0,
                            )
                except Exception:
                    pass
                # Optional macro calendar hard block (default off — prefer size mult)
                try:
                    from intelligence.macro.snapshot import get_risk_multipliers

                    mm = get_risk_multipliers(
                        self.config.raw if hasattr(self.config, "raw") else None
                    )
                    if mm.get("block_new_entries"):
                        return RiskDecision(
                            approved=False,
                            message=(
                                f"Macro calendar pre-window block: "
                                f"{mm.get('calendar_risk') or mm.get('next_event') or 'high impact'}"
                            ),
                            code="macro_calendar_block",
                            size_multiplier=0.0,
                        )
                except Exception:
                    pass

        open_slots = count_open_full_slots(self.config.raw)
        if not has_position:
            cap = self._resolve_position_capacity(full_slots=open_slots)
            if open_slots >= cap.max_open_eff:
                from risk.position_capacity import format_capacity_reject_message

                msg = format_capacity_reject_message(cap, open_slots)
                free = max(0, int(cap.max_open_eff) - int(open_slots))
                try:
                    from risk.slot_eviction_runtime import try_slot_eviction_on_max_open

                    _plan, suffix = try_slot_eviction_on_max_open(
                        order=order,
                        source=source,
                        free_full_slots=free,
                        config=self.config,
                        risk_config=self.config.risk_config,
                        config_raw=self.config.raw if hasattr(self.config, "raw") else None,
                        spike_multiple=float(
                            getattr(order, "entry_15m_vol_ratio", None) or 0
                        ),
                        risk_manager=self,
                    )
                    if suffix:
                        msg = f"{msg}{suffix}"
                except Exception:
                    pass
                return RiskDecision(
                    approved=False,
                    message=msg,
                    code="max_open_positions",
                )

        is_dca = self._is_dca_buy(source, order)
        floor_block = self._cash_floor_blocked(is_dca=is_dca)
        if floor_block:
            return floor_block

        buy_limit = self._daily_buy_limit_blocked(is_dca)
        if buy_limit:
            return buy_limit

        base_usdt = order.usdt_amount or self._base_usdt_cap()
        if source == "cmc":
            fusion = self.config.cmc_trending_fusion_config
            from data_manager import load_cmc_trending_overlay, trending_watchlist_live_enabled

            trending_syms = set()
            if trending_watchlist_live_enabled(self.config.raw):
                trending_syms = {
                    c.get("symbol") for c in load_cmc_trending_overlay().get("coins", [])
                }
            if order.symbol in trending_syms:
                pct = float(fusion.get("trending_trade_size_pct", 50)) / 100.0
                base_usdt = base_usdt * pct
        if source in ("dca", "dca_recovery") or order.signal == "BUY_DCA":
            params = self.config.strategy_params(order.symbol, timeframe)
            try:
                from strategies.registry import resolve_strategy_params

                pos = get_position(order.symbol, timeframe)
                params = resolve_strategy_params(
                    {"symbol": order.symbol, "timeframe": timeframe},
                    has_position=True,
                    frozen_tier=pos.get("strategy_tier"),
                )
            except Exception:
                pass
            dca_cfg = dict(params.get("dca") or {})
            if order.usdt_amount:
                base_usdt = float(order.usdt_amount)
            elif source == "dca_recovery":
                from strategies.dca_recovery import recovery_config, recovery_usdt_amount

                rec_cfg = recovery_config(params)
                pos = get_position(order.symbol, timeframe)
                base_usdt = recovery_usdt_amount(rec_cfg, params, position=pos)
            elif dca_cfg.get("fixed_usdt"):
                base_usdt = float(dca_cfg["fixed_usdt"])
        if source == "manual":
            # Telegram /buy amounts are explicit user intent — don't shrink via auto-trade multipliers.
            sized = base_usdt
            factors = {
                "trust_factor": 1.0,
                "conf_factor": 1.0,
                "atr_factor": 1.0,
                "drawdown_pct": round(self._equity_drawdown_pct(), 2),
                "drawdown_multiplier": 1.0,
                "total_multiplier": 1.0,
            }
        elif is_dca:
            sized = base_usdt
            factors = {
                "trust_factor": 1.0,
                "conf_factor": 1.0,
                "atr_factor": 1.0,
                "drawdown_pct": round(self._equity_drawdown_pct(), 2),
                "drawdown_multiplier": 1.0,
                "total_multiplier": 1.0,
            }
            # Mild size lift under moderate_deploy (+ cash-rich boost)
            try:
                from risk.moderate_deploy import size_boost_for_regime
                from services.market_policy_fusion import get_global_market_bias

                raw_cfg = self.config.raw if hasattr(self.config, "raw") else None
                bias = get_global_market_bias(raw_cfg) or {}
                regime = bias.get("regime")
                cash_pct = None
                try:
                    eq = float(self._portfolio_equity(order.price, order.symbol) or 0)
                    cash = float(self._available_usdt(eq) or 0)
                    if eq > 0:
                        cash_pct = 100.0 * cash / eq
                except Exception:
                    cash_pct = None
                md_boost = size_boost_for_regime(
                    raw_cfg, regime, is_dca=True, cash_pct=cash_pct
                )
                if md_boost > 1.0:
                    sized = float(sized) * md_boost
                    factors["moderate_deploy_mult"] = round(md_boost, 3)
                    factors["total_multiplier"] = round(md_boost, 3)
                    factors["global_regime"] = regime
                    if cash_pct is not None:
                        factors["cash_pct"] = round(cash_pct, 1)
            except Exception:
                pass
        else:
            if indicators is None:
                indicators = self.market.fetch_indicators(order.symbol, timeframe, order.price)
            # Sensor-entry-guard: no aggression inflation on pure sensor size
            from strategies.sensor_entry_policy import is_sensor_source

            sensor_cfg = {}
            try:
                sensor_cfg = self.config.entry_sensor_15m_config
            except Exception:
                sensor_cfg = {}
            skip_dyn = is_sensor_source(source) and bool(
                sensor_cfg.get("ignore_aggression_boost", True)
            )
            if skip_dyn:
                sized = float(base_usdt)
                factors = {
                    "trust_factor": 1.0,
                    "conf_factor": 1.0,
                    "atr_factor": 1.0,
                    "drawdown_pct": round(self._equity_drawdown_pct(), 2),
                    "drawdown_multiplier": 1.0,
                    "total_multiplier": 1.0,
                    "sensor_size_locked": True,
                }
            else:
                sized, factors = self._dynamic_size(
                    base_usdt,
                    order,
                    timeframe,
                    source,
                    trust_score,
                    confidence,
                    indicators,
                )
            # Absolute sensor cap (defense-in-depth)
            if is_sensor_source(source):
                abs_cap = sensor_cfg.get("max_usdt_absolute")
                if abs_cap is not None and float(abs_cap) > 0:
                    sized = min(float(sized), float(abs_cap))
                    factors["sensor_max_usdt_absolute"] = float(abs_cap)

        # Hard per-ticket ceiling AFTER multipliers (moderate_deploy / cash-rich / DCA
        # boosts must not push above max_usdt_per_trade — e.g. 4500 * 1.38 was ~6.2k).
        ticket_cap = float(self._base_usdt_cap() or 0)
        if ticket_cap > 0 and float(sized) > ticket_cap:
            sized = ticket_cap
            factors["ticket_capped"] = True
            factors["ticket_cap_usdt"] = ticket_cap

        equity = self._portfolio_equity(order.price, order.symbol)
        pos_value = float(pos.get("amount", 0)) * order.price
        max_position_value = equity * (self.config.max_position_percent / 100.0)
        room = max_position_value - pos_value

        if room <= 0:
            return RiskDecision(
                approved=False,
                message=f"Max position concentration ({self.config.max_position_percent}% of portfolio)",
                code="max_position_percent",
            )

        if sized > room:
            sized = room
            factors["concentration_capped"] = True

        balance = self._spendable_usdt(equity, is_dca=is_dca)
        if sized > balance:
            sized = balance
            factors["balance_capped"] = True
            factors["spendable_usdt"] = round(balance, 2)

        min_trade = float(self.config.risk_config.get("min_trade_usdt", 5.0))
        if sized < min_trade:
            floor_abs = self._cash_floor_abs()
            cash = self._available_usdt(equity)
            # Prefer clear cash_floor messaging when reserve/floor is the constraint
            if floor_abs > 0 and cash - floor_abs < min_trade:
                return RiskDecision(
                    approved=False,
                    message=(
                        f"Cash floor: free ${max(0.0, cash - floor_abs):.2f} "
                        f"(floor ${floor_abs:.0f}, cash ${cash:.2f})"
                    ),
                    code="cash_floor",
                    size_multiplier=factors.get("total_multiplier", 1.0),
                    drawdown_pct=factors.get("drawdown_pct", 0.0),
                    atr_factor=factors.get("atr_factor", 1.0),
                    trust_factor=factors.get("trust_factor", 1.0),
                )
            return RiskDecision(
                approved=False,
                message=f"Adjusted size ${sized:.2f} below minimum (${min_trade:.0f})",
                code="size_too_small",
                size_multiplier=factors.get("total_multiplier", 1.0),
                drawdown_pct=factors.get("drawdown_pct", 0.0),
                atr_factor=factors.get("atr_factor", 1.0),
                trust_factor=factors.get("trust_factor", 1.0),
            )

        if is_dca:
            dca_usdt_limit = self._daily_dca_usdt_limit_blocked(sized)
            if dca_usdt_limit:
                return dca_usdt_limit

        resolved_source = order.source if order.source not in ("", "auto") else source
        approved = TradeOrder(
            type=order.type,
            symbol=order.symbol,
            price=order.price,
            amount=order.amount,
            usdt_amount=round(sized, 2),
            signal=order.signal,
            source=resolved_source,
            order_id=order.order_id,
            timestamp=order.timestamp,
        )
        return RiskDecision(
            approved=True,
            order=approved,
            message="Approved",
            size_multiplier=factors.get("total_multiplier", 1.0),
            drawdown_pct=factors.get("drawdown_pct", 0.0),
            atr_factor=factors.get("atr_factor", 1.0),
            trust_factor=factors.get("trust_factor", 1.0),
        )

    def status_summary(self, current_price: float = None) -> dict:
        history = self._primary_history()
        equity = self._portfolio_equity(current_price or 0)
        initial = self._initial_capital()
        drawdown_pct = self._equity_drawdown_pct()
        throttle_at = float(self.config.risk_config.get("drawdown_throttle_pct", 10.0))
        cash = self._available_usdt()
        floor_abs = self._cash_floor_abs()
        spendable = self._spendable_usdt(equity, is_dca=False)
        full_slots = count_open_full_slots(self.config.raw)
        out = {
            "open_positions": count_open_positions(),
            "open_full_slots": full_slots,
            "max_open_positions": self.config.max_open_positions,
            "daily_trades": self._daily_trades_count(),
            "daily_buys": self._daily_buys_count(dca_only=False if self._dca_limits_enabled() else None),
            "daily_dca_buys": self._daily_dca_buys_count(),
            "daily_dca_usdt": round(self._daily_dca_usdt_sum(), 2),
            "daily_sells": self._daily_sells_count(),
            "max_daily_trades": self._effective_max_daily_buys(),
            "max_daily_buys": self._effective_max_daily_buys(),
            "max_daily_dca_buys": self._effective_max_daily_dca_buys(),
            "max_daily_dca_usdt": self._effective_max_daily_dca_usdt(),
            "max_daily_sells": self._effective_max_daily_sells(),
            "max_position_percent": self.config.max_position_percent,
            "base_usdt_per_trade": self._base_usdt_cap(),
            "portfolio_equity": round(equity, 2),
            "initial_capital": initial,
            "drawdown_pct": round(drawdown_pct, 2),
            "drawdown_throttle_active": drawdown_pct >= throttle_at,
            "virtual_balance": cash,
            "cash_floor_abs": round(floor_abs, 2),
            "cash_floor_pct": float(self.config.risk_config.get("cash_floor_pct", 0) or 0),
            "spendable_usdt": round(spendable, 2),
            "ledger_source": self._ledger_source_label(),
        }
        pol = self._evaluate_cash_policy(equity)
        if pol is not None and pol.enabled:
            out["cash_policy_enabled"] = True
            out["cash_mode"] = pol.mode
            out["cash_floor_pct_eff"] = round(pol.floor_pct_eff, 2)
            out["spendable_new"] = round(pol.spendable_new, 2)
            out["spendable_dca"] = round(pol.spendable_dca, 2)
            out["dca_buffer_target"] = round(pol.dca_buffer_target, 2)
            out["cash_policy_size_mult"] = round(pol.size_mult, 4)
        else:
            out["cash_policy_enabled"] = False
        try:
            cap = self._resolve_position_capacity(full_slots=full_slots, equity=equity)
            out["max_open_eff"] = cap.max_open_eff
            out["position_capacity_enabled"] = cap.enabled
            out["position_capacity_rationale"] = cap.rationale
            out["position_capacity_factors"] = dict(cap.factors or {})
            out["free_full_slots"] = cap.free_slots
            out["capacity_regime"] = cap.regime
            # Telegram /risk still uses max_open_positions as the live gate
            if cap.enabled:
                out["max_open_positions"] = cap.max_open_eff
        except Exception:
            out["max_open_eff"] = self.config.max_open_positions
            out["position_capacity_enabled"] = False
        return out


    def _sensor_reentry_cooloff_blocked(self, order: TradeOrder, source: str):
        """Block sensor-family re-entry after a recent gross loss on the symbol."""
        from strategies.sensor_entry_policy import is_sensor_source

        if not is_sensor_source(source):
            return None
        risk = self.config.risk_config or {}
        se = risk.get("sensor_entry") or {}
        hours = float(se.get("reentry_cooloff_hours_after_gross_loss") or 0)
        if hours <= 0:
            return None
        try:
            from intelligence.memory.cache import get_coin_profile

            prof = get_coin_profile(order.symbol)
            if not prof or not isinstance(prof.features, dict):
                return None
            last_loss = prof.features.get("last_loss_at") or prof.features.get("soft_block_until")
            if not last_loss:
                return None
            # only if last loss was large
            worst = float(prof.features.get("worst_loss_usdt") or 0)
            worst_pct = float(prof.features.get("worst_loss_pct") or 0)
            min_usdt = float(
                ((self.config.raw.get("memory") or {}).get("gross_loss") or {}).get(
                    "min_loss_usdt", 500
                )
            )
            min_pct = float(
                ((self.config.raw.get("memory") or {}).get("gross_loss") or {}).get(
                    "min_loss_pct", 25
                )
            )
            if abs(worst) < min_usdt and abs(worst_pct) < min_pct:
                return None
            from datetime import datetime, timezone, timedelta

            u = str(last_loss).replace("Z", "+00:00")
            loss_dt = datetime.fromisoformat(u)
            if loss_dt.tzinfo is None:
                loss_dt = loss_dt.replace(tzinfo=timezone.utc)
            # if soft_block_until is in future, use last_loss_at for cooloff start
            start = loss_dt
            if "last_loss_at" in prof.features:
                try:
                    s2 = str(prof.features["last_loss_at"]).replace("Z", "+00:00")
                    start = datetime.fromisoformat(s2)
                    if start.tzinfo is None:
                        start = start.replace(tzinfo=timezone.utc)
                except Exception:
                    pass
            elapsed_h = (datetime.now(timezone.utc) - start).total_seconds() / 3600.0
            if elapsed_h < hours:
                return RiskDecision(
                    approved=False,
                    message=(
                        f"Sensor re-entry cooloff {order.symbol}: "
                        f"{elapsed_h:.1f}h < {hours:.0f}h after gross loss"
                    ),
                    code="sensor_reentry_cooloff",
                    size_multiplier=0.0,
                )
        except Exception:
            return None
        return None

    def _ledger_source_label(self) -> str:
        if is_live_dry_run(self.config.raw):
            return "simulated"
        if uses_exchange_ledger(self.config.trading_mode):
            return "gate"
        return "paper"

    def _effective_max_daily_buys(self) -> int:
        if is_dry_run_enhanced(self.config.raw):
            defaults = self.config.dry_run_defaults
            if defaults.get("max_daily_buys") is not None:
                return int(defaults["max_daily_buys"])
            if defaults.get("max_daily_trades") is not None:
                return int(defaults["max_daily_trades"])
        risk_cfg = self.config.risk_config
        if risk_cfg.get("max_daily_buys") is not None:
            return int(risk_cfg["max_daily_buys"])
        return self.config.max_daily_trades

    def _effective_max_daily_sells(self) -> int:
        if is_dry_run_enhanced(self.config.raw):
            defaults = self.config.dry_run_defaults
            if defaults.get("max_daily_sells") is not None:
                return int(defaults["max_daily_sells"])
        risk_cfg = self.config.risk_config
        if risk_cfg.get("max_daily_sells") is not None:
            return int(risk_cfg["max_daily_sells"])
        return int(self.config.raw.get("max_daily_sells", 0))

    def _effective_max_daily_trades(self) -> int:
        """Backward-compatible alias for buy limit."""
        return self._effective_max_daily_buys()

    def _effective_max_daily_dca_buys(self) -> int:
        if is_dry_run_enhanced(self.config.raw):
            defaults = self.config.dry_run_defaults
            if defaults.get("max_daily_dca_buys") is not None:
                return int(defaults["max_daily_dca_buys"])
        risk_cfg = self.config.risk_config
        if risk_cfg.get("max_daily_dca_buys") is not None:
            return int(risk_cfg["max_daily_dca_buys"])
        return 0

    def _effective_max_daily_dca_usdt(self) -> float:
        if is_dry_run_enhanced(self.config.raw):
            defaults = self.config.dry_run_defaults
            if defaults.get("max_daily_dca_usdt") is not None:
                return float(defaults["max_daily_dca_usdt"])
        risk_cfg = self.config.risk_config
        if risk_cfg.get("max_daily_dca_usdt") is not None:
            return float(risk_cfg["max_daily_dca_usdt"])
        return 0.0

    def _dca_limits_enabled(self) -> bool:
        return self._effective_max_daily_dca_buys() > 0

    @staticmethod
    def _is_dca_buy(source: str, order: TradeOrder) -> bool:
        src = str(source or "").strip().lower()
        if src in ("dca", "dca_recovery", "dca_sniper", "dca_scheduled"):
            return True
        return str(getattr(order, "signal", "") or "").upper() == "BUY_DCA"

    @staticmethod
    def _order_is_dca(order: dict) -> bool:
        src = str(order.get("source", "")).lower()
        if src in ("dca", "dca_recovery", "dca_sniper", "dca_scheduled"):
            return True
        return str(order.get("signal", "")).upper() == "BUY_DCA"

    @staticmethod
    def _filled_order_usdt(order: dict) -> float:
        for key_path in (
            ("risk", "approved_usdt"),
            ("execution", "usdt"),
            ("request", "usdt"),
        ):
            section, field = key_path
            raw = (order.get(section) or {}).get(field)
            if raw is not None:
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    continue
        return 0.0

    def _daily_buy_limit_blocked(self, is_dca: bool) -> RiskDecision | None:
        if is_dca and self._dca_limits_enabled():
            daily_dca = self._daily_dca_buys_count()
            max_dca = self._effective_max_daily_dca_buys()
            if daily_dca >= max_dca:
                return RiskDecision(
                    approved=False,
                    message=f"Daily DCA limit reached ({daily_dca}/{max_dca})",
                    code="max_daily_dca_buys",
                )
            return None

        daily_buys = self._daily_buys_count(
            dca_only=False if self._dca_limits_enabled() else None,
        )
        max_daily_buys = self._effective_max_daily_buys()
        if max_daily_buys > 0 and daily_buys >= max_daily_buys:
            return RiskDecision(
                approved=False,
                message=f"Daily buy limit reached ({daily_buys}/{max_daily_buys})",
                code="max_daily_trades",
            )
        return None

    def _daily_dca_usdt_limit_blocked(self, sized_usdt: float) -> RiskDecision | None:
        max_usdt = self._effective_max_daily_dca_usdt()
        if max_usdt <= 0:
            return None
        spent = self._daily_dca_usdt_sum()
        if spent + sized_usdt > max_usdt:
            return RiskDecision(
                approved=False,
                message=(
                    f"Daily DCA USDT limit reached "
                    f"(${spent:.0f}+${sized_usdt:.0f}>${max_usdt:.0f})"
                ),
                code="max_daily_dca_usdt",
            )
        return None

    def _base_usdt_cap(self) -> float:
        if self.config.trading_mode == "live":
            return float(
                self.config.live_config.get("max_usdt_per_trade", self.config.max_usdt_per_trade)
            )
        return self.config.max_usdt_per_trade

    def _initial_capital(self) -> float:
        from core.portfolio_baseline import initial_capital
        from data_manager import resolve_ledger_scope

        return initial_capital(
            scope=resolve_ledger_scope(self.config.trading_mode),
            config=self.config.raw,
            trading_mode=self.config.trading_mode,
        )

    def _primary_history(self) -> dict:
        if uses_exchange_ledger(self.config.trading_mode):
            return load_live_trade_history()
        return load_trade_history()

    def _available_usdt(self, fallback: float = 0) -> float:
        from core.simulated_trading import is_simulated_trading, uses_order_ledger_cash
        from data_manager import resolve_sim_cash_balance

        if is_simulated_trading(self.config.raw):
            if uses_order_ledger_cash(self.config.raw):
                return resolve_sim_cash_balance(config=self.config.raw)
            history = load_live_trade_history()
            return float(history.get("virtual_balance", simulated_balance_usdt(self.config.raw)))
        if is_live_dry_run(self.config.raw):
            history = load_live_trade_history()
            return float(history.get("virtual_balance", simulated_balance_usdt(self.config.raw)))
        if uses_exchange_ledger(self.config.trading_mode):
            return fetch_usdt_balance(self.config)
        return float(load_trade_history().get("virtual_balance", fallback))

    def _cash_floor_basis_ref(
        self, equity: float | None = None, *, adaptive: bool = False
    ) -> float:
        """Reference capital for floor % → absolute conversion."""
        risk = self.config.risk_config
        pol = risk.get("cash_policy") if isinstance(risk.get("cash_policy"), dict) else {}
        # Adaptive floor_basis only when policy is active; else legacy cash_floor_basis
        if adaptive and pol:
            basis = str(pol.get("floor_basis") or risk.get("cash_floor_basis", "initial") or "initial")
        else:
            basis = str(risk.get("cash_floor_basis", "initial") or "initial")
        basis = basis.lower()
        if basis == "nav":
            return float(equity if equity is not None else self._portfolio_equity())
        return float(self._initial_capital())

    def _market_bias_for_cash(self) -> dict:
        """Fusion/global bias for cash policy; fail-open to neutral."""
        try:
            from services.market_policy_fusion import get_global_market_bias

            return dict(get_global_market_bias(self.config.raw) or {})
        except Exception:
            return {"size_mult": 1.0, "block_buys": False, "regime": None}

    def _process_uptime_sec(self) -> float | None:
        try:
            from services.market_oracle_store import process_uptime_sec

            return float(process_uptime_sec())
        except Exception:
            return None

    def _open_book_memory_counts(self) -> tuple[int, int, int]:
        """soft_block / toxic / prefer among open positions — fail-open (0,0,0)."""
        try:
            from intelligence.memory.cache import get_coin_profile
            from risk.position_capacity import count_open_book_memory_signals

            return count_open_book_memory_signals(
                list_active_positions(),
                get_profile=lambda sym: get_coin_profile(
                    sym, config=self.config.raw if hasattr(self.config, "raw") else None
                ),
            )
        except Exception:
            return 0, 0, 0

    def _resolve_position_capacity(
        self,
        *,
        full_slots: int | None = None,
        equity: float | None = None,
    ):
        """Fusion + cash + memory → CapacitySnapshot (fail-open to static base)."""
        from risk.cash_policy import MODE_STEADY
        from risk.position_capacity import resolve_max_open_eff

        base = int(self.config.max_open_positions)
        risk = self.config.risk_config
        bias = self._market_bias_for_cash()
        try:
            size_mult = float(bias.get("size_mult", 1.0) or 1.0)
        except (TypeError, ValueError):
            size_mult = 1.0
        block_buys = bool(bias.get("block_buys"))
        regime = bias.get("regime")

        cash_mode = MODE_STEADY
        spendable_new = None
        pol = self._evaluate_cash_policy(equity)
        if pol is not None:
            cash_mode = pol.mode
            spendable_new = float(pol.spendable_new)
            # Prefer policy-linked fusion signals when cash policy is on
            if pol.enabled:
                size_mult = float(pol.size_mult)
                block_buys = bool(pol.block_buys)

        throttle_at = float(risk.get("drawdown_throttle_pct", 10.0) or 10.0)
        drawdown_active = float(self._equity_drawdown_pct()) >= throttle_at
        soft_n, toxic_n, prefer_n = self._open_book_memory_counts()

        # Inject avg_entry into capacity section from bot trade size (no hardcode)
        risk_for_cap = dict(risk) if isinstance(risk, dict) else {}
        pc = dict(risk_for_cap.get("position_capacity") or {})
        if pc.get("enabled") and not pc.get("avg_entry_usdt"):
            try:
                pc["avg_entry_usdt"] = float(self._base_usdt_cap() or 0)
            except Exception:
                pass
            risk_for_cap["position_capacity"] = pc

        return resolve_max_open_eff(
            base=base,
            risk_config=risk_for_cap,
            regime=regime,
            size_mult=size_mult,
            block_buys=block_buys,
            cash_mode=cash_mode,
            spendable_new=spendable_new,
            soft_block_open=soft_n,
            toxic_open=toxic_n,
            prefer_open=prefer_n,
            process_uptime_sec=self._process_uptime_sec(),
            full_slots=full_slots,
            drawdown_active=drawdown_active,
        )

    def _evaluate_cash_policy(self, equity: float | None = None):
        """Adaptive cash evaluation when enabled; None when legacy path."""
        from risk.cash_policy import evaluate_cash_policy, is_cash_policy_enabled

        risk = self.config.risk_config
        if not is_cash_policy_enabled(risk):
            return None
        eq = float(equity if equity is not None else self._portfolio_equity())
        cash = float(self._available_usdt(eq))
        bias = self._market_bias_for_cash()
        try:
            size_mult = float(bias.get("size_mult", 1.0) or 1.0)
        except (TypeError, ValueError):
            size_mult = 1.0
        block_buys = bool(bias.get("block_buys"))
        throttle_at = float(risk.get("drawdown_throttle_pct", 10.0) or 10.0)
        dd_pct = float(self._equity_drawdown_pct())
        drawdown_active = dd_pct >= throttle_at
        basis = self._cash_floor_basis_ref(eq, adaptive=True)
        return evaluate_cash_policy(
            cash_total=cash,
            basis_for_floor=basis,
            equity=eq,
            size_mult=size_mult,
            block_buys=block_buys,
            drawdown_active=drawdown_active,
            risk_config=risk,
        )

    def _cash_floor_abs(self) -> float:
        """Absolute min free cash. Adaptive floor when cash_policy.enabled."""
        pol = self._evaluate_cash_policy()
        if pol is not None and pol.enabled:
            return float(pol.floor_abs)
        risk = self.config.risk_config
        pct = float(risk.get("cash_floor_pct", 0) or 0)
        if pct <= 0:
            return 0.0
        ref = self._cash_floor_basis_ref(adaptive=False)
        return max(0.0, float(ref) * (pct / 100.0))

    def _cash_floor_blocked(self, *, is_dca: bool = False) -> "RiskDecision | None":
        """Block buys when spendable for that bucket is below min trade."""
        min_trade = float(self.config.risk_config.get("min_trade_usdt", 5.0))
        pol = self._evaluate_cash_policy()
        if pol is not None and pol.enabled:
            free = float(pol.spendable_dca if is_dca else pol.spendable_new)
            if free >= min_trade:
                return None
            cash = float(self._available_usdt())
            return RiskDecision(
                approved=False,
                message=(
                    f"Cash floor ({pol.mode}): free ${max(0.0, free):.2f} "
                    f"({'dca' if is_dca else 'new'}) "
                    f"(floor ${pol.floor_abs:.0f} / {pol.floor_pct_eff:.1f}%, "
                    f"cash ${cash:.2f})"
                ),
                code="cash_floor",
            )

        floor_abs = self._cash_floor_abs()
        if floor_abs <= 0:
            return None
        cash = self._available_usdt()
        free = cash - floor_abs
        if free >= min_trade:
            return None
        return RiskDecision(
            approved=False,
            message=(
                f"Cash floor: free ${max(0.0, free):.2f} "
                f"(floor ${floor_abs:.0f}, cash ${cash:.2f})"
            ),
            code="cash_floor",
        )

    def _spendable_usdt(self, equity: float, *, is_dca: bool) -> float:
        pol = self._evaluate_cash_policy(equity)
        if pol is not None and pol.enabled:
            return float(pol.spendable_dca if is_dca else pol.spendable_new)

        balance = self._available_usdt(equity)
        floor_abs = self._cash_floor_abs()
        # Absolute floor first (initial capital %)
        if floor_abs > 0:
            balance = max(0.0, balance - floor_abs)
        # Optional extra % reserve of *remaining* cash (legacy dca_reserve_pct)
        if not is_dca:
            reserve_pct = float(self.config.risk_config.get("dca_reserve_pct", 0) or 0)
            if reserve_pct > 0:
                balance = max(0.0, balance * (1.0 - reserve_pct / 100.0))
        return balance

    def _dry_run_reference_prices(self, reference_price: float = 0, symbol: str = None) -> dict:
        ref_prices = {}
        for pos in list_active_positions():
            sym = pos["symbol"] if "/" in pos["symbol"] else f"{pos['symbol']}/USDT"
            ref_prices[sym] = float(pos.get("average_entry", pos.get("entry_price", 0)) or 0)
        if reference_price > 0 and symbol:
            ref_prices[symbol] = reference_price
        return ref_prices

    def _portfolio_equity(self, reference_price: float = 0, symbol: str = None) -> float:
        return fetch_portfolio_equity(
            self.config,
            reference_prices=self._dry_run_reference_prices(reference_price, symbol),
        )

    def _equity_drawdown_pct(self, reference_price: float = 0, symbol: str = None) -> float:
        if is_live_dry_run(self.config.raw):
            history = load_live_trade_history()
            initial = simulated_balance_usdt(self.config.raw)
            equity = self._portfolio_equity(reference_price, symbol)
        else:
            history = load_trade_history()
            initial = self._initial_capital()
            equity = self._portfolio_equity(reference_price, symbol)
        peak = float(history.get("peak_equity", initial))
        peak = max(peak, equity, initial)
        if peak <= 0:
            return 0.0
        return max(0.0, (peak - equity) / peak * 100.0)

    def _dynamic_size(
        self,
        base_usdt: float,
        order: TradeOrder,
        timeframe: str,
        source: str,
        trust_score: float,
        confidence: float,
        indicators: dict,
    ) -> tuple[float, dict]:
        aggression = self.config.aggression_config
        risk = self.config.risk_config

        trust = trust_score if trust_score is not None else 70.0
        conf = confidence if confidence is not None else 50.0

        trust_delta = (trust - 70.0) / 10.0
        trust_factor = 1.0 + trust_delta * 0.1
        if source == "x" and trust < aggression.get("min_trust_for_live", 70):
            trust_factor *= 0.85

        conf_factor = 0.8 + (conf / 100.0) * 0.4

        atr_pct = float(indicators.get("atr_pct", risk.get("atr_reference_pct", 3.0)))
        ref_atr = float(risk.get("atr_reference_pct", 3.0))
        atr_factor = min(1.5, max(0.5, ref_atr / max(atr_pct, 0.5)))

        drawdown_pct = self._equity_drawdown_pct()
        throttle_at = float(risk.get("drawdown_throttle_pct", 10.0))
        dd_mult = float(risk.get("drawdown_size_multiplier", 0.5)) if drawdown_pct >= throttle_at else 1.0

        global_mult = 1.0
        global_regime = None
        global_source = None
        try:
            from services.market_policy_fusion import get_global_market_bias

            bias = get_global_market_bias(
                self.config.raw if hasattr(self.config, "raw") else None
            )
            if bias.get("apply_size_mult") and bias.get("active"):
                global_mult = max(0.0, min(1.5, float(bias.get("size_mult") or 1.0)))
                global_regime = bias.get("regime")
                global_source = bias.get("source")
                if global_mult < 0.999:
                    try:
                        from services.market_context_observability import note_size_cut

                        note_size_cut(mult=global_mult, regime=global_regime)
                    except Exception:
                        pass
        except Exception:
            pass

        coin_bias = 1.0
        coin_entry = "neutral"
        coin_rationale = ""
        social_summary = ""
        try:
            from intelligence.memory.cache import get_coin_profile, get_size_bias

            coin_bias = float(
                get_size_bias(
                    order.symbol,
                    config=self.config.raw if hasattr(self.config, "raw") else None,
                )
            )
            prof = get_coin_profile(
                order.symbol,
                config=self.config.raw if hasattr(self.config, "raw") else None,
            )
            if prof:
                coin_entry = prof.entry_bias or "neutral"
                coin_rationale = (prof.rationale or "")[:120]
                feats = prof.features or {}
                social_summary = str((feats.get("social_summary") or ""))[:80]
        except Exception:
            coin_bias = 1.0
            social_summary = ""

        calendar_mult = 1.0
        session_mult = 1.0
        pm_mult = 1.0
        calendar_risk = ""
        session_risk = ""
        pm_risk = ""
        try:
            from intelligence.macro.snapshot import get_risk_multipliers

            mm = get_risk_multipliers(
                self.config.raw if hasattr(self.config, "raw") else None
            )
            calendar_mult = float(mm.get("calendar_mult") or 1.0)
            session_mult = float(mm.get("session_mult") or 1.0)
            pm_mult = float(mm.get("pm_mult") or 1.0)
            calendar_risk = str(mm.get("calendar_risk") or "")[:100]
            session_risk = str(mm.get("session_risk") or "")[:80]
            pm_risk = str(mm.get("pm_risk") or "")[:80]
        except Exception:
            pass

        total = (
            trust_factor
            * conf_factor
            * atr_factor
            * dd_mult
            * global_mult
            * coin_bias
            * calendar_mult
            * session_mult
            * pm_mult
        )
        max_mult = float(aggression.get("max_position_multiplier", 2.0))
        min_mult = float(risk.get("min_size_multiplier", 0.25))
        md_boost = 1.0
        # Moderate deploy: lift size when not CRASH; extra when cash-rich
        try:
            from risk.moderate_deploy import (
                effective_max_total_multiplier,
                size_boost_for_regime,
            )

            raw_cfg = self.config.raw if hasattr(self.config, "raw") else None
            cash_pct = None
            try:
                eq = float(self._portfolio_equity(order.price, order.symbol) or 0)
                cash = float(self._available_usdt(eq) or 0)
                if eq > 0:
                    cash_pct = 100.0 * cash / eq
            except Exception:
                cash_pct = None
            md_boost = size_boost_for_regime(
                raw_cfg,
                global_regime,
                is_dca=False,
                cash_pct=cash_pct,
            )
            if md_boost > 1.0 and global_mult > 0:
                total *= md_boost
                max_mult = effective_max_total_multiplier(
                    raw_cfg, base_max=max_mult, boost=md_boost
                )
        except Exception:
            md_boost = 1.0
        # Allow CRASH/warmup (0) to zero out size; otherwise keep floor.
        if global_mult <= 0:
            total = 0.0
        else:
            total = max(min_mult, min(max_mult, total))

        factors = {
            "trust_factor": round(trust_factor, 3),
            "conf_factor": round(conf_factor, 3),
            "atr_factor": round(atr_factor, 3),
            "drawdown_pct": round(drawdown_pct, 2),
            "drawdown_multiplier": dd_mult,
            "global_size_mult": round(global_mult, 3),
            "global_regime": global_regime,
            "global_bias_source": global_source,
            "coin_size_bias": round(coin_bias, 3),
            "coin_entry_bias": coin_entry,
            "coin_memory": coin_rationale,
            "coin_social": social_summary,
            "calendar_mult": round(calendar_mult, 3),
            "session_mult": round(session_mult, 3),
            "pm_mult": round(pm_mult, 3),
            "calendar_risk": calendar_risk,
            "session_risk": session_risk,
            "pm_risk": pm_risk,
            "moderate_deploy_mult": round(md_boost, 3),
            "total_multiplier": round(total, 3),
        }
        return base_usdt * total, factors

    def _partial_sell_limits(self, symbol: str, timeframe: str) -> dict:
        params = self.config.strategy_params(symbol, timeframe)
        cmc_cfg = self.config.cmc_config
        risk_cfg = self.config.risk_config
        return {
            "min_position_usdt": float(
                params.get("min_position_usdt_for_partial_sell")
                or params.get("min_position_usdt_for_social_sell")
                or risk_cfg.get("min_position_usdt_for_partial_sell")
                or cmc_cfg.get("min_position_usdt_for_social_sell", 25)
            ),
            "min_notional_usdt": float(
                params.get("min_sell_notional_usdt")
                or risk_cfg.get("min_sell_notional_usdt")
                or cmc_cfg.get("min_sell_notional_usdt", 15)
            ),
            "max_sold_percent": float(
                params.get("block_partial_sell_if_sold_percent_above")
                or params.get("block_social_sell_if_sold_percent_above")
                or risk_cfg.get("block_partial_sell_if_sold_percent_above")
                or cmc_cfg.get("block_social_sell_if_sold_percent_above", 0.75)
            ),
            "dust_sweep_max_position_usdt": float(
                risk_cfg.get("dust_sweep_max_position_usdt", 15)
            ),
            "dust_sweep_sold_percent_min": float(
                risk_cfg.get("dust_sweep_sold_percent_min", 0.70)
            ),
            "dust_sweep_min_remainder_usdt": float(
                risk_cfg.get("dust_sweep_min_remainder_usdt", 10)
            ),
        }

    def _fill_sell_amount_from_open_lot(
        self, order: TradeOrder, timeframe: str
    ) -> TradeOrder:
        """
        If sell amount is missing/zero, size from any open lot for the symbol.

        Guards against analysis TF ≠ position TF (e.g. 4h signal vs 1h volatile lot).
        """
        found = find_open_position_for_symbol(
            order.symbol, preferred_timeframe=timeframe
        )
        if not found:
            return order
        pos_tf, pos = found
        held = float(pos.get("amount", 0) or 0)
        if held <= 0:
            return order
        try:
            fraction = sell_fraction_for_signal(
                order.signal or "SELL_FULL",
                order.symbol,
                pos_tf,
                float(order.price or 0),
                None,
            )
        except Exception:
            fraction = 1.0
        amount = held * float(fraction or 0)
        if amount <= 0:
            return order
        return TradeOrder(
            type=order.type,
            symbol=order.symbol,
            price=order.price,
            amount=amount,
            usdt_amount=order.usdt_amount,
            signal=order.signal,
            source=order.source,
            order_id=order.order_id,
            timestamp=order.timestamp,
            exit_source=getattr(order, "exit_source", None),
            exit_rationale=getattr(order, "exit_rationale", None),
            idempotency_key=getattr(order, "idempotency_key", None),
        )

    def _resolve_sell_order(self, order: TradeOrder, timeframe: str, source: str) -> TradeOrder:
        """Upgrade partial sells to full close when the lot is dust or nearly exited."""
        if source == "manual" or _is_emergency_sell(order.signal):
            return order
        if not _is_partial_sell(order.signal) or order.price <= 0:
            return order

        pos = get_position(order.symbol, timeframe)
        amount = float(pos.get("amount", 0))
        if amount <= 0:
            found = find_open_position_for_symbol(
                order.symbol, preferred_timeframe=timeframe
            )
            if found:
                pos = found[1]
                amount = float(pos.get("amount", 0) or 0)
        if amount <= 0:
            return order

        pos_value = amount * order.price
        sold_pct = float(pos.get("sold_percent", 0))
        limits = self._partial_sell_limits(order.symbol, timeframe)
        notional = float(order.amount) * order.price
        remainder = pos_value - notional

        dust_min = limits["dust_sweep_min_remainder_usdt"]
        if sold_pct >= 0.50:
            dust_min = min(dust_min, 100.0)
        if sold_pct >= 0.70:
            limits["dust_sweep_max_position_usdt"] = max(
                limits["dust_sweep_max_position_usdt"], 500.0,
            )

        sweep = (
            pos_value <= limits["dust_sweep_max_position_usdt"]
            or (
                sold_pct >= limits["dust_sweep_sold_percent_min"]
                and pos_value <= limits["min_position_usdt"]
            )
            or (0 < remainder < dust_min)
        )
        if not sweep:
            return order

        from core.models import MarketContext
        from strategies.registry import resolve_strategy_params
        from strategies.sell_rotation_policy import can_rotation_evict, rotation_config

        sparams = None
        try:
            sparams = resolve_strategy_params(
                {"symbol": order.symbol, "timeframe": timeframe},
                has_position=True,
                frozen_tier=pos.get("strategy_tier"),
            )
        except Exception:
            pass
        cfg = rotation_config(self.config.raw, sparams)
        entry = float(pos.get("average_entry", 0) or order.price)
        market = MarketContext(
            symbol=order.symbol,
            timeframe=timeframe,
            current_price=order.price,
            has_position=True,
            average_entry=entry,
        )
        if not can_rotation_evict(market, pos, cfg):
            return order

        return TradeOrder(
            type=order.type,
            symbol=order.symbol,
            price=order.price,
            amount=amount,
            signal="SELL_FULL",
            source=order.source,
            order_id=order.order_id,
            timestamp=order.timestamp,
        )

    def _partial_sell_blocked(self, order: TradeOrder, timeframe: str, source: str) -> tuple[bool, str]:
        if source == "manual" or _is_emergency_sell(order.signal):
            return False, ""
        if not _is_partial_sell(order.signal) or order.price <= 0:
            return False, ""

        params = self.config.strategy_params(order.symbol, timeframe)
        try:
            from strategies.registry import resolve_strategy_params

            pos = get_position(order.symbol, timeframe)
            params = resolve_strategy_params(
                {"symbol": order.symbol, "timeframe": timeframe},
                has_position=float(pos.get("amount", 0) or 0) > 0,
                frozen_tier=pos.get("strategy_tier"),
            )
        except Exception:
            pass
        try:
            from strategies.exit_ladder import ladder_enabled

            if ladder_enabled(params):
                return False, ""
        except Exception:
            pass

        limits = self._partial_sell_limits(order.symbol, timeframe)
        pos = get_position(order.symbol, timeframe)
        pos_value = float(pos.get("amount", 0)) * order.price
        notional = float(order.amount) * order.price
        sold_pct = float(pos.get("sold_percent", 0))

        if pos_value < limits["min_position_usdt"]:
            return True, (
                f"Partial sell blocked: position ${pos_value:.2f} "
                f"below minimum ${limits['min_position_usdt']:.0f} "
                f"(use full close or manual sell)"
            )
        if notional < limits["min_notional_usdt"]:
            return True, (
                f"Partial sell blocked: notional ${notional:.2f} "
                f"below minimum ${limits['min_notional_usdt']:.0f}"
            )
        if sold_pct >= limits["max_sold_percent"]:
            return True, (
                f"Partial sell blocked: already sold {sold_pct * 100:.0f}% of position "
                f"(max {limits['max_sold_percent'] * 100:.0f}%)"
            )
        return False, ""

    def _social_sell_blocked(self, order: TradeOrder, timeframe: str, source: str) -> tuple[bool, str]:
        """Backward-compatible alias for tests and callers."""
        return self._partial_sell_blocked(order, timeframe, source)

    def _trade_cooldown_blocked(self, order: TradeOrder, timeframe: str, source: str = "auto") -> tuple:
        if source == "manual":
            return False, ""
        signal = order.signal or ""
        if order.type == "SELL" and signal in ("SELL_STOP_FULL", "SELL_STOP_PARTIAL", "SELL_FULL"):
            return False, ""
        if order.type == "SELL" and "FULL" in signal:
            return False, ""

        pos = get_position(order.symbol, timeframe)
        params = self.config.strategy_params(order.symbol, timeframe)
        defaults = self.config.dry_run_defaults if is_dry_run_enhanced(self.config.raw) else {}
        cmc_cfg = self.config.cmc_config

        if order.type == "SELL" and source == "cmc":
            last_cmc = pos.get("last_cmc_sell_at")
            if last_cmc:
                try:
                    last_ts = datetime.fromisoformat(str(last_cmc).replace("Z", ""))
                    min_hours = float(
                        params.get("cmc_min_hours_between_sells")
                        or defaults.get("cmc_min_hours_between_sells")
                        or cmc_cfg.get("cmc_min_hours_between_sells", 6)
                    )
                    elapsed = (datetime.now() - last_ts).total_seconds() / 3600.0
                    if elapsed < min_hours:
                        return True, (
                            f"CMC sell cooldown: {elapsed:.1f}h since last CMC sell "
                            f"(min {min_hours:.1f}h)"
                        )
                except Exception:
                    pass

        last_at = pos.get("last_trade_at")
        last_type = pos.get("last_trade_type")
        if not last_at:
            return False, ""

        try:
            last_ts = datetime.fromisoformat(str(last_at).replace("Z", ""))
        except Exception:
            return False, ""

        pos_amount = float(pos.get("amount", 0) or 0)
        is_dca = order.signal == "BUY_DCA" or source in ("dca", "dca_recovery")

        if order.type == "BUY" and source == "cmc":
            blocked, reason = self._trending_position_cap_blocked(order, timeframe)
            if blocked:
                return True, reason

        if order.type == "BUY" and last_type == "SELL" and not (is_dca and pos_amount > 0):
            blocked, reason = self._rebuy_after_sell_blocked(
                order, timeframe, source, last_ts, pos, params, defaults
            )
            if blocked:
                return True, reason

        if order.type == "BUY" and is_dca and pos_amount > 0:
            blocked, reason = self._dca_interval_blocked(pos, params, defaults, source=source)
            if blocked:
                return True, reason
            return False, ""

        if last_type != order.type:
            return False, ""

        if order.type == "BUY":
            min_hours = float(
                params.get("min_hours_between_buys")
                or defaults.get("min_hours_between_buys")
                or defaults.get("trade_cooldown_hours")
                or self.config.trade_cooldown_hours
            )
        else:
            min_hours = float(
                params.get("min_hours_between_sells")
                or defaults.get("min_hours_between_sells")
                or defaults.get("trade_cooldown_hours")
                or self.config.trade_cooldown_hours
            )

        elapsed = (datetime.now() - last_ts).total_seconds() / 3600.0
        if elapsed < min_hours:
            return True, (
                f"Trade cooldown: {elapsed:.1f}h since last {order.type} "
                f"(min {min_hours:.1f}h)"
            )
        return False, ""

    def _rebuy_after_sell_blocked(
        self,
        order: TradeOrder,
        timeframe: str,
        source: str,
        last_ts: datetime,
        pos: dict,
        params: dict,
        defaults: dict,
    ) -> tuple[bool, str]:
        if source == "manual":
            return False, ""

        from risk.rebuy_cooldown import (
            format_rebuy_reject_message,
            prepare_dynamic_config,
            rebuy_cooldown_config,
            rebuy_cooldown_enabled,
            resolve_rebuy_cooldown_hours,
            signal_quality_from_confidence,
        )

        arch = self.config.architecture_config
        last_signal = str(pos.get("last_sell_signal") or "")
        fallback_hours = float(
            params.get("min_hours_after_sell_before_rebuy")
            or defaults.get("min_hours_after_sell_before_rebuy")
            or self.config.min_hours_after_sell_before_rebuy
        )
        risk_cfg = self.config.risk_config or {}
        try:
            elapsed = (datetime.now() - last_ts).total_seconds() / 3600.0
        except TypeError:
            # aware/naive mix — treat as no cooldown rather than crash
            return False, ""

        if not rebuy_cooldown_enabled(risk_cfg, self.config.raw):
            return self._legacy_rebuy_after_sell_blocked(
                last_signal, arch, fallback_hours, elapsed
            )

        cfg = prepare_dynamic_config(
            rebuy_cooldown_config(risk_cfg, self.config.raw), arch
        )
        regime = None
        try:
            from services.market_policy_fusion import get_global_market_bias

            regime = (get_global_market_bias() or {}).get("regime")
        except Exception:
            pass
        profile = None
        try:
            from intelligence.memory.cache import get_coin_profile

            profile = get_coin_profile(order.symbol, config=self.config.raw)
        except Exception:
            pass

        last_exit = (
            pos.get("last_exit_source")
            or pos.get("exit_source")
            or pos.get("last_sell_exit_source")
        )
        vol_tier = pos.get("strategy_tier") or params.get("volatility_tier")
        result = resolve_rebuy_cooldown_hours(
            regime=regime,
            last_sell_signal=last_signal,
            last_exit_source=str(last_exit) if last_exit else None,
            order_signal=str(getattr(order, "signal", None) or "") or None,
            volatility_tier=str(vol_tier) if vol_tier else None,
            signal_quality=signal_quality_from_confidence(
                getattr(order, "confidence", None)
            ),
            profile=profile,
            config=cfg,
            fallback_hours=fallback_hours,
        )
        if cfg.get("log"):
            try:
                from logger import log

                log(
                    f"rebuy_cooldown symbol={order.symbol} hours={result.hours:.2f} "
                    f"elapsed={elapsed:.2f} regime={regime} "
                    f"factors={result.factors} reasons={result.reasons}",
                    "DEBUG",
                )
            except Exception:
                pass
        if result.hours <= 0:
            return False, ""
        if elapsed < result.hours:
            return True, format_rebuy_reject_message(elapsed_h=elapsed, result=result)
        return False, ""

    def _legacy_rebuy_after_sell_blocked(
        self,
        last_signal: str,
        arch: dict,
        fallback_hours: float,
        elapsed: float,
    ) -> tuple[bool, str]:
        """Flat hours when risk.rebuy_cooldown.enabled is false."""
        min_hours = fallback_hours
        stop_sell = _is_stop_loss_sell(last_signal)
        if arch.get("block_rebuy_if_last_sell_was_stop", False) and stop_sell:
            min_hours = float(arch.get("rebuy_after_stop_loss_hours", 24.0))
        if min_hours <= 0:
            return False, ""
        if elapsed < min_hours:
            label = "Stop-loss rebuy cooldown" if stop_sell else "Rebuy cooldown"
            return True, (
                f"{label}: {elapsed:.1f}h since last SELL "
                f"(min {min_hours:.1f}h after sell)"
            )
        return False, ""

    def _trending_position_cap_blocked(
        self,
        order: TradeOrder,
        timeframe: str,
    ) -> tuple[bool, str]:
        tw = self.config.trending_watchlist_config
        cap = int(tw.get("max_open_from_trending", 0))
        if cap <= 0 or order.type != "BUY":
            return False, ""
        from data_manager import load_cmc_trending_overlay, trending_watchlist_live_enabled
        from strategies.positions import list_active_positions

        if not trending_watchlist_live_enabled(self.config.raw):
            return False, ""
        trending_syms = {
            c.get("symbol") for c in load_cmc_trending_overlay().get("coins", [])
        }
        trending_open = sum(
            1 for pos in list_active_positions() if pos.get("symbol") in trending_syms
        )
        if trending_open >= cap:
            return True, (
                f"Trending position cap: {trending_open}/{cap} open from CMC trending"
            )
        return False, ""

    def _dca_interval_blocked(
        self,
        pos: dict,
        params: dict,
        defaults: dict,
        *,
        source: str = "dca",
    ) -> tuple[bool, str]:
        from strategies.dca import _hours_since_last_dca

        dca_cfg = dict(params.get("dca") or {})
        interval_hours = float(dca_cfg.get("interval_hours", 12))
        elapsed = _hours_since_last_dca(pos)
        if elapsed is None:
            return False, ""
        if elapsed < interval_hours:
            return True, (
                f"DCA interval: {elapsed:.1f}h since last DCA "
                f"(min {interval_hours:.1f}h)"
            )
        return False, ""

    @staticmethod
    def _order_side(order: dict) -> str:
        side = str(order.get("side") or order.get("type") or "").lower()
        if side in ("buy", "sell"):
            return side
        return ""

    def _daily_trades_count(
        self,
        side: str | None = None,
        *,
        dca_only: bool | None = None,
    ) -> int:
        """Count filled ledger orders in the last 24h (optional buy/sell + DCA filter)."""
        return sum(1 for _ in self._iter_daily_filled_orders(side=side, dca_only=dca_only))

    def _iter_daily_filled_orders(
        self,
        *,
        side: str | None = None,
        dca_only: bool | None = None,
    ):
        from data_manager import load_orders, resolve_ledger_scope

        cutoff = datetime.now() - timedelta(hours=24)
        scope = resolve_ledger_scope(self.config.trading_mode)
        want = (side or "").lower() or None
        for order in load_orders(scope).get("orders", []):
            if order.get("status") != "filled":
                continue
            order_side = self._order_side(order)
            if want and order_side != want:
                continue
            is_dca = self._order_is_dca(order)
            if dca_only is True and not is_dca:
                continue
            if dca_only is False and is_dca:
                continue
            ts_raw = (
                order.get("timestamps", {}).get("filled")
                or order.get("timestamps", {}).get("created")
                or ""
            )
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", ""))
            except Exception:
                continue
            if ts >= cutoff:
                yield order

    def _daily_buys_count(self, *, dca_only: bool | None = None) -> int:
        return self._daily_trades_count("buy", dca_only=dca_only)

    def _daily_dca_buys_count(self) -> int:
        return self._daily_buys_count(dca_only=True)

    def _daily_dca_usdt_sum(self) -> float:
        return sum(self._filled_order_usdt(order) for order in self._iter_daily_filled_orders(
            side="buy", dca_only=True,
        ))

    def _daily_sells_count(self) -> int:
        return self._daily_trades_count("sell")