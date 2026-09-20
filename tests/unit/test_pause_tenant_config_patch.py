"""#456 — ``/pause`` must not snapshot the operator config into the tenant body.

``mode_commands`` persists through ``data_manager.patch_config`` which writes
**only the changed keys** into the tenant body (``tenant_meta_store.patch_tenant_config``
→ ``$set`` on dotted paths). Operator ``config.json`` edits therefore keep
reaching the tenant after a pause. A tenant without a Mongo config backend
gets ``False`` (nothing persisted), never a green "Pausiert".

Everything is in-memory: Mongo is a fake collection, ``config.json`` is a dict
handed to ``_load_default_config_from_disk``. Nothing touches ``data/``.
"""

from __future__ import annotations

import copy
from unittest.mock import patch

import pytest

import data_manager
from core.config_guardrails import ConfigValidationError
from storage import tenant_meta_store as tms
from storage.errors import LedgerUnavailable

TID = "t456"

# tests/conftest.py::normalize_unit_test_config (autouse) swaps ``save_config``
# for a cache-only stub; capture the real function at collection time.
_REAL_SAVE_CONFIG = data_manager.save_config
assert _REAL_SAVE_CONFIG.__name__ == "save_config"


# --- fake Mongo -------------------------------------------------------------


def _set_path(doc: dict, path: str, value) -> None:
    parts = path.split(".")
    cur = doc
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


class FakeCollection:
    """Just enough of pymongo for tenant_meta_store: find_one / update_one($set) / replace_one."""

    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.ops: list[tuple] = []

    def find_one(self, flt):
        doc = self.docs.get(flt["tenant_id"])
        return copy.deepcopy(doc) if doc else None

    def update_one(self, flt, update, upsert=False):
        self.ops.append(("update_one", copy.deepcopy(flt), copy.deepcopy(update), upsert))
        tid = flt["tenant_id"]
        doc = self.docs.get(tid)
        if doc is None:
            if not upsert:
                return
            doc = {"tenant_id": tid}
            self.docs[tid] = doc
        for path, value in update.get("$set", {}).items():
            _set_path(doc, path, copy.deepcopy(value))

    def replace_one(self, flt, doc, upsert=False):
        self.ops.append(("replace_one", copy.deepcopy(flt), copy.deepcopy(doc), upsert))
        self.docs[flt["tenant_id"]] = copy.deepcopy(doc)


class FakeDb(dict):
    def __missing__(self, key):
        coll = FakeCollection()
        self[key] = coll
        return coll


def _operator_cfg() -> dict:
    return {
        "trading_mode": "paper",
        "virtual_trading": True,
        "max_open_positions": 5,
        "trading": {"entries_enabled": True, "exits_enabled": True},
        "risk": {"max_daily_buys": 5, "cash_floor_pct": 10},
        "shorts": {"enabled": False},
        # operator-only ``live.*`` knobs — must never end up in a tenant body
        "live": {
            "dry_run": True,
            "execution": "shadow",
            "max_usdt_per_trade": 50,
            "api_key_env": "GATE_API_KEY",
        },
        "trending_watchlist": {"enabled": True, "max_symbols": 3},
    }


@pytest.fixture
def tenant_mongo(monkeypatch):
    """Tenant ``t456`` on a Mongo tenant-config backend backed by ``FakeDb``."""
    operator = _operator_cfg()
    db = FakeDb()
    monkeypatch.setattr("core.tenant_context.resolve_tenant_id", lambda tenant_id=None: tenant_id or TID)
    monkeypatch.setattr(data_manager, "_load_default_config_from_disk", lambda: copy.deepcopy(operator))
    monkeypatch.setattr(data_manager, "_should_use_mongo_for_tenant_config", lambda cfg=None: True)
    monkeypatch.setattr(data_manager, "_mongo_test_mode", lambda cfg=None: True)
    monkeypatch.setattr(tms, "get_database", lambda test=False, config=None: db)
    return operator, db


