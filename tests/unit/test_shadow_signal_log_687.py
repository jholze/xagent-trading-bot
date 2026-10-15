"""Spec #687 shadow signal log. No network. No writes under data/."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.config import BotConfig
from services import entry_sensor_loop, shadow_signal_log as shadow
from services.watchlist_quality.venue_batch import QUOTE_VOL_24H_KEY

T0 = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
SIG = "SIG/USDT"
OTHER = "OTHER/USDT"
FREE = "FREE/USDT"
CTRL = "CTRL/USDT"


def _raw(**overrides) -> dict:
    raw = {
        "stop_loss_pct": 20,
        "costs": {"gate": {"spot": {"fee_taker_conservative_pct": 0.2}}},
        "entry_sensor_15m": {
            "enabled": True,
            "mode": "shadow",
            "shadow_exit_hours": 4,
            "market_cap_min_usd": 1_000_000,
            "vol_spike_mult": 2.0,
            "fakeout_min_body_atr_ratio": 0.3,
            "cooldown_after_reject_hours": 2,
            "gate_only": True,
            "poll_interval_sec": 20,
            "min_poll_gap_sec_per_coin": 20,
            "setup_modes": [],
        },
        "risk": {
            "liquidity_guard": {
                "min_quote_volume_24h_usdt": 1000,
                "depth_window_pct": 0.5,
                "order_book_cache_ttl_sec": 15,
            }
        },
    }
    raw.update(overrides)
    return raw


def _sensor(raw: dict) -> dict:
    return raw["entry_sensor_15m"]


def _passing_gate(**overrides) -> dict:
    cache = {
        "market_cap_usd": 5_000_000,
        "quote_volume_24h_usdt": 1_000_000,
        "daily_buys_count": 0,
        "max_daily_buys": 10,
        "loss_brake_halted": False,
        "open_slots": 0,
        "max_open_positions": 10,
        "cooldown_blocked": False,
        "rate_limited": False,
    }
    cache.update(overrides)
    return cache


def _eligible(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "timeframe": "4h",
        "active": True,
        "market_cap_usd": 5_000_000,
        QUOTE_VOL_24H_KEY: 1_000_000,
    }


def _rows(logs: list[tuple[str, str]]) -> list[dict]:
    out = []
    for level, message in logs:
        try:
            row = json.loads(message)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(row, dict) and row.get("type"):
            out.append(row)
    return out


def _of(logs, type_name: str) -> list[dict]:
    return [row for row in _rows(logs) if row.get("type") == type_name]


@pytest.fixture
def logs(monkeypatch, tmp_path):
    captured: list[tuple[str, str]] = []

    def _log(message, level="INFO"):
        captured.append((level, message))

    monkeypatch.setenv("SHADOW_SIGNAL_STATE_PATH", str(tmp_path / "open.json"))
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.setattr(shadow, "log", _log)
    shadow.reset_for_tests()
    yield captured
    shadow.reset_for_tests()


class _Unset:
    pass


_UNSET = _Unset()


def _trigger(logs, raw, symbol=SIG, price=1.25, gate=_UNSET, batch=None, cache=None, now=None, **kwargs):
    if gate is _UNSET:
        gate = _passing_gate()
    return shadow.log_shadow_trigger(
        symbol=symbol,
        timeframe=kwargs.get("timeframe", "4h"),
        price=price,
        price_source=kwargs.get("price_source", "gate_spot_last"),
        rationale=kwargs.get("rationale", "15m vol spike"),
        action=kwargs.get("action", "BUY"),
        sensor_policy_source=kwargs.get("sensor_policy_source", "mode"),
        config_raw=raw,
        price_batch=batch if batch is not None else {symbol: price},
        candidate_cache=cache if cache is not None else {},
        gate_cache=gate,
        sensor_cfg=_sensor(raw),
        now=now or T0,
    )


def test_t1_entry_row_includes_price(logs):
    raw = _raw()
    row = _trigger(logs, raw, price=1.25)
    assert row is not None
    parsed = _of(logs, "shadow_signal")
    assert len(parsed) == 1
    got = parsed[0]
    assert got["price_at_signal"] == 1.25
    version, reason = shadow.config_version(raw)
    for field in (
        "signal_id",
        "ts_utc",
        "symbol",
        "timeframe",
        "price_at_signal",
        "price_source",
        "stop_pct",
        "exit_hours",
        "rationale",
        "action",
        "sensor_policy_source",
        "config_version",
        "gates_passed",
        "gate_reject_reason",
    ):
        assert field in got
    assert got["symbol"] == SIG
    assert got["timeframe"] == "4h"
    assert got["price_source"] == "gate_spot_last"
    from strategies.dca import effective_stop_loss_thresholds

    cfg = BotConfig(raw=raw)
    params = dict(cfg.strategy_params(SIG, "4h") or {})
    base = float(params.get("stop_loss_pct", cfg.stop_loss_pct))
    full, partial, _grace = effective_stop_loss_thresholds(
        {
            "dca_rounds": 0,
            "dca_recovery_rounds": 0,
            "last_dca_at": None,
            "last_dca_recovery_at": None,
        },
        params,
        base,
    )
    first = float(full) if partial is None else min(float(full), float(partial))
    assert got["stop_pct"] == pytest.approx(first / 100.0)
    assert got["stop_full_pct"] == pytest.approx(float(full) / 100.0)
    assert got["stop_partial_pct"] == pytest.approx(float(partial) / 100.0)
    assert got["stop_pct_source"] == shadow.STOP_PCT_SOURCE
    assert got["exit_hours"] == 4
    assert got["sensor_policy_source"] == "mode"
    assert got["config_version"] == version
    assert got["config_version_reason"] == reason
    assert got["gates_passed"] is True
    assert got["gate_reject_reason"] is None


def test_t2_failing_gate_is_false(logs):
    raw = _raw()
    gate = _passing_gate(daily_buys_count=10, max_daily_buys=10)
    _trigger(logs, raw, gate=gate)
    row = _of(logs, "shadow_signal")[0]
    assert row["gates_passed"] is False
    assert row["gate_reject_reason"] == "max_daily_trades"


def test_t3_unavailable_and_uncached_are_null(logs):
    raw = _raw()
    _trigger(logs, raw, symbol="UNAVAIL/USDT", gate=None)
    unavailable = _of(logs, "shadow_signal")[-1]
    assert unavailable["gates_passed"] is None
    assert unavailable["gate_reject_reason"] == "gate_check_unavailable"

    partial = _passing_gate()
    partial.pop("quote_volume_24h_usdt")
    _trigger(logs, raw, symbol="UNCACHE/USDT", gate=partial)
    uncached = _of(logs, "shadow_signal")[-1]
    assert uncached["gates_passed"] is None
    assert uncached["gate_reject_reason"] == "input_uncached"


def _state_snapshot():
    from risk.risk_manager import _daily_loss_halt_memory
    from risk.slot_eviction_runtime import _EVICT_TS, _SYMBOL_COOLDOWN
    from strategies.positions import positions

    return {
        "positions": json.dumps(positions, default=str, sort_keys=True),
        "evict": list(_EVICT_TS),
        "cooldown": dict(_SYMBOL_COOLDOWN),
        "halt": _daily_loss_halt_memory(),
    }


def test_t4_shadow_and_dry_check_change_no_state(logs, monkeypatch):
    from risk.risk_manager import RiskManager

    raw = _raw()
    gate = _passing_gate(daily_buys_count=3)
    eval_calls = []
    buy_reads = []
    monkeypatch.setattr(
        RiskManager,
        "evaluate",
        lambda self, *args, **kwargs: eval_calls.append(1),
    )
    monkeypatch.setattr(
        RiskManager,
        "_daily_buys_count",
        lambda self, *args, **kwargs: buy_reads.append(1) or 0,
    )
    before = _state_snapshot()
    shadow.dry_check_gates(gate, raw)
    _trigger(logs, raw, gate=gate, batch={SIG: 1.25})
    assert gate["daily_buys_count"] == 3
    assert eval_calls == []
    assert buy_reads == []
    assert _state_snapshot() == before


def test_t4_with_control_row_changes_no_state(logs, monkeypatch):
    from risk.risk_manager import RiskManager

    raw = _raw()
    gate = _passing_gate()
    eval_calls = []
    monkeypatch.setattr(
        RiskManager,
        "evaluate",
        lambda self, *args, **kwargs: eval_calls.append(1),
    )
    batch = {SIG: 1.25, OTHER: 2.0}
    cache = {OTHER: {"market_cap_usd": 5_000_000, "quote_volume_24h_usdt": 50_000}}
    before = _state_snapshot()
    _trigger(logs, raw, gate=gate, batch=batch, cache=cache)
    assert _of(logs, "shadow_control")
    assert _of(logs, "shadow_control")[0]["control_symbol"] == OTHER
    assert eval_calls == []
    assert _state_snapshot() == before
    assert gate["daily_buys_count"] == 0


def test_t5_active_buy_carries_shadow_signal_id(logs):
    from services.signal_orchestrator import SignalOrchestrator

    raw = _raw()
    row = _trigger(logs, raw, now=datetime.now(timezone.utc))
    signal_id = row["signal_id"]
    analysis = SimpleNamespace(
        action="BUY",
        normalized_action="BUY",
        rationale="sensor",
        sources=["entry_sensor_15m"],
        confidence=1,
        timeframe="4h",
        symbol=SIG,
    )
    orch = MagicMock()
    orch.analyze.return_value = analysis
    orch.execute_if_needed.return_value = None
    outcome = SignalOrchestrator.process_entry_sensor(
        orch, {"symbol": SIG, "timeframe": "4h"}, 1.25, quiet=True
    )
    assert outcome["executed"] is False
    assert outcome["action"] == "BUY"
    assert outcome["shadow_signal_id"] == signal_id

    shadow.reset_for_tests()
    outcome_none = SignalOrchestrator.process_entry_sensor(
        orch, {"symbol": SIG, "timeframe": "4h"}, 1.25, quiet=True
    )
    assert outcome_none["shadow_signal_id"] is None


def test_t6_dedup_one_id_until_exit(logs):
    raw = _raw()
    first = _trigger(logs, raw, now=T0)
    second = _trigger(logs, raw, now=T0 + timedelta(minutes=1))
    assert second is None
    assert len(_of(logs, "shadow_signal")) == 1
    shadow.check_open_exits(
        {SIG: 1.25},
        price_source="gate_spot_last",
        watchlist={SIG},
        now=T0 + timedelta(hours=4, seconds=1),
    )
    exit_row = _of(logs, "shadow_exit")[0]
    assert exit_row["signal_id"] == first["signal_id"]
    assert exit_row["repeat_count"] == 1
    third = _trigger(logs, raw, now=T0 + timedelta(hours=5))
    assert third is not None
    assert third["signal_id"] != first["signal_id"]
    assert len(_of(logs, "shadow_signal")) == 2


def test_t7_active_decisions_ignore_open_shadow(logs, monkeypatch):
    raw = _raw()
    raw["entry_sensor_15m"]["mode"] = "active"
    cfg = BotConfig(raw=raw)

    def _run(with_shadow: bool):
        entry_sensor_loop.reset_poll_state_for_tests()
        shadow.reset_for_tests()
        if with_shadow:
            _trigger(logs, raw, now=datetime.now(timezone.utc))
        calls = []

        def _process(coin, price, sensor_metrics=None, quiet=True):
            calls.append(
                (
                    coin.get("symbol"),
                    price,
                    dict(sensor_metrics or {}),
                    quiet,
                )
            )
            return {"action": "BUY", "executed": False, "sources": ["entry_sensor_15m"]}

        orch = MagicMock()
        orch.process_entry_sensor.side_effect = _process
        orch.market.fetch_ohlcv.return_value = object()
        orch.market.compute_15m_sensor_metrics.return_value = {
            "volume_spike_ratio": 5.0,
            "body_atr_ratio": 1.0,
            "price_momentum": True,
        }
        monkeypatch.setattr(entry_sensor_loop, "get_bot_config", lambda: cfg)
        monkeypatch.setattr(
            entry_sensor_loop,
            "get_gate_prices_batch",
            lambda symbols: {s: 1.0 for s in symbols},
        )
        monkeypatch.setattr(
            entry_sensor_loop,
            "_coin_by_symbol",
            lambda symbol, entry=None: _eligible(symbol),
        )
        monkeypatch.setattr(
            "data.cmc_market_cap.resolve_market_cap_usd",
            lambda symbol, coin=None: 5_000_000,
        )
        monkeypatch.setattr(
            "services.market_policy_fusion.get_global_market_bias",
            lambda *args, **kwargs: {"active": False, "apply_sensor_policy": False},
        )
        monkeypatch.setattr("bus.eval_queue.eval_queue_enabled", lambda: False)
        from strategies import watch_15m_state

        watch_15m_state.reset_cache_for_tests()
        watch_15m_state.set_watch(SIG, "4h")
        monkeypatch.setattr(
            entry_sensor_loop.watch_15m_state,
            "hours_since_sensor_reject",
            lambda symbol: None,
        )
        entry_sensor_loop._poll_once(orch)
        return calls

    without = _run(False)
    with_open = _run(True)
    assert without == with_open
    assert without and without[0][0] == SIG


def test_t8_shadow_exception_is_warning_and_loop_continues(logs, monkeypatch):
    _assert_shadow_error_continues(logs, monkeypatch, with_control=False)


def test_t8_with_control_row_exception_continues(logs, monkeypatch):
    _assert_shadow_error_continues(logs, monkeypatch, with_control=True)


def _assert_shadow_error_continues(logs, monkeypatch, with_control: bool):
    warnings = []

    def _loop_log(message, level="INFO"):
        warnings.append((level, message))

    raw = _raw()
    cfg = BotConfig(raw=raw)
    symbols = ["AAA/USDT", "MMM/USDT", "ZZZ/USDT"]
    fetched = []
    real = shadow.log_shadow_trigger

    def _boom(**kwargs):
        if kwargs["symbol"] == "MMM/USDT":
            raise RuntimeError("shadow blew up")
        if with_control and kwargs["symbol"] == "AAA/USDT":
            return real(**kwargs)
        if not with_control:
            raise RuntimeError("shadow blew up")
        return real(**kwargs)

    monkeypatch.setattr(shadow, "log_shadow_trigger", _boom)
    monkeypatch.setattr(entry_sensor_loop, "log", _loop_log)
    monkeypatch.setattr(entry_sensor_loop, "get_bot_config", lambda: cfg)
    monkeypatch.setattr(
        entry_sensor_loop,
        "get_gate_prices_batch",
        lambda symbols_in: {s: 1.0 for s in symbols_in},
    )

    def _coin(symbol, entry=None):
        coin = _eligible(symbol)
        return coin

    monkeypatch.setattr(entry_sensor_loop, "_coin_by_symbol", _coin)
    monkeypatch.setattr(
        entry_sensor_loop,
        "evaluate_entry_sensor_15m",
        lambda **kwargs: SimpleNamespace(triggered=True, rationale="spike", action="BUY"),
    )
    monkeypatch.setattr(
        "data.cmc_market_cap.resolve_market_cap_usd",
        lambda symbol, coin=None: 5_000_000,
    )
    monkeypatch.setattr(
        "services.market_policy_fusion.get_global_market_bias",
        lambda *args, **kwargs: {"active": False, "apply_sensor_policy": False},
    )
    from strategies import watch_15m_state

    entry_sensor_loop.reset_poll_state_for_tests()
    watch_15m_state.reset_cache_for_tests()
    for symbol in symbols:
        watch_15m_state.set_watch(symbol, "4h")

    class Market:
        def fetch_ohlcv(self, symbol, timeframe, limit):
            fetched.append(symbol)
            return object()

        def compute_15m_sensor_metrics(self, df, **kwargs):
            return {"volume_spike_ratio": 5.0, "body_atr_ratio": 1.0}

        def fetch_indicators(self, symbol, timeframe, price):
            return {"rsi": 40}

    orch = MagicMock()
    orch.market = Market()
    entry_sensor_loop._poll_once(orch)
    assert fetched == symbols
    assert any(level == "WARNING" and "shadow_log_error" in message for level, message in warnings)
    if with_control:
        assert any(row.get("control") is True for row in _of(logs, "shadow_control"))


def test_t9_stop_uses_frozen_stop_pct(logs, monkeypatch):
    raw = _raw()
    raw["stop_loss_pct"] = 20
    _trigger(logs, raw, price=100.0, now=T0)
    updated = _raw()
    updated["stop_loss_pct"] = 5
    monkeypatch.setattr(
        "core.config.get_bot_config",
        lambda: BotConfig(raw=updated),
    )
    mid = shadow.check_open_exits(
        {SIG: 90.0},
        price_source="gate_spot_last",
        watchlist={SIG},
        now=T0 + timedelta(minutes=30),
    )
    assert mid == []
    hit = shadow.check_open_exits(
        {SIG: 79.0},
        price_source="gate_spot_last",
        watchlist={SIG},
        now=T0 + timedelta(minutes=31),
    )
    assert len(hit) == 1
    assert hit[0]["exit_reason"] == "stop"
    assert hit[0]["exit_price"] == 79.0
    assert hit[0]["signal_id"] == _of(logs, "shadow_signal")[0]["signal_id"]


def test_t10_exit_tracks_symbol_that_left_watchlist(logs):
    raw = _raw()
    _trigger(logs, raw, price=100.0, now=T0)
    early = shadow.check_open_exits(
        {SIG: 100.0},
        price_source="gate_spot_last",
        watchlist=set(),
        now=T0 + timedelta(hours=3),
    )
    assert early == []
    assert SIG in shadow.open_signal_symbols()
    late = shadow.check_open_exits(
        {SIG: 100.0},
        price_source="gate_spot_last",
        watchlist=set(),
        now=T0 + timedelta(hours=4, seconds=1),
    )
    assert len(late) == 1
    assert late[0]["exit_reason"] == "4h"
    assert late[0]["exit_price"] == 100.0


def test_t11_ret_net_pct_subtracts_both_fees(logs):
    raw = _raw()
    _trigger(logs, raw, price=100.0, now=T0)
    rows = shadow.check_open_exits(
        {SIG: 110.0},
        price_source="gate_spot_last",
        watchlist={SIG},
        now=T0 + timedelta(hours=4, seconds=1),
    )
    assert rows[0]["exit_reason"] == "4h"
    assert rows[0]["ret_net_pct"] == pytest.approx(9.6)


def test_t12_restart_one_unresolved_exit_per_id(logs):
    raw = _raw()
    batch = {SIG: 1.0, OTHER: 2.0, FREE: 3.0}
    cache = {
        OTHER: {"market_cap_usd": 5_000_000, "quote_volume_24h_usdt": 50_000},
        FREE: {"market_cap_usd": 5_000_000, "quote_volume_24h_usdt": 50_000},
    }
    _trigger(logs, raw, symbol=SIG, batch=batch, cache=cache, now=T0)
    _trigger(logs, raw, symbol="BETA/USDT", price=4.0, batch={"BETA/USDT": 4.0}, now=T0)
    first = shadow.resolve_restart(now=T0 + timedelta(minutes=1))
    second = shadow.resolve_restart(now=T0 + timedelta(minutes=2))
    assert second == []
    keys = [(row["signal_id"], row["control"]) for row in first]
    assert len(keys) == len(set(keys))
    assert keys
    assert all(row["exit_reason"] == "restart_unresolved" for row in first)
    assert all(row["exit_price"] is None for row in first)
    assert all(row["excluded_from_evaluation"] is True for row in first)
    logged = [
        row
        for row in _of(logs, "shadow_exit")
        if row["exit_reason"] == "restart_unresolved"
    ]
    assert len(logged) == len(first)


def test_t13_no_price_is_null_and_not_filled_later(logs):
    raw = _raw()
    _trigger(logs, raw, price=100.0, now=T0)
    missing = shadow.check_open_exits(
        {},
        price_source="gate_spot_last",
        watchlist=set(),
        now=T0 + timedelta(hours=4, seconds=1),
    )
    assert len(missing) == 1
    assert missing[0]["exit_reason"] == "no_price"
    assert missing[0]["exit_price"] is None
    assert missing[0]["excluded_from_evaluation"] is True
    later = shadow.check_open_exits(
        {SIG: 123.0},
        price_source="gate_spot_last",
        watchlist={SIG},
        now=T0 + timedelta(hours=5),
    )
    assert later == []
    exits = _of(logs, "shadow_exit")
    assert len(exits) == 1
    assert exits[0]["exit_price"] is None
    assert exits[0]["exit_reason"] == "no_price"


def test_t14_control_row_once_per_signal_not_repeat(logs):
    raw = _raw()
    batch = {SIG: 1.25, OTHER: 2.0}
    cache = {OTHER: {"market_cap_usd": 5_000_000, "quote_volume_24h_usdt": 50_000}}
    first = _trigger(logs, raw, batch=batch, cache=cache, now=T0)
    _trigger(logs, raw, batch=batch, cache=cache, now=T0 + timedelta(minutes=1))
    controls = [row for row in _of(logs, "shadow_control") if row["signal_id"] == first["signal_id"]]
    assert len(controls) == 1
    assert controls[0]["control"] is True
    assert controls[0]["control_symbol"] == OTHER
    assert controls[0]["universe_size"] == 1
    assert controls[0]["control_seed"] == shadow.control_seed_from_signal_id(first["signal_id"])
    assert len(_of(logs, "shadow_signal")) == 1


def test_t15_control_excludes_signal_and_open_names(logs):
    raw = _raw()
    batch = {SIG: 1.0, OTHER: 2.0, FREE: 3.0, CTRL: 4.0}
    cache = {
        sym: {"market_cap_usd": 5_000_000, "quote_volume_24h_usdt": 50_000}
        for sym in (OTHER, FREE, CTRL)
    }
    _trigger(logs, raw, symbol=OTHER, price=2.0, batch=batch, cache=cache, now=T0)
    open_control = _of(logs, "shadow_control")[0]["control_symbol"]
    assert open_control in {SIG, FREE, CTRL}
    blocked = {OTHER, open_control}
    universe = shadow.control_universe(SIG, batch, cache, _sensor(raw), raw)
    assert SIG not in universe
    assert blocked.isdisjoint(universe)
    row = _trigger(logs, raw, symbol=SIG, price=1.0, batch=batch, cache=cache, now=T0)
    drawn = _of(logs, "shadow_control")[-1]
    assert drawn["signal_id"] == row["signal_id"]
    assert drawn["control_symbol"] in universe
    assert drawn["control_symbol"] != SIG
    assert drawn["control_symbol"] not in blocked
    assert drawn["control_symbol"] != "BTC/USDT"


def test_t16_empty_universe_is_no_control(logs):
    raw = _raw()
    row = _trigger(logs, raw, batch={SIG: 1.25}, cache={}, now=T0)
    control = [item for item in _of(logs, "shadow_control") if item["signal_id"] == row["signal_id"]][0]
    assert control["control_symbol"] is None
    assert control["reason"] == "no_control"
    assert control["control_symbol"] != "BTC/USDT"


def test_t17_control_draw_is_reproducible(logs):
    raw = _raw()
    universe = [f"NAME{i}/USDT" for i in range(48)]
    batch = {SIG: 1.0, **{sym: 1.0 + i for i, sym in enumerate(universe, start=1)}}
    cache = {
        sym: {"market_cap_usd": 5_000_000, "quote_volume_24h_usdt": 50_000}
        for sym in universe
    }
    row = _trigger(logs, raw, batch=batch, cache=cache, now=T0)
    control = [item for item in _of(logs, "shadow_control") if item["signal_id"] == row["signal_id"]][0]
    seed = shadow.control_seed_from_signal_id(row["signal_id"])
    assert control["control_seed"] == seed
    ordered = sorted(universe)
    draws = [shadow.choose_control_symbol(ordered, seed) for _ in range(30)]
    assert draws[0] == control["control_symbol"]
    assert len(set(draws)) == 1


def test_t18_control_does_not_add_exchange_calls(logs, monkeypatch):
    raw = _raw()
    cfg = BotConfig(raw=raw)
    ticker_calls = []
    batch_calls = []

    def _batch(symbols):
        batch_calls.append(list(symbols))
        return {s: 1.0 for s in symbols}

    def _ticker(symbol, exchange=None):
        ticker_calls.append(symbol)
        return 9.0

    direct_batches = []

    def _direct_batch(symbols, *args, **kwargs):
        names = list(symbols) if isinstance(symbols, (list, tuple, set)) else []
        direct_batches.append(names)
        prices = {s: 1.0 for s in names}
        if kwargs.get("return_sources"):
            return prices, {}
        return prices

    monkeypatch.setattr(entry_sensor_loop, "get_bot_config", lambda: cfg)
    monkeypatch.setattr(entry_sensor_loop, "get_gate_prices_batch", _batch)
    monkeypatch.setattr("price_fetcher.get_ticker_price", _ticker)
    monkeypatch.setattr("price_fetcher.get_gate_prices_batch", _direct_batch)
    monkeypatch.setattr("price_fetcher.get_prices_batch", _direct_batch)
    monkeypatch.setattr(
        "services.market_policy_fusion.get_global_market_bias",
        lambda *args, **kwargs: {"active": False, "apply_sensor_policy": False},
    )
    monkeypatch.setattr(entry_sensor_loop.watch_15m_state, "hours_since_sensor_reject", lambda symbol: None)
    monkeypatch.setattr(
        entry_sensor_loop,
        "_coin_by_symbol",
        lambda symbol, entry=None: _eligible(symbol),
    )
    monkeypatch.setattr(
        "data.cmc_market_cap.resolve_market_cap_usd",
        lambda symbol, coin=None: 5_000_000,
    )
    from strategies import watch_15m_state

    def _poll(spike: float = 0.1):
        entry_sensor_loop.reset_poll_state_for_tests()
        watch_15m_state.reset_cache_for_tests()
        watch_15m_state.set_watch(SIG, "4h")
        orch = MagicMock()
        orch.market.fetch_ohlcv.return_value = object()
        orch.market.compute_15m_sensor_metrics.return_value = {
            "volume_spike_ratio": spike,
            "body_atr_ratio": 1.0,
        }
        entry_sensor_loop._poll_once(orch)

    _poll()
    assert len(batch_calls) == 1
    without = list(batch_calls[0])

    opened = datetime.now(timezone.utc)
    _trigger(
        logs,
        raw,
        price=1.25,
        batch={SIG: 1.25, CTRL: 2.0},
        cache={CTRL: {"market_cap_usd": 5_000_000, "quote_volume_24h_usdt": 50_000}},
        now=opened,
    )
    control = _of(logs, "shadow_control")[0]
    assert control["control_symbol"] == CTRL
    risk_before = _state_snapshot()
    snap = shadow.snapshot_gate_inputs(
        symbol=SIG,
        timeframe="4h",
        market_cap_usd=5_000_000,
        quote_volume_24h_usdt=1_000_000,
        config_raw=raw,
    )
    assert _state_snapshot() == risk_before
    assert "max_daily_buys" in snap
    assert "daily_buys_count" not in snap
    assert "loss_brake_halted" in snap
    assert "open_slots" in snap
    assert "max_open_positions" in snap
    assert "cooldown_blocked" in snap
    assert ticker_calls == []
    assert direct_batches == []
    # Second poll reaches the shadow branch, so the loop builds gate inputs too.
    _poll(spike=5.0)
    assert len(batch_calls) == 2
    assert batch_calls[1] == without
    assert CTRL not in batch_calls[1]
    assert SIG in batch_calls[1]
    assert ticker_calls == []
    assert direct_batches == []

    missing = shadow.check_open_exits(
        {SIG: 1.25},
        price_source="gate_spot_last",
        watchlist={SIG},
        now=opened + timedelta(hours=4, seconds=1),
    )
    control_exit = [row for row in missing if row.get("control")]
    assert len(control_exit) == 1
    assert control_exit[0]["exit_reason"] == "no_price"
    assert control_exit[0]["exit_price"] is None
    assert ticker_calls == []


def test_t19_control_exit_uses_frozen_stop_and_fees(logs):
    raw = _raw()
    raw["stop_loss_pct"] = 10
    batch = {SIG: 100.0, CTRL: 50.0}
    cache = {CTRL: {"market_cap_usd": 5_000_000, "quote_volume_24h_usdt": 50_000}}
    _trigger(logs, raw, price=100.0, batch=batch, cache=cache, now=T0)
    exits = shadow.check_open_exits(
        {SIG: 100.0, CTRL: 40.0},
        price_source="gate_spot_last",
        watchlist={SIG, CTRL},
        now=T0 + timedelta(minutes=10),
    )
    control_exit = [row for row in exits if row["control"]]
    signal_exit = [row for row in exits if not row["control"]]
    assert signal_exit == []
    assert len(control_exit) == 1
    assert control_exit[0]["exit_reason"] == "stop"
    assert control_exit[0]["exit_price"] == 40.0
    assert control_exit[0]["ret_net_pct"] == pytest.approx(-20.4)
    later = shadow.check_open_exits(
        {CTRL: 10.0},
        price_source="fallback",
        watchlist=set(),
        now=T0 + timedelta(hours=5),
    )
    assert all(not row["control"] or row["signal_id"] != control_exit[0]["signal_id"] for row in later)


def test_t20_daily_summary_counts(logs):
    raw = _raw()
    raw["stop_loss_pct"] = 10

    def _cache(*symbols):
        return {
            sym: {"market_cap_usd": 5_000_000, "quote_volume_24h_usdt": 50_000}
            for sym in symbols
        }

    # Restart pair first, while it is the only open shadow.
    _trigger(
        logs,
        raw,
        symbol="D/USDT",
        price=100.0,
        batch={"D/USDT": 100.0, "DC/USDT": 60.0},
        cache=_cache("DC/USDT"),
        now=T0,
    )
    restarted = shadow.resolve_restart(now=T0 + timedelta(minutes=1))
    assert len(restarted) == 2

    # Priced control exit (4h), a no_control row, and a control with no price.
    _trigger(
        logs,
        raw,
        symbol="A/USDT",
        price=100.0,
        batch={"A/USDT": 100.0, "AC/USDT": 80.0},
        cache=_cache("AC/USDT"),
        now=T0,
    )
    _trigger(logs, raw, symbol="B/USDT", price=100.0, batch={"B/USDT": 100.0}, now=T0)
    _trigger(
        logs,
        raw,
        symbol="C/USDT",
        price=100.0,
        batch={"C/USDT": 100.0, "CC/USDT": 70.0},
        cache=_cache("CC/USDT"),
        now=T0,
    )
    shadow.check_open_exits(
        {
            "A/USDT": 100.0,
            "AC/USDT": 80.0,
            "B/USDT": 100.0,
            "C/USDT": 100.0,
        },
        price_source="gate_spot_last",
        watchlist={"A/USDT", "AC/USDT", "B/USDT", "C/USDT"},
        now=T0 + timedelta(hours=4, seconds=1),
    )

    summary = shadow.note_poll_clock(T0 + timedelta(days=1))
    assert summary is not None
    assert summary["type"] == "shadow_control_daily"
    assert summary["day"] == "2026-10-15"
    assert summary["controls_drawn"] == 3
    assert summary["no_control"] == 1
    assert summary["no_price_controls"] == 1
    assert summary["no_price_signals"] == 0
    assert summary["restart_unresolved_signals"] == 1
    assert summary["restart_unresolved_controls"] == 1
    assert summary["drawn_with_result"] == 1
    assert summary["no_price_rate_signals"] == 0
    control_exits = (
        summary["drawn_with_result"]
        + summary["no_price_controls"]
        + summary["restart_unresolved_controls"]
    )
    assert summary["no_price_rate_controls"] == pytest.approx(
        summary["no_price_controls"] / control_exits
    )
    again = shadow.note_poll_clock(T0 + timedelta(days=1, hours=1))
    assert again is None
    daily_lines = _of(logs, "shadow_control_daily")
    assert len(daily_lines) == 1
    assert daily_lines[0]["drawn_with_result"] == 1
    assert any(level == "INFO" for level, message in logs if "shadow_control_daily" in message)


def _arm_shadow_poll(monkeypatch, raw, symbols, *, spike=5.0, warnings=None):
    """Real ``_poll_once`` with prices and market cap already in hand."""
    cfg = BotConfig(raw=raw)
    batch_calls = []
    fetched = []

    def _batch(names):
        batch_calls.append(list(names))
        return {name: 1.0 for name in names}

    def _log(message, level="INFO"):
        if warnings is not None:
            warnings.append((level, message))

    monkeypatch.setattr(entry_sensor_loop, "get_bot_config", lambda: cfg)
    monkeypatch.setattr(entry_sensor_loop, "get_gate_prices_batch", _batch)
    monkeypatch.setattr(entry_sensor_loop, "log", _log)
    monkeypatch.setattr(
        entry_sensor_loop,
        "_coin_by_symbol",
        lambda symbol, entry=None: _eligible(symbol),
    )
    monkeypatch.setattr(
        "data.cmc_market_cap.resolve_market_cap_usd",
        lambda symbol, coin=None: 5_000_000,
    )
    monkeypatch.setattr(
        "services.market_policy_fusion.get_global_market_bias",
        lambda *args, **kwargs: {"active": False, "apply_sensor_policy": False},
    )
    monkeypatch.setattr(
        entry_sensor_loop.watch_15m_state,
        "hours_since_sensor_reject",
        lambda symbol: None,
    )
    monkeypatch.setattr(
        entry_sensor_loop,
        "evaluate_entry_sensor_15m",
        lambda **kwargs: SimpleNamespace(triggered=True, rationale="spike", action="BUY"),
    )
    from strategies import watch_15m_state

    entry_sensor_loop.reset_poll_state_for_tests()
    watch_15m_state.reset_cache_for_tests()
    for symbol in symbols:
        watch_15m_state.set_watch(symbol, "4h")

    class Market:
        def fetch_ohlcv(self, symbol, timeframe, limit):
            fetched.append(symbol)
            return object()

        def compute_15m_sensor_metrics(self, df, **kwargs):
            return {"volume_spike_ratio": spike, "body_atr_ratio": 1.0}

        def fetch_indicators(self, symbol, timeframe, price):
            return {"rsi": 40}

    orch = MagicMock()
    orch.market = Market()
    return orch, batch_calls, fetched


def test_b1_loop_logs_per_gate_values(logs, monkeypatch):
    import time

    from services.order_service import OrderService, _ORDERS_READ_CACHE

    raw = _raw()
    raw["risk"]["slot_eviction"] = {
        "max_evictions_per_hour": 3,
        "max_evictions_per_day": 9,
    }
    svc = OrderService()
    cache_key = svc._cache_key()
    _ORDERS_READ_CACHE[cache_key] = (time.time(), {"orders": []})
    ticker = []
    monkeypatch.setattr(
        "price_fetcher.get_ticker_price",
        lambda symbol, exchange=None: ticker.append(symbol) or 1.0,
    )
    monkeypatch.setattr(
        "services.venue_quality.peek_cached_quote_volume",
        lambda *args, **kwargs: None,
    )
    try:
        orch, batch_calls, fetched = _arm_shadow_poll(monkeypatch, raw, [SIG])
        entry_sensor_loop._poll_once(orch)
    finally:
        _ORDERS_READ_CACHE.pop(cache_key, None)
    assert fetched == [SIG]
    assert ticker == []
    assert batch_calls and SIG in batch_calls[0]
    row = _of(logs, "shadow_signal")[0]
    gates = row["gates"]
    for name in shadow._GATE_ORDER:
        assert gates[name] is not None, name
    assert gates["market_cap"] is True
    assert gates["venue_liquidity"] is True
    assert row["market_cap_usd"] is not None
    assert row["quote_volume_24h_usdt"] is not None
    assert row["gates_passed"] is True


def test_b2_stop_matches_exit_path_not_profile(logs):
    """Volatile profile partial_stop_pct is not the stop the exit path fires."""
    from risk.risk_manager import RiskManager
    from strategies.dca import effective_stop_loss_thresholds

    raw = _raw()
    raw["stop_loss_pct"] = 50
    raw["volatile_altcoin"] = {"partial_stop_pct": 25, "enabled": True}
    cfg = BotConfig(raw=raw)
    params = dict(cfg.strategy_params(SIG, "4h") or {})
    base = float(params.get("stop_loss_pct", cfg.stop_loss_pct))
    fresh = {
        "dca_rounds": 0,
        "dca_recovery_rounds": 0,
        "last_dca_at": None,
        "last_dca_recovery_at": None,
    }
    full, partial, in_grace = effective_stop_loss_thresholds(fresh, params, base)
    assert in_grace is False
    assert partial is not None
    rm = RiskManager.__new__(RiskManager)
    rm.config = cfg
    resolver = float(RiskManager._effective_stop_loss_pct(rm, SIG, "4h", None))
    assert resolver == pytest.approx(25.0)
    before = _state_snapshot()
    row = _trigger(logs, raw, price=100.0, now=T0)
    assert _state_snapshot() == before
    assert row["stop_pct_source"] == shadow.STOP_PCT_SOURCE
    assert row["stop_full_pct"] == pytest.approx(float(full) / 100.0)
    assert row["stop_partial_pct"] == pytest.approx(float(partial) / 100.0)
    assert row["stop_pct"] == pytest.approx(min(float(full), float(partial)) / 100.0)
    assert row["stop_pct"] != pytest.approx(resolver / 100.0)


def test_c1_buy_persists_shadow_signal_id(logs):
    import logger
    from core.models import TradeOrder, TradeResult
    from services.trading_service import _attach_shadow_link

    raw = _raw()
    row = _trigger(logs, raw, now=datetime.now(timezone.utc))
    order = TradeOrder(type="BUY", symbol=SIG, price=1.25, source="entry_sensor_15m")
    result = TradeResult(True, "BUY", SIG, order_id="ord-shadow-1", message="filled")
    out = _attach_shadow_link(order, result)
    assert out is result
    assert out.executed is True
    saved = []
    path = logger.DECISIONS_LOG_FILE
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    saved.append(json.loads(line))
    links = [item for item in saved if item.get("type") == "shadow_link"]
    assert links
    assert links[-1]["shadow_signal_id"] == row["signal_id"]
    assert links[-1]["symbol"] == SIG
    assert links[-1]["order_id"] == "ord-shadow-1"


def _fetch_keys(max_per_poll=5, budget_s=5, timeout_s=2) -> dict:
    return {
        "shadow_price_fetch_max_per_poll": max_per_poll,
        "shadow_price_fetch_budget_s": budget_s,
        "shadow_price_fetch_timeout_s": timeout_s,
    }


def test_c2_loop_tracks_symbol_that_left_watchlist(logs, monkeypatch):
    raw = _raw()
    raw["entry_sensor_15m"].update(_fetch_keys())
    calls = []

    def _single(symbol, timeout=6):
        calls.append((symbol, timeout))
        return 1.5

    monkeypatch.setattr("price_fetcher._fetch_gate_single", _single)
    _trigger(
        logs,
        raw,
        batch={SIG: 1.25, CTRL: 2.0},
        cache={CTRL: {"market_cap_usd": 5_000_000, "quote_volume_24h_usdt": 50_000}},
        now=datetime.now(timezone.utc),
    )
    assert _of(logs, "shadow_control")[0]["control_symbol"] == CTRL
    orch, batch_calls, _fetched = _arm_shadow_poll(monkeypatch, raw, [OTHER])
    entry_sensor_loop._poll_once(orch)
    assert batch_calls
    assert SIG not in batch_calls[0]
    assert CTRL not in batch_calls[0]
    assert [item[0] for item in calls] == [SIG]
    assert calls[0][1] == 2


def test_c6_counts_and_journal_survive_restart(logs):
    raw = _raw()
    batch = {SIG: 1.0, OTHER: 2.0}
    cache = {OTHER: {"market_cap_usd": 5_000_000, "quote_volume_24h_usdt": 50_000}}
    row = _trigger(logs, raw, batch=batch, cache=cache, now=T0)
    assert shadow.build_daily_summary(T0.date().isoformat())["controls_drawn"] == 1
    shadow.drop_process_memory()
    assert shadow.build_daily_summary(T0.date().isoformat())["controls_drawn"] == 0
    restarted = shadow.resolve_restart(now=T0 + timedelta(minutes=5))
    ids = {(item["signal_id"], item["control"]) for item in restarted}
    assert (row["signal_id"], False) in ids
    assert (row["signal_id"], True) in ids
    assert all(item["exit_reason"] == "restart_unresolved" for item in restarted)
    summary = shadow.build_daily_summary(T0.date().isoformat())
    assert summary["controls_drawn"] == 1
    assert summary["restart_unresolved_signals"] == 1
    assert summary["restart_unresolved_controls"] == 1


def test_missing_fee_key_ret_net_pct_is_null(logs):
    raw = _raw()
    raw["costs"] = {"gate": {"spot": {}}}
    _trigger(logs, raw, price=100.0, now=T0)
    rows = shadow.check_open_exits(
        {SIG: 110.0},
        price_source="gate_spot_last",
        watchlist={SIG},
        now=T0 + timedelta(hours=4, seconds=1),
    )
    assert rows[0]["exit_reason"] == "4h"
    assert rows[0]["ret_net_pct"] is None
    assert rows[0]["ret_net_pct"] != 0


def test_exit_guard_loop_continues(logs, monkeypatch):
    warnings = []

    def _boom(*args, **kwargs):
        raise RuntimeError("exit blew up")

    monkeypatch.setattr(shadow, "check_open_exits", _boom)
    raw = _raw()
    orch, _batch_calls, fetched = _arm_shadow_poll(
        monkeypatch, raw, [SIG, OTHER], warnings=warnings
    )
    continued = True
    try:
        entry_sensor_loop._poll_once(orch)
    except Exception:
        continued = False
    assert continued
    assert set(fetched) == {SIG, OTHER}
    assert len(fetched) == 2
    assert any(level == "WARNING" and "shadow_log_error" in message for level, message in warnings)


def test_link_guard_buy_result_continues(logs, monkeypatch):
    warnings = []

    def _boom(*args, **kwargs):
        raise RuntimeError("link blew up")

    def _log(message, level="INFO"):
        warnings.append((level, message))

    monkeypatch.setattr(shadow, "attach_shadow_signal_id", _boom)
    monkeypatch.setattr("services.trading_service.log", _log)
    from core.models import TradeOrder, TradeResult
    from services.trading_service import _attach_shadow_link

    order = TradeOrder(type="BUY", symbol=SIG, price=1.25, source="entry_sensor_15m")
    result = TradeResult(True, "BUY", SIG, order_id="ord-shadow-2", message="filled")
    continued = True
    out = None
    try:
        out = _attach_shadow_link(order, result)
    except Exception:
        continued = False
    assert continued
    assert out is result
    assert out.executed is True
    assert out.message == "filled"
    assert out.order_id == "ord-shadow-2"
    assert any(level == "WARNING" and "shadow_log_error" in message for level, message in warnings)


def test_t4_repeat_poll_does_not_collect_or_touch_ledger(logs, monkeypatch):
    """A poll that mints no new signal must not collect gates or touch the ledger."""
    import data_manager

    io_calls = []

    def _spy(name):
        def _fn(*args, **kwargs):
            io_calls.append(name)
            return {}

        return _fn

    monkeypatch.setattr(data_manager, "load_orders", _spy("load_orders"))
    monkeypatch.setattr(data_manager, "load_trade_history", _spy("load_trade_history"))
    monkeypatch.setattr(
        data_manager, "load_trade_history_document", _spy("load_trade_history_document")
    )
    monkeypatch.setattr(
        data_manager, "save_trade_history_document", _spy("save_trade_history_document")
    )
    snaps = []
    real_snap = shadow.snapshot_gate_inputs

    def _snap(*args, **kwargs):
        snaps.append(1)
        return real_snap(*args, **kwargs)

    monkeypatch.setattr(shadow, "snapshot_gate_inputs", _snap)
    raw = _raw()
    orch, _batch_calls, _fetched = _arm_shadow_poll(monkeypatch, raw, [SIG])
    entry_sensor_loop._poll_once(orch)
    assert len(_of(logs, "shadow_signal")) == 1
    assert snaps == [1]
    assert io_calls == []
    snaps.clear()
    io_calls.clear()
    entry_sensor_loop._poll_once(orch)
    assert len(_of(logs, "shadow_signal")) == 1
    assert snaps == []
    assert io_calls == []


def test_c5_cold_venue_cache_uses_watchlist_volume(logs, monkeypatch):
    monkeypatch.setattr(
        "services.venue_quality.peek_cached_quote_volume",
        lambda *args, **kwargs: None,
    )
    raw = _raw()
    orch, batch_calls, _fetched = _arm_shadow_poll(monkeypatch, raw, [SIG, OTHER])
    entry_sensor_loop._poll_once(orch)
    signals = _of(logs, "shadow_signal")
    controls = [row for row in _of(logs, "shadow_control") if row.get("control_symbol")]
    assert signals
    for row in signals:
        assert row["gates"]["venue_liquidity"] is not None
        assert row["quote_volume_24h_usdt"] == 1_000_000
    assert controls
    assert controls[0]["universe_size"] > 0
    assert batch_calls


def test_venue_liquidity_null_reason_only_for_that_gate(logs):
    raw = _raw()
    missing_vol = _passing_gate()
    missing_vol.pop("quote_volume_24h_usdt")
    missing_vol.pop("market_cap_usd")
    row = _trigger(logs, raw, gate=missing_vol)
    assert row["gates"]["venue_liquidity"] is None
    assert row["gates"]["market_cap"] is None
    assert row["venue_liquidity_null_reason"] == "missing_quote_volume"
    assert [key for key in row if key.endswith("_null_reason")] == ["venue_liquidity_null_reason"]

    raw_floor = _raw()
    raw_floor["risk"].pop("liquidity_guard")
    row_floor = _trigger(logs, raw_floor, symbol=OTHER, gate=_passing_gate())
    assert row_floor["gates"]["venue_liquidity"] is None
    assert row_floor["gates"]["market_cap"] is True
    assert row_floor["venue_liquidity_null_reason"] == "missing_floor"
    assert "market_cap_null_reason" not in row_floor

    passed = _trigger(logs, raw, symbol=FREE, gate=_passing_gate())
    assert passed["gates"]["venue_liquidity"] is True
    assert "venue_liquidity_null_reason" not in passed


def test_c4_no_price_flag_on_signal_and_control_rows(logs):
    raw = _raw()
    batch = {SIG: 100.0, CTRL: 50.0}
    cache = {CTRL: {"market_cap_usd": 5_000_000, "quote_volume_24h_usdt": 50_000}}
    _trigger(logs, raw, price=100.0, batch=batch, cache=cache, now=T0)
    exits = shadow.check_open_exits(
        {SIG: 110.0},
        price_source="gate_spot_last",
        watchlist={SIG, CTRL},
        now=T0 + timedelta(hours=4, seconds=1),
    )
    signal = [row for row in exits if not row["control"]][0]
    control = [row for row in exits if row["control"]][0]
    assert signal["exit_reason"] == "4h"
    assert signal["no_price"] is False
    assert control["exit_reason"] == "no_price"
    assert control["no_price"] is True
    assert control["no_price_reason"] == "fetch_error"


_CLOSED_NO_PRICE_REASONS = (
    "cap_reached",
    "fetch_timeout",
    "fetch_error",
    "fetch_disabled_config",
)


def test_shadow_fetch_runs_after_active_triggers(logs, monkeypatch):
    """A ticker that raises or hangs does not run before this poll's triggers."""
    raw = _raw()
    raw["entry_sensor_15m"]["mode"] = "active"
    raw["entry_sensor_15m"]["min_poll_gap_sec_per_coin"] = 0
    raw["entry_sensor_15m"].update(_fetch_keys())
    events: list[str] = []

    def _trigger_active(*args, **kwargs):
        events.append("trigger")

    monkeypatch.setattr(entry_sensor_loop, "_active_trigger", _trigger_active)
    monkeypatch.setattr(entry_sensor_loop, "_has_open_position", lambda *args, **kwargs: False)
    monkeypatch.setattr("bus.eval_queue.eval_queue_enabled", lambda: False)
    _trigger(logs, raw, symbol="OFF/USDT", now=datetime.now(timezone.utc))

    def _raise(symbol, timeout=6):
        events.append("fetch")
        raise RuntimeError("ticker down")

    monkeypatch.setattr("price_fetcher._fetch_gate_single", _raise)
    orch, _batch, fetched = _arm_shadow_poll(monkeypatch, raw, [SIG])
    entry_sensor_loop._poll_once(orch)
    assert fetched == [SIG]
    assert events[0] == "trigger"
    assert events.index("trigger") < events.index("fetch")

    events.clear()
    entry_sensor_loop._last_poll_at.clear()

    def _hang(symbol, timeout=6):
        events.append("fetch")
        raise TimeoutError("ticker hung")

    monkeypatch.setattr("price_fetcher._fetch_gate_single", _hang)
    entry_sensor_loop._poll_once(orch)
    assert events[0] == "trigger"
    assert events.index("trigger") < events.index("fetch")


