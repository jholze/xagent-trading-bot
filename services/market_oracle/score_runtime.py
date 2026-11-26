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

import queue
import threading

from services.market_oracle.score import (
    MODE_ENFORCE,
    MODE_OFF,
    clamp_not_looser,
    components,
    filter_params,
    format_shadow_line,
    history_cutoff,
    raw_score,
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
_HISTORY_QUEUE: queue.Queue = queue.Queue(maxsize=64)
_HISTORY_WORKER_STARTED = False
_PERSIST_LOCK = threading.Lock()
_PERSIST_ERROR = {"logged": False, "last": 0.0}


def reset_for_tests() -> None:
    global _BLOCK_CACHE
    _BLOCK_CACHE = None
    _PERSIST_ERROR["logged"] = False
    _PERSIST_ERROR["last"] = 0.0
    while True:
        try:
            _HISTORY_QUEUE.get_nowait()
        except queue.Empty:
            break


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
    return dict(mapping) if isinstance(mapping, dict) else {}


def compute_poll(features: dict | None, memory: dict | None, block: dict, now_ts: float) -> tuple[dict | None, dict]:
    """One poll. Returns ``(published_block or None, new_memory)``. Raises on bad math
    only if the inputs themselves raise; callers still guard this.
    """
    mem = dict(memory or {})
    parts = components(features or {}, _specs(block))
    weights = weights_of(_specs(block))
    raw = raw_score(parts, weights)
    if raw is None:
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


def note_persist_failure(op: str, *, now: float | None = None, interval_sec: float | None = None) -> None:
    """Log ERROR on the first failed persist, then at most once per interval.

    The interval is ``persist_error_interval_sec`` from the committed block
    when the caller does not pass one.
    """
    import time

    if interval_sec is None:
        try:
            interval_sec = _configured_float(
                committed_oracle_score(), "persist_error_interval_sec", 300.0
            )
        except Exception:
            interval_sec = 300.0
    stamp = time.time() if now is None else float(now)
    with _PERSIST_LOCK:
        first = not _PERSIST_ERROR["logged"]
        due = stamp - float(_PERSIST_ERROR["last"]) >= float(interval_sec)
        if not first and not due:
            return
        _PERSIST_ERROR["logged"] = True
        _PERSIST_ERROR["last"] = stamp
    log.error("oracle score persist failed op=%s", op, exc_info=True)


def _redis_enabled() -> bool:
    if _pytest_running() and os.environ.get("ORACLE_SCORE_REDIS") != "1":
        return False
    return True


def _configured_float(block: dict, key: str, default: float) -> float:
    try:
        value = float(block.get(key))
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return value


def _configured_timeout_ms(block: dict) -> int:
    try:
        value = int(block.get("mongo_timeout_ms"))
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def score_state_key() -> str:
    from bus.redis_keys import redis_key_prefix

    return f"{redis_key_prefix()}market_oracle:score_state"


def _redis_client(timeout_sec: float):
    import redis
    from bus.redis_client import redis_url_from_env

    return redis.Redis.from_url(
        redis_url_from_env(),
        socket_timeout=float(timeout_sec),
        socket_connect_timeout=float(timeout_sec),
        decode_responses=True,
    )


def _memory_from_doc(doc: dict) -> dict:
    return {
        "smoothed": doc.get("smoothed"),
        "raw": doc.get("raw"),
        "ts": doc.get("ts"),
        "components": doc.get("components") or {},
        "last_history_ts": doc.get("last_history_ts"),
    }


def load_score_state(client: Any = None, *, timeout_sec: float = 0.5) -> dict | None:
    """EMA restart state from Redis, beside the oracle snapshot. Never raises."""
    own = client is None
    if own and not _redis_enabled():
        return None
    try:
        if own:
            client = _redis_client(timeout_sec)
        raw = client.get(score_state_key())
    except Exception:
        note_persist_failure("redis-load")
        return None
    if not raw:
        return None
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        doc = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        note_persist_failure("redis-load")
        return None
    return _memory_from_doc(doc) if isinstance(doc, dict) else None


def save_score_state(memory: dict, client: Any = None, *, timeout_sec: float = 0.5) -> bool:
    """Write the EMA restart state to Redis. A failure is logged and swallowed."""
    payload = json.dumps(
        {
            "smoothed": memory.get("smoothed"),
            "raw": memory.get("raw"),
            "ts": memory.get("ts"),
            "components": memory.get("components") or {},
            "last_history_ts": memory.get("last_history_ts"),
        }
    )
    own = client is None
    if own and not _redis_enabled():
        return False
    try:
        if own:
            client = _redis_client(timeout_sec)
        client.set(score_state_key(), payload)
        return True
    except Exception:
        note_persist_failure("redis-save")
        return False


def _write_history_mongo(payload: dict, timeout_ms: int) -> None:
    """One history write. Timeouts are the configured millisecond value, each."""
    from pymongo import MongoClient
    from storage.mongo_client import (
        assert_safe_dev_db_mutation,
        resolve_database_name,
        resolve_mongo_uri,
    )

    client = MongoClient(
        resolve_mongo_uri(),
        serverSelectionTimeoutMS=int(timeout_ms),
        socketTimeoutMS=int(timeout_ms),
        connectTimeoutMS=int(timeout_ms),
    )
    try:
        db = client[resolve_database_name()]
        assert_safe_dev_db_mutation(db.name, action="write")
        cutoff = payload.get("cutoff")
        row = payload.get("row")
        if cutoff is not None:
            db[HISTORY_COLLECTION].delete_many({"ts": {"$lt": float(cutoff)}})
        if isinstance(row, dict):
            db[HISTORY_COLLECTION].insert_one(dict(row))
    finally:
        client.close()


def _history_worker() -> None:
    while True:
        try:
            timeout_ms, payload = _HISTORY_QUEUE.get(timeout=0.2)
        except queue.Empty:
            continue
        try:
            _write_history_mongo(payload, int(timeout_ms))
        except Exception:
            note_persist_failure("history")


def _ensure_history_worker() -> None:
    global _HISTORY_WORKER_STARTED
    with _PERSIST_LOCK:
        if _HISTORY_WORKER_STARTED:
            return
        thread = threading.Thread(target=_history_worker, name="oracle-score-history", daemon=True)
        thread.start()
        _HISTORY_WORKER_STARTED = True


def enqueue_history(payload: dict, timeout_ms: int) -> None:
    """Queue a Mongo history write. Returns without waiting for Mongo."""
    if timeout_ms <= 0 or not _mongo_enabled():
        return
    try:
        _ensure_history_worker()
        _HISTORY_QUEUE.put_nowait((int(timeout_ms), payload))
    except queue.Full:
        note_persist_failure("history-queue")
    except Exception:
        note_persist_failure("history-queue")


def apply_poll_score(
    features: dict | None,
    state: dict,
    now: float | None = None,
    *,
    block: dict | None = None,
    sink: Callable[[str, dict], None] | None = None,
    redis_client: Any = None,
) -> dict | None:
    """Update ``state`` and return the published score block. Never raises.

    The EMA restart state is Redis. Mongo history is queued and does not block
    this call. A caller-supplied ``sink`` is invoked in-process for tests.
    """
    try:
        import time

        now_ts = float(time.time() if now is None else now)
        cfg = block if isinstance(block, dict) else committed_oracle_score()
        timeout_sec = _configured_float(cfg, "redis_timeout_sec", 0.5)
        if not state.get("oracle_score_memory_loaded"):
            loaded = load_score_state(redis_client, timeout_sec=timeout_sec)
            if loaded and not state.get("oracle_score_memory"):
                state["oracle_score_memory"] = loaded
            state["oracle_score_memory_loaded"] = True
        published, memory = compute_poll(
            features, state.get("oracle_score_memory"), cfg, now_ts
        )
        state["oracle_score_memory"] = memory
        if published is None:
            return None
        save_score_state(memory, redis_client, timeout_sec=timeout_sec)
        state_row = {
            "smoothed": memory.get("smoothed"),
            "raw": memory.get("raw"),
            "ts": memory.get("ts"),
            "components": memory.get("components") or {},
            "last_history_ts": memory.get("last_history_ts"),
        }
        if sink is not None:
            sink("state", state_row)
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
            history_payload = {"row": row, "cutoff": cutoff}
            if sink is not None:
                sink("history", history_payload)
            else:
                enqueue_history(history_payload, _configured_timeout_ms(cfg))
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
        "mcap_floor_usd": floors.get("mcap_floor_usd"),
        "liq_floor_usdt": floors.get("liq_floor_usdt"),
    }


