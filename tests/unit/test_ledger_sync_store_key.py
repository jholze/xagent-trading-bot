"""Legacy cache merge must use one (tenant, scope) key for the document and the store."""

from __future__ import annotations

import pytest

import strategies.positions as P
from core.tenant_context import DEFAULT_TENANT, tenant_context


@pytest.fixture
def isolated_books():
    P.reset_all_position_stores_for_tests()
    yield
    P.reset_all_position_stores_for_tests()


def test_preserve_legacy_cache_lots_follows_context_tenant_and_scope(monkeypatch, isolated_books):
    from services.ledger_sync import _preserve_legacy_cache_lots

    other = "satellite"
    scope = "paper"
    pos_key = P.get_key("LAB/USDT", "1h")
    seen: list[tuple[str, str | None]] = []

    def _load(target, tenant_id=None, **_k):
        seen.append((target, tenant_id))
        return {"positions": {pos_key: {"amount": 3.5, "average_entry": 2.0}}}

    monkeypatch.setattr("data_manager.load_positions_document", _load)
    monkeypatch.setattr(
        "services.ledger_sync._build_positions_snapshot_from_orders",
        lambda *_a, **_k: {},
    )

    # Thread key names the default book. The caller's context names another tenant.
    P._activate((DEFAULT_TENANT, "demo"))
    with tenant_context(other, scope=scope):
        _preserve_legacy_cache_lots(scope)

    assert seen == [(scope, other)]
    assert pos_key in P._store_for_key((other, scope))
    assert pos_key not in P.positions
