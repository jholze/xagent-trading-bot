"""#709: stale sold_percent must not block a WS hard partial that still holds the lot.

15.10.2026, HBAR/USDT default 1h. The exit-radar fired SELL_STOP_PARTIAL at
06:16:54 and 07:51:44 MESZ. partial_sell_guard rejected both with
``already sold 100%`` while amount was still the full peak (9611.2). The
fill landed at 11:55:25, 5h 38min after the first fire.

The plausibility check compares stored sold_percent with
``1 - amount / max(peak_amount, amount)``. It may only lower the value used
for the check. It is not written back onto the lot.

The hub, after that rejection (or a position-below-minimum rejection),
escalates the same fire to SELL_STOP_FULL when the lot has a usable peak.
That escalation is once per lot and reason.

Without a usable peak the stored sold_percent is ignored for one partial
sized from the current amount. A second rejection may still take the
existing dust full (lot under the partial minimum, or a remainder at or
above 99%). Each of those follow-ups runs once per lot.
"""

from __future__ import annotations

import inspect
import textwrap
from unittest.mock import patch

import pytest

from core.models import TradeOrder
from risk.risk_manager import RiskManager
from services.exit_realtime.hub import ExitRealtimeHub

AVG = 0.104045342923328  # default lot avg
PEAK = 9611.201653209378  # xagent_lots peak_amount
LIMITS = {  # config.json risk.* (staging), same as the reject message (max 99%)
    "min_position_usdt": 500.0,
    "min_notional_usdt": 300.0,
    "max_sold_percent": 0.99,
    "dust_sweep_max_position_usdt": 500.0,
    "dust_sweep_sold_percent_min": 0.70,
    "dust_sweep_min_remainder_usdt": 100.0,
}
_PLAUSIBILITY_LINE = "sold_pct = _lower_sold_percent_for_check(pos, sold_pct)"


def _hbar_lot(sold_percent: float) -> dict:
    return {
        "amount": PEAK,
        "peak_amount": PEAK,
        "sold_percent": sold_percent,
        "average_entry": AVG,
        "dca_rounds": 0,
        "strategy_tier": "stable",
        "side": "long",
    }


def _ws_partial(price: float) -> TradeOrder:
    return TradeOrder(
        type="SELL",
        symbol="HBAR/USDT",
        price=price,
        amount=PEAK * 0.5,
        signal="SELL_STOP_PARTIAL",
        source="exit_ws",
        exit_source="stop_loss",
        exit_rationale=f"WS hard stop SELL_STOP_PARTIAL last={price} bid={price}",
    )


def _uni_order(price: float, amount: float) -> TradeOrder:
    return TradeOrder(
        type="SELL",
        symbol="UNI/USDT",
        price=price,
        amount=amount,
        signal="SELL_STOP_PARTIAL",
        source="exit_ws",
        exit_source="stop_loss",
    )


def _uni_lot(sold_percent: float) -> dict:
    # xagent_lots 16.10.2026: amount above peak because DCA adds do not raise peak.
    return {
        "amount": 546.5397,
        "peak_amount": 297.8817,
        "sold_percent": sold_percent,
        "average_entry": 6.0,
        "dca_rounds": 2,
        "strategy_tier": "stable",
        "side": "long",
    }


def _blocked(lot: dict, price: float, order: TradeOrder | None = None):
    risk = RiskManager()
    with patch("risk.risk_manager.get_position", return_value=lot), patch.object(
        risk, "_partial_sell_limits", return_value=LIMITS
    ), patch("strategies.exit_ladder.ladder_enabled", return_value=False):
        return risk._partial_sell_blocked(order or _ws_partial(price), "1h", source="exit_ws")


def test_control_consistent_lot_passes_guard():
    """Control: same HBAR lot with sold_percent=0 passes at the 06:16 price."""
    blocked, reason = _blocked(_hbar_lot(0.0), 0.08168)
    assert not blocked, reason


def test_ws_hard_partial_not_blocked_by_stale_sold_percent_at_0616():
    """06:16:54 MESZ. amount == peak, sold_percent == 1.0 is stale.

    Red on staging (the guard blocks). Green once the check lowers sold_percent
    for this decision only.
    """
    lot = _hbar_lot(1.0)
    blocked, reason = _blocked(lot, 0.08168)
    assert not blocked, (
        "WS hard stop SELL_STOP_PARTIAL on a lot still holding 100% of peak was "
        f"blocked by stale sold_percent: {reason}"
    )
    assert lot["sold_percent"] == 1.0


