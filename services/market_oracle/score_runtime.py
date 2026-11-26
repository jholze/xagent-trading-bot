"""Oracle-poll and buy-path wiring for the regime score (#835).

The math lives in ``score.py``. This module reads committed config, keeps
the EMA memory, publishes a score block on the oracle snapshot, and applies
the shadow / enforce trend-filter decision. Failures are logged and treated
as a missing score. They do not raise.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

from services.market_oracle.score import (
    MODE_ENFORCE,
    MODE_OFF,
    clamp_not_looser,
    components,
    filter_params,
    format_shadow_line,
    history_cutoff,
    raw_score,
    resolve_mode,
    score_is_fresh,
    should_write_history,
    smooth_score,
    weights_of,
)

log = logging.getLogger("market_oracle.score")

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.json"
_BLOCK_CACHE: dict | None = None
HISTORY_COLLECTION = "obs_oracle_score"
SHADOW_COLLECTION = "obs_oracle_score_shadow"
STATE_COLLECTION = "oracle_score_state"
STATE_ID = "latest"


def reset_for_tests() -> None:
    global _BLOCK_CACHE
    _BLOCK_CACHE = None


def _pytest_running() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def committed_oracle_score() -> dict:
    """``rule_switches.oracle_score`` from the committed config file."""
    global _BLOCK_CACHE
    if _BLOCK_CACHE is not None:
        return _BLOCK_CACHE
    try:
        with _CONFIG_PATH.open(encoding="utf-8") as handle:
            raw = json.load(handle)
        block = ((raw or {}).get("rule_switches") or {}).get("oracle_score")
        _BLOCK_CACHE = block if isinstance(block, dict) else {}
    except Exception:
        log.warning("oracle score config unreadable", exc_info=True)
        _BLOCK_CACHE = {}
    return _BLOCK_CACHE


def read_tenant_oracle_block(tenant_id: str | None) -> dict | None:
    """Tenant overlay ``rule_switches.oracle_score``, or None when unset.

    Unit tests skip Mongo. A read failure is "unset", not a crash.
    """
    if not tenant_id:
        return None
    if _pytest_running() and os.environ.get("ORACLE_SCORE_READ_TENANT") != "1":
        return None
    try:
        from data_manager import _load_default_config_from_disk
        from storage.tenant_meta_store import load_tenant_config_body

        body = load_tenant_config_body(
            str(tenant_id), default_cfg=_load_default_config_from_disk()
        )
    except Exception:
        log.warning("oracle score tenant config unreadable tenant=%s", tenant_id, exc_info=True)
        return None
    if not isinstance(body, dict):
        return None
    switches = body.get("rule_switches")
    block = switches.get("oracle_score") if isinstance(switches, dict) else None
    return block if isinstance(block, dict) else None


def current_mode(tenant_id: str | None) -> str:
    try:
        return resolve_mode(
            read_tenant_oracle_block(tenant_id),
            committed_oracle_score(),
            tenant_id,
        )
    except Exception:
        log.warning("oracle score mode failed tenant=%s", tenant_id, exc_info=True)
        return MODE_OFF


def read_published_score() -> dict | None:
    """Latest score block on the oracle snapshot.

    A Redis timeout is a missing score (no in-process fallback). No Redis
    falls back to the in-process snapshot so a single-process bot and the
    unit tests still see an ingested snapshot.
    """
    snap = None
    timed_out = False
    try:
        from services.market_oracle.store import (
            OracleReadTimeout,
            get_order_path_redis_snapshot,
        )

        snap = get_order_path_redis_snapshot()
    except OracleReadTimeout:
        timed_out = True
        snap = None
    except Exception:
        log.warning("oracle score snapshot read failed", exc_info=True)
        snap = None
    if snap is None and not timed_out:
        try:
            from services.market_oracle.store import get_latest_snapshot

            snap = get_latest_snapshot(allow_redis=False)
        except Exception:
            log.warning("oracle score in-process snapshot read failed", exc_info=True)
            snap = None
    block = (snap or {}).get("oracle_score") if isinstance(snap, dict) else None
    return block if isinstance(block, dict) else None


def _specs(block: dict) -> list:
    specs = block.get("components")
    return specs if isinstance(specs, list) else []


def _mapping(block: dict) -> dict:
    mapping = block.get("mapping")
    out = dict(mapping) if isinstance(mapping, dict) else {}
    if "sma_days_min" not in out and block.get("sma_days_min") is not None:
        out["sma_days_min"] = block.get("sma_days_min")
    return out


def compute_poll(features: dict | None, memory: dict | None, block: dict, now_ts: float) -> tuple[dict | None, dict]:
    """One poll. Returns ``(published_block or None, new_memory)``. Raises on bad math
    only if the inputs themselves raise; callers still guard this.
    """
    mem = dict(memory or {})
    parts = components(features or {}, _specs(block))
    weights = weights_of(_specs(block))
    raw = raw_score(parts, weights)
    if raw is None and not parts:
        return None, mem
    smoothed, _reset = smooth_score(
        mem.get("smoothed"),
        mem.get("ts"),
        raw,
        now_ts,
        half_life_sec=block.get("half_life_sec"),
        max_gap_sec=block.get("max_gap_sec"),
    )
    if smoothed is None:
        return None, mem
    published = {
        "raw": None if raw is None else round(float(raw), 6),
        "smoothed": round(float(smoothed), 6),
        "components": {str(k): round(float(v), 6) for k, v in sorted(parts.items())},
        "ts": float(now_ts),
    }
    mem["smoothed"] = published["smoothed"]
    mem["raw"] = published["raw"]
    mem["ts"] = float(now_ts)
    mem["components"] = published["components"]
    return published, mem


def _mongo_enabled() -> bool:
    if _pytest_running() and os.environ.get("ORACLE_SCORE_MONGO") != "1":
        return False
    return True


def load_persisted_memory() -> dict | None:
    if not _mongo_enabled():
        return None
    try:
        from storage.mongo_client import get_database

        doc = get_database()[STATE_COLLECTION].find_one({"_id": STATE_ID})
    except Exception:
        log.warning("oracle score state load failed", exc_info=True)
        return None
    if not isinstance(doc, dict):
        return None
    return {
        "smoothed": doc.get("smoothed"),
        "raw": doc.get("raw"),
        "ts": doc.get("ts"),
        "components": doc.get("components") or {},
        "last_history_ts": doc.get("last_history_ts"),
    }


def _default_sink(op: str, payload: dict) -> None:
    if not _mongo_enabled():
        return
    try:
        from storage.mongo_client import assert_safe_dev_db_mutation, get_database

        db = get_database()
        assert_safe_dev_db_mutation(db.name, action="write")
        if op == "state":
            db[STATE_COLLECTION].update_one({"_id": STATE_ID}, {"$set": payload}, upsert=True)
        elif op == "history":
            cutoff = payload.get("cutoff")
            row = payload.get("row")
            if cutoff is not None:
                db[HISTORY_COLLECTION].delete_many({"ts": {"$lt": float(cutoff)}})
            if isinstance(row, dict):
                db[HISTORY_COLLECTION].insert_one(dict(row))
    except Exception:
        log.warning("oracle score persist failed op=%s", op, exc_info=True)


def apply_poll_score(
    features: dict | None,
    state: dict,
    now: float | None = None,
    *,
    block: dict | None = None,
    sink: Callable[[str, dict], None] | None = None,
) -> dict | None:
    """Update ``state`` and return the published score block. Never raises."""
    try:
        import time

        now_ts = float(time.time() if now is None else now)
        cfg = block if isinstance(block, dict) else committed_oracle_score()
        if not state.get("oracle_score_memory_loaded"):
            loaded = load_persisted_memory()
            if loaded and not state.get("oracle_score_memory"):
                state["oracle_score_memory"] = loaded
            state["oracle_score_memory_loaded"] = True
        published, memory = compute_poll(
            features, state.get("oracle_score_memory"), cfg, now_ts
        )
        state["oracle_score_memory"] = memory
        if published is None:
            return None
        writer = sink or _default_sink
        writer(
            "state",
            {
                "smoothed": memory.get("smoothed"),
                "raw": memory.get("raw"),
                "ts": memory.get("ts"),
                "components": memory.get("components") or {},
                "last_history_ts": memory.get("last_history_ts"),
            },
        )
        if should_write_history(
            memory.get("last_history_ts"), now_ts, cfg.get("history_min_interval_sec")
        ):
            cutoff = history_cutoff(now_ts, cfg.get("history_retain_sec"))
            row = {
                "kind": "oracle_score",
                "raw": published.get("raw"),
                "smoothed": published.get("smoothed"),
                "components": published.get("components") or {},
                "ts": now_ts,
            }
            writer("history", {"row": row, "cutoff": cutoff})
            memory["last_history_ts"] = now_ts
            state["oracle_score_memory"] = memory
        return published
    except Exception:
        log.warning("oracle score poll failed", exc_info=True)
        return None


def _floors(config_raw: dict | None) -> dict[str, float | None]:
    mcap = None
    liq = None
    try:
        from strategies.short_policy import resolve_long_market_cap_min_usd

        mcap = float(resolve_long_market_cap_min_usd(None, config_raw))
    except Exception:
        log.warning("oracle score mcap floor unreadable", exc_info=True)
        mcap = None
    try:
        risk = (config_raw or {}).get("risk") if isinstance(config_raw, dict) else None
        venue = risk.get("venue_quality") if isinstance(risk, dict) else None
        if isinstance(venue, dict) and venue.get("min_quote_volume_24h_usdt") is not None:
            liq = float(venue["min_quote_volume_24h_usdt"])
    except (TypeError, ValueError):
        liq = None
    return {"mcap_floor_usd": mcap, "liq_floor_usdt": liq}


def _base_cfg(trend_cfg: dict, floors: dict) -> dict:
    return {
        "sma_days": trend_cfg.get("sma_days"),
        "max_extension_pct": trend_cfg.get("max_extension_pct"),
        "max_pump_24h_pct": trend_cfg.get("max_pump_24h_pct"),
        "mcap_floor_usd": floors.get("mcap_floor_usd"),
        "liq_floor_usdt": floors.get("liq_floor_usdt"),
    }


def _record_shadow(row: dict) -> None:
    try:
        from services.observe_streams import mirror_record

        mirror_record(SHADOW_COLLECTION, row)
    except Exception:
        log.warning("oracle score shadow obs failed", exc_info=True)


def _emit(line: str) -> None:
    try:
        from logger import log as bot_log

        bot_log(line, "INFO")
    except Exception:
        log.info("%s", line)


def scored_trend_decision(
    *,
    trend_cfg: dict,
    hourly: list,
    fetch_daily: Callable[[int], Any],
    now: int,
    symbol: str,
    tenant_id: str,
    config_raw: dict | None,
    score_block: dict | None = None,
    committed: dict | None = None,
    mode: str | None = None,
) -> Any:
    """Evaluate the fixed filter and, in shadow/enforce, the score filter.

    The returned check is the one the buy path must use. Shadow always returns
    the fixed check. Enforce returns the score check only when the score is
    fresh. Mapping, logging and the observation write swallow their own errors.
    A candle failure propagates so the buy path can fall back to the fixed check.
    """
    from risk import rule_switches as rs

    cfg_block = committed if isinstance(committed, dict) else committed_oracle_score()
    if score_block is None:
        score_block = read_published_score()
    floors = _floors(config_raw)
    base = _base_cfg(trend_cfg, floors)
    fixed_sma = int(rs._as_float(trend_cfg.get("sma_days"), 50) or 50)
    mapped = None
    fresh = False
    age = None
    smoothed = None
    raw = None
    if isinstance(score_block, dict):
        smoothed = score_block.get("smoothed")
        raw = score_block.get("raw")
        ts = score_block.get("ts")
        try:
            age = float(now) - float(ts)
        except (TypeError, ValueError):
            age = None
        fresh = score_is_fresh(ts, now, cfg_block.get("stale_after_sec"))
        if smoothed is not None:
            try:
                mapped = filter_params(float(smoothed), base, _mapping(cfg_block))
                mapped = clamp_not_looser(mapped, base, mapped.get("s"))
            except Exception:
                log.warning("oracle score mapping failed", exc_info=True)
                mapped = None
    score_sma = fixed_sma
    if isinstance(mapped, dict) and mapped.get("sma_days") is not None:
        score_sma = int(mapped["sma_days"])
    limit = max(fixed_sma, score_sma) + 10
    daily = rs.closed_bars(fetch_daily(limit), now, rs.DAY)
    fixed = rs.evaluate_trend_filter(trend_cfg, daily, hourly)
    score_check = None
    if isinstance(mapped, dict) and mapped.get("sma_days") is not None:
        scored_cfg = dict(trend_cfg)
        scored_cfg["sma_days"] = mapped["sma_days"]
        if mapped.get("max_extension_pct") is not None:
            scored_cfg["max_extension_pct"] = mapped["max_extension_pct"]
        score_check = rs.evaluate_trend_filter(scored_cfg, daily, hourly)
    would_fixed = fixed.block_code is None
    would_score = None if score_check is None else score_check.block_code is None
    if mode is None:
        mode = current_mode(tenant_id)
    prefix = "oracle_score_shadow" if mode != MODE_ENFORCE else "oracle_score_enforce"
    line = format_shadow_line(
        tenant=str(tenant_id),
        symbol=str(symbol),
        s=None if not isinstance(mapped, dict) else mapped.get("s"),
        raw=raw,
        age_s=age,
        sma_days_fixed=fixed_sma,
        sma_days_score=None if not isinstance(mapped, dict) else mapped.get("sma_days"),
        max_ext_fixed=trend_cfg.get("max_extension_pct"),
        max_ext_score=None if not isinstance(mapped, dict) else mapped.get("max_extension_pct"),
        would_pass_fixed=would_fixed,
        would_pass_score=would_score,
        mcap_fixed=floors.get("mcap_floor_usd"),
        mcap_score=None if not isinstance(mapped, dict) else mapped.get("mcap_floor_usd"),
        liq_fixed=floors.get("liq_floor_usdt"),
        liq_score=None if not isinstance(mapped, dict) else mapped.get("liq_floor_usdt"),
        size_round_up=None if not isinstance(mapped, dict) else mapped.get("size_round_up_usdt"),
        prefix=prefix,
    )
    _emit(line)
    row = {
        "kind": "oracle_score_shadow",
        "tenant_id": str(tenant_id),
        "symbol": str(symbol),
        "mode": mode,
        "s": None if not isinstance(mapped, dict) else mapped.get("s"),
        "raw": raw,
        "smoothed": smoothed,
        "age_s": age,
        "fresh": fresh,
        "sma_days_fixed": fixed_sma,
        "sma_days_score": None if not isinstance(mapped, dict) else mapped.get("sma_days"),
        "max_ext_fixed": trend_cfg.get("max_extension_pct"),
        "max_ext_score": None if not isinstance(mapped, dict) else mapped.get("max_extension_pct"),
        "would_pass_fixed": would_fixed,
        "would_pass_score": would_score,
        "mcap_floor_fixed": floors.get("mcap_floor_usd"),
        "mcap_floor_score": None if not isinstance(mapped, dict) else mapped.get("mcap_floor_usd"),
        "liq_floor_fixed": floors.get("liq_floor_usdt"),
        "liq_floor_score": None if not isinstance(mapped, dict) else mapped.get("liq_floor_usdt"),
        "size_round_up": None if not isinstance(mapped, dict) else mapped.get("size_round_up_usdt"),
        "applied": mode == MODE_ENFORCE and fresh and score_check is not None,
        "decision_code_fixed": fixed.block_code,
        "decision_code_score": None if score_check is None else score_check.block_code,
        "ts": float(now),
    }
    _record_shadow(row)
    if mode == MODE_ENFORCE and fresh and score_check is not None:
        return score_check
    return fixed


def fixed_trend_check(trend_cfg: dict, hourly: list, fetch_daily: Callable[[int], Any], now: int):
    """The pre-#835 trend check. Same bar limit and the same evaluator."""
    from risk import rule_switches as rs

    sma_days = int(rs._as_float(trend_cfg.get("sma_days"), 50) or 50)
    daily = rs.closed_bars(fetch_daily(sma_days + 10), now, rs.DAY)
    return rs.evaluate_trend_filter(trend_cfg, daily, hourly)
