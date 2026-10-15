"""Issue #692 / spec v1.12.5. Fail-closed venue config. No code fallbacks."""

from __future__ import annotations

import ast
import copy
import json
import math
import os
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import data_manager
from core.config import BotConfig
from core.config_guardrails import (
    ConfigValidationError,
    assert_default_config_bounds_present,
    assert_default_config_floors_present,
    validate_config_for_save,
)
from core.models import TradeOrder
from services import shadow_signal_log as shadow
from services.venue_quality import (
    VenueConfigError,
    VenueQualityConfig,
    check_venue_for_buy,
    gate_public_rest_get,
    is_thin_venue_stamp,
    liquidity_multiple_floor_reasons,
    reset_venue_cache_for_tests,
    venue_quality_config,
)
from services.watchlist_quality.venue_batch import QUOTE_VOL_24H_KEY, attach_quote_volumes
from strategies.sensor_entry_policy import resolve_sensor_usdt, venue_gate_planned_usdt
from tests.support.venue_block import venue_block
from tests.unit.test_liquidity_guard import _book
from tests.unit.test_long_mcap_venue_563 import _buy, _cfg, _eval_env
from tests.unit.test_shadow_signal_log_687 import _passing_gate, _raw

_REAL_SAVE = data_manager.save_config
ROOT = Path(__file__).resolve().parents[2]
R2_KEYS = (
    "enabled",
    "exchange",
    "min_quote_volume_24h_usdt",
    "max_spread_pct",
    "min_top_book_usdt_per_side",
    "order_book_limit",
    "order_book_timeout_sec",
    "ticker_timeout_sec",
    "cache_ttl_sec",
    "book_unavailable_policy",
    "on_fetch_error",
    "min_volume_to_order_multiple",
)


def _disk_config() -> dict:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def _live_planned(raw: dict) -> float | None:
    """Run the live sensor call site and return the planned_usdt it passes."""
    from strategies.decision_engine import DecisionEngine

    captured: dict = {}

    def _check(*_args, **kwargs):
        captured["planned"] = kwargs.get("planned_usdt")
        return SimpleNamespace(ok=True, reasons=[])

    engine = DecisionEngine.__new__(DecisionEngine)
    engine.config = BotConfig(raw=raw)
    engine.market = SimpleNamespace(fetch_15m_sensor_metrics=lambda *_a, **_k: {})
    technical = SimpleNamespace(action="HOLD")
    market = SimpleNamespace(has_position=False)
    with (
        patch("strategies.watch_15m_state.is_watched", return_value=True),
        patch("services.venue_quality.check_venue_for_buy", _check),
        patch("core.config.get_bot_config", lambda *a, **k: BotConfig(raw=raw)),
        patch("data.cmc_market_cap.resolve_market_cap_usd", return_value=1_000_000),
        patch("price_fetcher.is_gate_tradeable", return_value=True),
    ):
        try:
            engine._apply_entry_sensor_buy("HOLD", [], 0.0, "LAB/USDT", market, technical)
        except Exception:
            pass
    return captured.get("planned")


def _trigger(raw, **kwargs):
    from tests.unit.test_shadow_signal_log_687 import _trigger as trigger

    return trigger([], raw, **kwargs)


# --- T8 / flag AST ----------------------------------------------------------


