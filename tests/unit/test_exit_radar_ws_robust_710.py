"""#710 exit-radar WS robustness: stale-price guard, reconnect watchdog, backoff reset, real ATR."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from services.exit_realtime import hub as hub_mod
from services.exit_realtime.hub import next_backoff, resolve_row_atr
from tests.unit.test_ws_long_hard_stop_657 import _frame, _hub, _logs, _row, _seed

FIXED = 1_700_000_000.0


@pytest.fixture(autouse=True)
def _clean_atr_cache():
    hub_mod._atr_cache.clear()
    yield
    hub_mod._atr_cache.clear()


def _eval_calls(h, message=None, *, pair="AAA_USDT", time_ms=None, envelope_ms=None):
    calls = []

    def _capture(**kw):
        calls.append(kw)
        return []

    frame = json.loads(_frame(pair, "0.40", "0.40", time_ms=time_ms))
    if envelope_ms is not None:
        frame["time_ms"] = envelope_ms
    with patch.object(hub_mod, "evaluate_would_sells", side_effect=_capture), patch(
        "services.exit_realtime.hub.time.time", return_value=FIXED
    ), _logs() as lines:
        h.handle_ws_message(message or json.dumps(frame))
    return calls, lines


# ----------------------------------------------------------------- stale-price guard
class TestStaleTickGuard:
    def _hub_with_lot(self, **rt):
        _seed("AAA/USDT", amount=20, entry=1.0)
        h = _hub()
        h._raw["exit_realtime"].update(rt)
        h.update_book([_row("AAA/USDT")])
        return h

    def test_fresh_tick_is_evaluated(self):
        h = self._hub_with_lot()
        calls, _ = _eval_calls(h, time_ms=int((FIXED - 1.0) * 1000))
        assert len(calls) == 1
        assert h.stats()["ws_stale_tick_skip"] == 0

    def test_stale_tick_is_not_evaluated_and_logged(self):
        h = self._hub_with_lot()
        _eval_calls(h, time_ms=int((FIXED - 0.5) * 1000))  # baseline lag 0.5 s
        calls, lines = _eval_calls(h, time_ms=int((FIXED - 60.5) * 1000))
        assert calls == []
        assert h.stats()["ws_stale_tick_skip"] == 1
        assert any("ws_stale_tick_skip symbol=AAA/USDT" in ln and "tick_age=60.0s" in ln for ln in lines)

    def test_envelope_time_is_used_when_result_has_none(self):
        h = self._hub_with_lot()
        _eval_calls(h, envelope_ms=int(FIXED * 1000))
        calls, _ = _eval_calls(h, envelope_ms=int((FIXED - 45.0) * 1000))
        assert calls == [] and h.stats()["ws_stale_tick_skip"] == 1

    def test_missing_time_stays_allowed(self):
        h = self._hub_with_lot()
        calls, _ = _eval_calls(h)
        assert len(calls) == 1

    def test_host_clock_ahead_does_not_mute_ticks(self):
        # Host 60 s ahead of Gate: every raw age is ~60 s, the corrected age ~0.
        h = self._hub_with_lot()
        for lag in (60.2, 60.0, 60.4):
            calls, _ = _eval_calls(h, time_ms=int((FIXED - lag) * 1000))
            assert len(calls) == 1
        assert h.stats()["ws_stale_tick_skip"] == 0

    def test_future_tick_is_allowed(self):
        h = self._hub_with_lot()
        calls, _ = _eval_calls(h, time_ms=int((FIXED + 20.0) * 1000))
        assert len(calls) == 1

    def test_guard_can_be_switched_off(self):
        h = self._hub_with_lot(max_tick_age_sec=0)
        _eval_calls(h, time_ms=int(FIXED * 1000))
        calls, _ = _eval_calls(h, time_ms=int((FIXED - 600.0) * 1000))
        assert len(calls) == 1

    def test_row_atr_reaches_the_trail_eval(self):
        _seed("AAA/USDT", amount=20, entry=1.0)
        h = _hub()
        row = _row("AAA/USDT")
        row["atr_pct"] = 6.5
        h.update_book([row])
        calls, _ = _eval_calls(h, time_ms=int((FIXED - 1.0) * 1000))
        assert calls[0]["atr_pct"] == pytest.approx(6.5)


# ----------------------------------------------------------------- backoff
def test_backoff_resets_after_a_stable_connection():
    assert next_backoff(60.0, 720.0, base=3.0, cap=60.0, stable_sec=60.0) == 3.0
    assert next_backoff(3.0, 5.0, base=3.0, cap=60.0, stable_sec=60.0) == pytest.approx(4.5)
    assert next_backoff(50.0, 0.0, base=3.0, cap=60.0, stable_sec=60.0) == 60.0


# ----------------------------------------------------------------- watchdog
class _FakeWS:
    def __init__(self):
        self.sent: list[dict] = []
        self.closed = 0

    def send(self, text):
        self.sent.append(json.loads(text))

    def close(self):
        self.closed += 1


def _connected_hub(now=1000.0):
    h = _hub()
    h.update_book([_row("AAA/USDT"), _row("BBB/USDT")])
    ws = _FakeWS()
    h._ws = ws
    h._app = ws
    h._connected = True
    h._subscribed = {"AAA_USDT", "BBB_USDT"}
    h._connected_since = now
    h._last_any_tick = now
    h._last_app_ping = now
    return h, ws


def test_watchdog_noop_when_disconnected():
    h = _hub()
    assert h.watchdog_check(now=10_000.0) == {"pinged": False, "reconnect": False, "resubscribed": []}


def test_watchdog_sends_app_ping():
    h, ws = _connected_hub()
    out = h.watchdog_check(now=1016.0)
    assert out["pinged"] and ws.sent[-1]["channel"] == "spot.ping"
    assert h.watchdog_check(now=1017.0)["pinged"] is False


def test_watchdog_resubscribes_a_silent_open_lot_pair_once_per_window():
    h, ws = _connected_hub()
    h._pair_last_tick["BBB_USDT"] = 1295.0  # BBB ticks, AAA has been silent since connect
    h._last_any_tick = 1295.0
    with _logs() as lines:
        out = h.watchdog_check(now=1301.0)
    assert out["resubscribed"] == ["AAA_USDT"] and not out["reconnect"]
    subs = [m for m in ws.sent if m.get("event") == "subscribe"]
    assert subs == [{"time": subs[0]["time"], "channel": "spot.tickers", "event": "subscribe", "payload": ["AAA_USDT"]}]
    assert any("resubscribed 1 open-lot pairs" in ln for ln in lines)
    h._last_any_tick = 1302.0
    assert h.watchdog_check(now=1302.0)["resubscribed"] == []
    assert h.stats()["watchdog_resubscribes"] == 1


def test_watchdog_forces_reconnect_on_a_silent_stream():
    h, ws = _connected_hub()
    with _logs() as lines:
        out = h.watchdog_check(now=1091.0)
    assert out["reconnect"] and ws.closed == 1
    assert h.stats()["watchdog_reconnects"] == 1
    assert any("forcing reconnect" in ln for ln in lines)


def test_watchdog_forced_reconnects_are_rate_limited():
    h, ws = _connected_hub()
    assert h.watchdog_check(now=1091.0)["reconnect"]
    h._ws, h._connected, h._connected_since, h._last_any_tick = ws, True, 1100.0, 1100.0
    assert not h.watchdog_check(now=1200.0)["reconnect"]  # 109 s after the last forced one
    assert h.watchdog_check(now=1400.0)["reconnect"]
    assert ws.closed == 2


def test_watchdog_reconnects_when_every_subscribe_failed():
    h, ws = _connected_hub()
    h._subscribed = set()  # all initial subscribes failed, book still has lots
    assert h.watchdog_check(now=1091.0)["reconnect"] and ws.closed == 1


def test_watchdog_quiet_stream_inside_window_is_left_alone():
    h, ws = _connected_hub()
    out = h.watchdog_check(now=1080.0)
    assert not out["reconnect"] and ws.closed == 0 and out["resubscribed"] == []


def test_tick_updates_watchdog_clocks():
    h = _hub()
    with patch("services.exit_realtime.hub.time.monotonic", return_value=5000.0):
        h.on_ticker("ZZZ_USDT", 1.0)
    assert h._pair_last_tick["ZZZ_USDT"] == 5000.0 and h._last_any_tick == 5000.0


def test_stale_ticks_do_not_keep_the_watchdog_quiet():
    """DeepSeek follow-up on #774: late frames must not refresh the silent-stream clock."""
    h = _hub()
    with patch("services.exit_realtime.hub.time.monotonic", return_value=5000.0), patch(
        "services.exit_realtime.hub.time.time", return_value=FIXED
    ):
        h.on_ticker("ZZZ_USDT", 1.0, time_ms=int((FIXED - 0.5) * 1000))  # baseline lag 0.5 s
    assert h._last_any_tick == 5000.0
    with patch("services.exit_realtime.hub.time.monotonic", return_value=5100.0), patch(
        "services.exit_realtime.hub.time.time", return_value=FIXED + 100.0
    ):
        h.on_ticker("ZZZ_USDT", 1.0, time_ms=int((FIXED + 100.0 - 60.5) * 1000))  # 60 s late
    assert h._last_any_tick == 5000.0 and h._pair_last_tick["ZZZ_USDT"] == 5000.0
    with patch("services.exit_realtime.hub.time.monotonic", return_value=5200.0), patch(
        "services.exit_realtime.hub.time.time", return_value=FIXED + 200.0
    ):
        h.on_ticker("ZZZ_USDT", 1.0, time_ms=int((FIXED + 200.0 - 1.0) * 1000))  # fresh again
    assert h._last_any_tick == 5200.0 and h._pair_last_tick["ZZZ_USDT"] == 5200.0


