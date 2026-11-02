"""#739 review fixes (code review 2026-11-01).

1. bus/eval_queue._trim_queue: bounded Redis round trips per over-cap enqueue, HDEL of trimmed members'
   meta (no stale debounce, no leaking meta hash), rate-limited over-cap warning, backoff while only
   protected heartbeats are left.
2. strategies/decision_engine: the merge hard-stop flag is per evaluation (local, passed through), not
   instance state that one coin's merge can leak into another coin's sell decision.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from cs_helpers import QCFG, TF, FakeRedis, fake_queue

CAP = int(QCFG["architecture"]["eval_queue_max_len"])
QUEUE = "cstest:eval:queue"
META = "cstest:eval:meta"


class CountingRedis(FakeRedis):
    def __init__(self):
        super().__init__()
        self.calls: dict[str, int] = {}

    def __getattribute__(self, name):
        attr = super().__getattribute__(name)
        if name in ("zrevrange", "hget", "hmget", "zrem", "hdel", "zcard"):
            calls = super().__getattribute__("calls")
            calls[name] = calls.get(name, 0) + 1
        return attr


def _enqueue(sym, reason="entry_15m_vol_spike", pri=15):
    from bus.eval_queue import enqueue_eval

    return enqueue_eval(sym, TF, reason=reason, priority=pri, config_raw=QCFG)


@pytest.fixture(autouse=True)
def _reset_trim_state():
    from bus import eval_queue

    if hasattr(eval_queue, "_reset_trim_state_for_tests"):
        eval_queue._reset_trim_state_for_tests()
    yield
    if hasattr(eval_queue, "_reset_trim_state_for_tests"):
        eval_queue._reset_trim_state_for_tests()


# ----------------------------------------------------------------------------- eval queue trim


def test_trimmed_member_meta_is_deleted():
    r = FakeRedis()
    with fake_queue(r):
        assert _enqueue("T739STALE/USDT", reason="stale_watchlist", pri=50)
        for i in range(CAP):
            _enqueue(f"T739E{i:04d}/USDT")
    assert not any("T739STALE" in m for m in r.z.get(QUEUE, {}))
    assert not any("T739STALE" in m for m in r.h.get(META, {})), "trimmed member left stale meta"
    assert set(r.h.get(META, {})) == set(r.z.get(QUEUE, {}))


def test_trimmed_member_can_be_requeued_without_stale_debounce():
    r = FakeRedis()
    with fake_queue(r):
        assert _enqueue("T739RQ/USDT", reason="stale_watchlist", pri=50)
        for i in range(CAP):
            _enqueue(f"T739E{i:04d}/USDT")
        assert not any("T739RQ" in m for m in r.z.get(QUEUE, {}))
        # Same priority, inside the 45 s debounce: the job is no longer queued, so it must be accepted.
        for i in range(5):
            r.zpopmin(QUEUE)  # room under the cap
        assert _enqueue("T739RQ/USDT", reason="stale_watchlist", pri=50) is True
    assert any("T739RQ" in m for m in r.z.get(QUEUE, {}))


def test_trim_round_trips_bounded_with_many_heartbeats():
    """450 heartbeats + entries over the cap: one over-cap enqueue costs a bounded number of calls."""
    r = CountingRedis()
    with fake_queue(r):
        for i in range(450):
            assert _enqueue(f"T739H{i:03d}/USDT", reason="position_heartbeat", pri=30)
        for i in range(CAP - 450):
            _enqueue(f"T739E{i:04d}/USDT")
        assert len(r.z[QUEUE]) == CAP
        r.calls.clear()
        _enqueue("T739NEW/USDT")
    members = r.z[QUEUE]
    assert len(members) == CAP
    assert sum(1 for m in members if "T739H" in m) == 450
    trim_calls = sum(r.calls.get(k, 0) for k in ("zrevrange", "hget", "hmget", "zrem", "hdel"))
    # enqueue itself does one HGET for its own meta; the trim must not do one per queued member
    assert r.calls.get("hget", 0) <= 1, r.calls
    assert trim_calls <= 12, r.calls


def test_overcap_warning_rate_limited_and_scan_backs_off():
    r = CountingRedis()
    with fake_queue(r), patch("bus.eval_queue.log") as log_mock:
        for i in range(CAP + 20):
            _enqueue(f"T739W{i:04d}/USDT", reason="position_heartbeat", pri=30)
    warnings = [c for c in log_mock.call_args_list if "over cap" in str(c.args[0])]
    assert len(r.z[QUEUE]) == CAP + 20  # heartbeats are never trimmed
    assert len(warnings) == 1, len(warnings)
    # after the first blocked trim, later over-cap enqueues skip the scan during the backoff
    assert r.calls.get("zrevrange", 0) <= 5, r.calls


def test_trim_resumes_after_backoff_window():
    from bus import eval_queue

    r = FakeRedis()
    with fake_queue(r):
        for i in range(CAP + 2):
            _enqueue(f"T739B{i:04d}/USDT", reason="position_heartbeat", pri=30)
        _enqueue("T739AFTER/USDT", reason="stale_watchlist", pri=50)
        assert any("T739AFTER" in m for m in r.z[QUEUE])  # inside the backoff: not trimmed yet
        eval_queue._trim_blocked_at.clear()  # backoff window over
        _enqueue("T739LAST/USDT", reason="stale_watchlist", pri=50)
    # backoff over: the next trim removes every non-heartbeat member above the cap
    assert not any("T739AFTER" in m or "T739LAST" in m for m in r.z[QUEUE])
    assert len(r.z[QUEUE]) == CAP + 2


# ----------------------------------------------------------------------------- merge hard-stop flag


def _engine_and_market(symbol):
    from core.models import MarketContext
    from strategies.decision_engine import DecisionEngine

    market = MarketContext(
        symbol=symbol,
        timeframe="4h",
        current_price=0.99,
        rsi=50,
        lower_bb=0.9,
        atr_pct=3.0,
        has_position=True,
        average_entry=1.0,
        open_positions=1,
        strategy_params={"strategy_profile": "volatile_altcoin"},
    )
    engine = DecisionEngine()
    engine.config.raw.setdefault("regime_detector", {})["enabled"] = False
    engine.config.raw.setdefault("strategy_allocator", {})["enabled"] = False
    return engine, market


def _evaluate(engine, market, merge_sell):
    from strategies.positions import positions, update_position

    backup = {k: dict(v) for k, v in positions.items()}
    positions.clear()
    try:
        update_position(market.symbol, market.timeframe, "BUY", 1.0, 500)
        with patch.object(engine, "_merge_sell", side_effect=merge_sell), patch(
            "strategies.decision_engine.evaluate_dca_addon", return_value=None
        ):
            return engine.evaluate_with_market({"symbol": market.symbol, "timeframe": market.timeframe}, market)
    finally:
        positions.clear()
        positions.update(backup)


def _stop_without_flag(*_a, **_k):
    return ("SELL_STOP_PARTIAL", ["stop_loss"], 80.0, [], "partial_stop", {})


def _stop_with_flag(*_a, **k):
    k["merge_flags"]["long_hard_stop"] = True
    return ("SELL_STOP_PARTIAL", ["stop_loss"], 80.0, [], "partial_stop", {})


def test_stale_instance_flag_does_not_leak_into_another_coin():
    engine, market = _engine_and_market("T739LEAK/USDT")
    clean = _evaluate(engine, market, _stop_without_flag)
    assert clean.long_hard_stop is False
    # Simulate the old leak: another coin's merge left the flag set on the shared engine.
    engine._merge_long_hard_stop = True
    leaked = _evaluate(engine, market, _stop_without_flag)
    assert leaked.long_hard_stop is False


def test_merge_flag_is_passed_through_per_evaluation():
    engine, market = _engine_and_market("T739PASS/USDT")
    a = _evaluate(engine, market, _stop_with_flag)
    assert a.long_hard_stop is True
    assert not hasattr(engine, "_merge_long_hard_stop")
    b = _evaluate(engine, market, _stop_without_flag)
    assert b.long_hard_stop is False


def test_merge_sell_sets_flag_in_caller_dict_not_on_engine():
    import inspect

    from strategies.decision_engine import DecisionEngine

    assert "merge_flags" in inspect.signature(DecisionEngine._merge_sell).parameters
    src = inspect.getsource(DecisionEngine)
    assert "self._merge_long_hard_stop" not in src
