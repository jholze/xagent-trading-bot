"""Observe-only shadow rules g/no-chase, h_oracle early exit, e age limit.

The rules only write rows for ``obs_shadow_rules``. These tests pin each
rule's trigger, the backtest/live parity of the 48 h pump definition, and
that trading results are identical with shadow off, on, and failing.
"""

from __future__ import annotations

import contextlib
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from services import shadow_rules as sr

H = 3600
NOW = 1_793_000_000  # fixed unix second, hour-aligned below
NOW = NOW // H * H


def _bars(lows, *, end=NOW, ms=False):
    """1h bars that opened at end-len(lows)h .. end-1h (all closed by ``end``)."""
    n = len(lows)
    out = []
    for i, low in enumerate(lows):
        t = end - (n - i) * H
        out.append([t * 1000 if ms else t, low * 1.01, low * 1.02, low, low * 1.01, 10.0])
    return out


@pytest.fixture(autouse=True)
def _shadow_isolated(monkeypatch):
    monkeypatch.delenv("SHADOW_RULES", raising=False)
    monkeypatch.delenv("SHADOW_RULES_UNDER_TEST", raising=False)
    sr.reset_for_tests()
    yield
    sr.reset_for_tests()


def _shadow_on(monkeypatch, *, oracle="NEUTRAL", bars=None, sink=None, fetcher=None):
    rows: list[dict] = []
    monkeypatch.setenv("SHADOW_RULES_UNDER_TEST", "1")
    sr.set_hooks_for_tests(
        sink=sink or rows.append,
        candle_fetcher=fetcher or (lambda _s, _n: list(bars or [])),
        oracle_reader=(oracle if callable(oracle) else (lambda: oracle)),
        inline=True,
    )
    return rows


# ---------------------------------------------------------------------------
# Constants are the fixed values from the brief.


def test_thresholds_are_fixed_code_constants():
    assert sr.NO_CHASE_LIMIT_RISK_OFF_PCT == 10.0
    assert sr.NO_CHASE_LIMIT_NEUTRAL_PCT == 15.0
    assert sr.NO_CHASE_LIMIT_RISK_ON_PCT == 20.0
    assert sr.NO_CHASE_LOOKBACK_HOURS == 48
    assert sr.EARLY_EXIT_LOSS_PCT == 10.0
    assert sr.EARLY_EXIT_WINDOW_HOURS == 72.0
    assert sr.AGE_LIMIT_DAYS == 14.0
    assert sr.AGE_LIMIT_LOSS_PCT == 15.0
    assert sr.RISK_OFF_STATES == frozenset({"RISK_OFF", "CRASH"})
    assert sr.COLLECTION == "obs_shadow_rules"


# ---------------------------------------------------------------------------
# g / no-chase


@pytest.mark.parametrize(
    "state,limit",
    [("RISK_OFF", 10.0), ("CRASH", 10.0), ("crash", 10.0), ("RISK_ON", 20.0),
     ("NEUTRAL", 15.0), (None, 15.0), ("", 15.0), ("UNKNOWN", 15.0)],
)
def test_no_chase_limit_by_oracle_state(state, limit):
    assert sr.no_chase_limit_pct(state) == limit


def test_pump_uses_min_closed_low_of_last_48h():
    lows = [100.0] * 48
    lows[10] = 80.0  # inside the window
    bars = _bars(lows)
    # a bar older than 48 h with an even lower low is ignored
    bars.insert(0, [NOW - 49 * H, 50, 50, 50, 50, 1])
    # the forming bar (opened at NOW) is not closed and is ignored
    bars.append([NOW, 40, 40, 40, 40, 1])
    assert sr.pump_pct_48h(bars, 100.0, NOW) == pytest.approx(25.0)


def test_pump_accepts_millisecond_timestamps():
    assert sr.pump_pct_48h(_bars([100.0] * 48, ms=True), 110.0, NOW) == pytest.approx(10.0)


def test_pump_needs_36_closed_bars():
    assert sr.pump_pct_48h(_bars([100.0] * 35), 200.0, NOW) is None
    assert sr.pump_pct_48h(_bars([100.0] * 36), 200.0, NOW) == pytest.approx(100.0)


