import os

from core.build_info import format_build_line
from core.config import get_bot_config
from data_manager import is_dry_run_enhanced, reload_config
from execution.gate_adapter import GateExecutionAdapter
from price_fetcher import get_prices_batch
from services.gate_balance import fetch_spot_holdings, format_holdings_lines
from services.trading_service import TradingService
from notifications.telegram_commands.command_context import (
    cancel_keyboard,
    clear_context,
    current_chat_id,
    get_context,
    set_chat_id,
    set_context,
)
from notifications.telegram_i18n import t
from telegram_notifier import answer_callback_query, send_telegram_message

DRYRUN_CALLBACK_PREFIX = "dryrun_ok:"


def _gate_key_status(cfg: dict, adapter: GateExecutionAdapter) -> tuple:
    key_env = cfg.get("api_key_env", "GATE_API_KEY")
    secret_env = cfg.get("api_secret_env", "GATE_API_SECRET")
    has_key = bool(os.getenv(key_env))
    has_secret = bool(os.getenv(secret_env))
    balance = 0.0
    api_hint = ""

    if not has_key or not has_secret:
        api_hint = (
            f"\n⚠️ Keys in <code>.env</code> setzen: <code>{key_env}</code> / <code>{secret_env}</code>"
        )
    else:
        balance = adapter._fetch_usdt_balance()
        err = getattr(adapter, "_last_api_error", "") or ""
        if "INVALID_KEY" in err or "Invalid key" in err:
            api_hint = (
                "\n⚠️ <b>INVALID_KEY</b> — Gate lehnt die API-Keys ab.\n"
                "• Neue Keys im Gate.io Dashboard erstellen (Spot-Trading, Read + Trade)\n"
                "• IP-Whitelist prüfen (leer = alle IPs)\n"
                "• <code>.env</code> aktualisieren und Bot neu starten"
            )
        elif err:
            api_hint = f"\n⚠️ Gate API: <code>{err[:80]}</code>"

    return key_env, secret_env, balance, api_hint, has_key, has_secret


def _gate_section(title: str, cfg: dict, adapter: GateExecutionAdapter, bot_config=None) -> str:
    key_env, secret_env, balance, api_hint, has_key, has_secret = _gate_key_status(cfg, adapter)
    dry = cfg.get("dry_run", True)
    enhanced = bot_config.is_dry_run_enhanced() if bot_config else (
        is_dry_run_enhanced({"trading_mode": "live", "live": cfg})
    )
    balance_line = f"USDT verfügbar: <b>${balance:,.2f}</b>"
    if enhanced:
        from services.gate_balance import fetch_usdt_balance
        sim = fetch_usdt_balance(bot_config or get_bot_config())
        balance_line = (
            f"Simulated USDT: <b>${sim:,.2f}</b>\n"
            f"Gate USDT (API): <b>${balance:,.2f}</b>"
        )

    enhanced_line = "\nEnhanced Dry Run: <b>ON</b>" if enhanced else ""

    return f"""<b>{title}</b>
{key_env}: {'✅ gesetzt' if has_key else '❌ fehlt'}
{secret_env}: {'✅ gesetzt' if has_secret else '❌ fehlt'}
Dry Run: <b>{'ON' if dry else 'OFF'}</b>{enhanced_line}
Max/Trade: ${cfg.get('max_usdt_per_trade', 150):.0f} USDT
{balance_line}{api_hint}"""


def _format_dryrun_status() -> str:
    from services.dry_run_watchlist import DryRunWatchlistSync

    cfg = get_bot_config()
    status = DryRunWatchlistSync(cfg).status()
    if not status.get("enabled"):
        return "Enhanced Dry Run ist <b>OFF</b> (live.dry_run_enhanced in config.json)."

    refreshed = status.get("refreshed_at") or "—"
    return (
        "<b>🧪 Enhanced Dry Run</b>\n\n"
        f"Status: <b>ON</b>\n"
        f"Simulated USDT: <b>${status.get('simulated_balance', 0):,.2f}</b>\n"
        f"Trending Coins: <b>{status.get('trending_count', 0)}</b>\n"
        f"Letzte Sync: <code>{refreshed}</code>\n"
        f"Quelle: <code>{status.get('source') or '—'}</code>"
    )


def _execute_dryrun() -> None:
    reload_config()
    send_telegram_message(_format_dryrun_status())