def _body(db: FakeDb) -> dict:
    doc = db[tms.TENANT_CONFIGS_COLL].docs.get(TID) or {}
    return doc.get("body") or {}


# --- tenant_meta_store.patch_tenant_config ----------------------------------


def test_patch_tenant_config_sets_only_dotted_paths(tenant_mongo):
    _, db = tenant_mongo
    coll = db[tms.TENANT_CONFIGS_COLL]
    coll.docs[TID] = {"tenant_id": TID, "body": {"trading": {"exits_enabled": True, "x": 1}, "keep": "me"}}

    assert tms.patch_tenant_config(TID, {"trading": {"entries_enabled": False}}, default_cfg={}, test=True) is True

    op, flt, update, upsert = coll.ops[-1]
    assert (op, flt, upsert) == ("update_one", {"tenant_id": TID}, True)
    assert set(update) == {"$set"}
    assert update["$set"]["body.trading.entries_enabled"] is False
    assert "updated_at" in update["$set"]
    assert set(update["$set"]) == {"body.trading.entries_enabled", "updated_at"}
    # siblings in the stored body survive
    assert _body(db) == {"trading": {"exits_enabled": True, "x": 1, "entries_enabled": False}, "keep": "me"}


def test_patch_tenant_config_upserts_missing_body(tenant_mongo):
    _, db = tenant_mongo
    assert tms.patch_tenant_config(TID, {"max_open_positions": 7}, default_cfg={}, test=True) is True
    assert _body(db) == {"max_open_positions": 7}


def test_patch_tenant_config_empty_dict_replaces_value(tenant_mongo):
    _, db = tenant_mongo
    db[tms.TENANT_CONFIGS_COLL].docs[TID] = {"tenant_id": TID, "body": {"shorts": {"enabled": True}}}
    assert tms.patch_tenant_config(TID, {"shorts": {}}, default_cfg={}, test=True) is True
    assert _body(db) == {"shorts": {}}


@pytest.mark.parametrize("bad", [{"a.b": 1}, {"$set": 1}, {"": 1}, {"risk": {"x.y": 2}}])
def test_patch_tenant_config_rejects_unsafe_keys(tenant_mongo, bad):
    _, db = tenant_mongo
    assert tms.patch_tenant_config(TID, bad, default_cfg={}, test=True) is False
    assert db[tms.TENANT_CONFIGS_COLL].ops == []


def test_patch_tenant_config_noop_and_invalid_input(tenant_mongo):
    _, db = tenant_mongo
    assert tms.patch_tenant_config(TID, {}, default_cfg={}, test=True) is True
    assert tms.patch_tenant_config("", {"a": 1}, default_cfg={}, test=True) is False
    assert tms.patch_tenant_config(TID, ["a"], default_cfg={}, test=True) is False  # type: ignore[arg-type]
    assert db[tms.TENANT_CONFIGS_COLL].ops == []


def test_patch_tenant_config_db_error_returns_false(monkeypatch):
    def boom(test=False, config=None):
        raise RuntimeError("mongo down")

    monkeypatch.setattr(tms, "get_database", boom)
    assert tms.patch_tenant_config(TID, {"a": 1}, default_cfg={}, test=True) is False


# --- data_manager.patch_config (tenant) --------------------------------------


def test_patch_config_tenant_writes_only_updates(tenant_mongo):
    _, db = tenant_mongo
    assert data_manager.patch_config({"trading": {"entries_enabled": False}}, tenant_id=TID) is True
    assert _body(db) == {"trading": {"entries_enabled": False}}
    ops = [op for op, *_ in db[tms.TENANT_CONFIGS_COLL].ops]
    assert ops == ["update_one"]  # never replace_one


def test_patch_config_tenant_guardrail_blocks_before_write(tenant_mongo):
    _, db = tenant_mongo
    with pytest.raises(ConfigValidationError) as exc_info:
        data_manager.patch_config({"risk": {"max_daily_buys": -1}}, tenant_id=TID)
    assert exc_info.value.path == "risk.max_daily_buys"
    assert db[tms.TENANT_CONFIGS_COLL].ops == []


