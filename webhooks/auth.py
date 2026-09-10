"""Webhook token validation and the single-tenant Telegram owner allowlist."""

from __future__ import annotations

import os

from logger import log


def signal_webhook_token_ok(provided: str | None, config_raw: dict | None = None) -> bool:
    env_token = os.environ.get("SIGNAL_WEBHOOK_TOKEN", "").strip()
    if env_token:
        return (provided or "").strip() == env_token
    if config_raw is None:
        from core.config import get_bot_config

        config_raw = get_bot_config().raw
    arch = (config_raw or {}).get("architecture") or {}
    cfg_token = str(arch.get("signal_webhook_token") or "").strip()
    if cfg_token:
        return (provided or "").strip() == cfg_token
    allow_no_token = bool(arch.get("signal_webhook_allow_no_token", False))
    if allow_no_token:
        log(
            "signal_webhook: no token configured — request allowed unauthenticated "
            "(set SIGNAL_WEBHOOK_TOKEN or architecture.signal_webhook_allow_no_token=false)",
            "WARNING",
        )
    return allow_no_token


def telegram_allowed_chat_ids() -> set[str]:
    """Owner chat(s) allowed to drive the bot when multi-tenancy is off.

    Multi-tenant deployments gate senders via core.tenant_routing's tenant
    registry instead (a separate, existing check) -- this allowlist only
    matters for a single-owner deployment with multi-tenancy disabled.
    """
    allowed: set[str] = set()
    primary = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if primary:
        allowed.add(primary)
    extra = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    if extra:
        allowed.update(part.strip() for part in extra.split(",") if part.strip())
    return allowed


def telegram_sender_allowed(chat_id) -> bool:
    """True if chat_id is a recognized owner chat. False if none is configured."""
    allowed = telegram_allowed_chat_ids()
    if not allowed:
        return False
    return str(chat_id or "").strip() in allowed