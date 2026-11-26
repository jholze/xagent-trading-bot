"""#836 regime gate on climax_fade opens, and the EMA48/480 trend-short shadow.

The gate is in front of the open fetch only. Cover doors and the climax
formula stay on their existing tests.
"""

from __future__ import annotations

import inspect
import json
import os
from datetime import datetime, timezone

import pytest

from services.market_oracle.score import score_is_fresh
from services.signal_orchestrator import (
    SignalOrchestrator,
    reset_climax_regime_gate_log_for_tests,
)
from services.trend_short_shadow import (
    ema_cross,
    ema_series,
    ema_spans,
    observe_trend_short,
    reset_for_tests as reset_trend_shadow,
    trend_short_shadow_config,
)
from strategies.climax_fade import (
    climax_fade_config,
    format_regime_gate_line,
    last_closed_4h_open_ms,
    regime_gate_verdict,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NOW = datetime(2026, 6, 1, 12, 30, tzinfo=timezone.utc)
_FOUR_H_MS = 4 * 3600 * 1000


@pytest.fixture(autouse=True)
def _reset_gate_state():
    reset_climax_regime_gate_log_for_tests()
    reset_trend_shadow()
    yield
    reset_climax_regime_gate_log_for_tests()
    reset_trend_shadow()


def _raw(mode="enforce", max_score=-0.2, *, enabled=True, shorts_on=True, stale_after=100.0):
    gate = {"mode": mode, "max_score": max_score}
    oracle = {"default_mode": "off"}
    if stale_after is not None:
        oracle["stale_after_sec"] = stale_after
    return {
        "shorts": {
            "enabled": shorts_on,
            "climax_fade": {"enabled": enabled, "regime_gate": gate},
        },
        "rule_switches": {"oracle_score": oracle},
    }


def _fields(line: str) -> dict[str, str]:
    out = {}
    for part in str(line).split():
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        out[key] = value
    return out


class _Frame:
    empty = False

    def __init__(self, closes, stamps):
        self._closes = list(closes)
        self._stamps = list(stamps)

    def __getitem__(self, key):
        if key == "close":
            return _Col(self._closes)
        if key == "ts":
            return _Col(self._stamps)
        raise KeyError(key)


class _Col:
    def __init__(self, values):
        self._values = list(values)

    def tolist(self):
        return list(self._values)


def _frame(closes, now=NOW):
    """Closed bars ending on the clock's last closed 4h bar, plus one forming bar."""
    now_ms = int(now.timestamp() * 1000)
    forming = (now_ms // _FOUR_H_MS) * _FOUR_H_MS
    last_closed = forming - _FOUR_H_MS
    n = len(closes)
    stamps = [last_closed - (n - 1 - i) * _FOUR_H_MS for i in range(n)]
    stamps.append(forming)
    return _Frame(list(closes) + [float(closes[-1]) * 50.0], stamps)


def _orch(raw, fetch, maybe=None):
    orch = SignalOrchestrator.__new__(SignalOrchestrator)
    orch.config = type("Cfg", (), {"raw": raw})()
    market = type("M", (), {})()
    market.fetch_ohlcv = fetch
    trading = type("T", (), {})()
    trading._maybe_climax_fade_short = maybe or (lambda *a, **k: "opened")
    orch.market = market
    orch.trading = trading
    return orch


def _patch_clock(monkeypatch, logs, block):
    monkeypatch.setattr("services.signal_orchestrator._utc_now", lambda: NOW)

    def _reader():
        if isinstance(block, BaseException):
            raise block
        return block

    def _no_disk():
        raise AssertionError("regime gate must not read the committed config file")

    monkeypatch.setattr("services.market_oracle.score_runtime.read_published_score", _reader)
    monkeypatch.setattr("services.market_oracle.score_runtime.committed_oracle_score", _no_disk)
    monkeypatch.setattr(
        "services.signal_orchestrator.log",
        lambda message, level="INFO": logs.append((level, str(message))),
    )


def _gate_lines(logs):
    return [text for _level, text in logs if text.startswith("climax_fade_regime_gate ")]


def test_last_closed_4h_open_uses_the_bar_that_already_ended():
    noon = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    eight = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)
    four = datetime(2026, 6, 1, 4, 0, tzinfo=timezone.utc)
    assert last_closed_4h_open_ms(noon) == int(eight.timestamp() * 1000)
    assert last_closed_4h_open_ms(NOW) == int(eight.timestamp() * 1000)
    assert last_closed_4h_open_ms(eight) == int(four.timestamp() * 1000)


def test_verdict_blocks_above_missing_and_stale_and_passes_at_max():
    gate = {"mode": "enforce", "max_score": -0.2}
    above = regime_gate_verdict(gate, smoothed=0.0, fresh=True)
    missing = regime_gate_verdict(gate, smoothed=None, fresh=False)
    stale = regime_gate_verdict(gate, smoothed=-0.5, fresh=False)
    at_max = regime_gate_verdict(gate, smoothed=-0.2, fresh=True)
    below = regime_gate_verdict(gate, smoothed=-0.8, fresh=True)
    assert above["block"] is True and above["reason"] == "above_max"
    assert missing["block"] is True and missing["reason"] == "missing"
    assert stale["block"] is True and stale["reason"] == "stale"
    assert at_max["block"] is False and at_max["verb"] is None
    assert below["block"] is False
    shadow = regime_gate_verdict({"mode": "shadow", "max_score": -0.2}, smoothed=0.0, fresh=True)
    assert shadow["block"] is False and shadow["verb"] == "would_block"
    assert regime_gate_verdict({"mode": "off"}, smoothed=None, fresh=False)["block"] is False


def test_score_is_fresh_is_the_835_helper():
    # Pin the reused helper: age == limit is fresh, one second over is not.
    assert score_is_fresh(1000.0, 1100.0, 100) is True
    assert score_is_fresh(1000.0, 1101.0, 100) is False
    assert score_is_fresh(None, 1100.0, 100) is False


def test_enforce_blocks_above_max_before_fetch(monkeypatch):
    logs = []
    now_ts = NOW.timestamp()
    _patch_clock(monkeypatch, logs, {"smoothed": 0.0, "ts": now_ts})
    fetches = []
    orch = _orch(_raw(), lambda *a, **k: fetches.append("fetch"))
    assert orch._try_climax_fade_open("AAA/USDT", 1.0) is None
    assert fetches == []
    fields = _fields(_gate_lines(logs)[0])
    assert fields["symbol"] == "AAA/USDT"
    assert fields["s"] == "0"
    assert fields["max"] == "-0.2"
    assert fields["reason"] == "above_max"
    assert _gate_lines(logs)[0].startswith("climax_fade_regime_gate blocked ")


def test_enforce_blocks_when_the_score_is_missing(monkeypatch):
    logs = []
    _patch_clock(monkeypatch, logs, None)
    fetches = []
    orch = _orch(_raw(), lambda *a, **k: fetches.append("fetch"))
    assert orch._try_climax_fade_open("AAA/USDT", 1.0) is None
    assert fetches == []
    assert _fields(_gate_lines(logs)[0])["reason"] == "missing"
    assert _fields(_gate_lines(logs)[0])["s"] == "na"


def test_enforce_blocks_when_the_score_is_stale(monkeypatch):
    logs = []
    now_ts = NOW.timestamp()
    _patch_clock(monkeypatch, logs, {"smoothed": -0.5, "ts": now_ts - 101})
    fetches = []
    orch = _orch(_raw(), lambda *a, **k: fetches.append("fetch"))
    assert orch._try_climax_fade_open("AAA/USDT", 1.0) is None
    assert fetches == []
    fields = _fields(_gate_lines(logs)[0])
    assert fields["reason"] == "stale"
    assert fields["s"] == "-0.5"
    assert fields["age_s"] == "101"

    # A missing in-memory window is not fresh. Do not fall back to the config file.
    logs.clear()
    reset_climax_regime_gate_log_for_tests()
    _patch_clock(monkeypatch, logs, {"smoothed": -0.5, "ts": now_ts})
    bare = _orch(_raw(stale_after=None), lambda *a, **k: fetches.append("fetch"))
    assert bare._try_climax_fade_open("CCC/USDT", 1.0) is None
    assert fetches == []
    assert _fields(_gate_lines(logs)[0])["reason"] == "stale"


def test_enforce_opens_at_and_below_max_score(monkeypatch):
    logs = []
    now_ts = NOW.timestamp()
    order = []

    def fetch(*_a, **_k):
        order.append("fetch")
        return None

    def closed(_df, _now):
        order.append("closed")
        return {"ts_ms": 1}

    def maybe(*_a, **_k):
        order.append("maybe")
        return "opened"

    monkeypatch.setattr("strategies.climax_fade.closed_bar_from_ohlcv", closed)
    _patch_clock(monkeypatch, logs, {"smoothed": -0.2, "ts": now_ts})
    orch = _orch(_raw(), fetch, maybe)
    assert orch._try_climax_fade_open("AAA/USDT", 1.0) == "opened"
    assert order == ["fetch", "closed", "maybe"]
    assert _gate_lines(logs) == []

    order.clear()
    _patch_clock(monkeypatch, logs, {"smoothed": -0.8, "ts": now_ts})
    assert orch._try_climax_fade_open("BBB/USDT", 1.0) == "opened"
    assert order == ["fetch", "closed", "maybe"]


def test_shadow_logs_would_block_and_still_opens(monkeypatch):
    logs = []
    now_ts = NOW.timestamp()
    order = []

    def fetch(*_a, **_k):
        order.append("fetch")
        return None

    def closed(_df, _now):
        order.append("closed")
        return {"ts_ms": 1}

    def maybe(*_a, **_k):
        order.append("maybe")
        return "opened"

    monkeypatch.setattr("strategies.climax_fade.closed_bar_from_ohlcv", closed)
    _patch_clock(monkeypatch, logs, {"smoothed": 0.4, "ts": now_ts})
    orch = _orch(_raw("shadow"), fetch, maybe)
    assert orch._try_climax_fade_open("AAA/USDT", 1.0) == "opened"
    assert order == ["fetch", "closed", "maybe"]
    line = _gate_lines(logs)[0]
    assert line.startswith("climax_fade_regime_gate would_block ")
    assert _fields(line)["reason"] == "above_max"


def test_off_does_not_read_the_score_and_still_opens(monkeypatch):
    logs = []
    order = []

    def fetch(*_a, **_k):
        order.append("fetch")
        return None

    def closed(_df, _now):
        return {"ts_ms": 1}

    def maybe(*_a, **_k):
        order.append("maybe")
        return "opened"

    def _reader():
        raise AssertionError("off mode must not read the score")

    def _no_disk():
        raise AssertionError("off mode must not read the committed config file")

    monkeypatch.setattr("strategies.climax_fade.closed_bar_from_ohlcv", closed)
    monkeypatch.setattr("services.signal_orchestrator._utc_now", lambda: NOW)
    monkeypatch.setattr("services.market_oracle.score_runtime.read_published_score", _reader)
    monkeypatch.setattr("services.market_oracle.score_runtime.committed_oracle_score", _no_disk)
    monkeypatch.setattr(
        "services.signal_orchestrator.log",
        lambda message, level="INFO": logs.append(str(message)),
    )
    orch = _orch(_raw("off"), fetch, maybe)
    assert orch._try_climax_fade_open("AAA/USDT", 1.0) == "opened"
    assert order == ["fetch", "maybe"]
    missing = {
        "shorts": {"enabled": True, "climax_fade": {"enabled": True}},
    }
    bare = _orch(missing, fetch, maybe)
    assert bare._try_climax_fade_open("AAA/USDT", 1.0) == "opened"


def test_gate_log_is_once_per_symbol_per_closed_bar(monkeypatch):
    logs = []
    _patch_clock(monkeypatch, logs, None)
    orch = _orch(_raw(), lambda *a, **k: None)
    assert orch._try_climax_fade_open("AAA/USDT", 1.0) is None
    assert orch._try_climax_fade_open("AAA/USDT", 1.0) is None
    assert len(_gate_lines(logs)) == 1
    assert orch._try_climax_fade_open("BBB/USDT", 1.0) is None
    assert len(_gate_lines(logs)) == 2


def test_reader_error_fails_closed_in_enforce_and_not_in_shadow(monkeypatch):
    logs = []
    fetches = []
    _patch_clock(monkeypatch, logs, RuntimeError("redis down"))
    orch = _orch(_raw(), lambda *a, **k: fetches.append(1))
    assert orch._try_climax_fade_open("AAA/USDT", 1.0) is None
    assert fetches == []
    assert _fields(_gate_lines(logs)[0])["reason"] == "error"

    logs.clear()
    reset_climax_regime_gate_log_for_tests()
    _patch_clock(monkeypatch, logs, RuntimeError("redis down"))

    def fetch(*_a, **_k):
        fetches.append("fetch")
        return None

    monkeypatch.setattr(
        "strategies.climax_fade.closed_bar_from_ohlcv", lambda *_a, **_k: {"ts_ms": 1}
    )
    shadow = _orch(_raw("shadow"), fetch, lambda *a, **k: "opened")
    assert shadow._try_climax_fade_open("AAA/USDT", 1.0) == "opened"
    assert "fetch" in fetches
    assert _fields(_gate_lines(logs)[0])["reason"] == "error"
    assert _gate_lines(logs)[0].startswith("climax_fade_regime_gate would_block ")


def test_unknown_mode_does_not_loosen(monkeypatch):
    logs = []
    _patch_clock(monkeypatch, logs, {"smoothed": 0.2, "ts": NOW.timestamp()})
    fetches = []
    orch = _orch(_raw("banana"), lambda *a, **k: fetches.append(1))
    assert orch._try_climax_fade_open("AAA/USDT", 1.0) is None
    assert fetches == []
    assert _fields(_gate_lines(logs)[0])["reason"] == "above_max"


def test_gate_config_does_not_change_climax_thresholds():
    cfg = climax_fade_config(
        {
            "shorts": {
                "climax_fade": {
                    "enabled": True,
                    "regime_gate": {"mode": "enforce", "max_score": -0.2},
                }
            }
        }
    )
    assert cfg["min_return_pct"] == 6.0
    assert cfg["vol_mult_min"] == 2.0
    assert cfg["cover_close_pct"] == 3.0
    assert cfg["stop_price_pct"] == 10.0
    assert cfg["time_cap_hours"] == 16
    assert cfg["size_factor"] == 0.5
    assert cfg["regime_gate"]["mode"] == "enforce"
    assert cfg["regime_gate"]["max_score"] == -0.2
    assert climax_fade_config({"shorts": {}})["regime_gate"]["mode"] == "off"


def test_cover_path_does_not_read_the_regime_score():
    src = inspect.getsource(SignalOrchestrator._cycle_short_cover)
    assert "read_published_score" not in src
    assert "regime_gate" not in src
    opened = inspect.getsource(SignalOrchestrator._try_climax_fade_open)
    assert opened.index("_climax_regime_gate_allows") < opened.index("fetch_ohlcv")
    gate_src = inspect.getsource(SignalOrchestrator._climax_regime_gate_allows)
    assert gate_src.index('gate.get("mode") == "off"') < gate_src.index("read_published_score")
    assert "committed_oracle_score" not in gate_src
    assert "load_score_state" not in gate_src
    assert "score_is_fresh" in gate_src


def test_gate_line_shape():
    line = format_regime_gate_line(
        "AAA/USDT",
        {"verb": "blocked", "s": -0.2, "max_score": -0.2, "reason": "stale"},
        1801,
    )
    assert line == (
        "climax_fade_regime_gate blocked symbol=AAA/USDT "
        "s=-0.2 age_s=1801 max=-0.2 reason=stale"
    )


def test_committed_config_pins_the_gate_and_leaves_the_kill_switches():
    with open(os.path.join(REPO_ROOT, "config.json"), encoding="utf-8") as handle:
        disk = json.load(handle)
    shorts = disk["shorts"]
    assert shorts["enabled"] is True
    assert shorts["allow_live"] is False
    assert shorts["auto_after_sell"] is False
    fade = shorts["climax_fade"]
    assert fade["enabled"] is True
    assert fade["min_return_pct"] == 6.0
    assert fade["size_factor"] == 0.5
    assert fade["regime_gate"]["mode"] == "enforce"
    assert fade["regime_gate"]["max_score"] == -0.2
    oracle = disk["rule_switches"]["oracle_score"]
    assert "mode" not in oracle
    assert oracle["default_mode"] == "off"
    assert oracle["stale_after_sec"] == 2400
    shadow = shorts["trend_short_shadow"]
    assert shadow["enabled"] is True
    assert shadow["fast_hours"] == 48
    assert shadow["slow_hours"] == 480
    assert shadow["timeframe"] == "4h"
    assert shadow["warmup_mult"] == 3
    assert shadow["round_trip_cost_pct"] == pytest.approx(0.70)
    spans = ema_spans(trend_short_shadow_config({"shorts": disk["shorts"]}))
    assert spans == (12, 120, 360)


def test_ema_seed_and_update_match_the_hand_series():
    series = ema_series([1.0, 2.0, 3.0, 4.0, 6.0], 3)
    assert series[0] is None and series[1] is None
    assert series[2] == 2.0
    assert series[3] == 3.0
    assert series[4] == 4.5


def test_ema_cross_directions():
    assert ema_cross(2.0, 2.0, 1.0, 2.0) == "would_open"
    assert ema_cross(1.0, 2.0, 3.0, 2.0) == "would_cover"
    assert ema_cross(3.0, 2.0, 2.5, 2.0) is None
    assert ema_cross(2.0, 2.0, 2.0, 2.0) is None


def _shadow_raw():
    return {
        "shorts": {
            "climax_fade": {"exclude_symbols": ["BTC/USDT", "ETH/USDT"]},
            "trend_short_shadow": {
                "enabled": True,
                "fast_hours": 8,
                "slow_hours": 12,
                "timeframe": "4h",
                "warmup_mult": 3,
                "round_trip_cost_pct": 0.7,
            },
        }
    }


def _run_shadow(monkeypatch, closes, symbol="AAA/USDT", raw=None, score=-0.4):
    logs = []
    rows = []
    fetches = []
    frame = _frame(closes)
    raw = _shadow_raw() if raw is None else raw

    def fetch(sym, timeframe, limit):
        fetches.append((sym, timeframe, limit))
        return frame

    monkeypatch.setattr(
        "logger.log", lambda message, level="INFO": logs.append(str(message))
    )
    monkeypatch.setattr(
        "services.observe_streams.mirror_record",
        lambda collection, row: rows.append((collection, row)),
    )
    monkeypatch.setattr(
        "services.market_oracle.score_runtime.read_published_score",
        lambda: {"smoothed": score, "ts": NOW.timestamp()},
    )
    market = type("M", (), {})()
    market.fetch_ohlcv = fetch
    row = observe_trend_short(symbol, market, raw, now=NOW)
    return row, logs, rows, fetches


def test_trend_shadow_emits_would_open_and_would_cover(monkeypatch):
    opened, logs, rows, fetches = _run_shadow(
        monkeypatch, [10.0] * 5 + [16.0, 16.0, 4.0]
    )
    assert opened is not None
    assert opened["event"] == "would_open"
    assert opened["close"] == 4.0
    assert opened["ema_fast"] == pytest.approx(7.777777777777778)
    assert opened["ema_slow"] == pytest.approx(9.25)
    assert opened["s"] == pytest.approx(-0.4)
    assert opened["excluded"] is False
    assert opened["fast_span"] == 2
    assert opened["slow_span"] == 3
    assert opened["round_trip_cost_pct"] == pytest.approx(0.70)
    assert fetches == [("AAA/USDT", "4h", 9)]
    assert rows[0][0] == "obs_trend_short_shadow"
    assert rows[0][1]["event"] == "would_open"
    line = [item for item in logs if item.startswith("trend_short_shadow ")][0]
    assert line.startswith("trend_short_shadow would_open ")
    fields = _fields(line)
    assert fields["symbol"] == "AAA/USDT"
    assert fields["close"] == "4"
    assert fields["s"] == "-0.4"
    assert fields["excluded"] == "false"

    reset_trend_shadow()
    covered, logs, _rows, _fetches = _run_shadow(monkeypatch, [10.0] * 5 + [16.0])
    assert covered["event"] == "would_cover"
    assert any(item.startswith("trend_short_shadow would_cover ") for item in logs)


def test_trend_shadow_is_once_per_closed_bar_and_tags_excluded(monkeypatch):
    fetches = []

    def fetch(sym, timeframe, limit):
        fetches.append(sym)
        return _frame([10.0] * 5 + [16.0, 16.0, 4.0])

    market = type("M", (), {})()
    market.fetch_ohlcv = fetch
    monkeypatch.setattr("logger.log", lambda *a, **k: None)
    monkeypatch.setattr("services.observe_streams.mirror_record", lambda *a, **k: None)
    monkeypatch.setattr(
        "services.market_oracle.score_runtime.read_published_score",
        lambda: {"smoothed": -0.4, "ts": NOW.timestamp()},
    )
    raw = _shadow_raw()
    first = observe_trend_short("BTC/USDT", market, raw, now=NOW)
    second = observe_trend_short("BTC/USDT", market, raw, now=NOW)
    assert first is not None and first["excluded"] is True
    assert second is None
    assert fetches == ["BTC/USDT"]


def test_flat_series_does_not_emit_and_disabled_does_not_fetch(monkeypatch):
    fetches = []

    def fetch(*_a, **_k):
        fetches.append(1)
        return _frame([10.0] * 8)

    market = type("M", (), {})()
    market.fetch_ohlcv = fetch
    monkeypatch.setattr("logger.log", lambda *a, **k: None)
    monkeypatch.setattr("services.observe_streams.mirror_record", lambda *a, **k: None)
    monkeypatch.setattr(
        "services.market_oracle.score_runtime.read_published_score", lambda: None
    )
    assert observe_trend_short("AAA/USDT", market, _shadow_raw(), now=NOW) is None
    assert fetches == [1]
    raw = _shadow_raw()
    raw["shorts"]["trend_short_shadow"]["enabled"] = False
    assert observe_trend_short("BBB/USDT", market, raw, now=NOW) is None
    assert fetches == [1]


def test_trend_shadow_never_calls_the_order_path_and_swallows_errors(monkeypatch):
    import services.trading_service as trading_service

    src = inspect.getsource(
        __import__("services.trend_short_shadow", fromlist=["observe_trend_short"])
    )
    assert "execute_order" not in src
    assert "trading_service" not in src
    called = []
    monkeypatch.setattr(
        trading_service.TradingService,
        "execute_order",
        lambda self, *a, **k: called.append("order"),
    )
    logs = []
    monkeypatch.setattr(
        "logger.log", lambda message, level="INFO": logs.append(str(message))
    )

    def boom(*_a, **_k):
        raise RuntimeError("ohlcv down")

    market = type("M", (), {})()
    market.fetch_ohlcv = boom
    assert observe_trend_short("AAA/USDT", market, _shadow_raw(), now=NOW) is None
    assert called == []
    assert any("trend_short_shadow error" in line for line in logs)
    # A second failure in the same bar is logged once.
    assert observe_trend_short("AAA/USDT", market, _shadow_raw(), now=NOW) is None
    assert sum("trend_short_shadow error" in line for line in logs) == 1


def test_process_coin_calls_the_shadow_and_not_from_the_cover_path():
    src = inspect.getsource(SignalOrchestrator.process_coin)
    assert "observe_trend_short" in src
    cover = inspect.getsource(SignalOrchestrator._cycle_short_cover)
    assert "observe_trend_short" not in cover
    assert "execute_order" not in inspect.getsource(
        __import__("services.trend_short_shadow", fromlist=["x"])
    )
