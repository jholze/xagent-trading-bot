"""One-shot tenant ledger repair after multi-tenant leakage (startup hook)."""

from __future__ import annotations

from core.tenant_context import DEFAULT_TENANT, multi_tenant_enabled
from logger import log


def maybe_repair_tenant_ledgers_once() -> None:
    """Split tenant leakage, then merge legacy operator docs into compound ledgers."""
    if not multi_tenant_enabled():
        return
    try:
        from storage.mongo_client import get_database
        from storage.tenant_registry import list_active_tenants
        from scripts.repair_tenant_ledgers import repair_tenant_ledgers
        from storage.ledger_merge import merge_operator_ledger_scope
    except Exception as e:
        log(f"Ledger repair skipped (import): {e}", "WARNING")
        return

    marker_id = "ledger_repair_v6"
    try:
        db = get_database()
        meta = db["meta"]
        if meta.find_one({"_id": marker_id}, {"done": 1}):
            return

        scopes = ("paper", "demo")
        tenants = [
            str(doc.get("tenant_id") or "").strip()
            for doc in list_active_tenants()
            if str(doc.get("tenant_id") or "").strip()
            and str(doc.get("tenant_id") or "").strip() != DEFAULT_TENANT
            and str((doc.get("telegram") or {}).get("owner_chat_id") or "").strip()
        ]

        for scope in scopes:
            legacy_scope_doc = db["orders"].find_one({"_id": scope})
            has_legacy_scope = bool(
                legacy_scope_doc and legacy_scope_doc.get("tenant_id") is None
            )
            if has_legacy_scope:
                for tid in tenants:
                    stats = repair_tenant_ledgers(
                        scope=scope,
                        target_tenant=tid,
                        dry_run=False,
                        test=False,
                    )
                    log(f"Ledger repair {tid}/{scope}: {stats.get('collections', {})}", "INFO")
            else:
                log(
                    f"Ledger repair skipped tenant split for scope={scope} "
                    "(no legacy scope doc — would wipe operator ledger)",
                    "INFO",
                )

            merge_stats = merge_operator_ledger_scope(
                scope=scope,
                dry_run=False,
                test=False,
                delete_legacy=True,
            )
            log(
                f"Operator ledger merge {scope}: {merge_stats.get('collections', {})}",
                "INFO",
            )

        from data_manager import reconcile_demo_trade_history_on_startup
        from services.ledger_sync import rebuild_positions_from_orders

        for tid in tenants:
            rebuild_positions_from_orders("demo", tenant_id=tid)
        reconcile_demo_trade_history_on_startup()

        meta.replace_one(
            {"_id": marker_id},
            {"_id": marker_id, "done": True, "version": 6},
            upsert=True,
        )
    except Exception as e:
        log(f"Ledger repair failed: {e}", "ERROR")