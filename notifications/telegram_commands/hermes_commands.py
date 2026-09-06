import os

from bus.jobs import heavy_job_queue
from hermes.agent import HermesAgent
from notifications.telegram_commands.command_context import current_chat_id
from hermes.memory import store
from notifications.telegram_commands.usage_hints import hint
from notifications.telegram_i18n import t
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
        send_telegram_message(t("hermes_failed", error=e))


def handle(text: str) -> bool:
    if text == "/hermes_last":
        recent = store.recent_experiments(1)
        if not recent:
            send_telegram_message(t("hermes_no_experiments"))
            return True
        send_telegram_message(
            t("hermes_last_cycle", text=explain_hermes_cycle(recent[0]))
        )
        return True

    if text in ("/hermes", "/hermes_status"):
        agent = HermesAgent()
        send_telegram_message(f"<pre>{agent.status()}</pre>")
        recent = store.recent_experiments(1)
        if recent:
            send_telegram_message(
                t("hermes_plain", text=explain_hermes_cycle(recent[0]))
            )
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
        send_telegram_message(t("hermes_started", job_id=job_id))
        return True

    if text.startswith("/hermes_veto"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            send_telegram_message(t("hermes_veto_usage"))
            return True
        from hermes.promotion import veto

        result = veto(parts[1].strip())
        if result.get("status") == "not_found":
            send_telegram_message(t("hermes_veto_missing", id=parts[1].strip()))
        else:
            send_telegram_message(t("hermes_veto_ok", id=parts[1].strip()))
        return True

    if text.startswith("/hermes_rollback"):
        parts = text.split(maxsplit=1)
        exp_id = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
        from hermes.agent import HermesAgent
        from hermes.promotion import rollback

        result = rollback(exp_id, agent=HermesAgent())
        if result.get("verdict") == "not_found":
            send_telegram_message(t("hermes_rollback_missing"))
        else:
            send_telegram_message(t("hermes_rollback_ok", id=result.get("experiment_id") or exp_id or ""))
        return True

    if text.startswith("/hermes"):
        send_telegram_message(hint("hermes"))
        return True

    return False
