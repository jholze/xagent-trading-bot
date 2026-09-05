"""I/O adapter for slot eviction: book scan, RAG, rate limits, live sell.

Called from RiskManager max_open path. Missing data must not look like good data.
"""

from __future__ import annotations

from dataclasses import replace
import threading
import time
from datetime import datetime, timezone
from typing import Any

from risk.slot_eviction import (
    EXIT_SOURCE_SLOT_EVICT,
    EvictionPlan,
    VictimCandidate,
    eviction_mode,
    format_eviction_reject_suffix,
    memory_keep_score,
    plan_slot_eviction,
    score_entry_demand,
    slot_eviction_section,
)
from risk.slot_eviction_rag import default_retrieve_fn, enrich_keeps_with_rag

_LOCK = threading.Lock()
_EVICT_TS: list[float] = []
_SYMBOL_COOLDOWN: dict[str, float] = {}


def _now() -> float:
    return time.time()


def _warn(msg: str) -> None:
    try:
        from logger import log

        log(msg, "WARNING")
    except Exception:
        pass


def _is_slot_evict_order(order: dict | None) -> bool:
    if not isinstance(order, dict):
        return False
    return str(order.get("exit_source") or "") == EXIT_SOURCE_SLOT_EVICT


def _slot_evict_orders_from_ledger(*, hours: float | None = None) -> list[dict]:
    """Filled slot-eviction sells from the orders ledger (survives restart)."""
    try:
        from services.order_service import ORDERS_LIST_HARD_CAP, OrderService, order_event_ts

        svc = OrderService()
        if hours is not None and hours <= 24:
            orders = svc.list_day_filled_all()
        else:
            orders, _ = svc.list_orders(
                trade_book_only=True,
                hours=hours,
                per_page=ORDERS_LIST_HARD_CAP,
            )
        out: list[dict] = []
        now = datetime.now()
        for o in orders or []:
            if not _is_slot_evict_order(o):
                continue
            if hours is not None:
                ts = order_event_ts(o)
                if ts is None:
                    continue
                ts_naive = ts.replace(tzinfo=None) if getattr(ts, "tzinfo", None) else ts
                try:
                    age_s = (now - ts_naive).total_seconds()
                except Exception:
                    continue
                if age_s > float(hours) * 3600.0:
                    continue
            out.append(o)
        return out
    except Exception:
        return []


def check_rate_limits(risk_config: dict | None) -> tuple[bool, str]:
    cfg = slot_eviction_section(risk_config)
    max_h = int(cfg.get("max_evictions_per_hour", 2) or 2)
    max_d = int(cfg.get("max_evictions_per_day", 8) or 8)
    now = _now()
    day_orders = _slot_evict_orders_from_ledger(hours=24)
    hour_orders = _slot_evict_orders_from_ledger(hours=1)
    with _LOCK:
        global _EVICT_TS
        _EVICT_TS = [t for t in _EVICT_TS if now - t < 86400]
        ram_hour = [t for t in _EVICT_TS if now - t < 3600]
        n_hour = max(len(hour_orders), len(ram_hour))
        n_day = max(len(day_orders), len(_EVICT_TS))
    if max_h > 0 and n_hour >= max_h:
        return True, "max_evictions_per_hour"
    if max_d > 0 and n_day >= max_d:
        return True, "max_evictions_per_day"
    return False, ""


def note_eviction_executed(symbol: str, risk_config: dict | None = None) -> None:
    cfg = slot_eviction_section(risk_config)
    cool_h = float(cfg.get("symbol_cooldown_hours", 24) or 24)
    now = _now()
    with _LOCK:
        _EVICT_TS.append(now)
        if cool_h > 0:
            _SYMBOL_COOLDOWN[symbol] = now + cool_h * 3600


