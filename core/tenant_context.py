"""Tenant context for multi-tenant ledger isolation (Phase 0)."""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

DEFAULT_TENANT = "default"


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    scope: str
    owner_chat_id: str = ""
    bot_token: str = ""
    redis_prefix: str = ""
    headless: bool = False

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("tenant_id is required")
        if not self.scope:
            raise ValueError("scope is required")
        if not self.redis_prefix:
            object.__setattr__(self, "redis_prefix", f"aria:{self.tenant_id}:")


_ctx: ContextVar[TenantContext | None] = ContextVar("tenant_ctx", default=None)


def current_tenant_context() -> TenantContext | None:
    return _ctx.get()


def resolve_tenant_id(tenant_id: str | None = None) -> str:
    if tenant_id:
        return tenant_id
    ctx = _ctx.get()
    if ctx is not None:
        return ctx.tenant_id
    return DEFAULT_TENANT


def resolve_tenant_scope(scope: str | None = None) -> str:
    if scope:
        return scope
    ctx = _ctx.get()
    if ctx is not None:
        return ctx.scope
    from data_manager import resolve_ledger_scope

    return resolve_ledger_scope()


def require_tenant() -> TenantContext:
    ctx = _ctx.get()
    if ctx is None:
        raise RuntimeError("No tenant context — bug in call chain")
    return ctx


def tenant_scope_tuple(
    *,
    tenant_id: str | None = None,
    scope: str | None = None,
) -> tuple[str, str]:
    return resolve_tenant_id(tenant_id), resolve_tenant_scope(scope)


@contextmanager
def tenant_context(
    tenant_id: str,
    *,
    scope: str | None = None,
    owner_chat_id: str = "",
    bot_token: str = "",
    headless: bool = False,
) -> Iterator[TenantContext]:
    from data_manager import resolve_ledger_scope

    resolved_scope = scope or resolve_ledger_scope()
    ctx = TenantContext(
        tenant_id=tenant_id,
        scope=resolved_scope,
        owner_chat_id=owner_chat_id,
        bot_token=bot_token,
        headless=headless,
    )
    token = _ctx.set(ctx)
    try:
        yield ctx
    finally:
        _ctx.reset(token)


def tenant_snapshot() -> tuple[str, str, str]:
    """Capture (tenant_id, scope, owner_chat_id) from active context or defaults."""
    ctx = _ctx.get()
    if ctx is not None:
        return ctx.tenant_id, ctx.scope, ctx.owner_chat_id
    from data_manager import resolve_ledger_scope

    return DEFAULT_TENANT, resolve_ledger_scope(), ""


def multi_tenant_enabled() -> bool:
    env = os.environ.get("MULTI_TENANT_ENABLED", "").strip().lower()
    if env in {"1", "true", "yes"}:
        return True
    if env in {"0", "false", "no"}:
        return False
    try:
        from core.config import get_bot_config

        return bool(get_bot_config().multi_tenant_config.get("enabled", False))
    except Exception:
        return False