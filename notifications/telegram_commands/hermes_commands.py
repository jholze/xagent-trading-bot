from hermes.agent import HermesAgent
from notifications.telegram_commands.usage_hints import hint
from telegram_notifier import send_telegram_message


def handle(text: str) -> bool:
    if text in ("/hermes", "/hermes_status"):
        agent = HermesAgent()
        send_telegram_message(f"<pre>{agent.status()}</pre>")
        return True

    if text == "/hermes_run":
        send_telegram_message("🔄 Hermes learning cycle started...")
        try:
            result = HermesAgent().run_cycle()
            send_telegram_message(
                f"🧠 <b>Hermes cycle done</b>\n"
                f"{result.summary}\n"
                f"Verdict: {result.verdict}"
            )
        except Exception as e:
            send_telegram_message(f"❌ Hermes cycle failed: {e}")
        return True

    if text.startswith("/hermes"):
        send_telegram_message(hint("hermes"))
        return True

    return False