def symbol_on_cooldown(symbol: str, risk_config: dict | None = None) -> bool:
    cfg = slot_eviction_section(risk_config)
    cool_h = float(cfg.get("symbol_cooldown_hours", 24) or 24)
    if cool_h > 0:
        for o in _slot_evict_orders_from_ledger(hours=cool_h):
            if str(o.get("symbol") or "") == symbol:
                return True
    with _LOCK:
        until = _SYMBOL_COOLDOWN.get(symbol, 0)
    return _now() < until


def set_pending_entry(entry_symbol: str, victim: str, risk_config: dict | None = None) -> None:
    """No-op. Same-cycle handoff is evaluate() re-count, not a RAM pending map."""
    return None


def reset_rate_limits_for_tests() -> None:
    with _LOCK:
        _EVICT_TS.clear()
        _SYMBOL_COOLDOWN.clear()


def _hours_since(iso_ts: str | None) -> float | None:
    if not iso_ts:
        return None
    try:
        raw = str(iso_ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0)
    except Exception:
        return None


def _peak_gain_pct(entry: float, peak: float, price: float) -> float:
    if entry <= 0:
        return 0.0
    hi = max(float(peak or 0), float(price or 0), entry)
    return (hi / entry - 1.0) * 100.0


def build_victim_candidates(
    *,
    config_raw: dict | None,
    risk_config: dict | None,
    entry_symbol: str,
    get_profile=None,
    retrieve_fn=None,
    prices: dict[str, float] | None = None,
) -> list[VictimCandidate]:
    """Scan open positions → full-slot candidates with keep scores."""
    from strategies.positions import list_active_positions, position_notional_usdt
    from strategies.sell_rotation_policy import is_tail_position, rotation_config

    rot = rotation_config(config_raw)
    positions = list_active_positions()
    cfg = slot_eviction_section(risk_config)
    get_profile = get_profile or (lambda s: None)

    # First pass: profiles + keep_profile
    keep_p: dict[str, float] = {}
    meta: list[dict] = []
    for pos in positions:
        sym = pos.get("symbol") or ""
        if not sym or sym == entry_symbol:
            continue
        if is_tail_position(pos, rot):
            continue  # tails don't free full slots
        amount = float(pos.get("amount", 0) or 0)
        if amount <= 0:
            continue
        if symbol_on_cooldown(sym, risk_config):
            continue
        entry = float(pos.get("average_entry") or pos.get("entry_price") or 0)
        try:
            price = float((prices or {}).get(sym) or 0)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            # No positive live price → drop; never fall back to entry / $1.
            continue
        gain = ((price / entry) - 1.0) * 100.0 if entry > 0 else 0.0
        peak = float(pos.get("recent_high") or 0)
        peak_g = _peak_gain_pct(entry, peak, price)
        sold = float(pos.get("sold_percent", 0) or 0)
        notional = position_notional_usdt(pos) if callable(position_notional_usdt) else amount * price
        try:
            notional = float(notional)
        except Exception:
            notional = amount * price
        idle = _hours_since(pos.get("last_trade_at") or pos.get("updated_at"))
        age = _hours_since(pos.get("first_buy_at") or pos.get("entry_at") or pos.get("opened_at"))
        if idle is None or age is None:
            continue
        tf = str(pos.get("timeframe") or "4h")
        prof = get_profile(sym)
        kp = memory_keep_score(prof, risk_config=risk_config)
        keep_p[sym] = kp
        prefer = str(getattr(prof, "entry_bias", "") or "").lower() == "prefer"
        trail_armed = False
        try:
            from strategies.sell_rotation_policy import trail_replacement_armed
            from core.models import MarketContext

            mc = MarketContext(
                symbol=sym,
                timeframe=tf,
                current_price=price,
                average_entry=entry,
                has_position=True,
            )
            trail_armed = bool(trail_replacement_armed(None, mc, pos))
            # also protect if peak already high even without trail cfg
            if peak_g >= float(cfg.get("protect_peak_gain_pct", 12) or 12):
                trail_armed = trail_armed or peak_g >= 15.0
        except Exception:
            trail_armed = peak_g >= float(cfg.get("protect_peak_gain_pct", 12) or 12)

        realized = float(pos.get("realized_pnl", 0) or 0)
        rot_ok = gain >= float(rot.get("evict_min_gain_pct", 0) or 0) or realized > 0
        class_name = "A" if gain >= 0 else "B"
        feats = getattr(prof, "features", None) if prof else None
        if isinstance(feats, dict) and (feats.get("structure_risk") or feats.get("hard_negative")):
            if gain < 0:
                class_name = "C"

        meta.append(
            {
                "symbol": sym,
                "timeframe": tf,
                "gain_pct": gain,
                "peak_gain_pct": peak_g,
                "idle_hours": idle,
                "sold_percent": sold,
                "notional_usdt": notional,
                "amount": amount,
                "price": price,
                "keep_profile": kp,
                "trail_armed": trail_armed,
                "rotation_eligible": rot_ok,
                "prefer": prefer,
                "age_hours": age,
                "class_name": class_name,
            }
        )

    # RAG enrich
    symbols = [m["symbol"] for m in meta]
    # include entry for swap scoring later
    if entry_symbol and entry_symbol not in keep_p:
        ep = get_profile(entry_symbol)
        keep_p[entry_symbol] = memory_keep_score(ep, risk_config=risk_config)
        symbols = [entry_symbol] + symbols

    rag_mode = str((cfg.get("rag") or {}).get("mode") or "off").lower()
    apply = bool((cfg.get("rag") or {}).get("apply_to_plan", False))
    rfn = retrieve_fn
    if rfn is None and rag_mode not in ("off", ""):
        rfn = default_retrieve_fn(config_raw)

    enriched = enrich_keeps_with_rag(
        symbols,
        keep_p,
        risk_config=risk_config,
        retrieve_fn=rfn,
    )

    cands: list[VictimCandidate] = []
    for m in meta:
        sym = m["symbol"]
        en = enriched.get(sym) or {}
        kp = float(en.get("keep_profile", m["keep_profile"]))
        kr = float(en.get("keep_rag", kp))
        keep_final = kr if (apply and rag_mode not in ("off", "")) else kp
        cands.append(
            VictimCandidate(
                symbol=sym,
                timeframe=m["timeframe"],
                gain_pct=m["gain_pct"],
                peak_gain_pct=m["peak_gain_pct"],
                idle_hours=m["idle_hours"],
                sold_percent=m["sold_percent"],
                notional_usdt=m["notional_usdt"],
                amount=m["amount"],
                price=m["price"],
                keep_profile=kp,
                keep_rag=kr,
                keep_final=keep_final,
                trail_armed=m["trail_armed"],
                rotation_eligible=m["rotation_eligible"],
                prefer=m["prefer"],
                age_hours=m["age_hours"],
                class_name=m["class_name"],
            )
        )
    # entry keep as synthetic for plan swap gate
    if entry_symbol in enriched or entry_symbol in keep_p:
        en = enriched.get(entry_symbol) or {}
        kp = float(en.get("keep_profile", keep_p.get(entry_symbol, 0.55)))
        kr = float(en.get("keep_rag", kp))
        keep_final = kr if (apply and rag_mode not in ("off", "")) else kp
        cands.append(
            VictimCandidate(
                symbol=entry_symbol,
                timeframe="4h",
                gain_pct=0.0,
                peak_gain_pct=0.0,
                idle_hours=0.0,
                sold_percent=0.0,
                notional_usdt=0.0,
                amount=0.0,
                price=0.0,
                keep_profile=kp,
                keep_rag=kr,
                keep_final=keep_final,
                trail_armed=False,
                rotation_eligible=False,
                prefer=False,
                age_hours=0.0,
                class_name="ENTRY",
                veto="entry_self",
            )
        )
    return cands


