"""#689: per-key lower bound for the liquidity-guard multiples.

Floors are read from base config.json through the disk loader. These cases
build that base in the test. Nothing is written under data/.
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import pytest

import data_manager
from core.config_guardrails import (
    ConfigValidationError,
    frozen_shorts_changed,
    pinned_patch_paths,
    reject_frozen_shorts_patch,
)
from core.models import TradeOrder
from core.tenant_context import tenant_context
from core.trading_profiles import TRADING_PROFILE_PRESETS, deep_merge_dicts
from risk.risk_manager import RiskManager
from services.venue_quality import (
    DEPTH_FLOOR_PATH,
    DEPTH_MULTIPLE_PATH,
    VOLUME_FLOOR_PATH,
    VOLUME_MULTIPLE_PATH,
    VenueMetrics,
    check_venue_for_buy,
    reset_venue_cache_for_tests,
    stamp_venue_for_fill,
)
from strategies.positions import clear_positions_memory
from tests.unit.test_issue_671_liquidity_cap import _book, _buy, _cfg, _eval

ROOT = Path(__file__).resolve().parents[2]
_REAL_SAVE = data_manager.save_config
assert _REAL_SAVE.__name__ == "save_config"

_DEPTH_FLOOR = "risk.liquidity_guard.depth_to_order_multiple_min"
_VOLUME_FLOOR = "risk.venue_quality.min_volume_to_order_multiple_min"
_DEPTH_MULTIPLE = "risk.liquidity_guard.depth_to_order_multiple"
_VOLUME_MULTIPLE = "risk.venue_quality.min_volume_to_order_multiple"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    clear_positions_memory()
    reset_venue_cache_for_tests()
    monkeypatch.setattr("services.venue_quality.log", lambda *a, **k: None)
    yield
    clear_positions_memory()
    reset_venue_cache_for_tests()


def _base(depth_floor=1, volume_floor=20) -> dict:
    return {
        "trading_mode": "paper",
        "risk": {
            "liquidity_guard": {"depth_to_order_multiple_min": depth_floor},
            "venue_quality": {"min_volume_to_order_multiple_min": volume_floor},
        },
    }


def _raw(*, depth=1, volume=20) -> dict:
    return {
        "trading_mode": "paper",
        "max_usdt_per_trade": 4500,
        "risk": {
            "min_trade_usdt": 15,
            "venue_quality": {
                "enabled": True,
                "min_quote_volume_24h_usdt": 0,
                "min_top_book_usdt_per_side": 0,
                "max_spread_pct": 50,
                "min_volume_to_order_multiple": volume,
            },
            "liquidity_guard": {
                "min_quote_volume_24h_usdt": 100,
                "depth_window_pct": 0.5,
                "order_book_cache_ttl_sec": 15,
                "depth_to_order_multiple": depth,
            },
        },
    }


def _patch_disk(monkeypatch, base: dict) -> None:
    monkeypatch.setattr(
        data_manager,
        "_load_default_config_from_disk",
        lambda: copy.deepcopy(base),
    )


def _lock(raw: dict, book: VenueMetrics | None = None, planned: float = 25):
    return check_venue_for_buy(
        "LAB/USDT",
        source="technical",
        planned_usdt=planned,
        config_raw=raw,
        metrics=book or _book(),
    )


def _blocked(result) -> None:
    assert result.ok is False
    assert result.code == "liq_guard_missing_input"
    assert result.cap_depth is None
    assert result.cap_volume is None
    assert result.liq_cap_usdt is None


def _floor_body(*, depth_floor=1, volume_floor=20, depth=1, volume=20) -> dict:
    return {
        "trading_mode": "paper",
        "max_open_positions": 5,
        "risk": {
            "long_hard_stop_max_loss_pct": 20,
            "liquidity_guard": {
                "depth_to_order_multiple_min": depth_floor,
                "depth_to_order_multiple": depth,
            },
            "venue_quality": {
                "min_volume_to_order_multiple_min": volume_floor,
                "min_volume_to_order_multiple": volume,
            },
        },
    }


def _arm_tenant(monkeypatch, base: dict, previous=None):
    saved: list = []
    monkeypatch.setattr(
        "core.tenant_context.resolve_tenant_id",
        lambda tenant_id=None: tenant_id or "t689",
    )
    _patch_disk(monkeypatch, base)
    monkeypatch.setattr(data_manager, "_should_use_mongo_for_tenant_config", lambda cfg=None: True)
    monkeypatch.setattr(data_manager, "_mongo_test_mode", lambda cfg=None: True)
    monkeypatch.setattr(
        data_manager,
        "_load_tenant_config_body",
        lambda tid, default_cfg: copy.deepcopy(previous) if previous is not None else None,
    )
    from storage import tenant_meta_store as tms

    monkeypatch.setattr(
        tms,
        "save_tenant_config",
        lambda *a, **k: saved.append(("save", a)) or True,
    )
    monkeypatch.setattr(
        tms,
        "patch_tenant_config",
        lambda *a, **k: saved.append(("patch", a)) or True,
    )
    return saved


def test_689_t1_depth_multiple_below_floor(monkeypatch):
    _patch_disk(monkeypatch, _base(depth_floor=1, volume_floor=20))
    result = _lock(_raw(depth=0.5, volume=20))
    _blocked(result)
    assert any(
        _DEPTH_MULTIPLE in reason and "0.5" in reason and "below floor 1" in reason
        for reason in result.reasons
    )


def test_689_t2_volume_multiple_below_floor(monkeypatch):
    _patch_disk(monkeypatch, _base())
    result = _lock(_raw(depth=1, volume=2))
    _blocked(result)
    assert any(
        _VOLUME_MULTIPLE in reason and "below floor 20" in reason
        for reason in result.reasons
    )


def test_689_t3_multiples_equal_to_floor_keep_the_size(monkeypatch):
    _patch_disk(monkeypatch, _base())
    rm = RiskManager(_cfg(depth_multiple=1, volume_multiple=20))
    dec = _eval(rm, _buy(usdt=25), "technical", _book(), planned=25)
    assert dec.approved is True, dec.message
    assert dec.order.usdt_amount == pytest.approx(25)
    assert dec.details["cap_depth"] == pytest.approx(325)
    assert dec.details["cap_volume"] == pytest.approx(30_000)


def test_689_t4_missing_floor_key_blocks(monkeypatch):
    for key in ("depth_to_order_multiple_min", "min_volume_to_order_multiple_min"):
        base = _base()
        if key == "depth_to_order_multiple_min":
            del base["risk"]["liquidity_guard"][key]
            floor_path = _DEPTH_FLOOR
        else:
            del base["risk"]["venue_quality"][key]
            floor_path = _VOLUME_FLOOR
        _patch_disk(monkeypatch, base)
        result = _lock(_raw())
        _blocked(result)
        assert f"liquidity guard floor missing: {floor_path}" in result.reasons


def test_689_t5_invalid_floor_blocks(monkeypatch):
    cases = (
        ("depth", math.nan),
        ("depth", math.inf),
        ("depth", 0),
        ("depth", -1),
        ("volume", "20"),
        ("depth", True),
    )
    for side, bad in cases:
        if side == "depth":
            base = _base(depth_floor=bad, volume_floor=20)
            floor_path = _DEPTH_FLOOR
        else:
            base = _base(depth_floor=1, volume_floor=bad)
            floor_path = _VOLUME_FLOOR
        _patch_disk(monkeypatch, base)
        result = _lock(_raw(depth=1, volume=20))
        _blocked(result)
        assert f"liquidity guard floor missing: {floor_path}" in result.reasons, (side, bad)


def test_689_t6_floor_comes_from_disk_not_tenant_or_preset(monkeypatch):
    passed = _raw(depth=0.5, volume=0.5)
    passed["risk"]["liquidity_guard"]["depth_to_order_multiple_min"] = 0.1
    passed["risk"]["venue_quality"]["min_volume_to_order_multiple_min"] = 0.1
    _patch_disk(monkeypatch, _base(depth_floor=1, volume_floor=20))
    monkeypatch.setattr(data_manager, "load_config", lambda tenant_id=None: copy.deepcopy(passed))
    result = _lock(passed)
    _blocked(result)
    assert any("below floor" in reason for reason in result.reasons)

    disk = _base(depth_floor=1, volume_floor=20)
    disk["trading_profile"] = "balanced"
    _patch_disk(monkeypatch, disk)
    preset = deep_merge_dicts(
        TRADING_PROFILE_PRESETS["balanced"],
        {"risk": {"liquidity_guard": {"depth_to_order_multiple_min": 0.1}}},
    )
    monkeypatch.setitem(TRADING_PROFILE_PRESETS, "balanced", preset)
    monkeypatch.setattr(data_manager, "load_config", data_manager.load_config)
    monkeypatch.setattr(data_manager, "_config_cache", None)
    # The second assignment above does not undo the first patch. Restore the
    # real loader function by grabbing it off the class of calls: re-bind the
    # name the implementation imports.
    real_load = _REAL_LOAD
    monkeypatch.setattr(data_manager, "load_config", real_load)
    passed_2 = _raw(depth=0.5, volume=20)
    result_2 = _lock(passed_2)
    _blocked(result_2)
    assert any(
        _DEPTH_MULTIPLE in reason and "below floor 1" in reason
        for reason in result_2.reasons
    )


_REAL_LOAD = data_manager.load_config


def test_689_t6b_disk_read_error_blocks(monkeypatch):
    def _boom():
        raise RuntimeError("config.json unreadable")

    monkeypatch.setattr(data_manager, "_load_default_config_from_disk", _boom)
    result = _lock(_raw())
    _blocked(result)
    assert f"liquidity guard floor missing: {_DEPTH_FLOOR}" in result.reasons
    assert f"liquidity guard floor missing: {_VOLUME_FLOOR}" in result.reasons


def test_689_t7a_volume_below_floor_blocks_when_depth_is_ok(monkeypatch):
    _patch_disk(monkeypatch, _base())
    result = _lock(_raw(depth=1, volume=2))
    _blocked(result)
    assert any(_VOLUME_MULTIPLE in reason and "below floor" in reason for reason in result.reasons)
    assert not any(_DEPTH_MULTIPLE in reason and "below floor" in reason for reason in result.reasons)


def test_689_t7b_depth_below_floor_blocks_when_volume_is_ok(monkeypatch):
    _patch_disk(monkeypatch, _base())
    result = _lock(_raw(depth=0.5, volume=20))
    _blocked(result)
    assert any(_DEPTH_MULTIPLE in reason and "below floor" in reason for reason in result.reasons)
    assert not any(_VOLUME_MULTIPLE in reason and "below floor" in reason for reason in result.reasons)


def test_689_t7c_both_below_floor_names_both_keys(monkeypatch):
    _patch_disk(monkeypatch, _base())
    result = _lock(_raw(depth=0.5, volume=2))
    _blocked(result)
    text = " ".join(result.reasons)
    assert _DEPTH_MULTIPLE in text
    assert _VOLUME_MULTIPLE in text


def test_689_t7d_missing_multiple_stays_on_the_existing_path(monkeypatch):
    _patch_disk(monkeypatch, _base())
    raw = _raw()
    del raw["risk"]["liquidity_guard"]["depth_to_order_multiple"]
    result = _lock(raw)
    _blocked(result)
    assert "liquidity guard config missing" in result.reasons
    assert not any("floor" in reason for reason in result.reasons)


def test_689_t8_stamp_path_ignores_the_floor():
    raw = _raw(depth=1, volume=2)
    thick = _book(bid=10_000, ask=10_000, qv=1000)
    stamp = stamp_venue_for_fill(
        "LAB/USDT", planned_usdt=25, config_raw=raw, metrics=thick
    )
    assert stamp["venue_ok"] is True
    assert stamp["venue_reasons"] == []

    thin = _book(bid=10_000, ask=10_000, qv=40)
    stamped_thin = stamp_venue_for_fill(
        "LAB/USDT", planned_usdt=25, config_raw=raw, metrics=thin
    )
    assert stamped_thin["venue_ok"] is False
    assert any("× order" in str(reason) for reason in stamped_thin["venue_reasons"])
    assert not any("below floor" in str(reason) for reason in stamped_thin["venue_reasons"])


def test_689_t9_repo_config_ships_the_floors():
    raw = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    depth_floor = raw["risk"]["liquidity_guard"]["depth_to_order_multiple_min"]
    volume_floor = raw["risk"]["venue_quality"]["min_volume_to_order_multiple_min"]
    assert depth_floor == 1
    assert volume_floor == 20
    assert raw["risk"]["liquidity_guard"]["depth_to_order_multiple"] >= depth_floor
    assert raw["risk"]["venue_quality"]["min_volume_to_order_multiple"] >= volume_floor
    assert "review:viktor" in raw["risk"]["liquidity_guard"]["_doc"]
    assert "review:viktor" in raw["risk"]["venue_quality"]["_doc"]


def _exits(monkeypatch):
    rm = RiskManager(_cfg())
    sell = TradeOrder("SELL", "LAB/USDT", 1.0, 10, signal="SELL_FULL", source="auto")
    sold = _eval(
        rm,
        sell,
        "auto",
        _book(),
        position={"amount": 10.0, "average_entry": 1.0, "side": "long"},
    )
    assert sold.approved is True, sold.message
    assert not str(sold.code).startswith("liq_guard_")
    stop = TradeOrder("SELL", "LAB/USDT", 1.0, 10, signal="SELL_STOP_FULL", source="exit_ws")
    stopped = _eval(
        rm,
        stop,
        "exit_ws",
        _book(),
        position={"amount": 10.0, "average_entry": 1.0, "side": "long"},
    )
    assert stopped.approved is True, stopped.message
    assert not str(stopped.code).startswith("liq_guard_")
    cover = TradeOrder(type="COVER", symbol="LAB/USDT", price=1.0, amount=8, signal="COVER", source="auto")
    covered = _eval(
        rm,
        cover,
        "auto",
        _book(),
        position={"amount": 8.0, "average_entry": 1.0, "side": "short", "leverage": 2},
    )
    assert covered.approved is True, covered.message
    assert covered.order.type == "COVER"


def test_689_t10_buy_blocks_sell_stop_cover_do_not(monkeypatch):
    _patch_disk(monkeypatch, {"risk": {}})
    rm = RiskManager(_cfg())
    blocked = _eval(rm, _buy(), "technical", _book(), planned=25)
    assert blocked.approved is False
    assert blocked.code == "liq_guard_missing_input"
    _exits(monkeypatch)

    def _boom():
        raise RuntimeError("config.json unreadable")

    monkeypatch.setattr(data_manager, "_load_default_config_from_disk", _boom)
    blocked_read = _eval(rm, _buy(), "technical", _book(), planned=25)
    assert blocked_read.approved is False
    assert blocked_read.code == "liq_guard_missing_input"
    _exits(monkeypatch)


def test_689_t11_short_open_below_floor_is_blocked(monkeypatch):
    _patch_disk(monkeypatch, _base())
    rm = RiskManager(_cfg(volume_floor=100, volume_multiple=2, shorts=True))
    order = TradeOrder(
        type="SHORT",
        symbol="LAB/USDT",
        price=1.0,
        amount=0,
        usdt_amount=100,
        signal="SHORT",
        source="auto",
        leverage=2,
    )
    dec = _eval(rm, order, "auto", _book(bid=100_000, ask=100_000, qv=10_000_000))
    assert dec.approved is False
    assert dec.code == "liq_guard_missing_input"
    assert "below floor" in dec.message


def test_689_t12_patch_rejects_floor_keys_for_default_and_tenant():
    depth = {"risk": {"liquidity_guard": {"depth_to_order_multiple_min": 2}}}
    volume = {"risk": {"venue_quality": {"min_volume_to_order_multiple_min": 19}}}
    for body in (depth, volume):
        with pytest.raises(ConfigValidationError, match="operator-pinned"):
            data_manager.patch_config(body)
        with pytest.raises(ConfigValidationError, match="operator-pinned"):
            data_manager.patch_config(body, tenant_id="t689")


def test_689_t13_floor_change_includes_missing_now(monkeypatch, tmp_path):
    path = _VOLUME_FLOOR
    prev = {"risk": {"venue_quality": {"min_volume_to_order_multiple_min": 20}}}
    lacking = {"risk": {"venue_quality": {}}}
    assert frozen_shorts_changed(lacking, prev, pinned_patch_paths()) == path
    changed = {"risk": {"venue_quality": {"min_volume_to_order_multiple_min": 19}}}
    assert frozen_shorts_changed(changed, prev, pinned_patch_paths()) == path

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(data_manager, "_config_cache", None)
    monkeypatch.setattr("core.tenant_context.resolve_tenant_id", lambda tenant_id=None: "default")
    previous = _floor_body(volume_floor=20)
    (tmp_path / "config.json").write_text(json.dumps(previous), encoding="utf-8")
    body = _floor_body(volume_floor=19)
    with pytest.raises(ConfigValidationError, match="operator-pinned") as ei:
        _REAL_SAVE(body)
    assert ei.value.path == path
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["risk"]["venue_quality"][
        "min_volume_to_order_multiple_min"
    ] == 20


def test_689_t14_validator_rejects_multiple_below_base_floor(monkeypatch, tmp_path):
    base = _floor_body()
    _patch_disk(monkeypatch, base)
    low = _floor_body(volume=2)
    with pytest.raises(ConfigValidationError, match="below floor"):
        from core.config_guardrails import validate_config_for_save

        validate_config_for_save(low)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(data_manager, "_config_cache", None)
    monkeypatch.setattr("core.tenant_context.resolve_tenant_id", lambda tenant_id=None: "default")
    (tmp_path / "config.json").write_text(json.dumps(base), encoding="utf-8")
    with pytest.raises(ConfigValidationError, match="below floor"):
        _REAL_SAVE(low)

    saved = _arm_tenant(monkeypatch, base, previous={})
    with pytest.raises(ConfigValidationError, match="below floor"):
        _REAL_SAVE(low, tenant_id="t689")
    assert saved == []
    with pytest.raises(ConfigValidationError, match="below floor"):
        data_manager.patch_config(
            {"risk": {"venue_quality": {"min_volume_to_order_multiple": 2}}},
            tenant_id="t689",
        )
    assert saved == []


def test_689_t14b_default_save_presence_rule(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(data_manager, "_config_cache", None)
    monkeypatch.setattr("core.tenant_context.resolve_tenant_id", lambda tenant_id=None: "default")
    _patch_disk(monkeypatch, _base())
    body = {
        "trading_mode": "paper",
        "max_open_positions": 5,
        "risk": {"long_hard_stop_max_loss_pct": 20},
    }
    with pytest.raises(ConfigValidationError, match="liquidity guard floor missing") as ei:
        _REAL_SAVE(body)
    assert ei.value.path in (_DEPTH_FLOOR, _VOLUME_FLOOR)
    assert not (tmp_path / "config.json").exists()


def test_689_t14c_tenant_save_sees_preset_multiple(monkeypatch):
    base = _base()
    base["risk"]["liquidity_guard"]["depth_to_order_multiple"] = 1
    base["risk"]["venue_quality"]["min_volume_to_order_multiple"] = 20
    preset = deep_merge_dicts(
        TRADING_PROFILE_PRESETS["balanced"],
        {"risk": {"venue_quality": {"min_volume_to_order_multiple": 2}}},
    )
    monkeypatch.setitem(TRADING_PROFILE_PRESETS, "balanced", preset)
    saved = _arm_tenant(monkeypatch, base, previous={})
    with pytest.raises(ConfigValidationError, match="below floor"):
        _REAL_SAVE({"trading_profile": "balanced"}, tenant_id="t689")
    assert saved == []


def test_689_t14d_validator_uses_base_floor_not_body_floor(monkeypatch):
    _patch_disk(monkeypatch, _base(depth_floor=1, volume_floor=20))
    saved = _arm_tenant(monkeypatch, _base(depth_floor=1, volume_floor=20), previous={})
    body = _floor_body(depth_floor=0.1, volume_floor=0.1, depth=0.5, volume=0.5)
    with pytest.raises(ConfigValidationError, match="below floor"):
        _REAL_SAVE(body, tenant_id="t689")
    assert saved == []


def test_689_t14e_unreadable_base_rejects_save(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(data_manager, "_config_cache", None)
    body = _floor_body()
    monkeypatch.setattr(
        "core.tenant_context.resolve_tenant_id",
        lambda tenant_id=None: tenant_id or "t689",
    )
    monkeypatch.setattr(data_manager, "_should_use_mongo_for_tenant_config", lambda cfg=None: True)
    with pytest.raises(ConfigValidationError):
        _REAL_SAVE(body, tenant_id="t689")
    monkeypatch.setattr("core.tenant_context.resolve_tenant_id", lambda tenant_id=None: "default")
    with pytest.raises(ConfigValidationError):
        _REAL_SAVE(body)


def test_689_t14f_tenant_save_allows_floor_copy_change(monkeypatch):
    base = _floor_body(volume_floor=20)
    previous = _floor_body(volume_floor=20)
    previous["shorts"] = {
        "allow_live": False,
        "leverage_cap": 2,
        "auto_after_sell": False,
        "climax_fade": {"enabled": True},
    }
    body = _floor_body(volume_floor=19)
    body["shorts"] = copy.deepcopy(previous["shorts"])
    saved = _arm_tenant(monkeypatch, base, previous=previous)
    assert _REAL_SAVE(body, tenant_id="t689") is True
    assert saved and saved[0][0] == "save"


def test_689_t15_warning_has_tenant_key_value_floor(monkeypatch):
    messages: list[tuple] = []

    def _capture(message, level="INFO"):
        messages.append((message, level))

    monkeypatch.setattr("services.venue_quality.log", _capture)
    _patch_disk(monkeypatch, _base())
    with tenant_context("floor-tenant"):
        result = _lock(_raw(depth=1, volume=2))
    _blocked(result)
    warnings = [text for text, level in messages if level == "WARNING"]
    assert warnings
    assert any(
        "floor-tenant" in text
        and _VOLUME_MULTIPLE in text
        and "value=2" in text
        and "floor=20" in text
        for text in warnings
    )


def test_689_t16_non_positive_multiple_uses_the_existing_path(monkeypatch):
    _patch_disk(monkeypatch, _base())
    for depth, volume in ((0, 20), (-1, 20), (1, 0), (1, -1)):
        result = _lock(_raw(depth=depth, volume=volume))
        _blocked(result)
        assert "liquidity guard config missing" in result.reasons
        assert not any("below floor" in reason for reason in result.reasons)


def test_689_t17_non_finite_quote_volume_blocks(monkeypatch):
    _patch_disk(monkeypatch, _base())
    raw = _raw()
    for bad in (math.nan, math.inf):
        book = _book()
        book = VenueMetrics(
            symbol=book.symbol,
            quote_volume_24h_usdt=bad,
            last=1.0,
            bid=0.999,
            ask=1.001,
            spread_pct=0.2,
            capture="ok",
            depth_bid_usdt=10_000,
            depth_ask_usdt=10_000,
            depth_parsed=True,
            quote_volume_present=True,
        )
        result = _lock(raw, book)
        _blocked(result)


def test_689_t18_shorts_save_pin_stays_on_both_branches(monkeypatch, tmp_path):
    shorts = {
        "allow_live": False,
        "leverage_cap": 2,
        "auto_after_sell": False,
        "climax_fade": {"enabled": True},
    }
    previous = _floor_body()
    previous["shorts"] = shorts

    def _case(body_shorts, path):
        body = _floor_body()
        body["shorts"] = body_shorts
        saved = _arm_tenant(monkeypatch, _floor_body(), previous=previous)
        with pytest.raises(ConfigValidationError, match="operator-pinned") as ei:
            _REAL_SAVE(body, tenant_id="t689")
        assert ei.value.path == path
        assert saved == []

    _case({**shorts, "allow_live": True}, "shorts.allow_live")
    dropped = {k: v for k, v in shorts.items() if k != "auto_after_sell"}
    _case(dropped, "shorts.auto_after_sell")
    _case({**shorts, "leverage_cap": 3}, "shorts.leverage_cap")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(data_manager, "_config_cache", None)
    monkeypatch.setattr("core.tenant_context.resolve_tenant_id", lambda tenant_id=None: "default")

    def _default_case(body_shorts, path):
        (tmp_path / "config.json").write_text(json.dumps(previous), encoding="utf-8")
        body = _floor_body()
        body["shorts"] = body_shorts
        with pytest.raises(ConfigValidationError, match="operator-pinned") as ei:
            _REAL_SAVE(body)
        assert ei.value.path == path

    _default_case({**shorts, "allow_live": True}, "shorts.allow_live")
    _default_case(dropped, "shorts.auto_after_sell")
    _default_case({**shorts, "leverage_cap": 3}, "shorts.leverage_cap")


@pytest.mark.parametrize(
    "key,value",
    (
        ("allow_live", True),
        ("leverage_cap", 4),
        ("auto_after_sell", True),
        ("climax_fade", {"min_return_pct": 5.0}),
    ),
)
def test_689_t18b_shorts_patch_rejects_every_key(key, value):
    with pytest.raises(ConfigValidationError, match="operator-pinned") as ei:
        reject_frozen_shorts_patch({"shorts": {key: value}})
    assert ei.value.path == f"shorts.{key}"


def test_689_c1_missing_or_invalid_floor_on_disk_rejects_save(monkeypatch, tmp_path):
    """A floor already on disk that is missing or invalid rejects the save.

    The body under test is otherwise valid, so this is not the presence rule
    on the body. NaN and inf cannot round-trip through JSON, so those two
    cases come back from the disk loader.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(data_manager, "_config_cache", None)
    monkeypatch.setattr("core.tenant_context.resolve_tenant_id", lambda tenant_id=None: "default")
    repair = _floor_body()

    disk = _floor_body()
    del disk["risk"]["liquidity_guard"]["depth_to_order_multiple_min"]
    (tmp_path / "config.json").write_text(json.dumps(disk), encoding="utf-8")
    with pytest.raises(ConfigValidationError, match="liquidity guard floor missing") as ei:
        _REAL_SAVE(repair)
    assert ei.value.path == _DEPTH_FLOOR
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert "depth_to_order_multiple_min" not in saved["risk"]["liquidity_guard"]

    for bad in (0, -1, True, "20"):
        disk = _floor_body()
        disk["risk"]["venue_quality"]["min_volume_to_order_multiple_min"] = bad
        (tmp_path / "config.json").write_text(json.dumps(disk), encoding="utf-8")
        with pytest.raises(ConfigValidationError, match="liquidity guard floor missing") as ei:
            _REAL_SAVE(repair)
        assert ei.value.path == _VOLUME_FLOOR, bad
        on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert on_disk["risk"]["venue_quality"]["min_volume_to_order_multiple_min"] == bad

    for bad in (math.nan, math.inf):
        disk = _floor_body()
        disk["risk"]["liquidity_guard"]["depth_to_order_multiple_min"] = bad
        _patch_disk(monkeypatch, disk)
        with pytest.raises(ConfigValidationError, match="liquidity guard floor missing") as ei:
            _REAL_SAVE(repair)
        assert ei.value.path == _DEPTH_FLOOR, bad

    tenant_disk = _floor_body()
    del tenant_disk["risk"]["venue_quality"]["min_volume_to_order_multiple_min"]
    saved_calls = _arm_tenant(monkeypatch, tenant_disk, previous={})
    with pytest.raises(ConfigValidationError, match="liquidity guard floor missing") as ei:
        _REAL_SAVE(repair, tenant_id="t689")
    assert ei.value.path == _VOLUME_FLOOR
    assert saved_calls == []