def test_t8_no_defaults_symbol_or_timeout_default():
    files = [
        ROOT / "services/venue_quality.py",
        ROOT / "services/shadow_signal_log.py",
        ROOT / "services/watchlist_quality/venue_batch.py",
        ROOT / "intelligence/memory/rebuild.py",
    ]
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "_DEFAULTS":
                pytest.fail(f"_DEFAULTS in {path}")
    src = (ROOT / "services/venue_quality.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "gate_public_rest_get":
            for arg in node.args.defaults + node.args.kw_defaults:
                assert arg is None or not (
                    isinstance(arg, ast.Constant) and arg.value == 12
                )
            timeout = [a.arg for a in node.args.kwonlyargs]
            assert "timeout" in timeout
            assert node.args.kw_defaults[timeout.index("timeout")] is None
    literal_keys = [
        key
        for key in R2_KEYS
        if key not in {"enabled", "exchange"}
    ]
    for path in files:
        text = path.read_text(encoding="utf-8")
        for key in literal_keys:
            assert f'.get("{key}",' not in text
            assert f".get('{key}'," not in text
        assert "or 90" not in text
        assert "_DEFAULTS" not in text


def test_skip_flag_ast_rules():
    """Keyword flag only from patch_config, plus the one save_config forward."""
    offenders = []
    for path in ROOT.rglob("*.py"):
        if "tests" in path.parts or ".venv" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            in_patch = False
            # Walk is flat; approximate by source line function via parent is hard.
            # Re-parse functions below.
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(fn):
                if not isinstance(call, ast.Call):
                    continue
                callee = call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", "")
                has_star = any(isinstance(a, ast.Starred) and isinstance(getattr(a, "value", None), ast.Name) or kw.arg is None for kw in [] )
                if any(kw.arg is None for kw in call.keywords) and callee in {
                    "save_config",
                    "validate_config_for_save",
                }:
                    if fn.name != "patch_config":
                        offenders.append(f"** {path}:{call.lineno} {fn.name}")
                for kw in call.keywords:
                    if kw.arg != "_skip_venue_bounds":
                        continue
                    allowed = fn.name == "patch_config" or (
                        fn.name == "save_config"
                        and path.name == "data_manager.py"
                        and callee == "validate_config_for_save"
                    )
                    if not allowed:
                        offenders.append(f"keyword {path}:{call.lineno} {fn.name}->{callee}")
            if fn.name not in {"patch_config", "save_config", "validate_config_for_save"}:
                for sub in ast.walk(fn):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and "_skip_venue_bounds" in sub.value:
                        offenders.append(f"string {path}:{sub.lineno}")
    assert offenders == []


# --- T19 planned reference --------------------------------------------------


def _sensor_raw(**over) -> dict:
    raw = _raw()
    raw["entry_sensor_15m"]["max_usdt_absolute"] = 1000
    raw["entry_sensor_15m"]["size_hint_by_mode"] = _disk_config()["entry_sensor_15m"]["size_hint_by_mode"]
    raw.update(over)
    return raw


def test_t19_planned_matches_live_call_site():
    hints = _disk_config()["entry_sensor_15m"]["size_hint_by_mode"]
    assert hints["GRID"]["volatile"] == 0.35
    assert hints["MOMENTUM"]["default"] == 0.3
    assert _disk_config()["entry_sensor_15m"]["max_usdt_absolute"] == 1000

    # (a) 150 GRID/volatile → 52.5, mode does not change the live argument
    raw_a = _sensor_raw(max_usdt_per_trade=150)
    assert _live_planned(raw_a) == pytest.approx(52.5)
    row_a = _trigger(raw_a)
    assert row_a["planned_order_usdt"] == pytest.approx(52.5)
    sized = resolve_sensor_usdt(
        "GRID", volatility_tier="volatile", max_usdt_per_trade=150, cfg=raw_a["entry_sensor_15m"]
    )
    assert sized == pytest.approx(52.5)

    # (b) MOMENTUM hint 0.30 differs; gate stays 52.5
    raw_b = _sensor_raw(max_usdt_per_trade=150)
    assert _live_planned(raw_b) == pytest.approx(52.5)
    assert _trigger(raw_b, symbol="B/USDT")["planned_order_usdt"] == pytest.approx(52.5)
    momentum = resolve_sensor_usdt(
        "MOMENTUM", volatility_tier="volatile", max_usdt_per_trade=150, cfg=raw_b["entry_sensor_15m"]
    )
    assert momentum != pytest.approx(52.5)

    # (c) 4500 GRID/volatile → 1575; absolute cap binds the sizer at 1000
    raw_c = _sensor_raw(max_usdt_per_trade=4500)
    assert _live_planned(raw_c) == pytest.approx(1575)
    assert _trigger(raw_c, symbol="C/USDT")["planned_order_usdt"] == pytest.approx(1575)
    capped_size = resolve_sensor_usdt(
        "GRID", volatility_tier="volatile", max_usdt_per_trade=4500, cfg=raw_c["entry_sensor_15m"]
    )
    assert capped_size == pytest.approx(1000)

    # (d) non-empty, key absent → 52.5
    raw_d = _sensor_raw()
    raw_d.pop("max_usdt_per_trade")
    assert _live_planned(raw_d) == pytest.approx(52.5)
    assert _trigger(raw_d, symbol="D/USDT")["planned_order_usdt"] == pytest.approx(52.5)

    # (e) max 0 → 175 via the accessor's or 500
    raw_e = _sensor_raw(max_usdt_per_trade=0)
    assert venue_gate_planned_usdt(0) == pytest.approx(175)
    assert _live_planned(raw_e) == pytest.approx(175)
    assert _trigger(raw_e, symbol="E/USDT")["planned_order_usdt"] == pytest.approx(175)

    # (f) negative, NaN, inf
    for bad in (-1, float("nan"), float("inf")):
        raw_f = _sensor_raw(max_usdt_per_trade=bad)
        row = _trigger(raw_f, symbol=f"F{bad}/USDT")
        assert row["planned_order_usdt"] is None
        assert row["venue_liquidity_null_reason"] == "planned_order_unavailable"

    # (g) accessor raises
    with patch(
        "strategies.sensor_entry_policy.venue_gate_planned_usdt",
        side_effect=RuntimeError("accessor"),
    ):
        row = _trigger(_sensor_raw(), symbol="G/USDT")
    assert row["planned_order_usdt"] is None
    assert row["venue_liquidity_null_reason"] == "planned_order_unavailable"

    # (h) None, non-dict, {} — accessor not called
    calls = {"n": 0}

    def _count(ticket):
        calls["n"] += 1
        return venue_gate_planned_usdt(ticket)

    with patch("strategies.sensor_entry_policy.venue_gate_planned_usdt", _count):
        for raw_h, sym in ((None, "H1/USDT"), ("nope", "H2/USDT"), ({}, "H3/USDT")):
            row = shadow.log_shadow_trigger(
                symbol=sym,
                timeframe="4h",
                price=1.25,
                price_source="gate_spot_last",
                rationale="x",
                action="BUY",
                sensor_policy_source="mode",
                config_raw=raw_h,
                gate_cache=_passing_gate(),
                sensor_cfg={"shadow_exit_hours": 4},
            )
            assert row is not None
            assert row["planned_order_usdt"] is None
    assert calls["n"] == 0

    # (i) patched accessor is what live and shadow pass
    def _fixed(_ticket):
        return 123.0

    with (
        patch("strategies.sensor_entry_policy.venue_gate_planned_usdt", _fixed),
        patch(
            "strategies.decision_engine.venue_gate_planned_usdt",
            _fixed,
            create=True,
        ),
    ):
        assert _live_planned(_sensor_raw()) == pytest.approx(123.0)
        assert _trigger(_sensor_raw(), symbol="I/USDT")["planned_order_usdt"] == pytest.approx(123.0)

    # (j) explicit None
    raw_j = _sensor_raw(max_usdt_per_trade=None)
    row = _trigger(raw_j, symbol="J/USDT")
    assert row["planned_order_usdt"] is None
    assert row["venue_liquidity_null_reason"] == "planned_order_unavailable"
    assert row["planned_order_usdt"] != pytest.approx(175)

    # (k) bad absolute cap does not blank a live value
    raw_k = _sensor_raw(max_usdt_per_trade=150)
    raw_k["entry_sensor_15m"]["max_usdt_absolute"] = "x"
    assert _live_planned(raw_k) == pytest.approx(52.5)
    assert _trigger(raw_k, symbol="K/USDT")["planned_order_usdt"] == pytest.approx(52.5)

    # (l) planned helper patched to raise still writes a row
    with patch.object(shadow, "_planned_order_usdt", side_effect=RuntimeError("boom")):
        row = _trigger(_sensor_raw(), symbol="L/USDT")
    assert row is not None
    assert row["venue_liquidity_null_reason"] == "planned_order_unavailable"

    # (m) patched property, not a copied .get default
    with patch.object(BotConfig, "max_usdt_per_trade", new=property(lambda self: 77.0)):
        assert _live_planned(_sensor_raw(max_usdt_per_trade=150)) == pytest.approx(26.95)
        assert _trigger(_sensor_raw(max_usdt_per_trade=150), symbol="M/USDT")[
            "planned_order_usdt"
        ] == pytest.approx(26.95)


def test_t19_i_live_and_shadow_use_the_accessor():
    """(i) stays red when the live call site stops calling the accessor."""

    def _fixed(_ticket):
        return 123.0

    with (
        patch("strategies.sensor_entry_policy.venue_gate_planned_usdt", _fixed),
        patch("strategies.decision_engine.venue_gate_planned_usdt", _fixed, create=True),
    ):
        assert _live_planned(_sensor_raw()) == pytest.approx(123.0)
        assert _trigger(_sensor_raw(), symbol="I2/USDT")["planned_order_usdt"] == pytest.approx(123.0)


def test_t19_m_property_not_copied_get():
    """(m) stays red when shadow copies ``config_raw.get(..., 150)``."""
    with patch.object(BotConfig, "max_usdt_per_trade", new=property(lambda self: 77.0)):
        assert _live_planned(_sensor_raw(max_usdt_per_trade=150)) == pytest.approx(26.95)
        assert _trigger(_sensor_raw(max_usdt_per_trade=150), symbol="M2/USDT")[
            "planned_order_usdt"
        ] == pytest.approx(26.95)


# --- T13 shadow cap / passed ------------------------------------------------


def test_t13_invalid_config_does_not_log_passed(monkeypatch):
    import services.venue_quality as venue

    messages = []
    monkeypatch.setattr(shadow, "log", lambda msg, level="INFO": messages.append(f"{level}:{msg}"))
    before = set(venue._CONFIG_WARNED)
    raw = _raw()
    raw["risk"]["venue_quality"].pop("min_volume_to_order_multiple")
    row = _trigger(raw, symbol="MISS/USDT")
    assert set(venue._CONFIG_WARNED) == before
    assert row["gates"]["venue_liquidity"] is None
    assert row["venue_liquidity_null_reason"] == "venue_config_invalid"
    assert row["venue_config_problem"] == "missing"
    assert row["gates"]["venue_liquidity"] is not True
    scrubbed = [msg.replace("gates_passed", "") for msg in messages]
    assert all("passed" not in msg.lower() for msg in scrubbed)


def test_t13_below_floor_is_range_and_missing_floor_is_missing(monkeypatch):
    raw = _raw()
    raw["risk"]["venue_quality"]["min_volume_to_order_multiple"] = 19
    low = _trigger(raw, symbol="LOW/USDT")
    assert low["venue_liquidity_null_reason"] == "venue_config_invalid"
    assert low["venue_config_problem"] == "range"
    assert low["gates"]["venue_liquidity"] is not True

    base = _disk_config()
    base["risk"]["venue_quality"].pop("min_volume_to_order_multiple_min")
    monkeypatch.setattr(shadow, "_BASE_STAT", None)
    monkeypatch.setattr(shadow, "_BASE_CFG", None)
    monkeypatch.setattr(data_manager, "_load_default_config_from_disk", lambda: base)
    # Force a stat miss by pointing stat at a changed pair via a temp file.
    gone = _trigger(_raw(), symbol="FLOOR/USDT")
    assert gone["venue_liquidity_null_reason"] == "venue_config_invalid"
    assert gone["venue_config_problem"] == "missing"
    assert gone["venue_liquidity_null_reason"] != "missing_floor"


def test_t13_volume_floor_absent_is_missing_floor():
    raw = _raw()
    raw["risk"]["liquidity_guard"].pop("min_quote_volume_24h_usdt")
    row = _trigger(raw, symbol="VOL/USDT", gate=_passing_gate())
    assert row["venue_liquidity_null_reason"] == "missing_floor"
    assert row["venue_liquidity_missing_key"] == "risk.liquidity_guard.min_quote_volume_24h_usdt"


def test_t13_base_reload_changes_sha(monkeypatch, tmp_path):
    src = _disk_config()
    path = tmp_path / "config.json"
    path.write_text(json.dumps(src), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    shadow._BASE_STAT = None
    shadow._BASE_CFG = None
    first = _trigger(_raw(), symbol="SHA1/USDT")
    assert first["base_config_sha"]
    assert first["venue_gate_version"] == "e12v8"
    assert first["cap_basis"] == "volume_only"
    src["risk"]["venue_quality"]["min_volume_to_order_multiple_min"] = 25
    path.write_text(json.dumps(src), encoding="utf-8")
    ns = time.time_ns()
    os.utime(path, ns=(ns, ns + 10**9))
    second = _trigger(_raw(), symbol="SHA2/USDT")
    assert second["venue_config_problem"] == "range"
    assert second["base_config_sha"] != first["base_config_sha"]


def test_t13_unreadable_base_is_not_cached(monkeypatch):
    real_stat = os.stat

    def _stat(path, *args, **kwargs):
        if str(path).endswith("config.json"):
            raise RuntimeError("disk")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(shadow, "_BASE_STAT", None)
    monkeypatch.setattr(shadow, "_BASE_CFG", None)
    monkeypatch.setattr(os, "stat", _stat)
    row = _trigger(_raw(), symbol="BAD/USDT")
    assert row["venue_liquidity_null_reason"] == "venue_config_invalid"
    assert row["venue_config_problem"] == "config_unavailable"
    assert row["base_config_sha"] is None
    assert shadow._BASE_CFG is None


# --- T2 / T6 / T15 / T9 -----------------------------------------------------


def test_t2_missing_key_blocks_buy_and_short():
    reset_venue_cache_for_tests()
    raw_risk = _cfg().raw
    block = dict(raw_risk["risk"]["venue_quality"])
    block.pop("exchange")
    raw_risk["risk"]["venue_quality"] = block
    from risk.risk_manager import RiskManager

    rm = RiskManager(BotConfig(raw_risk))
    with _eval_env(rm, metrics=_book(symbol="LAB/USDT", quote_volume_24h_usdt=5_000_000)):
        dec = rm.evaluate(_buy(source="technical"), "4h", source="technical")
    assert dec.approved is False
    assert dec.code == "liq_guard_missing_input"
    assert dec.details["reason"] == "venue_config_missing"
    assert dec.details["config_problem"] == "missing"
    assert "exchange" in dec.details["config_key"]


def test_t6_short_non_dict_raw_does_not_read_global(monkeypatch):
    from risk.risk_manager import RiskManager

    called = {"n": 0}
    real = venue_quality_config

    def _spy(config_raw=None):
        called["n"] += 1
        called["raw"] = config_raw
        return real(config_raw)

    monkeypatch.setattr("services.venue_quality.venue_quality_config", _spy)
    monkeypatch.setattr("core.simulated_trading.is_real_live_trading", lambda *_a, **_k: False)
    monkeypatch.setattr("strategies.short_policy.shorts_enabled", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "strategies.short_policy.resolve_short_params",
        lambda **_k: {
            "leverage": 2,
            "leverage_cap": 2,
            "max_open": 6,
            "max_margin_pct": 0,
            "market_cap_min_usd": 0,
        },
    )
    rm = RiskManager(_cfg())
    rm.config._raw = "not-a-dict"
    monkeypatch.setattr(rm, "_base_usdt_cap", lambda: 0.0)
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
    with _eval_env(rm, metrics=_book()):
        dec = rm._evaluate_short_or_cover(order, "4h", source="auto")
    assert dec.code == "liq_guard_missing_input"
    assert dec.details["config_problem"] == "config_unavailable"
    assert called["n"] == 0


def test_t15_manual_buy_logs_and_manual_short_skips(monkeypatch):
    from risk.risk_manager import RiskManager

    logged = []
    monkeypatch.setattr("logger.log", lambda msg, level="INFO": logged.append(msg))
    raw = _cfg().raw
    raw["risk"]["venue_quality"].pop("enabled")
    rm = RiskManager(BotConfig(raw))
    with _eval_env(rm, metrics=_book()):
        buy = rm.evaluate(_buy(source="manual"), "4h", source="manual")
    assert buy.approved is True
    assert buy.code != "liq_guard_missing_input"
    assert any("manual_buy_guard" in str(m) for m in logged)
    venue_calls = {"n": 0}
    real_check = check_venue_for_buy

    def _spy(*args, **kwargs):
        venue_calls["n"] += 1
        return real_check(*args, **kwargs)

    monkeypatch.setattr("services.venue_quality.check_venue_for_buy", _spy)
    short = TradeOrder(
        type="SHORT",
        symbol="LAB/USDT",
        price=1.0,
        amount=0,
        usdt_amount=100,
        signal="SHORT",
        source="manual",
        leverage=2,
    )
    raw["shorts"]["volatile"]["market_cap_min_usd"] = 0
    raw["shorts"]["stable"]["market_cap_min_usd"] = 0
    rm_short = RiskManager(BotConfig(raw))
    monkeypatch.setattr("core.simulated_trading.is_real_live_trading", lambda *_a, **_k: False)
    monkeypatch.setattr("strategies.short_policy.shorts_enabled", lambda *_a, **_k: True)
    with _eval_env(rm_short, metrics=_book()):
        short_dec = rm_short.evaluate(short, "4h", source="manual")
    assert short_dec.approved is True
    assert not str(short_dec.code or "").startswith("liq_guard_")
    assert venue_calls["n"] == 0


def test_t9_rearm_and_floor_warning_once(monkeypatch):
    reset_venue_cache_for_tests()
    lines = []
    monkeypatch.setattr("services.venue_quality.log", lambda msg, level="INFO": lines.append((level, msg)))
    bad = {"risk": {"venue_quality": venue_block(cache_ttl_sec="x")}}
    assert isinstance(venue_quality_config(bad), VenueConfigError)
    assert isinstance(venue_quality_config(bad), VenueConfigError)
    errors = [m for lvl, m in lines if lvl == "ERROR"]
    assert len(errors) == 1
    good = {"risk": {"venue_quality": venue_block()}}
    assert isinstance(venue_quality_config(good), VenueQualityConfig)
    assert isinstance(venue_quality_config(bad), VenueConfigError)
    errors = [m for lvl, m in lines if lvl == "ERROR"]
    assert len(errors) == 2
    reset_venue_cache_for_tests()
    lines.clear()
    liquidity_multiple_floor_reasons(1, 1)
    liquidity_multiple_floor_reasons(1, 1)
    warns = [m for lvl, m in lines if lvl == "WARNING"]
    assert len(warns) == 1


def test_t7_rebuild_omits_venue_and_batch_writes_none():
    from intelligence.memory.models import TradeMemory
    from intelligence.memory.rebuild import compute_profile_from_trades

    profile = compute_profile_from_trades(
        "LAB/USDT",
        [
            TradeMemory(
                trade_id="t1",
                symbol="LAB/USDT",
                direction="buy",
                source="technical",
                metadata={"venue": {"capture": "ok", "quote_volume_24h_usdt": 1}},
            )
        ],
        ledger_scope="paper",
        tenant_id="t",
        config_raw={"risk": {"venue_quality": {"enabled": "no"}}},
    )
    assert "venue" not in (profile.features or {})
    metrics = SimpleNamespace(capture="missing", quote_volume_24h_usdt=0.0)
    rows = attach_quote_volumes([{"symbol": "LAB/USDT"}], batch_fn=lambda symbols: {"LAB/USDT": metrics})
    assert rows[0][QUOTE_VOL_24H_KEY] is None
    assert rows[0][QUOTE_VOL_24H_KEY] != 0.0


def test_t14_freeze_helper_is_shared():
    from strategies.decision_engine import freeze_tier_on_entry

    assert freeze_tier_on_entry({}) is True
    assert freeze_tier_on_entry({"freeze_tier_on_entry": False}) is False
    with patch("strategies.decision_engine.freeze_tier_on_entry", return_value=False):
        stops = shadow._stops_from_exit_path(_raw(), "LAB/USDT", "4h", atr_pct=12)
    assert stops.get("stop_projection_null_reason") is None
    assert stops["volatility_tier"] is None
    text = (ROOT / "services/shadow_signal_log.py").read_text(encoding="utf-8")
    assert "__defaults__" not in text
    assert "_resolve_volatility_tier" not in text


def test_t22_floor_and_bounds_presence_are_separate():
    cfg = _disk_config()
    assert_default_config_floors_present(cfg)
    assert_default_config_bounds_present(cfg)
    missing_floor = copy.deepcopy(cfg)
    missing_floor["risk"]["liquidity_guard"].pop("depth_to_order_multiple_min")
    with pytest.raises(ConfigValidationError):
        assert_default_config_floors_present(missing_floor)
    missing_bound = copy.deepcopy(cfg)
    missing_bound["risk"]["venue_quality_bounds"].pop("order_book_limit_max")
    with pytest.raises(ConfigValidationError):
        assert_default_config_bounds_present(missing_bound)
    # Floor function does not care that bounds are gone.
    no_bounds = copy.deepcopy(cfg)
    no_bounds["risk"].pop("venue_quality_bounds")
    assert_default_config_floors_present(no_bounds)


def test_gate_timeout_keyword_is_required():
    with pytest.raises(TypeError):
        gate_public_rest_get("/spot/tickers", {})


def test_t1_http_params_from_validator(monkeypatch):
    seen = []

    def fake(path, params=None, *, timeout):
        seen.append((path, dict(params or {}), float(timeout)))
        if path == "/spot/tickers" and not (params or {}).get("currency_pair"):
            return [
                {
                    "currency_pair": "LAB_USDT",
                    "last": "1",
                    "highest_bid": "0.99",
                    "lowest_ask": "1.01",
                    "quote_volume": "8000000",
                    "highest_size": "1000",
                    "lowest_size": "1000",
                }
            ]
        if path == "/spot/order_book":
            return {"bids": [["0.99", "1000"]], "asks": [["1.01", "1000"]]}
        raise AssertionError(path)

    monkeypatch.setattr("services.venue_quality.gate_public_rest_get", fake)
    from services.venue_quality import fetch_gate_venue_metrics

    raw = {"risk": {"venue_quality": venue_block(), "liquidity_guard": {
        "min_quote_volume_24h_usdt": 500000,
        "depth_window_pct": 0.5,
        "order_book_cache_ttl_sec": 15,
        "depth_to_order_multiple": 1,
    }}}
    fetch_gate_venue_metrics(["LAB/USDT"], config_raw=raw, fetch_depth=True)
    books = [row for row in seen if row[0] == "/spot/order_book"]
    bulks = [row for row in seen if row[0] == "/spot/tickers" and not row[1].get("currency_pair")]
    assert books and int(books[0][1]["limit"]) == 100
    assert books[0][2] == pytest.approx(5.0)
    assert bulks and bulks[0][2] == pytest.approx(12.0)


def test_e11_structural_reasons_are_only_volume_and_floor():
    assert set(shadow._VENUE_NULL_REASONS) == {"missing_quote_volume", "missing_floor"}
    assert "venue_config_invalid" not in shadow._VENUE_NULL_REASONS
    assert "planned_order_unavailable" not in shadow._VENUE_NULL_REASONS
    assert "missing_depth_input" not in shadow._VENUE_NULL_REASONS


class _Pop:
    """Sentinel: delete the liquidity-guard key instead of setting a value."""


_POP = _Pop()


def _guard_raw(**overrides) -> dict:
    raw = _raw()
    guard = raw["risk"]["liquidity_guard"]
    for key, value in overrides.items():
        if value is _POP:
            guard.pop(key, None)
        else:
            guard[key] = value
    return raw


def _live_hard(raw: dict, metrics, planned: float = 100.0):
    from services.venue_quality import VenueQualityResult, _apply_hard_liquidity

    prior = VenueQualityResult(ok=True, reasons=[], metrics=metrics)
    return _apply_hard_liquidity(prior, metrics, planned_usdt=planned, config_raw=raw)


def _shadow_state(raw: dict, cache: dict):
    shadow._BASE_STAT = None
    shadow._BASE_CFG = None
    return shadow._evaluate_shadow_venue(cache, raw)


@pytest.mark.parametrize(
    ("key", "value", "problem"),
    [
        ("depth_window_pct", _POP, "missing"),
        ("depth_window_pct", "x", "type"),
        ("depth_window_pct", 0, "range"),
        ("depth_window_pct", -1, "range"),
        ("depth_window_pct", float("inf"), "range"),
        ("order_book_cache_ttl_sec", _POP, "missing"),
        ("order_book_cache_ttl_sec", "x", "type"),
        ("order_book_cache_ttl_sec", 0, "range"),
        ("order_book_cache_ttl_sec", -1, "range"),
        ("order_book_cache_ttl_sec", float("inf"), "range"),
        ("depth_to_order_multiple", _POP, "missing"),
        ("depth_to_order_multiple", "x", "type"),
        ("depth_to_order_multiple", 0, "range"),
        ("depth_to_order_multiple", -1, "range"),
        ("depth_to_order_multiple", 0.5, "range"),
    ],
)
def test_t22_bad_liquidity_config_blocks_live_and_shadow(key, value, problem):
    raw = _guard_raw(**{key: value})
    cache = _passing_gate(planned_order_usdt=100)
    metrics = _book(symbol="LAB/USDT", quote_volume_24h_usdt=1_000_000)
    live = _live_hard(raw, metrics)
    state = _shadow_state(raw, cache)
    assert live.ok is False
    assert live.code == "liq_guard_missing_input"
    assert state.value is not True
    assert state.reason == "venue_config_invalid"
    assert state.config_problem == problem
    assert key in (state.config_key or "")


def test_t22_stale_unparsed_and_missing_side_are_missing_depth_input():
    raw = _guard_raw()
    cases = [
        _passing_gate(planned_order_usdt=100, capture="stale", book_age_sec=100),
        _passing_gate(planned_order_usdt=100, capture="ok", book_age_sec=100),
        _passing_gate(planned_order_usdt=100, depth_parsed=False),
        _passing_gate(planned_order_usdt=100, depth_bid_usdt=None),
        _passing_gate(planned_order_usdt=100, depth_ask_usdt=None),
    ]
    for cache in cases:
        metrics = SimpleNamespace(
            symbol="LAB/USDT",
            capture=cache["capture"],
            quote_volume_present=True,
            quote_volume_24h_usdt=float(cache["quote_volume_24h_usdt"]),
            depth_parsed=cache["depth_parsed"] is True,
            depth_bid_usdt=cache.get("depth_bid_usdt"),
            depth_ask_usdt=cache.get("depth_ask_usdt"),
        )
        state = _shadow_state(raw, cache)
        assert state.value is not True
        assert state.reason == "missing_depth_input"
        # Age alone is a shadow input. Live blocks when capture, parse, or a side is bad.
        if cache["capture"] == "stale" or cache["depth_parsed"] is not True or cache.get("depth_bid_usdt") is None or cache.get("depth_ask_usdt") is None:
            live = _live_hard(raw, metrics)
            assert live.ok is False
            assert live.code == "liq_guard_missing_input"


def test_t22_positive_control_and_volume_below_floor():
    raw = _guard_raw()
    cache = _passing_gate(planned_order_usdt=100, quote_volume_24h_usdt=1_000_000)
    metrics = _book(symbol="LAB/USDT", quote_volume_24h_usdt=1_000_000)
    live = _live_hard(raw, metrics, planned=100)
    state = _shadow_state(raw, cache)
    assert live.ok is True
    assert state.value is True
    assert state.reason is None
    checked = check_venue_for_buy(
        "LAB/USDT", planned_usdt=100, config_raw=raw, metrics=metrics
    )
    assert checked.ok is True

    thin_cache = _passing_gate(planned_order_usdt=100, quote_volume_24h_usdt=10)
    thin = _book(symbol="LAB/USDT", quote_volume_24h_usdt=10)
    live_thin = _live_hard(raw, thin, planned=100)
    state_thin = _shadow_state(raw, thin_cache)
    assert live_thin.ok is False
    assert "liq_guard_volume_low" in (getattr(live_thin, "guard_codes", None) or [live_thin.code])
    assert state_thin.value is False
    assert state_thin.reason is None


def test_t13_missing_depth_multiple_does_not_pass(monkeypatch):
    messages = []
    monkeypatch.setattr(shadow, "log", lambda msg, level="INFO": messages.append(f"{level}:{msg}"))
    raw = _guard_raw(depth_to_order_multiple=_POP)
    row = _trigger(raw, symbol="DEPTH/USDT")
    assert row["gates"]["venue_liquidity"] is not True
    assert row["venue_liquidity_null_reason"] == "venue_config_invalid"
    scrubbed = [msg.replace("gates_passed", "") for msg in messages]
    assert all("passed" not in msg.lower() for msg in scrubbed)


def test_t24_rank_pairs_and_gate_version():
    invalid = _guard_raw(depth_window_pct=_POP)
    no_volume = _passing_gate()
    no_volume.pop("quote_volume_24h_usdt")
    assert _shadow_state(invalid, no_volume).reason == "venue_config_invalid"

    no_floor = _guard_raw(min_quote_volume_24h_usdt=_POP)
    assert _shadow_state(no_floor, no_volume).reason == "missing_quote_volume"

    present_volume = _passing_gate(quote_volume_24h_usdt=1_000_000)
    for field in ("capture", "book_age_sec", "depth_parsed", "depth_bid_usdt", "depth_ask_usdt"):
        present_volume.pop(field, None)
    assert _shadow_state(no_floor, present_volume).reason == "missing_floor"

    no_planned = dict(present_volume)
    assert _shadow_state(_guard_raw(), no_planned).reason == "missing_depth_input"

    row = _trigger(_raw(), symbol="VER/USDT")
    assert row["venue_gate_version"] == "e12v8"
    assert row["cap_basis"] == "volume_only"


def test_t1_pair_fallback_ttl_edges_book_branches_and_cap(monkeypatch):
    """Stored T1 pieces that the HTTP-params test does not cover."""
    import services.venue_quality as venue
    from services.venue_quality import (
        VenueMetrics,
        _apply_hard_liquidity,
        evaluate_venue_quality,
        fetch_gate_venue_metrics,
    )
    from tests.support.venue_block import as_venue_config

    seen = []

    def fake(path, params=None, *, timeout):
        seen.append((path, dict(params or {}), float(timeout)))
        if path == "/spot/order_book":
            raise RuntimeError("book down")
        if path == "/spot/tickers" and (params or {}).get("currency_pair"):
            return [
                {
                    "currency_pair": "LAB_USDT",
                    "last": "1",
                    "highest_bid": "0.99",
                    "lowest_ask": "1.01",
                    "quote_volume": "8000000",
                    "highest_size": "1000",
                    "lowest_size": "1000",
                }
            ]
        if path == "/spot/tickers":
            return [
                {
                    "currency_pair": "LAB_USDT",
                    "last": "1",
                    "highest_bid": "0.99",
                    "lowest_ask": "1.01",
                    "quote_volume": "8000000",
                    "highest_size": "1000",
                    "lowest_size": "1000",
                }
            ]
        raise AssertionError(path)

    monkeypatch.setattr(venue, "gate_public_rest_get", fake)
    reset_venue_cache_for_tests()
    raw = {
        "risk": {
            "venue_quality": venue_block(),
            "liquidity_guard": {
                "min_quote_volume_24h_usdt": 500000,
                "depth_window_pct": 0.5,
                "order_book_cache_ttl_sec": 15,
                "depth_to_order_multiple": 1,
            },
        }
    }
    fetch_gate_venue_metrics(["LAB/USDT"], config_raw=raw, fetch_depth=True)
    pair = [row for row in seen if row[0] == "/spot/tickers" and row[1].get("currency_pair")]
    assert pair
    assert pair[0][2] == pytest.approx(5.0)
    assert pair[0][2] != pytest.approx(12.0)

    now = 1_000_000.0
    monkeypatch.setattr(venue.time, "time", lambda: now)
    reset_venue_cache_for_tests()
    cached = _book(symbol="TTL/USDT", quote_volume_24h_usdt=8_000_000)
    hits = {}
    for age in (89, 90, 91):
        seen.clear()
        venue._cache["TTL/USDT"] = (now - age, cached)
        fetch_gate_venue_metrics(["TTL/USDT"], config_raw=raw, fetch_depth=False)
        hits[age] = bool(seen)
    assert hits[89] is False
    assert hits[90] is False
    assert hits[91] is True

    cfg = as_venue_config()
    unavailable = _book(symbol="BOOK/USDT", capture="book_unavailable", depth_parsed=False, quote_volume_present=False)
    blocked = evaluate_venue_quality(unavailable, cfg)
    assert blocked.ok is False
    assert blocked.code == "book_unavailable"
    volume_ok = _book(
        symbol="BOOK/USDT",
        capture="book_unavailable",
        depth_parsed=False,
        quote_volume_present=True,
        quote_volume_24h_usdt=8_000_000,
    )
    allowed = evaluate_venue_quality(volume_ok, cfg, skip_volume_multiple=True)
    assert allowed.ok is True
    assert is_thin_venue_stamp({"capture": "missing"}, cfg) is False
    assert is_thin_venue_stamp({"capture": "book_unavailable", "venue_ok": False}, cfg) is True
    assert is_thin_venue_stamp({"capture": "ok"}, VenueConfigError("risk.venue_quality", "missing")) is None

    thick = _book(symbol="CAP/USDT", quote_volume_24h_usdt=2_000_000, depth_bid_usdt=4_000, depth_ask_usdt=6_000)
    capped = _apply_hard_liquidity(
        VenueQualityResult_ok(thick),
        thick,
        planned_usdt=100,
        config_raw=raw,
    )
    assert capped.cap_volume == pytest.approx(2_000_000 / 20)
    assert capped.cap_depth == pytest.approx(4_000 / 1)


def VenueQualityResult_ok(metrics):
    from services.venue_quality import VenueQualityResult

    return VenueQualityResult(ok=True, reasons=[], metrics=metrics)


def test_t1_floor_warning_once_across_rearm_and_other_tenant(monkeypatch):
    reset_venue_cache_for_tests()
    lines = []
    monkeypatch.setattr(
        "services.venue_quality.log",
        lambda msg, level="INFO": lines.append((level, msg)),
    )
    tenant = {"id": "tenant-a"}
    monkeypatch.setattr(
        "core.tenant_context.resolve_tenant_id",
        lambda *a, **k: tenant["id"],
    )
    liquidity_multiple_floor_reasons(0.5, 20)
    assert isinstance(venue_quality_config({"risk": {"venue_quality": venue_block()}}), VenueQualityConfig)
    liquidity_multiple_floor_reasons(0.5, 20)
    warns = [msg for level, msg in lines if level == "WARNING"]
    assert len(warns) == 1
    assert "value=0.5" in warns[0]
    assert "floor=1" in warns[0]
    tenant["id"] = "tenant-b"
    liquidity_multiple_floor_reasons(0.5, 20)
    warns = [msg for level, msg in lines if level == "WARNING"]
    assert len(warns) == 2
    assert any("tenant-b" in msg for msg in warns)

    reset_venue_cache_for_tests()
    lines.clear()
    tenant["id"] = "tenant-a"
    base = _disk_config()
    base["risk"]["liquidity_guard"].pop("depth_to_order_multiple_min")
    monkeypatch.setattr("services.venue_quality._read_base_config_for_floors", lambda: base)
    liquidity_multiple_floor_reasons(1, 20)
    liquidity_multiple_floor_reasons(1, 20)
    missing = [msg for level, msg in lines if level == "WARNING" and "floor=missing" in msg]
    assert len(missing) == 1
    assert any("depth_to_order_multiple_min" in msg for msg in missing)


def test_t1_buy_and_short_missing_exchange():
    from risk.risk_manager import RiskManager

    raw = _cfg().raw
    raw["risk"]["venue_quality"].pop("exchange")
    raw["shorts"]["volatile"]["market_cap_min_usd"] = 0
    raw["shorts"]["stable"]["market_cap_min_usd"] = 0
    rm = RiskManager(BotConfig(raw))
    short = TradeOrder(
        type="SHORT",
        symbol="LAB/USDT",
        price=1.0,
        amount=0,
        usdt_amount=100,
        signal="SHORT",
        source="auto",
        leverage=2,
    )
    with _eval_env(rm, metrics=_book(symbol="LAB/USDT", quote_volume_24h_usdt=5_000_000)):
        buy = rm.evaluate(_buy(source="technical"), "4h", source="technical")
        short_dec = rm.evaluate(short, "4h", source="auto")
    for dec in (buy, short_dec):
        assert dec.approved is False
        assert dec.code == "liq_guard_missing_input"
        assert dec.details["config_problem"] == "missing"


def test_c6_unavailable_ledger_and_shadow_log_are_deduped(monkeypatch):
    import services.venue_quality as venue

    reset_venue_cache_for_tests()
    writes = []
    monkeypatch.setattr("logger.log_decision", lambda entry: writes.append(entry))
    monkeypatch.setattr(
        "services.observability_store.persist_decision",
        lambda entry: writes.append(entry),
    )
    err = VenueConfigError("risk.venue_quality_bounds", "config_unavailable")
    monkeypatch.setattr(venue, "load_venue_bounds", lambda: err)
    venue_quality_config({"risk": {"venue_quality": venue_block()}})
    venue_quality_config({"risk": {"venue_quality": venue_block()}})
    assert len(writes) == 2

    messages = []
    monkeypatch.setattr(shadow, "log", lambda msg, level="INFO": messages.append((level, msg)))
    before = set(venue._CONFIG_WARNED)
    shadow._SHADOW_VENUE_WARNED.clear()
    raw = _guard_raw(depth_window_pct=_POP)
    _trigger(raw, symbol="DED1/USDT")
    _trigger(raw, symbol="DED2/USDT")
    errors = [msg for level, msg in messages if level == "ERROR" and "venue_config_invalid" in msg]
    assert len(errors) == 1
    assert set(venue._CONFIG_WARNED) == before


def test_c7_bounds_rearm_does_not_clear_floor_warnings(monkeypatch):
    import services.venue_quality as venue

    reset_venue_cache_for_tests()
    lines = []
    monkeypatch.setattr(venue, "log", lambda msg, level="INFO": lines.append((level, msg)))
    err = VenueConfigError("risk.venue_quality_bounds.cache_ttl_sec_min", "range")
    real = venue.load_venue_bounds
    state = {"bad": True}

    def _bounds():
        if state["bad"]:
            return err
        return real()

    monkeypatch.setattr(venue, "load_venue_bounds", _bounds)
    good = {"risk": {"venue_quality": venue_block()}}
    assert isinstance(venue_quality_config(good), VenueConfigError)
    state["bad"] = False
    assert isinstance(venue_quality_config(good), VenueQualityConfig)
    state["bad"] = True
    assert isinstance(venue_quality_config(good), VenueConfigError)
    errors = [msg for level, msg in lines if level == "ERROR"]
    assert len(errors) == 2

    reset_venue_cache_for_tests()
    lines.clear()
    monkeypatch.setattr(venue, "load_venue_bounds", real)
    liquidity_multiple_floor_reasons(0.5, 20)
    assert isinstance(venue_quality_config(good), VenueQualityConfig)
    liquidity_multiple_floor_reasons(0.5, 20)
    warns = [msg for level, msg in lines if level == "WARNING"]
    assert len(warns) == 1


def test_n2_hard_liquidity_rejects_invalid_config_without_the_outer_return():
    raw = _guard_raw()
    raw["risk"]["venue_quality"].pop("exchange")
    metrics = _book(symbol="LAB/USDT", quote_volume_24h_usdt=5_000_000)
    live = _live_hard(raw, metrics)
    assert live.ok is False
    assert live.code == "liq_guard_missing_input"


def test_n9_buy_lock_backstop_rejects_missing_cap(monkeypatch):
    from risk.risk_manager import RiskManager
    from services.venue_quality import VenueQualityResult

    rm = RiskManager(_cfg())
    monkeypatch.setattr(rm, "_open_cost_usdt", lambda *a, **k: (0.0, None))
    monkeypatch.setattr(
        "services.venue_quality.check_venue_for_buy",
        lambda *a, **k: VenueQualityResult(ok=True, reasons=[]),
    )
    locked = rm._buy_lock_decision(
        _buy(source="technical"),
        source="technical",
        pos={"amount": 0},
        has_position=False,
        planned_usdt=100,
        indicators={},
    )
    assert locked is not None
    assert locked.decision is not None
    assert locked.decision.code == "liq_guard_missing_input"
    assert locked.apply_cap is False


def test_x6_invalid_config_still_calls_the_live_gate():
    raw = _sensor_raw()
    raw["risk"]["venue_quality"].pop("exchange")
    assert _live_planned(raw) is not None


def test_x7_fail_open_missing_capture_stays_blocked(monkeypatch):
    from services.venue_quality import VenueMetrics

    monkeypatch.setattr(
        "services.venue_quality.get_venue_metrics",
        lambda *a, **k: VenueMetrics(symbol="LAB/USDT", capture="missing", quote_volume_present=False),
    )
    raw = _guard_raw()
    raw["risk"]["venue_quality"]["on_fetch_error"] = "fail_open"
    result = check_venue_for_buy("LAB/USDT", planned_usdt=100, config_raw=raw, metrics=None)
    assert result.ok is False
    assert "liq_guard_missing_input" in (getattr(result, "guard_codes", None) or [])


def test_x3_cache_ttl_lower_bound_rejects_just_under_min():
    from services.venue_quality import parse_venue_bounds, validate_venue_quality

    bounds = parse_venue_bounds(_disk_config())
    assert not isinstance(bounds, VenueConfigError)
    lo = float(bounds.cache_ttl_sec_min)
    assert isinstance(validate_venue_quality(venue_block(cache_ttl_sec=lo), bounds), VenueQualityConfig)
    bad = validate_venue_quality(venue_block(cache_ttl_sec=lo - 1e-6), bounds)
    assert isinstance(bad, VenueConfigError)
    assert bad.config_problem == "range"


def test_x13_non_positive_timeout_does_not_reach_requests(monkeypatch):
    called = []
    monkeypatch.setattr("requests.get", lambda *a, **k: called.append(a) or SimpleNamespace(status_code=200, json=lambda: []))
    for timeout in (0, -1):
        with pytest.raises(ValueError):
            gate_public_rest_get("/spot/tickers", {}, timeout=timeout)
    assert called == []
