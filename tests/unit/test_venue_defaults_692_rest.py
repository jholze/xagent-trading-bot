"""Issue #692 remaining acceptance cases: T2–T7, T10–T12, T16, T18, T20, T21."""

from __future__ import annotations

import inspect
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import data_manager
from core.config import BotConfig
from core.config_guardrails import (
    ConfigValidationError,
    assert_venue_quality_within_bounds,
    validate_config_for_save,
)
from core.models import TradeOrder
from services import shadow_signal_log as shadow
from services.venue_quality import (
    VenueConfigError,
    is_thin_venue_stamp,
    load_venue_bounds,
    parse_venue_bounds,
    reset_venue_cache_for_tests,
    stamp_venue_for_fill,
    validate_venue_quality,
    venue_quality_config,
)
from services.watchlist_quality.soft import apply_soft_watchlist
from services.watchlist_quality.venue_batch import QUOTE_VOL_24H_KEY, attach_quote_volumes
from storage.errors import LedgerUnavailable
from tests.support.venue_block import as_venue_config, venue_block
from tests.unit.test_liquidity_guard import _book
from tests.unit.test_long_mcap_venue_563 import _buy, _cfg, _eval_env
from tests.unit.test_shadow_signal_log_687 import _passing_gate, _raw
from tests.unit.test_venue_defaults_692 import R2_KEYS, _disk_config, _trigger

# Captured before the autouse config fixture replaces default-tenant saves
# with an in-memory stub. Exemption tests need the real guardrail path.
_REAL_SAVE = data_manager.save_config
ROOT = Path(__file__).resolve().parents[2]


def _bounds():
    parsed = parse_venue_bounds(_disk_config())
    assert not isinstance(parsed, VenueConfigError)
    return parsed


def _broken_disk(tmp_path, monkeypatch, *, mutate):
    """Readable config.json whose only intended defect is whatever ``mutate`` does."""
    body = _disk_config()
    mutate(body)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    data_manager._config_cache = json.loads(path.read_text(encoding="utf-8"))
    return path


@contextmanager
def _ci_mongo():
    """Mongo already selected by isolate_test_mongo / the CI service. No binary path."""
    from storage.mongo_client import close_client

    uri = os.environ.get("MONGODB_URI") or "mongodb://127.0.0.1:27017"
    from pymongo import MongoClient

    client = MongoClient(uri, serverSelectionTimeoutMS=2000)
    try:
        client.admin.command("ping")
    except Exception as exc:
        pytest.fail(f"mongo at {uri} is not reachable: {exc}")
    finally:
        client.close()
    close_client()
    try:
        yield uri
    finally:
        close_client()


def _use_real_save(monkeypatch):
    monkeypatch.setattr(data_manager, "save_config", _REAL_SAVE)


def _reject(fn):
    with pytest.raises(ConfigValidationError) as caught:
        fn()
    return caught.value


# --- T2 matrix --------------------------------------------------------------


@pytest.mark.parametrize("key", R2_KEYS)
def test_t2_each_key_missing_blocks_buy_and_short(key):
    raw = _cfg().raw
    raw["risk"]["venue_quality"] = venue_block()
    raw["risk"]["venue_quality"].pop(key)
    raw["shorts"]["volatile"]["market_cap_min_usd"] = 0
    raw["shorts"]["stable"]["market_cap_min_usd"] = 0
    from risk.risk_manager import RiskManager

    rm = RiskManager(BotConfig(raw))
    metrics = _book(symbol="LAB/USDT", quote_volume_24h_usdt=5_000_000)
    with _eval_env(rm, metrics=metrics):
        buy = rm.evaluate(_buy(source="technical"), "4h", source="technical")
        short = rm.evaluate(
            TradeOrder(
                type="SHORT",
                symbol="LAB/USDT",
                price=1.0,
                amount=0,
                usdt_amount=100,
                signal="SHORT",
                source="auto",
                leverage=2,
            ),
            "4h",
            source="auto",
        )
    for dec in (buy, short):
        assert dec.approved is False
        assert dec.code == "liq_guard_missing_input"
        assert dec.details["reason"] == "venue_config_missing"
        assert dec.details["config_problem"] == "missing"
        assert key in dec.details["config_key"]


