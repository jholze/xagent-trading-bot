from __future__ import annotations

import hashlib
import hmac

from services.mcp_authz import Actor

OWNER_CAPS = ("read", "trade", "lock", "config_read", "kill")
ROLE_CAPS = {
    "owner": OWNER_CAPS,
    "operator": ("read", "trade", "lock"),
    "observer": ("read",),
}


def hash_token(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def actor_from_bearer(raw: str, actors_by_hash: dict[str, Actor]) -> Actor | None:
    if not raw:
        return None
    h = hash_token(raw)
    for stored, actor in (actors_by_hash or {}).items():
        if hmac.compare_digest(stored, h):
            return actor
    return None


def tokens_match(got: str, expected: str) -> bool:
    if not got or not expected:
        return False
    return hmac.compare_digest(hash_token(got), hash_token(expected))


def actor_from_extra(row: dict | None) -> Actor | None:
    """Operator/observer only. Owner is MCP_OWNER_TOKEN. Fail-closed skip."""
    if not isinstance(row, dict):
        return None
    tok = str(row.get("token") or "").strip()
    if not tok:
        return None
    role = str(row.get("role") or "observer").strip().lower()
    if role == "owner" or role not in ROLE_CAPS:
        return None
    tenants = tuple(str(t).strip() for t in (row.get("tenants") or ()) if str(t).strip())
    if len(tenants) != 1 or "*" in tenants:
        return None
    allowed = ROLE_CAPS[role]
    requested = row.get("caps")
    if requested is None:
        caps = allowed
    else:
        caps = tuple(c for c in requested if c in allowed)
    if not caps:
        return None
    status = str(row.get("status") or "active").strip().lower()
    if status != "active":
        return None
    actor_id = str(row.get("actor_id") or "actor").strip() or "actor"
    return Actor(actor_id, role, tenants, caps, status="active")


def bootstrap_from_env(*, owner_token: str, extras: list[dict] | None = None) -> dict[str, Actor]:
    out: dict[str, Actor] = {}
    if owner_token:
        out[hash_token(owner_token)] = Actor(
            "owner", "owner", ("*",), OWNER_CAPS,
        )
    for row in extras or []:
        tok = str(row.get("token") or "").strip()
        actor = actor_from_extra(row)
        if not tok or actor is None:
            continue
        out[hash_token(tok)] = actor
    return out
