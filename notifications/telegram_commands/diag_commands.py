"""Telegram /diag — operator snapshot of cycle health, workers, sources, queues."""

from __future__ import annotations

import os

from html import escape as html_escape

from notifications.telegram_i18n import t
from telegram_notifier import send_telegram_message


def _yes_no(ok: bool) -> str:
    return t("diag_yes") if ok else t("diag_no")


def _data_source_flags() -> dict:
    x_token = bool((os.getenv("X_API_BEARER_TOKEN") or "").strip())
    cmc_env = "CMC_API_KEY"
    lc_env = "LUNARCRUSH_API_KEY"
    try:
        from core.config import get_bot_config

        cfg = get_bot_config()
        cmc_env = str(cfg.cmc_config.get("api_key_env") or cmc_env)
        lc_env = str(cfg.lunarcrush_config.get("api_key_env") or lc_env)
    except Exception:
        pass
    cmc_key = bool((os.getenv(cmc_env) or "").strip())
    lc_key = bool((os.getenv(lc_env) or "").strip())
    lc_blocked = False
    try:
        from data.lunarcrush_provider import list_tier_blocked

        lc_blocked = bool(list_tier_blocked())
    except Exception:
        pass
    cmc_plan = ""
    try:
        from data.cmc_capabilities import cached_capabilities

        caps = cached_capabilities() or {}
        cmc_plan = str(caps.get("plan_label") or "")
    except Exception:
        pass
    return {
        "x_token": x_token,
        "cmc_key": cmc_key,
        "cmc_plan": cmc_plan,
        "lc_key": lc_key,
        "lc_blocked": lc_blocked,
    }


def _queue_sizes() -> tuple[int, int]:
    intents = 0
    eval_depth = 0
    try:
        from bus.trade_intents import trade_intent_queue

        intents = int(trade_intent_queue.depth())
    except Exception:
        pass
    try:
        from bus.eval_queue import queue_depth

        eval_depth = int(queue_depth())
    except Exception:
        pass
    return intents, eval_depth


def build_diag_report() -> str:
    from core.cycle_health import snapshot

    snap = snapshot()
    age = snap.get("last_cycle_age_sec")
    if age is None:
        cycle_age = t("diag_cycle_never")
    else:
        cycle_age = t("diag_cycle_age_sec", age=age)
    failures = int(snap.get("consecutive_failures") or 0)

    stale: list[str] = []
    try:
        from bus.heartbeats import heartbeat_registry

        stale = list(heartbeat_registry.stale_workers() or [])
    except Exception:
        stale = []
    stale_txt = ", ".join(html_escape(s) for s in stale) if stale else t("diag_stale_none")

    flags = _data_source_flags()
    sources = t(
        "diag_sources_line",
        x=_yes_no(flags["x_token"]),
        cmc=_yes_no(flags["cmc_key"]),
        cmc_plan=html_escape(flags["cmc_plan"]) if flags["cmc_plan"] else t("diag_cmc_plan_unknown"),
        lc=_yes_no(flags["lc_key"]),
        lc_blocked=_yes_no(flags["lc_blocked"]),
    )
    intents, eval_depth = _queue_sizes()
    queues = t("diag_queues_line", intents=intents, eval=eval_depth)
    return t(
        "diag_report",
        cycle_age=cycle_age,
        failures=failures,
        stale=stale_txt,
        sources=sources,
        queues=queues,
    )


def handle(text: str) -> bool:
    if text != "/diag" and not str(text).startswith("/diag "):
        return False
    send_telegram_message(build_diag_report())
    return True
