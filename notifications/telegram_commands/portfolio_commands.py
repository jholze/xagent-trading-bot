import threading

from core.interactive_priority import interactive_priority
from core.tenant_context import tenant_context, tenant_snapshot
from notifications.telegram_commands.command_context import current_chat_id
from notifications.telegram_commands.menu_i18n import current_language, set_user_language
from notifications.telegram_commands.position_display import send_positions_snapshot
from notifications.telegram_i18n import t
from telegram_notifier import send_telegram_message

_COMPACT_COMMANDS = {"/positions", "/portfolio", "/status", "/balance"}
_FULL_COMMANDS = {
    "/positions full",
    "/positions detail",
    "/positions_full",
    "/portfolio full",
    "/portfolio detail",
}


def _is_mongo_client_closed_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "after close" in msg or (
        exc.__class__.__name__ == "InvalidOperation" and "mongoclient" in msg
    )


def _build_positions(
    chat_id: str,
    *,
    detail_level: str,
    tenant_id: str,
    scope: str,
    owner_chat_id: str,
    lang: str,
):
    set_user_language(lang)
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            with tenant_context(tenant_id, scope=scope, owner_chat_id=owner_chat_id):
                send_positions_snapshot(
                    fast=True,
                    chat_id=chat_id or None,
                    detail_level=detail_level,
                    tenant_id=tenant_id,
                    scope=scope,
                )
            return
        except Exception as e:
            last_err = e
            if attempt == 0 and _is_mongo_client_closed_error(e):
                # Shared client was closed mid-request — drop + reopen, retry once.
                try:
                    from storage.mongo_client import close_client

                    close_client()
                except Exception:
                    pass
                continue
            break
    set_user_language(lang)
    send_telegram_message(
        t("portfolio_load_failed", error=last_err),
        chat_id=chat_id or None,
    )


def handle(text: str) -> bool:
    if text in _COMPACT_COMMANDS:
        detail_level = "compact"
        loading = t("portfolio_loading_compact")
    elif text in _FULL_COMMANDS:
        detail_level = "full"
        loading = t("portfolio_loading_full")
    else:
        return False

    chat_id = current_chat_id()
    tenant_id, scope, owner_chat_id = tenant_snapshot()
    lang = current_language()
    send_telegram_message(loading, chat_id=chat_id or None)
    # Raise the flag before the worker starts so eval/cycle yield immediately.
    token = interactive_priority()
    token.__enter__()

    def _run():
        try:
            _build_positions(
                chat_id,
                detail_level=detail_level,
                tenant_id=tenant_id,
                scope=scope,
                owner_chat_id=owner_chat_id,
                lang=lang,
            )
        finally:
            token.__exit__(None, None, None)

    try:
        threading.Thread(
            target=_run,
            daemon=True,
            name="positions-cmd",
        ).start()
    except Exception:
        token.__exit__(None, None, None)
        raise
    return True
