"""Per-(tenant, scope) ledger thread lock and snapshot-after-unlock (#304 slice 2)."""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

from bus.locks import LedgerLock
from core.models import RiskDecision, TradeOrder, TradeResult
from services.trading_service import TradingService


def _hold_until(
    tenant_id: str,
    scope: str,
    holding: threading.Event,
    release: threading.Event,
    entered: threading.Event,
    errors: list,
    *,
    enabled: bool = False,
) -> None:
    try:
        with LedgerLock(scope, tenant_id=tenant_id, enabled=enabled):
            entered.set()
            holding.set()
            if not release.wait(timeout=3.0):
                errors.append(f"{tenant_id}/{scope} release timed out")
    except BaseException as exc:
        errors.append(exc)


def _join(threads: list[threading.Thread], releases: list[threading.Event]) -> None:
    for ev in releases:
        ev.set()
    for t in threads:
        t.join(timeout=3.0)


def test_distinct_tenant_scopes_do_not_block_each_other():
    """Thread A holding (default, demo) must not block (henry, demo)."""
    a_holding = threading.Event()
    a_entered = threading.Event()
    b_entered = threading.Event()
    release_a = threading.Event()
    release_b = threading.Event()
    errors: list = []

    t_a = threading.Thread(
        target=_hold_until,
        args=("default", "demo", a_holding, release_a, a_entered, errors),
        daemon=True,
    )
    t_b = threading.Thread(
        target=_hold_until,
        args=("henry", "demo", threading.Event(), release_b, b_entered, errors),
        daemon=True,
    )
    t_a.start()
    try:
        assert a_holding.wait(timeout=2.0), "A never acquired (default, demo)"
        t_b.start()
        assert b_entered.wait(timeout=2.0), (
            "B blocked on (henry, demo) while A held (default, demo)"
        )
    finally:
        _join([t_a, t_b], [release_a, release_b])
    assert not t_a.is_alive() and not t_b.is_alive()
    assert errors == []


def test_same_tenant_scope_serializes():
    a_holding = threading.Event()
    b_waiting = threading.Event()
    b_entered = threading.Event()
    release_a = threading.Event()
    sequence: list[str] = []
    errors: list = []

    def hold_a():
        try:
            with LedgerLock("demo", tenant_id="default", enabled=False):
                sequence.append("a_enter")
                a_holding.set()
                b_waiting.wait(timeout=2.0)
                time.sleep(0.12)
                blocked = not b_entered.is_set()
                sequence.append("a_held_alone" if blocked else "a_overlapped")
                sequence.append("a_exit")
                if not release_a.wait(timeout=3.0):
                    errors.append("A release timed out")
        except BaseException as exc:
            errors.append(exc)

    def hold_b():
        try:
            if not a_holding.wait(timeout=2.0):
                errors.append("A never acquired")
                return
            b_waiting.set()
            with LedgerLock("demo", tenant_id="default", enabled=False):
                sequence.append("b_enter")
                b_entered.set()
        except BaseException as exc:
            errors.append(exc)

    t_a = threading.Thread(target=hold_a, daemon=True)
    t_b = threading.Thread(target=hold_b, daemon=True)
    t_a.start()
    t_b.start()
    try:
        assert a_holding.wait(timeout=2.0)
        assert b_waiting.wait(timeout=2.0)
        time.sleep(0.12)
        assert not b_entered.is_set(), "B entered while A still held the same key"
    finally:
        release_a.set()
        t_a.join(timeout=3.0)
        t_b.join(timeout=3.0)
    assert not t_a.is_alive() and not t_b.is_alive()
    assert errors == []
    assert "a_enter" in sequence and "b_enter" in sequence
    assert sequence.index("a_enter") < sequence.index("a_exit") < sequence.index("b_enter")
    assert "a_held_alone" in sequence


