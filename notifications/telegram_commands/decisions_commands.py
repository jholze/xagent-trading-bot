import threading

from core.config import get_bot_config
from core.tenant_context import tenant_context, tenant_snapshot
from logger import DECISIONS_LOG_FILE
from notifications.telegram_commands.command_context import activate_command, current_chat_id
from notifications.user_explain import explain_rationale, explanations_config, format_decision_entry
from services.observability_store import tail_jsonl
from strategies.registry import resolve_coin_config
from telegram_notifier import send_telegram_message


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
    lines = [f"<b>❓ Warum — {sym_html}</b>", ""]
    if links:
        lines.append(links)
        lines.append("")
    if match:
        lines.append(f"<b>Letzte Entscheidung:</b> {match.get('action')} ({match.get('normalized_action', '')})")
        lines.append(f"<b>Warum:</b> {explain_rationale(match.get('rationale', ''))}")
        if match.get("rationale") and explanations_config(cfg).get("show_technical_codes", True):
            lines.append(f"<code>{match['rationale']}</code>")
        if match.get("trade_message") and not match.get("executed"):
            lines.append(f"<i>Blockiert: {match['trade_message']}</i>")
        lines.append(f"<i>{(match.get('timestamp') or '')[:16]}</i>")
    else:
        lines.append("Keine gespeicherte Entscheidung für diesen Coin.")

    if sp.get("hermes_experiment_id"):
        lines.append("")
        lines.append(f"<b>Hermes:</b> Experiment <code>{sp['hermes_experiment_id']}</code>")
        if sp.get("hermes_updated_at"):
            lines.append(f"Aktualisiert: {sp['hermes_updated_at']}")

    return "\n".join(lines)


def _build_why(
    symbol_filter: str,
    chat_id: str,
    *,
    tenant_id: str,
    scope: str,
    owner_chat_id: str,
) -> None:
    try:
        with tenant_context(tenant_id, scope=scope, owner_chat_id=owner_chat_id):
            cfg = get_bot_config()
            if not cfg.decisions_audit_enabled:
                send_telegram_message(
                    "Entscheidungs-Protokoll ist deaktiviert (observability.decisions_audit).",
                    chat_id=chat_id or None,
                )
                return
            message = _format_why_message(symbol_filter, _load_decisions(100), cfg)
            send_telegram_message(message, chat_id=chat_id or None)
    except Exception as e:
        send_telegram_message(
            f"❌ Warum-Antwort konnte nicht geladen werden: {e}",
            chat_id=chat_id or None,
        )


def _dispatch_why_async(symbol_filter: str) -> bool:
    chat_id = current_chat_id()
    tenant_id, scope, owner_chat_id = tenant_snapshot()
    send_telegram_message("⏳ <b>Warum</b> wird geladen…", chat_id=chat_id or None)
    threading.Thread(
        target=_build_why,
        args=(symbol_filter, chat_id),
        kwargs={
            "tenant_id": tenant_id,
            "scope": scope,
            "owner_chat_id": owner_chat_id,
        },
        daemon=True,
        name="why-cmd",
    ).start()
    return True


def _build_decisions_list(chat_id: str, *, tenant_id: str, scope: str, owner_chat_id: str) -> None:
    try:
        with tenant_context(tenant_id, scope=scope, owner_chat_id=owner_chat_id):
            cfg = get_bot_config()
            if not cfg.decisions_audit_enabled:
                send_telegram_message(
                    "Entscheidungs-Protokoll ist deaktiviert (observability.decisions_audit).",
                    chat_id=chat_id or None,
                )
                return

            entries = _load_decisions(50)
            if not entries:
                send_telegram_message(
                    "Noch keine Entscheidungen protokolliert.\n"
                    "<i>Der Bot schreibt ab jetzt nach <code>logs/decisions.jsonl</code>.</i>",
                    chat_id=chat_id or None,
                )
                return

            show_tech = explanations_config(cfg).get("show_technical_codes", True)
            lines = ["<b>📜 Letzte Bot-Entscheidungen</b>", ""]
            for entry in reversed(entries[-8:]):
                lines.append(format_decision_entry(entry, show_technical=show_tech))
                lines.append("")

            lines.append("<i>Filter: <code>/why SYMBOL</code> · <code>/decisions SYMBOL</code></i>")
            send_telegram_message("\n".join(lines).strip(), chat_id=chat_id or None)
    except Exception as e:
        send_telegram_message(
            f"❌ Entscheidungen konnten nicht geladen werden: {e}",
            chat_id=chat_id or None,
        )


def handle(text: str) -> bool:
    parts = text.strip().split()
    cmd = parts[0].lower() if parts else ""

    if cmd == "/why":
        if len(parts) < 2:
            activate_command("why")
            send_telegram_message(
                "❌ <b>/why</b> — Erklärung zur letzten Bot-Entscheidung für einen Coin\n\n"
                "Beispiel: <code>H</code> oder <code>/why H</code>\n"
                "<i>Nach <code>/why</code> reicht das Symbol allein.</i>"
            )
            return True
        return _dispatch_why_async(parts[1])

    if cmd not in ("/decisions", "/decision"):
        return False

    if len(parts) > 1 and parts[1].lower() not in ("help", "?"):
        return _dispatch_why_async(parts[1])

    chat_id = current_chat_id()
    tenant_id, scope, owner_chat_id = tenant_snapshot()
    send_telegram_message("⏳ <b>Entscheidungen</b> werden geladen…", chat_id=chat_id or None)
    threading.Thread(
        target=_build_decisions_list,
        args=(chat_id,),
        kwargs={
            "tenant_id": tenant_id,
            "scope": scope,
            "owner_chat_id": owner_chat_id,
        },
        daemon=True,
        name="decisions-cmd",
    ).start()
    return True