def test_dca_lot_amount_above_peak_consistent_passes():
    blocked, reason = _blocked(_uni_lot(0.0), 5.0, _uni_order(5.0, 546.5397 * 0.5))
    assert not blocked, reason


def test_dca_lot_amount_above_peak_stale_sold_percent_passes():
    lot = _uni_lot(1.0)
    blocked, reason = _blocked(lot, 5.0, _uni_order(5.0, 546.5397 * 0.5))
    assert not blocked, reason
    assert lot["sold_percent"] == 1.0


def test_genuinely_sold_out_lot_still_blocked():
    """Guard keeps working: 0.5% of peak left, stored 0.995 is plausible."""
    lot = {
        "amount": 5000.0,
        "peak_amount": 1_000_000.0,
        "sold_percent": 0.995,
        "average_entry": 1.2,
        "dca_rounds": 0,
        "strategy_tier": "stable",
        "side": "long",
    }
    blocked, reason = _blocked(lot, 1.0, _uni_order(1.0, 2500.0))
    assert blocked and "already sold" in reason, reason
    assert lot["sold_percent"] == 0.995


def test_correction_never_raises_sold_percent_and_is_not_persisted():
    """Stored below the implied fraction must stay stored. Nothing is written back.

    implied = 1 - 500/100000 = 0.995, which is over max_sold_percent 0.99.
    Raising 0.10 to 0.995 would block the partial. The check must not do that,
    and the lot dict keeps 0.10.
    """
    lot = {
        "amount": 500.0,
        "peak_amount": 100_000.0,
        "sold_percent": 0.10,
        "average_entry": 2.0,
        "dca_rounds": 0,
        "strategy_tier": "stable",
        "side": "long",
    }
    order = TradeOrder(
        type="SELL",
        symbol="HBAR/USDT",
        price=2.0,
        amount=250.0,
        signal="SELL_STOP_PARTIAL",
        source="exit_ws",
        exit_source="stop_loss",
    )
    blocked, reason = _blocked(lot, 2.0, order)
    assert not blocked, reason
    assert lot["sold_percent"] == 0.10
    assert "sold_percent" in lot
    # The corrected HBAR case lowers the value used in the check and still
    # leaves the stored field alone.
    stale = _hbar_lot(1.0)
    blocked, reason = _blocked(stale, 0.08168)
    assert not blocked, reason
    assert stale["sold_percent"] == 1.0


def test_mutation_removing_plausibility_check_blocks_hbar_again():
    """Counter-proof: the check is load-bearing.

    Red when the assignment is gone from ``_partial_sell_blocked``. With the
    assignment present, replacing it with ``pass`` blocks the 06:16 HBAR lot
    and the stale UNI lot again, while a consistent lot still passes.
    """
    import risk.risk_manager as rm

    src = inspect.getsource(RiskManager._partial_sell_blocked)
    assert _PLAUSIBILITY_LINE in src, (
        "plausibility check missing from _partial_sell_blocked; "
        "the 06:16 HBAR stop stays blocked by stale sold_percent"
    )
    blocked, reason = _blocked(_hbar_lot(1.0), 0.08168)
    assert not blocked, reason

    dedented = textwrap.dedent(src)
    assert _PLAUSIBILITY_LINE in dedented
    mutated = dedented.replace(_PLAUSIBILITY_LINE, "pass", 1)
    assert _PLAUSIBILITY_LINE not in mutated
    fn_name = "_partial_sell_blocked_mutation_709"
    mutated = mutated.replace("def _partial_sell_blocked(", f"def {fn_name}(", 1)
    exec(mutated, rm.__dict__)  # noqa: S102 — load-bearing mutation of this method
    try:
        fn = rm.__dict__[fn_name]

        def _mut_blocked(lot, price, order=None):
            risk = RiskManager()
            with patch("risk.risk_manager.get_position", return_value=lot), patch.object(
                risk, "_partial_sell_limits", return_value=LIMITS
            ), patch("strategies.exit_ladder.ladder_enabled", return_value=False):
                return fn(risk, order or _ws_partial(price), "1h", "exit_ws")

        hbar = _hbar_lot(1.0)
        blocked, reason = _mut_blocked(hbar, 0.08168)
        assert blocked and "already sold" in reason, reason
        assert hbar["sold_percent"] == 1.0

        uni = _uni_lot(1.0)
        blocked, reason = _mut_blocked(uni, 5.0, _uni_order(5.0, 546.5397 * 0.5))
        assert blocked and "already sold" in reason, reason

        ok, reason = _mut_blocked(_hbar_lot(0.0), 0.08168)
        assert not ok, reason

        sold = {
            "amount": 5000.0,
            "peak_amount": 1_000_000.0,
            "sold_percent": 0.995,
            "average_entry": 1.2,
            "dca_rounds": 0,
            "strategy_tier": "stable",
            "side": "long",
        }
        blocked, reason = _mut_blocked(sold, 1.0, _uni_order(1.0, 2500.0))
        assert blocked and "already sold" in reason, reason
    finally:
        rm.__dict__.pop(fn_name, None)