def test_t3_nan_ttl_is_rejected():
    err = validate_venue_quality(venue_block(cache_ttl_sec=float("nan")), _bounds())
    assert isinstance(err, VenueConfigError)
    assert err.config_problem == "range"


def test_t12_disk_bounds_ignore_tenant_copy_and_pin_blocks_patch(tmp_path, monkeypatch):
    _use_real_save(monkeypatch)
    copied = venue_block(order_book_timeout_sec=30)
    tenant = {
        "risk": {
            "venue_quality": copied,
            "venue_quality_bounds": {
                "order_book_timeout_sec_max": 100,
                "ticker_timeout_sec_max": 100,
                "cache_ttl_sec_min": 1,
                "cache_ttl_sec_max": 1000,
                "order_book_limit_max": 500,
            },
        }
    }
    err = venue_quality_config(tenant)
    assert isinstance(err, VenueConfigError)
    assert err.config_problem == "range"
    _broken_disk(tmp_path, monkeypatch, mutate=lambda body: None)
    with pytest.raises(ConfigValidationError):
        data_manager.patch_config(
            {"risk": {"venue_quality_bounds": {"cache_ttl_sec_min": 6}}}
        )


def test_t13_volume_cap_not_a_reject():
    raw = _raw()
    raw["max_usdt_per_trade"] = 4500
    high = _trigger(raw, symbol="CAPH/USDT", gate=_passing_gate(quote_volume_24h_usdt=2000))
    assert high["gates"]["venue_liquidity"] is True
    assert high["capped"] is True
    assert high["cap_volume"] == pytest.approx(100)
    assert high["planned_order_usdt_capped"] == pytest.approx(100)
    low = _trigger(raw, symbol="CAPL/USDT", gate=_passing_gate(quote_volume_24h_usdt=200_000))
    assert low["capped"] is False
    assert low["planned_order_usdt_capped"] == pytest.approx(low["planned_order_usdt"])


def test_t3_limit_exchange_and_policy_values():
    bounds = _bounds()
    ok = validate_venue_quality(venue_block(order_book_limit=100), bounds)
    assert not isinstance(ok, VenueConfigError)
    for bad, problem in ((100.5, "type"), (True, "type"), ("Gate", "value")):
        block = venue_block()
        if bad == "Gate":
            block["exchange"] = bad
        else:
            block["order_book_limit"] = bad
        err = validate_venue_quality(block, bounds)
        assert isinstance(err, VenueConfigError)
        assert err.config_problem == problem
    for key, bad in (
        ("book_unavailable_policy", "shrug"),
        ("on_fetch_error", "sometimes"),
    ):
        err = validate_venue_quality(venue_block(**{key: bad}), bounds)
        assert isinstance(err, VenueConfigError)
        assert err.config_problem == "value"


def test_t4_extra_key_is_ignored(monkeypatch):
    lines = []
    monkeypatch.setattr("services.venue_quality.log", lambda msg, level="INFO": lines.append(msg))
    block = venue_block(note="ignored")
    cfg = validate_venue_quality(block, _bounds())
    assert not isinstance(cfg, VenueConfigError)
    assert lines == []


def test_t5_tenant_resolution_failure_is_one_unavailable(monkeypatch):
    def _boom():
        raise LedgerUnavailable("tenant down")

    monkeypatch.setattr("core.config.get_bot_config", _boom)
    err = venue_quality_config(None)
    assert isinstance(err, VenueConfigError)
    assert err.config_problem == "config_unavailable"
    assert err.config_key == "risk.venue_quality"


def test_t6_non_dict_block_is_type_and_fill_stamp_resolves(monkeypatch):
    err = validate_venue_quality("nope", _bounds())
    assert isinstance(err, VenueConfigError)
    assert err.config_problem == "type"
    seen = {}

    def _resolve(config_raw=None):
        seen["raw"] = config_raw
        return as_venue_config()

    monkeypatch.setattr("services.venue_quality.venue_quality_config", _resolve)
    monkeypatch.setattr(
        "services.venue_quality.get_venue_metrics",
        lambda *a, **k: SimpleNamespace(capture="missing"),
    )
    stamp = stamp_venue_for_fill("LAB/USDT")
    assert seen["raw"] is None
    assert stamp["capture"] == "missing"
    assert "config_problem" not in stamp