def test_patch_config_tenant_validates_effective_config(tenant_mongo):
    """A body-only patch is validated against the merged view, not the bare patch."""
    _, db = tenant_mongo
    with pytest.raises(ConfigValidationError):
        data_manager.patch_config({"trading_mode": "yolo"}, tenant_id=TID)
    assert db[tms.TENANT_CONFIGS_COLL].ops == []


def test_patch_config_tenant_non_mongo_backend_is_false(tenant_mongo, monkeypatch):
    _, db = tenant_mongo
    monkeypatch.setattr(data_manager, "_should_use_mongo_for_tenant_config", lambda cfg=None: False)
    assert data_manager.patch_config({"trading": {"entries_enabled": False}}, tenant_id=TID) is False
    assert db[tms.TENANT_CONFIGS_COLL].ops == []


def test_patch_config_tenant_ledger_unavailable_is_false(tenant_mongo, monkeypatch):
    _, db = tenant_mongo

    def unavailable(tid, default_cfg):
        raise LedgerUnavailable(op="load_tenant_config_body", tenant_id=tid)

    monkeypatch.setattr(data_manager, "_load_tenant_config_body", unavailable)
    assert data_manager.patch_config({"trading": {"entries_enabled": False}}, tenant_id=TID) is False
    assert db[tms.TENANT_CONFIGS_COLL].ops == []


def test_patch_config_tenant_store_failure_is_false(tenant_mongo, monkeypatch):
    monkeypatch.setattr(tms, "patch_tenant_config", lambda *a, **k: False)
    assert data_manager.patch_config({"trading": {"entries_enabled": False}}, tenant_id=TID) is False


def test_patch_config_rejects_non_dict(tenant_mongo):
    assert data_manager.patch_config(["x"], tenant_id=TID) is False  # type: ignore[arg-type]


def test_save_config_tenant_non_mongo_backend_is_false(tenant_mongo, monkeypatch):
    """The former silent ``True`` on a skipped tenant write is gone (#456)."""
    monkeypatch.setattr(data_manager, "_should_use_mongo_for_tenant_config", lambda cfg=None: False)
    assert _REAL_SAVE_CONFIG({"trading": {"entries_enabled": False}}, tenant_id=TID) is False


# --- data_manager.patch_config (default tenant) ------------------------------


def test_patch_config_default_tenant_merges_into_config_json_path(monkeypatch):
    saved: list[tuple[dict, str | None]] = []
    monkeypatch.setattr("core.tenant_context.resolve_tenant_id", lambda tenant_id=None: "default")
    monkeypatch.setattr(
        data_manager,
        "_config_cache",
        {"trading": {"entries_enabled": True, "exits_enabled": True}, "risk": {"max_daily_buys": 3}},
    )

    def fake_save(config, tenant_id=None, **_):
        saved.append((copy.deepcopy(config), tenant_id))
        return True

    monkeypatch.setattr(data_manager, "save_config", fake_save)
    assert data_manager.patch_config({"trading": {"entries_enabled": False}}) is True
    assert saved == [
        (
            {"trading": {"entries_enabled": False, "exits_enabled": True}, "risk": {"max_daily_buys": 3}},
            "default",
        )
    ]


# --- end-to-end: /pause then operator risk edit still reaches the tenant -----


def _pause(exits: bool = True) -> str:
    from notifications.telegram_commands import pause_commands

    with patch("notifications.telegram_commands.pause_commands.send_telegram_message") as send, \
         patch("notifications.telegram_commands.pause_commands.get_bot_config") as gbc:
        gbc.return_value.exits_enabled = exits
        assert pause_commands.handle("/pause") is True
    return send.call_args[0][0]


def test_pause_persists_only_trading_flags_for_tenant(tenant_mongo):
    from notifications.telegram_i18n import t

    _, db = tenant_mongo
    msg = _pause()
    assert t("pause_done") in msg
    assert _body(db) == {"trading": {"entries_enabled": False, "exits_enabled": True}}
    assert "risk" not in _body(db)
    assert "shorts" not in _body(db)


