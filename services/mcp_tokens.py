from __future__ import annotations

import hashlib
import hmac

from services.mcp_authz import Actor

OWNER_CAPS = ("read", "trade", "lock", "config_read", "kill")


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


def bootstrap_from_env(*, owner_token: str, extras: list[dict] | None = None) -> dict[str, Actor]:
    out: dict[str, Actor] = {}
    if owner_token:
        out[hash_token(owner_token)] = Actor(
            "owner", "owner", ("*",), OWNER_CAPS,
        )
    for row in extras or []:
        tok = str(row.get("token") or "")
        if not tok:
            continue
        caps = tuple(row.get("caps") or ("read",))
        tenants = tuple(row.get("tenants") or ())
        out[hash_token(tok)] = Actor(
            str(row.get("actor_id") or "actor"),
            str(row.get("role") or "observer"),
            tenants,
            caps,
        )
    return out
