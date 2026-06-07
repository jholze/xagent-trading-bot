from data_manager import load_cmc_posts
from services.social_pipeline import SocialPipeline
from telegram_notifier import send_telegram_message
from x_analyzer import XAnalyzer


def handle(text: str) -> bool:
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
        send_telegram_message("No CMC community signals available. Enable cmc.enabled in config.")
        return True

    msg = "<b>📊 CMC Community Signals</b>\n\n"
    for s in signals[:10]:
        msg += (
            f"<b>{s.coin}</b> {s.action} — {s.confidence}% "
            f"(votes {s.votes_bullish}↑/{s.votes_bearish}↓)\n"
            f"  {s.rationale[:80]}\n\n"
        )
    send_telegram_message(msg)
    return True