# ----------------------------------------------------------------- real ATR
def test_live_atr_is_fetched_then_cached():
    calls = []

    def fetch(sym, tf, price):
        calls.append((sym, tf))
        return 5.5

    assert resolve_row_atr("AAA/USDT", "1h", price=1.0, reference=3.0, cfg={}, fetch_fn=fetch, now=0.0) == (5.5, "live")
    assert resolve_row_atr("AAA/USDT", "1h", price=1.0, reference=3.0, cfg={}, fetch_fn=fetch, now=100.0) == (5.5, "cached")
    assert calls == [("AAA/USDT", "1h")]
    # expired -> refetch
    resolve_row_atr("AAA/USDT", "1h", price=1.0, reference=3.0, cfg={"atr_ttl_sec": 50}, fetch_fn=fetch, now=200.0)
    assert len(calls) == 2


def test_atr_falls_back_to_reference():
    def boom(*_a):
        raise RuntimeError("no network")

    assert resolve_row_atr("A/USDT", "1h", price=1.0, reference=3.0, cfg={}, fetch_fn=boom) == (3.0, "reference")
    assert resolve_row_atr("B/USDT", "1h", price=1.0, reference=3.0, cfg={}, fetch_fn=lambda *a: None) == (3.0, "reference")
    assert resolve_row_atr("C/USDT", "1h", price=1.0, reference=2.0, cfg={"live_atr": False}, fetch_fn=lambda *a: 9.0) == (2.0, "reference")