def test_t7_no_http_on_invalid_and_none_volume_kept(monkeypatch):
    calls = []
    fetches = []
    monkeypatch.setattr(
        "services.venue_quality.gate_public_rest_get",
        lambda *a, **k: calls.append(a) or {},
    )
    monkeypatch.setattr(
        "services.venue_quality.get_venue_metrics",
        lambda *a, **k: fetches.append(a) or None,
    )
    from services.venue_quality import check_venue_for_buy

    result = check_venue_for_buy("LAB/USDT", config_raw={"risk": {"venue_quality": {}}})
    assert result.ok is False
    assert calls == []
    assert fetches == []
    assert is_thin_venue_stamp({"capture": "ok"}, VenueConfigError("risk.venue_quality", "type")) is None
    rows = attach_quote_volumes(
        [{"symbol": "LAB/USDT"}],
        batch_fn=lambda symbols: {"LAB/USDT": SimpleNamespace(capture="missing", quote_volume_24h_usdt=0.0)},
    )
    assert rows[0][QUOTE_VOL_24H_KEY] is None
    kept = apply_soft_watchlist(
        [{"symbol": "LAB/USDT", "quality_score": 1, QUOTE_VOL_24H_KEY: None}],
        min_quote_vol_usd=1_000_000,
        use_ai_score=False,
    )
    assert [row["symbol"] for row in kept] == ["LAB/USDT"]


def test_t10_rank_order_and_validator():
    raw = _raw()
    raw["risk"]["venue_quality"].pop("enabled")
    gate = _passing_gate()
    gate.pop("quote_volume_24h_usdt", None)
    row = _trigger(raw, symbol="RANK/USDT", gate=gate)
    assert row["venue_liquidity_null_reason"] == "venue_config_invalid"
    assert row["venue_gate_version"] == "e12v8"
    shadow.validate_shadow_signal_row(row)
    bad = {"gates": {"venue_liquidity": None}}
    with pytest.raises(ValueError, match="without reason"):
        shadow.validate_shadow_signal_row(bad)


def test_t11_validator_is_not_cached_across_tenants():
    first = venue_quality_config({"risk": {"venue_quality": venue_block(cache_ttl_sec="x")}})
    second = venue_quality_config({"risk": {"venue_quality": venue_block()}})
    assert isinstance(first, VenueConfigError)
    assert not isinstance(second, VenueConfigError)


def test_t12_bounds_and_save_value_checks(tmp_path, monkeypatch):
    _use_real_save(monkeypatch)
    base = _disk_config()
    for name, bad in (
        ("order_book_timeout_sec_max", None),
        ("ticker_timeout_sec_max", True),
        ("cache_ttl_sec_min", "x"),
        ("order_book_limit_max", 100.5),
    ):
        body = json.loads(json.dumps(base))
        body["risk"]["venue_quality_bounds"][name] = bad
        err = parse_venue_bounds(body)
        assert isinstance(err, VenueConfigError)
        assert name in err.config_key
    edges = json.loads(json.dumps(base))
    edges["risk"]["venue_quality_bounds"]["cache_ttl_sec_min"] = 5
    edges["risk"]["venue_quality_bounds"]["cache_ttl_sec_max"] = 5
    assert not isinstance(parse_venue_bounds(edges), VenueConfigError)
    edges["risk"]["venue_quality_bounds"]["cache_ttl_sec_min"] = 6
    assert parse_venue_bounds(edges).config_problem == "range"

    path = _broken_disk(tmp_path, monkeypatch, mutate=lambda body: None)
    data_manager._config_cache = json.loads(path.read_text(encoding="utf-8"))
    hot = json.loads(path.read_text(encoding="utf-8"))
    hot["risk"]["venue_quality"]["order_book_timeout_sec"] = 30
    err = _reject(lambda: data_manager.save_config(hot))
    assert "order_book_timeout_sec" in err.path
    tenant = json.loads(json.dumps(hot))
    err = _reject(lambda: validate_config_for_save(tenant))
    assert "order_book_timeout_sec" in err.path
    script = (ROOT / "scripts/setup_satellite_tenant.py").read_text(encoding="utf-8")
    assert "assert_venue_quality_within_bounds" in script
    bounds = load_venue_bounds()
    with pytest.raises(ConfigValidationError):
        assert_venue_quality_within_bounds(hot, bounds)