def test_shadow_fetch_cap_keeps_nearest_exit(logs, monkeypatch):
    raw = _raw()
    sensor = raw["entry_sensor_15m"]
    sensor.update(_fetch_keys(max_per_poll=1, budget_s=5, timeout_s=2))
    calls = []

    def _single(symbol, timeout=6):
        calls.append(symbol)
        return 4.0

    monkeypatch.setattr("price_fetcher._fetch_gate_single", _single)
    near = "NEAR/USDT"
    far = "FAR/USDT"
    _trigger(logs, raw, symbol=near, now=T0)
    _trigger(logs, raw, symbol=far, now=T0 + timedelta(hours=2))
    priced = shadow.fill_offwatch_signal_prices(
        {}, watchlist=set(), sensor_cfg=sensor
    )
    assert calls == [near]
    assert priced[near] == 4.0
    assert far not in priced

    mid = "MID/USDT"
    later = "LATER/USDT"
    sensor["shadow_price_fetch_max_per_poll"] = 2
    shadow._PRICE_FETCH_CURSOR = 0
    calls.clear()
    _trigger(logs, raw, symbol=mid, now=T0 + timedelta(hours=1))
    _trigger(logs, raw, symbol=later, now=T0 + timedelta(hours=3))
    shadow.fill_offwatch_signal_prices({}, watchlist=set(), sensor_cfg=sensor)
    assert calls == [near, mid]
    calls.clear()
    shadow.fill_offwatch_signal_prices({}, watchlist=set(), sensor_cfg=sensor)
    assert calls == [near, far]
    calls.clear()
    shadow.fill_offwatch_signal_prices({}, watchlist=set(), sensor_cfg=sensor)
    assert calls == [near, later]


