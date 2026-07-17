import os

from core.config import get_bot_config
from data.cmc_capabilities import endpoint_available, probe_capabilities
from data_manager import load_cmc_posts
from notifications.user_explain import explain_cmc_signal
from services.dry_run_watchlist import TrendingWatchlistSync
from services.social_pipeline import SocialPipeline
from notifications.telegram_i18n import t
from telegram_notifier import send_telegram_message
from x_analyzer import XAnalyzer


def _cmc_unavailable_message(cfg) -> str:
    cmc_cfg = cfg.cmc_config
    if not cmc_cfg.get("enabled", True):
        return "CMC ist deaktiviert — <code>cmc.enabled</code> in config.json auf true setzen."

    if not os.getenv(str(cmc_cfg.get("api_key_env") or "CMC_API_KEY"), "").strip():
        return (
            "CMC API-Key fehlt — <code>CMC_API_KEY</code> in Railway/Env setzen "
            "(Dashboard → Variables)."
        )

    caps = probe_capabilities()
    has_community = endpoint_available("community/trending/token", caps) or endpoint_available(
        "content/latest", caps
    )
    quotes_fallback = bool(cmc_cfg.get("quotes_fallback_as_signal", False))

    if not has_community and not quotes_fallback:
        return (
            "<b>Keine CMC Community-Signale</b>\n\n"
            "Dein CMC-Plan hat keine Community/Content-Endpoints "
            "(nur <code>quotes/latest</code>-Fallback aktiv).\n"
            "Für /cmc-Anzeige: <code>cmc.quotes_fallback_as_signal: true</code> "
            "oder CMC-Plan mit Community upgraden.\n\n"
            "<i>Trending-Watchlist + 15m-Entry laufen unabhängig davon — "
            "siehe /trending.</i>"
        )

    return (
        "Keine aktiven CMC-Signale im TTL-Fenster "
        f"({cmc_cfg.get('signal_ttl_hours', 4)}h). "
        "Nächster Bot-Cycle holt neue Daten."
    )


def handle(text: str) -> bool:
    if text == "/trending":
        status = TrendingWatchlistSync(get_bot_config()).status()
        lines = [
            "<b>📈 CMC Trending Watchlist</b>",
            f"Aktiv: {'ja' if status.get('enabled') else 'nein'}",
            f"Coins: {status.get('trending_count', 0)}",
            f"Quelle: {status.get('source') or '—'}",
            f"Sync: {status.get('refreshed_at') or '—'}",
            "",
        ]
        added = status.get("added_last") or []
        removed = status.get("removed_last") or []
        if added:
            lines.append("<b>Zuletzt hinzugefügt:</b>")
            for c in added[:10]:
                lines.append(f"• {c.get('symbol')} (#{c.get('trending_rank', '?')})")
        if removed:
            lines.append("<b>Zuletzt entfernt:</b>")
            for sym in removed[:10]:
                lines.append(f"• {sym}")
        if status.get("enabled"):
            lines.append("")
            lines.append("<i>Beobachtung — Trade nur bei ✅ EXECUTED im Cycle.</i>")
        send_telegram_message("\n".join(lines))
        return True

    if text == "/dexsignals":
        cfg = get_bot_config().cmc_config.get("dexscan_alerts", {})
        if not cfg.get("enabled", True):
            send_telegram_message(t("cmc_dex_disabled"))
            return True
        from data.cmc_dex_signals_provider import get_dexscan_provider

        alerts = get_dexscan_provider().fetch_alerts(limit=int(cfg.get("max_alerts", 10)))
        if not alerts:
            send_telegram_message(t("cmc_dex_none"))
            return True
        lines = ["<b>🔔 DexScan Alerts</b> — nur Info, kein Auto-Trade", ""]
        for a in alerts[:10]:
            gate = "Gate ✅" if a.gate_tradeable else "kein Gate"
            lines.append(f"• <b>{a.symbol}</b> ({a.platform}) — {a.signal_type} · {gate}")
        send_telegram_message("\n".join(lines))
        return True

    if text not in ["/cmc", "/cmcsignals"]:
        return False

    pipeline = SocialPipeline(XAnalyzer())
    pipeline.process_cmc_posts()
    signals = pipeline.refresh_cmc_signals()

    if not signals:
        posts = load_cmc_posts().get("posts", [])
        if posts:
            msg = "<b>📊 CMC Community Signals (logged)</b>\n\n"
            for p in posts[-8:]:
                msg += (
                    f"{p.get('coin')} {p.get('action')} ({p.get('confidence', 0)}%) — "
                    f"{p.get('rationale', '')[:60]}\n"
                )
            send_telegram_message(msg)
            return True
        send_telegram_message(_cmc_unavailable_message(get_bot_config()))
        return True

    msg = "<b>📊 CMC Signale</b> — Beobachtung\n\n"
    for s in signals[:10]:
        msg += explain_cmc_signal(s) + "\n\n"
    send_telegram_message(msg)
    return True