def test_t16_exits_ignore_invalid_venue():
    raw = _cfg().raw
    raw["risk"]["venue_quality"] = {"enabled": "no"}
    from risk.risk_manager import RiskManager

    rm = RiskManager(BotConfig(raw))
    with _eval_env(rm, metrics=_book(), position={"amount": 2, "side": "short", "average_entry": 1}):
        sell = rm.evaluate(
            TradeOrder(type="SELL", symbol="LAB/USDT", price=1, amount=1, signal="SELL", source="stop"),
            "4h",
            source="stop",
        )
        cover = rm.evaluate(
            TradeOrder(type="COVER", symbol="LAB/USDT", price=1, amount=1, signal="COVER", source="stop"),
            "4h",
            source="stop",
        )
    assert sell.code != "liq_guard_missing_input"
    assert cover.approved is True
    assert cover.code != "liq_guard_missing_input"


# --- T18 shadow thread ------------------------------------------------------


class _Proxy:
    def __init__(self, inner, events, *, held=None, mark=False):
        self.inner = inner
        self.events = events
        self.held = held
        self.mark = mark

    def acquire(self, *args, **kwargs):
        ident = threading.get_ident()
        holding = bool(getattr(self.held, "flag", False)) if self.held is not None else False
        self.events.append((ident, holding, "acquire"))
        ok = self.inner.acquire(*args, **kwargs)
        if self.mark and self.held is not None:
            self.held.flag = True
        return ok

    def release(self, *args, **kwargs):
        if self.mark and self.held is not None:
            self.held.flag = False
        return self.inner.release(*args, **kwargs)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


def test_t18_shadow_thread_does_not_take_foreign_locks(monkeypatch):
    import services.venue_quality as venue
    from strategies import positions as posmod

    held = threading.local()
    events = []
    shadow_ids = set()
    warned = []

    class _RecordingSet(set):
        def add(self, item):
            if threading.get_ident() in shadow_ids:
                warned.append(item)
            return super().add(item)

    foreign = []
    real_get = venue.gate_public_rest_get
    real_activate = posmod._activate
    real_find = posmod.find_open_positions_for_symbol

    def _note(name, real):
        def _spy(*args, **kwargs):
            if threading.get_ident() in shadow_ids:
                foreign.append(name)
            return real(*args, **kwargs)

        return _spy

    monkeypatch.setattr(venue, "_cache_lock", _Proxy(venue._cache_lock, events, held=held))
    monkeypatch.setattr(posmod, "_positions_lock", _Proxy(posmod._positions_lock, events, held=held))
    monkeypatch.setattr(shadow, "_LOCK", _Proxy(shadow._LOCK, events, held=held, mark=True))
    monkeypatch.setattr(venue, "_CONFIG_WARNED", _RecordingSet())
    monkeypatch.setattr(venue, "gate_public_rest_get", _note("http", real_get))
    monkeypatch.setattr(posmod, "_activate", _note("activate", real_activate))
    monkeypatch.setattr(posmod, "find_open_positions_for_symbol", _note("positions", real_find))
    from risk.risk_manager import RiskManager

    real_cost = RiskManager._open_cost_usdt

    def _cost(self, *args, **kwargs):
        if threading.get_ident() in shadow_ids:
            foreign.append("open_cost")
        return real_cost(self, *args, **kwargs)

    monkeypatch.setattr(RiskManager, "_open_cost_usdt", _cost)
    stop = threading.Event()
    errors = []

    def _shadow():
        shadow_ids.add(threading.get_ident())
        raw = _raw()
        try:
            while not stop.is_set():
                shadow.candidate_info_from_coin("THIN/USDT", {"market_cap_usd": 5_000_000}, raw)
                shadow.log_shadow_trigger(
                    symbol=f"THIN{time.time_ns()}/USDT",
                    timeframe="4h",
                    price=1.25,
                    price_source="gate_spot_last",
                    rationale="x",
                    action="BUY",
                    sensor_policy_source="mode",
                    config_raw=raw,
                    gate_cache=_passing_gate(),
                    sensor_cfg={"shadow_exit_hours": 4},
                )
        except Exception as exc:
            errors.append(exc)

    def _live():
        from risk.risk_manager import RiskManager

        rm = RiskManager(_cfg())
        try:
            while not stop.is_set():
                with posmod._positions_lock:
                    with venue._cache_lock:
                        time.sleep(0.001)
                with _eval_env(rm, metrics=_book()):
                    rm.evaluate(_buy(source="technical"), "4h", source="technical")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_shadow), threading.Thread(target=_live)]
    for thread in threads:
        thread.start()
    time.sleep(0.4)
    stop.set()
    for thread in threads:
        thread.join(timeout=3)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    shadow_held_foreign = [
        event for event in events if event[0] in shadow_ids and event[1] and event[2] == "acquire"
    ]
    # The shadow lock's own acquire sets the flag after recording, so its
    # acquire is not a foreign lock. Foreign acquires while the flag is set fail.
    assert shadow_held_foreign == []
    assert foreign == []
    assert warned == []