def _prompt_dryrun_confirm() -> None:
    cid = current_chat_id()
    if cid:
        set_context(cid, "dryrun", state="dryrun_awaiting_confirm")
    send_telegram_message(
        t("dryrun_confirm"),
        reply_markup={
            "inline_keyboard": [
                [
                    {
                        "text": t("dryrun_confirm_btn_ok"),
                        "callback_data": DRYRUN_CALLBACK_PREFIX,
                    }
                ],
                cancel_keyboard()[0],
            ]
        },
    )


def _callback_chat_id(callback_query: dict, log_label: str):
    from logger import log

    callback_id = (callback_query or {}).get("id")
    if callback_id:
        answer_callback_query(callback_id)
    chat_id = ((callback_query or {}).get("message") or {}).get("chat") or {}
    chat_id = chat_id.get("id") if isinstance(chat_id, dict) else None
    if not chat_id:
        log(f"{log_label} callback missing chat id", "WARNING")
        return None
    set_chat_id(chat_id)
    return chat_id


def handle_callback(callback_query: dict) -> bool:
    data = str((callback_query or {}).get("data") or "")
    if not data.startswith(DRYRUN_CALLBACK_PREFIX):
        return False
    chat_id = _callback_chat_id(callback_query, "dryrun_ok")
    if not chat_id:
        return True
    entry = get_context(chat_id)
    meta = (entry or {}).get("meta") or {}
    if (
        not entry
        or entry.get("command") != "dryrun"
        or str(meta.get("state") or "") != "dryrun_awaiting_confirm"
    ):
        send_telegram_message(t("dryrun_confirm_expired"))
        return True
    clear_context(chat_id)
    _execute_dryrun()
    return True


def handle(text: str) -> bool:
    if text in ["/dryrun", "/dry_run"]:
        _prompt_dryrun_confirm()
        return True

    if text not in ["/gate", "/gatestatus", "/gate_status", "/gate mainnet"]:
        return False

    reload_config()
    cfg = get_bot_config()
    trading = TradingService(cfg)
    adapter = GateExecutionAdapter(cfg)

    from data_manager import is_demo_mode, resolve_ledger_backend, resolve_ledger_scope
    from storage.mongo_client import (
        is_local_mongo_uri,
        mongo_uri_host,
        resolve_database_name,
        resolve_mongo_uri,
    )

    msg = (
        f"<b>🔗 Gate.io Status</b>\n\n"
        f"<b>Bot-Modus:</b> {trading.mode_label()}\n"
        f"{format_build_line()}\n"
    )
    if is_demo_mode():
        scope = resolve_ledger_scope()
        backend = resolve_ledger_backend(scope, cfg.raw)
        host = mongo_uri_host(resolve_mongo_uri(cfg.raw))
        db = resolve_database_name(config=cfg.raw)
        where = "lokal" if is_local_mongo_uri(config=cfg.raw) else "remote"
        msg += (
            f"<b>Ledger:</b> <code>{scope}</code> · {backend}\n"
            f"<b>Mongo ({where}):</b> <code>{host}</code> / <code>{db}</code>\n"
        )
    msg += "\n"
    msg += _gate_section("Mainnet (Live)", cfg.live_config, adapter, bot_config=cfg)
    msg += "\n\n"

    if cfg.trading_mode == "live":
        holdings = fetch_spot_holdings(cfg)
        if holdings:
            prices = get_prices_batch(
                [h["symbol"] for h in holdings],
                allow_entry_price_fallback=True,
            )
            msg += "<b>Spot-Bestände</b>\n"
            msg += "\n".join(format_holdings_lines(holdings, prices))
            msg += "\n\n"

    msg += """<b>Modi</b>
/mode paper — Paper veraltet → Simulated Live (dry-run, Order-Ledger)
/mode live — Simulated Live (dry-run, keine echten Gate-Orders)
/live_confirm — Simulated Live (Staging, Order-Ausführung aktiv)
/live_cancel — Mainnet widerrufen, zurück zu Simulated Live (dry-run)

<b>Simulated Live</b>
1. Keys in .env: GATE_API_KEY / GATE_API_SECRET
2. /mode live oder /live_confirm — Simulated Live, keine echten Gate-Orders
"""
    send_telegram_message(msg)
    return True