def _warning_lines(calls) -> list[str]:
    lines = []
    for call in calls:
        args = call.args
        kwargs = call.kwargs
        message = args[0] if args else kwargs.get("message", "")
        level = args[1] if len(args) > 1 else kwargs.get("level", "INFO")
        if level == "WARNING" and str(message).startswith("rejected_stop "):
            lines.append(str(message))
    return lines


def _evaluate_stop(lot: dict, order: TradeOrder, *, source: str = "exit_ws"):
    risk = RiskManager()
    with patch("risk.risk_manager.get_position", return_value=lot), patch.object(
        risk, "_partial_sell_limits", return_value=LIMITS
    ), patch("strategies.exit_ladder.ladder_enabled", return_value=False), patch(
        "strategies.position_lock.attach_lock_from_ledger",
        side_effect=lambda pos, *a, **k: pos,
    ):
        return risk.evaluate(order, "1h", source=source)


def test_rejected_stop_warning_fields_and_counter():
    """One WARNING per rejected stop, plus a counter readable after deploy.

    Fields: timestamp, tenant, symbol, tf, signal kind (SELL_STOP_FULL or
    partial), stored sold_percent, amount, peak_amount, rejection reason.
    ``rejected_stop_count()`` matches the warnings in this process.
    ``ExitRealtimeHub.stats()['rejected_stops']`` is the same count on the
    exit-radar ``GET /health`` payload (``hub.rejected_stops``).
    """
    import risk.risk_manager as rm

    reset = getattr(rm, "reset_risk_manager_globals_for_tests", None)
    if reset:
        reset()
    count = getattr(rm, "rejected_stop_count", lambda: 0)

    lot = {
        "amount": 5000.0,
        "peak_amount": 1_000_000.0,
        "sold_percent": 0.995,
        "average_entry": 1.2,
        "dca_rounds": 0,
        "strategy_tier": "stable",
        "side": "long",
    }
    partial = TradeOrder(
        type="SELL",
        symbol="HBAR/USDT",
        price=1.0,
        amount=2500.0,
        signal="SELL_STOP_PARTIAL",
        source="exit_ws",
        exit_source="stop_loss",
    )
    before = count()
    with patch("logger.log") as log:
        decision = _evaluate_stop(lot, partial)
    assert decision.approved is False
    assert "already sold" in (decision.message or "")
    warnings = _warning_lines(log.call_args_list)
    assert len(warnings) == 1, warnings
    msg = warnings[0]
    assert "timestamp=" in msg
    # display clock is YYYY-MM-DD HH:MM:SS
    ts = msg.split("timestamp=", 1)[1][:19]
    assert ts[4] == "-" and ts[7] == "-" and ts[10] == " " and ts[13] == ":" and ts[16] == ":"
    assert "tenant=default" in msg
    assert "symbol=HBAR/USDT" in msg
    assert "tf=1h" in msg
    assert "signal=partial" in msg
    assert "signal=SELL_STOP_FULL" not in msg
    assert "sold_percent=0.995" in msg
    assert "amount=5000.0" in msg or "amount=5000" in msg
    assert "peak_amount=1000000.0" in msg or "peak_amount=1000000" in msg
    assert "reason=" in msg
    assert "already sold" in msg.split("reason=", 1)[1]
    assert lot["sold_percent"] == 0.995
    assert count() == before + 1
    assert f"rejected_stops={count()}" in msg

    full_lot = _hbar_lot(0.0)
    full = TradeOrder(
        type="SELL",
        symbol="HBAR/USDT",
        price=0.08168,
        amount=PEAK,
        signal="SELL_STOP_FULL",
        source="auto",
        exit_source="stop_loss",
    )
    risk = RiskManager()
    with patch("risk.risk_manager.get_position", return_value=full_lot), patch.object(
        risk, "_trade_cooldown_blocked", return_value=(True, "cooldown for test")
    ), patch(
        "strategies.position_lock.attach_lock_from_ledger",
        side_effect=lambda pos, *a, **k: pos,
    ), patch("logger.log") as log:
        decision = risk.evaluate(full, "1h", source="auto")
    assert decision.approved is False
    warnings = _warning_lines(log.call_args_list)
    assert len(warnings) == 1, warnings
    full_msg = warnings[0]
    assert "signal=SELL_STOP_FULL" in full_msg
    assert "tenant=default" in full_msg
    assert "symbol=HBAR/USDT" in full_msg
    assert "tf=1h" in full_msg
    assert "sold_percent=0.0" in full_msg
    assert f"amount={PEAK}" in full_msg
    assert f"peak_amount={PEAK}" in full_msg
    assert "reason=cooldown for test" in full_msg
    assert "timestamp=" in full_msg
    assert full_lot["sold_percent"] == 0.0
    # Cooldown is not one of the two counted reasons.
    assert count() == before + 1
    assert count("default") == before + 1

    hub = _hub()
    assert hub.stats().get("rejected_stops") == 0
    _fire_rejected_partial(hub, "Partial sell blocked: already sold 100% of position (max 99%)")
    assert hub.stats()["rejected_stops"] == 1