def test_shadow_fetch_budget_keeps_next_poll_on_time(logs, monkeypatch):
    raw = _raw()
    sensor = raw["entry_sensor_15m"]
    cap = 5
    timeout_s = 2
    budget_s = 5
    sensor.update(_fetch_keys(max_per_poll=cap, budget_s=budget_s, timeout_s=timeout_s))
    clock = {"t": 0.0}
    monkeypatch.setattr(shadow.time, "monotonic", lambda: clock["t"])
    calls = []

    def _hang(symbol, timeout=6):
        calls.append((symbol, timeout))
        clock["t"] += float(timeout)
        raise TimeoutError("hung")

    monkeypatch.setattr("price_fetcher._fetch_gate_single", _hang)
    for index in range(cap):
        _trigger(
            logs,
            raw,
            symbol=f"H{index}/USDT",
            now=T0 + timedelta(minutes=index),
        )
    started = clock["t"]
    shadow.fill_offwatch_signal_prices({}, watchlist=set(), sensor_cfg=sensor)
    elapsed = clock["t"] - started
    assert elapsed <= budget_s
    assert len(calls) < cap
    assert calls
    assert all(item[1] <= timeout_s for item in calls)
    next_start = clock["t"]
    shadow.fill_offwatch_signal_prices({}, watchlist=set(), sensor_cfg=sensor)
    assert clock["t"] - next_start <= budget_s


