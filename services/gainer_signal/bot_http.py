"""Bot-side: POST /internal/gainer-signal — consume signals → demo buy (WS-2)."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Callable

from flask import Flask, jsonify, request

from logger import log
from services.gainer_signal.pure import (
    DEFAULT_ELIGIBLE_MIN_VOL,
    check_gainer_entry_caps,
    clamp_usdt_to_vol,
    count_open_gainer_positions,
    is_eligible,
    is_gainer_source,
    is_leverage_symbol,
    normalize_symbol,
    position_has_symbol_open,
)

# process-local day counter (fail-open; ledger is source of truth when available)
_day_buys: dict[str, int] = {}
_day_key: str = ""


def gainer_entry_enabled(config: dict | None = None) -> bool:
    env = (os.environ.get("GAINER_ENTRY_ENABLED") or "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    raw = config
    if raw is None:
        try:
            from core.config import get_bot_config

            raw = get_bot_config().raw
        except Exception:
            raw = {}
    ge = (raw or {}).get("gainer_entry") if isinstance(raw, dict) else {}
    if isinstance(ge, dict) and "enabled" in ge:
        return bool(ge.get("enabled"))
    # default OFF: leaders board is observe/scorecard, not a buy decision source.
    # Re-enable only with an indicator/DE gate — never raw rank→market order.
    return False


def gainer_entry_config(config: dict | None = None) -> dict[str, Any]:
    raw = config
    if raw is None:
        try:
            from core.config import get_bot_config

            raw = get_bot_config().raw
        except Exception:
            raw = {}
    ge = (raw or {}).get("gainer_entry") if isinstance(raw, dict) else {}
    if not isinstance(ge, dict):
        ge = {}
    # require_de_confirm: rank/heat only nominates; DecisionEngine (+ recipes/memory
    # via DE) must emit BUY. Default ON so board never bypasses indicators.
    require_de = ge.get("require_de_confirm")
    if require_de is None:
        require_de = True
    return {
        "enabled": gainer_entry_enabled(raw if isinstance(raw, dict) else None),
        "max_open": int(ge.get("max_open") or 3),
        "max_buys_per_day": int(ge.get("max_buys_per_day") or 6),
        "require_eligible": bool(ge.get("require_eligible", True)),
        "require_de_confirm": bool(require_de),
        "timeframe": str(ge.get("timeframe") or "1h"),
        "max_notional_pct_of_vol": float(ge.get("max_notional_pct_of_vol") or 2.0),
        "default_usdt": float(ge.get("default_usdt") or 0) or None,
    }


def _internal_token() -> str:
    return (
        os.environ.get("GAINER_SIGNAL_TOKEN")
        or os.environ.get("EXIT_WS_INTERNAL_TOKEN")
        or ""
    ).strip()


def _utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _bump_day_buy() -> int:
    global _day_key, _day_buys
    d = _utc_day()
    if d != _day_key:
        _day_key = d
        _day_buys = {}
    _day_buys[d] = int(_day_buys.get(d) or 0) + 1
    return _day_buys[d]


def _get_day_buys() -> int:
    global _day_key, _day_buys
    d = _utc_day()
    if d != _day_key:
        return 0
    return int(_day_buys.get(d) or 0)


def count_gainer_buys_today_from_fills(
    fills: list[dict[str, Any]] | None,
    *,
    day_key: str | None = None,
    day_scoped: bool = False,
) -> int:
    """Count filled gainer BUY orders.

    - day_scoped=True: list already filtered to the trading day (e.g. OrderService.list_day_filled_all)
    - day_key set: only count orders matching that day_key / filled ts prefix
    - both unset: use UTC calendar day (legacy)
    """
    n = 0
    day = day_key
    if day is None and not day_scoped:
        day = _utc_day()
    for o in fills or []:
        if str(o.get("side") or "").lower() != "buy":
            continue
        if str(o.get("status") or "filled").lower() not in ("filled", "closed"):
            continue
        if not is_gainer_source(o.get("source")):
            continue
        if not day_scoped and day is not None:
            dk = str(o.get("day_key") or "")
            ts = str(
                (o.get("timestamps") or {}).get("filled")
                or o.get("ts_event")
                or o.get("timestamp")
                or ""
            )
            if dk != day and not ts.startswith(day):
                continue
        n += 1
    return n


def load_gainer_buys_today_from_ledger(
    *,
    source_exact: str | None = None,
) -> int | None:
    """Best-effort day count from orders ledger. None if ledger unavailable."""
    try:
        from services.order_service import OrderService

        fills = OrderService().list_day_filled_all()
        if source_exact:
            want = source_exact.strip().lower()
            fills = [
                f
                for f in (fills or [])
                if str(f.get("source") or f.get("entry_source") or "").lower() == want
                or str(f.get("source") or "").lower().endswith(want)
            ]
            # count buys only
            n = 0
            for f in fills:
                side = str(f.get("side") or "").lower()
                if side in ("buy", "b"):
                    n += 1
            return n
        return count_gainer_buys_today_from_fills(fills, day_scoped=True)
    except Exception:
        return None


def _relvol_cfg(config: dict | None = None) -> dict[str, Any]:
    """Bot-side RelVol trade config (SSOT: config.json gainer_relvol_shadow)."""
    try:
        from services.gainer_universe.relvol_shadow import relvol_shadow_config

        return relvol_shadow_config(config)
    except Exception:
        raw = config
        if raw is None:
            try:
                from core.config import get_bot_config

                raw = get_bot_config().raw
            except Exception:
                raw = {}
        block = (raw or {}).get("gainer_relvol_shadow") if isinstance(raw, dict) else {}
        return dict(block) if isinstance(block, dict) else {}


def _default_de_allows_buy(symbol: str, price: float, timeframe: str, data: dict) -> tuple[bool, str, list]:
    """Run DecisionEngine on gainer nominee; BUY only if TA/recipe path agrees."""
    from core.actions import is_buy
    from services.signal_orchestrator import SignalOrchestrator

    coin = {
        "symbol": symbol,
        "timeframe": timeframe,
        "source": str(data.get("source") or "gainer_rank_entry"),
        "gainer_meta": {
            "leader_rank": data.get("rank"),
            "pct_24h": data.get("pct_24h"),
            "trigger": data.get("trigger"),
        },
    }
    orch = SignalOrchestrator()
    analysis = orch.analyze(coin, float(price))
    if analysis is None:
        return False, "de_no_analysis", []
    action = getattr(analysis, "action", "HOLD")
    sources = list(getattr(analysis, "sources", None) or [])
    rationale = str(getattr(analysis, "rationale", "") or "")[:200]
    if not is_buy(action):
        return False, f"de_hold:{action}:{rationale}", sources
    return True, f"de_buy:{action}", sources


def process_gainer_signal(
    data: dict[str, Any],
    *,
    config: dict | None = None,
    positions: list[dict[str, Any]] | None = None,
    gainer_buys_today: int | None = None,
    execute_buy: Callable[..., Any] | None = None,
    de_allows_buy: Callable[..., tuple[bool, str, list]] | None = None,
) -> tuple[dict[str, Any], int]:
    """Core consume logic (testable). Returns (body, http_status).

    With require_de_confirm (default True), board/heat only nominates; DE must BUY.
    """
    cfg = gainer_entry_config(config)
    source_early = str(data.get("source") or data.get("entry_source") or "")
    is_relvol = source_early == "gainer_relvol" or str(
        data.get("trigger") or ""
    ).startswith("relvol")

    # RelVol has its own kill switch (mode must be trade) — independent of gainer_entry.enabled
    rcfg: dict[str, Any] = {}
    if is_relvol:
        rcfg = _relvol_cfg(config)
        if not bool(rcfg.get("enabled", False)) or str(rcfg.get("mode") or "").lower() != "trade":
            return {
                "ok": False,
                "executed": False,
                "message": "relvol_disabled",
                "reject_reason": f"mode={rcfg.get('mode')} enabled={rcfg.get('enabled')}",
            }, 409
    elif not cfg["enabled"]:
        return {"ok": False, "executed": False, "message": "gainer_entry_disabled"}, 503

    sym = normalize_symbol(data.get("symbol") or "")
    if not sym:
        return {"ok": False, "executed": False, "message": "bad_symbol"}, 400

    try:
        price = float(data.get("last") or data.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        return {"ok": False, "executed": False, "message": "bad_price"}, 400

    try:
        quote_vol = float(data.get("quote_vol") or data.get("quote_vol_24h") or 0)
    except (TypeError, ValueError):
        quote_vol = 0.0
    lev = bool(data.get("leverage")) or is_leverage_symbol(sym)
    eligible_flag = data.get("eligible")
    # RelVol discovery: intentionally below 500k abs vol — do not apply min_vol cut
    if cfg["require_eligible"] and not is_relvol:
        if eligible_flag is False:
            return {
                "ok": False,
                "executed": False,
                "message": "not_eligible",
                "reject_reason": data.get("reject_reason") or "flagged",
            }, 409
        ok_e, reason = is_eligible(quote_vol=quote_vol, leverage=lev)
        if not ok_e:
            return {
                "ok": False,
                "executed": False,
                "message": "not_eligible",
                "reject_reason": reason,
            }, 409
    elif is_relvol and lev:
        return {
            "ok": False,
            "executed": False,
            "message": "not_eligible",
            "reject_reason": "leverage",
        }, 409

    # positions
    if positions is None:
        try:
            from strategies.positions import list_active_positions

            positions = list_active_positions()
        except Exception:
            positions = []

    # RelVol: independent cap pool + no duplicate open symbol
    if is_relvol:
        if position_has_symbol_open(positions, sym):
            return {
                "ok": False,
                "executed": False,
                "message": "already_open",
                "symbol": sym,
            }, 409
        open_n = count_open_gainer_positions(positions, source_exact="gainer_relvol")
        max_open = int(rcfg.get("max_open") or 4)
        max_day = int(rcfg.get("max_buys_per_day") or 8)
        if gainer_buys_today is not None:
            buys_today = int(gainer_buys_today)
        else:
            ledger_n = load_gainer_buys_today_from_ledger(source_exact="gainer_relvol")
            local_n = _get_day_buys()  # process total; floor with ledger when available
            buys_today = int(ledger_n) if ledger_n is not None else local_n
        ok_cap, cap_reason = check_gainer_entry_caps(
            open_gainer_count=open_n,
            gainer_buys_today=buys_today,
            max_open=max_open,
            max_buys_per_day=max_day,
        )
    else:
        open_n = count_open_gainer_positions(positions)
        if gainer_buys_today is not None:
            buys_today = int(gainer_buys_today)
        else:
            ledger_n = load_gainer_buys_today_from_ledger()
            local_n = _get_day_buys()
            buys_today = max(local_n, int(ledger_n) if ledger_n is not None else 0)
        ok_cap, cap_reason = check_gainer_entry_caps(
            open_gainer_count=open_n,
            gainer_buys_today=buys_today,
            max_open=cfg["max_open"],
            max_buys_per_day=cfg["max_buys_per_day"],
        )
    if not ok_cap:
        return {
            "ok": False,
            "executed": False,
            "message": cap_reason,
            "open_gainer_count": open_n,
            "gainer_buys_today": buys_today,
        }, 409

    # size
    raw_cfg = config
    if raw_cfg is None:
        try:
            from core.config import get_bot_config

            raw_cfg = get_bot_config().raw
        except Exception:
            raw_cfg = {}
    max_usdt = float((raw_cfg or {}).get("max_usdt_per_trade") or 500)
    if cfg.get("default_usdt"):
        max_usdt = float(cfg["default_usdt"])

    # Coin-facts memory gate (best-effort; fail-open if no flags)
    memory_size_mult = 1.0
    memory_reason = ""
    try:
        from strategies.sensor_entry_memory import apply_sensor_memory_entry_policy

        flags = data.get("coin_facts_flags") or data.get("flags")
        entry_bias = str(data.get("entry_bias") or "neutral")
        mem_cfg = {}
        if isinstance(raw_cfg, dict):
            mem = (raw_cfg.get("memory") or {}) if isinstance(raw_cfg.get("memory"), dict) else {}
            mem_cfg = dict(mem.get("sensor_entry") or mem.get("entry_policy") or {})
            if "memory_enabled" not in mem_cfg:
                mem_cfg["memory_enabled"] = bool(mem.get("enabled", True))
        verdict = apply_sensor_memory_entry_policy(
            flags=flags, entry_bias=entry_bias, cfg=mem_cfg
        )
        if not verdict.allow:
            return {
                "ok": False,
                "executed": False,
                "message": "blocked_coin_facts",
                "reject_reason": verdict.reason or "coin_facts",
            }, 409
        memory_size_mult = float(verdict.size_mult or 1.0)
        memory_reason = verdict.reason or ""
    except Exception as e:
        log(f"gainer_entry memory gate skip {sym}: {e}", "DEBUG")

    usdt = float(max_usdt) * max(0.0, min(1.0, memory_size_mult))
    if is_relvol:
        # Size from 1h RelVol liquidity — fail-closed on errors (never ignore liq caps)
        try:
            from services.gainer_universe.relvol_shadow import size_usdt_for_signal

            q1 = float(data.get("qvol_1h") or data.get("qvol") or 0)
            usdt = size_usdt_for_signal(
                qvol_1h=q1,
                abs_vol_24h=float(quote_vol or 0),
                cfg=rcfg,
                max_usdt_per_trade=float(max_usdt),
            )
            usdt = float(usdt) * max(0.0, min(1.0, memory_size_mult))
        except Exception as e:
            log(f"gainer_relvol size fail-closed {sym}: {e}", "WARNING")
            usdt = 0.0
    else:
        usdt = clamp_usdt_to_vol(
            usdt, quote_vol, max_pct_of_vol=cfg["max_notional_pct_of_vol"]
        )
    if usdt < 10:
        return {
            "ok": False,
            "executed": False,
            "message": "usdt_too_small",
            "usdt": usdt,
        }, 409

    source = str(data.get("source") or "gainer_rank_entry")
    if is_relvol:
        source = "gainer_relvol"
    elif not is_gainer_source(source):
        source = "gainer_signal"
    trigger = str(data.get("trigger") or "")
    try:
        rank = int(data.get("rank") or 0)
    except (TypeError, ValueError):
        rank = 0
    try:
        pct = float(data.get("pct_24h") or 0)
    except (TypeError, ValueError):
        pct = 0.0

    # RelVol extension gate: do not chase already-extended day moves
    if is_relvol:
        max_ext = float(rcfg.get("max_pct_24h") or 40.0)
        if pct > max_ext:
            return {
                "ok": False,
                "executed": False,
                "message": "extension_cap",
                "reject_reason": f"pct_24h={pct:.1f}>{max_ext:.0f}",
                "pct_24h": pct,
            }, 409

    timeframe = str(data.get("timeframe") or cfg["timeframe"] or "1h")

    # DecisionEngine gate: indicators + personal recipes (via registry) + TA merge
    # RelVol: ignition *is* the signal — skip DE by default (config require_de_confirm)
    de_sources: list = []
    de_reason = ""
    need_de = bool(cfg.get("require_de_confirm", True))
    if is_relvol:
        need_de = bool(rcfg.get("require_de_confirm", False))
    if need_de:
        checker = de_allows_buy or _default_de_allows_buy
        try:
            ok_de, de_reason, de_sources = checker(sym, price, timeframe, data)
        except Exception as e:
            log(f"gainer_entry DE gate error {sym}: {e}", "WARNING")
            return {
                "ok": False,
                "executed": False,
                "message": "de_gate_error",
                "reject_reason": str(e)[:160],
            }, 500
        if not ok_de:
            log(
                f"gainer_entry DE reject {sym} rank={rank} pct={pct} why={de_reason[:120]}",
                "INFO",
            )
            return {
                "ok": False,
                "executed": False,
                "message": "de_hold",
                "reject_reason": de_reason,
                "de_sources": de_sources,
                "symbol": sym,
                "rank": rank,
                "pct_24h": pct,
            }, 409

    gainer_meta = {
        "leader_rank": rank,
        "pct_24h": pct,
        "quote_vol": quote_vol,
        "trigger": trigger,
        "source": source,
        "entry_source": source,
        "entry_policy": data.get("entry_policy"),
        "vol_bucket": data.get("vol_bucket"),
        "atr_pct": data.get("atr_pct"),
        "extension_score": data.get("extension_score", pct),
        "band_lo": data.get("band_lo"),
        "band_hi": data.get("band_hi"),
        "scans_in_top_k": data.get("scans_in_top_k"),
        "rank_improved": data.get("rank_improved"),
        "hard_ceiling": data.get("hard_ceiling"),
        "memory_size_mult": memory_size_mult,
        "memory_reason": memory_reason or None,
        "de_confirm": bool(cfg.get("require_de_confirm", True)),
        "de_reason": de_reason or None,
        "de_sources": de_sources or None,
    }
    request_extra = {
        "gainer_meta": gainer_meta,
        "entry_source": source,
        "leader_rank": rank,
        "pct_24h": pct,
        "quote_vol": quote_vol,
        "trigger": trigger,
    }

    if execute_buy is None:
        def execute_buy(**kwargs):
            from core.config import get_bot_config
            from core.models import TradeOrder
            from services.trading_service import TradingService

            conf = get_bot_config()
            ts = TradingService(conf)
            src = str(kwargs.get("source") or "gainer_signal")
            sig = "GAINER_RELVOL" if src == "gainer_relvol" else "GAINER_SIGNAL"
            order = TradeOrder(
                type="BUY",
                symbol=kwargs["symbol"],
                price=float(kwargs["price"]),
                amount=0,
                usdt_amount=float(kwargs["usdt"]),
                source=src,
                signal=sig,
            )
            return ts.execute_order(
                order,
                kwargs.get("timeframe") or "1h",
                source=src,
                request_extra=kwargs.get("request_extra"),
            )

    try:
        result = execute_buy(
            symbol=sym,
            price=price,
            usdt=usdt,
            source=source,
            timeframe=timeframe,
            request_extra=request_extra,
        )
    except Exception as e:
        log(f"gainer_entry execute error {sym}: {e}", "WARNING")
        return {"ok": False, "executed": False, "message": f"execute_error:{e}"}, 500

    executed = bool(getattr(result, "executed", False) or (isinstance(result, dict) and result.get("executed")))
    message = getattr(result, "message", None) or (
        result.get("message") if isinstance(result, dict) else ""
    )
    order_id = getattr(result, "order_id", None) or (
        result.get("order_id") if isinstance(result, dict) else ""
    )
    if executed:
        _bump_day_buy()
        try:
            from strategies.positions import flush_positions, get_position

            pos = get_position(sym, timeframe)
            if isinstance(pos, dict):
                pos["entry_source"] = source
                flush_positions(force=True)
        except Exception:
            pass
        log(
            f"gainer_entry BUY {sym} usdt={usdt:.0f} rank={rank} trigger={trigger} "
            f"src={source}",
            "INFO",
        )
    return {
        "ok": True,
        "executed": executed,
        "message": message or ("filled" if executed else "not_executed"),
        "symbol": sym,
        "usdt": usdt,
        "source": source,
        "order_id": order_id,
        "meta": request_extra.get("gainer_meta"),
        "open_gainer_count_before": open_n,
        "gainer_buys_today_before": buys_today,
    }, 200 if executed else 409


def register_gainer_signal_routes(app: Flask) -> None:
    """POST /internal/gainer-signal — token-gated demo buy from signal service."""

    @app.route("/internal/gainer-signal", methods=["POST"])
    def internal_gainer_signal():
        expected = _internal_token()
        if not expected:
            return (
                jsonify(
                    {
                        "ok": False,
                        "executed": False,
                        "message": "not_configured",
                        "error": "GAINER_SIGNAL_TOKEN or EXIT_WS_INTERNAL_TOKEN unset",
                    }
                ),
                503,
            )
        got = (
            request.headers.get("X-Gainer-Signal-Token")
            or request.headers.get("X-Exit-Ws-Token")
            or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            or ""
        )
        if got != expected:
            return jsonify({"ok": False, "executed": False, "message": "unauthorized"}), 401

        data = request.get_json(silent=True) or {}
        tid = str(
            data.get("tenant_id")
            or request.headers.get("X-Tenant-Id")
            or "default"
        ).strip().lower() or "default"
        try:
            from core.tenant_context import tenant_context

            with tenant_context(tid):
                body, status = process_gainer_signal(data)
        except Exception:
            body, status = process_gainer_signal(data)
        if isinstance(body, dict):
            body.setdefault("tenant_id", tid)
        return jsonify(body), status

    log("gainer_signal consume route registered (/internal/gainer-signal)", "INFO")
