"""#330 slice 1 — ``save_config`` bounds-checks trading-critical keys before writing.

One boundary table entry per guarded key (in + out), plus wiring tests for both
save paths (default ``config.json`` and tenant ``tenant_meta_store``). All file
I/O goes to ``tmp_path``; the repo ``config.json`` is only read.
"""

from __future__ import annotations

import json
import math
import os

import pytest

import data_manager
from core.config_guardrails import (
    GUARDED_KEYS,
    MAX_OPEN_POSITIONS_CAP,
    ConfigValidationError,
    validate_config_for_save,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# tests/conftest.py::normalize_unit_test_config (autouse) swaps the default-tenant
# ``save_config`` for a cache-only stub. Capture the real function at import
# (collection) time so the default-path wiring tests exercise the guardrail.
_REAL_SAVE_CONFIG = data_manager.save_config
assert _REAL_SAVE_CONFIG.__name__ == "save_config"


def _nest(path: str, value):
    """Build ``{"a": {"b": value}}`` from ``"a.b"``."""
    cfg: dict = {}
    node = cfg
    parts = path.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    return cfg


# (path, values that must pass, values that must be rejected)
_POS = ([0, 1, MAX_OPEN_POSITIONS_CAP, float(MAX_OPEN_POSITIONS_CAP)],
        [-1, MAX_OPEN_POSITIONS_CAP + 1, 3600, 1.5, "36", None, True, math.nan])
_INT_NN = ([0, 1, 10**7], [-1, 10**7 + 1, 0.5, "5", None, False, math.inf])
_PCT = ([0, 0.0, 0.15, 50, 100, 100.0], [-0.01, 100.01, 1000, "5", None, True, math.nan, math.inf])
_POSITIVE = ([0.01, 1, 4500, 1e9], [0, -1, "4500", None, True, math.nan, math.inf])
_NON_NEG = ([0, 0.0, 800, 1e9], [-0.01, -1, "800", None, True, math.nan, -math.inf])

BOUNDARY_TABLE: dict[str, tuple[list, list]] = {
    "max_open_positions": _POS,
    "max_usdt_per_trade": _POSITIVE,
    "live.max_usdt_per_trade": _POSITIVE,
    # Exact match: case/whitespace variants are rejected, not normalized —
    # execution_mode lowercases but risk_manager/registry compare case-sensitively.
    "trading_mode": (["live", "paper", "demo", "gate_testnet", "off"],
                     ["", "shadow", "real", "prod", "LIVE", " paper ", "OFF", None, 1, True]),
    "live.execution": (["shadow", "testnet", "real"],
                       ["", "live", "paper", "dry_run", "REAL", None, 0, False]),
    "costs.gate.spot.fee_maker_pct": _PCT,
    "costs.gate.spot.fee_taker_pct": _PCT,
    "costs.gate.spot.slippage_pct": _PCT,
    "costs.gate.swap.fee_maker_pct": _PCT,
    "costs.gate.swap.fee_taker_pct": _PCT,
    "costs.gate.swap.slippage_pct": _PCT,
    "costs.slippage_by_tier.volatile": _PCT,
    "costs.slippage_by_tier.mid": _PCT,
    "costs.slippage_by_tier.stable": _PCT,
    "risk.max_daily_loss_pct": _PCT,
    "risk.cash_floor_pct": _PCT,
    "risk.dca_reserve_pct": _PCT,
    "risk.drawdown_throttle_pct": _PCT,
    "risk.max_daily_buys": _INT_NN,
    "risk.max_daily_dca_buys": _INT_NN,
    "risk.max_daily_sells": _INT_NN,
    "risk.max_daily_dca_usdt": _NON_NEG,
    "risk.min_trade_usdt": _NON_NEG,
    "risk.min_sell_notional_usdt": _NON_NEG,
    "risk.position_capacity.base": _POS,
    "risk.position_capacity.min_floor": _POS,
    "risk.position_capacity.max_ceiling": _POS,
    "risk.slot_eviction.max_evict_notional_usdt": _NON_NEG,
    "risk.slot_eviction.max_evictions_per_hour": _INT_NN,
    "risk.slot_eviction.max_evictions_per_day": _INT_NN,
    "risk.moderate_deploy.max_total_multiplier": _NON_NEG,
    "risk.moderate_deploy.max_boost": _NON_NEG,
    "risk.cash_policy.floor_pct_base": _PCT,
    "risk.cash_policy.floor_pct_min": _PCT,
    "risk.cash_policy.floor_pct_max": _PCT,
}


def test_boundary_table_covers_every_guarded_key():
    assert set(BOUNDARY_TABLE) == set(GUARDED_KEYS)


@pytest.mark.parametrize("path", sorted(BOUNDARY_TABLE))
def test_guarded_key_boundaries(path):
    valid, invalid = BOUNDARY_TABLE[path]
    for value in valid:
        validate_config_for_save(_nest(path, value))  # must not raise
    for value in invalid:
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config_for_save(_nest(path, value))
        assert exc_info.value.path == path
        assert exc_info.value.value is value or exc_info.value.value == value


@pytest.mark.parametrize("path", sorted(BOUNDARY_TABLE))
def test_guarded_key_absent_is_valid(path):
    """Partial tenant bodies never mention most keys — absence is not an error."""
    parent = path.rsplit(".", 1)[0] if "." in path else None
    validate_config_for_save({})
    if parent:
        validate_config_for_save(_nest(parent, {}))


def test_root_must_be_dict():
    for bad in (None, [], "cfg", 1):
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config_for_save(bad)
        assert exc_info.value.path == "<root>"


def test_error_is_value_error_with_readable_message():
    with pytest.raises(ValueError) as exc_info:
        validate_config_for_save({"max_open_positions": 3600})
    assert str(exc_info.value) == (
        f"config.max_open_positions=3600 rejected: must be in [0, {MAX_OPEN_POSITIONS_CAP}]"
    )


def test_current_repo_config_json_is_valid():
    """Regression guard: the live config.json must still save (read-only here)."""
    with open(os.path.join(REPO_ROOT, "config.json"), "r", encoding="utf-8") as f:
        cfg = json.load(f)
    validate_config_for_save(cfg)


# --- save_config wiring: default path (config.json) -------------------------


@pytest.fixture
def default_tenant(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(data_manager, "_config_cache", None)
    monkeypatch.setattr("core.tenant_context.resolve_tenant_id", lambda tenant_id=None: "default")
    return tmp_path


def test_default_save_rejects_invalid_and_writes_nothing(default_tenant):
    sentinel = {"sentinel": True}
    data_manager._config_cache = sentinel
    with pytest.raises(ConfigValidationError):
        _REAL_SAVE_CONFIG({"max_open_positions": 3600, "trading_mode": "live"})
    assert not (default_tenant / "config.json").exists()
    # no atomic-write temp file left behind either
    assert [p.name for p in default_tenant.iterdir() if p.name.startswith("config")] == []
    assert data_manager._config_cache is sentinel  # cache untouched


def test_default_save_accepts_boundary_valid_values(default_tenant):
    cfg = {
        "max_open_positions": MAX_OPEN_POSITIONS_CAP,
        "max_usdt_per_trade": 0.01,
        "trading_mode": "paper",
        "live": {"execution": "shadow", "max_usdt_per_trade": 4500},
        "costs": {"gate": {"spot": {"fee_maker_pct": 0, "fee_taker_pct": 100}}},
        "risk": {"max_daily_loss_pct": 100, "max_daily_sells": 0, "min_trade_usdt": 0},
    }
    assert _REAL_SAVE_CONFIG(cfg) is True
    with open(default_tenant / "config.json", "r", encoding="utf-8") as f:
        assert json.load(f) == cfg
    assert data_manager._config_cache is None


def test_default_save_validates_before_atomic_write(default_tenant, monkeypatch):
    calls = []
    monkeypatch.setattr(data_manager, "atomic_write_json", lambda p, d: calls.append((p, d)))
    with pytest.raises(ConfigValidationError):
        _REAL_SAVE_CONFIG({"risk": {"cash_floor_pct": 101}})
    assert calls == []
    assert _REAL_SAVE_CONFIG({"risk": {"cash_floor_pct": 100}}) is True
    assert calls == [("config.json", {"risk": {"cash_floor_pct": 100}})]


# --- save_config wiring: tenant path (tenant_meta_store) --------------------


@pytest.fixture
def tenant_mongo(monkeypatch):
    from storage import tenant_meta_store as tms

    calls = []
    monkeypatch.setattr("core.tenant_context.resolve_tenant_id", lambda tenant_id=None: tenant_id or "t1")
    monkeypatch.setattr(data_manager, "_load_default_config_from_disk", lambda: {"trading_mode": "demo"})
    monkeypatch.setattr(data_manager, "_should_use_mongo_for_tenant_config", lambda cfg=None: True)
    monkeypatch.setattr(data_manager, "_mongo_test_mode", lambda cfg=None: True)

    def fake_save(tid, body, *, default_cfg, test=False):
        calls.append((tid, body))
        return True

    monkeypatch.setattr(tms, "save_tenant_config", fake_save)
    return calls


def test_tenant_save_rejects_invalid_before_meta_store(tenant_mongo):
    with pytest.raises(ConfigValidationError) as exc_info:
        _REAL_SAVE_CONFIG({"live": {"execution": "yolo"}}, tenant_id="t1")
    assert exc_info.value.path == "live.execution"
    assert tenant_mongo == []


def test_tenant_save_partial_body_still_saves(tenant_mongo):
    body = {"virtual_trading": True, "max_open_positions": 0}
    assert _REAL_SAVE_CONFIG(body, tenant_id="t1") is True
    assert tenant_mongo == [("t1", body)]


def test_tenant_save_non_mongo_backend_still_validates(monkeypatch):
    """Even when the tenant path is a no-op (paper backend), invalid input is rejected.

    #456: the skipped write reports ``False`` (nothing was persisted) instead of
    the former silent ``True``.
    """
    monkeypatch.setattr("core.tenant_context.resolve_tenant_id", lambda tenant_id=None: "t2")
    monkeypatch.setattr(data_manager, "_load_default_config_from_disk", lambda: {})
    monkeypatch.setattr(data_manager, "_should_use_mongo_for_tenant_config", lambda cfg=None: False)
    with pytest.raises(ConfigValidationError):
        _REAL_SAVE_CONFIG({"risk": {"max_daily_buys": -1}}, tenant_id="t2")
    assert _REAL_SAVE_CONFIG({"risk": {"max_daily_buys": 0}}, tenant_id="t2") is False


# --- in-repo producers: every literal actually written must survive the guard --
#
# The enum is exact-match, so a producer writing a value outside TRADING_MODES /
# LIVE_EXECUTION_MODES would be silently blocked at save time (the "/mode off"
# kill switch was the first casualty). Run each producer and validate its output.


def _capture_mode_command(text: str, cfg: dict, env: dict | None = None) -> dict:
    """Run ``mode_commands.handle(text)`` and return the dict handed to the
    persist seam (``patch_config`` since #456 — only the changed keys)."""
    from unittest.mock import patch

    from notifications.telegram_commands import mode_commands

    saved: list[dict] = []

    def _fake_save(config, *args, **kwargs):
        saved.append(json.loads(json.dumps(config)))  # snapshot
        return True

    with patch.dict(os.environ, env or {}, clear=False), \
         patch.object(mode_commands, "get_config", side_effect=lambda: dict(cfg)), \
         patch.object(mode_commands, "patch_config", side_effect=_fake_save), \
         patch.object(mode_commands, "reload_config"), \
         patch.object(mode_commands, "on_trading_mode_change", return_value=""), \
         patch.object(mode_commands, "send_telegram_message"):
        assert mode_commands.handle(text) is True
    assert len(saved) == 1, f"{text} should save exactly once, saved {len(saved)}"
    return saved[0]


def test_producer_mode_off_kill_switch_survives_guard(monkeypatch):
    """mode_commands.py:115 — ``/mode off`` writes trading_mode="off"."""
    monkeypatch.delenv("DEMO_MODE", raising=False)
    body = _capture_mode_command("/mode off", {"trading_mode": "live", "virtual_trading": False})
    assert body["trading_mode"] == "off"
    assert body["virtual_trading"] is False
    validate_config_for_save(body)  # must not raise


def test_producer_live_confirm_survives_guard(monkeypatch):
    """mode_commands.py:157 — ``/live_confirm`` writes trading_mode="live"."""
    monkeypatch.delenv("DEMO_MODE", raising=False)
    cfg = {
        "trading_mode": "paper",
        "live": {"api_key_env": "T330_KEY", "api_secret_env": "T330_SECRET", "dry_run": True},
    }
    body = _capture_mode_command(
        "/live_confirm", cfg, env={"T330_KEY": "k", "T330_SECRET": "s"}
    )
    assert body["trading_mode"] == "live"
    assert body["live_confirmed"] is True
    validate_config_for_save(body)


@pytest.mark.parametrize("text", ["/mode paper", "/mode live", "/live_cancel"])
def test_producer_simulated_live_commands_survive_guard(text, monkeypatch):
    """mode_commands ``/mode paper|live`` and ``/live_cancel`` all persist
    ``simulated_live_config_updates()`` (core/simulated_trading.py:58)."""
    monkeypatch.delenv("DEMO_MODE", raising=False)
    body = _capture_mode_command(text, {"trading_mode": "off", "live": {"execution": "shadow"}})
    assert body["trading_mode"] == "live"
    validate_config_for_save(body)


def test_producer_simulated_live_config_updates_survives_guard():
    """core/simulated_trading.py:58"""
    from core.simulated_trading import simulated_live_config_updates

    body = simulated_live_config_updates({"live": {"execution": "shadow"}})
    assert body["trading_mode"] == "live"
    validate_config_for_save(body)
    validate_config_for_save(simulated_live_config_updates({}))


def test_producer_operator_like_tenant_config_survives_guard():
    """core/trading_profiles.py:137"""
    from core.trading_profiles import build_operator_like_tenant_config

    validate_config_for_save(build_operator_like_tenant_config({}))
    with open(os.path.join(REPO_ROOT, "config.json"), "r", encoding="utf-8") as f:
        repo_cfg = json.load(f)
    body = build_operator_like_tenant_config(repo_cfg)
    assert body["trading_mode"] == "live"
    validate_config_for_save(body)


@pytest.mark.parametrize("trading_mode", ["paper", "live", "demo", "gate_testnet", "off"])
def test_producer_tenant_seed_config_survives_guard(trading_mode):
    """core/trading_profiles.py:164 — default and every mode the code reads."""
    from core.trading_profiles import TRADING_PROFILE_PRESETS, build_tenant_seed_config

    validate_config_for_save(build_tenant_seed_config())  # default trading_mode="paper"
    for profile in TRADING_PROFILE_PRESETS:
        body = build_tenant_seed_config(profile, trading_mode=trading_mode)
        assert body["trading_mode"] == trading_mode
        validate_config_for_save(body)


def test_producer_tenant_registry_defaults_survive_guard():
    """storage/tenant_registry.py — per-tenant ``defaults`` seed ``trading_mode``."""
    import re

    with open(os.path.join(REPO_ROOT, "storage", "tenant_registry.py"), "r", encoding="utf-8") as f:
        src = f.read()
    modes = set(re.findall(r'"trading_mode":\s*"([^"]*)"', src))
    assert modes, "expected at least one trading_mode default in tenant_registry.py"
    for mode in modes:
        validate_config_for_save({"trading_mode": mode})