def test_no_price_reason_list_is_closed(logs, monkeypatch):
    assert shadow._NO_PRICE_REASONS == _CLOSED_NO_PRICE_REASONS
    raw = _raw()
    sensor = raw["entry_sensor_15m"]
    sensor.update(_fetch_keys())

    def _boom(symbol, timeout=6):
        raise RuntimeError("ticker down")

    monkeypatch.setattr("price_fetcher._fetch_gate_single", _boom)
    opened = datetime.now(timezone.utc) - timedelta(hours=5)
    _trigger(
        logs,
        raw,
        batch={SIG: 1.25, CTRL: 2.0},
        cache={CTRL: {"market_cap_usd": 5_000_000, "quote_volume_24h_usdt": 50_000}},
        now=opened,
    )
    priced = shadow.fill_offwatch_signal_prices({}, watchlist=set(), sensor_cfg=sensor)
    exits = shadow.check_open_exits(
        priced,
        price_source="gate_spot_last",
        watchlist=set(),
        now=datetime.now(timezone.utc),
    )
    signal = [row for row in exits if not row["control"]][0]
    control = [row for row in exits if row["control"]][0]
    assert signal["no_price_reason"] == "fetch_error"
    assert control["no_price_reason"] == "cap_reached"
    for row in (signal, control):
        assert row["no_price_reason"] in _CLOSED_NO_PRICE_REASONS
    summary = shadow.build_daily_summary(datetime.now(timezone.utc).date().isoformat())
    assert summary["no_price_by_reason"]["signals"]["fetch_error"] == 1
    assert summary["no_price_by_reason"]["controls"]["fetch_error"] == 0
    assert summary["no_price_by_reason"]["controls"]["cap_reached"] == 1
    assert summary["no_price_by_reason"]["signals"]["cap_reached"] == 0


