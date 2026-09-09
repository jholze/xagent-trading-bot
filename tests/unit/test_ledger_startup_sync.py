"""#338: ledger startup sync waits for the writer lease; #339 import-guard helper."""

from __future__ import annotations

import threading
import time
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import services.architecture_runtime as rt


@pytest.fixture
def aria_mod():
    import aria_bot

    aria_bot.reset_ledger_startup_sync_for_tests()
    rt.reset_on_lease_acquired_extras_for_tests()
    yield aria_bot
    aria_bot.reset_ledger_startup_sync_for_tests()
    rt.reset_on_lease_acquired_extras_for_tests()


def _enter_sync_mocks(stack: ExitStack):
    rebuild = stack.enter_context(
        patch("services.ledger_sync.rebuild_positions_from_orders", return_value=0)
    )
    sync = stack.enter_context(patch("services.ledger_sync.sync_positions_on_startup"))
    recon = stack.enter_context(
        patch("data_manager.reconcile_demo_trade_history_on_startup")
    )
    flush = stack.enter_context(patch("strategies.positions.flush_positions"))
    stack.enter_context(patch("data_manager.resolve_ledger_scope", return_value="demo"))
    stack.enter_context(
        patch("core.tenant_context.multi_tenant_enabled", return_value=False)
    )
    return rebuild, sync, recon, flush


def test_standby_defers_sync_no_writes_no_traceback(aria_mod):
    logged: list[tuple[str, str]] = []

    def capture(msg, level="INFO"):
        logged.append((level, str(msg)))

    with ExitStack() as stack:
        rebuild, sync, recon, flush = _enter_sync_mocks(stack)
        stack.enter_context(patch.object(aria_mod, "log", side_effect=capture))
        stack.enter_context(patch("bus.writer_lease.lease_enabled", return_value=True))
        stack.enter_context(patch("bus.writer_lease.writer_lease_held", return_value=False))
        aria_mod._schedule_ledger_startup_sync()

    assert rebuild.call_count == 0
    assert sync.call_count == 0
    assert recon.call_count == 0
    assert flush.call_count == 0
    assert any(
        level == "INFO" and "ledger startup sync deferred: writer lease not held" in msg
        for level, msg in logged
    )
    assert not any("Traceback" in msg for _level, msg in logged)
    assert not any(level in ("ERROR", "WARNING") for level, _msg in logged)
    assert aria_mod._run_ledger_startup_sync in rt._on_lease_acquired_extras


def test_lease_disabled_runs_sync_exactly_once(aria_mod):
    with ExitStack() as stack:
        rebuild, sync, recon, flush = _enter_sync_mocks(stack)
        stack.enter_context(patch("bus.writer_lease.lease_enabled", return_value=False))
        stack.enter_context(patch("bus.writer_lease.writer_lease_held", return_value=False))
        aria_mod._schedule_ledger_startup_sync()
        aria_mod._schedule_ledger_startup_sync()
        aria_mod._run_ledger_startup_sync()

    assert rebuild.call_count == 1
    assert sync.call_count == 1
    assert recon.call_count == 1
    assert flush.call_count == 1
    flush.assert_called_with(scope="demo", force=True)
    # Always composed into on_acquired so a later ensure_started waits
    # on the once-guard instead of racing recovery.
    assert aria_mod._run_ledger_startup_sync in rt._on_lease_acquired_extras


def test_lease_already_held_runs_sync_exactly_once(aria_mod):
    rec: list[str] = []
    with ExitStack() as stack:
        rebuild, sync, recon, flush = _enter_sync_mocks(stack)
        stack.enter_context(patch("bus.writer_lease.lease_enabled", return_value=True))
        stack.enter_context(patch("bus.writer_lease.writer_lease_held", return_value=True))
        stack.enter_context(
            patch.object(rt, "_ensure_tenant_exchange_recovery", lambda: rec.append("rec"))
        )
        aria_mod._schedule_ledger_startup_sync()
        rt._on_writer_lease_acquired()
        aria_mod._run_ledger_startup_sync()

    assert rebuild.call_count == 1
    assert sync.call_count == 1
    assert recon.call_count == 1
    assert flush.call_count == 1
    assert rec == ["rec"]


def test_deferred_sync_runs_once_via_on_acquired_callback(aria_mod):
    rec: list[str] = []
    with ExitStack() as stack:
        rebuild, sync, recon, flush = _enter_sync_mocks(stack)
        stack.enter_context(patch("bus.writer_lease.lease_enabled", return_value=True))
        stack.enter_context(patch("bus.writer_lease.writer_lease_held", return_value=False))
        stack.enter_context(
            patch.object(rt, "_ensure_tenant_exchange_recovery", lambda: rec.append("rec"))
        )
        aria_mod._schedule_ledger_startup_sync()
        assert rebuild.call_count == 0
        rt._on_writer_lease_acquired()
        rt._on_writer_lease_acquired()

    assert rebuild.call_count == 1
    assert sync.call_count == 1
    assert recon.call_count == 1
    assert flush.call_count == 1
    assert rec == ["rec", "rec"]


