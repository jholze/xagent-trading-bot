"""Shadow RelVol ignition scanner — log only, no orders.

Kill: gainer_relvol_shadow.enabled=false
"""

from __future__ import annotations

import json
import os
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

_DEFAULT: dict[str, Any] = {
    "enabled": False,
    "mode": "shadow",  # shadow | off
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
    "min_volume_discover": 0.0,  # no 500k cut for discovery scan list
    "prod_min_vol_compare": 500_000.0,
    "fetch_workers": 6,
    "log_path": "",  # default: LOG_DIR/gainer_relvol_shadow.jsonl
}

_lock = threading.Lock()
_last_run_mono: float = 0.0
_last_fire_ts: dict[str, float] = {}  # symbol -> unix last signal


def relvol_shadow_config(config: dict | None = None) -> dict[str, Any]:
    raw: dict = {}
    if isinstance(config, dict):
        block = config.get("gainer_relvol_shadow")
        if isinstance(block, dict):
            raw = block
    out = {**_DEFAULT, **raw}
    out["enabled"] = bool(out.get("enabled", False))
    mode = str(out.get("mode") or "shadow").strip().lower()
    if mode not in ("shadow", "off"):
        mode = "shadow"
    out["mode"] = mode
    out["poll_sec"] = max(300.0, float(out.get("poll_sec") or 3600))
    out["max_symbols"] = max(20, int(out.get("max_symbols") or 150))
    out["ohlcv_limit"] = max(int(out.get("win") or 12) + 5, int(out.get("ohlcv_limit") or 30))
    return out


def relvol_shadow_enabled(config: dict | None = None) -> bool:
    cfg = relvol_shadow_config(config)
    return bool(cfg.get("enabled")) and cfg.get("mode") != "off"


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
    """USDT spot symbols ranked by 24h quote vol — **no** 500k floor."""
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
    """Return ccxt-style bars [ts_ms, o, h, l, c, base_vol]."""
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
    """Evaluate last closed bar only; honor process cooldown."""
    if len(bars) < int(cfg.get("win") or 12) + 1:
        return None
    # Drop incomplete current hour if last bar is "now"
    now_ts = now_ts or time.time()
    last = bars[-1]
    last_ts = int(last[0])
    if last_ts > 10_000_000_000:
        last_ts_s = last_ts // 1000
    else:
        last_ts_s = last_ts
    # If bar started less than ~55 min ago, treat as incomplete → use previous
    if now_ts - last_ts_s < 55 * 60:
        bars = bars[:-1]
    if len(bars) < int(cfg.get("win") or 12) + 1:
        return None

    sigs = find_signals_ccxt(
        symbol,
        bars,
        mult=float(cfg.get("mult") or 10),
        win=int(cfg.get("win") or 12),
        cooldown_h=1,  # process-level cooldown below
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
    sig["mode"] = "shadow"
    return sig


def run_relvol_shadow_once(config: dict | None = None) -> dict[str, Any]:
    """One scan pass. Fail-open. Returns stats."""
    cfg = relvol_shadow_config(config)
    if not relvol_shadow_enabled(config):
        return {"ok": False, "reason": "disabled"}

    stats: dict[str, Any] = {
        "ok": True,
        "symbols": 0,
        "fetched": 0,
        "signals": 0,
        "errors": 0,
    }
    try:
        from services.gainer_universe.scanner import fetch_gate_tickers
        from services.gainer_universe.config import gainer_universe_config

        tickers = fetch_gate_tickers()
        gu = gainer_universe_config(config)
        syms = discovery_symbols_from_tickers(tickers, cfg, blacklist_cfg=gu)
        stats["symbols"] = len(syms)
        path = _log_path(cfg)
        limit = int(cfg.get("ohlcv_limit") or 30)
        workers = max(1, int(cfg.get("fetch_workers") or 6))

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
                    if row:
                        _append_jsonl(path, row)
                        stats["signals"] += 1
                        log(
                            f"relvol_shadow FIRE {row.get('symbol')} "
                            f"factor={row.get('factor')} qvol={row.get('qvol')} "
                            f"abs24≈{row.get('abs_vol_24h_est')} "
                            f"pass500k={row.get('would_pass_prod_min_vol')}",
                            "INFO",
                        )
                except Exception:
                    stats["errors"] += 1
        log(
            f"relvol_shadow scan symbols={stats['symbols']} signals={stats['signals']} "
            f"err={stats['errors']} log={path}",
            "INFO",
        )
    except Exception as e:
        stats["ok"] = False
        stats["reason"] = str(e)[:200]
        log(f"relvol_shadow scan failed (fail-open): {e}", "WARNING")
    return stats


def maybe_run_relvol_shadow(config: dict | None = None) -> dict[str, Any]:
    """Rate-limited entry from trading cycle / gainer refresh."""
    global _last_run_mono
    if not relvol_shadow_enabled(config):
        return {}
    cfg = relvol_shadow_config(config)
    poll = float(cfg.get("poll_sec") or 3600)
    with _lock:
        now_m = time.monotonic()
        if _last_run_mono and (now_m - _last_run_mono) < poll:
            return {"skipped": True, "reason": "poll"}
        # reserve slot before heavy work so parallel cycles don't stampede
        _last_run_mono = now_m

    # Background so cycle / health stay free
    def _bg():
        try:
            run_relvol_shadow_once(config)
        except Exception as e:
            log(f"relvol_shadow bg failed: {e}", "WARNING")

    threading.Thread(target=_bg, name="relvol-shadow", daemon=True).start()
    return {"started": True}
