"""Minimal tenant registry for Phase 0 migration and onboarding."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from core.tenant_context import DEFAULT_TENANT
from storage.mongo_client import get_database

TENANTS_COLLECTION = "tenants"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_default_tenant(
    *,
    test: bool = False,
    owner_chat_id: str | None = None,
    bot_token: str | None = None,
) -> dict:
    db = get_database(test=test)
    coll = db[TENANTS_COLLECTION]
    existing = coll.find_one({"tenant_id": DEFAULT_TENANT})
    if existing:
        return existing
    doc = {
        "tenant_id": DEFAULT_TENANT,
        "status": "active",
        "plan": "legacy",
        "telegram": {
            "owner_chat_id": owner_chat_id or os.environ.get("TELEGRAM_CHAT_ID", ""),
            "bot_token_ref": "env:TELEGRAM_BOT_TOKEN" if bot_token else "",
        },
        "defaults": {"trading_mode": "paper", "ledger_scope": "paper"},
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    coll.replace_one({"tenant_id": DEFAULT_TENANT}, doc, upsert=True)
    return doc