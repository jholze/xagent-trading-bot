import threading

from core.config import get_bot_config
from core.tenant_context import tenant_context, tenant_snapshot
from logger import DECISIONS_LOG_FILE
from notifications.telegram_commands.command_context import (
    activate_command,
    cancel_keyboard,
    current_chat_id,
    set_chat_id,
)
from notifications.telegram_commands.menu_i18n import (
    _command_entry,
    _pack,
    current_language,
    set_user_language,
)
from notifications.telegram_i18n import t
from notifications.user_explain import explain_rationale, explanations_config, format_decision_entry
from services.observability_store import tail_jsonl
from strategies.registry import resolve_coin_config
from telegram_notifier import (
    answer_callback_query,
    send_telegram_buttons,
    send_telegram_message,
)

WHY_SYM_CALLBACK_PREFIX = "whysym:"
_WHY_PICK_LIMIT = 8


def _load_decisions(limit: int = 200) -> list[dict]:
    return tail_jsonl(DECISIONS_LOG_FILE, limit)


def _normalize_symbol(symbol_filter: str) -> str:
    sym = (symbol_filter or "").upper().strip()
    if "/" not in sym:
        sym = f"{sym}/USDT"
    return sym


def _find_latest_decision(symbol: str, entries: list[dict]) -> dict | None:
    target = _normalize_symbol(symbol)
    for entry in reversed(entries):
        if (entry.get("symbol") or "").upper() == target:
            return entry
    return None


def _format_why_message(symbol_filter: str, entries: list[dict], cfg) -> str:
    sym = _normalize_symbol(symbol_filter)
    match = _find_latest_decision(sym, entries)
    coin_cfg = resolve_coin_config({"symbol": sym})
    sp = coin_cfg.get("strategy_params") or {}

    from notifications.coin_links import format_links_line, format_ticker_html

    sym_html = format_ticker_html(sym.replace("/USDT", ""), symbol_suffix="/USDT")
    links = format_links_line(sym.replace("/USDT", ""))
    lines = [t("why_title", sym=sym_html), ""]
    if links:
        lines.append(links)
        lines.append("")
    if match:
        lines.append(
            t(
                "why_last_decision",
                action=match.get("action"),
                normalized=match.get("normalized_action", ""),
            )
        )
        lines.append(t("why_reason", text=explain_rationale(match.get("rationale", ""))))
        if match.get("rationale") and explanations_config(cfg).get("show_technical_codes", True):
            lines.append(f"<code>{match['rationale']}</code>")
        if match.get("trade_message") and not match.get("executed"):
            lines.append(t("why_blocked", msg=match["trade_message"]))
        lines.append(f"<i>{(match.get('timestamp') or '')[:16]}</i>")
    else:
        lines.append(t("why_no_decision"))

    if sp.get("hermes_experiment_id"):
        lines.append("")
        lines.append(t("why_hermes", id=sp["hermes_experiment_id"]))
        if sp.get("hermes_updated_at"):
            lines.append(t("why_hermes_updated", ts=sp["hermes_updated_at"]))

    return "\n".join(lines)


def _build_why(
    symbol_filter: str,
    chat_id: str,
    *,
    tenant_id: str,
    scope: str,
    owner_chat_id: str,
    lang: str,
) -> None:
    try:
        set_user_language(lang)
        with tenant_context(tenant_id, scope=scope, owner_chat_id=owner_chat_id):
            cfg = get_bot_config()
            if not cfg.decisions_audit_enabled:
                send_telegram_message(t("why_audit_off"), chat_id=chat_id or None)
                return
            message = _format_why_message(symbol_filter, _load_decisions(100), cfg)
            send_telegram_message(message, chat_id=chat_id or None)
    except Exception as e:
        set_user_language(lang)
        send_telegram_message(
            t("why_load_failed", error=e),
            chat_id=chat_id or None,
        )


def _why_menu_text(field: str) -> str:
    return str(_command_entry(_pack(current_language()), "why").get(field) or "")


def _why_ticker(raw: str) -> str:
    return (raw or "").upper().replace("/USDT", "").strip()