# --- T20 / T21 --------------------------------------------------------------


def _mode_off_body():
    return {"trading_mode": "off", "virtual_trading": False}


def test_t20_mode_off_exemption_default(tmp_path, monkeypatch):
    _use_real_save(monkeypatch)
    from notifications.telegram_commands import mode_commands

    sent = []
    monkeypatch.setattr(mode_commands, "send_telegram_message", lambda msg: sent.append(msg))
    floor_calls = {"n": 0}
    real_floor = __import__(
        "core.config_guardrails", fromlist=["assert_default_config_floors_present"]
    ).assert_default_config_floors_present

    def _spy(config):
        floor_calls["n"] += 1
        return real_floor(config)

    monkeypatch.setattr("core.config_guardrails.assert_default_config_floors_present", _spy)
    path = _broken_disk(
        tmp_path,
        monkeypatch,
        mutate=lambda body: body["risk"]["venue_quality_bounds"].__setitem__(
            "cache_ttl_sec_min", "x"
        ),
    )

    assert mode_commands.handle("/mode off") is True
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["trading_mode"] == "off"
    assert written["virtual_trading"] is False
    assert floor_calls["n"] == 1

    data_manager._config_cache = json.loads(path.read_text(encoding="utf-8"))
    before = floor_calls["n"]
    with pytest.raises(ConfigValidationError) as live_err:
        mode_commands.handle("/mode live")
    assert "config_problem=" in live_err.value.reason
    assert json.loads(path.read_text(encoding="utf-8"))["trading_mode"] == "off"
    assert floor_calls["n"] == before

    extra = _mode_off_body()
    extra["note"] = "no"
    with pytest.raises(ConfigValidationError):
        data_manager.patch_config(extra)
    assert floor_calls["n"] == before

    direct = json.loads(path.read_text(encoding="utf-8"))
    with pytest.raises(ConfigValidationError):
        data_manager.save_config(direct)
    assert floor_calls["n"] == before

    zero = _mode_off_body()
    zero["virtual_trading"] = 0
    with pytest.raises(ConfigValidationError):
        data_manager.patch_config(zero)


def test_t20_missing_bound_path_still_writes_and_floor_rejects(tmp_path, monkeypatch):
    _use_real_save(monkeypatch)
    path = _broken_disk(
        tmp_path,
        monkeypatch,
        mutate=lambda body: body["risk"]["venue_quality_bounds"].pop("order_book_limit_max"),
    )
    assert data_manager.patch_config(_mode_off_body()) is True
    assert json.loads(path.read_text(encoding="utf-8"))["trading_mode"] == "off"

    low = _disk_config()
    low["risk"]["venue_quality_bounds"]["cache_ttl_sec_min"] = "x"
    low["risk"]["venue_quality"]["min_volume_to_order_multiple"] = 1
    path.write_text(json.dumps(low), encoding="utf-8")
    data_manager._config_cache = json.loads(json.dumps(low))
    err = _reject(lambda: data_manager.patch_config(_mode_off_body()))
    assert "floor" in err.reason or "multiple" in err.reason


