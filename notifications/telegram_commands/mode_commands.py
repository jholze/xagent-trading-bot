import os

from core.runtime_identity import format_identity_section
from core.simulated_trading import is_simulated_trading, simulated_live_config_updates
from data_manager import get_config, patch_config, reload_config
from notifications.telegram_commands.command_context import (
    activate_command,
    cancel_keyboard,
    clear_context,
    get_context,
    set_chat_id,
)
from notifications.telegram_commands.usage_hints import hint
from notifications.telegram_commands.utils import safe_int
from notifications.telegram_i18n import t
from services.ledger_sync import on_trading_mode_change
from services.trading_service import TradingService
from strategies.positions import count_open_positions
from telegram_notifier import answer_callback_query, send_telegram_message

MAX_POSITIONS_MIN = 1
MAX_POSITIONS_MAX = 50
MAXPOS_PRESETS = (5, 8, 10, 15)

MODE_CALLBACK_PREFIX = "modepick:"
MAXPOS_CALLBACK_PREFIX = "maxpos:"
_MODE_PICK_TO_COMMAND = {
    "live": "/mode live",
    "off": "/mode off",
}


_TRADING_FLAG_KEYS = ("entries_enabled", "exits_enabled")


def _save_mode_updates(updates: dict) -> bool:
    """Persist only ``updates`` (#456).

    For a tenant this patches the stored body instead of snapshotting the
    merged operator config, so later operator ``config.json`` edits still
    reach the tenant. Returns False when nothing was written.
    """
    return patch_config(updates)


def save_trading_flags(*, entries_enabled=None, exits_enabled=None) -> bool:
    """Persist trading.entries_enabled / exits_enabled via the /mode save path."""
    config = get_config()
    current = config.get("trading") or {}
    trading = {k: current[k] for k in _TRADING_FLAG_KEYS if k in current}
    if entries_enabled is not None:
        trading["entries_enabled"] = bool(entries_enabled)
    if exits_enabled is not None:
        trading["exits_enabled"] = bool(exits_enabled)
    if not _save_mode_updates({"trading": trading}):
        return False
    reload_config()
    return True


def _apply_mode_switch(updates: dict) -> tuple[bool, str]:
    old_mode = get_config().get("trading_mode", "paper")
    if not _save_mode_updates(updates):
        return False, ""
    reload_config()
    new_mode = get_config().get("trading_mode", "paper")
    ledger_msg = on_trading_mode_change(old_mode, new_mode)
    return True, ledger_msg


def _mode_status_text() -> str:
    service = TradingService()
    sim = t("mode_sim_tag") if is_simulated_trading() else ""
    return (
        f"{t('mode_title')}\n\n"
        f"{t('mode_current', label=service.mode_label(), sim=sim)}\n\n"
        f"{format_identity_section()}\n\n"
        f"{t('mode_commands')}"
    )


def _mode_button_rows() -> list:
    return [
        [
            {
                "text": t("mode_btn_sim_live"),
                "callback_data": f"{MODE_CALLBACK_PREFIX}live",
            },
            {
                "text": t("mode_btn_off"),
                "callback_data": f"{MODE_CALLBACK_PREFIX}off",
            },
        ],
        cancel_keyboard()[0],
    ]


def _maxpos_button_rows() -> list:
    return [
        [
            {
                "text": str(n),
                "callback_data": f"{MAXPOS_CALLBACK_PREFIX}{n}",
            }
            for n in MAXPOS_PRESETS
        ],
        cancel_keyboard()[0],
    ]


def prompt_mode_choice() -> None:
    """#497 copy unchanged; #514 adds Simulated Live / Aus taps."""
    send_telegram_message(
        _mode_status_text(),
        reply_markup={"inline_keyboard": _mode_button_rows()},
    )


def prompt_maxpositions(*, invalid: bool = False) -> None:
    cfg = get_config()
    current = int(cfg.get("max_open_positions", 5))
    open_count = count_open_positions()
    msg = t(
        "maxpos_show",
        current=current,
        open=open_count,
        min=MAX_POSITIONS_MIN,
        max=MAX_POSITIONS_MAX,
    )
    if invalid:
        msg = hint("maxpositions") + "\n\n" + msg
    send_telegram_message(
        msg,
        reply_markup={"inline_keyboard": _maxpos_button_rows()},
    )


def handle(text: str) -> bool:
    if text.strip().lower() == "/myid":
        from core.tenant_context import resolve_tenant_id
        from notifications.telegram_commands.command_context import current_chat_id

        cid = current_chat_id() or "?"
        tid = resolve_tenant_id()
        send_telegram_message(t("myid", chat_id=cid, tenant_id=tid))
        return True

    if text in ["/mode", "/tradingmode"]:
        prompt_mode_choice()
        activate_command("mode", chrome=False, state="mode_awaiting_choice")
        return True

    if text in ["/stand", "/version", "/build"]:
        send_telegram_message(_mode_status_text())
        return True

    if text in ["/maxpositions", "/maxpos"]:
        prompt_maxpositions()
        activate_command("maxpositions", chrome=False, state="maxpos_awaiting_value")
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
        # #497: un-confirm; do not keep simulated_live_config_updates()'s True.
        ok, ledger_msg = _apply_mode_switch({
            **simulated_live_config_updates(),
            "live_confirmed": False,
        })
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


def _wizard_entry(chat_id, command: str, state: str) -> dict | None:
    entry = get_context(chat_id)
    meta = (entry or {}).get("meta") or {}
    if not entry or entry.get("command") != command or str(meta.get("state") or "") != state:
        return None
    return entry


def _handle_mode_pick_callback(callback_query: dict) -> bool:
    chat_id = _callback_chat_id(callback_query, "modepick")
    if not chat_id:
        return True
    if _wizard_entry(chat_id, "mode", "mode_awaiting_choice") is None:
        send_telegram_message(t("mode_pick_expired"))
        return True
    data = str((callback_query or {}).get("data") or "")
    raw = data.split(":", 1)[1] if ":" in data else ""
    cmd = _MODE_PICK_TO_COMMAND.get(raw)
    if cmd is None:
        send_telegram_message(t("mode_pick_expired"))
        return True
    clear_context(chat_id)
    return handle(cmd)


def _handle_maxpos_callback(callback_query: dict) -> bool:
    chat_id = _callback_chat_id(callback_query, "maxpos")
    if not chat_id:
        return True
    if _wizard_entry(chat_id, "maxpositions", "maxpos_awaiting_value") is None:
        send_telegram_message(t("maxpos_pick_expired"))
        return True
    data = str((callback_query or {}).get("data") or "")
    raw = data.split(":", 1)[1] if ":" in data else ""
    value = safe_int(raw)
    if value is None or value < MAX_POSITIONS_MIN or value > MAX_POSITIONS_MAX:
        prompt_maxpositions(invalid=True)
        return True
    clear_context(chat_id)
    return handle(f"/maxpositions {value}")


def handle_callback(callback_query: dict) -> bool:
    data = str((callback_query or {}).get("data") or "")
    if data.startswith(MODE_CALLBACK_PREFIX):
        return _handle_mode_pick_callback(callback_query)
    if data.startswith(MAXPOS_CALLBACK_PREFIX):
        return _handle_maxpos_callback(callback_query)
    return False