def _hub() -> ExitRealtimeHub:
    return ExitRealtimeHub(
        {
            "exit_realtime": {
                "enabled": True,
                "mode": "live",
                "sources": ["stop_loss"],
                "live_cooldown_sec": 0,
                "default_atr_pct": 3.0,
            }
        }
    )


def _reset_once() -> None:
    import services.exit_realtime.hub as hubmod

    reset = getattr(hubmod, "reset_stop_retry_memory_for_tests", None)
    if reset:
        reset()


def _fire_rejected_partial(
    hub: ExitRealtimeHub,
    message: str,
    *,
    symbol: str = "HBAR/USDT",
    lot: dict | None = None,
    reset_once: bool = True,
    tenant_id: str | None = None,
    full_result: dict | None = None,
    amounts: list | None = None,
):
    from strategies import positions as pm

    if reset_once:
        _reset_once()
    lot = dict(lot or _hbar_lot(1.0))
    pm.positions[pm.get_key(symbol, "1h")] = dict(lot)
    row = {
        "symbol": symbol,
        "timeframe": "1h",
        "position": dict(lot),
        "average_entry": lot.get("average_entry", AVG),
        "recent_high": AVG,
        "strategy_params": {"stop_loss_pct": 50.0, "partial_stop_pct": 33.5},
        "atr_pct": 3.0,
    }
    if tenant_id:
        row["tenant_id"] = tenant_id
    hub.update_book([row])
    calls: list[str] = []

    def _exec(**kwargs):
        action = kwargs.get("action")
        calls.append(action)
        if amounts is not None:
            amounts.append(kwargs.get("sell_amount"))
        if action == "SELL_STOP_PARTIAL":
            return {"ok": True, "executed": False, "message": message}
        if action == "SELL_STOP_FULL":
            if full_result is not None:
                return full_result
            return {"ok": True, "executed": True, "message": "filled"}
        return {"ok": False, "executed": False, "message": "unexpected"}

    events = [
        {
            "source": "stop_loss",
            "action": "SELL_STOP_PARTIAL",
            "priority": 8,
            "rationale": "WS hard stop SELL_STOP_PARTIAL",
            "strategy_shadow": False,
        }
    ]
    with patch(
        "services.exit_realtime.hub.evaluate_would_sells", return_value=events
    ), patch(
        "services.exit_realtime.hub.try_execute_trail_exit", side_effect=_exec
    ), patch(
        "services.exit_realtime.hub._log_event"
    ), patch(
        "strategies.position_lock.attach_lock_from_ledger",
        side_effect=lambda pos, *a, **k: pos,
    ), patch(
        "strategies.position_lock.auto_sell_blocked", return_value=(False, "")
    ):
        hub.on_ticker(symbol.replace("/", "_"), 0.08168, highest_bid=0.08164)
    return calls


def test_rejected_partial_escalates_to_sell_stop_full():
    """A rejected hard partial is re-fired once as SELL_STOP_FULL.

    Red when the hub does not escalate: try_execute is called only with
    SELL_STOP_PARTIAL.
    """
    hub = _hub()
    calls = _fire_rejected_partial(
        hub,
        "Partial sell blocked: already sold 100% of position (max 99%)",
    )
    assert calls == ["SELL_STOP_PARTIAL", "SELL_STOP_FULL"], calls
    full = calls[-1]
    assert full == "SELL_STOP_FULL"
    src = inspect.getsource(ExitRealtimeHub._eval_row_in_context)
    assert "_rejected_partial_escalates_to_full(" in src
    assert 'action="SELL_STOP_FULL"' in src


