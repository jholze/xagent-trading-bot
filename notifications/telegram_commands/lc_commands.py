import os

from core.config import get_bot_config
from data_manager import load_effective_watchlist, load_lc_signals
from services.social_pipeline import SocialPipeline
from telegram_notifier import send_telegram_message
from x_analyzer import XAnalyzer


def _lc_unavailable_message(cfg, *, metrics_count: int = 0) -> str:
    lc_cfg = cfg.lunarcrush_config
    if not lc_cfg.get("enabled", True):
        return "LunarCrush ist deaktiviert — <code>lunarcrush.enabled</code> in config.json auf true setzen."

    if lc_cfg.get("use_mock"):
        return (
            "Mock-Modus aktiv, aber keine LC-Signale — Watchlist leer oder "
            "keine Mock-Coins (SOL, ETH, BTC, ARIA) auf der Liste."
        )

    api_env = str(lc_cfg.get("api_key_env") or "LUNARCRUSH_API_KEY")
    if not os.getenv(api_env, "").strip():
        return (
            f"LunarCrush API-Key fehlt — <code>{api_env}</code> in Railway/Env setzen, "
            "oder <code>lunarcrush.use_mock: true</code> für Tests ohne Key."
        )

    th = lc_cfg.get("thresholds") or {}
    ttl = lc_cfg.get("signal_ttl_hours", 4)
    if metrics_count <= 0:
        return (
            "<b>Keine LunarCrush-Daten</b>\n\n"
            "API-Key ist gesetzt, aber für die Watchlist kamen keine Metriken zurück.\n"
            "Häufige Ursachen:\n"
            "• Coin nicht bei LunarCrush indexiert (viele Gate-Only-Altcoins)\n"
            "• Individual-Plan: nur Per-Coin-Fetch "
            f"(<code>use_list_endpoint: {lc_cfg.get('use_list_endpoint', True)}</code>)\n"
            "• API-Limit oder temporärer Fehler\n\n"
            "<i>15m-Entry und CMC-Trending laufen unabhängig davon.</i>"
        )

    return (
        "<b>Keine aktiven LC-Signale</b>\n\n"
        f"Metriken für <b>{metrics_count}</b> Coins, aber kein BUY/SELL "
        f"im TTL-Fenster ({ttl}h).\n"
        f"Schwellen: Galaxy ≥ {th.get('buy_galaxy_min', 52)}, "
        f"Sentiment ≥ {th.get('buy_sentiment_min', 55)}.\n"
        "Viele Coins liegen auf HOLD unter der Anzeige-Schwelle.\n\n"
        "Optional: <code>/lcscore</code> nach starkem Galaxy-Move, "
        "oder Schwellen in <code>lunarcrush.thresholds</code> lockern."
    )


def handle(text: str) -> bool:
    if text not in ["/lc", "/lcsignals", "/lcscore"]:
        return False

    cfg = get_bot_config()
    pipeline = SocialPipeline(XAnalyzer())
    pipeline.process_lc_signals(force=True)
    signals = pipeline.refresh_lc_signals()

    if text == "/lcscore" and signals:
        msg = "<b>🌙 LunarCrush Scores</b>\n\n"
        for s in signals[:12]:
            msg += (
                f"<b>{s.coin}</b> — Galaxy {s.galaxy_score:.0f}, "
                f"AltRank {s.alt_rank}, Sentiment {s.sentiment:.0f}% → "
                f"{s.action} ({s.confidence}%)\n"
            )
        send_telegram_message(msg.strip())
        return True

    if not signals:
        logged = load_lc_signals().get("signals", [])
        if logged:
            msg = "<b>🌙 LunarCrush Signals (logged)</b>\n\n"
            for entry in logged[-8:]:
                msg += (
                    f"{entry.get('coin')} {entry.get('action')} ({entry.get('confidence', 0)}%) — "
                    f"Galaxy {entry.get('galaxy_score', 0):.0f} — "
                    f"{str(entry.get('rationale', ''))[:60]}\n"
                )
            send_telegram_message(msg)
            return True
        metrics_count = int(getattr(pipeline, "_last_lc_metrics_count", 0) or 0)
        send_telegram_message(_lc_unavailable_message(cfg, metrics_count=metrics_count))
        return True

    msg = "<b>🌙 LunarCrush Signals</b>\n\n"
    for s in signals[:10]:
        msg += (
            f"<b>{s.coin}</b> {s.action} — {s.confidence}% "
            f"(Galaxy {s.galaxy_score:.0f}, AltRank {s.alt_rank})\n"
            f"  {s.rationale[:80]}\n\n"
        )
    send_telegram_message(msg)
    return True