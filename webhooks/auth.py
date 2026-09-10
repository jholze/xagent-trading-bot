"""Webhook token validation and Telegram sender allowlist."""

from __future__ import annotations

import os


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
    # Fail closed: an open signal webhook would inject watches/trades.
    return False


def telegram_allowed_chat_ids() -> set[str]:
    allowed: set[str] = set()
    primary = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if primary:
        allowed.add(primary)
    extra = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    if extra:
        allowed.update(part.strip() for part in extra.split(",") if part.strip())
    return allowed


def telegram_sender_allowed(chat_id) -> bool:
    """Only the configured owner chat(s) may drive commands."""
    allowed = telegram_allowed_chat_ids()
    if not allowed:
        return False
    return str(chat_id or "").strip() in allowed


def telegram_chat_id_from_update(update: dict | None) -> str | None:
    if not update:
        return None
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    if chat.get("id") is not None:
        return str(chat.get("id"))
    callback = update.get("callback_query") or {}
    cb_message = callback.get("message") or {}
    cb_chat = cb_message.get("chat") or {}
    if cb_chat.get("id") is not None:
        return str(cb_chat.get("id"))
    sender = callback.get("from") or {}
    if sender.get("id") is not None:
        return str(sender.get("id"))
    return None