def test_position_below_minimum_escalates_to_sell_stop_full():
    hub = _hub()
    calls = _fire_rejected_partial(
        hub,
        "Partial sell blocked: position $390.55 below minimum $500 "
        "(use full close or manual sell)",
    )
    assert calls == ["SELL_STOP_PARTIAL", "SELL_STOP_FULL"], calls


def test_below_gate_minimum_does_not_escalate_here():
    """The venue pause stays the owner of 'below Gate minimum'."""
    hub = _hub()
    calls = _fire_rejected_partial(
        hub,
        "Order value below Gate minimum ($3.00)",
    )
    assert calls == ["SELL_STOP_PARTIAL"], calls


def test_helper_only_lowers_and_does_not_write_the_lot():
    from risk.risk_manager import _lower_sold_percent_for_check

    held = {"amount": PEAK, "peak_amount": PEAK}
    assert _lower_sold_percent_for_check(held, 1.0) == 0.0
    assert "sold_percent" not in held
    assert _lower_sold_percent_for_check(held, 0.0) == 0.0

    under = {"amount": 500.0, "peak_amount": 100_000.0, "sold_percent": 0.10}
    # implied 0.995 would block; the helper must not raise 0.10 to that.
    assert _lower_sold_percent_for_check(under, 0.10) == 0.10
    assert under["sold_percent"] == 0.10

    plausible = {"amount": 5000.0, "peak_amount": 1_000_000.0}
    assert _lower_sold_percent_for_check(plausible, 0.995) == pytest.approx(0.995)

    # No peak: amount alone would imply 0 sold and must not clear a real fraction.
    no_peak = {"amount": 200.0, "sold_percent": 0.80}
    assert _lower_sold_percent_for_check(no_peak, 0.80) == 0.80
    assert no_peak["sold_percent"] == 0.80


def test_nan_peak_does_not_zero_sold_percent():
    """A NaN peak is not a basis. It must not lower sold_percent to 0."""
    from risk.risk_manager import _lower_sold_percent_for_check

    lot = {
        "amount": PEAK,
        "peak_amount": float("nan"),
        "sold_percent": 1.0,
        "average_entry": AVG,
        "dca_rounds": 0,
        "strategy_tier": "stable",
        "side": "long",
    }
    assert _lower_sold_percent_for_check(lot, 1.0) == 1.0
    blocked, reason = _blocked(lot, 0.08168)
    assert blocked and "already sold" in reason, reason
    assert lot["sold_percent"] == 1.0


def test_missing_peak_logs_null_and_still_counts():
    """No peak logs peak_amount=null, and the WARNING plus the counter still fire."""
    import risk.risk_manager as rm

    reset = getattr(rm, "reset_risk_manager_globals_for_tests", None)
    if reset:
        reset()
    count = rm.rejected_stop_count
    lot = {
        "amount": PEAK,
        "sold_percent": 1.0,
        "average_entry": AVG,
        "dca_rounds": 0,
        "strategy_tier": "stable",
        "side": "long",
        "first_buy_at": "2026-10-15T04:16:54+00:00",
    }
    assert "peak_amount" not in lot
    order = TradeOrder(
        type="SELL",
        symbol="HBAR/USDT",
        price=0.08168,
        amount=PEAK * 0.5,
        signal="SELL_STOP_PARTIAL",
        source="exit_ws",
        exit_source="stop_loss",
    )
    before = count()
    with patch("logger.log") as log:
        decision = _evaluate_stop(lot, order)
    assert decision.approved is False
    assert "already sold" in (decision.message or "")
    warnings = _warning_lines(log.call_args_list)
    assert len(warnings) == 1, warnings
    msg = warnings[0]
    assert "peak_amount=null" in msg
    assert "peak_amount=0.0" not in msg
    assert "peak_amount=0 " not in msg
    assert "signal=partial" in msg
    assert count() == before + 1
    assert count("default") == before + 1
    assert lot.get("sold_percent") == 1.0


class _Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _hub_cooldown() -> ExitRealtimeHub:
    return ExitRealtimeHub(
        {
            "exit_realtime": {
                "enabled": True,
                "mode": "live",
                "sources": ["stop_loss"],
                "live_cooldown_sec": 15,
                "default_atr_pct": 3.0,
            }
        }
    )


_ALREADY_SOLD = "Partial sell blocked: already sold 100% of position (max 99%)"
_BELOW_MIN = (
    "Partial sell blocked: position $100.00 below minimum $500 "
    "(use full close or manual sell)"
)


