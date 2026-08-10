"""Tenant registry for multi-tenant SaaS: supports creation, retrieval and per-tenant encrypted Gate credentials."""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken

from core.tenant_context import DEFAULT_TENANT
from logger import log
from storage.mongo_client import get_database

TENANTS_COLLECTION = "tenants"

# Stable test key for unit tests (32 bytes url-safe base64). Never use in prod.
_TEST_FERNET_KEY = "MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE="


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_fernet(test: bool = False) -> Fernet:
    key = os.getenv("TENANT_SECRET_KEY")
    if not key and test:
        key = _TEST_FERNET_KEY
    if not key:
        raise RuntimeError("TENANT_SECRET_KEY env var is required for tenant secret operations")
    if isinstance(key, str):
        key = key.encode()
    return Fernet(key)


def _encrypt(value: str | None, test: bool = False) -> str:
    if not value:
        return ""
    f = _get_fernet(test=test)
    return f.encrypt(value.encode()).decode()


def _decrypt(enc_value: str | None, test: bool = False) -> str:
    if not enc_value:
        return ""
    f = _get_fernet(test=test)
    try:
        return f.decrypt(enc_value.encode()).decode()
    except (InvalidToken, Exception):
        return ""


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
        "defaults": {"trading_mode": "paper", "ledger_scope": "paper", "ui_language": "de"},
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    coll.replace_one({"tenant_id": DEFAULT_TENANT}, doc, upsert=True)
    return doc


def create_tenant(
    tenant_id: str,
    *,
    plan: str = "free",
    status: str = "active",
    owner_chat_id: str = "",
    bot_token: str = "",
    gate_api_key: str = "",
    gate_api_secret: str = "",
    limits: dict | None = None,
    features: list[str] | None = None,
    test: bool = False,
) -> dict:
    """Create or upsert a full tenant record with encrypted Gate credentials."""
    db = get_database(test=test)
    coll = db[TENANTS_COLLECTION]
    doc = {
        "tenant_id": tenant_id,
        "status": status,
        "plan": plan,
        "limits": limits or {
            "max_open_positions": 10,
            "max_daily_trades": 20,
            "max_daily_usdt": 3000,
            "allow_live": False,
        },
        "features": features or ["basic"],
        "telegram": {
            "owner_chat_id": owner_chat_id,
            "bot_token_enc": _encrypt(bot_token, test=test) if bot_token else "",
            "bot_token_ref": "env:TELEGRAM_BOT_TOKEN" if not bot_token else "",
            "webhook_secret": secrets.token_urlsafe(32),
        },
        "exchange": {
            "gate": {
                "api_key_enc": _encrypt(gate_api_key, test=test),
                "api_secret_enc": _encrypt(gate_api_secret, test=test),
                "testnet": False,
            }
        },
        "defaults": {"trading_mode": "paper", "ledger_scope": "paper", "ui_language": "de"},
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    coll.replace_one({"tenant_id": tenant_id}, doc, upsert=True)
    return doc


def get_tenant(tenant_id: str, *, test: bool = False) -> dict | None:
    """Retrieve tenant doc by id."""
    db = get_database(test=test)
    coll = db[TENANTS_COLLECTION]
    return coll.find_one({"tenant_id": tenant_id})


def get_gate_credentials(tenant_id: str, *, test: bool = False) -> dict[str, str]:
    """Return decrypted Gate API credentials for the tenant (empty strings if missing)."""
    tenant = get_tenant(tenant_id, test=test) or {}
    gate = (tenant.get("exchange") or {}).get("gate") or {}
    return {
        "api_key": _decrypt(gate.get("api_key_enc"), test=test),
        "api_secret": _decrypt(gate.get("api_secret_enc"), test=test),
    }


def get_webhook_secret(tenant_id: str, *, test: bool = False) -> str:
    """Return the webhook secret for secret_token validation (empty if not set)."""
    tenant = get_tenant(tenant_id, test=test) or {}
    return (tenant.get("telegram") or {}).get("webhook_secret", "")


def list_active_tenants(*, test: bool = False) -> list[dict]:
    db = get_database(test=test)
    coll = db[TENANTS_COLLECTION]
    return list(coll.find({"status": "active"}))


def link_tenant_owner_chat(
    tenant_id: str,
    chat_id: str | int,
    *,
    test: bool = False,
) -> tuple[bool, str]:
    """Bind a Telegram chat to a tenant (invite /start flow). Returns (ok, message)."""
    tid = (tenant_id or "").strip().lower()
    cid = str(chat_id or "").strip()
    if not tid or not cid:
        return False, "tenant_id und chat_id erforderlich"

    doc = get_tenant(tid, test=test)
    if not doc:
        return False, f"Tenant <code>{tid}</code> existiert nicht."

    op_chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    current = str((doc.get("telegram") or {}).get("owner_chat_id") or "").strip()
    if current and current != cid and current != op_chat:
        return False, "Dieser Tenant ist bereits mit einem anderen Chat verbunden."

    try:
        db = get_database(test=test)
        coll = db[TENANTS_COLLECTION]
        coll.update_one(
            {"tenant_id": tid},
            {
                "$set": {
                    "telegram.owner_chat_id": cid,
                    "updated_at": _now_iso(),
                }
            },
        )
        log(f"[TENANT-LINK] {tid} → chat {cid}", "INFO")
        return True, (
            f"✅ Verbunden mit <code>{tid}</code> (Paper).\n\n"
            f"<code>/menu</code> · <code>/help</code> · <code>/myid</code>"
        )
    except Exception as e:
        log(f"tenant_registry: link_owner failed for {tid}: {e}", "WARNING")
        return False, "Verknüpfung fehlgeschlagen."


def find_tenant_by_owner_chat_id(chat_id: str | int, *, test: bool = False) -> dict | None:
    """Lookup active tenant by telegram.owner_chat_id (shared-bot routing)."""
    cid = str(chat_id or "").strip()
    if not cid:
        return None
    try:
        db = get_database(test=test)
        coll = db[TENANTS_COLLECTION]
        return coll.find_one(
            {"status": "active", "telegram.owner_chat_id": cid},
            sort=[("updated_at", -1)],
        )
    except Exception as e:
        log(f"tenant_registry: find by chat_id failed: {e}", "WARNING")
        return None
