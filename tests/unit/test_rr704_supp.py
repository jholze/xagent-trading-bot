"""PR #704 re-review probes (Viktor F1/F2/F2b/F3, Lena P5/P6, lock-key M1).

F1: a fresh thread with no positions key and no tenant_context must raise.
F2/F2b/P6: shutdown must not write the default RAM book into another scope.
P5: a rebuild marks that scope known, so a debounced peak is flushed; a
debounce whose key is not flushed is not cancelled.
F3: peak sync uses one (tenant, scope) key.
M1: the ledger thread lock is keyed by tenant, not by scope alone.
"""

from __future__ import annotations

import copy
import threading

import pytest

import strategies.positions as P
from core.tenant_context import tenant_context
from tests.unit.test_mcp_lock_tenant_race_lockbug import (  # noqa: F401
    _lot,
    ledger,
)


def _fresh(fn):
    out = {}

    def _run():
        try:
            out["v"] = fn()
        except BaseException as exc:  # noqa: BLE001
            out["exc"] = exc

    thread = threading.Thread(target=_run)
    thread.start()
    thread.join(10)
    assert not thread.is_alive()
    return out


def _snapshot_docs(fl):
    return {k: sorted((v.get("positions") or {}).keys()) for k, v in fl.docs.items()}


WRITERS = {
    "flush_positions_force": lambda: P.flush_positions(force=True),
    "save_positions": lambda: P.save_positions(),
    "_do_save_positions_None": lambda: P._do_save_positions(None),
    "init_position": lambda: P.init_position("RRX/USDT", "4h"),
    "set_position_field": lambda: P.set_position_field("RRX/USDT", "4h", "rr", 1),
    "get_position(ensure)": lambda: P.get_position("RRX/USDT", "4h"),
    "update_position_BUY": lambda: P.update_position("RRX/USDT", "4h", "BUY", 100.0, 0.05),
    "set_position_lock": lambda: P.set_position_lock(
        "ZEC/USDT", "4h", {"enabled": True, "modes": ["no_auto_sell"]}
    ),
    "mark_time_profit_exit_done": lambda: P.mark_time_profit_exit_done("RRX/USDT", "4h"),
}


@pytest.mark.parametrize("name", list(WRITERS))
def test_F1_write_without_key_or_tenant_is_refused(ledger, name):
    """Fresh thread: no tenant_context, no positions key, no explicit scope."""
    P._active_key_var.set(None)
    before = _snapshot_docs(ledger)
    dflt_before = set(P.positions)
    out = _fresh(WRITERS[name])
    after = _snapshot_docs(ledger)
    ram_added = sorted(set(P.positions) - dflt_before)
    assert "exc" in out, (
        f"{name}: no raise; doc_changed={before != after}, default RAM keys added={ram_added}"
    )


def _live_orders_snap(orig, live_orders):
    def _snap(target, tenant_id=None, **k):
        if tenant_id == "default" and target == "live":
            return copy.deepcopy(live_orders)
        return orig(target, tenant_id=tenant_id, **k)

    return _snap


def test_F2_shutdown_after_mode_switch_does_not_cross_write_scopes(ledger, monkeypatch):
    """Paper known, then load live. Shutdown must not write the live book into paper."""
    from aria_bot import _flush_positions_on_exit

    monkeypatch.setattr("bus.writer_lease.lease_enabled", lambda: False)
    ledger.orders["default"] = {"ZEC_USDT_4h": ledger.orders["default"]["ZEC_USDT_4h"]}
    live_orders = {"LIVEONLY_USDT_4h": _lot(1.0, 10.0)}
    monkeypatch.setattr(
        "services.ledger_sync._build_positions_snapshot_from_orders",
        _live_orders_snap(ledger.orders_snap, live_orders),
    )
    with tenant_context("default", scope="live"):
        P.load_positions(scope="live", tenant_id="default")
    ledger.docs.clear()
    _flush_positions_on_exit()
    docs = _snapshot_docs(ledger)
    assert "LIVEONLY_USDT_4h" not in docs.get(("default", "paper"), []), (
        f"live lot written into default/paper doc at shutdown: {docs}"
    )


