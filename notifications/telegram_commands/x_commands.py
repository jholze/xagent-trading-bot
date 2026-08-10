import os
import threading
from datetime import datetime
from html import escape

from bus.jobs import heavy_job_queue
from data_manager import get_config, load_x_accounts, load_x_posts, save_x_accounts
from notifications.telegram_commands.command_context import current_chat_id
from intelligence.x_account_backtest import XAccountBacktester
from notifications.telegram_commands.command_context import activate_command
from notifications.telegram_commands.usage_hints import hint
from notifications.telegram_commands.utils import safe_int
from intelligence.accuracy_tracker import AccuracyTracker
from services.signal_orchestrator import SignalOrchestrator
from notifications.telegram_i18n import t
from telegram_notifier import (
    answer_callback_query,
    send_telegram_buttons,
    send_telegram_message,
    send_x_recommendation_message,
)
from x_analyzer import XAnalyzer


def _normalize_handle(raw: str) -> str:
    return raw.strip().replace("@", "").strip()


def add_x_account(handle_name: str) -> tuple[bool, str]:
    accounts = load_x_accounts()
    if any(a.get("handle", a) == handle_name for a in accounts):
        return False, f"@{handle_name} is already monitored."
    accounts.append({
        "handle": handle_name,
        "trust_score": 70,
        "enabled": True,
        "notes": "Added via Telegram",
    })
    if save_x_accounts(accounts):
        return True, t("x_added", handle=handle_name)
    return False, t("x_save_failed")


def _parse_testaccount_args(text: str) -> tuple[str | None, int | None]:
    parts = [p.strip() for p in text.split() if p.strip()]
    if len(parts) < 2:
        return None, None
    handle = _normalize_handle(parts[1])
    if not handle:
        return None, None
    cfg = get_config().get("x_backtest", {})
    default_days = cfg.get("default_days", 60)
    max_days = cfg.get("max_days", 365)
    days = safe_int(parts[2], default=default_days) if len(parts) > 2 else default_days
    days = max(1, min(days, max_days))
    return handle, days


def _run_backtest(handle: str, days: int):
    def progress(msg: str):
        send_telegram_message(msg)

    try:
        backtester = XAccountBacktester(progress_callback=progress)
        result = backtester.run(handle, days=days)
        summary = result.to_telegram_summary()
        already_monitored = any(
            a.get("handle", a) == handle for a in load_x_accounts()
        )
        if already_monitored:
            summary += f"\n\nℹ️ @{handle} wird bereits überwacht."
            send_telegram_message(summary)
            return

        prompt = (
            f"{summary}\n\n"
            f"<b>@{handle} zur Monitoring-Liste hinzufügen?</b>"
        )
        send_telegram_buttons(prompt, [[
            {"text": t("x_yes_add"), "callback_data": f"testaccount_add:{handle}"},
            {"text": t("x_no_add"), "callback_data": f"testaccount_skip:{handle}"},
        ]])
    except Exception as e:
        send_telegram_message(t("x_backtest_failed", handle=handle, error=e))


def handle_callback(callback_query: dict) -> bool:
    data = callback_query.get("data", "")
    callback_id = callback_query.get("id")
    if not data.startswith("testaccount_"):
        return False

    answer_callback_query(callback_id)

    if data.startswith("testaccount_add:"):
        handle = data.split(":", 1)[1]
        ok, msg = add_x_account(handle)
        send_telegram_message(msg)
        return True

    if data.startswith("testaccount_skip:"):
        handle = data.split(":", 1)[1]
        send_telegram_message(t("x_skipped_add", handle=handle))
        return True

    return False


