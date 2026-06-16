from data_manager import load_lc_signals
from services.social_pipeline import SocialPipeline
from telegram_notifier import send_telegram_message
from x_analyzer import XAnalyzer


def handle(text: str) -> bool:
    if text not in ["/lc", "/lcsignals", "/lcscore"]:
        return False

    pipeline = SocialPipeline(XAnalyzer())
    pipeline.process_lc_signals()
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
        send_telegram_message(
            "No LunarCrush signals available. Enable lunarcrush.enabled in config "
            "(use_mock: true works without API key)."
        )
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