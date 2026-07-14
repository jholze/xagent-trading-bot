"""One-shot tenant ledger repair after multi-tenant leakage (startup hook)."""

from __future__ import annotations

from core.tenant_context import DEFAULT_TENANT, multi_tenant_enabled
from logger import log


def maybe_repair_tenant_ledgers_once() -> None:
    """Split operator vs tenant ledgers in Mongo once per deployment generation."""
    if not multi_tenant_enabled():
        return
    try:
        from storage.mongo_client import get_database
        from storage.tenant_registry import list_active_tenants
        from scripts.repair_tenant_ledgers import repair_tenant_ledgers
    except Exception as e:
        log(f"Ledger repair skipped (import): {e}", "WARNING")
        return

    marker_id = "ledger_repair_v2"
    try:
        db = get_database()
        meta = db["meta"]
        if meta.find_one({"_id": marker_id}, {"done": 1}):
            return
        scopes = ("paper",)
        for doc in list_active_tenants():
            tid = str(doc.get("tenant_id") or "").strip()
            if not tid or tid == DEFAULT_TENANT:
                continue
            owner = str((doc.get("telegram") or {}).get("owner_chat_id") or "").strip()
            if not owner:
                continue
            for scope in scopes:
                stats = repair_tenant_ledgers(
                    scope=scope,
                    target_tenant=tid,
                    dry_run=False,
                    test=False,
                )
                log(f"Ledger repair {tid}/{scope}: {stats.get('collections', {})}", "INFO")
        meta.replace_one(
            {"_id": marker_id},
            {"_id": marker_id, "done": True, "version": 2},
            upsert=True,
        )
    except Exception as e:
        log(f"Ledger repair failed: {e}", "ERROR")