def handle(text: str) -> bool:
    if text == "/addx":
        activate_command("addx")
        send_telegram_message(hint("addx"))
        return True

    if text == "/removex":
        activate_command("removex")
        send_telegram_message(hint("removex"))
        return True

    if text.startswith("/addx "):
        handle_name = _normalize_handle(text[6:])
        if not handle_name:
            send_telegram_message(hint("addx"))
            return True
        ok, msg = add_x_account(handle_name)
        send_telegram_message(msg)
        return True

    if text.startswith("/removex "):
        handle_name = _normalize_handle(text[8:])
        if not handle_name:
            send_telegram_message(hint("removex"))
            return True
        accounts = load_x_accounts()
        new_accounts = [a for a in accounts if a.get("handle", a) != handle_name]
        if len(new_accounts) != len(accounts):
            if save_x_accounts(new_accounts):
                send_telegram_message(t("x_removed", handle=handle_name))
            else:
                send_telegram_message(t("x_save_failed"))
        else:
            send_telegram_message(t("x_not_found", handle=handle_name))
        return True

    if text in ["/listx", "/xaccounts", "/xlist"]:
        accounts = load_x_accounts()
        if not accounts:
            send_telegram_message(t("x_none"))
        else:
            msg = "<b>📋 Monitored X Accounts:</b>\n\n"
            for a in accounts:
                handle_name = escape(str(a.get("handle", a) or ""))
                trust = a.get("trust_score", 70)
                enabled = "🟢" if a.get("enabled", True) else "🔴"
                notes = escape(str(a.get("notes", "") or ""))
                msg += f"{enabled} @{handle_name} | Trust: {trust} | {notes}\n"
            send_telegram_message(msg)
        return True

    if text in ["/xposts", "/xhistory", "/xlog"]:
        posts = load_x_posts().get("posts", [])[-10:]
        if not posts:
            send_telegram_message(t("x_no_posts"))
        else:
            msg = "<b>📜 Last 10 Tracked X Posts:</b>\n\n"
            for p in reversed(posts):
                ts = p.get("timestamp", "")[:16].replace("T", " ")
                rec = p.get("action", "IGNORE")
                emoji = "🟢" if rec == "BUY" else "🔴" if rec == "SELL" else "📋" if rec == "ADD_TO_WATCHLIST" else "⏸️"
                raw_src = p.get("raw_tweet", "—") or "—"
                raw = raw_src[:80] + "..." if len(raw_src) > 80 else raw_src
                raw = escape(str(raw))
                account = escape(str(p.get("account") or ""))
                coin = escape(str(p.get("coin") or ""))
                rat = escape(str(p.get("rationale", "") or "")[:80])
                msg += (
                    f"{emoji} {ts} | @{account} | {rec} {coin} | {p.get('confidence')}% \n"
                    f"Raw: {raw}\nRationale: {rat}...\n\n"
                )
            send_telegram_message(msg)
        return True

    if text in ["/xsignals", "/xsignal"]:
        analyzer = XAnalyzer()
        signals = analyzer.get_top_signals()
        if not signals:
            send_telegram_message(t("x_no_signals"))
        else:
            msg = "<b>📡 Latest X Signals:</b>\n\n"
            for s in signals[:8]:
                account = escape(str(getattr(s, "account", "") or ""))
                coin = escape(str(getattr(s, "coin", "") or ""))
                rat = escape(str(getattr(s, "rationale", "") or "")[:80])
                msg += (
                    f"@{account} | {s.action} {coin} | {s.confidence}% | "
                    f"Score: {s.score:.2f}\n{rat}\n\n"
                )
            send_telegram_message(msg)
        return True

    if text in ["/xaccuracy", "/xleaderboard"]:
        tracker = AccuracyTracker()
        board = tracker.get_leaderboard()
        if not board:
            send_telegram_message(t("x_no_accuracy"))
        else:
            msg = "<b>📊 X Account Accuracy Leaderboard:</b>\n\n"
            for i, row in enumerate(board, 1):
                status = "🟢" if row["enabled"] else "🔴"
                msg += (
                    f"{i}. {status} @{row['handle']} | Trust: {row['trust_score']} | "
                    f"Hit: {row['hit_rate']*100:.0f}% ({row['samples']} samples) | "
                    f"Avg 24h: {row['avg_return_24h']:+.1f}%\n"
                )
            send_telegram_message(msg)
        return True

    if text == "/tracktest":
        analyzer = XAnalyzer()
        orchestrator = SignalOrchestrator()
        test_tweet = "SOL looking very strong on the weekly chart. Breaking resistance with good volume. Long bias."
        recommendation = analyzer.track_and_recommend(test_tweet, "TestAccount", 0.05, orchestrator=orchestrator)
        recommendation["post_id"] = f"tracktest_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        analyzer.log_tracked_post(recommendation)
        send_x_recommendation_message(recommendation)
        return True

    if text == "/testaccount":
        activate_command("testaccount")
        send_telegram_message(hint("testaccount"))
        return True

    if text.startswith("/testaccount "):
        handle, days = _parse_testaccount_args(text)
        if not handle:
            send_telegram_message(hint("testaccount"))
            return True
        chat_id = current_chat_id() or os.getenv("TELEGRAM_CHAT_ID", "")
        job_id, err = heavy_job_queue.enqueue(
            "testaccount",
            chat_id,
            lambda: _run_backtest(handle, days),
            params={"handle": handle, "days": days},
            ttl_minutes=90,
        )
        if err:
            send_telegram_message(err)
            return True
        send_telegram_message(
            t("x_backtest_started", handle=handle, days=days, job_id=job_id)
        )
        return True

    return False