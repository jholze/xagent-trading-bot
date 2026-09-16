"""#330 slice 2 — versioned config snapshots + /config diff|revert."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import data_manager
from core.config_guardrails import ConfigValidationError
from notifications.telegram_commands import config_commands, router
from notifications.telegram_commands.command_context import _chat_id_var
from notifications.telegram_commands.menu_commands import HOME_KEYS, MENU_SECTIONS_SATELLITE
from storage.config_history import (
    MAX_SNAPSHOTS,
    ConfigSnapshotError,
    get_snapshot,
    history_dir,
    list_snapshots,
    unified_diff,
)
from tests.support.telegram_capture import install_telegram_capture, texts

_REAL_SAVE_CONFIG = data_manager.save_config
assert _REAL_SAVE_CONFIG.__name__ == "save_config"

OPERATOR_CHAT = "12345"
SATELLITE_CHAT = "999"
DENY_TEXT = "Nur Operator"


@pytest.fixture
def history_home(monkeypatch, tmp_path):
    """Real save_config; config.json in cwd; snapshots under tmp data/."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(data_manager, "_config_cache", None)
    monkeypatch.setattr(data_manager, "save_config", _REAL_SAVE_CONFIG)
    monkeypatch.setattr("core.tenant_context.resolve_tenant_id", lambda tenant_id=None: "default")
    config_commands.reset_config_revert_for_tests()
    yield tmp_path
    config_commands.reset_config_revert_for_tests()


def _valid_cfg(**overrides) -> dict:
    body = {
        "max_open_positions": 5,
        "max_usdt_per_trade": 100,
        "trading_mode": "paper",
        "live": {"execution": "shadow", "max_usdt_per_trade": 100},
    }
    body.update(overrides)
    return body


def _snapshot_files(tenant_id: str = "default") -> list[Path]:
    dest = Path(history_dir(tenant_id))
    if not dest.is_dir():
        return []
    return sorted(dest.glob("*.json"), reverse=True)


# --------------------------------------------------------------------------
# save_config snapshots
# --------------------------------------------------------------------------


def test_first_save_has_no_previous_so_no_snapshot(history_home):
    assert _REAL_SAVE_CONFIG(_valid_cfg()) is True
    assert (history_home / "config.json").exists()
    assert _snapshot_files() == []


def test_snapshot_written_on_save(history_home):
    first = _valid_cfg(max_open_positions=5)
    second = _valid_cfg(max_open_positions=9)
    assert _REAL_SAVE_CONFIG(first) is True
    assert _REAL_SAVE_CONFIG(second) is True
    files = _snapshot_files()
    assert len(files) == 1
    envelope = json.loads(files[0].read_text(encoding="utf-8"))
    assert envelope["body"]["max_open_positions"] == 5
    assert json.loads((history_home / "config.json").read_text(encoding="utf-8")) == second
    snaps = list_snapshots("default")
    assert len(snaps) == 1
    assert snaps[0].n == 1
    assert snaps[0].snapshot_id
    assert snaps[0].created_at


def test_rotation_keeps_at_most_20(history_home):
    assert _REAL_SAVE_CONFIG(_valid_cfg(max_open_positions=1)) is True
    for i in range(2, MAX_SNAPSHOTS + 3):
        assert _REAL_SAVE_CONFIG(_valid_cfg(max_open_positions=i)) is True
    files = _snapshot_files()
    assert len(files) == MAX_SNAPSHOTS
    bodies = [json.loads(p.read_text(encoding="utf-8"))["body"]["max_open_positions"] for p in files]
    # newest-first filenames; newest previous is max_open_positions=MAX_SNAPSHOTS+1
    assert 1 not in bodies
    assert MAX_SNAPSHOTS + 1 in bodies


def test_failed_snapshot_does_not_write_config(history_home):
    assert _REAL_SAVE_CONFIG(_valid_cfg(max_open_positions=5)) is True
    before = (history_home / "config.json").read_text(encoding="utf-8")
    with patch(
        "storage.config_history.record_snapshot",
        side_effect=ConfigSnapshotError("disk full"),
    ):
        with pytest.raises(ConfigSnapshotError, match="disk full"):
            _REAL_SAVE_CONFIG(_valid_cfg(max_open_positions=8))
    assert (history_home / "config.json").read_text(encoding="utf-8") == before
    assert json.loads(before)["max_open_positions"] == 5