def test_missing_fetch_config_disables_single_fetch(logs, monkeypatch):
    calls = []

    def _single(symbol, timeout=6):
        calls.append(symbol)
        return 3.0

    monkeypatch.setattr("price_fetcher._fetch_gate_single", _single)
    raw = _raw()
    opened = datetime.now(timezone.utc) - timedelta(hours=5)
    _trigger(logs, raw, symbol=SIG, now=opened)
    orch, batch_calls, _fetched = _arm_shadow_poll(monkeypatch, raw, [OTHER])
    entry_sensor_loop._poll_once(orch)
    assert calls == []
    assert SIG not in batch_calls[0]
    exits = [
        row
        for row in _of(logs, "shadow_exit")
        if not row.get("control") and row.get("exit_reason") == "no_price"
    ]
    assert exits
    assert exits[0]["no_price_reason"] == "fetch_disabled_config"
    assert any(
        level == "ERROR" and "shadow price fetch disabled" in message
        for level, message in logs
    )

    raw_bad = _raw()
    raw_bad["entry_sensor_15m"].update(_fetch_keys())
    raw_bad["entry_sensor_15m"]["shadow_price_fetch_budget_s"] = 0
    calls.clear()
    _trigger(logs, raw_bad, symbol="BAD/USDT", now=opened)
    orch_bad, _batch_bad, _fetched_bad = _arm_shadow_poll(monkeypatch, raw_bad, [OTHER])
    entry_sensor_loop._poll_once(orch_bad)
    assert "BAD/USDT" not in calls
    bad_exit = [
        row
        for row in _of(logs, "shadow_exit")
        if row.get("symbol") is None or row.get("exit_reason") == "no_price"
    ]
    reasons = {row.get("no_price_reason") for row in bad_exit}
    assert "fetch_disabled_config" in reasons