def test_F2b_shutdown_after_rebuild_switch(ledger, monkeypatch):
    """Mode switch via rebuild must mark live known and must not cross-write paper."""
    from aria_bot import _flush_positions_on_exit
    from services.ledger_sync import rebuild_positions_from_orders

    monkeypatch.setattr("bus.writer_lease.lease_enabled", lambda: False)
    monkeypatch.setattr(
        "services.ledger_sync._writer_lease_blocks_position_rebuild", lambda: False
    )
    live_orders = {"LIVEONLY_USDT_4h": _lot(1.0, 10.0)}
    monkeypatch.setattr(
        "services.ledger_sync._build_positions_snapshot_from_orders",
        _live_orders_snap(ledger.orders_snap, live_orders),
    )
    monkeypatch.setattr("data_manager.load_orders", lambda *a, **k: {"orders": []})
    monkeypatch.setattr("data_manager.load_positions_document", ledger.load_doc)
    rebuild_positions_from_orders("live", tenant_id="default")
    known = sorted(k for k, v in P._positions_state.items() if v == "known")
    assert ("default", "live") in known, f"rebuild left live unknown: {known}"
    assert ("default", "paper") not in known, f"paper stayed known after the switch: {known}"
    ledger.docs.clear()
    _flush_positions_on_exit()
    docs = _snapshot_docs(ledger)
    assert "LIVEONLY_USDT_4h" not in docs.get(("default", "paper"), []), (
        f"live lot written into default/paper doc at shutdown: {docs}"
    )
    assert "LIVEONLY_USDT_4h" in docs.get(("default", "live"), []), (
        f"live book was not flushed: {docs}"
    )


def test_P5_shutdown_persists_debounced_peak_after_rebuild(ledger, monkeypatch):
    """Startup rebuild marks default known. A debounced peak survives shutdown."""
    from aria_bot import _flush_positions_on_exit
    from services.ledger_sync import rebuild_positions_from_orders

    monkeypatch.setattr("bus.writer_lease.lease_enabled", lambda: False)
    monkeypatch.setattr(
        "services.ledger_sync._writer_lease_blocks_position_rebuild", lambda: False
    )
    monkeypatch.setattr("data_manager.load_orders", lambda *a, **k: {"orders": []})
    monkeypatch.setattr("data_manager.load_positions_document", ledger.load_doc)
    # Production never load_positions() for default; the fixture did. Drop that
    # flag so only the rebuild can mark the book known.
    P._positions_state.clear()
    rebuild_positions_from_orders("paper", tenant_id="default")
    assert P._positions_state.get(("default", "paper")) == "known"

    def _cycle():
        with tenant_context("default", scope="paper"):
            P.update_market_snapshot("ZEC/USDT", "4h", 99999.0)

    out = _fresh(_cycle)
    assert "exc" not in out, out.get("exc")
    ledger.docs.clear()
    _flush_positions_on_exit()
    row = (ledger.docs.get(("default", "paper"), {}).get("positions") or {}).get("ZEC_USDT_4h") or {}
    assert float(row.get("recent_high") or 0) == 99999.0, row


def test_P5_unflushed_debounce_timer_is_not_cancelled(ledger, monkeypatch):
    """Shutdown must not cancel a debounce whose book it does not flush."""
    from aria_bot import _flush_positions_on_exit

    monkeypatch.setattr("bus.writer_lease.lease_enabled", lambda: False)
    monkeypatch.setattr(P, "_FLUSH_DEBOUNCE_SEC", 60.0)

    def _arm():
        with tenant_context("henry", scope="paper"):
            P.flush_positions(scope="paper")

    out = _fresh(_arm)
    assert "exc" not in out, out.get("exc")
    timer = P._flush_timer
    assert timer is not None
    _flush_positions_on_exit()
    assert P._flush_timer is timer, "shutdown cancelled a debounce it did not flush"
    assert getattr(P, "_pending_flush_key", None) == ("henry", "paper")