def test_escalation_once_across_three_windows_and_hub_rebuild():
    """Three cooldown windows plus a new hub escalate exactly once."""
    _reset_once()
    lot = _hbar_lot(1.0)
    lot["first_buy_at"] = "2026-10-15T04:16:54+00:00"
    clock = _Clock()
    calls: list[str] = []
    with patch("services.exit_realtime.hub.time.monotonic", clock):
        hub = _hub_cooldown()
        for _ in range(3):
            calls.extend(
                _fire_rejected_partial(
                    hub, _ALREADY_SOLD, lot=lot, reset_once=False
                )
            )
            clock.now += 15
        rebuilt = _hub_cooldown()
        calls.extend(
            _fire_rejected_partial(
                rebuilt, _ALREADY_SOLD, lot=lot, reset_once=False
            )
        )
    fulls = [c for c in calls if c == "SELL_STOP_FULL"]
    partials = [c for c in calls if c == "SELL_STOP_PARTIAL"]
    assert fulls == ["SELL_STOP_FULL"], calls
    assert len(partials) == 4, calls


def test_partial_and_full_of_one_event_count_once_and_not_other_reasons():
    """Inflight and exchange errors are not counted. Partial+full is one.

    Each fire is its own cooldown window. A configured cooldown of 0 is read
    as the 15s default, so the clock has to move or the later fires never leave.
    """
    import risk.risk_manager as rm

    reset = getattr(rm, "reset_risk_manager_globals_for_tests", None)
    if reset:
        reset()
    clock = _Clock()
    hub = _hub()
    with patch("services.exit_realtime.hub.time.monotonic", clock):
        _fire_rejected_partial(hub, "inflight")
        assert hub.stats()["rejected_stops"] == 0
        clock.now += 15
        _fire_rejected_partial(hub, "remote_error: exchange 503", reset_once=False)
        assert hub.stats()["rejected_stops"] == 0

        clock.now += 15
        rejected = {"ok": True, "executed": False, "message": _ALREADY_SOLD}
        calls = _fire_rejected_partial(
            hub, _ALREADY_SOLD, full_result=rejected, reset_once=False
        )
        assert calls == ["SELL_STOP_PARTIAL", "SELL_STOP_FULL"], calls
        assert hub.stats()["rejected_stops"] == 1
        by_tenant = hub.stats()["rejected_stops_by_tenant"]
        assert by_tenant.get("default") == 1

        clock.now += 15
        with patch("storage.tenant_registry.get_tenant", return_value={}):
            _fire_rejected_partial(
                hub,
                _BELOW_MIN,
                symbol="UNI/USDT",
                lot=_uni_lot(1.0),
                tenant_id="henry",
                reset_once=False,
            )
    assert hub.stats()["rejected_stops_by_tenant"].get("default") == 1
    assert hub.stats()["rejected_stops_by_tenant"].get("henry") == 1
    assert hub.stats()["rejected_stops"] == 2
    assert rm.rejected_stop_count("other") == 0


def test_process_counter_splits_tenants_and_counts_one_event_once():
    import risk.risk_manager as rm

    rm.reset_risk_manager_globals_for_tests()
    common = dict(
        symbol="HBAR/USDT",
        timeframe="1h",
        sold_percent=1.0,
        amount=PEAK,
        peak_amount=PEAK,
    )
    rm.note_rejected_stop(
        tenant="default",
        signal="SELL_STOP_PARTIAL",
        reason=_ALREADY_SOLD,
        **common,
    )
    rm.note_rejected_stop(
        tenant="henry",
        signal="SELL_STOP_PARTIAL",
        reason=_BELOW_MIN,
        **common,
    )
    rm.note_rejected_stop(
        tenant="default",
        signal="SELL_STOP_PARTIAL",
        reason="inflight",
        **common,
    )
    assert rm.rejected_stop_count("default") == 1
    assert rm.rejected_stop_count("henry") == 1
    assert rm.rejected_stop_count() == 2

    rm.reset_risk_manager_globals_for_tests()
    with rm.rejected_stop_event():
        rm.note_rejected_stop(
            tenant="default",
            signal="SELL_STOP_PARTIAL",
            reason=_ALREADY_SOLD,
            **common,
        )
        rm.note_rejected_stop(
            tenant="default",
            signal="SELL_STOP_FULL",
            reason=_ALREADY_SOLD,
            **common,
        )
    assert rm.rejected_stop_count("default") == 1
    assert rm.rejected_stop_count() == 1


def _no_peak_lot() -> dict:
    lot = _hbar_lot(1.0)
    lot.pop("peak_amount", None)
    lot["first_buy_at"] = "2026-10-15T04:16:54+00:00"
    return lot


