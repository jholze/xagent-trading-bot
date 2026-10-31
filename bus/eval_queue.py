"""Redis-backed priority queue for per-coin evaluations (replaces full-cycle scan)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from bus.redis_client import get_redis, resolve_redis_url
from logger import log

# Lower number = higher priority (ZPOPMIN).
PRIORITY_WEBHOOK = 0
PRIORITY_ENTRY_15M = 15
PRIORITY_POSITION_DELTA = 20
PRIORITY_POSITION_HEARTBEAT = 30
PRIORITY_SOCIAL = 25  # ahead of heartbeat 30; CMC/Lunar must not wait behind 365 pos jobs. Kill: 40.
PRIORITY_STALE = 50
PRIORITY_DISCOVERY = 60

_SCORE_SLOT = 10**16  # priority in high bits; slot >> ms timestamp (~1.7e12 in 2026)


@dataclass(frozen=True)
class EvalJob:
    symbol: str
    timeframe: str
    reason: str
    priority: int
    enqueued_at: float
    score: float = 0.0
    tenant_id: str = ""

    @property
    def member(self) -> str:
        return eval_member_key(self.tenant_id, self.symbol, self.timeframe)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "reason": self.reason,
            "priority": self.priority,
            "enqueued_at": self.enqueued_at,
            "score": self.score,
            "tenant_id": self.tenant_id,
        }


def eval_queue_config(config_raw: dict | None = None) -> dict:
    if config_raw is None:
        from core.config import get_bot_config

        config_raw = get_bot_config().raw
    defaults = {
        "eval_queue_enabled": False,
        "eval_worker_poll_sec": 2.0,
        "eval_batch_size": 3,
        "eval_debounce_sec": 45,
        "eval_position_heartbeat_sec": 300,
        "eval_stale_sec": 7200,
        "eval_meta_interval_sec": 300,
        "eval_queue_max_len": 500,
    }
    arch = (config_raw or {}).get("architecture") or {}
    return {**defaults, **{k: v for k, v in arch.items() if k in defaults or k.startswith("eval_")}}


def eval_queue_enabled(config_raw: dict | None = None) -> bool:
    return bool(eval_queue_config(config_raw).get("eval_queue_enabled", False))


def _arch(config_raw: dict | None) -> dict:
    if config_raw is None:
        from core.config import get_bot_config

        config_raw = get_bot_config().raw
    return (config_raw or {}).get("architecture") or {}


def _keys(prefix: str) -> tuple[str, str, str]:
    p = prefix or "aria:"
    return (
        f"{p}eval:queue",
        f"{p}eval:meta",
        f"{p}eval:processed",
    )


def _client(config_raw: dict | None = None):
    arch = _arch(config_raw)
    url = resolve_redis_url(arch.get("redis_url"))
    return get_redis(url, key_prefix=str(arch.get("key_prefix", "aria:")))


def _score(priority: int, *, now: float | None = None) -> float:
    ts = int((now or time.time()) * 1000)
    return float(priority * _SCORE_SLOT + ts)


def _resolve_enqueue_tenant(tenant_id: str | None) -> str:
    from core.tenant_context import DEFAULT_TENANT, resolve_tenant_id

    return resolve_tenant_id(tenant_id) or DEFAULT_TENANT


def eval_member_key(tenant_id: str, symbol: str, timeframe: str) -> str:
    """Redis ZSET member — tenant prefix when multi-tenant is on."""
    from core.tenant_context import DEFAULT_TENANT, multi_tenant_enabled

    sym = (symbol or "").strip()
    tf = (timeframe or "4h").strip() or "4h"
    tid = (tenant_id or DEFAULT_TENANT).strip() or DEFAULT_TENANT
    if multi_tenant_enabled() and tid:
        return f"{tid}|{sym}|{tf}"
    return f"{sym}|{tf}"


def _parse_member(member: str) -> tuple[str, str, str]:
    """Return (tenant_id, symbol, timeframe). Legacy members map to default."""
    from core.tenant_context import DEFAULT_TENANT

    parts = [p.strip() for p in str(member).split("|") if p.strip()]
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2] or "4h"
    if len(parts) == 2:
        return DEFAULT_TENANT, parts[0], parts[1] or "4h"
    return DEFAULT_TENANT, parts[0] if parts else "", "4h"


def enqueue_eval(
    symbol: str,
    timeframe: str,
    *,
    reason: str,
    priority: int,
    config_raw: dict | None = None,
    force: bool = False,
    tenant_id: str | None = None,
) -> bool:
    """Enqueue a coin for evaluation. Returns True if accepted."""
    cfg = eval_queue_config(config_raw)
    if not cfg.get("eval_queue_enabled", False):
        return False

    client = _client(config_raw)
    if not client:
        log("eval_queue: Redis unavailable — cannot enqueue", "WARNING")
        return False

    arch = _arch(config_raw)
    prefix = str(arch.get("key_prefix", "aria:"))
    queue_key, meta_key, processed_key = _keys(prefix)
    tid = _resolve_enqueue_tenant(tenant_id)
    member = eval_member_key(tid, symbol, timeframe)
    now = time.time()
    debounce = float(cfg.get("eval_debounce_sec", 45))

    try:
        prev_at = 0.0
        raw_meta = client.hget(meta_key, member)
        if raw_meta and not force:
            prev = json.loads(raw_meta)
            prev_pri = int(prev.get("priority", 99))
            prev_at = float(prev.get("enqueued_at", 0))
            if priority >= prev_pri and (now - prev_at) < debounce:
                return False

        new_score = _score(priority, now=now)
        existing = client.zscore(queue_key, member)
        if existing is not None and not force:
            existing_pri = int(existing // _SCORE_SLOT)
            if priority >= existing_pri and (now - prev_at) < debounce:
                return False
        meta_reason = reason
        if existing is not None and raw_meta and _is_entry_reason(reason):
            # CS-F2: never replace a PENDING heartbeat with an entry job (the worker would return HOLD for the
            # open lot before analyze). Coalesce: keep the heartbeat reason, take the better priority.
            try:
                prev_meta = json.loads(raw_meta)
            except Exception:
                prev_meta = {}
            if str(prev_meta.get("reason") or "") == HEARTBEAT_REASON:  # CS-F2-KEEP
                meta_reason = HEARTBEAT_REASON
                existing_pri = int(existing // _SCORE_SLOT)
                if existing_pri <= priority:
                    new_score = float(existing)

        pipe = client.pipeline()
        pipe.zadd(queue_key, {member: new_score})
        max_len = int(cfg.get("eval_queue_max_len", 500))
        pipe.hset(
            meta_key,
            member,
            json.dumps(
                {
                    "reason": meta_reason,
                    "priority": priority,
                    "enqueued_at": now,
                    "tenant_id": tid,
                },
                default=str,
            ),
        )
        pipe.execute()
        if max_len > 0:
            _trim_queue(client, queue_key, meta_key, max_len)  # CS-F3
        return True
    except Exception as e:
        log(f"eval_queue enqueue failed {member}: {e}", "WARNING")
        return False


HEARTBEAT_REASON = "position_heartbeat"


def _is_entry_reason(reason: str) -> bool:
    r = str(reason or "")
    return r.startswith("entry_15m") or r == "15m_watch"


def _is_protected_member(client, meta_key: str, member) -> bool:
    """CS-F3: a pending position heartbeat is never trimmed (it is the only path to analyze for a held lot)."""
    try:
        raw = client.hget(meta_key, member)
        return bool(raw) and str(json.loads(raw).get("reason") or "") == HEARTBEAT_REASON  # CS-F3-PROTECT
    except Exception:
        return False


def _trim_queue(client, queue_key: str, meta_key: str, max_len: int) -> int:
    """CS-F3: trim the worst-ranked NON-heartbeat members until len <= max_len (or only heartbeats are left)."""
    excess = int(client.zcard(queue_key)) - int(max_len)
    if excess <= 0:
        return 0
    removed = 0
    for member in client.zrevrange(queue_key, 0, -1):
        if removed >= excess:
            break
        if _is_protected_member(client, meta_key, member):
            continue
        client.zrem(queue_key, member)
        removed += 1
    if removed < excess:
        log(f"eval_queue over cap by {excess - removed} protected heartbeat(s) (max_len={max_len})", "WARNING")
    return removed


def pop_eval_batch(
    count: int = 1,
    *,
    config_raw: dict | None = None,
) -> list[EvalJob]:
    client = _client(config_raw)
    if not client:
        return []

    arch = _arch(config_raw)
    prefix = str(arch.get("key_prefix", "aria:"))
    queue_key, meta_key, _ = _keys(prefix)

    try:
        items = client.zpopmin(queue_key, count=max(1, int(count)))
    except Exception as e:
        log(f"eval_queue pop failed: {e}", "WARNING")
        return []

    jobs: list[EvalJob] = []
    now = time.time()
    for member, score in items or []:
        tenant_id, symbol, timeframe = _parse_member(str(member))
        reason = "unknown"
        priority = int(float(score) // _SCORE_SLOT)
        enqueued_at = now
        try:
            raw = client.hget(meta_key, member)
            if raw:
                meta = json.loads(raw)
                reason = str(meta.get("reason") or reason)
                priority = int(meta.get("priority", priority))
                enqueued_at = float(meta.get("enqueued_at", enqueued_at))
                tenant_id = str(meta.get("tenant_id") or tenant_id)
        except Exception:
            pass
        jobs.append(
            EvalJob(
                symbol=symbol,
                timeframe=timeframe,
                reason=reason,
                priority=priority,
                enqueued_at=enqueued_at,
                score=float(score),
                tenant_id=tenant_id,
            )
        )
    return jobs


def mark_eval_processed(
    symbol: str,
    timeframe: str,
    *,
    config_raw: dict | None = None,
    tenant_id: str | None = None,
    processed_at: float | None = None,
) -> bool:
    """Stamp processed only after a successful evaluation, not at dequeue."""
    client = _client(config_raw)
    if not client:
        return False
    arch = _arch(config_raw)
    _, _, processed_key = _keys(str(arch.get("key_prefix", "aria:")))
    member = eval_member_key(_resolve_enqueue_tenant(tenant_id), symbol, timeframe)
    try:
        client.hset(
            processed_key,
            member,
            str(processed_at if processed_at is not None else time.time()),
        )
        return True
    except Exception as e:
        log(f"eval_queue mark_processed failed {member}: {e}", "WARNING")
        return False


def queue_depth(config_raw: dict | None = None) -> int:
    client = _client(config_raw)
    if not client:
        return 0
    arch = _arch(config_raw)
    queue_key, _, _ = _keys(str(arch.get("key_prefix", "aria:")))
    try:
        return int(client.zcard(queue_key))
    except Exception:
        return 0


def peek_eval_queue(limit: int = 10, config_raw: dict | None = None) -> list[dict]:
    client = _client(config_raw)
    if not client:
        return []
    arch = _arch(config_raw)
    queue_key, meta_key, _ = _keys(str(arch.get("key_prefix", "aria:")))
    try:
        items = client.zrange(queue_key, 0, max(0, limit - 1), withscores=True)
    except Exception:
        return []
    out = []
    for member, score in items or []:
        tenant_id, symbol, timeframe = _parse_member(str(member))
        reason = ""
        try:
            raw = client.hget(meta_key, member)
            if raw:
                meta = json.loads(raw)
                reason = meta.get("reason", "")
                tenant_id = str(meta.get("tenant_id") or tenant_id)
        except Exception:
            pass
        out.append(
            {
                "tenant_id": tenant_id,
                "symbol": symbol,
                "timeframe": timeframe,
                "reason": reason,
                "priority": int(float(score) // _SCORE_SLOT),
                "score": float(score),
            }
        )
    return out


def last_processed_at(
    symbol: str,
    timeframe: str,
    config_raw: dict | None = None,
    *,
    tenant_id: str | None = None,
) -> float | None:
    client = _client(config_raw)
    if not client:
        return None
    arch = _arch(config_raw)
    _, _, processed_key = _keys(str(arch.get("key_prefix", "aria:")))
    member = eval_member_key(_resolve_enqueue_tenant(tenant_id), symbol, timeframe)
    try:
        raw = client.hget(processed_key, member)
        return float(raw) if raw else None
    except Exception:
        return None


def reset_eval_queue_for_tests(config_raw: dict | None = None) -> None:
    """Clear eval queue keys (unit tests with mocked Redis)."""
    client = _client(config_raw)
    if not client:
        return
    arch = _arch(config_raw)
    prefix = str(arch.get("key_prefix", "aria:"))
    queue_key, meta_key, processed_key = _keys(prefix)
    try:
        client.delete(queue_key, meta_key, processed_key)
    except Exception:
        pass