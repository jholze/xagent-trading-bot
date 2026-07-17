import os

from core.config import get_bot_config
from data.cmc_capabilities import (
    format_cmc_status_line,
    has_community_endpoints,
    has_dexscan_endpoint,
    probe_capabilities,
    trade_path_mode,
)
from data_manager import load_cmc_posts
from notifications.user_explain import explain_cmc_signal
from services.dry_run_watchlist import TrendingWatchlistSync
from services.social_pipeline import SocialPipeline
from notifications.telegram_i18n import t
from telegram_notifier import send_telegram_message
from x_analyzer import XAnalyzer


def _cmc_status_header(cfg) -> str:
    caps = probe_capabilities()
    return f"<i>{format_cmc_status_line(cfg.cmc_config, caps)}</i>"


def _cmc_unavailable_message(cfg) -> str:
    cmc_cfg = cfg.cmc_config
    header = _cmc_status_header(cfg)
    if not cmc_cfg.get("enabled", True):
        return f"{header}\n\nCMC ist deaktiviert — <code>cmc.enabled</code> in config.json auf true setzen."

    if not os.getenv(str(cmc_cfg.get("api_key_env") or "CMC_API_KEY"), "").strip():
        return (
            f"{header}\n\n"
            "CMC API-Key fehlt — <code>CMC_API_KEY</code> in Railway/Env setzen "
            "(Dashboard → Variables)."
        )

    caps = probe_capabilities()
    mode = trade_path_mode(cmc_cfg, caps)
    quotes_fallback = bool(cmc_cfg.get("quotes_fallback_as_signal", False))

    if mode == "quotes_blocked" or (not has_community_endpoints(caps) and not quotes_fallback):
        return (
            f"{header}\n\n"
            "<b>Keine CMC Trade-Signale</b>\n\n"
            "Plan ohne Community/Content — nur Listings/Quotes.\n"
            "Trade-Pfad ist aus: setze <code>cmc.quotes_fallback_as_signal: true</code> "
            "(niedrigeres Trust, <code>sell_requires_ta</code> + Churn-Guards bleiben) "
            "oder CMC-Plan mit Community upgraden.\n\n"
            "<i>Trending-Watchlist + 15m-Entry laufen unabhängig — siehe /trending.</i>"
        )

    return (
        f"{header}\n\n"
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
        bot_cfg = get_bot_config()
        cfg = bot_cfg.cmc_config.get("dexscan_alerts", {})
        if not cfg.get("enabled", True):
            send_telegram_message(t("cmc_dex_disabled"))
            return True
        caps = probe_capabilities()
        if cfg.get("require_endpoint", True) and not has_dexscan_endpoint(caps):
            send_telegram_message(
                f"{_cmc_status_header(bot_cfg)}\n\n"
                "<b>DexScan nicht freigeschaltet</b>\n"
                "Endpoint <code>dex/tokens/trending/list</code> liefert 403 auf diesem Plan.\n"
                "Startup freischaltet oft nur <b>market trending</b> — DexScan braucht "
                "ggf. höheres Add-on / Enterprise. Market-Signale: <code>/cmc</code> · "
                "<code>/trending</code>."
            )
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

    cfg = get_bot_config()
    pipeline = SocialPipeline(XAnalyzer())
    pipeline.process_cmc_posts()
    signals = pipeline.refresh_cmc_signals()
    header = _cmc_status_header(cfg)

    if not signals:
        posts = load_cmc_posts().get("posts", [])
        if posts:
            from notifications.user_explain import cmc_score_line_de, cmc_signal_kind, cmc_source_label_de

            msg = f"<b>📊 CMC Signale (logged)</b>\n{header}\n\n"
            for p in posts[-8:]:
                kind = cmc_signal_kind(p)
                src = cmc_source_label_de(kind)
                bull = p.get("votes_bullish", 0)
                bear = p.get("votes_bearish", 0)
                msg += (
                    f"<b>[{src}]</b> {p.get('coin')} {p.get('action')} "
                    f"({p.get('confidence', 0)}%) — "
                    f"{cmc_score_line_de(kind, bull, bear)}\n"
                    f"  {str(p.get('rationale', ''))[:70]}\n"
                )
            send_telegram_message(msg)
            return True
        send_telegram_message(_cmc_unavailable_message(cfg))
        return True

    msg = (
        f"<b>📊 CMC Signale</b> — Beobachtung\n{header}\n"
        f"<i>„Community-Votes“ nur bei echter Community-API; "
        f"sonst Trend-/Kurs-Score.</i>\n\n"
    )
    for s in signals[:10]:
        msg += explain_cmc_signal(s) + "\n\n"
    send_telegram_message(msg)
    return True