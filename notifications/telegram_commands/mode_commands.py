import os

from core.runtime_identity import format_identity_section
from core.simulated_trading import is_simulated_trading, simulated_live_config_updates
from data_manager import get_config, reload_config, save_config
from notifications.telegram_commands.usage_hints import hint
from notifications.telegram_commands.utils import safe_int
from notifications.telegram_i18n import t
from services.ledger_sync import on_trading_mode_change
from services.trading_service import TradingService
from strategies.positions import count_open_positions
from notifications.telegram_commands.command_context import activate_command
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
    if text.strip().lower() == "/myid":
        from core.tenant_context import resolve_tenant_id
        from notifications.telegram_commands.command_context import current_chat_id

        cid = current_chat_id() or "?"
        tid = resolve_tenant_id()
        send_telegram_message(t("myid", chat_id=cid, tenant_id=tid))
        return True

    if text in ["/mode", "/tradingmode", "/stand", "/version", "/build"]:
        service = TradingService()
        sim = t("mode_sim_tag") if is_simulated_trading() else ""
        msg = (
            f"{t('mode_title')}\n\n"
            f"{t('mode_current', label=service.mode_label(), sim=sim)}\n\n"
            f"{format_identity_section()}\n\n"
            f"{t('mode_commands')}"
        )
        send_telegram_message(msg)
        return True

    if text in ["/maxpositions", "/maxpos"]:
        cfg = get_config()
        current = int(cfg.get("max_open_positions", 5))
        open_count = count_open_positions()
        activate_command("maxpositions")
        send_telegram_message(
            t(
                "maxpos_show",
                current=current,
                open=open_count,
                min=MAX_POSITIONS_MIN,
                max=MAX_POSITIONS_MAX,
            )
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
                t("maxpos_set", value=value, open=open_count)
            )
        else:
            send_telegram_message(t("config_save_failed"))
        return True

    if text == "/mode paper":
        ok, ledger_msg = _apply_mode_switch(simulated_live_config_updates())
        if ok:
            msg = t("mode_paper_migrated")
            if ledger_msg:
                msg += f"\n\n{ledger_msg}"
            send_telegram_message(msg)
        else:
            send_telegram_message(t("config_save_failed"))
        return True

    if text == "/mode off":
        if _save_mode_updates({"trading_mode": "off", "virtual_trading": False}):
            reload_config()
            send_telegram_message(t("mode_off"))
        else:
            send_telegram_message(t("config_save_failed"))
        return True

    if text == "/mode live":
        cfg = get_config()
        ok, ledger_msg = _apply_mode_switch(simulated_live_config_updates(cfg))
        if ok:
            staging = os.environ.get("DEMO_MODE") == "1"
            tag = t("mode_staging_tag") if staging else t("mode_dryrun_tag")
            msg = t("mode_live_sim", staging=tag)
            if ledger_msg:
                msg += f"\n\n{ledger_msg}"
            send_telegram_message(msg)
        else:
            send_telegram_message(t("config_save_failed"))
        return True

    if text == "/live_confirm":
        if os.environ.get("DEMO_MODE") == "1":
            ok, ledger_msg = _apply_mode_switch(simulated_live_config_updates())
            msg = t("live_confirm_staging")
            if ledger_msg:
                msg += f"\n\n{ledger_msg}"
            send_telegram_message(msg if ok else t("config_save_failed"))
            return True

        cfg = get_config()
        live_cfg = cfg.get("live", {})
        key_env = live_cfg.get("api_key_env", "GATE_API_KEY")
        secret_env = live_cfg.get("api_secret_env", "GATE_API_SECRET")
        if not os.getenv(key_env) or not os.getenv(secret_env):
            send_telegram_message(
                t("live_keys_missing", key_env=key_env, secret_env=secret_env)
            )
            return True

        dry = live_cfg.get("dry_run", True)
        ok, ledger_msg = _apply_mode_switch({
            "trading_mode": "live",
            "live_confirmed": True,
            "virtual_trading": False,
        })
        if ok:
            msg = t("live_confirmed")
            msg += t("live_dry_still_on") if dry else t("live_real_on")
            if ledger_msg:
                msg += f"\n\n{ledger_msg}"
            send_telegram_message(msg)
        else:
            send_telegram_message(t("config_save_failed"))
        return True

    if text == "/live_cancel":
        ok, ledger_msg = _apply_mode_switch(simulated_live_config_updates())
        if ok:
            msg = t("live_cancelled")
            if ledger_msg:
                msg += f"\n\n{ledger_msg}"
            send_telegram_message(msg)
        else:
            send_telegram_message(t("config_save_failed"))
        return True

    if text.startswith("/mode "):
        activate_command("mode")
        send_telegram_message(hint("mode"))
        return True

    return False