def test_atr_fetch_budget_bounds_rest_calls():
    calls = []

    def fetch(sym, tf, price):
        calls.append(sym)
        return 4.0

    budget = [2]
    got = [resolve_row_atr(f"S{i}/USDT", "1h", price=1.0, reference=3.0, cfg={}, budget=budget, fetch_fn=fetch)[1] for i in range(4)]
    assert got == ["live", "live", "reference", "reference"] and len(calls) == 2
    # an expired entry that cannot be refreshed is labelled stale, not fresh
    assert resolve_row_atr(
        "S0/USDT", "1h", price=1.0, reference=3.0, cfg={"atr_ttl_sec": 1}, budget=[0], fetch_fn=fetch, now=1e9
    ) == (4.0, "stale_cached")


def test_book_row_carries_live_atr():
    pos = {"amount": 10.0, "average_entry": 1.0, "side": "long", "strategy_tier": "volatile"}
    cfg = SimpleNamespace(risk_config={"atr_reference_pct": 3.0}, raw={}, strategy_params=lambda s, t: {})
    with patch.object(hub_mod, "_fetch_live_atr_pct", return_value=7.25):
        row = hub_mod._book_row_for_position(key="AAA/USDT_1h", pos=pos, tenant_id="default", cfg=cfg)
    assert row is not None and row["atr_pct"] == pytest.approx(7.25) and row["atr_source"] == "live"
    hub_mod._atr_cache.clear()
    with patch.object(hub_mod, "_fetch_live_atr_pct", side_effect=RuntimeError("down")):
        row = hub_mod._book_row_for_position(key="AAA/USDT_1h", pos=pos, tenant_id="default", cfg=cfg)
    assert row["atr_pct"] == pytest.approx(3.0) and row["atr_source"] == "reference"