def test_t20_unreadable_file_blocks_mode_off(tmp_path, monkeypatch):
    _use_real_save(monkeypatch)
    path = _broken_disk(tmp_path, monkeypatch, mutate=lambda body: None)
    data_manager._config_cache = json.loads(path.read_text(encoding="utf-8"))
    path.unlink()
    path.mkdir()
    with pytest.raises(ConfigValidationError):
        data_manager.patch_config(_mode_off_body())


def test_t20_mode_off_exemption_mongo(tmp_path, monkeypatch):
    _use_real_save(monkeypatch)
    path = _broken_disk(
        tmp_path,
        monkeypatch,
        mutate=lambda body: body["risk"]["venue_quality_bounds"].__setitem__(
            "cache_ttl_sec_min", "x"
        )
        or body.setdefault("paper", {}).__setitem__("backend", "mongo"),
    )
    with _ci_mongo():
        assert data_manager.patch_config(_mode_off_body(), tenant_id="lab692") is True
        with pytest.raises(ConfigValidationError):
            data_manager.patch_config({"trading_mode": "live"}, tenant_id="lab692")
        with pytest.raises(ConfigValidationError):
            data_manager.save_config(json.loads(path.read_text(encoding="utf-8")), tenant_id="lab692")


def test_t21_entries_off_exemption(tmp_path, monkeypatch):
    _use_real_save(monkeypatch)
    from notifications.telegram_commands import mode_commands, pause_commands

    sent = []
    monkeypatch.setattr(pause_commands, "send_telegram_message", lambda msg: sent.append(msg))
    path = _broken_disk(
        tmp_path,
        monkeypatch,
        mutate=lambda body: body["risk"]["venue_quality_bounds"].__setitem__(
            "cache_ttl_sec_min", "x"
        ),
    )

    def _reload(exits):
        body = json.loads(path.read_text(encoding="utf-8"))
        body.setdefault("trading", {})
        body["trading"]["entries_enabled"] = True
        body["trading"]["exits_enabled"] = exits
        path.write_text(json.dumps(body), encoding="utf-8")
        data_manager._config_cache = body

    _reload(False)
    assert mode_commands.save_trading_flags(entries_enabled=False) is True
    _reload(True)
    assert mode_commands.save_trading_flags(entries_enabled=False) is True
    _reload(True)
    body = json.loads(path.read_text(encoding="utf-8"))
    trading = dict(body["trading"])
    trading.pop("exits_enabled", None)
    body["trading"] = trading
    path.write_text(json.dumps(body), encoding="utf-8")
    data_manager._config_cache = body
    assert data_manager.patch_config({"trading": {"entries_enabled": False}}) is True

    _reload("true")
    with pytest.raises(ConfigValidationError):
        data_manager.patch_config(
            {"trading": {"entries_enabled": False, "exits_enabled": False}}
        )
    _reload(True)
    assert mode_commands.save_trading_flags(entries_enabled=True) is False
    _reload(True)
    assert mode_commands.save_trading_flags(entries_enabled=False, exits_enabled=False) is False
    with pytest.raises(ConfigValidationError):
        data_manager.patch_config({"trading": {"entries_enabled": False, "extra": 1}})
    with pytest.raises(ConfigValidationError):
        data_manager.patch_config({"trading": {"entries_enabled": 0}})
    with pytest.raises(ConfigValidationError):
        data_manager.patch_config({"trading": {"entries_enabled": False, "exits_enabled": 1}})

    sent.clear()
    _reload("true")
    pause_commands._execute_panic([])
    assert sent
    assert "nicht gespeichert" in sent[-1] or "not saved" in sent[-1].lower()
    assert "pausiert" not in sent[-1]

    sent.clear()
    assert pause_commands.handle("/pause") is True
    assert "pausiert" not in sent[-1]
    sent.clear()
    assert pause_commands.handle("/resume") is True
    assert "pausiert" not in sent[-1]


