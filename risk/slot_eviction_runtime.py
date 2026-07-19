"""I/O adapter for slot eviction: book scan, RAG, rate limits, live sell.

Called from RiskManager max_open path. Fail-open on errors.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from risk.slot_eviction import (
    EXIT_SOURCE_SLOT_EVICT,
    EntryDemand,
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
_PENDING_ENTRY: dict[str, dict[str, Any]] = {}


def _now() -> float:
    return time.time()


def check_rate_limits(risk_config: dict | None) -> tuple[bool, str]:
    cfg = slot_eviction_section(risk_config)
    max_h = int(cfg.get("max_evictions_per_hour", 2) or 2)
    max_d = int(cfg.get("max_evictions_per_day", 8) or 8)
    now = _now()
    with _LOCK:
        global _EVICT_TS
        _EVICT_TS = [t for t in _EVICT_TS if now - t < 86400]
        hour = [t for t in _EVICT_TS if now - t < 3600]
        if max_h > 0 and len(hour) >= max_h:
            return True, "max_evictions_per_hour"
        if max_d > 0 and len(_EVICT_TS) >= max_d:
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


def symbol_on_cooldown(symbol: str) -> bool:
    with _LOCK:
        until = _SYMBOL_COOLDOWN.get(symbol, 0)
    return _now() < until


def set_pending_entry(entry_symbol: str, victim: str, risk_config: dict | None = None) -> None:
    cfg = slot_eviction_section(risk_config)
    ttl = float(cfg.get("pending_entry_ttl_min", 30) or 30) * 60
    with _LOCK:
        _PENDING_ENTRY[entry_symbol] = {
            "victim": victim,
            "expires_at": _now() + ttl,
            "set_at": _now(),
        }


def get_pending_entry(entry_symbol: str) -> dict[str, Any] | None:
    with _LOCK:
        p = _PENDING_ENTRY.get(entry_symbol)
        if not p:
            return None
        if _now() > float(p.get("expires_at") or 0):
            _PENDING_ENTRY.pop(entry_symbol, None)
            return None
        return dict(p)


def clear_pending_entry(entry_symbol: str) -> None:
    with _LOCK:
        _PENDING_ENTRY.pop(entry_symbol, None)


def reset_rate_limits_for_tests() -> None:
    with _LOCK:
        _EVICT_TS.clear()
        _SYMBOL_COOLDOWN.clear()
        _PENDING_ENTRY.clear()


def _hours_since(iso_ts: str | None) -> float:
    if not iso_ts:
        return 999.0
    try:
        raw = str(iso_ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0)
    except Exception:
        return 999.0


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
        if symbol_on_cooldown(sym):
            continue
        entry = float(pos.get("average_entry") or pos.get("entry_price") or 0)
        price = float((prices or {}).get(sym) or pos.get("mark_price") or entry or 0)
        if price <= 0 and entry > 0:
            price = entry
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
        tf = str(pos.get("timeframe") or "4h")
        prof = None
        try:
            prof = get_profile(sym)
        except Exception:
            prof = None
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
        try:
            ep = get_profile(entry_symbol)
            keep_p[entry_symbol] = memory_keep_score(ep, risk_config=risk_config)
        except Exception:
            keep_p[entry_symbol] = 0.55
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
    cands = []
    if demand.passed and eviction_mode(risk_config) != "off":
        try:
            cands = build_victim_candidates(
                config_raw=config_raw,
                risk_config=risk_config,
                entry_symbol=order_symbol,
                get_profile=get_profile,
                retrieve_fn=retrieve_fn,
                prices=prices,
            )
        except Exception:
            cands = []
    return plan_slot_eviction(
        demand=demand,
        candidates=cands,
        risk_config=risk_config,
        rate_limit_blocked=blocked,
        rate_limit_reason=reason,
        warmup_active=warmup_active,
    )


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
        price = float(pos.get("mark_price") or pos.get("average_entry") or 0) or 1.0
        # Prefer live mark from market if available
        try:
            if hasattr(svc, "market") and svc.market:
                p2 = svc.market.get_price(plan.victim_symbol)
                if p2 and float(p2) > 0:
                    price = float(p2)
        except Exception:
            pass
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
            set_pending_entry(plan.entry_symbol, plan.victim_symbol)
        return {"ok": executed, "message": msg, "result": result}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def try_slot_eviction_on_max_open(
    *,
    order,
    source: str,
    free_full_slots: int,
    config,
    risk_config: dict | None,
    config_raw: dict | None = None,
    spike_multiple: float = 0.0,
) -> tuple[EvictionPlan | None, str]:
    """Full path for RiskManager. Returns (plan, message_suffix)."""
    mode = eviction_mode(risk_config)
    if mode == "off":
        return None, ""

    raw = config_raw
    if raw is None and config is not None and hasattr(config, "raw"):
        raw = config.raw

    # Fusion / soft_block / spendable — fail-open neutral
    block_buys = False
    regime = "NEUTRAL"
    try:
        from services.market_policy_fusion import get_global_market_bias

        bias = get_global_market_bias(raw) or {}
        block_buys = bool(bias.get("block_buys"))
        regime = str(bias.get("regime") or "NEUTRAL")
    except Exception:
        pass

    soft_block = False
    structure_risk = False
    prefer = False
    try:
        from intelligence.memory.cache import get_coin_profile, get_entry_bias

        soft_block = get_entry_bias(order.symbol, config=raw) == "soft_block"
        prof = get_coin_profile(order.symbol, config=raw)
        if prof and isinstance(prof.features, dict):
            structure_risk = bool(
                prof.features.get("structure_risk") or prof.features.get("hard_negative")
            )
        prefer = bool(prof and prof.entry_bias == "prefer")
    except Exception:
        pass

    spendable_ok = True
    try:
        # If risk manager already passed cash later, we only check roughly here
        min_trade = float((risk_config or {}).get("min_trade_usdt", 100) or 100)
        # leave True — cash_floor runs after max_open today; demand may still plan
        _ = min_trade
    except Exception:
        pass

    warmup = False
    try:
        from services.market_oracle_store import process_uptime_sec

        up = float(process_uptime_sec())
        warm_min = float(slot_eviction_section(risk_config).get("restart_warmup_min", 0) or 0)
        # use skip_if_warmup with capacity-style: process age from oracle
        if slot_eviction_section(risk_config).get("skip_if_warmup", True):
            # only if process very young < 2 min default unless configured
            warm_min = warm_min or 0
            if warm_min > 0 and up < warm_min * 60:
                warmup = True
    except Exception:
        pass

    def _gp(sym: str):
        try:
            from intelligence.memory.cache import get_coin_profile

            return get_coin_profile(sym, config=raw)
        except Exception:
            return None

    # spike from order if present
    spike = spike_multiple
    if spike <= 0 and getattr(order, "entry_15m_vol_ratio", None):
        try:
            spike = float(order.entry_15m_vol_ratio or 0)
        except Exception:
            spike = 0.0

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
    )

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
            suffix = (
                f" · eviction LIVE {plan.victim_symbol}→{plan.entry_symbol} "
                f"({plan.action})"
            )
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
