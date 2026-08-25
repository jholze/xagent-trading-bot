from __future__ import annotations

import hashlib
import time

from services.mcp_authz import Actor, authorize, check_write_rate, mcp_write_rate_per_min
from services.mcp_explain import list_orders_public, memory_pack, why_pack

IDEMPOTENCY_BUCKET_SEC = 30


def write_idempotency_key(
    *,
    actor_id: str,
    action: str,
    tenant_id: str,
    symbol: str | None,
    usdt=None,
    pct=None,
    amount=None,
    now: float | None = None,
    bucket_sec: int = IDEMPOTENCY_BUCKET_SEC,
) -> str:
    ts = float(now if now is not None else time.time())
    bucket = int(ts // max(1, int(bucket_sec)))
    raw = "|".join(
        [
            str(actor_id or ""),
            str(action or ""),
            str(tenant_id or ""),
            str(symbol or "").upper(),
            "" if usdt is None else str(usdt),
            "" if pct is None else str(pct),
            "" if amount is None else str(amount),
            str(bucket),
        ]
    )
    return "mcp:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def tool_whoami(actor: Actor | None) -> dict:
    if actor is None:
        return {"actor_id": "", "role": "", "tenants": [], "caps": []}
    return {
        "actor_id": actor.actor_id,
        "role": actor.role,
        "tenants": list(actor.tenants),
        "caps": list(actor.caps),
    }


def _effective_tenant(actor: Actor | None, requested: str | None) -> str:
    req = str(requested or "").strip() or "default"
    if actor is None:
        return req
    if "*" in actor.tenants:
        return req
    if actor.tenants:
        return str(actor.tenants[0])
    return req


def _call_snapshot(snapshot_fn, *, tenant_id: str, symbol: str | None) -> dict:
    fn = snapshot_fn
    if fn is None:
        from services.desk.snapshot import build_snapshot

        fn = build_snapshot
    try:
        out = fn(tenant_id=tenant_id, symbol=symbol)
    except Exception:
        return {"ok": False, "error": "snapshot_failed"}
    if not isinstance(out, dict):
        return {"ok": False, "error": "snapshot_failed"}
    return out


def tool_snapshot(actor, tenant=None, symbol=None, snapshot_fn=None):
    tenant_id = _effective_tenant(actor, tenant)
    ok, err = authorize(actor, "read", tenant_id)
    if not ok:
        return {"ok": False, "error": err}
    return _call_snapshot(snapshot_fn, tenant_id=tenant_id, symbol=symbol)


def tool_lots(actor, tenant=None, symbol=None, snapshot_fn=None):
    out = tool_snapshot(actor, tenant=tenant, symbol=symbol, snapshot_fn=snapshot_fn)
    if not out.get("ok"):
        return {"ok": False, "error": out.get("error") or "snapshot_failed"}
    lots = out.get("lots")
    if not isinstance(lots, list):
        lots = []
    return {"ok": True, "tenant_id": out.get("tenant_id"), "lots": lots}


def _call_execute(execute_fn, **kwargs) -> dict:
    fn = execute_fn
    if fn is None:
        from services.mcp_client import execute

        fn = execute
    try:
        out = fn(**kwargs)
    except Exception:
        return {"ok": False, "error": "bot_unreachable"}
    if not isinstance(out, dict):
        return {"ok": False, "error": "bot_unreachable"}
    return out


def _write(
    actor,
    cap: str,
    action: str,
    tenant,
    execute_fn,
    payload: dict,
    *,
    writes_enabled: bool = True,
    enabled: bool = True,
    rate_per_min: int | None = None,
    now: float | None = None,
) -> dict:
    tenant_id = _effective_tenant(actor, tenant)
    ok, err = authorize(
        actor, cap, tenant_id, enabled=enabled, writes_enabled=writes_enabled
    )
    if not ok:
        return {"ok": False, "error": err}
    actor_id = actor.actor_id if actor else ""
    per = rate_per_min if rate_per_min is not None else mcp_write_rate_per_min()
    ok, err = check_write_rate(actor_id, per_min=per, now=now)
    if not ok:
        return {"ok": False, "error": err}
    body = {
        "action": action,
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "idempotency_key": write_idempotency_key(
            actor_id=actor_id,
            action=action,
            tenant_id=tenant_id,
            symbol=payload.get("symbol"),
            usdt=payload.get("usdt"),
            pct=payload.get("pct"),
            amount=payload.get("amount"),
            now=now,
        ),
    }
    for key, val in payload.items():
        if val is not None:
            body[key] = val
    return _call_execute(execute_fn, **body)


def tool_buy(
    actor,
    tenant=None,
    symbol=None,
    usdt=None,
    execute_fn=None,
    timeframe="1h",
    price=None,
    writes_enabled=True,
    enabled=True,
    rate_per_min=None,
    now=None,
):
    return _write(
        actor,
        "trade",
        "buy",
        tenant,
        execute_fn,
        {
            "symbol": symbol,
            "usdt": usdt,
            "timeframe": timeframe or "1h",
            "price": price,
        },
        writes_enabled=writes_enabled,
        enabled=enabled,
        rate_per_min=rate_per_min,
        now=now,
    )


def tool_sell(
    actor,
    tenant=None,
    symbol=None,
    pct=None,
    amount=None,
    execute_fn=None,
    timeframe="1h",
    price=None,
    writes_enabled=True,
    enabled=True,
):
    return _write(
        actor,
        "trade",
        "sell",
        tenant,
        execute_fn,
        {
            "symbol": symbol,
            "pct": pct,
            "amount": amount,
            "timeframe": timeframe or "1h",
            "price": price,
        },
        writes_enabled=writes_enabled,
        enabled=enabled,
    )


def tool_lock(
    actor,
    tenant=None,
    symbol=None,
    reason=None,
    execute_fn=None,
    timeframe="1h",
    writes_enabled=True,
    enabled=True,
):
    return _write(
        actor,
        "lock",
        "lock",
        tenant,
        execute_fn,
        {
            "symbol": symbol,
            "reason": reason,
            "timeframe": timeframe or "1h",
        },
        writes_enabled=writes_enabled,
        enabled=enabled,
    )


def tool_unlock(
    actor,
    tenant=None,
    symbol=None,
    execute_fn=None,
    timeframe="1h",
    writes_enabled=True,
    enabled=True,
):
    return _write(
        actor,
        "lock",
        "unlock",
        tenant,
        execute_fn,
        {
            "symbol": symbol,
            "timeframe": timeframe or "1h",
        },
        writes_enabled=writes_enabled,
        enabled=enabled,
    )


def tool_orders(
    actor,
    tenant=None,
    symbol=None,
    hours=None,
    limit=None,
    statuses=None,
    list_fn=None,
):
    tenant_id = _effective_tenant(actor, tenant)
    ok, err = authorize(actor, "read", tenant_id)
    if not ok:
        return {"ok": False, "error": err}
    return list_orders_public(
        tenant_id,
        symbol=symbol,
        hours=hours,
        limit=limit if limit is not None else 40,
        statuses=statuses,
        list_fn=list_fn,
        live=list_fn is None,
    )


def tool_memory(
    actor,
    tenant=None,
    symbol=None,
    query=None,
    memory_fn=None,
    store=None,
    facts_fn=None,
    rag_fn=None,
):
    tenant_id = _effective_tenant(actor, tenant)
    ok, err = authorize(actor, "read", tenant_id)
    if not ok:
        return {"ok": False, "error": err}
    if memory_fn is not None:
        try:
            out = memory_fn(tenant_id=tenant_id, symbol=symbol, query=query)
        except Exception:
            return {"ok": True, "tenant_id": tenant_id, "symbol": symbol, "errors": ["memory_failed"]}
        if not isinstance(out, dict):
            return {"ok": True, "tenant_id": tenant_id, "symbol": symbol, "errors": ["memory_failed"]}
        out.setdefault("tenant_id", tenant_id)
        return out
    return memory_pack(
        tenant_id,
        symbol,
        query=query,
        store=store,
        facts_fn=facts_fn,
        rag_fn=rag_fn,
        live=True,
    )


def tool_why(
    actor,
    tenant=None,
    symbol=None,
    query=None,
    why_fn=None,
    store=None,
    facts_fn=None,
    rag_fn=None,
    list_fn=None,
    snapshot_fn=None,
):
    tenant_id = _effective_tenant(actor, tenant)
    ok, err = authorize(actor, "read", tenant_id)
    if not ok:
        return {"ok": False, "error": err}
    if why_fn is not None:
        try:
            out = why_fn(tenant_id=tenant_id, symbol=symbol, query=query)
        except Exception:
            return {"ok": False, "error": "why_failed"}
        if not isinstance(out, dict):
            return {"ok": False, "error": "why_failed"}
        out.setdefault("tenant_id", tenant_id)
        return out
    return why_pack(
        tenant_id,
        symbol,
        query=query,
        store=store,
        facts_fn=facts_fn,
        rag_fn=rag_fn,
        list_fn=list_fn,
        snapshot_fn=snapshot_fn,
        live=True,
    )