def test_689_c2_default_save_rejects_bool_floor(monkeypatch, tmp_path):
    """Default save rejects floor True even though the pin treats True as 1."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(data_manager, "_config_cache", None)
    monkeypatch.setattr("core.tenant_context.resolve_tenant_id", lambda tenant_id=None: "default")
    previous = _floor_body()
    (tmp_path / "config.json").write_text(json.dumps(previous), encoding="utf-8")
    body = _floor_body()
    body["risk"]["liquidity_guard"]["depth_to_order_multiple_min"] = True
    assert frozen_shorts_changed(body, previous, pinned_patch_paths()) is None
    with pytest.raises(ConfigValidationError, match="liquidity guard floor missing") as ei:
        _REAL_SAVE(body)
    assert ei.value.path == _DEPTH_FLOOR
    assert ei.value.value is True
    assert "operator-pinned" not in str(ei.value)
    on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert on_disk["risk"]["liquidity_guard"]["depth_to_order_multiple_min"] == 1
    assert isinstance(on_disk["risk"]["liquidity_guard"]["depth_to_order_multiple_min"], int)


def test_689_c3_non_positive_multiples_rejected_on_save(monkeypatch, tmp_path):
    from core.config_guardrails import validate_config_for_save

    _patch_disk(monkeypatch, _floor_body())
    cases = (
        (0, 20, _DEPTH_MULTIPLE),
        (-1, 20, _DEPTH_MULTIPLE),
        (1, 0, _VOLUME_MULTIPLE),
        (1, -1, _VOLUME_MULTIPLE),
    )
    for depth, volume, path in cases:
        with pytest.raises(ConfigValidationError, match="below floor") as ei:
            validate_config_for_save(_floor_body(depth=depth, volume=volume))
        assert ei.value.path == path

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(data_manager, "_config_cache", None)
    monkeypatch.setattr("core.tenant_context.resolve_tenant_id", lambda tenant_id=None: "default")
    previous = _floor_body()
    (tmp_path / "config.json").write_text(json.dumps(previous), encoding="utf-8")
    with pytest.raises(ConfigValidationError, match="below floor") as ei:
        _REAL_SAVE(_floor_body(volume=0))
    assert ei.value.path == _VOLUME_MULTIPLE
    on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert on_disk["risk"]["venue_quality"]["min_volume_to_order_multiple"] == 20


def test_689_c4_save_rejects_string_nan_and_bool_multiples(monkeypatch, tmp_path):
    """A present multiple that is a str, NaN/inf, or bool is rejected on save.

    Depth ``True`` and volume ``"20"`` would pass a numeric compare (``True == 1``,
    ``float("20") == 20``). The type check has to reject them first.
    """
    from core.config_guardrails import validate_config_for_save

    _patch_disk(monkeypatch, _floor_body())
    cases = (
        ("depth", True),
        ("depth", False),
        ("volume", "20"),
        ("volume", "2"),
        ("depth", math.nan),
        ("depth", math.inf),
        ("volume", math.nan),
    )
    for side, bad in cases:
        body = _floor_body()
        if side == "depth":
            body["risk"]["liquidity_guard"]["depth_to_order_multiple"] = bad
            path = _DEPTH_MULTIPLE
        else:
            body["risk"]["venue_quality"]["min_volume_to_order_multiple"] = bad
            path = _VOLUME_MULTIPLE
        with pytest.raises(ConfigValidationError, match="liquidity guard multiple invalid") as ei:
            validate_config_for_save(body)
        assert ei.value.path == path, (side, bad)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(data_manager, "_config_cache", None)
    monkeypatch.setattr("core.tenant_context.resolve_tenant_id", lambda tenant_id=None: "default")
    previous = _floor_body()
    (tmp_path / "config.json").write_text(json.dumps(previous), encoding="utf-8")
    body = _floor_body()
    body["risk"]["liquidity_guard"]["depth_to_order_multiple"] = True
    with pytest.raises(ConfigValidationError, match="liquidity guard multiple invalid") as ei:
        _REAL_SAVE(body)
    assert ei.value.path == _DEPTH_MULTIPLE
    on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert on_disk["risk"]["liquidity_guard"]["depth_to_order_multiple"] == 1

    saved_calls = _arm_tenant(monkeypatch, _floor_body(), previous=_floor_body())
    tenant_body = _floor_body()
    tenant_body["risk"]["venue_quality"]["min_volume_to_order_multiple"] = "20"
    with pytest.raises(ConfigValidationError, match="liquidity guard multiple invalid") as ei:
        _REAL_SAVE(tenant_body, tenant_id="t689")
    assert ei.value.path == _VOLUME_MULTIPLE
    assert saved_calls == []


def test_689_path_constants_match_the_spec():
    assert DEPTH_FLOOR_PATH == _DEPTH_FLOOR
    assert VOLUME_FLOOR_PATH == _VOLUME_FLOOR
    assert DEPTH_MULTIPLE_PATH == _DEPTH_MULTIPLE
    assert VOLUME_MULTIPLE_PATH == _VOLUME_MULTIPLE
