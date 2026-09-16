from logger import log
from notifications.telegram_commands import ask_commands, backtest_commands, cmc_commands, config_commands, decisions_commands, diag_commands, gate_commands, grid_commands, help_commands, hermes_commands, lc_commands, lock_commands, menu_commands, mode_commands, morning_commands, onboarding_commands, order_commands, pause_commands, plan_commands, portfolio_commands, reload_commands, replay_commands, reporting_commands, risk_commands, sandbox_commands, short_commands, stack_commands, tenant_link_commands, trading_commands, watchlist_commands, x_commands, xai_auth_commands
from notifications.telegram_commands.usage_hints import hint
from telegram_notifier import answer_callback_query, send_telegram_message

_HANDLERS = [
    tenant_link_commands.handle,
    onboarding_commands.handle,
    mode_commands.handle,
    pause_commands.handle,
    reload_commands.handle,
    config_commands.handle,
    plan_commands.handle,
    reporting_commands.handle,
    gate_commands.handle,
    risk_commands.handle,
    lock_commands.handle,
    short_commands.handle,
    sandbox_commands.handle,
    hermes_commands.handle,
    ask_commands.handle,
    decisions_commands.handle,
    grid_commands.handle,
    backtest_commands.handle,
    replay_commands.handle,
    cmc_commands.handle,
    lc_commands.handle,
    watchlist_commands.handle,
    trading_commands.handle,
    order_commands.handle,
    x_commands.handle,
    diag_commands.handle,
    xai_auth_commands.handle,
    portfolio_commands.handle,
    morning_commands.handle,
    stack_commands.handle,
    menu_commands.handle,
    help_commands.handle,
]

# #451: commands only the operator chat may run. Hiding a command from the
# satellite menu (#398) is not a gate — the typed path reached every handler.
# Entries are command names without the leading slash; a two-word entry gates
# a specific sub-command ("help onboarding"). Checked in dispatch_command AND
# dispatch_callback (menu:run:<key>, panic_*, testaccount_*), so the inline and
# the typed path share one allow/deny decision.
# /onboard and /xai_login self-gate inside their handlers (own messages).
# panic is denied for satellites here (typed hole); keyboard placement is #448.
OPERATOR_ONLY: frozenset[str] = frozenset({
    "live_confirm", "live_cancel",
    "hermes_run", "hermes_veto", "hermes_rollback",
    "sandbox", "sandbox_results", "sandbox_promote",
    "backtest", "backtest_lock", "backtest_results",
    "testaccount", "tracktest",
    "reload", "hotreload",
    "diag",
    "wqe", "wqe_soak", "wqescores", "watchlist_quality",
    "churn_replay", "counterfactual", "session_cancel",
    "panic",
    "config", "config revert",
    "help onboarding", "help onboard", "help onb", "commands onboarding", "? onboarding",
})

# Inline callbacks whose flow belongs to an OPERATOR_ONLY command.
_OPERATOR_ONLY_CALLBACK_PREFIXES: tuple[str, ...] = ("panic_", "testaccount_", "config_")


def _is_operator_only_text(text: str) -> bool:
    if not text.startswith("/"):
        return False
    parts = text[1:].lower().split()
    if not parts:
        return False
    if parts[0] in OPERATOR_ONLY:
        return True
    return len(parts) > 1 and f"{parts[0]} {parts[1]}" in OPERATOR_ONLY


def _is_operator_only_callback(data: str) -> bool:
    if data.startswith("menu:run:"):
        return data.split(":", 2)[2].strip().lower() in OPERATOR_ONLY
    return data.startswith(_OPERATOR_ONLY_CALLBACK_PREFIXES)


def _is_operator(chat_id=None) -> bool:
    """Operator = full role per menu_commands.menu_role_for (single-tenant →
    always operator; multi-tenant → only TELEGRAM_CHAT_ID, fail closed)."""
    return menu_commands.menu_role_for(chat_id=chat_id) == "operator"


def _deny_text() -> str:
    from notifications.telegram_i18n import t

    return t("command_operator_only")


def _strip_bot_suffix(text: str) -> str:
    """Telegram may send `/short@BotName` from the slash picker."""
    if not text.startswith("/"):
        return text
    head, sep, tail = text.partition(" ")
    if "@" in head:
        head = head.split("@", 1)[0]
    return f"{head} {tail}".strip() if sep else head


def dispatch_command(text: str) -> bool:
    if not isinstance(text, str):
        return False
    text = _strip_bot_suffix(text.strip())
    log(f"[DEBUG] Empfangener Befehl: '{text}'", "DEBUG")

    try:
        if _is_operator_only_text(text) and not _is_operator():
            log(f"Denied operator-only command '{text.split()[0]}' for non-operator chat", "WARNING")
            send_telegram_message(_deny_text())
            return True
        for handler in _HANDLERS:
            if handler(text):
                return True
        if text.startswith("/"):
            send_telegram_message(hint("unknown"))
            return True
        return False
    except Exception as e:
        log(f"Error in dispatch_command for '{text}': {e}", "ERROR")
        try:
            from notifications.telegram_i18n import t

            send_telegram_message(t("error_command"))
        except Exception:
            pass
        return True


def dispatch_callback(callback_query: dict) -> bool:
    try:
        data = str((callback_query or {}).get("data") or "").strip()
        if _is_operator_only_callback(data):
            chat_id = ((callback_query.get("message") or {}).get("chat") or {}).get("id")
            # No chat on the callback (inline mode / stale) → fail closed.
            if chat_id is None or not _is_operator(chat_id):
                log(f"Denied operator-only callback '{data}' for non-operator chat", "WARNING")
                callback_id = callback_query.get("id")
                if callback_id:
                    answer_callback_query(callback_id, _deny_text())
                return True
        if menu_commands.handle_callback(callback_query):
            return True
        if pause_commands.handle_callback(callback_query):
            return True
        if config_commands.handle_callback(callback_query):
            return True
        if trading_commands.handle_callback(callback_query):
            return True
        if order_commands.handle_callback(callback_query):
            return True
        return x_commands.handle_callback(callback_query)
    except Exception as e:
        log(f"Error in dispatch_callback: {e}", "ERROR")
        return True