def _clear_execute_guards(symbol: str = "HBAR/USDT") -> None:
    import services.exit_realtime.execute as ex
    from services.exit_realtime.execute import exit_guard_key

    with ex._inflight_lock:
        for key in (symbol, exit_guard_key(symbol), exit_guard_key(symbol, "default")):
            ex._inflight.discard(key)
            ex._last_exit_at.pop(key, None)


def test_no_peak_rejection_sells_partial_on_current_amount():
    """Peak missing: sell the stop fraction of the current amount.

    Not the full lot, and not zero. Red when the rejection escalates to a
    full sell or the partial never leaves.
    """
    from types import SimpleNamespace

    from services.trading_service import TradingService
    from strategies import positions as pm

    _reset_once()
    lot = _no_peak_lot()
    key = pm.get_key("HBAR/USDT", "1h")
    pm.positions[key] = dict(lot)
    _clear_execute_guards()
    placed: list[dict] = []

    def _execute(self, order, timeframe="4h", source=None, **kwargs):
        decision = self.risk.evaluate(order, timeframe, source=source or "exit_ws")
        placed.append(
            {
                "signal": order.signal,
                "amount": float(order.amount),
                "approved": bool(decision.approved),
                "message": decision.message,
            }
        )
        return SimpleNamespace(
            executed=bool(decision.approved),
            message=str(decision.message or ""),
        )

    events = [
        {
            "source": "stop_loss",
            "action": "SELL_STOP_PARTIAL",
            "priority": 8,
            "rationale": "WS hard stop SELL_STOP_PARTIAL",
            "strategy_shadow": False,
        }
    ]
    hub = _hub()
    try:
        with patch(
            "services.exit_realtime.hub.evaluate_would_sells", return_value=events
        ), patch(
            "services.exit_realtime.config.exit_execute_url", return_value=""
        ), patch(
            "strategies.exit_ladder.ladder_enabled", return_value=False
        ), patch(
            "strategies.position_lock.attach_lock_from_ledger",
            side_effect=lambda pos, *a, **k: pos,
        ), patch.object(
            RiskManager, "_partial_sell_limits", return_value=LIMITS
        ), patch.object(
            RiskManager, "_trade_cooldown_blocked", return_value=(False, "")
        ), patch.object(
            RiskManager, "_effective_max_daily_sells", return_value=0
        ), patch.object(
            TradingService, "execute_order", _execute
        ), patch(
            "services.exit_realtime.hub._log_event"
        ):
            hub.update_book(
                [
                    {
                        "symbol": "HBAR/USDT",
                        "timeframe": "1h",
                        "position": dict(lot),
                        "average_entry": AVG,
                        "recent_high": AVG,
                        "strategy_params": {
                            "stop_loss_pct": 50.0,
                            "partial_stop_pct": 33.5,
                        },
                        "atr_pct": 3.0,
                    }
                ]
            )
            hub.on_ticker("HBAR_USDT", 0.08168, highest_bid=0.08164)
    finally:
        pm.positions.pop(key, None)

    approved = [row for row in placed if row["approved"]]
    assert len(approved) == 1, placed
    assert approved[0]["signal"] == "SELL_STOP_PARTIAL"
    assert approved[0]["amount"] == pytest.approx(PEAK * 0.5)
    assert approved[0]["amount"] != pytest.approx(PEAK)
    assert approved[0]["amount"] > 0
    assert all(row["signal"] != "SELL_STOP_FULL" for row in placed), placed
    assert pm.positions.get(key, lot).get("sold_percent", lot["sold_percent"]) == 1.0 or lot["sold_percent"] == 1.0


def test_no_peak_repeated_rejections_one_partial_and_one_remainder():
    """Three cooldown windows and a rebuilt hub: one partial, one remainder."""
    _reset_once()
    lot = _no_peak_lot()
    rejected = {"ok": True, "executed": False, "message": _ALREADY_SOLD}
    clock = _Clock()
    calls: list[str] = []
    with patch("services.exit_realtime.hub.time.monotonic", clock):
        hub = _hub_cooldown()
        for _ in range(3):
            calls.extend(
                _fire_rejected_partial(
                    hub,
                    _ALREADY_SOLD,
                    lot=lot,
                    reset_once=False,
                    full_result=rejected,
                )
            )
            clock.now += 15
        rebuilt = _hub_cooldown()
        calls.extend(
            _fire_rejected_partial(
                rebuilt,
                _ALREADY_SOLD,
                lot=lot,
                reset_once=False,
                full_result=rejected,
            )
        )
    partials = [c for c in calls if c == "SELL_STOP_PARTIAL"]
    fulls = [c for c in calls if c == "SELL_STOP_FULL"]
    assert partials == ["SELL_STOP_PARTIAL"], calls
    assert fulls == ["SELL_STOP_FULL"], calls


