"""Spec #687 shadow signal log. No network. No writes under data/."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.config import BotConfig
from services import entry_sensor_loop, shadow_signal_log as shadow

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
        "quote_volume_24h_usdt": 1_000_000,
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
    assert got["stop_pct"] == pytest.approx(0.20)
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

    monkeypatch.setattr(entry_sensor_loop, "get_bot_config", lambda: cfg)
    monkeypatch.setattr(entry_sensor_loop, "get_gate_prices_batch", _batch)
    monkeypatch.setattr("price_fetcher.get_ticker_price", _ticker)
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

    def _poll():
        entry_sensor_loop.reset_poll_state_for_tests()
        watch_15m_state.reset_cache_for_tests()
        watch_15m_state.set_watch(SIG, "4h")
        orch = MagicMock()
        orch.market.fetch_ohlcv.return_value = object()
        orch.market.compute_15m_sensor_metrics.return_value = {
            "volume_spike_ratio": 0.1,
            "body_atr_ratio": 0.0,
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
    _poll()
    assert len(batch_calls) == 2
    assert batch_calls[1] == without
    assert CTRL not in batch_calls[1]
    assert ticker_calls == []

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
    assert summary["restart_unresolved_signals"] >= 1
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
