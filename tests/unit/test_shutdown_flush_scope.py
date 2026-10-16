"""Shutdown flush must use the configured ledger scope, not the main thread key.

A cycle thread can load the default book for the live scope and leave that book
in the shared default store. The main thread's key stays on the context default
(paper). Flushing ``get_active_scope()`` then writes the live book into the
paper document.
"""

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


def test_shutdown_flush_uses_configured_scope_not_main_thread_key(monkeypatch, isolated_books):
    import data_manager
    from aria_bot import _flush_positions_on_exit

    ledger_scope = "live"
    saved: list[tuple[str | None, str, dict]] = []

    monkeypatch.setattr(data_manager, "resolve_ledger_scope", lambda *_a, **_k: ledger_scope)
    monkeypatch.setattr("bus.writer_lease.lease_enabled", lambda: False)
    monkeypatch.setattr(P, "load_positions_document", lambda *_a, **_k: {"positions": {}})

    def _save(payload, target, tenant_id=None, **_k):
        saved.append((tenant_id, target, payload))
        return True

    monkeypatch.setattr(P, "save_positions_document", _save)

    pos_key = P.get_key("LAB/USDT", "1h")
    lot = P._empty_position()
    lot["amount"] = 1.25
    lot["peak_amount"] = 1.25
    lot["last_action"] = "BUY"

    # Main thread keeps the context default. A cycle thread loads the live book.
    P._activate((DEFAULT_TENANT, "paper"))

    def _cycle_loads_live_book():
        with tenant_context(DEFAULT_TENANT, scope=ledger_scope):
            P._activate(P._resolve_store_key(ledger_scope))
            P._active_store()[pos_key] = lot

    worker = threading.Thread(target=_cycle_loads_live_book, name="cycle-live")
    worker.start()
    worker.join(10)
    assert not worker.is_alive()

    _flush_positions_on_exit()

    expected = data_manager.resolve_ledger_scope()
    assert saved, "shutdown did not flush"
    tenants = {row[0] for row in saved}
    scopes = [row[1] for row in saved]
    assert tenants == {DEFAULT_TENANT}
    assert scopes == [expected], (
        f"shutdown wrote the default book to {scopes}, configured scope is {expected}"
    )
    assert pos_key in saved[0][2].get("positions", {})