def plan_for_blocked_entry(
    *,
    order_symbol: str,
    source: str,
    free_full_slots: int,
    config_raw: dict | None,
    risk_config: dict | None,
    spike_multiple: float = 0.0,
    venue_ok: bool = True,
    soft_block: bool = False,
    structure_risk: bool = False,
    block_buys: bool = False,
    regime: str | None = None,
    spendable_ok: bool = True,
    warmup_active: bool = False,
    get_profile=None,
    retrieve_fn=None,
    prices: dict[str, float] | None = None,
) -> EvictionPlan:
    demand = score_entry_demand(
        symbol=order_symbol,
        source=source,
        free_full_slots=free_full_slots,
        spike_multiple=spike_multiple,
        venue_ok=venue_ok,
        soft_block=soft_block,
        structure_risk=structure_risk,
        block_buys=block_buys,
        regime=regime,
        spendable_ok=spendable_ok,
        risk_config=risk_config,
    )
    blocked, reason = check_rate_limits(risk_config)
    cands: list[VictimCandidate] = []
    if demand.passed and eviction_mode(risk_config) != "off":
        cands = build_victim_candidates(
            config_raw=config_raw,
            risk_config=risk_config,
            entry_symbol=order_symbol,
            get_profile=get_profile,
            retrieve_fn=retrieve_fn,
            prices=prices,
        )
    plan = plan_slot_eviction(
        demand=demand,
        candidates=cands,
        risk_config=risk_config,
        rate_limit_blocked=blocked,
        rate_limit_reason=reason,
        warmup_active=warmup_active,
        config_raw=config_raw,
    )
    if (
        demand.passed
        and eviction_mode(risk_config) != "off"
        and not plan.ok
        and plan.veto_reason in ("", "no_candidate")
        and not any(c.symbol != order_symbol for c in cands)
        and prices is not None
        and not any(float(v or 0) > 0 for v in prices.values())
    ):
        return EvictionPlan(
            ok=False,
            mode=plan.mode,
            entry_symbol=order_symbol,
            demand_score=demand.score,
            veto_reason="no_positive_price",
            reason_codes=("no_positive_price",),
            candidates=plan.candidates,
            ab=plan.ab,
        )
    if plan.ok and float(plan.victim_price or 0) <= 0:
        return EvictionPlan(
            ok=False,
            mode=plan.mode,
            entry_symbol=order_symbol,
            victim_symbol=plan.victim_symbol,
            demand_score=demand.score,
            veto_reason="no_positive_price",
            reason_codes=("no_positive_price",),
            candidates=plan.candidates,
            ab=plan.ab,
            victim_price=0.0,
        )
    return plan


