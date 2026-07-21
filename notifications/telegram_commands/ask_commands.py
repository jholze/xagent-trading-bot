import os

from notifications.telegram_commands.command_context import activate_command, current_chat_id
from notifications.telegram_commands.usage_hints import hint
from services.telegram_ask_bridge import enqueue_question
from telegram_notifier import send_telegram_message


def handle(text: str) -> bool:
    if not text.startswith("/ask"):
        return False

    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        activate_command("ask")
        send_telegram_message(hint("ask"))
        return True

    question = parts[1].strip()
    # Multi-tenant: answer must go to the chat that asked (Henry ≠ operator).
    chat_id = str(current_chat_id() or "").strip() or os.getenv("TELEGRAM_CHAT_ID", "")
    qid, err = enqueue_question(chat_id, question)
    if err:
        send_telegram_message(f"❌ {err}", chat_id=chat_id or None)
        return True

    send_telegram_message(
        f"⏳ <b>Frage #{qid} eingereiht</b>\n\n"
        f"<i>{question}</i>\n\n"
        "🔄 Antwort kommt in ca. 30–90s (Grok-Fallback, falls kein Cursor).",
        chat_id=chat_id or None,
    )
    return True