from datetime import datetime

from data_manager import load_x_accounts, load_x_posts, save_x_accounts
from notifications.telegram_commands.usage_hints import hint
from intelligence.accuracy_tracker import AccuracyTracker
from services.signal_orchestrator import SignalOrchestrator
from telegram_notifier import send_telegram_message, send_x_recommendation_message
from x_analyzer import XAnalyzer


def handle(text: str) -> bool:
    if text == "/addx":
        send_telegram_message(hint("addx"))
        return True

    if text == "/removex":
        send_telegram_message(hint("removex"))
        return True

    if text.startswith("/addx "):
        handle_name = text[6:].strip().replace("@", "").strip()
        if not handle_name:
            send_telegram_message(hint("addx"))
            return True
        accounts = load_x_accounts()
        if not any(a.get("handle", a) == handle_name for a in accounts):
            accounts.append({"handle": handle_name, "trust_score": 70, "enabled": True, "notes": "Added via Telegram"})
            if save_x_accounts(accounts):
                send_telegram_message(f"✅ Added @{handle_name} to monitored X accounts.")
            else:
                send_telegram_message("❌ Failed to save x_accounts.json.")
        else:
            send_telegram_message(f"@{handle_name} is already monitored.")
        return True

    if text.startswith("/removex "):
        handle_name = text[8:].strip().replace("@", "").strip()
        if not handle_name:
            send_telegram_message(hint("removex"))
            return True
        accounts = load_x_accounts()
        new_accounts = [a for a in accounts if a.get("handle", a) != handle_name]
        if len(new_accounts) != len(accounts):
            if save_x_accounts(new_accounts):
                send_telegram_message(f"✅ Removed @{handle_name} from X accounts.")
            else:
                send_telegram_message("❌ Failed to save x_accounts.json.")
        else:
            send_telegram_message(f"@{handle_name} not found.")
        return True

    if text in ["/listx", "/xaccounts", "/xlist"]:
        accounts = load_x_accounts()
        if not accounts:
            send_telegram_message("No X accounts configured.")
        else:
            msg = "<b>📋 Monitored X Accounts:</b>\n\n"
            for a in accounts:
                handle_name = a.get("handle", a)
                trust = a.get("trust_score", 70)
                enabled = "🟢" if a.get("enabled", True) else "🔴"
                msg += f"{enabled} @{handle_name} | Trust: {trust} | {a.get('notes', '')}\n"
            send_telegram_message(msg)
        return True

    if text in ["/xposts", "/xhistory", "/xlog"]:
        posts = load_x_posts().get("posts", [])[-10:]
        if not posts:
            send_telegram_message("No tracked X posts yet.")
        else:
            msg = "<b>📜 Last 10 Tracked X Posts:</b>\n\n"
            for p in reversed(posts):
                ts = p.get("timestamp", "")[:16].replace("T", " ")
                rec = p.get("action", "IGNORE")
                emoji = "🟢" if rec == "BUY" else "🔴" if rec == "SELL" else "📋" if rec == "ADD_TO_WATCHLIST" else "⏸️"
                raw = p.get("raw_tweet", "—")[:80] + "..." if len(p.get("raw_tweet", "")) > 80 else p.get("raw_tweet", "—")
                msg += f"{emoji} {ts} | @{p.get('account')} | {rec} {p.get('coin')} | {p.get('confidence')}% \nRaw: {raw}\nRationale: {p.get('rationale', '')[:80]}...\n\n"
            send_telegram_message(msg)
        return True

    if text in ["/xsignals", "/xsignal"]:
        analyzer = XAnalyzer()
        signals = analyzer.get_top_signals()
        if not signals:
            send_telegram_message("No strong X signals right now.")
        else:
            msg = "<b>📡 Latest X Signals:</b>\n\n"
            for s in signals[:8]:
                msg += f"@{s.account} | {s.action} {s.coin} | {s.confidence}% | Score: {s.score:.2f}\n{s.rationale[:80]}\n\n"
            send_telegram_message(msg)
        return True

    if text in ["/xaccuracy", "/xleaderboard"]:
        tracker = AccuracyTracker()
        board = tracker.get_leaderboard()
        if not board:
            send_telegram_message("No X account accuracy data yet.")
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

    return False