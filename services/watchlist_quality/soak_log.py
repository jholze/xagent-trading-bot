"""Durable soak observability under LOG_DIR (Railway volume /app/logs).

R15 (#155): cycle_summary.jsonl, risk_rejects.jsonl, boot_fingerprint.
Fail-open. Independent of observability.json_logs.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from logger import LOG_DIR, log

CYCLE_SUMMARY_LOG = os.path.join(LOG_DIR, "cycle_summary.jsonl")
RISK_REJECTS_LOG = os.path.join(LOG_DIR, "risk_rejects.jsonl")


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _wq_section(config: dict | None) -> dict:
    if config is None:
        try:
            from core.config import get_bot_config

            config = get_bot_config().raw
        except Exception:
            config = {}
    sec = (config or {}).get("watchlist_quality")
    return sec if isinstance(sec, dict) else {}


def _flag_enabled(
    *,
    env_key: str,
    config_key: str,
    config: dict | None,
    default_when_mode_active: bool = True,
) -> bool:
    """Env beats config; default on when WQE mode is shadow/soft/enforce."""
    try:
        env = (os.environ.get(env_key) or "").strip().lower()
        if env in ("0", "false", "no", "off"):
            return False
        if env in ("1", "true", "yes", "on"):
            return True
        wq = _wq_section(config)
        if config_key in wq:
            return bool(wq.get(config_key))
        if not default_when_mode_active:
            return True
        from services.watchlist_quality.config import wqe_mode

        return wqe_mode(config) in ("shadow", "soft", "enforce")
    except Exception:
        return True


def cycle_summary_enabled(config: dict | None = None) -> bool:
    return _flag_enabled(
        env_key="WQE_CYCLE_SUMMARY",
        config_key="cycle_summary_log",
        config=config,
    )


def risk_reject_enabled(config: dict | None = None) -> bool:
    return _flag_enabled(
        env_key="WQE_RISK_REJECT_LOG",
        config_key="risk_reject_log",
        config=config,
    )


def _append(path: str, rec: dict[str, Any]) -> None:
    try:
        from services.observability_store import append_jsonl, maybe_rotate_jsonl

        append_jsonl(path, rec)
        maybe_rotate_jsonl(path, max_bytes=8_000_000, keep_lines=5_000)
    except Exception as e:
        log(f"soak_log write failed ({path}): {e}", "DEBUG")


def log_soak_event(
    path: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    human: str | None = None,
    level: str = "INFO",
) -> None:
    """Low-level append; always attempts human line if provided."""
    rec: dict[str, Any] = {
        "ts": _now_ts(),
        "type": str(event_type or "soak"),
        **(payload or {}),
    }
    if human:
        try:
            log(human, level)
        except Exception:
            pass
    try:
        _append(path, rec)
    except Exception:
        pass


def log_boot_fingerprint(config: dict | None = None) -> None:
    """Once per process boot: config proof for soak (mode, volume, commit)."""
    try:
        from core.build_info import get_build_info
        from core.runtime_identity import resolve_bot_stack
        from services.watchlist_quality.config import wqe_mode
        from services.watchlist_quality.event_log import _enabled as wqe_event_log_enabled

        if config is None:
            try:
                from core.config import get_bot_config

                config = get_bot_config().raw
            except Exception:
                config = {}

        build = get_build_info()
        mode = wqe_mode(config)
        stack = resolve_bot_stack()
        volume_mount = (
            (os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or "").strip() or "/app/logs"
        )
        volume_name = (os.environ.get("RAILWAY_VOLUME_NAME") or "").strip() or ""
        demo = (os.environ.get("DEMO_MODE") or "").strip() in ("1", "true", "yes")
        try:
            from data_manager import is_demo_mode, resolve_ledger_backend

            demo = is_demo_mode()
            ledger = resolve_ledger_backend(
                "demo" if demo else "live", config or {}
            )
        except Exception:
            ledger = (os.environ.get("DEMO_LEDGER_BACKEND") or "file").strip()

        payload = {
            "commit": build.get("commit"),
            "branch": build.get("branch"),
            "dirty": bool(build.get("dirty")),
            "stack": stack,
            "wqe_mode": mode,
            "wqe_event_log": bool(wqe_event_log_enabled(config)),
            "cycle_summary_log": cycle_summary_enabled(config),
            "risk_reject_log": risk_reject_enabled(config),
            "volume_mount": volume_mount,
            "volume_name": volume_name or None,
            "demo_mode": demo,
            "ledger_backend": ledger,
            "log_dir": LOG_DIR,
        }
        human = (
            f"WQE boot mode={mode} volume={volume_mount} "
            f"commit={build.get('commit')} branch={build.get('branch')} stack={stack}"
        )
        # Always human line (ops proof); JSON when cycle_summary_log on (or mode active)
        try:
            log(human, "INFO")
        except Exception:
            pass
        if cycle_summary_enabled(config) or mode in ("shadow", "soft", "enforce"):
            log_soak_event(
                CYCLE_SUMMARY_LOG,
                "boot_fingerprint",
                payload,
            )
    except Exception as e:
        try:
            log(f"boot_fingerprint failed: {e}", "DEBUG")
        except Exception:
            pass


def build_cycle_summary_record(
    *,
    config: dict | None = None,
    duration_sec: float | None = None,
    n_watchlist: int | None = None,
    n_open_positions: int | None = None,
    coin_results: list | None = None,
    eval_queue_depth: int | None = None,
    eval_processed_delta: int | None = None,
    tenant_id: str = "default",
) -> dict[str, Any]:
    """Best-effort cycle snapshot — no network required when inputs passed in."""
    if config is None:
        try:
            from core.config import get_bot_config

            config = get_bot_config().raw
        except Exception:
            config = {}

    from services.watchlist_quality.config import wqe_mode

    mode = wqe_mode(config)
    commit = "unknown"
    stack = "unknown"
    try:
        from core.build_info import get_build_info
        from core.runtime_identity import resolve_bot_stack

        commit = get_build_info().get("commit") or "unknown"
        stack = resolve_bot_stack()
    except Exception:
        pass

    if n_watchlist is None:
        try:
            from data_manager import load_effective_watchlist

            wl = load_effective_watchlist() or []
            n_watchlist = len([c for c in wl if isinstance(c, dict) and c.get("active", True)])
        except Exception:
            n_watchlist = None

    if n_open_positions is None:
        try:
            from strategies.positions import count_open_positions

            n_open_positions = int(count_open_positions())
        except Exception:
            n_open_positions = None

    if eval_queue_depth is None:
        try:
            from bus.eval_queue import eval_queue_enabled, queue_depth

            if eval_queue_enabled(config):
                eval_queue_depth = int(queue_depth())
        except Exception:
            eval_queue_depth = None

    buys_attempted = buys_filled = buys_blocked = sells = 0
    for r in coin_results or []:
        if not isinstance(r, dict):
            continue
        act = str(r.get("normalized_action") or r.get("action") or "").upper()
        order_type = str(r.get("order_type") or "").upper()
        executed = bool(r.get("executed") or r.get("trade_executed"))
        is_buy = act in ("BUY", "BUY_DCA", "DCA") or order_type == "BUY"
        is_sell = act.startswith("SELL") or order_type == "SELL"
        if is_buy:
            buys_attempted += 1
            if executed:
                buys_filled += 1
            elif r.get("blocked") or r.get("risk_blocked"):
                buys_blocked += 1
        elif is_sell:
            sells += 1

    market_regime = None
    market_block_buys = None
    market_size_mult = None
    try:
        from services.market_policy_fusion import get_global_market_bias

        bias = get_global_market_bias(config) or {}
        market_regime = bias.get("regime")
        market_block_buys = bool(bias.get("block_buys"))
        market_size_mult = bias.get("size_mult")
    except Exception:
        pass

    sensor_policy = None
    try:
        sp = (config or {}).get("sensor_entry") or (config or {}).get("entry_sensor") or {}
        if isinstance(sp, dict):
            sensor_policy = sp.get("policy") or sp.get("mode")
    except Exception:
        pass

    wqe_score_age_sec = None
    wqe_last_sync_n_scored = None
    try:
        from services.watchlist_quality.store import load_quality_scores, score_age_seconds

        wqe_score_age_sec = score_age_seconds()
        data = load_quality_scores()
        coins = data.get("coins") or []
        wqe_last_sync_n_scored = len(coins) if coins else data.get("scored")
    except Exception:
        pass

    return {
        "tenant_id": tenant_id,
        "stack": stack,
        "commit": commit,
        "wqe_mode": mode,
        "n_watchlist": n_watchlist,
        "n_open_positions": n_open_positions,
        "eval_queue_depth": eval_queue_depth,
        "eval_processed_delta": eval_processed_delta
        if eval_processed_delta is not None
        else (len(coin_results) if coin_results is not None else None),
        "buys_attempted": buys_attempted,
        "buys_filled": buys_filled,
        "buys_blocked": buys_blocked,
        "sells": sells,
        "market_regime": market_regime,
        "market_block_buys": market_block_buys,
        "market_size_mult": market_size_mult,
        "sensor_policy": sensor_policy,
        "wqe_score_age_sec": wqe_score_age_sec,
        "wqe_last_sync_n_scored": wqe_last_sync_n_scored,
        "duration_sec": round(float(duration_sec), 2) if duration_sec is not None else None,
    }


def log_cycle_summary(
    payload: dict[str, Any] | None = None,
    *,
    config: dict | None = None,
    **kwargs: Any,
) -> None:
    """Append one cycle_summary row (fail-open)."""
    if not cycle_summary_enabled(config):
        return
    try:
        if payload is None:
            payload = build_cycle_summary_record(config=config, **kwargs)
        else:
            payload = dict(payload)
        log_soak_event(CYCLE_SUMMARY_LOG, "cycle_summary", payload)
        try:
            log(
                "cycle_summary "
                f"mode={payload.get('wqe_mode')} wl={payload.get('n_watchlist')} "
                f"pos={payload.get('n_open_positions')} "
                f"dur={payload.get('duration_sec')}s "
                f"regime={payload.get('market_regime')}",
                "INFO",
            )
        except Exception:
            pass
    except Exception as e:
        log(f"cycle_summary log failed: {e}", "DEBUG")


def log_risk_reject(
    *,
    symbol: str,
    side: str = "BUY",
    source: str = "",
    code: str = "",
    message: str = "",
    quality_score: Any = None,
    quality_shadow_ai: Any = None,
    tenant_id: str | None = None,
    config: dict | None = None,
    wqe_mode_value: str | None = None,
) -> None:
    """One row per RiskDecision reject on BUY (all codes). Fail-open."""
    if not risk_reject_enabled(config):
        return
    try:
        from services.watchlist_quality.config import wqe_mode

        mode = wqe_mode_value if wqe_mode_value is not None else wqe_mode(config)
        if tenant_id is None:
            try:
                from core.tenant_context import current_tenant_id

                tenant_id = current_tenant_id() or "default"
            except Exception:
                tenant_id = "default"

        # Enrich quality from score file when missing (WQE + others)
        if quality_score is None or quality_shadow_ai is None:
            try:
                from services.watchlist_quality.store import load_quality_scores

                data = load_quality_scores(tenant_id=tenant_id)
                for c in data.get("coins") or []:
                    if isinstance(c, dict) and c.get("symbol") == symbol:
                        if quality_score is None:
                            quality_score = c.get("quality_score")
                        if quality_shadow_ai is None:
                            quality_shadow_ai = c.get("quality_shadow_ai")
                        break
            except Exception:
                pass

        payload = {
            "symbol": symbol,
            "side": side,
            "source": source,
            "code": code or "unknown",
            "message": (message or "")[:300],
            "wqe_mode": mode,
            "quality_score": quality_score,
            "quality_shadow_ai": quality_shadow_ai,
            "tenant_id": tenant_id,
        }
        log_soak_event(RISK_REJECTS_LOG, "risk_reject", payload)
        try:
            log(
                f"risk_reject code={payload['code']} symbol={symbol} "
                f"source={source} mode={mode}",
                "INFO",
            )
        except Exception:
            pass
    except Exception as e:
        log(f"risk_reject log failed: {e}", "DEBUG")


def last_cycle_summary_age_sec(path: str | None = None) -> float | None:
    """Seconds since last cycle_summary line; None if missing."""
    path = path or CYCLE_SUMMARY_LOG
    try:
        from services.observability_store import tail_jsonl

        rows = tail_jsonl(path, limit=20)
        for rec in reversed(rows):
            if not isinstance(rec, dict):
                continue
            if rec.get("type") not in ("cycle_summary", "boot_fingerprint"):
                continue
            # Prefer real cycle_summary for age display
            if rec.get("type") != "cycle_summary":
                continue
            ts = rec.get("ts") or ""
            if not ts:
                continue
            u = str(ts).replace("Z", "+00:00")
            dt = datetime.fromisoformat(u)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
        # fall back to any last record
        if rows:
            rec = rows[-1]
            ts = rec.get("ts") or ""
            if ts:
                u = str(ts).replace("Z", "+00:00")
                dt = datetime.fromisoformat(u)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return None
    return None


def cycle_summary_path() -> str:
    return CYCLE_SUMMARY_LOG


def risk_rejects_path() -> str:
    return RISK_REJECTS_LOG