def test_t21_entries_off_mongo_and_loader_spy(tmp_path, monkeypatch):
    _use_real_save(monkeypatch)
    from notifications.telegram_commands import mode_commands

    path = _broken_disk(
        tmp_path,
        monkeypatch,
        mutate=lambda body: body["risk"]["venue_quality_bounds"].__setitem__(
            "cache_ttl_sec_min", "x"
        )
        or body.setdefault("paper", {}).__setitem__("backend", "mongo")
        or body.setdefault("trading", {}).__setitem__("entries_enabled", True)
        or body["trading"].__setitem__("exits_enabled", True),
    )
    with _ci_mongo():
        assert (
            data_manager.patch_config(
                {"trading": {"entries_enabled": False}}, tenant_id="lab692"
            )
            is True
        )
        real = data_manager._load_default_config_from_disk

        def _spy():
            if any(frame.function == "patch_config" for frame in inspect.stack()):
                raise LedgerUnavailable("unreadable inside patch_config")
            return real()

        monkeypatch.setattr(data_manager, "_load_default_config_from_disk", _spy)
        from core.tenant_context import tenant_context

        with tenant_context("lab692", scope="paper"):
            assert mode_commands.save_trading_flags(entries_enabled=False) is False

    path.unlink()
    path.mkdir()
    data_manager._config_cache = None
    assert mode_commands.save_trading_flags(entries_enabled=False) is False


def _disk_missing_exchange(tmp_path, monkeypatch):
    def _mutate(body):
        body["risk"]["venue_quality"].pop("exchange")
        body.setdefault("paper", {})["backend"] = "mongo"

    return _broken_disk(tmp_path, monkeypatch, mutate=_mutate)


def test_m26a_default_save_stage2_rejects_missing_exchange(tmp_path, monkeypatch):
    _use_real_save(monkeypatch)
    _broken_disk(tmp_path, monkeypatch, mutate=lambda body: None)
    body = _disk_config()
    body["risk"]["venue_quality"].pop("exchange")
    err = _reject(lambda: data_manager.save_config(body))
    assert "exchange" in err.path


def test_m20a_tenant_save_stage2_rejects_merged_missing_key(tmp_path, monkeypatch):
    _use_real_save(monkeypatch)
    _disk_missing_exchange(tmp_path, monkeypatch)
    with _ci_mongo():
        err = _reject(
            lambda: data_manager.save_config({"trading_mode": "paper"}, tenant_id="lab692m20")
        )
    assert "exchange" in err.path


def test_m26b_tenant_patch_stage2_rejects_merged_missing_key(tmp_path, monkeypatch):
    _use_real_save(monkeypatch)
    _disk_missing_exchange(tmp_path, monkeypatch)
    with _ci_mongo():
        err = _reject(
            lambda: data_manager.patch_config({"trading_mode": "paper"}, tenant_id="lab692m26")
        )
    assert "exchange" in err.path


def test_x10_satellite_script_runs_the_full_bounds_check(monkeypatch):
    disk = _disk_config()
    disk["risk"]["venue_quality"].pop("exchange")
    monkeypatch.setattr(data_manager, "_load_default_config_from_disk", lambda: disk)
    monkeypatch.setattr("storage.tenant_registry.get_tenant", lambda tid: {"tenant_id": tid})
    saved = []

    class _Store:
        def save_orders(self, *args, **kwargs):
            return None

        def save_positions(self, *args, **kwargs):
            return None

        def save_trade_history(self, *args, **kwargs):
            return None

    monkeypatch.setattr("storage.mongo_ledger.MongoLedgerStore", _Store)
    monkeypatch.setattr(
        "storage.tenant_meta_store.save_tenant_config",
        lambda *args, **kwargs: saved.append(args) or True,
    )
    from scripts.setup_satellite_tenant import setup_satellite_tenant

    with pytest.raises(ConfigValidationError) as caught:
        setup_satellite_tenant("lab692sat", dry_run=False)
    assert "exchange" in str(caught.value)
    assert saved == []