def _locked(lot: dict) -> dict:
    from strategies.position_lock import MODE_NO_AUTO_SELL, build_lock

    out = dict(lot)
    out["lock"] = build_lock(reason="c10", modes=[MODE_NO_AUTO_SELL])
    return out


def test_locked_lot_rejection_places_zero_orders_through_try_execute():
    """Real lock, real try_execute. Already-sold and below-minimum lots place nothing."""
    from services.exit_realtime.execute import try_execute_trail_exit
    from services.trading_service import TradingService
    from strategies import positions as pm

    def _run(symbol: str, lot: dict, price: float) -> None:
        key = pm.get_key(symbol, "1h")
        pm.positions[key] = dict(lot)
        _clear_execute_guards(symbol)
        calls: list = []

        def _execute(self, order, timeframe="4h", source=None, **kwargs):
            calls.append(order)
            raise AssertionError("locked lot placed an order")

        try:
            with patch(
                "services.exit_realtime.config.exit_execute_url", return_value=""
            ), patch.object(TradingService, "execute_order", _execute):
                out = try_execute_trail_exit(
                    symbol=symbol,
                    timeframe="1h",
                    price=price,
                    action="SELL_STOP_PARTIAL",
                    exit_source="stop_loss",
                    force_local=True,
                    sell_amount=float(lot["amount"]) * 0.5,
                )
            assert out.get("executed") is False
            assert out.get("code") == "position_locked", out
            assert calls == []
            with patch(
                "services.exit_realtime.config.exit_execute_url", return_value=""
            ), patch.object(TradingService, "execute_order", _execute):
                full = try_execute_trail_exit(
                    symbol=symbol,
                    timeframe="1h",
                    price=price,
                    action="SELL_STOP_FULL",
                    exit_source="stop_loss",
                    force_local=True,
                    sell_amount=float(lot["amount"]),
                )
            assert full.get("executed") is False
            assert full.get("code") == "position_locked", full
            assert calls == []
        finally:
            pm.positions.pop(key, None)

    sold = _locked(_hbar_lot(1.0))
    _run("HBAR/USDT", sold, 0.08168)
    small = _locked(
        {
            "amount": 100.0,
            "peak_amount": 100.0,
            "sold_percent": 0.0,
            "average_entry": 1.0,
            "dca_rounds": 0,
            "strategy_tier": "stable",
            "side": "long",
            "first_buy_at": "2026-10-01T00:00:00+00:00",
        }
    )
    _run("UNI/USDT", small, 1.0)


def test_no_peak_locked_lot_places_zero_orders():
    """Same no-peak rejection on a locked lot: zero orders, a warning, no sell."""
    from services.trading_service import TradingService
    from strategies import positions as pm

    _reset_once()
    lot = _locked(_no_peak_lot())
    key = pm.get_key("HBAR/USDT", "1h")
    pm.positions[key] = dict(lot)
    _clear_execute_guards()
    placed: list = []
    warnings: list[str] = []

    def _execute(self, order, timeframe="4h", source=None, **kwargs):
        placed.append(order)
        raise AssertionError("locked lot placed an order")

    def _log(message, level="INFO"):
        if level == "WARNING":
            warnings.append(str(message))

    events = [
        {
            "source": "stop_loss",
            "action": "SELL_STOP_PARTIAL",
            "priority": 8,
            "rationale": "WS hard stop SELL_STOP_PARTIAL",
            "strategy_shadow": False,
        }
    ]
    hub = _hub()
    try:
        with patch(
            "services.exit_realtime.hub.evaluate_would_sells", return_value=events
        ), patch(
            "services.exit_realtime.hub.log", side_effect=_log
        ), patch(
            "services.exit_realtime.config.exit_execute_url", return_value=""
        ), patch.object(TradingService, "execute_order", _execute), patch(
            "services.exit_realtime.hub._log_event"
        ):
            hub.update_book(
                [
                    {
                        "symbol": "HBAR/USDT",
                        "timeframe": "1h",
                        "position": dict(lot),
                        "average_entry": AVG,
                        "recent_high": AVG,
                        "strategy_params": {
                            "stop_loss_pct": 50.0,
                            "partial_stop_pct": 33.5,
                        },
                        "atr_pct": 3.0,
                    }
                ]
            )
            hub.on_ticker("HBAR_USDT", 0.08168, highest_bid=0.08164)
    finally:
        pm.positions.pop(key, None)
    assert placed == []
    assert any("no order" in line for line in warnings), warnings