def _record_shadow(row: dict) -> None:
    """Bot-side obs row. ``mirror_record`` enqueues with put_nowait and does not wait on Mongo."""
    try:
        from services.observe_streams import mirror_record

        mirror_record(SHADOW_COLLECTION, row)
    except Exception:
        note_persist_failure("shadow-obs")


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
    fresh. The fixed decision fetches ``sma_days + 10`` bars. The score variant
    fetches on its own; a failed extra fetch leaves the score check unset.
    Mapping, logging and the observation write swallow their own errors.
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
    sma_log = None
    sma_off = False
    if isinstance(mapped, dict):
        if mapped.get("sma_condition_off"):
            window = mapped.get("sma_window")
            if window is not None:
                score_sma = int(window)
            sma_log = "off"
            sma_off = True
        elif mapped.get("sma_days") is not None:
            score_sma = int(mapped["sma_days"])
            sma_log = mapped["sma_days"]
    daily_fixed = rs.closed_bars(fetch_daily(fixed_sma + 10), now, rs.DAY)
    fixed = rs.evaluate_trend_filter(trend_cfg, daily_fixed, hourly)
    score_check = None
    if isinstance(mapped, dict):
        scored_cfg = dict(trend_cfg)
        scored_cfg.pop("sma_condition_off", None)
        use_score = False
        if sma_off:
            if mapped.get("sma_window") is not None:
                scored_cfg["sma_days"] = mapped["sma_window"]
            use_score = True
        elif mapped.get("sma_days") is not None:
            scored_cfg["sma_days"] = mapped["sma_days"]
            use_score = True
        if mapped.get("max_extension_pct") is not None:
            scored_cfg["max_extension_pct"] = mapped["max_extension_pct"]
            use_score = True
        if use_score:
            try:
                raw_bars = fetch_daily(max(fixed_sma, score_sma) + 10)
            except Exception:
                log.warning("oracle score daily fetch failed", exc_info=True)
                raw_bars = None
            if raw_bars:
                daily_score = rs.closed_bars(raw_bars, now, rs.DAY)
                score_check = rs.evaluate_trend_filter(
                    scored_cfg, daily_score, hourly, sma_condition_off=sma_off
                )
                if score_check.details.get("missing_data"):
                    score_check = None
    would_fixed = fixed.block_code is None
    would_score = None if score_check is None else score_check.block_code is None
    if mode is None:
        mode = MODE_OFF
    prefix = "oracle_score_shadow" if mode != MODE_ENFORCE else "oracle_score_enforce"
    line = format_shadow_line(
        tenant=str(tenant_id),
        symbol=str(symbol),
        s=None if not isinstance(mapped, dict) else mapped.get("s"),
        raw=raw,
        age_s=age,
        sma_days_fixed=fixed_sma,
        sma_days_score=sma_log,
        max_ext_fixed=trend_cfg.get("max_extension_pct"),
        max_ext_score=None if not isinstance(mapped, dict) else mapped.get("max_extension_pct"),
        would_pass_fixed=would_fixed,
        would_pass_score=would_score,
        mcap_fixed=floors.get("mcap_floor_usd"),
        mcap_score=None if not isinstance(mapped, dict) else mapped.get("mcap_floor_usd"),
        liq_fixed=floors.get("liq_floor_usdt"),
        liq_score=None if not isinstance(mapped, dict) else mapped.get("liq_floor_usdt"),
        size_round_up=None if not isinstance(mapped, dict) else mapped.get("size_round_up_frac"),
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
        "sma_days_score": sma_log,
        "max_ext_fixed": trend_cfg.get("max_extension_pct"),
        "max_ext_score": None if not isinstance(mapped, dict) else mapped.get("max_extension_pct"),
        "would_pass_fixed": would_fixed,
        "would_pass_score": would_score,
        "mcap_floor_fixed": floors.get("mcap_floor_usd"),
        "mcap_floor_score": None if not isinstance(mapped, dict) else mapped.get("mcap_floor_usd"),
        "liq_floor_fixed": floors.get("liq_floor_usdt"),
        "liq_floor_score": None if not isinstance(mapped, dict) else mapped.get("liq_floor_usdt"),
        "size_round_up": None if not isinstance(mapped, dict) else mapped.get("size_round_up_frac"),
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