def test_P6_mode_switch_does_not_rewrite_previous_default_scope(ledger, monkeypatch):
    """Paper doc keeps its own row after a live rebuild and shutdown."""
    from aria_bot import _flush_positions_on_exit
    from services.ledger_sync import rebuild_positions_from_orders

    monkeypatch.setattr("bus.writer_lease.lease_enabled", lambda: False)
    monkeypatch.setattr(
        "services.ledger_sync._writer_lease_blocks_position_rebuild", lambda: False
    )
    ledger.docs[("default", "paper")] = {
        "positions": {
            "LAB_USDT_1h": _lot(1.0, 2.0),
            "ZEC_USDT_4h": _lot(1.5945, 1534.20),
        }
    }
    with tenant_context("default", scope="paper"):
        P.load_positions(scope="paper", tenant_id="default")
    monkeypatch.setattr("data_manager.load_orders", lambda *a, **k: {"orders": []})
    monkeypatch.setattr("data_manager.load_positions_document", ledger.load_doc)
    rebuild_positions_from_orders("live", tenant_id="default")
    _flush_positions_on_exit()
    paper_keys = sorted(
        (ledger.docs.get(("default", "paper"), {}).get("positions") or {}).keys()
    )
    assert "LAB_USDT_1h" in paper_keys, paper_keys
    assert paper_keys != ["ZEC_USDT_4h"], paper_keys


def test_F3_peak_sync_uses_one_tenant_key(ledger, monkeypatch):
    """Thread key names default; tenant_context names henry. Both reads use henry."""
    from services.ledger_sync import sync_ledger_files_peak_amounts

    seen = []
    orig = ledger.orders_snap

    def _snap(target, tenant_id=None, **k):
        seen.append(tenant_id)
        return orig(target, tenant_id=tenant_id, **k)

    monkeypatch.setattr("services.ledger_sync._build_positions_snapshot_from_orders", _snap)
    P._store_for_key(("default", "paper"))["ZEC_USDT_4h"]["peak_amount"] = 999.0
    P._activate(("default", "paper"))
    saved = []
    real = ledger.save_doc
    monkeypatch.setattr(
        P,
        "save_positions_document",
        lambda payload, target, tenant_id=None, **k: saved.append(tenant_id)
        or real(payload, target, tenant_id=tenant_id),
    )
    with tenant_context("henry", scope="paper"):
        sync_ledger_files_peak_amounts("paper")
    assert seen == ["henry"] and "default" not in saved, (
        f"peak sync mixed tenants seen={seen} saved={saved}"
    )


def test_M1_ledger_lock_key_includes_tenant():
    """Holding (default, scope) must not block (henry, scope).

    A lock-cache key of ('*', scope) makes this time out.
    """
    from bus.locks import LedgerLock

    scope = "m1scope"
    a_holding = threading.Event()
    b_entered = threading.Event()
    release_a = threading.Event()
    errors: list = []

    def _hold(tenant_id, holding, entered, release):
        try:
            with LedgerLock(scope, tenant_id=tenant_id, enabled=False):
                entered.set()
                if holding is not None:
                    holding.set()
                if not release.wait(timeout=3.0):
                    errors.append(f"{tenant_id} release timed out")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    release_b = threading.Event()
    t_a = threading.Thread(
        target=_hold, args=("default", a_holding, threading.Event(), release_a), daemon=True
    )
    t_b = threading.Thread(
        target=_hold, args=("henry", None, b_entered, release_b), daemon=True
    )
    t_a.start()
    try:
        assert a_holding.wait(timeout=2.0), "A never acquired (default, m1scope)"
        t_b.start()
        assert b_entered.wait(timeout=2.0), (
            "B blocked on (henry, m1scope) while A held (default, m1scope)"
        )
    finally:
        release_a.set()
        release_b.set()
        t_a.join(3)
        t_b.join(3)
    assert errors == []