@pytest.mark.parametrize(
    "price,state,action",
    [
        (115.0, "NEUTRAL", sr.ACTION_ALLOW_ENTRY),   # exactly 15 % is not > 15 %
        (115.01, "NEUTRAL", sr.ACTION_SKIP_ENTRY),
        (115.01, None, sr.ACTION_SKIP_ENTRY),
        (109.99, "RISK_OFF", sr.ACTION_ALLOW_ENTRY),
        (110.01, "RISK_OFF", sr.ACTION_SKIP_ENTRY),
        (110.01, "CRASH", sr.ACTION_SKIP_ENTRY),
        (119.0, "RISK_ON", sr.ACTION_ALLOW_ENTRY),
        (120.01, "RISK_ON", sr.ACTION_SKIP_ENTRY),
    ],
)
def test_no_chase_trigger(price, state, action):
    pump = sr.pump_pct_48h(_bars([100.0] * 48), price, NOW)
    assert sr.evaluate_no_chase(pump, state)[0] == action


def test_no_chase_without_history_is_no_data():
    assert sr.evaluate_no_chase(None, "RISK_OFF") == (sr.ACTION_NO_DATA, 10.0)


def test_entry_job_row_fields(monkeypatch):
    rows = _shadow_on(monkeypatch, oracle="RISK_OFF", bars=_bars([100.0] * 48, end=int(time.time()) // H * H))
    doc = sr.process_entry_job(
        {"kind": "entry", "tenant_id": "henry", "symbol": "PUMP/USDT", "timeframe": "4h",
         "price": 112.0, "signal_unix": int(time.time()) // H * H, "source": "auto",
         "idempotency_key": "k1"}
    )
    assert rows == [doc]
    assert doc["rule"] == sr.RULE_NO_CHASE
    assert doc["action"] == sr.ACTION_SKIP_ENTRY
    assert doc["tenant_id"] == "henry"
    assert doc["symbol"] == "PUMP/USDT"
    assert doc["oracle_state"] == "RISK_OFF"
    assert doc["price"] == 112.0
    assert doc["pump_48h_pct"] == pytest.approx(12.0)
    assert doc["limit_pct"] == 10.0
    assert doc["rule_params"]["limit_risk_off_pct"] == 10.0
    assert doc["observe_only"] is True
    assert isinstance(doc["timestamp"], datetime)
    assert "lot_id" in doc and "pnl_pct" in doc


# ---------------------------------------------------------------------------
# h_oracle early exit


@pytest.mark.parametrize(
    "pnl,age,state,fires",
    [
        (-10.0, 72.0, "RISK_OFF", True),
        (-25.0, 1.0, "CRASH", True),
        (-9.99, 10.0, "RISK_OFF", False),
        (-10.0, 72.01, "RISK_OFF", False),
        (-15.0, 10.0, "NEUTRAL", False),
        (-15.0, 10.0, "RISK_ON", False),
        (-15.0, 10.0, None, False),
        (-15.0, -1.0, "RISK_OFF", False),
        (None, 10.0, "RISK_OFF", False),
    ],
)
def test_early_exit_trigger(pnl, age, state, fires):
    assert sr.early_exit_triggered(pnl, age, state) is fires


# ---------------------------------------------------------------------------
# e age limit


@pytest.mark.parametrize(
    "pnl,age_days,fires",
    [
        (-15.0, 14.01, True),
        (-40.0, 30.0, True),
        (-15.0, 14.0, False),    # not older than 14 days
        (-14.99, 20.0, False),
        (5.0, 20.0, False),
    ],
)
def test_age_limit_trigger(pnl, age_days, fires):
    assert sr.age_limit_triggered(pnl, age_days * 24.0) is fires


def _lot_job(*, avg=1.0, price=0.8, age_h=24.0 * 20, tenant="henry", sym="LOSE/USDT"):
    now = datetime.now(timezone.utc)
    entry = (now - timedelta(hours=age_h)).isoformat()
    return {"kind": "lot", "tenant_id": tenant, "symbol": sym, "timeframe": "4h",
            "lot_id": sr.lot_id(sym, "4h", entry), "price": price, "average_entry": avg,
            "amount": 10.0, "entry_at": entry, "observed_at": now.isoformat()}


def test_lot_job_age_limit_row_and_dedupe(monkeypatch):
    rows = _shadow_on(monkeypatch, oracle="RISK_ON")
    job = _lot_job(price=0.8, age_h=24 * 20)
    out = sr.process_lot_job(job)
    assert [d["rule"] for d in out] == [sr.RULE_AGE_LIMIT]
    doc = out[0]
    assert doc["action"] == sr.ACTION_CLOSE_AGED
    assert doc["pnl_pct"] == pytest.approx(-20.0)
    assert doc["lot_id"] == job["lot_id"]
    assert doc["tenant_id"] == "henry"
    assert doc["oracle_state"] == "RISK_ON"
    assert doc["rule_params"] == {"min_age_days": 14.0, "loss_pct": 15.0}
    # same lot next cycle: no second row
    assert sr.process_lot_job(job) == []
    assert len(rows) == 1


def test_lot_job_early_exit_needs_risk_off(monkeypatch):
    state = {"v": "NEUTRAL"}
    rows = _shadow_on(monkeypatch, oracle=lambda: state["v"])
    job = _lot_job(price=0.88, age_h=30)
    assert sr.process_lot_job(job) == []
    state["v"] = "CRASH"
    out = sr.process_lot_job(job)
    assert [(d["rule"], d["action"]) for d in out] == [(sr.RULE_ORACLE_EARLY_EXIT, sr.ACTION_FULL_EXIT)]
    assert out[0]["oracle_state"] == "CRASH"
    assert out[0]["lot_age_hours"] == pytest.approx(30.0, abs=0.01)
    assert len(rows) == 1


def test_lot_job_healthy_lot_does_not_read_oracle(monkeypatch):
    calls = []
    _shadow_on(monkeypatch, oracle=lambda: calls.append(1) or "RISK_OFF")
    assert sr.process_lot_job(_lot_job(price=1.05, age_h=10)) == []
    assert calls == []


# ---------------------------------------------------------------------------
# Backtest harness and live shadow share the 48 h pump definition.


def test_harness_no_chase_matches_live_rule():
    from backtest.harness import scenarios as sc
    from backtest.harness.regime import Series

    sig = NOW
    lows = [100.0] * 48
    bars = [tuple(b) for b in _bars(lows, end=sig)]
    ctx = SimpleNamespace(engine=SimpleNamespace(candles_1h=lambda _s: bars))
    for label in ("RISK_OFF", "CRASH", "NEUTRAL", "RISK_ON", "OFF", "ON", None):
        ser = Series("t", {} if label is None else {sig // H * H - H: label})
        flt = sc._nochase_regime(ctx, ser, set())
        for price in (109.0, 110.5, 114.0, 116.0, 119.0, 121.0):
            ep = SimpleNamespace(symbol="X/USDT", key="k",
                                 entry=SimpleNamespace(price=price, ts=datetime.fromtimestamp(sig, tz=timezone.utc)))
            harness_keeps = flt(ep)
            live_action, _ = sr.evaluate_no_chase(sr.pump_pct_48h(bars, price, sig), label)
            assert harness_keeps == (live_action == sr.ACTION_ALLOW_ENTRY), (label, price)


# ---------------------------------------------------------------------------
# Hooks: gating, entry detection, failure isolation.


def test_hooks_are_off_under_pytest_without_flag():
    rows: list = []
    sr.set_hooks_for_tests(sink=rows.append, inline=True)
    order = SimpleNamespace(type="BUY", symbol="OFF/USDT", price=1.0, idempotency_key="x")
    sr.observe_entry_candidate(order)
    sr.observe_lot("OFF/USDT", "4h", {"amount": 1, "average_entry": 1, "entry_at": "2026-01-01T00:00:00"}, 0.5)
    assert rows == []
    assert sr.drain_for_tests() == []


def test_kill_switch_env(monkeypatch):
    monkeypatch.setenv("SHADOW_RULES_UNDER_TEST", "1")
    monkeypatch.setenv("SHADOW_RULES", "0")
    assert sr.enabled() is False
    monkeypatch.setenv("SHADOW_RULES", "1")
    assert sr.enabled() is True


def test_entry_hook_only_for_new_long_lots(monkeypatch):
    rows = _shadow_on(monkeypatch, bars=_bars([100.0] * 48, end=int(time.time()) // H * H))
    buy = SimpleNamespace(type="BUY", symbol="NEW/USDT", price=130.0, idempotency_key="k-new")
    with patch("strategies.positions.find_open_position_for_symbol", return_value=None):
        sr.observe_entry_candidate(buy, timeframe="4h", source="auto")
        sr.observe_entry_candidate(buy, timeframe="4h", source="auto")  # same idem: once
        sr.observe_entry_candidate(SimpleNamespace(type="SELL", symbol="NEW/USDT", price=1.0))
    with patch("strategies.positions.find_open_position_for_symbol", return_value=("4h", {"amount": 1})):
        sr.observe_entry_candidate(SimpleNamespace(type="BUY", symbol="ADD/USDT", price=1.0, idempotency_key="k-add"))
    assert [(r["symbol"], r["action"]) for r in rows] == [("NEW/USDT", sr.ACTION_SKIP_ENTRY)]
    assert rows[0]["order_source"] == "auto"


def test_hooks_never_raise(monkeypatch):
    _shadow_on(monkeypatch)
    with patch("strategies.positions.find_open_position_for_symbol", side_effect=RuntimeError("boom")):
        sr.observe_entry_candidate(SimpleNamespace(type="BUY", symbol="E/USDT", price=1.0))
        sr.observe_symbol_lot("E/USDT", 1.0, "4h")
    sr.observe_lot("E/USDT", "4h", object(), 1.0)
    sr.observe_lot("E/USDT", "4h", {"amount": "x"}, 1.0)


def test_inline_errors_are_swallowed(monkeypatch):
    def bad_sink(_doc):
        raise RuntimeError("mongo down")

    _shadow_on(monkeypatch, oracle="RISK_OFF", sink=bad_sink,
               fetcher=lambda *_a: (_ for _ in ()).throw(RuntimeError("ccxt down")))
    with patch("strategies.positions.find_open_position_for_symbol", return_value=None):
        sr.observe_entry_candidate(SimpleNamespace(type="BUY", symbol="E/USDT", price=1.0, idempotency_key="z"))
    sr.observe_lot("E/USDT", "4h", {"amount": 1, "average_entry": 1.0,
                                     "entry_at": (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()}, 0.5)


def test_worker_thread_does_not_block_hook_and_survives_errors(monkeypatch):
    """Async path: a slow or failing sink never stalls the trading thread."""
    monkeypatch.setenv("SHADOW_RULES_UNDER_TEST", "1")
    gate = threading.Event()
    seen: list[str] = []

    def sink(doc):
        if doc["symbol"] == "FAIL/USDT":
            raise RuntimeError("insert failed")
        gate.wait(timeout=2)
        seen.append(doc["symbol"])

    sr.set_hooks_for_tests(sink=sink, oracle_reader=lambda: "RISK_OFF", inline=False)
    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    lot = {"amount": 1, "average_entry": 1.0, "entry_at": old}
    started = time.perf_counter()
    sr.observe_lot("FAIL/USDT", "4h", lot, 0.5)
    sr.observe_lot("SLOW/USDT", "4h", lot, 0.5)
    sr.observe_lot("NEXT/USDT", "4h", lot, 0.5)
    assert time.perf_counter() - started < 0.25
    gate.set()
    deadline = time.time() + 3
    while time.time() < deadline and len(seen) < 2:
        time.sleep(0.01)
    assert seen == ["SLOW/USDT", "NEXT/USDT"]


# ---------------------------------------------------------------------------
# Same orders and decisions with shadow off / on / failing.


def _reset_books():
    from data_manager import save_trade_history
    from services.order_service import OrderService
    from strategies.positions import clear_positions_memory

    clear_positions_memory()
    OrderService()._save({"orders": []})
    save_trade_history({"virtual_balance": 5000.0, "realized_pnl": 0.0, "open_positions": 0, "trades": []})


_RESULT_KEYS = ("executed", "order_type", "symbol", "amount", "price", "usdt_amount", "pnl",
                "message", "fee", "filled_qty", "code", "pending", "needs_reconcile")


def _trade_tape(symbol: str) -> dict:
    from core.models import TradeOrder
    from services.order_service import OrderService
    from services.trading_service import TradingService
    from strategies.positions import get_position

    _reset_books()
    svc = TradingService()
    results = []
    with patch("price_fetcher.get_ticker_price", return_value=2.0), patch(
        "services.market_policy_fusion.get_global_market_bias", return_value={}
    ), patch("bus.locks.ledger_lock", lambda *_a, **_k: contextlib.nullcontext()), patch(
        "services.trading_engine_runtime.ledger_lock", lambda *_a, **_k: contextlib.nullcontext()
    ):
        for order in (
            TradeOrder(type="BUY", symbol=symbol, price=2.0, qty=0, usdt_amount=200, signal="BUY"),
            TradeOrder(type="BUY", symbol=symbol, price=1.8, qty=0, usdt_amount=200, signal="BUY"),
            TradeOrder(type="SELL", symbol=symbol, price=1.5, qty=50, signal="SELL"),
        ):
            r = svc.execute_order(order, "4h", source="manual")
            results.append({k: getattr(r, k, None) for k in _RESULT_KEYS})
    pos = get_position(symbol, "4h")
    orders = [
        {
            "side": o.get("side"), "status": o.get("status"), "source": o.get("source"),
            "signal": o.get("signal"), "request": o.get("request"),
            "approved": (o.get("risk") or {}).get("approved"), "code": (o.get("risk") or {}).get("code"),
            "approved_usdt": (o.get("risk") or {}).get("approved_usdt"),
            "exec": {k: (o.get("execution") or {}).get(k) for k in ("price", "amount", "usdt")},
            "ctx_oracle_state": o.get("ctx_oracle_state"),
        }
        for o in OrderService()._load().get("orders") or []
    ]
    return {
        "results": results,
        "orders": orders,
        "position": {k: str(pos.get(k)) for k in ("amount", "average_entry", "realized_pnl", "dca_rounds", "sold_percent")},
    }


def test_trading_is_identical_with_shadow_off_on_and_failing(monkeypatch, zero_cost_model):
    sym = "SHDW/USDT"
    monkeypatch.setenv("SHADOW_RULES", "0")
    off = _trade_tape(sym)
    assert off["results"][0]["executed"] is True  # the tape really trades

    monkeypatch.setenv("SHADOW_RULES", "1")
    pump = _bars([1.0] * 48, end=int(time.time()) // H * H)  # +100 % pump -> would_skip_entry
    rows = _shadow_on(monkeypatch, oracle="RISK_OFF", bars=pump)
    on = _trade_tape(sym)
    assert on == off
    assert [(r["rule"], r["action"]) for r in rows] == [(sr.RULE_NO_CHASE, sr.ACTION_SKIP_ENTRY)]

    def boom(*_a, **_k):
        raise RuntimeError("shadow exploded")

    sr.reset_for_tests()
    _shadow_on(monkeypatch, oracle=boom, sink=boom, fetcher=boom)
    assert _trade_tape(sym) == off

    sr.reset_for_tests()
    with patch("services.shadow_rules.observe_entry_candidate", side_effect=RuntimeError("hook exploded")):
        assert _trade_tape(sym) == off


def test_process_coin_is_identical_with_shadow_off_and_on(monkeypatch, zero_cost_model):
    from services.signal_orchestrator import SignalOrchestrator
    from strategies.positions import get_position, set_position_field, update_position

    sym, tf = "AGED/USDT", "4h"
    entry = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    indicators = {"rsi": 50.0, "lower_bb": 0.5, "vol_multiplier": 1.0}

    def run():
        _reset_books()
        update_position(sym, tf, "BUY", 1.0, 100.0)
        set_position_field(sym, tf, "entry_at", entry)
        set_position_field(sym, tf, "first_buy_at", entry)
        orch = SignalOrchestrator()
        with patch.object(orch.market, "fetch_indicators", return_value=indicators), patch.object(
            orch.decision_engine.market, "fetch_ohlcv_and_indicators", return_value=(None, indicators)
        ), patch.object(orch.decision_engine.market, "fetch_indicators", return_value=indicators), patch(
            "price_fetcher.get_ticker_price", return_value=0.8
        ), patch("services.market_policy_fusion.get_global_market_bias", return_value={}):
            out = orch.process_coin({"symbol": sym, "timeframe": tf, "name": "Aged"}, 0.8)
        pos = get_position(sym, tf)
        keep = {k: out.get(k) for k in ("action", "normalized_action", "executed", "symbol")}
        return keep, {k: str(pos.get(k)) for k in ("amount", "average_entry", "sold_percent")}

    monkeypatch.setenv("SHADOW_RULES", "0")
    off = run()
    monkeypatch.setenv("SHADOW_RULES", "1")
    rows = _shadow_on(monkeypatch, oracle="RISK_OFF")
    on = run()
    assert on == off
    assert [(r["rule"], r["symbol"], r["action"]) for r in rows] == [(sr.RULE_AGE_LIMIT, sym, sr.ACTION_CLOSE_AGED)]
    assert rows[0]["pnl_pct"] == pytest.approx(-20.0)

    def boom(*_a, **_k):
        raise RuntimeError("shadow exploded")

    sr.reset_for_tests()
    _shadow_on(monkeypatch, oracle=boom, sink=boom)
    assert run() == off
    with patch("services.shadow_rules.observe_symbol_lot", side_effect=RuntimeError("hook exploded")):
        assert run() == off


# ---------------------------------------------------------------------------
# Oracle state on every order document.


def test_order_without_decision_state_gets_oracle_state_at_record(monkeypatch):
    from core.models import TradeOrder, trade_ctx_fields

    monkeypatch.setattr("strategies.ctx_axes.read_oracle_state", lambda: "RISK_OFF")
    dca = TradeOrder("BUY", "DCA/USDT", 1.0, 0, usdt_amount=100, source="dca")
    assert trade_ctx_fields(dca)["ctx_oracle_state"] == "RISK_OFF"
    assert dca.ctx_oracle_state is None  # the order object is not changed
    assert trade_ctx_fields(None)["ctx_oracle_state"] == "RISK_OFF"
    tagged = TradeOrder("BUY", "DEC/USDT", 1.0, 0, usdt_amount=100, ctx_oracle_state="RISK_ON")
    assert trade_ctx_fields(tagged)["ctx_oracle_state"] == "RISK_ON"


def test_oracle_state_read_failure_keeps_none(monkeypatch):
    from core.models import TradeOrder, trade_ctx_fields

    def boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr("strategies.ctx_axes.read_oracle_state", boom)
    assert trade_ctx_fields(TradeOrder("SELL", "X/USDT", 1.0, 1.0))["ctx_oracle_state"] is None


def test_order_document_carries_oracle_state_from_snapshot():
    from core.models import RiskDecision, TradeOrder
    from services.market_oracle.store import store_snapshot
    from services.order_service import OrderService

    store_snapshot({"state": "CRASH", "source": "market_oracle"})
    svc = OrderService()
    order = TradeOrder("SELL", "STOP/USDT", 1.0, 5.0, source="exit_ws", signal="SELL_STOP_FULL")
    rec = svc.create_from_request(order, telegram_token="ora-1")
    assert rec["ctx_oracle_state"] == "CRASH"
    rej = svc.record_rejected(
        TradeOrder("BUY", "STOP/USDT", 1.0, 0, usdt_amount=50, source="dca"),
        RiskDecision(approved=False, message="no", code="x"),
    )
    assert rej["ctx_oracle_state"] == "CRASH"


def test_default_sink_mirrors_to_obs_shadow_rules(monkeypatch):
    """Without a test sink the row goes through observe_streams (PR #743)."""
    from services import observe_streams

    calls: list = []
    monkeypatch.setenv("SHADOW_RULES_UNDER_TEST", "1")
    monkeypatch.setattr(observe_streams, "mirror_record", lambda c, r: calls.append((c, r)))
    sr.set_hooks_for_tests(oracle_reader=lambda: "RISK_OFF", inline=True)
    out = sr.process_lot_job(_lot_job(price=0.85, age_h=20, tenant="anne"))
    assert [c for c, _ in calls] == ["obs_shadow_rules"]
    doc = observe_streams.build_observe_document(calls[0][1])
    assert doc["tenant_id"] == "anne"
    assert doc["timestamp"] == out[0]["timestamp"]