def test_tenant_save_snapshots_previous_mongo_body(history_home, monkeypatch):
    from storage import tenant_meta_store as tms

    saved = []
    previous = {"virtual_trading": True, "max_open_positions": 3}

    monkeypatch.setattr("core.tenant_context.resolve_tenant_id", lambda tenant_id=None: tenant_id or "t1")
    monkeypatch.setattr(data_manager, "_load_default_config_from_disk", lambda: {"trading_mode": "demo"})
    monkeypatch.setattr(data_manager, "_should_use_mongo_for_tenant_config", lambda cfg=None: True)
    monkeypatch.setattr(data_manager, "_mongo_test_mode", lambda cfg=None: True)
    monkeypatch.setattr(tms, "load_tenant_config_body", lambda tid, *, default_cfg, test=False: previous)
    monkeypatch.setattr(
        tms,
        "save_tenant_config",
        lambda tid, body, *, default_cfg, test=False: saved.append((tid, body)) or True,
    )

    body = {"virtual_trading": True, "max_open_positions": 7}
    assert _REAL_SAVE_CONFIG(body, tenant_id="t1") is True
    assert saved == [("t1", body)]
    snaps = list_snapshots("t1")
    assert len(snaps) == 1
    assert snaps[0].body == previous


def test_tenant_failed_snapshot_skips_meta_store(history_home, monkeypatch):
    from storage import tenant_meta_store as tms

    saved = []
    monkeypatch.setattr("core.tenant_context.resolve_tenant_id", lambda tenant_id=None: "t1")
    monkeypatch.setattr(data_manager, "_load_default_config_from_disk", lambda: {})
    monkeypatch.setattr(data_manager, "_should_use_mongo_for_tenant_config", lambda cfg=None: True)
    monkeypatch.setattr(data_manager, "_mongo_test_mode", lambda cfg=None: True)
    monkeypatch.setattr(tms, "load_tenant_config_body", lambda *a, **k: {"max_open_positions": 2})
    monkeypatch.setattr(
        tms,
        "save_tenant_config",
        lambda *a, **k: saved.append(True) or True,
    )
    with patch(
        "storage.config_history.record_snapshot",
        side_effect=ConfigSnapshotError("nope"),
    ):
        with pytest.raises(ConfigSnapshotError):
            _REAL_SAVE_CONFIG(_valid_cfg(), tenant_id="t1")
    assert saved == []


# --------------------------------------------------------------------------
# /config commands
# --------------------------------------------------------------------------


@pytest.fixture
def tg(monkeypatch):
    return install_telegram_capture(monkeypatch)


def test_config_help_shows_newest_snapshot(history_home, tg):
    assert _REAL_SAVE_CONFIG(_valid_cfg(max_open_positions=5)) is True
    assert _REAL_SAVE_CONFIG(_valid_cfg(max_open_positions=6)) is True
    assert config_commands.handle("/config") is True
    joined = "\n".join(texts(tg))
    assert "/config diff" in joined
    snap = list_snapshots("default")[0]
    assert snap.snapshot_id in joined


def test_diff_shows_known_key_change(history_home, tg):
    assert _REAL_SAVE_CONFIG(_valid_cfg(max_open_positions=5)) is True
    assert _REAL_SAVE_CONFIG(_valid_cfg(max_open_positions=11)) is True
    assert config_commands.handle("/config diff") is True
    joined = "\n".join(texts(tg))
    assert "max_open_positions" in joined
    assert "5" in joined
    assert "11" in joined
    # default n=1
    assert config_commands.handle("/config diff 1") is True


def test_unified_diff_helper_marks_key_change():
    diff = unified_diff(
        {"max_open_positions": 11},
        {"max_open_positions": 5},
        from_label="current",
        to_label="snapshot 1",
    )
    assert "max_open_positions" in diff
    assert "-  \"max_open_positions\": 11" in diff or '-  "max_open_positions": 11' in diff
    assert "+  \"max_open_positions\": 5" in diff or '+  "max_open_positions": 5' in diff


def test_revert_without_confirm_does_not_write(history_home, tg, monkeypatch):
    assert _REAL_SAVE_CONFIG(_valid_cfg(max_open_positions=5)) is True
    assert _REAL_SAVE_CONFIG(_valid_cfg(max_open_positions=9)) is True
    calls: list = []

    def _spy(config, tenant_id=None, **kwargs):
        calls.append(config)
        return True

    monkeypatch.setattr(data_manager, "save_config", _spy)
    assert config_commands.handle("/config revert 1") is True
    assert calls == []
    assert any(item.get("kind") == "buttons" for item in tg)
    current = json.loads((history_home / "config.json").read_text(encoding="utf-8"))
    assert current["max_open_positions"] == 9


def test_revert_restores_through_save_config(history_home, tg):
    first = _valid_cfg(max_open_positions=5, max_usdt_per_trade=80)
    second = _valid_cfg(max_open_positions=9, max_usdt_per_trade=120)
    assert _REAL_SAVE_CONFIG(first) is True
    assert _REAL_SAVE_CONFIG(second) is True
    assert config_commands.handle("/config revert 1") is True
    buttons = [item for item in tg if item.get("kind") == "buttons"]
    assert buttons
    token = buttons[0]["buttons"][0][0]["callback_data"].split(":", 1)[1]
    assert config_commands.handle_callback(
        {"id": "cq", "data": f"config_ok:{token}", "message": {"chat": {"id": 12345}}}
    ) is True
    restored = json.loads((history_home / "config.json").read_text(encoding="utf-8"))
    assert restored["max_open_positions"] == 5
    assert restored["max_usdt_per_trade"] == 80
    # revert itself snapshots the pre-revert current
    snaps = list_snapshots("default")
    assert snaps[0].body["max_open_positions"] == 9


