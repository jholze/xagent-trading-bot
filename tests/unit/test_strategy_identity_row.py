"""#501 — identity ``strategies[]`` row type + tenant-safe upsert. No auto-birth.

Fixture identity is meta + ``auto_identity: true`` only. Tests must not write
``data/`` and must not name today's live lots in product code.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import data_manager
from core.config import BotConfig
from storage import tenant_meta_store as tms
from strategies.registry import (
    STRATEGY_IDENTITY_META_KEYS,
    STRATEGY_PARAM_KEYS,
    _explicit_strategy_entry,
    is_identity_strategy_entry,
    promote_hypothesis_to_config,
    resolve_coin_config,
    sync_hermes_baseline_to_config,
    upsert_strategy_row,
)

IDENTITY = {
    "symbol": "SLOT/USDT",
    "timeframe": "4h",
    "strategy_class": "technical_rsi_bb",
    "description": "identity slot",
    "auto_identity": True,
}

HENRY = "henry"


def _identity(**overrides):
    row = dict(IDENTITY)
    row.update(overrides)
    return row


# --- fake Mongo (in-memory; nothing touches data/) ---------------------------


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
        "live": {"dry_run": True, "execution": "shadow", "max_usdt_per_trade": 50},
        "strategies": [
            {
                "symbol": "CURATED/USDT",
                "timeframe": "4h",
                "rsi_buy_low": 30,
                "rsi_sell_30": 70,
                "description": "operator curated",
            }
        ],
    }


@pytest.fixture
def henry_mongo(monkeypatch):
    operator = _operator_cfg()
    db = FakeDb()
    monkeypatch.setattr(
        "core.tenant_context.resolve_tenant_id",
        lambda tenant_id=None: tenant_id or HENRY,
    )
    monkeypatch.setattr(
        data_manager, "_load_default_config_from_disk", lambda: copy.deepcopy(operator)
    )
    monkeypatch.setattr(
        data_manager, "_should_use_mongo_for_tenant_config", lambda cfg=None: True
    )
    monkeypatch.setattr(data_manager, "_mongo_test_mode", lambda cfg=None: True)
    monkeypatch.setattr(tms, "get_database", lambda test=False, config=None: db)
    return operator, db


def _henry_body(db: FakeDb) -> dict:
    doc = db[tms.TENANT_CONFIGS_COLL].docs.get(HENRY) or {}
    return doc.get("body") or {}


# --- predicate ---------------------------------------------------------------


def test_predicate_six_live_config_rows_are_not_identity():
    rows = json.loads(Path("config.json").read_text(encoding="utf-8"))["strategies"]
    assert len(rows) == 6
    for entry in rows:
        assert is_identity_strategy_entry(entry) is False, entry.get("symbol")


def test_predicate_identity_fixture_true_and_param_keys_win():
    assert is_identity_strategy_entry(IDENTITY) is True
    assert is_identity_strategy_entry(None) is False
    assert is_identity_strategy_entry("x") is False
    # Absence of keys without the marker is not identity (broken ARIA).
    assert is_identity_strategy_entry({
        "symbol": "ARIA/USDT",
        "timeframe": "4h",
        "strategy_class": "technical_rsi_bb",
        "description": "broken",
    }) is False
    assert is_identity_strategy_entry(_identity(auto_identity=False)) is False
    stale = _identity(rsi_sell_30=70)
    assert is_identity_strategy_entry(stale) is False
    assert "rsi_sell_30" in STRATEGY_PARAM_KEYS
    assert "auto_identity" in STRATEGY_IDENTITY_META_KEYS
    assert "rsi_sell_30" not in STRATEGY_IDENTITY_META_KEYS


def test_explicit_strategy_entry_identity_is_none():
    raw = copy.deepcopy(data_manager.get_config())
    cfg = BotConfig(raw={**raw, "strategies": [IDENTITY]})
    with patch("strategies.registry.get_bot_config", return_value=cfg):
        assert _explicit_strategy_entry("SLOT/USDT", "4h") is None
        aria = next(e for e in raw["strategies"] if e.get("symbol") == "ARIA/USDT" and e.get("timeframe") == "4h")
        cfg_aria = BotConfig(raw={**raw, "strategies": [aria]})
    with patch("strategies.registry.get_bot_config", return_value=cfg_aria):
        got = _explicit_strategy_entry("ARIA/USDT", "4h")
        assert got is not None
        assert is_identity_strategy_entry(got) is False


# --- resolve_coin_config (B2 / Opus N2) --------------------------------------


def test_resolve_coin_config_identity_fully_resolved_like_no_row():
    coin = {"symbol": "SLOT/USDT", "timeframe": "4h", "source": "dry_run_expansion"}
    raw = copy.deepcopy(data_manager.get_config())
    curated = [e for e in raw.get("strategies", []) if e.get("symbol") != "SLOT/USDT"]
    cfg_none = BotConfig(raw={**raw, "strategies": curated})
    cfg_id = BotConfig(raw={**raw, "strategies": curated + [IDENTITY]})
    with patch("strategies.registry._hermes_memory_params", return_value=None), \
         patch("strategies.registry.get_bot_config", return_value=cfg_none):
        no_row = resolve_coin_config(coin)
    with patch("strategies.registry._hermes_memory_params", return_value=None), \
         patch("strategies.registry.get_bot_config", return_value=cfg_id):
        ident = resolve_coin_config(coin)
    assert "strategy_params" in ident
    assert ident["strategy_params"] == no_row["strategy_params"]
    assert ident["strategy_params"] != IDENTITY
    assert ident["strategy_params"].get("auto_identity") is not True
    # Fully resolved overlay (dca/buy knobs), not the bare identity dict.
    assert ident["strategy_params"]
    assert "dca" in ident["strategy_params"] or "rsi_buy_high" in ident["strategy_params"]


# --- BotConfig.strategy_params / _partial_sell_limits (Opus N3) --------------


def test_botconfig_strategy_params_identity_is_empty_like_unlisted():
    """_partial_sell_limits (risk/risk_manager.py:2034) has no resolve fallback."""
    from risk.risk_manager import RiskManager

    cfg = BotConfig(raw={
        "strategies": [
            IDENTITY,
            {"symbol": "ARIA/USDT", "timeframe": "4h", "rsi_buy_low": 25},
        ],
        "risk": {
            "min_position_usdt_for_partial_sell": 25,
            "min_sell_notional_usdt": 15,
            "block_partial_sell_if_sold_percent_above": 0.75,
        },
        "cmc": {},
    })
    assert cfg.strategy_params("SLOT/USDT", "4h") == {}
    assert cfg.strategy_params("UNLISTED/USDT", "4h") == {}
    assert cfg.strategy_params("ARIA/USDT", "4h")["rsi_buy_low"] == 25

    rm = RiskManager.__new__(RiskManager)
    rm.config = cfg
    assert rm._partial_sell_limits("SLOT/USDT", "4h") == rm._partial_sell_limits(
        "UNLISTED/USDT", "4h"
    )


# --- list_strategy_targets / tuner / slice (B1) ------------------------------


def test_list_strategy_targets_skips_identity_keeps_curated():
    from data_manager import list_strategy_targets

    cfg = copy.deepcopy(data_manager.get_config())
    cfg["trading_mode"] = "paper"
    cfg["strategies"] = list(cfg.get("strategies") or []) + [IDENTITY]
    with patch.object(data_manager, "get_config", return_value=cfg):
        targets = list_strategy_targets()
    assert not any(is_identity_strategy_entry(t) for t in targets)
    symbols = {(t.get("symbol"), t.get("timeframe")) for t in targets}
    assert ("SLOT/USDT", "4h") not in symbols
    assert ("ARIA/USDT", "4h") in symbols
    assert ("ARIA/USDT", "1h") in symbols  # paper: live_enabled False still listed
    assert ("RAVE/USDT", "4h") in symbols
    assert ("HIGH/USDT", "4h") in symbols
    assert ("SOL/USDT", "4h") in symbols
    assert ("BTC/USDT", "4h") in symbols


def test_auto_tuner_apply_refuses_identity_without_save_config():
    from services.strategy_auto_tuner import StrategyAutoTuner

    tuner = StrategyAutoTuner({
        "strategy_backtest": {
            "auto_apply": True,
            "guardrails": {
                "rsi_buy_low": {"min": 20, "max": 35, "max_delta": 5},
            },
        },
    })
    cfg = {"strategies": [copy.deepcopy(IDENTITY)]}
    with patch("services.strategy_auto_tuner.get_config", return_value=cfg), \
         patch("services.strategy_auto_tuner.save_config") as save:
        ok, applied, msg = tuner.apply(
            "SLOT/USDT", "4h", {"rsi_buy_low": 20, "rsi_buy_high": 50}
        )
    assert ok is False
    assert applied == {}
    assert msg == "identity row"
    save.assert_not_called()


def test_strategy_slice_identity_is_none():
    from hermes.promotion import _strategy_slice

    with patch("data_manager.get_config", return_value={"strategies": [IDENTITY]}):
        assert _strategy_slice("SLOT/USDT", "4h") is None
        assert _strategy_slice("MISSING/USDT", "4h") is None


# --- TF/symbol-only readers (acceptance 9) -----------------------------------


def test_replay_and_renew_may_use_identity_tf_symbol_not_params():
    """replay_commands.py:50 / renew_entry_recipes.py:116 harvest TF/symbol only."""
    tf = "1h"
    for entry in [IDENTITY]:
        if entry.get("symbol") == "SLOT/USDT":
            tf = entry.get("timeframe", "4h")
            break
    assert tf == "4h"
    assert is_identity_strategy_entry(IDENTITY)
    assert not any(k in IDENTITY for k in STRATEGY_PARAM_KEYS)

    harvested = []
    for entry in [IDENTITY]:
        if entry.get("symbol"):
            harvested.append(entry)
    assert harvested[0]["symbol"] == "SLOT/USDT"
    assert harvested[0]["timeframe"] == "4h"
    # Must not treat identity as personal params.
    personal = {k: harvested[0][k] for k in STRATEGY_PARAM_KEYS if k in harvested[0]}
    assert personal == {}


# --- promote + hermes wiring -------------------------------------------------


def test_promote_hypothesis_on_identity_merges_and_clears_marker():
    cfg = dict(data_manager.get_config())
    original = list(cfg.get("strategies", []))
    cfg["strategies"] = original + [copy.deepcopy(IDENTITY)]
    data_manager.save_config(cfg)
    try:
        ok, msg = promote_hypothesis_to_config({
            "id": "hyp_slot_identity",
            "name": "Slot test",
            "symbol": "SLOT/USDT",
            "timeframe": "4h",
            "source_account": "Tester",
            "params": {"rsi_buy_low": 28, "rsi_buy_high": 48, "volume_multiplier": 1.3},
        })
        assert ok, msg
        reloaded = data_manager.get_config()
        rows = [
            s for s in reloaded.get("strategies", [])
            if s.get("symbol") == "SLOT/USDT" and s.get("timeframe") == "4h"
        ]
        assert len(rows) == 1
        row = rows[0]
        assert row.get("auto_identity") is not True
        assert "auto_identity" not in row
        assert row.get("rsi_buy_low") == 28
        assert row.get("sandbox_id") == "hyp_slot_identity"
        assert is_identity_strategy_entry(row) is False
    finally:
        cfg["strategies"] = original
        data_manager.save_config(cfg)


def test_promote_hypothesis_still_refuses_explicit_duplicate():
    ok, msg = promote_hypothesis_to_config({
        "id": "hyp_aria_dup",
        "name": "dup",
        "symbol": "ARIA/USDT",
        "timeframe": "4h",
        "params": {"rsi_buy_low": 20},
    })
    assert ok is False
    assert "already exists" in msg


def test_sync_hermes_baseline_patches_identity_and_clears_marker():
    cfg = dict(data_manager.get_config())
    original = list(cfg.get("strategies", []))
    cfg["strategies"] = original + [copy.deepcopy(IDENTITY)]
    data_manager.save_config(cfg)
    try:
        with patch("data_manager.reload_config"):
            ok, msg = sync_hermes_baseline_to_config(
                {
                    "symbol": "SLOT/USDT",
                    "timeframe": "4h",
                    "params": {"rsi_buy_low": 31, "volume_multiplier": 1.1},
                    "updated_at": "2026-09-19T00:00:00Z",
                },
                experiment_id="exp_slot",
            )
        assert ok, msg
        row = next(
            s for s in data_manager.get_config().get("strategies", [])
            if s.get("symbol") == "SLOT/USDT" and s.get("timeframe") == "4h"
        )
        assert "auto_identity" not in row
        assert row.get("rsi_buy_low") == 31
        assert row.get("hermes_experiment_id") == "exp_slot"
        assert is_identity_strategy_entry(row) is False
    finally:
        cfg["strategies"] = original
        data_manager.save_config(cfg)


def test_sync_hermes_does_not_enable_hermes():
    cfg = data_manager.get_config()
    assert (cfg.get("hermes") or {}).get("enabled") is not True


# --- upsert tenant-safe ------------------------------------------------------


def test_upsert_identity_create_no_overlay_keys_second_is_noop():
    original = list(data_manager.get_config().get("strategies", []))
    try:
        with patch("data_manager.patch_config", wraps=data_manager.patch_config) as pc:
            ok, msg = upsert_strategy_row("SLOT/USDT", "4h", {})
            assert ok, msg
            n_calls = pc.call_count
            ok2, msg2 = upsert_strategy_row("SLOT/USDT", "4h", {})
            assert ok2
            assert msg2 == "noop"
            assert pc.call_count == n_calls
        row = next(
            s for s in data_manager.get_config().get("strategies", [])
            if s.get("symbol") == "SLOT/USDT" and s.get("timeframe") == "4h"
        )
        assert row.get("auto_identity") is True
        assert is_identity_strategy_entry(row)
        assert not (set(row) - STRATEGY_IDENTITY_META_KEYS)
        for key in STRATEGY_PARAM_KEYS:
            assert key not in row
    finally:
        cfg = dict(data_manager.get_config())
        cfg["strategies"] = original
        data_manager.save_config(cfg)


def test_upsert_param_patch_clears_marker():
    original = list(data_manager.get_config().get("strategies", []))
    try:
        assert upsert_strategy_row("SLOT/USDT", "4h", {})[0]
        ok, msg = upsert_strategy_row("SLOT/USDT", "4h", {"rsi_sell_30": 66})
        assert ok, msg
        row = next(
            s for s in data_manager.get_config().get("strategies", [])
            if s.get("symbol") == "SLOT/USDT" and s.get("timeframe") == "4h"
        )
        assert "auto_identity" not in row
        assert row.get("rsi_sell_30") == 66
        assert is_identity_strategy_entry(row) is False
    finally:
        cfg = dict(data_manager.get_config())
        cfg["strategies"] = original
        data_manager.save_config(cfg)


def test_henry_upsert_does_not_replace_operator_strategies(henry_mongo):
    operator, db = henry_mongo
    coll = db[tms.TENANT_CONFIGS_COLL]
    coll.docs[HENRY] = {
        "tenant_id": HENRY,
        "body": {"keep": "me", "trading": {"entries_enabled": True}},
    }
    with patch.object(data_manager, "save_config") as save:
        ok, msg = upsert_strategy_row("SLOT/USDT", "4h", {}, tenant_id=HENRY)
    assert ok, msg
    save.assert_not_called()
    body = _henry_body(db)
    assert body.get("keep") == "me"
    rows = body.get("strategies") or []
    assert len(rows) == 1
    assert is_identity_strategy_entry(rows[0])
    assert rows[0]["symbol"] == "SLOT/USDT"
    # Operator list is untouched (arrays replace only inside the tenant body).
    assert operator["strategies"][0]["symbol"] == "CURATED/USDT"
    assert len(operator["strategies"]) == 1


def test_default_tenant_upsert_does_not_write_henry_body(henry_mongo):
    _, db = henry_mongo
    original = list(data_manager.get_config().get("strategies", []))
    try:
        ok, msg = upsert_strategy_row("SLOT/USDT", "4h", {}, tenant_id="default")
        assert ok, msg
        assert HENRY not in db[tms.TENANT_CONFIGS_COLL].docs
    finally:
        cfg = dict(data_manager.get_config())
        cfg["strategies"] = original
        data_manager.save_config(cfg)


def test_upsert_never_save_config_get_config_on_tenant(henry_mongo):
    """Tenant path must use patch_config, not save_config(get_config()) (#456)."""
    _, db = henry_mongo
    with patch.object(data_manager, "save_config") as save, \
         patch.object(data_manager, "patch_config", wraps=data_manager.patch_config) as pc:
        ok, msg = upsert_strategy_row("SLOT/USDT", "4h", {"rsi_buy_low": 29}, tenant_id=HENRY)
    assert ok, msg
    save.assert_not_called()
    pc.assert_called()
    rows = _henry_body(db).get("strategies") or []
    assert len(rows) == 1
    assert rows[0].get("rsi_buy_low") == 29
    assert "auto_identity" not in rows[0]


def test_identity_resolve_does_not_copy_then_break():
    """Identity continue so the for/else still sets fully resolved strategy_params."""
    coin = {"symbol": "SLOT/USDT", "timeframe": "4h"}
    raw = copy.deepcopy(data_manager.get_config())
    cfg = BotConfig(raw={**raw, "strategies": [IDENTITY], "trading_mode": "paper"})
    with patch("strategies.registry._hermes_memory_params", return_value=None), \
         patch("strategies.registry.get_bot_config", return_value=cfg):
        merged = resolve_coin_config(coin)
    params = merged.get("strategy_params")
    assert isinstance(params, dict)
    assert params != IDENTITY
    assert "auto_identity" not in params
    # Equal to the no-row resolution (same coin, empty strategies).
    cfg_empty = BotConfig(raw={**raw, "strategies": [], "trading_mode": "paper"})
    with patch("strategies.registry._hermes_memory_params", return_value=None), \
         patch("strategies.registry.get_bot_config", return_value=cfg_empty):
        no_row = resolve_coin_config(coin)
    assert params == no_row["strategy_params"]