def test_open_slots_read_tenant_snapshot_without_lock(logs):
    from core.tenant_context import tenant_context
    from strategies import positions as posmod

    foreign = "shadow-tenant-b"
    missing = "shadow-tenant-empty"
    key = (foreign, "paper")
    missing_key = (missing, "paper")
    saved_positions = dict(posmod.positions)
    saved_stores = dict(posmod._position_stores)
    saved_lock = posmod._positions_lock
    lock_calls = []
    ensure_calls = []
    real_ensure = posmod._ensure_store

    class _RecordingLock:
        def acquire(self, *args, **kwargs):
            lock_calls.append(1)
            return saved_lock.acquire(*args, **kwargs)

        def release(self):
            return saved_lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *args):
            self.release()

    def _ensure(store_key):
        ensure_calls.append(store_key)
        return real_ensure(store_key)

    posmod._positions_lock = _RecordingLock()
    posmod._ensure_store = _ensure
    try:
        posmod.positions.clear()
        posmod.positions["A"] = {"amount": 5}
        posmod.positions["B"] = {"amount": 5}
        posmod._position_stores.pop(key, None)
        posmod._position_stores.pop(missing_key, None)
        with tenant_context(missing, scope="paper"):
            assert shadow._peek_open_slots(_raw()) is None
            assert missing_key not in posmod._position_stores
        posmod._position_stores[key] = {"LOT": {"amount": 5}}
        with tenant_context(foreign, scope="paper"):
            assert shadow._peek_open_slots(_raw()) == 1
        assert ensure_calls == []
        assert lock_calls == []
    finally:
        posmod._positions_lock = saved_lock
        posmod._ensure_store = real_ensure
        posmod.positions.clear()
        posmod.positions.update(saved_positions)
        posmod._position_stores.clear()
        posmod._position_stores.update(saved_stores)
        posmod._position_stores.pop(key, None)
        posmod._position_stores.pop(missing_key, None)


