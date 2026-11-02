"""Cycle-silence watchdog (spec_cycle_silence_fix AC4).

Per tenant, once per meta cycle (aria_bot._run_one_tenant_cycle), two independent counters:
  no_auto_order  cycles in a row without a NEW ledger order with source == "auto" (ANY status, incl. rejected)
  no_fill        cycles in a row without a NEW ledger order with status filled (ANY source)
State (watermark + counter per tenant and kind) lives in Redis so a restart does not hide a silence.
Alarm on the cycle the counter reaches N, then every repeat_every_cycles. Sink: the Learning Loop inbox
(Redis list <prefix>learning_loop:inbox, JSON event) plus one ERROR log line. Read-only on the ledger; no orders,
no config writes. Enabled unless observability.cycle_watchdog.enabled is false. N comes from config only (observability.cycle_watchdog.*_silent_cycles); missing/invalid N
raises a one-time 'watchdog_unconfigured' alarm per tenant and kind instead of guessing a value.
"""
from __future__ import annotations

import json
from datetime import datetime

from logger import log

KINDS = (("no_auto_order", "auto_silent_cycles"), ("no_fill", "fill_silent_cycles"))


def watchdog_config(config_raw: dict | None = None) -> dict:
    if config_raw is None:
        from core.config import get_bot_config

        config_raw = get_bot_config().raw
    return dict(((config_raw or {}).get("observability") or {}).get("cycle_watchdog") or {})


def _arch(config_raw):
    if config_raw is None:
        from core.config import get_bot_config

        config_raw = get_bot_config().raw
    return (config_raw or {}).get("architecture") or {}


def _client(config_raw=None):
    from bus.redis_client import get_redis, resolve_redis_url

    arch = _arch(config_raw)
    return get_redis(resolve_redis_url(arch.get("redis_url")), key_prefix=str(arch.get("key_prefix", "aria:")))


def _prefix(config_raw=None) -> str:
    return str(_arch(config_raw).get("key_prefix", "aria:") or "aria:")


def _key(prefix: str, tid: str, kind: str, field: str) -> str:
    return f"{prefix}watchdog:cycle_silence:{tid}:{kind}:{field}"  # CS-F4-KEY (tenant-scoped)


def _valid_n(v) -> int | None:
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        return None
    return v


def _created(o: dict) -> str:
    return str((o.get("timestamps") or {}).get("created") or o.get("created") or "")


def _filled_at(o: dict) -> str:
    ts = o.get("timestamps") or {}
    return str(ts.get("filled") or ts.get("created") or o.get("created") or "")


def ledger_marks(tenant_id: str) -> dict[str, str]:
    """Newest matching row per kind for THIS tenant's ledger (watermark string; '' when none)."""
    from core.models import is_executed_status
    from data_manager import load_orders, resolve_ledger_scope

    doc = load_orders(resolve_ledger_scope(), tenant_id=tenant_id) or {}
    rows = [o for o in (doc.get("orders") or []) if str(o.get("tenant_id") or tenant_id) == tenant_id]
    auto = [(_created(o), str(o.get("id") or "")) for o in rows if str(o.get("source") or "") == "auto"]
    fills = [(_filled_at(o), str(o.get("id") or "")) for o in rows
             if is_executed_status(o.get("status"))]  # CS-F4-FILL (status filled, any source)
    return {
        "no_auto_order": "|".join(max(auto)) if auto else "",
        "no_fill": "|".join(max(fills)) if fills else "",
    }


def emit_alarm(client, prefix: str, event: dict) -> None:
    """Learning Loop sink (concrete consumer UNCLEAR, spec section 9): JSON event on <prefix>learning_loop:inbox."""
    payload = json.dumps(event, sort_keys=True, default=str)
    try:
        client.lpush(f"{prefix}learning_loop:inbox", payload)
    finally:
        log(f"cycle_watchdog_alarm {payload}", "ERROR")


def note_tenant_cycle(tenant_id: str | None = None, *, config_raw: dict | None = None) -> list[dict]:
    """Call once per completed meta cycle for the tenant whose context is active. Returns the alarms emitted."""
    from core.tenant_context import DEFAULT_TENANT, resolve_tenant_id

    tid = (resolve_tenant_id(tenant_id) or DEFAULT_TENANT).strip() or DEFAULT_TENANT
    if config_raw is None:
        # CS-R1-TENANT: N comes from THIS tenant's effective config
        # (config.json → profile → tenant body), not from whichever config
        # happens to be active. data_manager.get_config is that layering.
        # get_bot_config(tid) is the same call in production; the unit harness
        # replaces get_bot_config with a tenant-blind copy of the default
        # config, so the watchdog does not go through that stub.
        from data_manager import get_config

        config_raw = get_config(tenant_id=tid)
    cfg = watchdog_config(config_raw)
    if not cfg.get("enabled", True):  # CS-F4-ENABLED (mandatory by default; explicit false switches it off)
        return []
    client = _client(config_raw)
    if not client:
        log(f"cycle_watchdog: Redis unavailable, tenant={tid} not counted", "WARNING")
        return []
    prefix = _prefix(config_raw)
    marks = ledger_marks(tid)
    out: list[dict] = []
    now = datetime.now().isoformat()
    for kind, n_key in KINDS:
        n = _valid_n(cfg.get(n_key))
        if n is None:
            flag = _key(prefix, tid, kind, "unconfigured_alarmed")
            if not client.get(flag):
                client.set(flag, "1")
                ev = {"type": "watchdog_unconfigured", "kind": kind, "tenant_id": tid, "key": n_key, "at": now}
                emit_alarm(client, prefix, ev)
                out.append(ev)
            continue
        repeat = _valid_n(cfg.get("repeat_every_cycles")) or n
        wm_key, cnt_key = _key(prefix, tid, kind, "watermark"), _key(prefix, tid, kind, "count")
        prev = client.get(wm_key)
        mark = marks[kind]
        if prev is None or str(prev) != mark:  # first observation or a new row: (re)arm
            client.set(wm_key, mark)
            client.set(cnt_key, "0")  # CS-F4-RESET
            continue
        cnt = int(client.incr(cnt_key))
        if cnt == n or (cnt > n and (cnt - n) % repeat == 0):
            ev = {"type": "cycle_silence", "kind": kind, "tenant_id": tid, "cycles": cnt, "n": n,
                  "watermark": mark, "at": now}
            emit_alarm(client, prefix, ev)
            out.append(ev)
    return out
