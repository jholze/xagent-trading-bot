"""#722 DCA grace: the full stop holds inside the window.

Acceptance ids match the grace spec r2 and the Lena/Viktor build conditions
(AC-G01 … AC-G18, AC-B1, AC-B2). Thresholds and floors come from the loaded
config or from the params the test passes in. Symbols are synthetic.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from core.config import get_bot_config
from core.models import MarketContext
from hermes.memory.store import DEFAULT_PARAMS
from services.exit_realtime.shadow_eval import evaluate_would_sells
from strategies import dca as dca_mod
from strategies.dca import (
    _DEFAULT_GRACE_HOURS,
    _hours_since,
    dca_stop_in_grace,
    evaluate_long_hard_stop,
    full_stop_loss_pct,
    grace_window_hours,
    should_pause_partial_stop,
    trail_exits_paused_after_dca,
    trail_grace_hours_after_dca,
)
from strategies.technical_rsi_bb import TechnicalRSIStrategy

_SYMBOL = "GAP/USDT"
_TF = "4h"
_ENTRY = 2.0


def _cfg():
    return get_bot_config().raw


def _hours_ago(hours: float, *, form: str = "naive") -> str:
    if form == "naive":
        return (datetime.now() - timedelta(hours=hours)).isoformat()
    moment = datetime.now(timezone.utc) - timedelta(hours=hours)
    if form == "z":
        return moment.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    if form == "offset":
        return moment.astimezone(timezone(timedelta(hours=2))).isoformat()
    raise ValueError(form)


def _position(shape: str, age_h: float | None, *, form: str = "naive") -> dict:
    pos = {"dca_rounds": 0, "dca_recovery_rounds": 0, "average_entry": _ENTRY}
    if shape == "dca":
        pos["dca_rounds"] = 1
        key = "last_dca_at"
    else:
        pos["dca_recovery_rounds"] = 1
        key = "last_dca_recovery_at"
    if age_h is not None:
        pos[key] = _hours_ago(age_h, form=form)
    return pos


def _params(*, stop: float, partial: float, grace=None, interval=None, widen: float = 0.0) -> dict:
    block: dict = {"pause_partial_stop_during_dca": True}
    if grace is not None:
        block["grace_hours_after_dca"] = grace
    if interval is not None:
        block["interval_hours"] = interval
    if widen:
        block["stop_loss_widen_pct_per_round"] = widen
    return {
        "stop_loss_pct": stop,
        "partial_stop_pct": partial,
        "dca": block,
    }


def _price(loss_pct: float) -> float:
    return _ENTRY * (1.0 - loss_pct / 100.0)


def _cycle_action(price: float, position: dict, params: dict) -> str:
    market = MarketContext(
        symbol=_SYMBOL,
        timeframe=_TF,
        current_price=price,
        rsi=45.0,
        lower_bb=price * 0.5,
        vol_multiplier=1.0,
        has_position=True,
        open_positions=1,
        average_entry=_ENTRY,
        sim_state=dict(position),
        strategy_params=params,
    )
    return TechnicalRSIStrategy().analyze({"symbol": _SYMBOL, "timeframe": _TF}, market).action


def _ws_action(price: float, position: dict, params: dict) -> str:
    events = evaluate_would_sells(
        symbol=_SYMBOL,
        timeframe=_TF,
        price=price,
        position={"average_entry": _ENTRY, "recent_high": _ENTRY, **position},
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


def _direct_action(price: float, position: dict, params: dict) -> str:
    hit = evaluate_long_hard_stop(
        price=price,
        entry=_ENTRY,
        position=position,
        strategy_params=params,
        base_stop_loss_pct=float(params["stop_loss_pct"]),
    )
    return hit[0] if hit else "HOLD"


_PATHS = {
    "direct": _direct_action,
    "cycle": _cycle_action,
    "ws": _ws_action,
}


def _act(path: str, price: float, position: dict, params: dict) -> str:
    return _PATHS[path](price, position, params)


@pytest.mark.parametrize("path", ["direct", "cycle", "ws"])
@pytest.mark.parametrize("shape", ["dca", "dca_recovery"])
def test_ac_g01_full_stop_holds_inside_grace(path, shape):
    """AC-G01: loss above the full stop fires inside the grace, from the average."""
    window = float(_cfg()["volatile_altcoin"]["dca"]["grace_hours_after_dca"])
    stop, partial = 20.0, 10.0
    params = _params(stop=stop, partial=partial, grace=window)
    pos = _position(shape, window - 0.1)
    assert dca_stop_in_grace(pos, params)
    assert _act(path, _price(stop + 1.0), pos, params) == "SELL_STOP_FULL"


@pytest.mark.parametrize("path", ["direct", "cycle", "ws"])
@pytest.mark.parametrize("shape", ["dca", "dca_recovery"])
def test_ac_g02_partial_stays_paused_inside_grace(path, shape):
    """AC-G02: a partial-band loss inside the grace does not sell."""
    window = float(_cfg()["volatile_altcoin"]["dca"]["grace_hours_after_dca"])
    stop, partial = 20.0, 10.0
    params = _params(stop=stop, partial=partial, grace=window)
    pos = _position(shape, window - 0.1)
    assert _act(path, _price((stop + partial) / 2.0), pos, params) == "HOLD"


@pytest.mark.parametrize("path", ["direct", "cycle", "ws"])
@pytest.mark.parametrize("shape", ["dca", "dca_recovery"])
def test_ac_g03_after_grace_partial_and_full_follow_normal_logic(path, shape):
    """AC-G03 / U-C3-2: after the grace, open rounds do not pause the partial."""
    window = float(_cfg()["volatile_altcoin"]["dca"]["grace_hours_after_dca"])
    stop, partial = 20.0, 10.0
    params = _params(stop=stop, partial=partial, grace=window)
    pos = _position(shape, window + 0.1)
    assert not dca_stop_in_grace(pos, params)
    assert not should_pause_partial_stop(pos, params)
    assert _act(path, _price((stop + partial) / 2.0), pos, params) == "SELL_STOP_PARTIAL"
    assert _act(path, _price(stop + 1.0), pos, params) == "SELL_STOP_FULL"


def test_ac_g04_positive_grace_key_beats_interval():
    """AC-G04: grace_hours_after_dca > 0 is the window, even when it is shorter than the interval."""
    configured = float(_cfg()["volatile_altcoin"]["dca"]["grace_hours_after_dca"])
    shorter = configured / 2.0
    interval = float(_cfg()["mid_cap_defaults"]["dca"]["interval_hours"])
    assert shorter < interval
    params = _params(stop=20.0, partial=10.0, grace=shorter, interval=interval)
    assert grace_window_hours(params) == pytest.approx(shorter)
    inside = _position("dca", shorter - 0.1)
    outside = _position("dca", shorter + 0.1)
    assert dca_stop_in_grace(inside, params)
    assert not dca_stop_in_grace(outside, params)
    assert _direct_action(_price(15.0), inside, params) == "HOLD"
    assert _direct_action(_price(15.0), outside, params) == "SELL_STOP_PARTIAL"


def test_ac_g05_zero_grace_falls_back_to_interval():
    """AC-G05 / U-C4-1: grace 0 falls through to interval_hours (mid-cap on the committed config)."""
    interval = float(_cfg()["mid_cap_defaults"]["dca"]["interval_hours"])
    params = _params(stop=20.0, partial=10.0, grace=0, interval=interval)
    assert grace_window_hours(params) == pytest.approx(interval)
    assert dca_stop_in_grace(_position("dca", interval - 0.1), params)
    assert not dca_stop_in_grace(_position("dca", interval + 0.1), params)
    assert _direct_action(_price(25.0), _position("dca", interval - 0.1), params) == "SELL_STOP_FULL"


def test_ac_g06_missing_grace_and_interval_use_default():
    """AC-G06: no grace key and no interval falls back to the 12 h default, and the full stop still holds."""
    params = {"stop_loss_pct": float(DEFAULT_PARAMS["stop_loss_pct"]), "partial_stop_pct": 8.0}
    assert grace_window_hours(params) == pytest.approx(_DEFAULT_GRACE_HOURS)
    window = grace_window_hours(params)
    stop = float(params["stop_loss_pct"])
    inside = _position("dca", window - 0.1)
    assert dca_stop_in_grace(inside, params)
    assert _direct_action(_price(stop + 1.0), inside, params) == "SELL_STOP_FULL"
    assert _direct_action(_price(stop - 1.0), inside, params) == "HOLD"


@pytest.mark.parametrize("path", ["cycle", "ws"])
@pytest.mark.parametrize("shape", ["dca", "dca_recovery"])
@pytest.mark.parametrize(
    "source",
    ["volatile_key", "stable_key", "mid_interval", "default_12"],
)
def test_ac_g07_full_stop_holds_on_every_grace_source(path, shape, source):
    """AC-G07: the full stop holds for the key, the interval fallback, and the 12 h default."""
    raw = _cfg()
    stop, partial = 20.0, 10.0
    if source == "volatile_key":
        window = float(raw["volatile_altcoin"]["dca"]["grace_hours_after_dca"])
        params = _params(stop=stop, partial=partial, grace=window, interval=1.0)
    elif source == "stable_key":
        window = float(raw["stable_altcoin"]["dca"]["grace_hours_after_dca"])
        params = _params(stop=stop, partial=partial, grace=window, interval=1.0)
    elif source == "mid_interval":
        window = float(raw["mid_cap_defaults"]["dca"]["interval_hours"])
        params = _params(stop=stop, partial=partial, grace=0, interval=window)
    else:
        window = _DEFAULT_GRACE_HOURS
        params = _params(stop=stop, partial=partial, grace=0, interval=0)
    assert grace_window_hours(params) == pytest.approx(window)
    pos = _position(shape, window - 0.1)
    assert _act(path, _price(stop + 1.0), pos, params) == "SELL_STOP_FULL"
    assert _act(path, _price((stop + partial) / 2.0), pos, params) == "HOLD"
    aged = _position(shape, window + 0.1)
    assert _act(path, _price((stop + partial) / 2.0), aged, params) == "SELL_STOP_PARTIAL"


def test_ac_g08_default_boundary_is_11_9_in_and_12_1_out():
    """AC-G08: Hermes params have no dca block, so the window is the 12 h default."""
    params = dict(DEFAULT_PARAMS)
    assert "dca" not in params
    assert grace_window_hours(params) == pytest.approx(_DEFAULT_GRACE_HOURS)
    stop = float(params["stop_loss_pct"])
    ratio = float(_cfg()["risk"]["partial_stop_ratio"])
    partial_band = stop * ratio
    inside = _position("dca", _DEFAULT_GRACE_HOURS - 0.1)
    outside = _position("dca", _DEFAULT_GRACE_HOURS + 0.1)
    assert dca_stop_in_grace(inside, params)
    assert not dca_stop_in_grace(outside, params)
    assert _direct_action(_price(stop + 1.0), inside, params) == "SELL_STOP_FULL"
    assert _direct_action(_price((stop + partial_band) / 2.0), inside, params) == "HOLD"
    assert _direct_action(_price((stop + partial_band) / 2.0), outside, params) == "SELL_STOP_PARTIAL"


def test_ac_g09_trail_grace_follows_stop_chain_without_own_key():
    """AC-G09: with no trail_grace_hours_after_dca key, trail grace uses the stop chain."""
    configured = float(_cfg()["volatile_altcoin"]["dca"]["grace_hours_after_dca"])
    shorter = configured / 2.0
    interval = float(_cfg()["mid_cap_defaults"]["dca"]["interval_hours"])
    assert trail_grace_hours_after_dca(
        {"dca": {"grace_hours_after_dca": shorter, "interval_hours": interval}}
    ) == pytest.approx(shorter)
    assert trail_grace_hours_after_dca(
        {"dca": {"grace_hours_after_dca": 0, "interval_hours": interval}}
    ) == pytest.approx(interval)
    assert trail_grace_hours_after_dca(
        {"dca": {"grace_hours_after_dca": 0, "interval_hours": 0}}
    ) == pytest.approx(_DEFAULT_GRACE_HOURS)
    # The own key still wins when a profile sets it. This test only removes it
    # for the fallback the spec names.
    assert trail_grace_hours_after_dca(
        {"dca": {"trail_grace_hours_after_dca": configured, "grace_hours_after_dca": shorter}}
    ) == pytest.approx(configured)


def test_ac_g10_youngest_timestamp_controls_grace():
    """AC-G10: the younger of the two DCA timestamps keeps the lot in grace."""
    window = float(_cfg()["volatile_altcoin"]["dca"]["grace_hours_after_dca"])
    params = _params(stop=20.0, partial=10.0, grace=window)
    young = _hours_ago(window - 1.0)
    old = _hours_ago(window + 24.0)
    youngest_recovery = {
        "dca_rounds": 1,
        "dca_recovery_rounds": 1,
        "last_dca_at": old,
        "last_dca_recovery_at": young,
    }
    youngest_dca = {
        "dca_rounds": 1,
        "dca_recovery_rounds": 1,
        "last_dca_at": young,
        "last_dca_recovery_at": old,
    }
    both_old = {
        "dca_rounds": 1,
        "dca_recovery_rounds": 1,
        "last_dca_at": old,
        "last_dca_recovery_at": old,
    }
    assert dca_stop_in_grace(youngest_recovery, params)
    assert dca_stop_in_grace(youngest_dca, params)
    assert not dca_stop_in_grace(both_old, params)
    assert _direct_action(_price(15.0), youngest_recovery, params) == "HOLD"
    assert _direct_action(_price(15.0), both_old, params) == "SELL_STOP_PARTIAL"


def test_ac_g11_recovery_rounds_widen_with_dca_rounds():
    """AC-G11: dca_rounds 1 + dca_recovery_rounds 1 widens the full stop by two steps."""
    widen = float(_cfg()["volatile_altcoin"]["dca"]["stop_loss_widen_pct_per_round"])
    base = float(DEFAULT_PARAMS["stop_loss_pct"])
    params = _params(stop=base, partial=base / 2.0, grace=12, widen=widen)
    pos = {"dca_rounds": 1, "dca_recovery_rounds": 1}
    assert full_stop_loss_pct(pos, params, base) == pytest.approx(base + 2 * widen)
    only_dca = full_stop_loss_pct({"dca_rounds": 1, "dca_recovery_rounds": 0}, params, base)
    assert only_dca == pytest.approx(base + widen)


def test_ac_g12_no_auto_sell_blocks_full_stop(monkeypatch):
    """AC-G12: a no_auto_sell lock rejects SELL_STOP_FULL, and a lock-check error fails closed."""
    from unittest.mock import patch

    from core.models import TradeOrder
    from risk.risk_manager import RiskManager
    from strategies.position_lock import build_lock

    cfg = get_bot_config()
    rm = RiskManager(cfg)
    order = TradeOrder(
        type="SELL",
        symbol=_SYMBOL,
        amount=1.0,
        price=_ENTRY,
        signal="SELL_STOP_FULL",
        source="exit_ws",
    )
    locked = {
        "amount": 1.0,
        "average_entry": _ENTRY,
        "lock": build_lock(reason="hold", modes=["no_auto_sell"]),
    }
    with patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")), patch(
        "risk.risk_manager.get_position", return_value=locked
    ), patch("strategies.position_lock.log_lock_block"):
        decision = rm.evaluate(order, timeframe=_TF, source="exit_ws")
    assert decision.approved is False
    assert decision.code == "position_locked"

    def _explode(*_a, **_k):
        raise RuntimeError("lock store down")

    monkeypatch.setattr("strategies.position_lock.auto_sell_blocked", _explode)
    with patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")), patch(
        "risk.risk_manager.get_position", return_value=locked
    ):
        failed = rm.evaluate(order, timeframe=_TF, source="exit_ws")
    assert failed.approved is False
    assert failed.code == "position_lock_check_error"


def test_ac_g13_position_floor_rejects_partial_and_full_is_exempt():
    """AC-G13: after the grace, a lot under the position floor cannot partial; FULL still passes."""
    from unittest.mock import patch

    from core.models import TradeOrder
    from risk.risk_manager import RiskManager

    risk_cfg = get_bot_config().risk_config
    position_floor = float(risk_cfg["min_position_usdt_for_partial_sell"])
    price = 1.0
    lot = position_floor / 2.0
    assert lot < position_floor
    rm = RiskManager(get_bot_config())
    held = {"amount": lot, "average_entry": price, "sold_percent": 0}
    partial = TradeOrder(
        type="SELL",
        symbol=_SYMBOL,
        price=price,
        amount=lot / 2.0,
        signal="SELL_STOP_PARTIAL",
    )
    full = TradeOrder(
        type="SELL",
        symbol=_SYMBOL,
        price=price,
        amount=lot,
        signal="SELL_STOP_FULL",
    )
    with patch("risk.risk_manager.get_position", return_value=held):
        blocked, reason = rm._partial_sell_blocked(partial, _TF, "auto")
        full_blocked, full_reason = rm._partial_sell_blocked(full, _TF, "auto")
    assert blocked is True
    assert "below minimum" in reason.lower()
    assert f"{position_floor:.0f}" in reason
    assert full_blocked is False
    assert full_reason == ""


def test_ac_b1_min_sell_notional_floor_rejects_partial_between_floors():
    """AC-B1 / R1: partial notional under min_sell_notional_usdt is rejected; FULL is approved.

    On the committed floors (position 500, notional 300, partial fraction 0.5)
    the two lots are 550 (partial 275, rejected) and 700 (partial 350, approved).
    """
    from unittest.mock import patch

    from core.models import TradeOrder
    from risk.risk_manager import RiskManager
    from strategies.positions import sell_fraction_for_signal

    risk_cfg = get_bot_config().risk_config
    notional_floor = float(risk_cfg["min_sell_notional_usdt"])
    position_floor = float(risk_cfg["min_position_usdt_for_partial_sell"])
    fraction = sell_fraction_for_signal("SELL_STOP_PARTIAL")
    assert fraction < 1.0
    partial_ceiling = notional_floor / fraction
    assert position_floor < partial_ceiling
    span = partial_ceiling - position_floor
    lot_reject = (position_floor + partial_ceiling) / 2.0
    lot_allow = partial_ceiling + span
    price = 1.0
    rm = RiskManager(get_bot_config())

    def _order(lot: float, signal: str) -> TradeOrder:
        amount = lot if signal == "SELL_STOP_FULL" else lot * fraction
        return TradeOrder(
            type="SELL", symbol=_SYMBOL, price=price, amount=amount, signal=signal
        )

    held_reject = {"amount": lot_reject, "average_entry": price, "sold_percent": 0}
    with patch("risk.risk_manager.get_position", return_value=held_reject):
        blocked, reason = rm._partial_sell_blocked(_order(lot_reject, "SELL_STOP_PARTIAL"), _TF, "auto")
        full_blocked, _ = rm._partial_sell_blocked(_order(lot_reject, "SELL_STOP_FULL"), _TF, "auto")
    partial_notional = lot_reject * fraction
    assert partial_notional < notional_floor
    assert blocked is True
    assert "below minimum" in reason.lower()
    assert f"{notional_floor:.0f}" in reason
    assert full_blocked is False

    held_allow = {"amount": lot_allow, "average_entry": price, "sold_percent": 0}
    with patch("risk.risk_manager.get_position", return_value=held_allow):
        allowed, allow_reason = rm._partial_sell_blocked(
            _order(lot_allow, "SELL_STOP_PARTIAL"), _TF, "auto"
        )
    assert lot_allow * fraction > notional_floor
    assert allowed is False
    assert allow_reason == ""


_G15_VARIANTS = ("none", "missing", "empty", "blank", "unparsable")


def _g15_position(shape: str, variant: str) -> dict:
    pos = {"dca_rounds": 0, "dca_recovery_rounds": 0}
    if shape == "dca":
        pos["dca_rounds"] = 1
        key = "last_dca_at"
    else:
        pos["dca_recovery_rounds"] = 1
        key = "last_dca_recovery_at"
    if variant == "missing":
        return pos
    pos[key] = {
        "none": None,
        "empty": "",
        "blank": "   ",
        "unparsable": "not-a-timestamp",
    }[variant]
    return pos


@pytest.mark.parametrize("shape", ["dca", "dca_recovery"])
@pytest.mark.parametrize("variant", _G15_VARIANTS)
def test_ac_g15_rounds_without_parsable_timestamp_are_outside_grace(shape, variant):
    """AC-G15 / AC-B2: unparsable or absent timestamps are outside the grace.

    The partial fires. The trail pause stays fail-closed.
    """
    stop, partial = 20.0, 10.0
    params = _params(stop=stop, partial=partial, grace=float(
        _cfg()["volatile_altcoin"]["dca"]["grace_hours_after_dca"]
    ))
    pos = _g15_position(shape, variant)
    assert not dca_stop_in_grace(pos, params)
    assert _direct_action(_price((stop + partial) / 2.0), pos, params) == "SELL_STOP_PARTIAL"
    assert _cycle_action(_price((stop + partial) / 2.0), pos, params) == "SELL_STOP_PARTIAL"
    assert _ws_action(_price((stop + partial) / 2.0), pos, params) == "SELL_STOP_PARTIAL"
    paused, why = trail_exits_paused_after_dca(pos, params)
    assert paused is True
    assert why == "dca_trail_pause:missing_ts"


def test_ac_g16_full_stop_is_checked_before_timestamp_parsing(monkeypatch):
    """AC-G16: a parser error cannot suppress a loss that is already past the full stop."""

    def _boom(*_a, **_k):
        raise TypeError("timestamp parsed before the full stop")

    monkeypatch.setattr(dca_mod, "_hours_since", _boom)
    monkeypatch.setattr(dca_mod, "_parse_stop_timestamp", _boom)
    params = _params(stop=20.0, partial=10.0, grace=12)
    pos = {"dca_rounds": 1, "last_dca_at": "2026-10-26T00:00:00+00:00"}
    hit = evaluate_long_hard_stop(
        price=_price(21.0),
        entry=_ENTRY,
        position=pos,
        strategy_params=params,
        base_stop_loss_pct=20.0,
    )
    assert hit == ("SELL_STOP_FULL", "stop_loss")


@pytest.mark.parametrize("path", ["direct", "cycle", "ws"])
def test_ac_g17_offset_timestamps_are_utc(path):
    """AC-G17: a numeric offset is an absolute instant, not a TypeError and not local time."""
    window = float(_cfg()["volatile_altcoin"]["dca"]["grace_hours_after_dca"])
    params = _params(stop=20.0, partial=10.0, grace=window)
    inside = _position("dca", window - 0.1, form="offset")
    outside = _position("dca", window + 0.1, form="offset")
    assert _hours_since(inside["last_dca_at"]) == pytest.approx(window - 0.1, abs=0.05)
    assert _act(path, _price(15.0), inside, params) == "HOLD"
    assert _act(path, _price(15.0), outside, params) == "SELL_STOP_PARTIAL"
    assert _act(path, _price(21.0), inside, params) == "SELL_STOP_FULL"


@contextmanager
def _timezone(name: str):
    previous = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


@pytest.mark.parametrize("path", ["direct", "cycle", "ws"])
def test_ac_g18_zulu_timestamps_are_utc(path):
    """AC-G18: a trailing Z is UTC. On a CET host the old parser read it as local and aged it +1 h."""
    window = _DEFAULT_GRACE_HOURS
    assert window == pytest.approx(12.0)
    params = _params(stop=20.0, partial=10.0, grace=window)
    age = window - 0.5
    with _timezone("Europe/Berlin"):
        stamp = _hours_ago(age, form="z")
        naive = datetime.fromisoformat(stamp[:-1])
        local_age = (datetime.now() - naive).total_seconds() / 3600.0
        assert local_age >= window
        assert _hours_since(stamp) == pytest.approx(age, abs=0.05)
        pos = {"dca_rounds": 1, "last_dca_at": stamp}
        assert _act(path, _price(15.0), pos, params) == "HOLD"
        assert _act(path, _price(21.0), pos, params) == "SELL_STOP_FULL"
