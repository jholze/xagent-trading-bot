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


def test_at_lower_bb_true_when_close_near_lower():
    rows = [{"ts": i, "open": 100, "high": 101, "low": 99, "close": 100.0} for i in range(19)]
    rows.append({"ts": 19, "open": 80, "high": 90, "low": 40, "close": 50.0})
    out = build_ohlcv_pack(rows, rsi_period=14, bb_period=20)
    assert out["ok"] is True
    assert out["bb_lower"][-1] is not None
    assert out["at_lower_bb"] is True


def test_at_lower_bb_false_on_rising_series():
    out = build_ohlcv_pack(_rows(30), rsi_period=14, bb_period=20)
    assert out["ok"] is True
    assert out["at_lower_bb"] is False
    assert out["bb_lower"][-1] is not None