def test_enabled_false_takes_per_key_thread_lock_only():
    """enabled=False still serializes the same key and never talks to Redis."""
    with patch("bus.locks.get_redis") as mock_redis:
        a_holding = threading.Event()
        b_entered = threading.Event()
        release_a = threading.Event()
        errors: list = []

        def hold_a():
            try:
                with LedgerLock("demo", tenant_id="default", enabled=False):
                    a_holding.set()
                    if not release_a.wait(timeout=3.0):
                        errors.append("A release timed out")
            except BaseException as exc:
                errors.append(exc)

        def hold_b_same_key():
            try:
                if not a_holding.wait(timeout=2.0):
                    errors.append("A never acquired")
                    return
                with LedgerLock("demo", tenant_id="default", enabled=False):
                    b_entered.set()
            except BaseException as exc:
                errors.append(exc)

        t_a = threading.Thread(target=hold_a, daemon=True)
        t_b = threading.Thread(target=hold_b_same_key, daemon=True)
        t_a.start()
        try:
            assert a_holding.wait(timeout=2.0)
            t_b.start()
            time.sleep(0.12)
            assert not b_entered.is_set(), (
                "same-key holder B entered while enabled=False A still held"
            )
        finally:
            release_a.set()
            t_a.join(timeout=3.0)
            t_b.join(timeout=3.0)
        assert b_entered.is_set()
        assert errors == []
        mock_redis.assert_not_called()

        a_holding_2 = threading.Event()
        b_entered_2 = threading.Event()
        release_a_2 = threading.Event()
        release_b_2 = threading.Event()
        t_a2 = threading.Thread(
            target=_hold_until,
            args=("default", "demo", a_holding_2, release_a_2, threading.Event(), errors),
            daemon=True,
        )
        t_b2 = threading.Thread(
            target=_hold_until,
            args=("henry", "demo", threading.Event(), release_b_2, b_entered_2, errors),
            daemon=True,
        )
        t_a2.start()
        try:
            assert a_holding_2.wait(timeout=2.0)
            t_b2.start()
            assert b_entered_2.wait(timeout=2.0), (
                "enabled=False still used a process-wide lock across tenants"
            )
        finally:
            _join([t_a2, t_b2], [release_a_2, release_b_2])
        assert errors == []
        mock_redis.assert_not_called()


def _execute_buy(svc: TradingService, *, executed: bool, message: str = "") -> TradeResult:
    order = TradeOrder(type="BUY", symbol="XRVM/USDT", price=1.0, amount=10, usdt_amount=10)
    result = TradeResult(
        executed, "BUY", "XRVM/USDT", amount=10, price=1.0, usdt_amount=10, message=message
    )
    decision = RiskDecision(
        approved=executed,
        message=message,
        order=order,
    )
    with patch.object(svc, "can_execute", return_value=(True, "")), patch.object(
        svc.risk, "evaluate", return_value=decision
    ), patch.object(svc.adapter, "execute", return_value=result), patch(
        "services.trading_engine_runtime.should_queue_intent", return_value=False
    ), patch.object(
        svc, "_maybe_auto_short_after_sell"
    ):
        if not executed:
            with patch("services.order_service.OrderService.record_rejected"):
                return svc.execute_order(order, "4h")
        return svc.execute_order(order, "4h")


def test_positions_snapshot_sent_after_ledger_lock_exit():
    from bus.locks import LedgerLock as LL

    svc = TradingService()
    call_order: list[str] = []
    orig_exit = LL.__exit__

    def tracking_exit(self, *args, **kwargs):
        try:
            return orig_exit(self, *args, **kwargs)
        finally:
            call_order.append("lock_exit")

    def tracking_snapshot(*_a, **_k):
        call_order.append("snapshot")

    with patch.object(LL, "__exit__", tracking_exit), patch(
        "notifications.telegram_commands.position_display.send_positions_snapshot",
        tracking_snapshot,
    ):
        result = _execute_buy(svc, executed=True)

    assert result.executed
    assert "snapshot" in call_order
    assert "lock_exit" in call_order
    assert call_order.index("snapshot") > call_order.index("lock_exit")


def test_positions_snapshot_not_sent_when_order_rejected():
    svc = TradingService()
    with patch(
        "notifications.telegram_commands.position_display.send_positions_snapshot"
    ) as mock_snapshot:
        result = _execute_buy(svc, executed=False, message="risk rejected")
    assert result.executed is False
    mock_snapshot.assert_not_called()
