"""Pins for the safety behaviour that is already on staging.

Each invariant calls the real guard. The matching mutation test disables
that guard and expects the invariant assertion to fail. A green mutation
test means the pin is not wired to the guard.
"""

from __future__ import annotations

import pytest

from core.config import BotConfig
from core.models import TradeOrder
from price_fetcher import _stale_price_max_age_sec
from strategies.dca import effective_stop_loss_thresholds
from strategies.dca_policy import DcaGuardResult
from tests.unit.test_dca_guard import _cfg, _open
from tests.unit.test_long_mcap_venue_563 import _eval_env

import risk.risk_manager as risk_manager
import strategies.dca as dca_mod
import strategies.dca_policy as dca_policy


def _lot(**extra) -> dict:
    lot = {
        "amount": 10.0,
        "average_entry": 1.0,
        "entry_price": 1.0,
        "dca_rounds": 0,
        "symbol": "TAG/USDT",
    }
    lot.update(extra)
    return lot


def _add(price: float) -> TradeOrder:
    return TradeOrder(
        type="BUY",
        symbol="TAG/USDT",
        price=price,
        amount=0,
        usdt_amount=200,
        signal="BUY_DCA",
        source="dca",
    )


def _guard(**kwargs) -> DcaGuardResult:
    params = dict(
        pos=_lot(),
        price=1.1,
        source="dca",
        has_open_lot=True,
        symbol="TAG/USDT",
        indicators={"price_age_sec": 0},
    )
    params.update(kwargs)
    return dca_policy.evaluate_dca_guard(**params)


def _assert_below_avg() -> None:
    result = _guard(price=0.9)
    assert result.blocked
    assert "dca_guard_below_avg" in result.codes


def _assert_max_rounds() -> None:
    result = _guard(pos=_lot(dca_rounds=1), price=1.1)
    assert result.blocked
    assert "dca_guard_max_rounds" in result.codes


def _assert_locked() -> None:
    result = _guard(pos=_lot(lock={"enabled": True}), price=1.1)
    assert result.blocked
    assert "dca_guard_locked" in result.codes


def _assert_missing(pos, price, indicators) -> None:
    result = _guard(pos=pos, price=price, indicators=indicators)
    assert result.blocked
    assert result.codes == ["dca_guard_missing_input"]


def _assert_partial_paused() -> None:
    pos = {"dca_rounds": 1, "last_dca_at": None}
    params = {"dca": {"pause_partial_stop_during_dca": True, "max_rounds": 2}}
    assert dca_mod.should_pause_partial_stop(pos, params) is True
    _full, partial, _grace = effective_stop_loss_thresholds(pos, params, 50.0)
    assert partial is None


def _assert_partial_paused_while_rounds_remain() -> None:
    pos = {"dca_rounds": 1, "dca_max_rounds": 2}
    params = {"dca": {"pause_partial_stop_during_dca": False, "max_rounds": 2}}
    assert dca_mod.should_pause_partial_stop(pos, params) is True
    _full, partial, _grace = effective_stop_loss_thresholds(pos, params, 50.0)
    assert partial is None


def test_no_dca_when_live_below_average():
    _assert_below_avg()


def test_live_equal_to_average_is_not_below_avg():
    result = _guard(price=1.0)
    assert "dca_guard_below_avg" not in result.codes
    assert result.blocked is False


def test_new_entry_is_not_the_dca_guard():
    result = _guard(price=0.5, has_open_lot=False)
    assert result.blocked is False


def test_mutation_below_avg_fails_when_guard_disabled(monkeypatch):
    monkeypatch.setattr(
        dca_policy,
        "evaluate_dca_guard",
        lambda *args, **kwargs: DcaGuardResult(blocked=False, codes=[]),
    )
    with pytest.raises(AssertionError):
        _assert_below_avg()


def test_no_second_add_when_dca_rounds_at_least_one():
    _assert_max_rounds()


def test_mutation_max_rounds_fails_when_guard_disabled(monkeypatch):
    monkeypatch.setattr(
        dca_policy,
        "evaluate_dca_guard",
        lambda *args, **kwargs: DcaGuardResult(blocked=False, codes=[]),
    )
    with pytest.raises(AssertionError):
        _assert_max_rounds()


def test_no_add_when_position_is_locked():
    _assert_locked()