def test_revert_invalid_snapshot_rejected_by_guardrails(history_home, tg):
    assert _REAL_SAVE_CONFIG(_valid_cfg(max_open_positions=5)) is True
    dest = Path(history_dir("default"))
    dest.mkdir(parents=True, exist_ok=True)
    bad = {
        "id": "bad1",
        "created_at": "2026-01-01T00:00:00+00:00",
        "tenant_id": "default",
        "body": {"max_open_positions": 3600, "trading_mode": "paper"},
    }
    (dest / "z_bad.json").write_text(json.dumps(bad), encoding="utf-8")
    # newest-first: z_bad sorts first as n=1
    assert get_snapshot("default", 1).body["max_open_positions"] == 3600
    assert config_commands.handle("/config revert 1") is True
    token = [item for item in tg if item.get("kind") == "buttons"][0]["buttons"][0][0][
        "callback_data"
    ].split(":", 1)[1]
    tg.clear()
    assert config_commands.handle_callback({"id": "cq", "data": f"config_ok:{token}"}) is True
    joined = "\n".join(texts(tg))
    assert "3600" in joined or "rejected" in joined.lower() or "Guardrail" in joined or "guardrail" in joined.lower()
    current = json.loads((history_home / "config.json").read_text(encoding="utf-8"))
    assert current["max_open_positions"] == 5
    # ConfigValidationError message
    with pytest.raises(ConfigValidationError):
        _REAL_SAVE_CONFIG({"max_open_positions": 3600})


def test_config_not_on_satellite_home_keyboard():
    sat = {k for _, keys in MENU_SECTIONS_SATELLITE for k in keys}
    assert "config" not in HOME_KEYS
    assert "config" not in sat


def test_locales_de_and_en():
    from notifications.telegram_i18n import t

    assert "/config" in t("config_help", lang="de")
    assert "/config" in t("config_help", lang="en")
    assert t("config_revert_btn_ok", lang="de") != t("config_revert_btn_ok", lang="en")
    assert t("config_revert_cancelled", lang="de") != t("config_revert_cancelled", lang="en")


def test_revert_confirm_restores_shown_body_after_newer_snapshot(history_home, tg):
    """Confirm binds snapshot_id; a later save_config must not shift the restore."""
    first = _valid_cfg(max_open_positions=5)
    second = _valid_cfg(max_open_positions=9)
    third = _valid_cfg(max_open_positions=11)
    assert _REAL_SAVE_CONFIG(first) is True
    assert _REAL_SAVE_CONFIG(second) is True
    shown = get_snapshot("default", 1)
    assert shown is not None
    assert shown.body["max_open_positions"] == 5
    assert config_commands.handle("/config revert 1") is True
    token = [item for item in tg if item.get("kind") == "buttons"][0]["buttons"][0][0][
        "callback_data"
    ].split(":", 1)[1]
    assert _REAL_SAVE_CONFIG(third) is True
    shifted = get_snapshot("default", 1)
    assert shifted is not None
    assert shifted.body["max_open_positions"] == 9
    assert shifted.snapshot_id != shown.snapshot_id
    assert config_commands.handle_callback(
        {"id": "cq", "data": f"config_ok:{token}", "message": {"chat": {"id": 12345}}}
    ) is True
    restored = json.loads((history_home / "config.json").read_text(encoding="utf-8"))
    assert restored["max_open_positions"] == 5


def test_revert_confirm_missing_id_writes_nothing(history_home, tg):
    """Confirm aborts with config_snapshot_missing when the bound id is gone."""
    assert _REAL_SAVE_CONFIG(_valid_cfg(max_open_positions=5)) is True
    assert _REAL_SAVE_CONFIG(_valid_cfg(max_open_positions=9)) is True
    shown = get_snapshot("default", 1)
    assert shown is not None
    assert config_commands.handle("/config revert 1") is True
    token = [item for item in tg if item.get("kind") == "buttons"][0]["buttons"][0][0][
        "callback_data"
    ].split(":", 1)[1]
    Path(shown.path).unlink()
    tg.clear()
    before = (history_home / "config.json").read_text(encoding="utf-8")
    assert json.loads(before)["max_open_positions"] == 9
    assert config_commands.handle_callback(
        {"id": "cq", "data": f"config_ok:{token}", "message": {"chat": {"id": 12345}}}
    ) is True
    assert (history_home / "config.json").read_text(encoding="utf-8") == before
    joined = "\n".join(texts(tg))
    assert shown.snapshot_id in joined
    assert "existiert nicht" in joined or "does not exist" in joined