def _why_pick_symbols() -> list[str]:
    """Recent decision symbols, then open positions — tappable /why targets (#449)."""
    seen: list[str] = []
    seen_set: set[str] = set()

    def _add(raw: str) -> None:
        ticker = _why_ticker(raw)
        if ticker and ticker not in seen_set:
            seen_set.add(ticker)
            seen.append(ticker)

    for entry in reversed(_load_decisions(100)):
        _add(str(entry.get("symbol") or ""))
        if len(seen) >= _WHY_PICK_LIMIT:
            return seen
    try:
        from notifications.telegram_commands.position_display import position_symbol
        from strategies.positions import list_active_positions

        for pos in list_active_positions() or []:
            _add(position_symbol(pos))
            if len(seen) >= _WHY_PICK_LIMIT:
                break
    except Exception:
        pass
    return seen


def _why_pick_button_rows(tickers: list[str]) -> list[list[dict]]:
    rows: list[list[dict]] = []
    row: list[dict] = []
    for ticker in tickers:
        row.append({"text": ticker, "callback_data": f"{WHY_SYM_CALLBACK_PREFIX}{ticker}"})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.extend(cancel_keyboard())
    return rows


def _offer_why_picker() -> bool:
    """``/why`` without args: tappable recent symbols, not only a usage string."""
    activate_command("why")
    tickers = _why_pick_symbols()
    if not tickers:
        send_telegram_message(t("why_usage"))
        return True
    prompt = _why_menu_text("pick_prompt") or t("why_usage")
    send_telegram_buttons(prompt, _why_pick_button_rows(tickers))
    return True


def handle_callback(callback_query: dict) -> bool:
    data = str((callback_query or {}).get("data") or "")
    if not data.startswith(WHY_SYM_CALLBACK_PREFIX):
        return False
    callback_id = (callback_query or {}).get("id")
    if callback_id:
        answer_callback_query(callback_id)
    message = (callback_query or {}).get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id:
        set_chat_id(chat_id)
    ticker = _why_ticker(data[len(WHY_SYM_CALLBACK_PREFIX):])
    if not ticker:
        send_telegram_message(t("why_usage"))
        return True
    return _dispatch_why_async(ticker)


def _dispatch_why_async(symbol_filter: str) -> bool:
    chat_id = current_chat_id()
    tenant_id, scope, owner_chat_id = tenant_snapshot()
    lang = current_language()
    send_telegram_message(t("loading_why"), chat_id=chat_id or None)
    threading.Thread(
        target=_build_why,
        args=(symbol_filter, chat_id),
        kwargs={
            "tenant_id": tenant_id,
            "scope": scope,
            "owner_chat_id": owner_chat_id,
            "lang": lang,
        },
        daemon=True,
        name="why-cmd",
    ).start()
    return True


def _build_decisions_list(
    chat_id: str, *, tenant_id: str, scope: str, owner_chat_id: str, lang: str
) -> None:
    try:
        set_user_language(lang)
        with tenant_context(tenant_id, scope=scope, owner_chat_id=owner_chat_id):
            cfg = get_bot_config()
            if not cfg.decisions_audit_enabled:
                send_telegram_message(t("why_audit_off"), chat_id=chat_id or None)
                return

            entries = _load_decisions(50)
            if not entries:
                send_telegram_message(t("decisions_empty"), chat_id=chat_id or None)
                return

            show_tech = explanations_config(cfg).get("show_technical_codes", True)
            lines = [t("decisions_title"), ""]
            for entry in reversed(entries[-8:]):
                lines.append(format_decision_entry(entry, show_technical=show_tech))
                lines.append("")

            lines.append(t("decisions_footer"))
            send_telegram_message("\n".join(lines).strip(), chat_id=chat_id or None)
    except Exception as e:
        set_user_language(lang)
        send_telegram_message(
            t("decisions_load_failed", error=e),
            chat_id=chat_id or None,
        )


def handle(text: str) -> bool:
    parts = text.strip().split()
    cmd = parts[0].lower() if parts else ""

    if cmd == "/why":
        if len(parts) < 2:
            return _offer_why_picker()
        return _dispatch_why_async(parts[1])

    if cmd not in ("/decisions", "/decision"):
        return False

    if len(parts) > 1 and parts[1].lower() not in ("help", "?"):
        return _dispatch_why_async(parts[1])

    chat_id = current_chat_id()
    tenant_id, scope, owner_chat_id = tenant_snapshot()
    lang = current_language()
    send_telegram_message(t("loading_decisions"), chat_id=chat_id or None)
    threading.Thread(
        target=_build_decisions_list,
        args=(chat_id,),
        kwargs={
            "tenant_id": tenant_id,
            "scope": scope,
            "owner_chat_id": owner_chat_id,
            "lang": lang,
        },
        daemon=True,
        name="decisions-cmd",
    ).start()
    return True