def test_stop_ratio_comes_from_tenant_config(logs, monkeypatch):
    """0.5 on the passed config wins over 0.67 on the process config."""
    strategies = [
        {
            "symbol": "SOL/USDT",
            "timeframe": "4h",
            "stop_loss_pct": 40,
            "description": "SOL 4h Live — Large-Cap",
        },
        {
            "symbol": "ARIA/USDT",
            "timeframe": "4h",
            "stop_loss_pct": 40,
            "description": "ARIA 4h Live — Meme/High-Vol",
        },
    ]
    process = _raw()
    process["stop_loss_pct"] = 40
    process["risk"]["partial_stop_ratio"] = 0.67
    process["volatile_altcoin"] = {"enabled": True}
    process["strategies"] = strategies
    tenant = _raw()
    tenant["stop_loss_pct"] = 40
    tenant["risk"]["partial_stop_ratio"] = 0.5
    tenant["volatile_altcoin"] = {"enabled": True}
    tenant["strategies"] = strategies
    process_cfg = BotConfig(raw=process)
    monkeypatch.setattr("core.config.get_bot_config", lambda: process_cfg)
    monkeypatch.setattr("strategies.registry.get_bot_config", lambda: process_cfg)
    monkeypatch.setattr(
        "strategies.entry_recipe.load_personal_params", lambda *args, **kwargs: None
    )
    expected_tier = {
        "SOL/USDT": "stable",
        "ARIA/USDT": "volatile",
        "STABLE/USDT": "stable",
    }
    for symbol, tier in expected_tier.items():
        row = _trigger(logs, tenant, symbol=symbol, price=100.0, now=T0)
        assert row["partial_stop_ratio"] == pytest.approx(0.5)
        assert row["stop_full_pct"] == pytest.approx(0.40)
        assert row["stop_partial_pct"] == pytest.approx(0.20)
        assert row["stop_pct"] == pytest.approx(0.20)
        assert row["volatility_tier"] == tier
        assert row["stop_pct_source"] == shadow.STOP_PCT_SOURCE