def test_operator_risk_edit_after_pause_reaches_tenant(tenant_mongo):
    operator, db = tenant_mongo
    _pause()
    assert data_manager.load_config(tenant_id=TID)["risk"]["max_daily_buys"] == 5

    # operator edits config.json after the tenant paused
    operator["risk"]["max_daily_buys"] = 9
    operator["shorts"]["enabled"] = True

    effective = data_manager.load_config(tenant_id=TID)
    assert effective["risk"]["max_daily_buys"] == 9
    assert effective["shorts"]["enabled"] is True
    assert effective["trading"]["entries_enabled"] is False  # pause still holds
    assert effective["trading"]["exits_enabled"] is True


def test_pause_then_resume_keeps_body_minimal(tenant_mongo):
    from notifications.telegram_commands import pause_commands

    _, db = tenant_mongo
    _pause()
    with patch("notifications.telegram_commands.pause_commands.send_telegram_message"), \
         patch("notifications.telegram_commands.pause_commands.get_bot_config") as gbc:
        gbc.return_value.exits_enabled = True
        assert pause_commands.handle("/resume") is True
    assert _body(db) == {"trading": {"entries_enabled": True, "exits_enabled": True}}


def test_pause_non_mongo_tenant_reports_failure(tenant_mongo, monkeypatch):
    from notifications.telegram_i18n import t

    _, db = tenant_mongo
    monkeypatch.setattr(data_manager, "_should_use_mongo_for_tenant_config", lambda cfg=None: False)
    msg = _pause()
    assert msg == t("config_save_failed")
    assert t("pause_done") not in msg
    assert db[tms.TENANT_CONFIGS_COLL].ops == []


# --- /mode paper|live, /live_cancel: only ``live.dry_run`` may land in the body --

_SIM_LIVE_BODY = {
    "trading_mode": "live",
    "virtual_trading": False,
    "live_confirmed": True,
    "live": {"dry_run": True},
}


def _mode(text: str, monkeypatch) -> str:
    from notifications.telegram_commands import mode_commands

    monkeypatch.delenv("DEMO_MODE", raising=False)
    with patch.object(mode_commands, "send_telegram_message") as send, \
         patch.object(mode_commands, "on_trading_mode_change", return_value=""):
        assert mode_commands.handle(text) is True
    return send.call_args[0][0]


def test_simulated_live_config_updates_returns_only_dry_run():
    """The patch must not copy the merged ``live`` block (reviewer BLOCK, #456)."""
    from core.simulated_trading import simulated_live_config_updates

    merged = {"live": {"execution": "shadow", "max_usdt_per_trade": 50, "dry_run": False}}
    assert simulated_live_config_updates(merged) == _SIM_LIVE_BODY
    assert simulated_live_config_updates() == _SIM_LIVE_BODY
    assert simulated_live_config_updates({}) == _SIM_LIVE_BODY


@pytest.mark.parametrize("text", ["/mode live", "/mode paper", "/live_cancel"])
def test_mode_switch_persists_only_dry_run_for_tenant(tenant_mongo, text, monkeypatch):
    from notifications.telegram_i18n import t

    _, db = tenant_mongo
    msg = _mode(text, monkeypatch)
    assert t("config_save_failed") not in msg

    body = _body(db)
    expected = dict(_SIM_LIVE_BODY)
    if text == "/live_cancel":
        # #497: /live_cancel un-confirms; helper still returns True.
        expected["live_confirmed"] = False
    assert body == expected
    assert "max_usdt_per_trade" not in body["live"]
    assert "execution" not in body["live"]
    assert "api_key_env" not in body["live"]
    assert "trending_watchlist" not in body
    assert "risk" not in body


