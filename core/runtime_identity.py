"""Runtime identity — which bot instance, code revision, and feature flags."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from core.build_info import get_build_info


def resolve_bot_stack() -> str:
    """Return production | staging | local | unknown."""
    explicit = (os.getenv("BOT_STACK") or "").strip().lower()
    if explicit in {"production", "prod", "live"}:
        return "production"
    if explicit in {"staging", "test", "dev"}:
        return "staging"
    if explicit in {"local", "mac"}:
        return "local"

    service = (os.getenv("RAILWAY_SERVICE_NAME") or "").strip().lower()
    if "staging" in service or "test" in service:
        return "staging"
    if service in {"xagent-bot", "aria-bot", "trading-bot"}:
        return "production"

    env = (os.getenv("RAILWAY_ENVIRONMENT") or "").strip().lower()
    if env in {"test", "staging"}:
        return "staging"
    if env == "production" and os.getenv("RAILWAY_DEPLOY"):
        return "production"

    if os.getenv("RAILWAY_DEPLOY") or os.getenv("RAILWAY_PUBLIC_DOMAIN"):
        return "production"

    if os.getenv("DEMO_MODE") == "1" and not os.getenv("RAILWAY_DEPLOY"):
        return "local"

    return "unknown"


_STACK_LABELS = {
    "production": ("🟢", "Production"),
    "staging": ("🧪", "Staging"),
    "local": ("💻", "Local"),
    "unknown": ("❔", "Unbekannt"),
}


def stack_badge(stack: str | None = None) -> str:
    stack = stack or resolve_bot_stack()
    if stack == "test":
        stack = "staging"
    emoji, label = _STACK_LABELS.get(stack, _STACK_LABELS["unknown"])
    return f"{emoji} [{label.upper()}]"


def message_prefix() -> str:
    """Short prefix for every Telegram message."""
    stack = resolve_bot_stack()
    if stack == "unknown":
        from data_manager import is_demo_mode

        if is_demo_mode():
            return "🧪 [DEMO] "
        return ""
    return f"{stack_badge(stack)} "


def _public_url() -> str:
    base = (os.getenv("WEBHOOK_BASE_URL") or "").strip().rstrip("/")
    if base:
        return base if base.startswith("http") else f"https://{base}"
    domain = (os.getenv("RAILWAY_PUBLIC_DOMAIN") or "").strip()
    if domain:
        return f"https://{domain}"
    return ""


def _feature_flags(config_raw: dict | None = None) -> dict[str, bool]:
    if config_raw is None:
        try:
            from core.config import get_bot_config

            config_raw = get_bot_config().raw
        except Exception:
            config_raw = {}
    arch = (config_raw or {}).get("architecture") or {}
    redis = False
    try:
        from bus.redis_client import get_redis, resolve_redis_url, reset_redis_client

        reset_redis_client()
        redis = get_redis(resolve_redis_url(arch.get("redis_url"))) is not None
    except Exception:
        redis = False
    try:
        from core.tenant_context import multi_tenant_enabled

        mt = multi_tenant_enabled()
    except Exception:
        mt = False
    regime_on = bool((config_raw or {}).get("regime_detector", {}).get("enabled", False))
    allocator_on = bool((config_raw or {}).get("strategy_allocator", {}).get("enabled", False))
    return {
        "redis": redis,
        "price_cache": bool(arch.get("price_cache_enabled", True)),
        "ohlcv_cache": bool(arch.get("ohlcv_cache_enabled", True)),
        "signal_webhook": bool(arch.get("signal_webhook_enabled", True)),
        "coin_webhook": bool(arch.get("coin_query_webhook_enabled", True)),
        "multi_tenant": mt,
        "regime": regime_on,
        "strategy_allocator": allocator_on,
    }


def get_runtime_identity(config_raw: dict | None = None) -> dict:
    build = get_build_info()
    stack = resolve_bot_stack()
    emoji, stack_label = _STACK_LABELS.get(stack, _STACK_LABELS["unknown"])

    mongo_db = os.getenv("MONGODB_DB", "")
    if not mongo_db:
        try:
            from storage.mongo_client import resolve_database_name

            mongo_db = resolve_database_name()
        except Exception:
            mongo_db = ""

    return {
        "stack": stack,
        "stack_label": stack_label,
        "stack_emoji": emoji,
        "service": os.getenv("RAILWAY_SERVICE_NAME", ""),
        "environment": os.getenv("RAILWAY_ENVIRONMENT", ""),
        "commit": build["commit"],
        "branch": build["branch"],
        "dirty": build["dirty"],
        "public_url": _public_url(),
        "demo_mode": os.getenv("DEMO_MODE") == "1",
        "mongo_db": mongo_db,
        "ledger_backend": os.getenv("DEMO_LEDGER_BACKEND") or "mongo",
        "deployment_id": (os.getenv("RAILWAY_DEPLOYMENT_ID") or "")[:8],
        "features": _feature_flags(config_raw),
    }


def format_identity_section(*, html: bool = True) -> str:
    """Bot instance + code revision block (embedded in /mode)."""
    info = get_runtime_identity()
    dirty = " *" if info["dirty"] else ""
    features = info["features"]
    feat_lines = [
        f"Redis: {'✅' if features['redis'] else '❌'}",
        f"Price-Cache: {'✅' if features['price_cache'] else '❌'}",
        f"OHLCV-Cache: {'✅' if features['ohlcv_cache'] else '❌'}",
        f"Signal-Webhook: {'✅' if features['signal_webhook'] else '❌'}",
        f"Multi-Tenant: {'✅' if features.get('multi_tenant') else '❌'}",
        f"Regime: {'✅' if features.get('regime') else '❌'}",
        f"Allocator: {'✅' if features.get('strategy_allocator') else '❌'}",
    ]

    if html:
        lines = [
            f"<b>{info['stack_emoji']} Instanz:</b> {info['stack_label']}",
            f"<b>Code:</b> <code>{info['commit']}{dirty}</code> · <code>{info['branch']}</code>",
        ]
        if info["service"]:
            lines.append(f"<b>Service:</b> <code>{info['service']}</code>")
        if info["environment"]:
            lines.append(f"<b>Railway-Env:</b> <code>{info['environment']}</code>")
        if info["public_url"]:
            lines.append(f"<b>URL:</b> {info['public_url']}")
        lines.append(
            f"<b>Demo:</b> {'ON' if info['demo_mode'] else 'OFF'} · "
            f"<b>Mongo:</b> <code>{info['mongo_db'] or '—'}</code> · "
            f"<b>Ledger:</b> <code>{info['ledger_backend']}</code>"
        )
        if info["deployment_id"]:
            lines.append(f"<b>Deploy:</b> <code>{info['deployment_id']}</code>")
        from core.time_utils import format_display_with_zone

        lines.append(f"<b>Zeit:</b> {format_display_with_zone()}")
        lines.append("<b>Features:</b> " + " · ".join(feat_lines))
        return "\n".join(lines)

    return (
        f"{info['stack_label']} | {info['commit']}{dirty} | {info['branch']} | "
        f"{info['public_url'] or 'no url'}"
    )


def format_build_line(html: bool = True) -> str:
    """Compact one-liner for /mode and briefings."""
    info = get_runtime_identity()
    dirty = " *" if info["dirty"] else ""
    badge = stack_badge(info["stack"])
    if html:
        return (
            f"{badge} · Version <code>{info['commit']}{dirty}</code> · "
            f"Branch <code>{info['branch']}</code>"
        )
    return f"{badge} · {info['commit']}{dirty} · {info['branch']}"


def format_startup_message() -> str:
    info = get_runtime_identity()
    dirty = " *" if info["dirty"] else ""
    from core.time_utils import format_display_with_zone

    when = format_display_with_zone()
    lines = [
        f"<b>Bot gestartet — {info['stack_label']}</b>",
        "",
        f"<code>{info['commit']}{dirty}</code> · <code>{info['branch']}</code>",
    ]
    if info["service"]:
        lines.append(f"Service: <code>{info['service']}</code>")
    if info["public_url"]:
        lines.append(f"Webhook: {info['public_url']}")
    lines.append(f"Zeit: {when}")
    lines.append("")
    lines.append("Details: <code>/mode</code>")
    return "\n".join(lines)


def should_notify_startup() -> bool:
    if os.getenv("TELEGRAM_STARTUP_NOTIFY") == "0":
        return False
    if os.getenv("TELEGRAM_STARTUP_NOTIFY") == "1":
        return True
    if not (os.getenv("RAILWAY_DEPLOY") or os.getenv("RAILWAY_PUBLIC_DOMAIN")):
        return False
    try:
        from core.config import get_bot_config

        return bool(
            get_bot_config().observability_config.get("telegram_startup_notify", True)
        )
    except Exception:
        return True