def test_empty_candles_are_not_reported_as_live_atr():
    import pandas as pd

    with patch("services.market_service.MarketService._fetch_ohlcv", return_value=pd.DataFrame()):
        assert hub_mod._fetch_live_atr_pct("AAA/USDT", "1h", 1.0) is None
    assert resolve_row_atr(
        "AAA/USDT", "1h", price=1.0, reference=7.0, cfg={},
        fetch_fn=lambda *a: None,
    ) == (7.0, "reference")


def test_live_atr_uses_candle_close_not_lot_price():
    import pandas as pd

    df = pd.DataFrame({"open": [1.0] * 20, "high": [1.1] * 20, "low": [0.9] * 20, "close": [2.0] * 20, "volume": [1.0] * 20})
    seen = []

    def _ind(self, frame, tf, price, symbol=None):
        seen.append(price)
        return {"atr_pct": 4.2}

    with patch("services.market_service.MarketService._fetch_ohlcv", return_value=df), patch(
        "services.market_service.MarketService.indicators_from_df", _ind
    ):
        assert hub_mod._fetch_live_atr_pct("AAA/USDT", "1h", 123.0) == pytest.approx(4.2)
    assert seen == [2.0]


# ----------------------------------------------------------------- _run_loop backoff (integration)
def test_run_loop_resets_backoff_after_a_long_connection():
    import sys
    import types

    clock = {"t": 1000.0}
    lasts = iter([5.0, 5.0, 720.0, 5.0])

    class FakeApp:
        def __init__(self, url, on_open=None, on_message=None, on_error=None, on_close=None):
            self.on_open, self.on_close = on_open, on_close

        def send(self, _text):
            pass

        def close(self):
            pass

        def run_forever(self, **_kw):
            self.on_open(self)
            clock["t"] += next(lasts)
            self.on_close(self)

    fake_ws = types.ModuleType("websocket")
    fake_ws.WebSocketApp = FakeApp
    h = _hub()
    h.update_book([_row("AAA/USDT")])
    waits = []

    class Stop:
        def is_set(self):
            return False

        def wait(self, t):
            waits.append(round(t, 3))
            return len(waits) >= 4

    h._stop = Stop()
    with patch.dict(sys.modules, {"websocket": fake_ws}), patch(
        "services.exit_realtime.hub.time.monotonic", side_effect=lambda: clock["t"]
    ), patch("services.exit_realtime.hub.time.sleep"), patch.object(h, "_broadcast_gui"), _logs():
        h._run_loop()
    assert waits == [4.5, 6.75, 3.0, 4.5]
