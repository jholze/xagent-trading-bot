"""Shutdown flush persists every known store, not this thread's key."""

from __future__ import annotations

import threading

import pytest

import strategies.positions as P
from core.tenant_context import DEFAULT_TENANT, tenant_context


@pytest.fixture
def isolated_books():
    P.reset_all_position_stores_for_tests()
    yield
    P.reset_all_position_stores_for_tests()


def _lot(amount: float) -> dict:
    pos = P._empty_position()
    pos["amount"] = amount
    pos["peak_amount"] = amount
    pos["last_action"] = "BUY"
    return pos


def test_shutdown_flush_writes_each_known_store(monkeypatch, isolated_books):
    from aria_bot import _flush_positions_on_exit

    saved: list[tuple[str | None, str]] = []

    monkeypatch.setattr("bus.writer_lease.lease_enabled", lambda: False)
    monkeypatch.setattr(P, "load_positions_document", lambda *_a, **_k: {"positions": {}})

    def _save(payload, target, tenant_id=None, **_k):
        saved.append((tenant_id, target))
        return True

    monkeypatch.setattr(P, "save_positions_document", _save)

    live_key = P.get_key("LAB/USDT", "1h")
    henry_key = P.get_key("LAB/USDT", "4h")
    P.positions[live_key] = _lot(1.25)
    P._mark_positions_known((DEFAULT_TENANT, "live"))
    P._store_for_key(("henry", "paper"))[henry_key] = _lot(2.5)
    P._mark_positions_known(("henry", "paper"))
    # Main thread key is paper and that book was never loaded.
    P._activate((DEFAULT_TENANT, "paper"))

    def _cycle_loads_live_book():
        with tenant_context(DEFAULT_TENANT, scope="live"):
            P._activate(P._resolve_store_key("live"))

    worker = threading.Thread(target=_cycle_loads_live_book, name="cycle-live")
    worker.start()
    worker.join(10)
    assert not worker.is_alive()

    _flush_positions_on_exit()

    assert sorted(saved) == [("default", "live"), ("henry", "paper")]


def test_flush_without_a_key_is_refused(monkeypatch, isolated_books):
    saved: list[str] = []
    monkeypatch.setattr(
        P,
        "save_positions_document",
        lambda payload, target, tenant_id=None, **_k: saved.append(target) or True,
    )
    P._active_key_var.set(None)
    P.positions[P.get_key("LAB/USDT", "1h")] = _lot(1.0)

    def _flush():
        P.flush_positions(force=True)

    worker = threading.Thread(target=_flush, name="unset-flush")
    worker.start()
    worker.join(10)
    assert not worker.is_alive()
    assert saved == []
