"""Optional scheduled (calendar / weekly-split) DCA — GitHub #102 D7.

Pure scheduling + budget split; candidates reuse BUY_DCA + existing risk boundary.
Default OFF — dip/recovery path unchanged when disabled.

Does not write ledger/orders. Fail-open when misconfigured.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.actions import BUY_DCA
from strategies.dca import DCACandidate, dca_config


def scheduled_config(
    dca_cfg: dict | None = None,
    *,
    config_raw: dict | None = None,
) -> dict[str, Any]:
    """Merge defaults ← global volatile_altcoin.dca.scheduled ← coin dca.scheduled."""
    defaults: dict[str, Any] = {
        "enabled": False,
        "mode": "shadow",  # shadow | live
        "interval_days": 7.0,
        "weekday": None,  # 0=Mon .. 6=Sun; None = interval-only
        "total_usdt": 500.0,
        "min_usdt_per_symbol": 50.0,
        "max_symbols": 10,
        "require_open_position": True,
        "apply_policy": True,
        "respect_spendable_dca": True,
        "only_when_dip_ineligible": True,
        "source_tag": "dca_scheduled",
    }
    global_sched: dict = {}
    if config_raw is None:
        try:
            from core.config import get_bot_config

            config_raw = get_bot_config().raw
        except Exception:
            config_raw = {}
    try:
        global_sched = dict(
            ((config_raw or {}).get("volatile_altcoin") or {}).get("dca") or {}
        ).get("scheduled") or {}
        if not isinstance(global_sched, dict):
            global_sched = {}
    except Exception:
        global_sched = {}
    coin_sched = dict((dca_cfg or {}).get("scheduled") or {})
    return {**defaults, **global_sched, **coin_sched}


def scheduled_enabled(
    strategy_params: dict | None = None,
    *,
    config_raw: dict | None = None,
) -> bool:
    dca = dict((strategy_params or {}).get("dca") or {})
    return bool(scheduled_config(dca, config_raw=config_raw).get("enabled"))


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s[:32] if len(s) > 32 else s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def is_schedule_due(
    now: datetime | None = None,
    last_run: datetime | str | None = None,
    *,
    interval_days: float = 7.0,
    weekday: int | None = None,
) -> bool:
    """True when a scheduled DCA cycle may fire.

    - No last_run: due if weekday is None or matches today.
    - With last_run: need elapsed >= interval_days; if weekday set, also match weekday.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    last_dt: datetime | None
    if isinstance(last_run, datetime):
        last_dt = last_run if last_run.tzinfo else last_run.replace(tzinfo=timezone.utc)
    else:
        last_dt = _parse_iso(str(last_run) if last_run else None)

    wd = weekday
    if wd is not None:
        try:
            wd = int(wd)
        except (TypeError, ValueError):
            wd = None
        if wd is not None and not (0 <= wd <= 6):
            wd = None

    if last_dt is None:
        if wd is None:
            return True
        return now.weekday() == wd

    try:
        interval = max(0.0, float(interval_days))
    except (TypeError, ValueError):
        interval = 7.0
    elapsed_days = (now - last_dt).total_seconds() / 86400.0
    if elapsed_days < interval - 1e-9:
        return False
    if wd is not None and now.weekday() != wd:
        return False
    return True


