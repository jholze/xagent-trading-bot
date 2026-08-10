"""RelVol ignition scanner — shadow log and optional staging **trade**.

Modes:
  off     — disabled
  shadow  — log only
  trade   — log + demo/paper buy (TradingService), no 500k discovery filter

Kill: gainer_relvol_shadow.enabled=false or mode=off
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from logger import LOG_DIR, log
from services.gainer_universe.filters import normalize_symbol, passes_spot_usdt_filter
from services.gainer_universe.relvol_pure import (
    abs_vol_24h_from_qs,
    find_signals_ccxt,
    qvol_ccxt,
)

SOURCE = "gainer_relvol"

_DEFAULT: dict[str, Any] = {
    "enabled": False,
    "mode": "shadow",  # shadow | trade | off
    "poll_sec": 3600,
    "max_symbols": 150,
    "ohlcv_limit": 30,
    "mult": 10.0,
    "win": 12,
    "cooldown_hours": 8,
    "min_ign_qvol": 5_000.0,
    "baseline_floor": 100.0,
    "min_nonzero": 0.25,
    "require_green": True,
    "min_volume_discover": 0.0,
    "prod_min_vol_compare": 500_000.0,
    "fetch_workers": 6,
    "log_path": "",
    # Primary path: gainer_signal REST+WS ticker stream (not this OHLCV mass-scan)
    "bot_ohlcv_scan": False,
    # trade path (staging / paper)
    "require_de_confirm": False,  # ignition *is* the signal
    "max_open": 4,
    "max_buys_per_day": 8,
    "max_ticket_usdt": 500.0,
    "min_ticket_usdt": 50.0,
    "participation": 0.02,  # of 1h quote vol
    "max_pct_of_vol_24h": 0.02,
    "timeframe": "1h",
    "tenants": ["default", "henry"],  # empty = current context only
}

_lock = threading.Lock()
_last_run_mono: float = 0.0
_last_fire_ts: dict[str, float] = {}
_day_buys: dict[str, dict[str, int]] = {}  # day -> tenant -> count


def relvol_shadow_config(config: dict | None = None) -> dict[str, Any]:
    raw: dict = {}
    if isinstance(config, dict):
        block = config.get("gainer_relvol_shadow")
        if isinstance(block, dict):
            raw = block
    out = {**_DEFAULT, **raw}
    out["enabled"] = bool(out.get("enabled", False))
    mode = str(out.get("mode") or "shadow").strip().lower()
    if mode not in ("shadow", "trade", "off"):
        mode = "shadow"
    out["mode"] = mode
    out["poll_sec"] = max(300.0, float(out.get("poll_sec") or 3600))
    out["max_symbols"] = max(20, int(out.get("max_symbols") or 150))
    out["ohlcv_limit"] = max(int(out.get("win") or 12) + 5, int(out.get("ohlcv_limit") or 30))
    return out


def relvol_shadow_enabled(config: dict | None = None) -> bool:
    cfg = relvol_shadow_config(config)
    return bool(cfg.get("enabled")) and cfg.get("mode") != "off"


def relvol_trade_enabled(config: dict | None = None) -> bool:
    cfg = relvol_shadow_config(config)
    return bool(cfg.get("enabled")) and cfg.get("mode") == "trade"


def size_usdt_for_signal(
    *,
    qvol_1h: float,
    abs_vol_24h: float,
    cfg: dict,
    max_usdt_per_trade: float,
) -> float:
    """Liquidity-aware ticket: participation × 1h vol, caps, min floor."""
    part = float(cfg.get("participation") or 0.02)
    max_ticket = float(cfg.get("max_ticket_usdt") or 500)
    min_ticket = float(cfg.get("min_ticket_usdt") or 50)
    max_pct_24 = float(cfg.get("max_pct_of_vol_24h") or 0.02)
    usdt = min(float(max_usdt_per_trade or 500), max_ticket, part * float(qvol_1h or 0))
    if abs_vol_24h and abs_vol_24h > 0:
        usdt = min(usdt, float(abs_vol_24h) * max_pct_24)
    if usdt < min_ticket:
        return 0.0
    return round(usdt, 2)


def _log_path(cfg: dict) -> Path:
    p = str(cfg.get("log_path") or "").strip()
    if p:
        return Path(p)
    return Path(LOG_DIR) / "gainer_relvol_shadow.jsonl"


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def discovery_symbols_from_tickers(
    tickers: dict[str, dict],
    cfg: dict,
    *,
    blacklist_cfg: dict | None = None,
) -> list[str]:
    bc = blacklist_cfg or {}
    min_disc = float(cfg.get("min_volume_discover") or 0)
    rows: list[tuple[str, float]] = []
    for raw_sym, t in (tickers or {}).items():
        if not isinstance(t, dict):
            continue
        sym = normalize_symbol(raw_sym)
        if not passes_spot_usdt_filter(
            sym,
            blacklist_suffixes=bc.get("blacklist_suffixes"),
            blacklist_bases=bc.get("blacklist_bases"),
            blacklist_name_keywords=bc.get("blacklist_name_keywords"),
        ):
            continue
        qv = t.get("quoteVolume")
        if qv is None:
            last = float(t.get("last") or 0)
            bv = float(t.get("baseVolume") or 0)
            qv = last * bv if last > 0 else 0.0
        qv = float(qv or 0)
        if qv < min_disc:
            continue
        rows.append((sym, qv))
    rows.sort(key=lambda x: x[1], reverse=True)
    cap = int(cfg.get("max_symbols") or 150)
    return [s for s, _ in rows[:cap]]


def _fetch_ohlcv_1h(symbol: str, limit: int) -> list[list[float]]:
    try:
        import ccxt

        ex = ccxt.gate({"enableRateLimit": True, "options": {"defaultType": "spot"}})
        bars = ex.fetch_ohlcv(symbol, timeframe="1h", limit=limit) or []
        return [list(b) for b in bars if len(b) >= 6]
    except Exception as e:
        log(f"relvol_shadow ohlcv {symbol}: {e}", "DEBUG")
        return []


def scan_symbol(
    symbol: str,
    bars: list[list[float]],
    cfg: dict,
    *,
    now_ts: float | None = None,
) -> dict[str, Any] | None:
    if len(bars) < int(cfg.get("win") or 12) + 1:
        return None
    now_ts = now_ts or time.time()
    last = bars[-1]
    last_ts = int(last[0])
    last_ts_s = last_ts // 1000 if last_ts > 10_000_000_000 else last_ts
    if now_ts - last_ts_s < 55 * 60:
        bars = bars[:-1]
    if len(bars) < int(cfg.get("win") or 12) + 1:
        return None

    sigs = find_signals_ccxt(
        symbol,
        bars,
        mult=float(cfg.get("mult") or 10),
        win=int(cfg.get("win") or 12),
        cooldown_h=1,
        min_ign_qvol=float(cfg.get("min_ign_qvol") or 5_000),
        baseline_floor=float(cfg.get("baseline_floor") or 100),
        min_nonzero=float(cfg.get("min_nonzero") or 0.25),
        require_green=bool(cfg.get("require_green", True)),
        only_last_closed=True,
    )
    if not sigs:
        return None
    sig = sigs[-1]
    cool = float(cfg.get("cooldown_hours") or 8) * 3600
    with _lock:
        prev = _last_fire_ts.get(symbol, 0.0)
        if now_ts - prev < cool:
            return None
        _last_fire_ts[symbol] = now_ts

    qs = [qvol_ccxt(b) for b in bars]
    t_i = len(bars) - 1
    abs24 = abs_vol_24h_from_qs(qs, t_i)
    prod_min = float(cfg.get("prod_min_vol_compare") or 500_000)
    sig["abs_vol_24h_est"] = round(abs24, 2)
    sig["would_pass_prod_min_vol"] = abs24 >= prod_min
    sig["detected_at"] = datetime.now(timezone.utc).isoformat()
    sig["mode"] = str(cfg.get("mode") or "shadow")
    sig["price"] = float(sig.get("close") or 0)
    return sig


def _day_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _bump_day_buy(tenant_id: str) -> None:
    d = _day_key()
    with _lock:
        _day_buys.setdefault(d, {})
        _day_buys[d][tenant_id] = int(_day_buys[d].get(tenant_id, 0)) + 1


def _day_buy_count(tenant_id: str) -> int:
    d = _day_key()
    with _lock:
        return int((_day_buys.get(d) or {}).get(tenant_id, 0))


def _count_open_relvol(tenant_id: str | None = None) -> int:
    try:
        from strategies.positions import list_active_positions

        n = 0
        for p in list_active_positions(tenant_id=tenant_id) or []:
            es = str(p.get("entry_source") or "").lower()
            src = str(p.get("source") or "").lower()
            if es == SOURCE or src == SOURCE or SOURCE in es:
                n += 1
        return n
    except Exception:
        return 0


def try_execute_relvol_buy(
    signal: dict,
    config: dict | None = None,
    *,
    tenant_id: str = "default",
) -> dict[str, Any]:
    """Paper/demo buy for one RelVol signal under *tenant_id*."""
    cfg = relvol_shadow_config(config)
    if cfg.get("mode") != "trade":
        return {"ok": False, "executed": False, "message": "not_trade_mode"}

    sym = normalize_symbol(signal.get("symbol") or "")
    price = float(signal.get("price") or signal.get("close") or 0)
    if not sym or price <= 0:
        return {"ok": False, "executed": False, "message": "bad_symbol_price"}

    from core.tenant_context import tenant_context
    from data_manager import get_config

    with tenant_context(tenant_id):
        raw = config if isinstance(config, dict) else get_config(tenant_id=tenant_id)
        # per-tenant overlay if present
        tcfg = relvol_shadow_config(raw if isinstance(raw, dict) else config)

        max_open = int(tcfg.get("max_open") or 4)
        max_day = int(tcfg.get("max_buys_per_day") or 8)
        if _count_open_relvol(tenant_id) >= max_open:
            return {
                "ok": False,
                "executed": False,
                "message": "max_open",
                "tenant_id": tenant_id,
            }
        if _day_buy_count(tenant_id) >= max_day:
            return {
                "ok": False,
                "executed": False,
                "message": "max_buys_per_day",
                "tenant_id": tenant_id,
            }

        try:
            from strategies.positions import get_position, is_open_position

            pos = get_position(sym, str(tcfg.get("timeframe") or "1h"))
            if is_open_position(pos):
                return {
                    "ok": False,
                    "executed": False,
                    "message": "already_open",
                    "tenant_id": tenant_id,
                }
        except Exception:
            pass

        max_usdt = float((raw or {}).get("max_usdt_per_trade") or 500)
        usdt = size_usdt_for_signal(
            qvol_1h=float(signal.get("qvol") or 0),
            abs_vol_24h=float(signal.get("abs_vol_24h_est") or 0),
            cfg=tcfg,
            max_usdt_per_trade=max_usdt,
        )
        if usdt <= 0:
            return {
                "ok": False,
                "executed": False,
                "message": "usdt_too_small",
                "tenant_id": tenant_id,
            }

        # Optional DE confirm (default off for RelVol)
        if tcfg.get("require_de_confirm"):
            try:
                from services.gainer_signal.bot_http import _default_de_allows_buy

                ok_de, why, _ = _default_de_allows_buy(
                    sym, price, str(tcfg.get("timeframe") or "1h"), signal
                )
                if not ok_de:
                    return {
                        "ok": False,
                        "executed": False,
                        "message": "de_hold",
                        "reject_reason": why,
                        "tenant_id": tenant_id,
                    }
            except Exception as e:
                return {
                    "ok": False,
                    "executed": False,
                    "message": f"de_error:{e}"[:160],
                    "tenant_id": tenant_id,
                }

        request_extra = {
            "entry_source": SOURCE,
            "gainer_meta": {
                "source": SOURCE,
                "entry_source": SOURCE,
                "factor": signal.get("factor"),
                "qvol": signal.get("qvol"),
                "baseline": signal.get("baseline"),
                "abs_vol_24h_est": signal.get("abs_vol_24h_est"),
                "would_pass_prod_min_vol": signal.get("would_pass_prod_min_vol"),
                "variant": signal.get("variant"),
                "ts": signal.get("ts"),
            },
            "relvol_factor": signal.get("factor"),
            "quote_vol": signal.get("qvol"),
        }

        try:
            from core.config import get_bot_config
            from core.models import TradeOrder
            from services.trading_service import TradingService

            conf = get_bot_config()
            ts = TradingService(conf)
            order = TradeOrder(
                type="BUY",
                symbol=sym,
                price=price,
                amount=0,
                usdt_amount=float(usdt),
                source=SOURCE,
                signal="GAINER_RELVOL",
            )
            result = ts.execute_order(
                order,
                str(tcfg.get("timeframe") or "1h"),
                source=SOURCE,
                request_extra=request_extra,
            )
        except Exception as e:
            log(f"relvol trade execute error {tenant_id} {sym}: {e}", "WARNING")
            return {
                "ok": False,
                "executed": False,
                "message": f"execute_error:{e}"[:200],
                "tenant_id": tenant_id,
            }

        executed = bool(
            getattr(result, "executed", False)
            or (isinstance(result, dict) and result.get("executed"))
        )
        message = getattr(result, "message", None) or (
            result.get("message") if isinstance(result, dict) else ""
        )
        order_id = getattr(result, "order_id", None) or (
            result.get("order_id") if isinstance(result, dict) else ""
        )
        if executed:
            _bump_day_buy(tenant_id)
            # tag entry_source on position for open-count
            try:
                from strategies.positions import get_position, flush_positions

                pos = get_position(sym, str(tcfg.get("timeframe") or "1h"))
                if isinstance(pos, dict):
                    pos["entry_source"] = SOURCE
                    flush_positions(force=True)
            except Exception:
                pass
            log(
                f"relvol TRADE BUY tenant={tenant_id} {sym} usdt={usdt:.0f} "
                f"factor={signal.get('factor')} qvol={signal.get('qvol')}",
                "INFO",
            )
        return {
            "ok": True,
            "executed": executed,
            "message": message or ("filled" if executed else "not_executed"),
            "symbol": sym,
            "usdt": usdt,
            "source": SOURCE,
            "order_id": order_id,
            "tenant_id": tenant_id,
        }


def run_relvol_shadow_once(config: dict | None = None) -> dict[str, Any]:
    cfg = relvol_shadow_config(config)
    if not relvol_shadow_enabled(config):
        return {"ok": False, "reason": "disabled"}

    stats: dict[str, Any] = {
        "ok": True,
        "mode": cfg.get("mode"),
        "symbols": 0,
        "fetched": 0,
        "signals": 0,
        "trades_ok": 0,
        "trades_fail": 0,
        "errors": 0,
    }
    try:
        from services.gainer_universe.config import gainer_universe_config
        from services.gainer_universe.scanner import fetch_gate_tickers

        tickers = fetch_gate_tickers()
        gu = gainer_universe_config(config)
        syms = discovery_symbols_from_tickers(tickers, cfg, blacklist_cfg=gu)
        stats["symbols"] = len(syms)
        path = _log_path(cfg)
        limit = int(cfg.get("ohlcv_limit") or 30)
        workers = max(1, int(cfg.get("fetch_workers") or 6))
        trade = cfg.get("mode") == "trade"
        tenants = list(cfg.get("tenants") or ["default"])
        if not tenants:
            tenants = ["default"]

        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _one(sym: str) -> dict | None:
            bars = _fetch_ohlcv_1h(sym, limit)
            if not bars:
                return None
            return scan_symbol(sym, bars, cfg)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_one, s): s for s in syms}
            for f in as_completed(futs):
                try:
                    row = f.result()
                    stats["fetched"] += 1
                    if not row:
                        continue
                    stats["signals"] += 1
                    trade_results = []
                    if trade:
                        for tid in tenants:
                            tr = try_execute_relvol_buy(row, config, tenant_id=str(tid))
                            trade_results.append(tr)
                            if tr.get("executed"):
                                stats["trades_ok"] += 1
                            else:
                                stats["trades_fail"] += 1
                        row["trade_results"] = trade_results
                    _append_jsonl(path, row)
                    log(
                        f"relvol FIRE {row.get('symbol')} mode={cfg.get('mode')} "
                        f"factor={row.get('factor')} qvol={row.get('qvol')} "
                        f"abs24≈{row.get('abs_vol_24h_est')} "
                        f"pass500k={row.get('would_pass_prod_min_vol')} "
                        f"trades_ok={sum(1 for t in trade_results if t.get('executed'))}",
                        "INFO",
                    )
                except Exception:
                    stats["errors"] += 1
        log(
            f"relvol scan mode={cfg.get('mode')} symbols={stats['symbols']} "
            f"signals={stats['signals']} trades_ok={stats['trades_ok']} "
            f"err={stats['errors']} log={path}",
            "INFO",
        )
    except Exception as e:
        stats["ok"] = False
        stats["reason"] = str(e)[:200]
        log(f"relvol scan failed (fail-open): {e}", "WARNING")
    return stats


def maybe_run_relvol_shadow(config: dict | None = None) -> dict[str, Any]:
    """Optional bot-side OHLCV mass-scan — **off by default**.

    Primary RelVol path is gainer_signal REST seed + spot.tickers WS
    (see services/gainer_signal/relvol_tracker.py + ws_loop).
    """
    global _last_run_mono
    if not relvol_shadow_enabled(config):
        return {}
    cfg = relvol_shadow_config(config)
    if not bool(cfg.get("bot_ohlcv_scan", False)):
        return {"skipped": True, "reason": "ws_primary_bot_ohlcv_scan_off"}
    poll = float(cfg.get("poll_sec") or 3600)
    with _lock:
        now_m = time.monotonic()
        if _last_run_mono and (now_m - _last_run_mono) < poll:
            return {"skipped": True, "reason": "poll"}
        _last_run_mono = now_m

    def _bg():
        try:
            run_relvol_shadow_once(config)
        except Exception as e:
            log(f"relvol bg failed: {e}", "WARNING")

    threading.Thread(target=_bg, name="relvol-trade", daemon=True).start()
    return {"started": True, "mode": cfg.get("mode")}