def execute_eviction_sell(
    plan: EvictionPlan,
    *,
    config=None,
    trading=None,
) -> dict[str, Any]:
    """Place SELL via TradingService. Returns {ok, message, ...}."""
    if not plan.ok or plan.mode != "live" or not plan.victim_symbol:
        return {"ok": False, "message": "no live plan", "skipped": True}
    try:
        from core.models import TradeOrder
        from services.trading_service import TradingService
        from strategies.exit_attribution import truncate_rationale
        from strategies.positions import get_position

        svc = trading or TradingService(config=config)
        tf = plan.victim_timeframe or "4h"
        pos = get_position(plan.victim_symbol, tf)
        amount = float(pos.get("amount", 0) or 0)
        if amount <= 0:
            return {"ok": False, "message": "no amount"}
        frac = float(plan.sell_fraction or 0.4)
        if frac <= 0:
            frac = 0.4
        if frac > 1:
            frac = 1.0
        sell_amt = amount * frac
        try:
            price = float(plan.victim_price or 0)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            return {
                "ok": False,
                "message": "no positive price",
                "code": "slot_eviction_no_price",
            }
        signal = "SELL_FULL" if frac >= 0.99 else "SELL_PARTIAL"
        order = TradeOrder(
            type="SELL",
            symbol=plan.victim_symbol,
            price=price,
            amount=sell_amt,
            usdt_amount=sell_amt * price,
            signal=signal,
            source="auto",
            exit_source=plan.exit_source or EXIT_SOURCE_SLOT_EVICT,
            exit_rationale=truncate_rationale(plan.exit_rationale or ""),
        )
        result = svc.execute_order(order, tf, source="auto")
        executed = bool(getattr(result, "executed", False) or getattr(result, "ok", False))
        msg = str(getattr(result, "message", "") or result)
        if executed:
            note_eviction_executed(plan.victim_symbol)
        return {"ok": executed, "message": msg, "result": result}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def resolve_spendable_ok_for_entry(
    *,
    order,
    risk_manager=None,
    risk_config: dict | None = None,
    spendable_override: bool | None = None,
) -> bool:
    """Must-gate: entry must be fundable after eviction (P4)."""
    if spendable_override is not None:
        return bool(spendable_override)
    cfg = slot_eviction_section(risk_config)
    if not cfg.get("require_spendable_for_entry", True):
        return True
    min_trade = float((risk_config or {}).get("min_trade_usdt", 100) or 100)
    try:
        planned = float(getattr(order, "usdt_amount", 0) or 0)
    except (TypeError, ValueError):
        planned = 0.0
    need = max(min_trade, planned) if planned > 0 else min_trade
    if risk_manager is not None:
        try:
            price = float(getattr(order, "price", 0) or 0)
            sym = getattr(order, "symbol", None)
            eq = float(risk_manager._portfolio_equity(price, sym))
            sp = float(risk_manager._spendable_usdt(eq, is_dca=False))
            return sp >= need
        except Exception:
            # Cannot verify spendable → do not free a slot for an unfundable entry
            return False
    return False