def test_operator_live_edit_after_mode_live_reaches_tenant(tenant_mongo, monkeypatch):
    operator, _ = tenant_mongo
    _mode("/mode live", monkeypatch)

    effective = data_manager.load_config(tenant_id=TID)
    assert effective["trading_mode"] == "live"
    assert effective["live"]["dry_run"] is True
    assert effective["live"]["execution"] == "shadow"
    assert effective["live"]["max_usdt_per_trade"] == 50

    # operator edits config.json after the tenant switched mode
    operator["live"]["max_usdt_per_trade"] = 25
    operator["live"]["execution"] = "testnet"
    operator["trending_watchlist"]["max_symbols"] = 7

    effective = data_manager.load_config(tenant_id=TID)
    assert effective["live"]["max_usdt_per_trade"] == 25
    assert effective["live"]["execution"] == "testnet"
    assert effective["trending_watchlist"]["max_symbols"] == 7
    assert effective["live"]["dry_run"] is True  # tenant flag still holds
    assert effective["trading_mode"] == "live"


def test_mode_live_then_pause_keeps_body_minimal(tenant_mongo, monkeypatch):
    _, db = tenant_mongo
    _mode("/mode live", monkeypatch)
    _pause()
    assert _body(db) == {
        **_SIM_LIVE_BODY,
        "trading": {"entries_enabled": False, "exits_enabled": True},
    }


def test_patch_config_default_tenant_keeps_live_siblings(monkeypatch):
    """Default-tenant path: ``deep_merge_dicts`` keeps the rest of ``live.*``."""
    from core.simulated_trading import simulated_live_config_updates

    saved: list[dict] = []
    monkeypatch.setattr("core.tenant_context.resolve_tenant_id", lambda tenant_id=None: "default")
    monkeypatch.setattr(
        data_manager,
        "_config_cache",
        {
            "trading_mode": "paper",
            "virtual_trading": True,
            "live": {"dry_run": False, "execution": "shadow", "max_usdt_per_trade": 50},
            "trending_watchlist": {"enabled": True},
        },
    )

    def fake_save(config, tenant_id=None, **_):
        saved.append(copy.deepcopy(config))
        return True

    monkeypatch.setattr(data_manager, "save_config", fake_save)
    assert data_manager.patch_config(simulated_live_config_updates()) is True
    assert saved == [
        {
            "trading_mode": "live",
            "virtual_trading": False,
            "live_confirmed": True,
            "live": {"dry_run": True, "execution": "shadow", "max_usdt_per_trade": 50},
            "trending_watchlist": {"enabled": True},
        }
    ]


def test_panic_reports_failed_persist(monkeypatch):
    """/panic must not claim "Entries AUS" when the flag was not persisted."""
    from notifications.telegram_commands import pause_commands
    from notifications.telegram_i18n import t

    pause_commands.reset_panic_for_tests()
    with patch.object(pause_commands, "save_trading_flags", return_value=False), \
         patch.object(pause_commands, "send_telegram_message") as send, \
         patch.object(pause_commands, "get_bot_config") as gbc:
        gbc.return_value.exits_enabled = True
        pause_commands._execute_panic([])
    summary = send.call_args[0][0]
    assert t("config_save_failed") in summary
    assert t("panic_entries_off") not in summary


def test_save_trading_flags_drops_unrelated_trading_keys():
    from notifications.telegram_commands import mode_commands

    cfg = {"trading": {"entries_enabled": True, "exits_enabled": True, "other_knob": 3}}
    with patch.object(mode_commands, "get_config", return_value=cfg), \
         patch.object(mode_commands, "patch_config", return_value=True) as pc, \
         patch.object(mode_commands, "reload_config"):
        assert mode_commands.save_trading_flags(entries_enabled=False) is True
    pc.assert_called_once_with({"trading": {"entries_enabled": False, "exits_enabled": True}})


def test_mode_commands_have_no_full_snapshot_seam():
    """No path in mode_commands may hand ``get_config()`` to ``save_config``."""
    from notifications.telegram_commands import mode_commands

    assert not hasattr(mode_commands, "save_config")
    assert mode_commands._save_mode_updates.__module__ == mode_commands.__name__
