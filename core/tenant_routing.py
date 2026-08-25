"""Resolve incoming Telegram chats and trading cycles to tenant contexts."""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from core.tenant_context import DEFAULT_TENANT, multi_tenant_enabled, tenant_context


@dataclass(frozen=True)
class IncomingTenantRoute:
    tenant_id: str
    owner_chat_id: str
    scope: str = "paper"
    rejected: bool = False
    reject_message: str = ""


def extract_chat_id_from_update(update: dict | None) -> str:
    if not update:
        return ""
    if "message" in update:
        raw = update["message"].get("chat", {}).get("id")
        return str(raw).strip() if raw is not None else ""
    if "callback_query" in update:
        msg = update["callback_query"].get("message") or {}
        raw = msg.get("chat", {}).get("id")
        return str(raw).strip() if raw is not None else ""
    return ""


def _runtime_ledger_scope() -> str:
    from data_manager import resolve_ledger_scope

    return resolve_ledger_scope()


def _scope_from_tenant_doc(doc: dict | None) -> str:
    defaults = (doc or {}).get("defaults") or {}
    return str(defaults.get("ledger_scope") or _runtime_ledger_scope())


def _effective_ledger_scope(doc: dict | None) -> str:
    """Resolve ledger scope for a tenant route (demo mode overrides tenant doc)."""
    from data_manager import is_demo_mode

    if is_demo_mode():
        return _runtime_ledger_scope()
    return _scope_from_tenant_doc(doc)


def _operator_chat_id() -> str:
    return (os.getenv("TELEGRAM_CHAT_ID") or "").strip()


def resolve_incoming_tenant(
    *,
    chat_id: str | int | None,
    explicit_tenant_id: str | None = None,
    test: bool = False,
) -> IncomingTenantRoute:
    """Map a Telegram update to a tenant (shared-bot multi-tenant)."""
    from storage.tenant_registry import find_tenant_by_owner_chat_id, get_tenant

    cid = str(chat_id or "").strip()
    op_chat = _operator_chat_id()

    if explicit_tenant_id and explicit_tenant_id != DEFAULT_TENANT:
        doc = get_tenant(explicit_tenant_id, test=test) or {}
        tg = doc.get("telegram") or {}
        owner = str(tg.get("owner_chat_id") or cid or op_chat)
        return IncomingTenantRoute(
            tenant_id=explicit_tenant_id,
            owner_chat_id=owner,
            scope=_effective_ledger_scope(doc),
        )

    if not multi_tenant_enabled():
        return IncomingTenantRoute(
            tenant_id=DEFAULT_TENANT,
            owner_chat_id=op_chat or cid,
            scope=_runtime_ledger_scope(),
        )

    if cid and op_chat and cid == op_chat:
        return IncomingTenantRoute(
            tenant_id=DEFAULT_TENANT,
            owner_chat_id=op_chat,
            scope=_effective_ledger_scope(None),
        )

    if cid:
        doc = find_tenant_by_owner_chat_id(cid, test=test)
        if doc and doc.get("tenant_id"):
            tid = str(doc["tenant_id"]).strip()
            if tid and tid != DEFAULT_TENANT:
                return IncomingTenantRoute(
                    tenant_id=tid,
                    owner_chat_id=cid,
                    scope=_effective_ledger_scope(doc),
                )

    return IncomingTenantRoute(
        tenant_id=DEFAULT_TENANT,
        owner_chat_id=cid,
        rejected=True,
        reject_message=(
            "❌ Dieser Chat ist keinem Tenant zugeordnet.\n\n"
            "Bitte den <b>Einladungs-Link</b> vom Operator öffnen "
            "(z.B. <code>t.me/…?start=dein_name</code>)."
        ),
    )


def iter_price_cycle_tenants(*, test: bool = False) -> list[str]:
    """Tenant ids that receive an isolated price/trading cycle."""
    if not multi_tenant_enabled():
        return [DEFAULT_TENANT]

    from storage.tenant_registry import list_active_tenants

    ids = [DEFAULT_TENANT]
    for doc in list_active_tenants(test=test):
        tid = str(doc.get("tenant_id") or "").strip()
        if not tid or tid == DEFAULT_TENANT:
            continue
        if doc.get("status", "active") != "active":
            continue
        tg = doc.get("telegram") or {}
        owner = str(tg.get("owner_chat_id") or "").strip()
        if not owner and not tg.get("headless"):
            continue
        if tid not in ids:
            ids.append(tid)
    return ids


@contextmanager
def tenant_cycle_context(tenant_id: str, *, test: bool = False) -> Iterator[None]:
    from storage.tenant_registry import get_tenant

    doc = get_tenant(tenant_id, test=test) or {}
    tg = doc.get("telegram") or {}
    headless = bool(tg.get("headless"))
    owner = str(tg.get("owner_chat_id") or "").strip()
    # Headless paper tenants share the operator inbox (tagged). Bound tenants
    # must never fall back — that leaked Henry fills into the operator chat.
    if not owner and (headless or tenant_id == DEFAULT_TENANT):
        owner = _operator_chat_id()
    scope = _effective_ledger_scope(doc if tenant_id != DEFAULT_TENANT else None)
    with tenant_context(tenant_id, scope=scope, owner_chat_id=owner, headless=headless):
        from strategies.positions import activate_tenant_positions

        activate_tenant_positions(scope=scope, tenant_id=tenant_id)
        yield