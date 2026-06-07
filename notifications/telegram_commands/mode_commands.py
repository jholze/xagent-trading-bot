from data_manager import get_config, reload_config, save_config
from data_manager import is_demo_mode
from services.trading_service import TradingService
from telegram_notifier import send_telegram_message


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
/mode paper — Paper trading (virtual ledger)
/mode live — Switch to live Gate.io (requires /live_confirm)
/mode off — Analysis only, no execution
/live_confirm — Confirm live trading
/live_cancel — Revoke live confirmation
"""
        send_telegram_message(msg)
        return True

    if text == "/mode paper":
        if _save_mode_updates({"trading_mode": "paper", "virtual_trading": True, "live_confirmed": False}):
            reload_config()
            send_telegram_message("✅ Switched to <b>paper</b> trading mode.")
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
                "⚠️ Switched to <b>live</b> mode.\n"
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
        if _save_mode_updates({"live_confirmed": False, "trading_mode": "paper", "virtual_trading": True}):
            reload_config()
            send_telegram_message("✅ Live trading cancelled. Back to <b>paper</b> mode.")
        else:
            send_telegram_message("❌ Failed to save config.")
        return True

    return False