def test_symbols_guard_poll_continues(logs, monkeypatch):
    warnings = []

    def _boom():
        raise RuntimeError("symbols blew up")

    monkeypatch.setattr(shadow, "open_signal_symbols", _boom)
    raw = _raw()
    orch, _batch, fetched = _arm_shadow_poll(monkeypatch, raw, [SIG], warnings=warnings)
    continued = True
    try:
        entry_sensor_loop._poll_once(orch)
    except Exception:
        continued = False
    assert continued
    assert fetched == [SIG]
    assert any(
        level == "WARNING" and "shadow_log_error symbols" in message
        for level, message in warnings
    )


def test_candidates_guard_poll_continues(logs, monkeypatch):
    warnings = []

    def _boom(*args, **kwargs):
        raise RuntimeError("candidates blew up")

    monkeypatch.setattr(shadow, "candidate_info_from_coin", _boom)
    raw = _raw()
    orch, _batch, fetched = _arm_shadow_poll(monkeypatch, raw, [SIG, OTHER], warnings=warnings)
    continued = True
    try:
        entry_sensor_loop._poll_once(orch)
    except Exception:
        continued = False
    assert continued
    assert set(fetched) == {SIG, OTHER}
    assert any(
        level == "WARNING" and "shadow_log_error candidates" in message
        for level, message in warnings
    )


def test_restart_guard_loop_starts(logs, monkeypatch):
    warnings = []
    raw = _raw()
    cfg = BotConfig(raw=raw)

    def _boom():
        raise RuntimeError("restart blew up")

    monkeypatch.setattr(entry_sensor_loop, "get_bot_config", lambda: cfg)
    monkeypatch.setattr(
        entry_sensor_loop,
        "log",
        lambda message, level="INFO": warnings.append((level, message)),
    )
    monkeypatch.setattr(entry_sensor_loop, "_loop_main", lambda orchestrator: None)
    monkeypatch.setattr(entry_sensor_loop, "_maybe_seed_watches", lambda *args, **kwargs: 0)
    monkeypatch.setattr(shadow, "resolve_restart", _boom)
    entry_sensor_loop.reset_poll_state_for_tests()
    continued = True
    thread = None
    try:
        thread = entry_sensor_loop.start_entry_sensor_loop(MagicMock())
    except Exception:
        continued = False
    finally:
        entry_sensor_loop.stop_entry_sensor_loop()
        entry_sensor_loop.reset_poll_state_for_tests()
    assert continued
    assert thread is not None
    assert any(
        level == "WARNING" and "shadow_log_error restart" in message
        for level, message in warnings
    )


def test_orchestrator_lookup_guard_buy_continues(logs, monkeypatch):
    warnings = []

    def _boom(symbol):
        raise RuntimeError("lookup blew up")

    def _log(message, level="INFO"):
        warnings.append((level, message))

    monkeypatch.setattr(shadow, "lookup_open_signal_id", _boom)
    monkeypatch.setattr("services.signal_orchestrator.log", _log)
    monkeypatch.setattr("services.signal_orchestrator.get_position", lambda *args, **kwargs: {})
    from services.signal_orchestrator import SignalOrchestrator

    orch = SignalOrchestrator.__new__(SignalOrchestrator)
    orch.audit = MagicMock()
    analysis = SimpleNamespace(
        action="BUY",
        normalized_action="BUY",
        symbol=SIG,
        timeframe="4h",
        rationale="spike",
        sources=["entry_sensor_15m"],
        confidence=0.5,
    )
    orch.analyze = lambda *args, **kwargs: analysis
    trade = SimpleNamespace(
        executed=True,
        order_type="BUY",
        message="filled",
        pnl=1.0,
        usdt_amount=10.0,
    )
    orch.execute_if_needed = lambda *args, **kwargs: trade
    continued = True
    out = None
    try:
        out = orch.process_entry_sensor({"symbol": SIG, "timeframe": "4h"}, 1.25)
    except Exception:
        continued = False
    assert continued
    assert out is not None
    assert out["action"] == "BUY"
    assert out["executed"] is True
    assert out["trade_message"] == "filled"
    assert out["shadow_signal_id"] is None
    assert any(
        level == "WARNING" and "shadow_log_error link" in message
        for level, message in warnings
    )
