"""Webhook token validation."""

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
    allow_no_token = bool(arch.get("signal_webhook_allow_no_token", True))
    if allow_no_token:
        log(
            "signal_webhook: no token configured — request allowed unauthenticated "
            "(set SIGNAL_WEBHOOK_TOKEN or architecture.signal_webhook_allow_no_token=false)",
            "WARNING",
        )
    return allow_no_token