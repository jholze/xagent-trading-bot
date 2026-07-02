"""Compound ledger identifiers for tenant + scope partitioning."""

from __future__ import annotations

from core.tenant_context import DEFAULT_TENANT, resolve_tenant_id


def compound_ledger_id(tenant_id: str, scope: str) -> str:
    return f"{tenant_id}:{scope}"


def ledger_query(tenant_id: str | None, scope: str) -> dict:
    tid = resolve_tenant_id(tenant_id)
    return {
        "tenant_id": tid,
        "ledger_scope": scope,
        "_id": compound_ledger_id(tid, scope),
    }


def legacy_scope_id(scope: str) -> str:
    return scope


def is_legacy_doc(doc: dict | None) -> bool:
    if not doc:
        return False
    return not doc.get("tenant_id")