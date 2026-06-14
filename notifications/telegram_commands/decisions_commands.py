import json
import os

from core.config import get_bot_config
from logger import DECISIONS_LOG_FILE
from notifications.telegram_commands.usage_hints import hint
from notifications.user_explain import explain_rationale, explanations_config, format_decision_entry
from strategies.registry import resolve_coin_config
from telegram_notifier import send_telegram_message


def _load_decisions(limit: int = 200) -> list[dict]:
    path = DECISIONS_LOG_FILE
    if not os.path.isfile(path):
        return []
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries[-limit:]


def _handle_why(symbol_filter: str) -> bool:
    cfg = get_bot_config()
    entries = _load_decisions(100)
    sym = symbol_filter.upper()
    if "/" not in sym:
        sym = f"{sym}/USDT"

    match = None
    for entry in reversed(entries):
        if (entry.get("symbol") or "").upper() == sym:
            match = entry
            break

    coin_cfg = resolve_coin_config({"symbol": sym})
    sp = coin_cfg.get("strategy_params") or {}

    lines = [f"<b>❓ Warum — {sym}</b>", ""]
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

    send_telegram_message("\n".join(lines))
    return True


def handle(text: str) -> bool:
    parts = text.strip().split()
    cmd = parts[0].lower() if parts else ""

    if cmd == "/why":
        if len(parts) < 2:
            send_telegram_message(
                "❌ <b>/why</b> — Erklärung zur letzten Bot-Entscheidung für einen Coin\n\n"
                "Beispiel: <code>/why H</code> oder <code>/why ARIA/USDT</code>"
            )
            return True
        return _handle_why(parts[1])

    if cmd not in ("/decisions", "/decision"):
        return False

    if len(parts) > 1 and parts[1].lower() not in ("help", "?"):
        return _handle_why(parts[1])

    cfg = get_bot_config()
    if not cfg.decisions_audit_enabled:
        send_telegram_message("Entscheidungs-Protokoll ist deaktiviert (observability.decisions_audit).")
        return True

    entries = _load_decisions(50)
    if not entries:
        send_telegram_message(
            "Noch keine Entscheidungen protokolliert.\n"
            "<i>Der Bot schreibt ab jetzt nach <code>logs/decisions.jsonl</code>.</i>"
        )
        return True

    show_tech = explanations_config(cfg).get("show_technical_codes", True)
    lines = ["<b>📜 Letzte Bot-Entscheidungen</b>", ""]
    for entry in reversed(entries[-8:]):
        lines.append(format_decision_entry(entry, show_technical=show_tech))
        lines.append("")

    lines.append("<i>Filter: <code>/why SYMBOL</code> · <code>/decisions SYMBOL</code></i>")
    send_telegram_message("\n".join(lines).strip())
    return True