def test_composed_on_acquired_runs_sync_then_recovery_and_does_not_clobber(aria_mod):
    """ensure_started must register the composed wrapper, not recovery alone (#338)."""
    import bus.writer_lease as wl
    from core.config import get_bot_config

    order: list[str] = []

    def extra():
        order.append("sync")

    def recovery():
        order.append("recovery")

    rt.reset_recovery_state_for_tests()
    rt.reset_on_lease_acquired_extras_for_tests()
    rt._started = True
    rt._last_mode = get_bot_config().architecture_config.get("notification_mode", "async")
    rt.register_on_lease_acquired(extra)
    with patch.object(rt, "_ensure_tenant_exchange_recovery", side_effect=recovery), \
         patch.object(rt, "_heartbeat_tick", lambda cfg: None), \
         patch.object(rt, "_maybe_warn_stale", lambda cfg: None):
        rt.ensure_started()

    assert wl._on_acquired is rt._on_writer_lease_acquired
    assert order == ["sync", "recovery"]
    rt.reset_on_lease_acquired_extras_for_tests()


def test_extra_failure_is_logged_and_recovery_still_runs():
    rec: list[str] = []
    logged: list[tuple[str, str]] = []

    def boom():
        raise RuntimeError("sync failed")

    def capture(msg, level="INFO"):
        logged.append((level, str(msg)))

    rt.reset_on_lease_acquired_extras_for_tests()
    rt.register_on_lease_acquired(boom)
    with patch.object(rt, "_ensure_tenant_exchange_recovery", lambda: rec.append("rec")), \
         patch.object(rt, "log", side_effect=capture):
        rt._on_writer_lease_acquired()

    assert rec == ["rec"]
    assert any(
        level == "WARNING" and "sync failed" in msg for level, msg in logged
    )
    rt.reset_on_lease_acquired_extras_for_tests()


def test_concurrent_on_acquired_recovery_waits_for_in_flight_sync(aria_mod):
    """Two concurrent on_acquired calls must not recover until rebuild returns."""
    order: list[str] = []
    order_lock = threading.Lock()
    rebuild_started = threading.Event()
    allow_rebuild_finish = threading.Event()

    def slow_rebuild(*_a, **_k):
        with order_lock:
            order.append("rebuild_start")
        rebuild_started.set()
        assert allow_rebuild_finish.wait(timeout=5)
        with order_lock:
            order.append("rebuild_end")
        return 0

    def recovery():
        with order_lock:
            order.append("recovery")

    with ExitStack() as stack:
        stack.enter_context(
            patch("services.ledger_sync.rebuild_positions_from_orders", side_effect=slow_rebuild)
        )
        stack.enter_context(patch("services.ledger_sync.sync_positions_on_startup"))
        stack.enter_context(patch("data_manager.reconcile_demo_trade_history_on_startup"))
        stack.enter_context(patch("strategies.positions.flush_positions"))
        stack.enter_context(patch("data_manager.resolve_ledger_scope", return_value="demo"))
        stack.enter_context(
            patch("core.tenant_context.multi_tenant_enabled", return_value=False)
        )
        stack.enter_context(patch("bus.writer_lease.lease_enabled", return_value=True))
        stack.enter_context(patch("bus.writer_lease.writer_lease_held", return_value=False))
        stack.enter_context(
            patch.object(rt, "_ensure_tenant_exchange_recovery", recovery)
        )
        aria_mod._schedule_ledger_startup_sync()

        t1 = threading.Thread(target=rt._on_writer_lease_acquired, name="acq-1")
        t2 = threading.Thread(target=rt._on_writer_lease_acquired, name="acq-2")
        t1.start()
        assert rebuild_started.wait(timeout=5)
        t2.start()
        time.sleep(0.2)
        with order_lock:
            snapshot = list(order)
        assert "recovery" not in snapshot, snapshot
        allow_rebuild_finish.set()
        t1.join(timeout=5)
        t2.join(timeout=5)

    assert not t1.is_alive() and not t2.is_alive()
    end_idx = order.index("rebuild_end")
    rec_indices = [i for i, x in enumerate(order) if x == "recovery"]
    assert rec_indices, order
    assert all(i > end_idx for i in rec_indices), order
    assert order.count("rebuild_start") == 1
    assert order.count("rebuild_end") == 1


