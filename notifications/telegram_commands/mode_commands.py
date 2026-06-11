from data_manager import get_config, is_demo_mode, reload_config, save_config
from notifications.telegram_commands.usage_hints import hint
from notifications.telegram_commands.utils import safe_int
from services.trading_service import TradingService
from strategies.positions import count_open_positions
from telegram_notifier import send_telegram_message

MAX_POSITIONS_MIN = 1
MAX_POSITIONS_MAX = 50


def _save_mode_updates(updates: dict) -> bool:
    config = get_config()
    config.update(updates)
    return save_config(config)


def handle(text: str) -> bool:
    if text in ["/mode", "/tradingmode"]:
        service = TradingService()
        demo = " | Demo: ON" if is_demo_mode() else ""
        msg = f"""<b>Trading Mode</b>

Current: <b>{service.mode_label()}</b>{demo}

<b>Commands:</b>
/mode paper — Local paper trading (virtual ledger)
/mode gate_testnet — Gate.io testnet orders (visible on Gate)
/mode live — Live Gate.io mainnet (requires /live_confirm)
/mode off — Analysis only, no execution
/live_confirm — Confirm live trading
/live_cancel — Revoke live confirmation
/gate — Mainnet + testnet API status
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
        if _save_mode_updates({
            "trading_mode": "paper",
            "virtual_trading": True,
            "live_confirmed": False,
        }):
            reload_config()
            send_telegram_message(
                "✅ Switched to <b>paper</b> mode (local ledger).\n"
                "Trades in trade_history.json — not on Gate.io."
            )
        else:
            send_telegram_message("❌ Failed to save config.")
        return True

    if text == "/mode gate_testnet":
        dry = get_config().get("gate_testnet", {}).get("dry_run", False)
        if _save_mode_updates({
            "trading_mode": "gate_testnet",
            "virtual_trading": True,
            "live_confirmed": False,
        }):
            reload_config()
            send_telegram_message(
                "✅ Switched to <b>gate_testnet</b> mode.\n"
                "Orders go to Gate.io Testnet (visible in Spot Order History).\n"
                f"Dry run: <b>{'ON' if dry else 'OFF'}</b> — use /gate to check keys."
            )
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
        cfg = get_config()
        dry = cfg.get("live", {}).get("dry_run", True)
        if _save_mode_updates({"trading_mode": "live", "virtual_trading": False}):
            reload_config()
            send_telegram_message(
                "⚠️ Switched to <b>live</b> mode (mainnet).\n"
                "Send <code>/live_confirm</code> to enable real orders.\n"
                f"Dry run: <b>{'ON' if dry else 'OFF'}</b> (set live.dry_run in config.json)"
            )
        else:
            send_telegram_message("❌ Failed to save config.")
        return True

    if text == "/live_confirm":
        if _save_mode_updates({"trading_mode": "live", "live_confirmed": True}):
            reload_config()
            send_telegram_message("🔴 <b>Live trading CONFIRMED.</b> Real Gate.io orders may be placed.")
        else:
            send_telegram_message("❌ Failed to save config.")
        return True

    if text == "/live_cancel":
        if _save_mode_updates({
            "live_confirmed": False,
            "trading_mode": "paper",
            "virtual_trading": True,
        }):
            reload_config()
            send_telegram_message("✅ Live trading cancelled. Back to <b>paper</b> mode.")
        else:
            send_telegram_message("❌ Failed to save config.")
        return True

    if text.startswith("/mode "):
        send_telegram_message(hint("mode"))
        return True

    return False