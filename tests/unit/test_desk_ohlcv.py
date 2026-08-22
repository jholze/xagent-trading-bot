import json

import numpy as np

from services.desk.ohlcv import build_ohlcv_pack


def _rows(n: int, *, close0: float = 1.0, step: float = 0.001) -> list[dict]:
    return [
        {
            "ts": i,
            "open": 1,
            "high": 1.1,
            "low": 0.9,
            "close": close0 + i * step,
        }
        for i in range(n)
    ]


def test_bb_and_rsi_lengths_match_closes():
    rows = [{"ts": i, "open": 1, "high": 1.1, "low": 0.9, "close": 1.0 + i * 0.001} for i in range(30)]
    out = build_ohlcv_pack(rows, rsi_period=14, bb_period=20)
    assert len(out["closes"]) == 30
    assert len(out["rsi"]) == 30
    assert out["rsi"][-1] is None or 0 <= out["rsi"][-1] <= 100
    assert len(out["bb_upper"]) == 30
    assert len(out["bb_middle"]) == 30
    assert len(out["bb_lower"]) == 30
    assert out["ok"] is True
    assert out["closes"][-1] == rows[-1]["close"]


def test_warmup_bars_are_none_rows_not_dropped():
    rows = _rows(30)
    out = build_ohlcv_pack(rows, rsi_period=14, bb_period=20)
    assert len(out["bars"]) == 30
    assert all(v is None for v in out["rsi"][:14])
    assert out["rsi"][14] is not None
    assert all(v is None for v in out["bb_upper"][:19])
    assert all(v is None for v in out["bb_middle"][:19])
    assert all(v is None for v in out["bb_lower"][:19])
    assert out["bb_middle"][19] is not None
    assert out["last_rsi"] == out["rsi"][-1]


def test_empty_rows_unavailable():
    out = build_ohlcv_pack([])
    assert out == {"ok": False, "error": "ohlcv_unavailable", "bars": []}


def test_at_lower_bb_none_during_warmup():
    out = build_ohlcv_pack(_rows(10), rsi_period=14, bb_period=20)
    assert out["ok"] is True
    assert len(out["closes"]) == 10
    assert all(v is None for v in out["bb_lower"])
    assert out["at_lower_bb"] is None


def test_pack_json_safe_with_numpy_ts():
    rows = [{
        "ts": np.int64(i),
        "open": np.float64(1),
        "high": np.float64(1.1),
        "low": np.float64(0.9),
        "close": np.float64(1.0 + i * 0.001),
    } for i in range(30)]
    json.dumps(build_ohlcv_pack(rows))


def test_non_finite_ohlc_and_ts_become_none():
    rows = [{
        "ts": float("inf"),
        "open": float("inf"),
        "high": float("nan"),
        "low": 1.0,
        "close": 1.0,
    }]
    out = build_ohlcv_pack(rows)
    bar = out["bars"][0]
    assert bar["ts"] is None
    assert bar["open"] is None
    assert bar["high"] is None
    assert bar["low"] == 1.0
    json.dumps(out)


def test_at_lower_bb_true_when_close_near_lower():
    rows = [{"ts": i, "open": 100, "high": 101, "low": 99, "close": 100.0} for i in range(19)]
    rows.append({"ts": 19, "open": 80, "high": 90, "low": 40, "close": 50.0})
    out = build_ohlcv_pack(rows, rsi_period=14, bb_period=20)
    assert out["ok"] is True
    assert out["bb_lower"][-1] is not None
    assert out["at_lower_bb"] is True


def test_at_lower_bb_true_within_live_1_02_ratio():
    """1.5% above lower band still counts (live BB support 1.02, not 0.2%)."""
    rows = [{"ts": i, "open": 100, "high": 101, "low": 99, "close": 100.0} for i in range(19)]
    rows.append({"ts": 19, "open": 100, "high": 102, "low": 99, "close": 101.3})
    out = build_ohlcv_pack(rows, rsi_period=14, bb_period=20)
    lower = out["bb_lower"][-1]
    close = out["closes"][-1]
    assert lower is not None and close is not None
    ratio = close / lower
    assert ratio > 1.002
    assert ratio <= 1.02
    assert out["at_lower_bb"] is True


def test_at_lower_bb_false_beyond_live_1_02_ratio():
    rows = [{"ts": i, "open": 100, "high": 101, "low": 99, "close": 100.0} for i in range(19)]
    rows.append({"ts": 19, "open": 100, "high": 103, "low": 99, "close": 101.5})
    out = build_ohlcv_pack(rows, rsi_period=14, bb_period=20)
    lower = out["bb_lower"][-1]
    close = out["closes"][-1]
    assert lower is not None and close is not None
    assert close / lower > 1.02
    assert out["at_lower_bb"] is False


def test_at_lower_bb_false_on_rising_series():
    out = build_ohlcv_pack(_rows(30), rsi_period=14, bb_period=20)
    assert out["ok"] is True
    assert out["at_lower_bb"] is False
    assert out["bb_lower"][-1] is not None