def test_immediate_sync_blocks_concurrent_on_acquired_recovery(aria_mod):
    """Import-time (lease already held) rebuild vs on_acquired must not overlap recovery."""
    order: list[str] = []
    order_lock = threading.Lock()
    rebuild_started = threading.Event()
    allow_rebuild_finish = threading.Event()

    def slow_rebuild(*_a, **_k):
        with order_lock:
            order.append("rebuild_start")
        rebuild_started.set()
        assert allow_rebuild_finish.wait(timeout=5)
        with order_lock:
            order.append("rebuild_end")
        return 0

    def recovery():
        with order_lock:
            order.append("recovery")

    with ExitStack() as stack:
        stack.enter_context(
            patch("services.ledger_sync.rebuild_positions_from_orders", side_effect=slow_rebuild)
        )
        stack.enter_context(patch("services.ledger_sync.sync_positions_on_startup"))
        stack.enter_context(patch("data_manager.reconcile_demo_trade_history_on_startup"))
        stack.enter_context(patch("strategies.positions.flush_positions"))
        stack.enter_context(patch("data_manager.resolve_ledger_scope", return_value="demo"))
        stack.enter_context(
            patch("core.tenant_context.multi_tenant_enabled", return_value=False)
        )
        stack.enter_context(patch("bus.writer_lease.lease_enabled", return_value=True))
        stack.enter_context(patch("bus.writer_lease.writer_lease_held", return_value=True))
        stack.enter_context(
            patch.object(rt, "_ensure_tenant_exchange_recovery", recovery)
        )

        t_sync = threading.Thread(
            target=aria_mod._schedule_ledger_startup_sync, name="import-sync"
        )
        t_acq = threading.Thread(target=rt._on_writer_lease_acquired, name="acq")
        t_sync.start()
        assert rebuild_started.wait(timeout=5)
        t_acq.start()
        time.sleep(0.2)
        with order_lock:
            snapshot = list(order)
        assert "recovery" not in snapshot, snapshot
        allow_rebuild_finish.set()
        t_sync.join(timeout=5)
        t_acq.join(timeout=5)

    assert not t_sync.is_alive() and not t_acq.is_alive()
    end_idx = order.index("rebuild_end")
    rec_indices = [i for i, x in enumerate(order) if x == "recovery"]
    assert rec_indices, order
    assert all(i > end_idx for i in rec_indices), order


def test_failed_sync_is_retried_by_later_caller(aria_mod):
    calls = {"n": 0}

    def flaky_rebuild(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("mongo down")
        return 0

    logged: list[tuple[str, str]] = []

    def capture(msg, level="INFO"):
        logged.append((level, str(msg)))

    with ExitStack() as stack:
        stack.enter_context(
            patch("services.ledger_sync.rebuild_positions_from_orders", side_effect=flaky_rebuild)
        )
        recon = stack.enter_context(
            patch("services.ledger_sync.sync_positions_on_startup")
        )
        stack.enter_context(patch("data_manager.reconcile_demo_trade_history_on_startup"))
        stack.enter_context(patch("strategies.positions.flush_positions"))
        stack.enter_context(patch("data_manager.resolve_ledger_scope", return_value="demo"))
        stack.enter_context(
            patch("core.tenant_context.multi_tenant_enabled", return_value=False)
        )
        stack.enter_context(patch.object(aria_mod, "log", side_effect=capture))
        aria_mod._run_ledger_startup_sync()
        assert recon.call_count == 0
        aria_mod._run_ledger_startup_sync()

    assert calls["n"] == 2
    assert recon.call_count == 1
    assert any(
        level == "WARNING" and "mongo down" in msg for level, msg in logged
    )


def test_scheduling_failure_uses_scheduling_error_message(aria_mod):
    logged: list[tuple[str, str]] = []

    def capture(msg, level="INFO"):
        logged.append((level, str(msg)))

    with ExitStack() as stack:
        rebuild, sync, recon, flush = _enter_sync_mocks(stack)
        stack.enter_context(patch.object(aria_mod, "log", side_effect=capture))
        stack.enter_context(
            patch(
                "services.architecture_runtime.register_on_lease_acquired",
                side_effect=RuntimeError("extras exploded"),
            )
        )
        stack.enter_context(patch("bus.writer_lease.lease_enabled", return_value=True))
        stack.enter_context(patch("bus.writer_lease.writer_lease_held", return_value=True))
        aria_mod._schedule_ledger_startup_sync()

    assert any(
        level == "WARNING" and "Ledger startup sync scheduling failed" in msg
        for level, msg in logged
    )
    assert not any(
        "Ledger position sync on startup failed" in msg and "extras exploded" in msg
        for _level, msg in logged
    )
    # Lease already held: still attempt the body after the scheduling error.
    assert rebuild.call_count == 1
    assert flush.call_count == 1


def test_duplicate_import_detected_when_main_is_bootstrapped(aria_mod):
    stub = SimpleNamespace(_ARIA_BOT_BOOTSTRAPPED=True, __file__=aria_mod.__file__)
    with patch.dict("sys.modules", {"__main__": stub}):
        assert aria_mod._is_duplicate_aria_bot_import() is True


def test_import_under_pytest_is_not_treated_as_duplicate(aria_mod):
    assert aria_mod._DUPLICATE_ARIA_BOT_IMPORT is False
    assert aria_mod._is_duplicate_aria_bot_import() is False
