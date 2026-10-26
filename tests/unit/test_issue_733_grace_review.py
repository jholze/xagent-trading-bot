"""Review follow-up on #733 (Viktor A1–A7, Lena F1).

Thresholds, floors, and the grace window come from the loaded config.
The symbol is synthetic. No production config is changed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from bus.eval_queue import PRIORITY_ENTRY_15M, EvalJob
from core.config import get_bot_config
from core.models import MarketContext
from core.tenant_context import DEFAULT_TENANT
from services.eval_queue_runtime import process_eval_job
from strategies import dca as dca_mod
from strategies.dca import (
    _DEFAULT_GRACE_HOURS,
    dca_stop_in_grace,
    evaluate_long_hard_stop,
    full_stop_loss_pct,
)
from strategies.decision_engine import DecisionEngine
from strategies.technical_rsi_bb import TechnicalRSIStrategy

_SYMBOL = "GAP/USDT"
_TF = "4h"
_ENTRY = 1.0


def _raw() -> dict:
    return get_bot_config().raw


def _base_stop() -> float:
    return float(_raw()["stop_loss_pct"])


def _partial_of(stop: float) -> float:
    return stop * float(_raw()["risk"]["partial_stop_ratio"])


def _widen() -> float:
    return float(_raw()["volatile_altcoin"]["dca"]["stop_loss_widen_pct_per_round"])


def _grace() -> float:
    return float(_raw()["volatile_altcoin"]["dca"]["grace_hours_after_dca"])


def _price(entry: float, loss_pct: float) -> float:
    return entry * (1.0 - loss_pct / 100.0)


def _params(*, stop: float, grace=None, interval=None, widen: float = 0.0) -> dict:
    block: dict = {"enabled": False}
    if grace is not None:
        block["grace_hours_after_dca"] = grace
    if interval is not None:
        block["interval_hours"] = interval
    if widen:
        block["stop_loss_widen_pct_per_round"] = widen
    return {
        "stop_loss_pct": stop,
        "partial_stop_pct": _partial_of(stop),
        "dca": block,
    }


def _cycle(price: float, entry: float, position: dict, params: dict) -> str:
    market = MarketContext(
        symbol=_SYMBOL,
        timeframe=_TF,
        current_price=price,
        rsi=45.0,
        lower_bb=price * 0.5,
        vol_multiplier=1.0,
        has_position=True,
        open_positions=1,
        average_entry=entry,
        sim_state=dict(position),
        strategy_params=params,
    )
    return TechnicalRSIStrategy().analyze(
        {"symbol": _SYMBOL, "timeframe": _TF}, market
    ).action


def _ws(price: float, entry: float, position: dict, params: dict) -> str:
    from services.exit_realtime.shadow_eval import evaluate_would_sells

    events = evaluate_would_sells(
        symbol=_SYMBOL,
        timeframe=_TF,
        price=price,
        position={"average_entry": entry, "recent_high": entry, **position},
        strategy_params=params,
        sources=frozenset({"stop_loss"}),
        bid=price,
        base_stop_loss_pct=float(params["stop_loss_pct"]),
    )
    errors = [ev.get("error") for ev in events if ev.get("error")]
    assert not errors, errors
    for ev in events:
        if ev.get("source") == "stop_loss" and ev.get("action"):
            return ev["action"]
    return "HOLD"


def _act(path: str, price: float, entry: float, position: dict, params: dict) -> str:
    if path == "cycle":
        return _cycle(price, entry, position, params)
    if path == "ws":
        return _ws(price, entry, position, params)
    hit = evaluate_long_hard_stop(
        price=price,
        entry=entry,
        position={"symbol": _SYMBOL, "average_entry": entry, **position},
        strategy_params=params,
        base_stop_loss_pct=float(params["stop_loss_pct"]),
    )
    return hit[0] if hit else "HOLD"


_BAD_ROUNDS = [
    pytest.param("1.0", id="str_one_decimal"),
    pytest.param("abc", id="str_abc"),
    pytest.param(["1"], id="list"),
]


@pytest.mark.parametrize("path", ["cycle", "ws"])
@pytest.mark.parametrize("field", ["dca_rounds", "dca_recovery_rounds"])
@pytest.mark.parametrize("bad", _BAD_ROUNDS)
def test_ac_a1_malformed_rounds_still_fire_full_stop(path, field, bad):
    """A1: a non-numeric round field uses the un-widened base and still fires.

    The loss sits above the base stop and below one round of widening, so a
    swallowed exception or a widened stop would not be SELL_STOP_FULL.
    """
    stop = _base_stop()
    widen = _widen()
    assert widen > 0
    loss = stop + widen / 2.0
    assert stop < loss < stop + widen
    params = _params(stop=stop, grace=_grace(), widen=widen)
    position = {field: bad, "average_entry": _ENTRY}
    with patch("strategies.dca.log") as warned:
        action = _act(path, _price(_ENTRY, loss), _ENTRY, position, params)
    assert action == "SELL_STOP_FULL"
    messages = [str(call.args[0]) for call in warned.call_args_list if call.args]
    assert messages
    assert any(
        f"field={field}" in message
        and repr(bad) in message
        and f"symbol={_SYMBOL}" in message
        and f"tenant={DEFAULT_TENANT}" in message
        for message in messages
    )
    assert any(call.args[1] == "WARNING" for call in warned.call_args_list if len(call.args) > 1)
    assert full_stop_loss_pct(
        {"symbol": _SYMBOL, **position}, params, stop
    ) == pytest.approx(stop)


@pytest.mark.parametrize("path", ["cycle", "ws", "direct"])
def test_ac_a2_future_timestamp_is_outside_grace(path):
    """A2 / m4i: a future DCA timestamp is outside grace, so the partial fires."""
    stop = _base_stop()
    partial = _partial_of(stop)
    window = _grace()
    loss = (stop + partial) / 2.0
    assert partial < loss < stop
    future = (datetime.now() + timedelta(hours=window)).isoformat()
    position = {"dca_rounds": 1, "last_dca_at": future, "average_entry": _ENTRY}
    params = _params(stop=stop, grace=window)
    assert dca_stop_in_grace(position, params) is False
    assert _act(path, _price(_ENTRY, loss), _ENTRY, position, params) == "SELL_STOP_PARTIAL"


class _FixedNow(datetime):
    """datetime.now() returns one instant so an age can equal the window."""

    fixed = datetime(2026, 6, 1, 12, 0, 0)

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.fixed
        return cls.fixed.replace(tzinfo=tz)


def test_ac_a3_age_equal_to_window_is_outside_grace(monkeypatch):
    """A3 / m5: age == window is outside grace. ``<=`` would keep the partial paused."""
    window = _grace()
    monkeypatch.setattr(dca_mod, "datetime", _FixedNow)
    stamp = (_FixedNow.fixed - timedelta(hours=window)).isoformat()
    position = {"dca_rounds": 1, "last_dca_at": stamp}
    params = _params(stop=_base_stop(), grace=window)
    assert dca_mod._hours_since(stamp) == pytest.approx(window)
    assert dca_stop_in_grace(position, params) is False
    stop = _base_stop()
    partial = _partial_of(stop)
    loss = (stop + partial) / 2.0
    assert _act("direct", _price(_ENTRY, loss), _ENTRY, position, params) == "SELL_STOP_PARTIAL"


def test_ac_a5_hub_to_risk_recovery_deep_loss_fires_full():
    """A5: a recovery fill at a deep loss still produces an approved full stop.

    Hub evaluates the lot, then RiskManager sees SELL_STOP_FULL and approves it.
    """
    from risk.risk_manager import RiskManager
    from strategies.positions import reset_all_position_stores_for_tests
    from tests.unit.test_ws_long_hard_stop_657 import (
        _clear_guards,
        _fire,
        _hub,
        _orders,
        _paper,
        _row,
        _seed,
    )

    stop = _base_stop()
    widen = _widen()
    full = stop + widen
    loss = full + (100.0 - full) / 2.0
    assert loss > full
    price = _price(_ENTRY, loss)
    amount = float(_raw()["risk"]["min_sell_notional_usdt"]) / price * 2.0
    recent = datetime.now().isoformat()
    params = _params(stop=stop, grace=_grace(), widen=widen)
    reset_all_position_stores_for_tests()
    _clear_guards()
    try:
        _seed(
            _SYMBOL,
            "1h",
            amount=amount,
            entry=_ENTRY,
            dca_rounds=0,
            dca_recovery_rounds=1,
            last_dca_recovery_at=recent,
        )
        hub = _hub()
        hub.update_book(
            [
                _row(
                    _SYMBOL,
                    "1h",
                    position={
                        "amount": amount,
                        "average_entry": _ENTRY,
                        "dca_rounds": 0,
                        "dca_recovery_rounds": 1,
                        "last_dca_recovery_at": recent,
                    },
                    params=params,
                    entry=_ENTRY,
                )
            ]
        )
        seen: list[tuple] = []
        real = RiskManager.evaluate

        def _spy(self, order, timeframe="4h", source="auto", **kwargs):
            decision = real(
                self,
                order,
                timeframe=timeframe,
                source=source,
                **kwargs,
            )
            seen.append((order.signal, decision.approved, decision.code))
            return decision

        with _paper(), patch.object(RiskManager, "evaluate", _spy):
            _fire(hub, _SYMBOL, f"{price:.8f}", f"{price:.8f}")
        assert any(
            signal == "SELL_STOP_FULL" and approved for signal, approved, _code in seen
        )
        filled = [
            order
            for order in _orders(_SYMBOL)
            if order.get("signal") == "SELL_STOP_FULL"
            and order.get("source") == "exit_ws"
            and order.get("status") == "filled"
        ]
        assert len(filled) == 1
    finally:
        _clear_guards()
        reset_all_position_stores_for_tests()


def test_ac_a6_stop_is_measured_from_average_entry():
    """A6: the cycle stop uses market.average_entry, not another price on the lot."""
    assert "average_entry" in (dca_mod.evaluate_long_hard_stop.__doc__ or "")
    stop = _base_stop()
    partial = _partial_of(stop)
    deep_loss = stop + (100.0 - stop) / 2.0
    shallow_loss = partial / 2.0
    assert shallow_loss < partial < stop < deep_loss
    deep_entry = _ENTRY
    price = _price(deep_entry, deep_loss)
    stale_entry = price / (1.0 - shallow_loss / 100.0)
    params = _params(stop=stop, grace=_grace())
    other_prices = {
        "entry": stale_entry,
        "last_buy_price": stale_entry,
        "recent_high": stale_entry,
        "average_entry": stale_entry,
        "dca_rounds": 0,
    }
    assert _cycle(price, deep_entry, other_prices, params) == "SELL_STOP_FULL"
    shallow_on_average = {
        "entry": deep_entry,
        "last_buy_price": deep_entry,
        "recent_high": deep_entry,
        "average_entry": deep_entry,
        "dca_rounds": 0,
    }
    assert _cycle(price, stale_entry, shallow_on_average, params) != "SELL_STOP_FULL"


def test_ac_a7_nonfinite_grace_is_not_a_permanent_pause():
    """A7: inf and NaN are invalid. The window stays finite and the partial can fire."""
    stop = _base_stop()
    partial = _partial_of(stop)
    loss = (stop + partial) / 2.0
    past_default = _DEFAULT_GRACE_HOURS + 1.0
    stamp = (datetime.now() - timedelta(hours=past_default)).isoformat()
    position = {"dca_rounds": 1, "last_dca_at": stamp}
    for bad in (float("inf"), float("nan")):
        params = _params(stop=stop, grace=bad, interval=bad)
        with patch("strategies.dca.log") as warned:
            in_grace = dca_stop_in_grace(position, params)
            action = _act("direct", _price(_ENTRY, loss), _ENTRY, position, params)
        assert in_grace is False
        assert action == "SELL_STOP_PARTIAL"
        assert any(
            "WARNING" == call.args[1] and "grace invalid" in str(call.args[0])
            for call in warned.call_args_list
            if len(call.args) > 1
        )
    finite = _params(stop=stop, grace=float("inf"), interval=_grace())
    inside = {
        "dca_rounds": 1,
        "last_dca_at": (datetime.now() - timedelta(hours=_grace() / 2.0)).isoformat(),
    }
    assert dca_stop_in_grace(inside, finite) is True


def _assert_engine_pause(params: dict, *, inside: bool) -> None:
    """The DecisionEngine pause itself, fed a partial the strategy already emitted."""
    from core.models import SignalAnalysis

    if inside:
        stamp = datetime.now().isoformat()
    else:
        stamp = (datetime.now() - timedelta(hours=_grace() + 1.0)).isoformat()
    position = {"dca_rounds": 1, "last_dca_at": stamp, "average_entry": _ENTRY}
    market = _market_for(_price(_ENTRY, _partial_of(_base_stop())), position, params)
    technical = SignalAnalysis(
        action="SELL_STOP_PARTIAL",
        symbol=_SYMBOL,
        timeframe=_TF,
        rsi=45.0,
        lower_bb=0.0,
        vol_multiplier=1.0,
        ampel_emoji="",
        ampel_text="",
        sources=["technical", "stop_loss"],
    )
    engine = DecisionEngine()
    normalized, _sources, _confidence, rationales, _sell_source, _audit = engine._merge_sell(
        technical, None, None, [], market, position, None
    )
    text = " ".join(str(item) for item in rationales)
    if inside:
        assert normalized != "SELL_PARTIAL_50"
        assert "partial_stop paused" in text
    else:
        assert normalized == "SELL_PARTIAL_50"
        assert "partial_stop paused" not in text


def _decision(market: MarketContext):
    engine = DecisionEngine()
    regime = engine.config.raw.setdefault("regime_detector", {})
    allocator = engine.config.raw.setdefault("strategy_allocator", {})
    with patch.dict(regime, {"enabled": False}), patch.dict(allocator, {"enabled": False}):
        return engine.evaluate_with_market(
            {"symbol": _SYMBOL, "timeframe": _TF, "active": True},
            market,
        )


def _market_for(price: float, position: dict, params: dict) -> MarketContext:
    return MarketContext(
        symbol=_SYMBOL,
        timeframe=_TF,
        current_price=price,
        rsi=45.0,
        lower_bb=price * 0.5,
        vol_multiplier=1.0,
        has_position=True,
        open_positions=1,
        average_entry=_ENTRY,
        sim_state={"average_entry": _ENTRY, **position},
        strategy_params=params,
    )


def _via_eval_job(market: MarketContext):
    class _Orchestrator:
        def begin_tenant_cycle(self):
            return None

        def process_coin(self, coin, price, *_args, **_kwargs):
            market.current_price = float(price)
            analysis = _decision(market)
            return {"action": analysis.action, "rationale": analysis.rationale}

    job = EvalJob(
        symbol=_SYMBOL,
        timeframe=_TF,
        reason="stale_watchlist",
        priority=PRIORITY_ENTRY_15M,
        enqueued_at=0.0,
        tenant_id=DEFAULT_TENANT,
    )
    with patch(
        "storage.tenant_registry.get_tenant",
        return_value={"defaults": {"ledger_scope": "demo"}, "telegram": {}},
    ), patch(
        "services.eval_queue_runtime.get_prices_batch",
        return_value={_SYMBOL: market.current_price},
    ), patch(
        "services.eval_queue_runtime.load_effective_watchlist",
        return_value=[],
    ):
        result = process_eval_job(_Orchestrator(), job)
    assert result is not None
    return result


@pytest.mark.parametrize("via", ["engine", "eval_job"])
def test_ac_f1_decision_engine_grace_boundary(via, monkeypatch):
    """F1: the second pause lives in DecisionEngine, including via process_eval_job.

    Partial is paused only inside the window. Age == window is outside, so a
    ``<=`` grace end and a DecisionEngine that re-pauses open rounds both fail.
    A full stop inside the window is never paused.
    """
    stop = _base_stop()
    partial = _partial_of(stop)
    window = _grace()
    widen = _widen()
    params = _params(stop=stop, grace=window, widen=widen)
    band = (stop + partial) / 2.0
    assert partial < band < stop

    def _run(market: MarketContext):
        if via == "engine":
            analysis = _decision(market)
            return analysis.action, analysis.rationale
        result = _via_eval_job(market)
        return result["action"], result.get("rationale") or ""

    monkeypatch.setattr(dca_mod, "datetime", _FixedNow)
    on_window = (_FixedNow.fixed - timedelta(hours=window)).isoformat()
    action, rationale = _run(
        _market_for(
            _price(_ENTRY, band),
            {"dca_rounds": 1, "last_dca_at": on_window},
            params,
        )
    )
    assert action == "SELL_STOP_PARTIAL"
    assert "partial_stop paused" not in rationale

    monkeypatch.undo()
    inside = (datetime.now() - timedelta(hours=window / 2.0)).isoformat()
    paused, _paused_why = _run(
        _market_for(
            _price(_ENTRY, band),
            {"dca_rounds": 1, "last_dca_at": inside},
            params,
        )
    )
    # The strategy already withholds the partial inside grace, so the merged
    # action is not a partial. The engine's own second pause is asserted below.
    assert paused != "SELL_STOP_PARTIAL"
    _assert_engine_pause(params, inside=True)
    _assert_engine_pause(params, inside=False)

    widened = stop + 2.0 * widen
    deep = widened + (100.0 - widened) / 2.0
    assert deep > widened
    full, _full_why = _run(
        _market_for(
            _price(_ENTRY, deep),
            {"dca_rounds": 1, "dca_recovery_rounds": 1, "last_dca_recovery_at": inside},
            params,
        )
    )
    assert full == "SELL_STOP_FULL"
