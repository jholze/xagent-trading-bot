from __future__ import annotations

from services.mcp_authz import Actor, authorize


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