def test_mutation_lock_fails_when_guard_disabled(monkeypatch):
    monkeypatch.setattr(
        dca_policy,
        "evaluate_dca_guard",
        lambda *args, **kwargs: DcaGuardResult(blocked=False, codes=[]),
    )
    with pytest.raises(AssertionError):
        _assert_locked()


@pytest.mark.parametrize(
    "pos,price",
    [
        ({"amount": 1, "average_entry": 1.0}, 1.1),
        ({"amount": 1, "average_entry": 1.0, "dca_rounds": 0}, None),
        ({"amount": 1, "average_entry": 0, "dca_rounds": 0}, 1.1),
        ({"amount": 1, "dca_rounds": 0}, 1.1),
        ({"amount": 1, "average_entry": 1.0, "dca_rounds": "nope"}, 1.1),
        ({"amount": 1, "average_entry": 1.0, "dca_rounds": 0, "lock": "bad"}, 1.1),
    ],
)
def test_missing_or_invalid_inputs_fail_closed(pos, price):
    _assert_missing(pos, price, {"price_age_sec": 0})


def test_stale_price_fails_closed():
    age = float(_stale_price_max_age_sec()) + 1.0
    _assert_missing(_lot(), 1.1, {"price_age_sec": age})


def test_non_dict_position_fails_closed():
    result = dca_policy.evaluate_dca_guard(
        None,
        price=1.1,
        source="dca",
        has_open_lot=True,
        symbol="TAG/USDT",
        indicators={"price_age_sec": 0},
    )
    assert result.blocked
    assert result.codes == ["dca_guard_missing_input"]


def test_mutation_missing_input_fails_when_guard_disabled(monkeypatch):
    monkeypatch.setattr(
        dca_policy,
        "evaluate_dca_guard",
        lambda *args, **kwargs: DcaGuardResult(blocked=False, codes=[]),
    )
    with pytest.raises(AssertionError):
        _assert_missing(_lot(), None, {"price_age_sec": 0})


def test_partial_stop_paused_during_dca():
    _assert_partial_paused()


def test_partial_stop_stays_paused_while_rounds_remain_if_flag_off():
    _assert_partial_paused_while_rounds_remain()
    done = {"dca_rounds": 2, "dca_max_rounds": 2}
    params = {"dca": {"pause_partial_stop_during_dca": False, "max_rounds": 2}}
    assert dca_mod.should_pause_partial_stop(done, params) is False
    _full, partial, _grace = effective_stop_loss_thresholds(
        done,
        {**params, "partial_stop_pct": 25},
        50.0,
    )
    assert partial == 25


def test_mutation_partial_stop_pause_fails_when_guard_disabled(monkeypatch):
    monkeypatch.setattr(dca_mod, "should_pause_partial_stop", lambda *args, **kwargs: False)
    with pytest.raises(AssertionError):
        _assert_partial_paused()
    with pytest.raises(AssertionError):
        _assert_partial_paused_while_rounds_remain()


def _risk_add(price: float, lot: dict):
    rm = risk_manager.RiskManager(_cfg())
    with _open(rm, lot):
        return rm.evaluate(_add(price), "1h", source="dca", indicators={"price_age_sec": 0})


def _assert_risk_below_avg() -> None:
    decision = _risk_add(0.9, _lot())
    assert decision.approved is False
    assert decision.code == "dca_guard_below_avg"


def test_risk_manager_blocks_add_below_average():
    _assert_risk_below_avg()


def test_mutation_risk_manager_below_avg_fails_when_guard_disabled(monkeypatch):
    monkeypatch.setattr(
        dca_policy,
        "evaluate_dca_guard",
        lambda *args, **kwargs: DcaGuardResult(blocked=False, codes=[]),
    )
    with pytest.raises(AssertionError):
        _assert_risk_below_avg()


def test_risk_manager_fail_closed_when_guard_raises(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("guard down")

    monkeypatch.setattr(dca_policy, "evaluate_dca_guard", _boom)
    decision = _risk_add(1.1, _lot())
    assert decision.approved is False
    assert "dca_guard_missing_input" in (decision.details or {}).get("codes", [])


def test_cfg_fixture_is_botconfig():
    assert isinstance(_cfg(), BotConfig)
    assert _eval_env is not None
