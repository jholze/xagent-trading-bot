import os

from core.build_info import format_build_line
from data_manager import get_config, is_demo_mode, reload_config, save_config
from notifications.telegram_commands.usage_hints import hint
from notifications.telegram_commands.utils import safe_int
from services.ledger_sync import on_trading_mode_change
from services.trading_service import TradingService
from strategies.positions import count_open_positions
from telegram_notifier import send_telegram_message

MAX_POSITIONS_MIN = 1
MAX_POSITIONS_MAX = 50


def _save_mode_updates(updates: dict) -> bool:
    config = get_config()
    config.update(updates)
    return save_config(config)


def _apply_mode_switch(updates: dict) -> tuple[bool, str]:
    old_mode = get_config().get("trading_mode", "paper")
    if not _save_mode_updates(updates):
        return False, ""
    reload_config()
    new_mode = get_config().get("trading_mode", "paper")
    ledger_msg = on_trading_mode_change(old_mode, new_mode)
    return True, ledger_msg


def handle(text: str) -> bool:
    if text in ["/mode", "/tradingmode"]:
        service = TradingService()
        demo = " | Demo: ON" if is_demo_mode() else ""
        msg = f"""<b>Trading Mode</b>

Current: <b>{service.mode_label()}</b>{demo}
{format_build_line()}

<b>Commands:</b>
/mode paper — Local paper trading (virtual ledger)
/mode live — Live Gate.io mainnet (requires /live_confirm)
/mode off — Analysis only, no execution
/live_confirm — Confirm live trading
/live_cancel — Revoke live confirmation
/gate — Gate.io API status + Balance
/maxpositions — Max. offene Positionen anzeigen/setzen
"""
        send_telegram_message(msg)
        return True

    if text in ["/maxpositions", "/maxpos"]:
        cfg = get_config()
        current = int(cfg.get("max_open_positions", 5))
        open_count = count_open_positions()
        send_telegram_message(
            f"<b>Max. offene Positionen</b>\n\n"
            f"Aktuell: <b>{current}</b>  ·  Offen: <b>{open_count}</b>\n\n"
            f"Ändern: <code>/maxpositions ANZAHL</code>\n"
            f"Beispiel: <code>/maxpositions 10</code>  "
            f"(Bereich {MAX_POSITIONS_MIN}–{MAX_POSITIONS_MAX})"
        )
        return True

    if text.startswith("/maxpositions ") or text.startswith("/maxpos "):
        parts = [p.strip() for p in text.split() if p.strip()]
        value = safe_int(parts[1]) if len(parts) > 1 else None
        if value is None or value < MAX_POSITIONS_MIN or value > MAX_POSITIONS_MAX:
            send_telegram_message(hint("maxpositions"))
            return True
        if _save_mode_updates({"max_open_positions": value}):
            reload_config()
            open_count = count_open_positions()
            send_telegram_message(
                f"✅ Max. offene Positionen auf <b>{value}</b> gesetzt.\n"
                f"Aktuell offen: <b>{open_count}</b>/{value}"
            )
        else:
            send_telegram_message("❌ Konfiguration konnte nicht gespeichert werden.")
        return True

    if text == "/mode paper":
        ok, ledger_msg = _apply_mode_switch({
            "trading_mode": "paper",
            "virtual_trading": True,
            "live_confirmed": False,
        })
        if ok:
            msg = (
                "✅ Switched to <b>paper</b> mode (local ledger).\n"
                "Trades in trade_history.json — not on Gate.io."
            )
            if ledger_msg:
                msg += f"\n\n{ledger_msg}"
            send_telegram_message(msg)
        else:
            send_telegram_message("❌ Failed to save config.")
        return True

    if text == "/mode off":
        if _save_mode_updates({"trading_mode": "off", "virtual_trading": False}):
            reload_config()
            send_telegram_message("✅ Trading set to <b>off</b> — analysis only.")
        else:
            send_telegram_message("❌ Failed to save config.")
        return True

    if text == "/mode live":
        if is_demo_mode():
            send_telegram_message(
                "❌ Live-Modus im <b>Demo-Modus</b> nicht verfügbar.\n"
                "Bot ohne <code>--demo</code> starten."
            )
            return True
        cfg = get_config()
        dry = cfg.get("live", {}).get("dry_run", True)
        ok, ledger_msg = _apply_mode_switch({
            "trading_mode": "live",
            "virtual_trading": False,
        })
        if ok:
            msg = (
                "⚠️ Switched to <b>live</b> mode (mainnet).\n"
                "Send <code>/live_confirm</code> to enable real orders.\n"
                f"Dry run: <b>{'ON' if dry else 'OFF'}</b> (set live.dry_run in config.json)"
            )
            if ledger_msg:
                msg += f"\n\n{ledger_msg}"
            send_telegram_message(msg)
        else:
            send_telegram_message("❌ Failed to save config.")
        return True

    if text == "/live_confirm":
        if is_demo_mode():
            send_telegram_message(
                "❌ Live-Trading im <b>Demo-Modus</b> nicht möglich.\n"
                "Bot ohne <code>--demo</code> starten."
            )
            return True

        cfg = get_config()
        live_cfg = cfg.get("live", {})
        key_env = live_cfg.get("api_key_env", "GATE_API_KEY")
        secret_env = live_cfg.get("api_secret_env", "GATE_API_SECRET")
        if not os.getenv(key_env) or not os.getenv(secret_env):
            send_telegram_message(
                f"❌ Gate Mainnet-Keys fehlen.\n"
                f"Setze <code>{key_env}</code> und <code>{secret_env}</code> in .env, dann <code>/gate</code>."
            )
            return True

        dry = live_cfg.get("dry_run", True)
        ok, ledger_msg = _apply_mode_switch({
            "trading_mode": "live",
            "live_confirmed": True,
            "virtual_trading": False,
        })
        if ok:
            msg = "🔴 <b>Live trading CONFIRMED.</b>"
            if dry:
                msg += (
                    "\n\n⚠️ <b>dry_run ist noch ON</b> — Orders werden nur lokal geloggt.\n"
                    "Für echte Orders: <code>live.dry_run: false</code> in config.json, Bot neu starten."
                )
            else:
                msg += "\n\nEchte Gate.io Mainnet-Orders sind aktiv."
            if ledger_msg:
                msg += f"\n\n{ledger_msg}"
            send_telegram_message(msg)
        else:
            send_telegram_message("❌ Failed to save config.")
        return True

    if text == "/live_cancel":
        ok, ledger_msg = _apply_mode_switch({
            "live_confirmed": False,
            "trading_mode": "paper",
            "virtual_trading": True,
        })
        if ok:
            msg = "✅ Live trading cancelled. Back to <b>paper</b> mode."
            if ledger_msg:
                msg += f"\n\n{ledger_msg}"
            send_telegram_message(msg)
        else:
            send_telegram_message("❌ Failed to save config.")
        return True

    if text.startswith("/mode "):
        send_telegram_message(hint("mode"))
        return True

    return False