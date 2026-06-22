import os

from bus.jobs import heavy_job_queue
from hermes.agent import HermesAgent
from notifications.telegram_commands.command_context import current_chat_id
from hermes.memory import store
from notifications.telegram_commands.usage_hints import hint
from notifications.user_explain import explain_hermes_cycle
from telegram_notifier import send_telegram_message


def _run_hermes_cycle() -> None:
    try:
        result = HermesAgent().run_cycle()
        send_telegram_message(
            f"🧠 <b>Hermes cycle done</b>\n"
            f"{result.summary}\n"
            f"Verdict: {result.verdict}"
        )
    except Exception as e:
        send_telegram_message(f"❌ Hermes cycle failed: {e}")


def handle(text: str) -> bool:
    if text == "/hermes_last":
        recent = store.recent_experiments(1)
        if not recent:
            send_telegram_message("Noch keine Hermes-Experimente vorhanden.")
            return True
        send_telegram_message(f"🧠 <b>Hermes — letzter Zyklus</b>\n{explain_hermes_cycle(recent[0])}")
        return True

    if text in ("/hermes", "/hermes_status"):
        agent = HermesAgent()
        send_telegram_message(f"<pre>{agent.status()}</pre>")
        recent = store.recent_experiments(1)
        if recent:
            send_telegram_message(f"🧠 <b>In Klartext:</b>\n{explain_hermes_cycle(recent[0])}")
        return True

    if text == "/hermes_run":
        chat_id = current_chat_id() or os.getenv("TELEGRAM_CHAT_ID", "")
        job_id, err = heavy_job_queue.enqueue(
            "hermes_run",
            chat_id,
            _run_hermes_cycle,
            ttl_minutes=120,
        )
        if err:
            send_telegram_message(err)
            return True
        send_telegram_message(f"🔄 Hermes learning cycle started (Job {job_id})…")
        return True

    if text.startswith("/hermes"):
        send_telegram_message(hint("hermes"))
        return True

    return False