def _fetch_victim_prices(entry_symbol: str) -> dict[str, float]:
    """Fetch mark prices once for open lots (never fall back to entry / $1)."""
    try:
        from price_fetcher import get_prices_batch
        from strategies.positions import list_active_positions

        syms: list[str] = []
        for pos in list_active_positions() or []:
            sym = str(pos.get("symbol") or "")
            if sym and sym != entry_symbol:
                syms.append(sym)
        if not syms:
            return {}
        raw = get_prices_batch(syms) or {}
        out: dict[str, float] = {}
        for sym, val in raw.items():
            try:
                px = float(val or 0)
            except (TypeError, ValueError):
                continue
            if px > 0:
                out[str(sym)] = px
        return out
    except Exception:
        return {}


def try_slot_eviction_on_max_open(
    *,
    order,
    source: str,
    free_full_slots: int,
    config,
    risk_config: dict | None,
    config_raw: dict | None = None,
    spike_multiple: float = 0.0,
    risk_manager=None,
    spendable_ok: bool | None = None,
) -> tuple[EvictionPlan | None, str]:
    """Full path for RiskManager. Returns (plan, message_suffix)."""
    mode = eviction_mode(risk_config)
    if mode == "off":
        return None, ""

    raw = config_raw
    if raw is None and config is not None and hasattr(config, "raw"):
        raw = config.raw

    # Fusion / Memory: missing data must not look like good data.
    try:
        from services.market_policy_fusion import get_global_market_bias

        bias = get_global_market_bias(raw) or {}
        block_buys = bool(bias.get("block_buys"))
        regime = str(bias.get("regime") or "NEUTRAL")
    except Exception as exc:
        _warn(f"slot_eviction abort: fusion lookup failed: {exc}")
        return None, ""

    try:
        from intelligence.memory.cache import get_coin_profile, get_entry_bias

        soft_block = get_entry_bias(order.symbol, config=raw) == "soft_block"
        prof = get_coin_profile(order.symbol, config=raw)
        structure_risk = False
        if prof and isinstance(getattr(prof, "features", None), dict):
            structure_risk = bool(
                prof.features.get("structure_risk") or prof.features.get("hard_negative")
            )
    except Exception as exc:
        _warn(f"slot_eviction abort: memory lookup failed: {exc}")
        return None, ""

    spendable_ok = resolve_spendable_ok_for_entry(
        order=order,
        risk_manager=risk_manager,
        risk_config=risk_config,
        spendable_override=spendable_ok,
    )

    warmup = False
    try:
        from services.market_oracle_store import process_uptime_sec

        up = float(process_uptime_sec())
        cap_cfg = {}
        if isinstance(risk_config, dict) and isinstance(
            risk_config.get("position_capacity"), dict
        ):
            cap_cfg = risk_config["position_capacity"]
        # Source of truth: risk.position_capacity.restart_warmup_min (exists).
        warm_min = float(cap_cfg.get("restart_warmup_min", 15) or 0)
        if slot_eviction_section(risk_config).get("skip_if_warmup", True):
            if warm_min > 0 and up < warm_min * 60:
                warmup = True
    except Exception:
        pass

    def _gp(sym: str):
        from intelligence.memory.cache import get_coin_profile

        return get_coin_profile(sym, config=raw)

    # spike from order if present
    spike = spike_multiple
    if spike <= 0 and getattr(order, "entry_15m_vol_ratio", None):
        try:
            spike = float(order.entry_15m_vol_ratio or 0)
        except Exception:
            spike = 0.0

    prices = _fetch_victim_prices(getattr(order, "symbol", "") or "")

    try:
        plan = plan_for_blocked_entry(
            order_symbol=order.symbol,
            source=source,
            free_full_slots=free_full_slots,
            config_raw=raw,
            risk_config=risk_config,
            spike_multiple=spike,
            venue_ok=True,  # venue already checked earlier in evaluate
            soft_block=soft_block,
            structure_risk=structure_risk,
            block_buys=block_buys,
            regime=regime,
            spendable_ok=spendable_ok,
            warmup_active=warmup,
            get_profile=_gp,
            prices=prices,
        )
    except Exception as exc:
        _warn(f"slot_eviction abort: profile lookup failed: {exc}")
        return None, ""

    suffix = format_eviction_reject_suffix(plan)
    if plan and plan.ok and plan.mode == "live":
        try:
            from logger import log

            log(
                f"slot_eviction LIVE plan victim={plan.victim_symbol} for={plan.entry_symbol} "
                f"ab={plan.ab}",
                "INFO",
            )
        except Exception:
            pass
        exec_res = execute_eviction_sell(plan, config=config)
        if exec_res.get("ok"):
            plan = replace(plan, sell_executed=True)  # structured signal for RiskManager (#300 audit)
            suffix = (
                f" · eviction LIVE {plan.victim_symbol}→{plan.entry_symbol} "
                f"({plan.action})"
            )
        elif exec_res.get("code") == "slot_eviction_no_price":
            plan = EvictionPlan(
                ok=False,
                mode=plan.mode,
                entry_symbol=plan.entry_symbol,
                victim_symbol=plan.victim_symbol,
                demand_score=plan.demand_score,
                veto_reason="no_positive_price",
                reason_codes=("no_positive_price",),
                candidates=plan.candidates,
                ab=plan.ab,
            )
            suffix = format_eviction_reject_suffix(plan)
        else:
            suffix = (
                f" · eviction plan {plan.victim_symbol} sell_failed: "
                f"{exec_res.get('message', '')[:80]}"
            )
    elif plan and plan.ok and plan.mode == "shadow":
        try:
            from logger import log

            log(
                f"slot_eviction SHADOW would_evict {plan.victim_symbol} for {plan.entry_symbol} "
                f"ab={plan.ab}",
                "INFO",
            )
        except Exception:
            pass
    return plan, suffix