def split_usdt_budget(
    total_usdt: float,
    symbols: list[str],
    *,
    min_usdt_per_symbol: float = 0.0,
    max_symbols: int = 0,
    weights: dict[str, float] | None = None,
) -> dict[str, float]:
    """Split total USDT across symbols (equal or weighted).

    Uses largest-remainder rounding so allocations sum to total (within 0.01).
    Drops symbols that would receive < min_usdt after equal split among remaining.
    Returns {} if total <= 0 or no symbols.
    """
    try:
        total = float(total_usdt)
    except (TypeError, ValueError):
        return {}
    if total <= 0:
        return {}

    # stable unique order
    seen: set[str] = set()
    syms: list[str] = []
    for s in symbols or []:
        s = str(s or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        syms.append(s)
    if not syms:
        return {}

    try:
        max_n = int(max_symbols or 0)
    except (TypeError, ValueError):
        max_n = 0
    if max_n > 0:
        syms = syms[:max_n]

    try:
        min_u = max(0.0, float(min_usdt_per_symbol or 0))
    except (TypeError, ValueError):
        min_u = 0.0

    # Drop symbols until equal share >= min_u (or one left)
    while len(syms) > 1 and min_u > 0 and (total / len(syms)) + 1e-9 < min_u:
        syms = syms[:-1]
    if min_u > 0 and total + 1e-9 < min_u:
        return {}

    n = len(syms)
    if weights:
        w = []
        for s in syms:
            try:
                wi = float(weights.get(s, 1.0) or 0.0)
            except (TypeError, ValueError):
                wi = 1.0
            w.append(max(0.0, wi))
        wsum = sum(w)
        if wsum <= 0:
            w = [1.0] * n
            wsum = float(n)
        raw = [total * (wi / wsum) for wi in w]
    else:
        raw = [total / n] * n

    # largest remainder method (cents)
    floors = [int(x * 100) / 100.0 for x in raw]
    rem = round(total - sum(floors), 2)
    order = sorted(range(n), key=lambda i: raw[i] - floors[i], reverse=True)
    out_list = list(floors)
    i = 0
    # distribute remaining cents
    cents = int(round(rem * 100))
    while cents > 0 and order:
        out_list[order[i % len(order)]] = round(out_list[order[i % len(order)]] + 0.01, 2)
        cents -= 1
        i += 1

    result = {syms[i]: out_list[i] for i in range(n) if out_list[i] > 0}
    # drop below min after rounding
    if min_u > 0:
        result = {s: u for s, u in result.items() if u + 1e-9 >= min_u}
    return result


@dataclass(frozen=True)
class ScheduledAllocationPlan:
    """Result of a due scheduled cycle."""

    due: bool
    allocations: dict[str, float]
    total_usdt: float
    reason: str = ""


def plan_scheduled_allocations(
    symbols: list[str],
    *,
    config: dict | None = None,
    now: datetime | None = None,
    last_run: datetime | str | None = None,
) -> ScheduledAllocationPlan:
    """High-level pure entry: due-check + budget split.

    config is scheduled_config(...) result (or raw scheduled dict).
    """
    cfg = dict(config or {})
    if not cfg.get("enabled"):
        return ScheduledAllocationPlan(False, {}, 0.0, "disabled")

    due = is_schedule_due(
        now,
        last_run,
        interval_days=float(cfg.get("interval_days") or 7),
        weekday=cfg.get("weekday"),
    )
    if not due:
        return ScheduledAllocationPlan(False, {}, 0.0, "not_due")

    total = float(cfg.get("total_usdt") or 0)
    alloc = split_usdt_budget(
        total,
        symbols,
        min_usdt_per_symbol=float(cfg.get("min_usdt_per_symbol") or 0),
        max_symbols=int(cfg.get("max_symbols") or 0),
    )
    if not alloc:
        return ScheduledAllocationPlan(True, {}, total, "empty_split")
    return ScheduledAllocationPlan(True, alloc, sum(alloc.values()), "ok")


def evaluate_scheduled_dca_addon(
    market,
    position: dict,
    strategy_params: dict | None,
    *,
    allocated_usdt: float,
    config_raw: dict | None = None,
    spendable_dca: float | None = None,
) -> DCACandidate | None:
    """Build a scheduled BUY_DCA candidate for one symbol.

    Caller ensures schedule is due and provides this symbol's allocation.
    Does **not** require dip/loss-band gates. Still optional policy + spendable cap.
    """
    try:
        alloc = float(allocated_usdt)
    except (TypeError, ValueError):
        return None
    if alloc <= 0:
        return None

    dca_cfg = dca_config(strategy_params)
    # Master switch: coin dca.enabled still required so we don't DCA on symbols with DCA off
    if not dca_cfg.get("enabled", False):
        return None

    scfg = scheduled_config(dca_cfg, config_raw=config_raw)
    if not scfg.get("enabled"):
        return None

    if scfg.get("require_open_position", True):
        amt = float((position or {}).get("amount", 0) or 0)
        if amt <= 0:
            return None

    usdt = alloc
    shadow = str(scfg.get("mode", "shadow")).lower() != "live"
    breakdown: dict[str, int | float] = {"scheduled": 1, "allocated_usdt": round(usdt, 2)}
    rationale = f"Scheduled DCA ${usdt:.0f} (interval calendar split)"

    # Optional policy layer (same as dip path) — fail-open
    if scfg.get("apply_policy", True):
        try:
            from strategies.dca_context import build_dca_context
            from strategies.dca_policy import (
                apply_policy_to_usdt,
                dca_policy_config,
                evaluate_dca_policy,
            )

            pcfg = dca_policy_config(dca_cfg)
            if pcfg.get("enabled"):
                sym = str(
                    (position or {}).get("symbol")
                    or getattr(market, "symbol", "")
                    or ""
                )
                loss_pct = 0.0
                try:
                    entry = float((position or {}).get("average_entry", 0) or 0)
                    px = float(getattr(market, "current_price", 0) or 0)
                    if entry > 0 and px > 0:
                        loss_pct = (px / entry - 1.0) * 100.0
                except Exception:
                    pass
                ctx = build_dca_context(
                    symbol=sym,
                    position=position,
                    market=market,
                    strategy_params=strategy_params,
                    loss_pct=loss_pct,
                    include_rag=False,
                    config_raw=config_raw,
                )
                # Prefer injected spendable when provided
                if spendable_dca is not None:
                    ctx.spendable_dca = float(spendable_dca)
                result = evaluate_dca_policy(ctx, pcfg)
                pol_shadow = bool(pcfg.get("shadow", True))
                # Scheduled uses scfg.mode for candidate shadow; policy shadow only affects size
                effective_pol_shadow = pol_shadow
                codes = ",".join(result.reason_codes) if result.reason_codes else "-"
                rationale = (
                    f"{rationale} policy[v{result.policy_version} "
                    f"mult={result.size_mult} skip={result.skip} {codes}]"
                )
                breakdown["policy_mult"] = result.size_mult
                breakdown["policy_skip"] = 1 if result.skip else 0
                if result.skip and not effective_pol_shadow:
                    return None
                usdt = apply_policy_to_usdt(
                    usdt,
                    result,
                    spendable_dca=ctx.spendable_dca
                    if scfg.get("respect_spendable_dca", True)
                    else None,
                    shadow=effective_pol_shadow,
                )
        except Exception:
            pass
    elif scfg.get("respect_spendable_dca", True) and spendable_dca is not None:
        try:
            usdt = min(usdt, max(0.0, float(spendable_dca)))
        except (TypeError, ValueError):
            pass

    if usdt <= 0:
        return None

    return DCACandidate(
        action=BUY_DCA,
        source=str(scfg.get("source_tag") or "dca_scheduled"),
        rationale=rationale,
        usdt_amount=float(usdt),
        shadow_only=shadow,
        score=0,
        breakdown=breakdown,
    )


def collect_open_position_symbols(
    coins: list[dict],
    *,
    get_position_fn=None,
    resolve_coin_config_fn=None,
) -> list[str]:
    """Symbols with open amount > 0 (stable order from coins list)."""
    if get_position_fn is None:
        from strategies.positions import get_position as get_position_fn
    if resolve_coin_config_fn is None:
        from strategies.registry import resolve_coin_config as resolve_coin_config_fn

    out: list[str] = []
    for coin in coins or []:
        try:
            symbol = str(coin.get("symbol") or "")
            if not symbol:
                continue
            coin_cfg = resolve_coin_config_fn(coin)
            tf = coin_cfg.get("timeframe", "4h")
            pos = get_position_fn(symbol, tf)
            if float(pos.get("amount", 0) or 0) > 0:
                out.append(symbol)
        except Exception:
            continue
    return out


def open_symbols_for_schedule(*, include_symbol: str | None = None) -> list[str]:
    """Open symbols for budget split (active positions + optional current symbol)."""
    out: list[str] = []
    seen: set[str] = set()
    try:
        from strategies.positions import list_active_positions

        for lot in list_active_positions() or []:
            sym = str((lot or {}).get("symbol") or "")
            if not sym or sym in seen:
                continue
            seen.add(sym)
            out.append(sym)
    except Exception:
        pass
    if include_symbol:
        sym = str(include_symbol).strip()
        if sym and sym not in seen:
            out.append(sym)
    return out or ([str(include_symbol)] if include_symbol else [])


def last_scheduled_for_symbol(
    symbol: str,
    timeframe: str = "4h",
) -> str | None:
    """Per-symbol last_scheduled_dca_at (ISO) or None."""
    try:
        from strategies.positions import get_position

        pos = get_position(symbol, timeframe)
        ts = pos.get("last_scheduled_dca_at")
        return str(ts) if ts else None
    except Exception:
        return None


def is_symbol_schedule_due(
    symbol: str,
    *,
    timeframe: str = "4h",
    config: dict | None = None,
    now: datetime | None = None,
    last_run: datetime | str | None = None,
) -> bool:
    """True when *this* symbol may receive a scheduled allocation this cycle.

    Cadence is per-symbol so multi-coin DE/portfolio passes can fire all
    equal-share allocations in one cycle without the first stamp blocking the rest.
    """
    cfg = dict(config or {})
    if last_run is None:
        last_run = last_scheduled_for_symbol(symbol, timeframe)
    return is_schedule_due(
        now,
        last_run,
        interval_days=float(cfg.get("interval_days") or 7),
        weekday=cfg.get("weekday"),
    )


def equal_share_allocations(
    symbols: list[str],
    *,
    config: dict | None = None,
) -> dict[str, float]:
    """Split total_usdt across the open universe (ignores due-check)."""
    cfg = dict(config or {})
    if not cfg.get("enabled"):
        return {}
    try:
        total = float(cfg.get("total_usdt") or 0)
    except (TypeError, ValueError):
        return {}
    return split_usdt_budget(
        total,
        symbols,
        min_usdt_per_symbol=float(cfg.get("min_usdt_per_symbol") or 0),
        max_symbols=int(cfg.get("max_symbols") or 0),
    )


def stamp_last_scheduled_dca(
    symbol: str,
    timeframe: str,
    *,
    now: datetime | None = None,
) -> str:
    """Persist last_scheduled_dca_at on the position after a real fire/execute."""
    from strategies.positions import set_position_field

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    stamp = now.isoformat().replace("+00:00", "Z")
    set_position_field(symbol, timeframe, "last_scheduled_dca_at", stamp)
    return stamp
