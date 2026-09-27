"""#584: startup rebuild must not reopen stored-flat lots or write without the lease."""

from __future__ import annotations

import importlib
from unittest.mock import patch

from storage.errors import LedgerWriteFailed
from strategies.positions import derive_positions_from_orders_and_cache, is_open_position


def _open_lot(amount: float, **extra) -> dict:
    pos = {
        "amount": amount,
        "peak_amount": amount,
        "average_entry": 1.0,
        "sold_percent": 0.0,
    }
    pos.update(extra)
    return pos


def _flat_lot(**extra) -> dict:
    pos = {
        "amount": 0.0,
        "peak_amount": 50.0,
        "average_entry": 0.01,
        "sold_percent": 1.0,
    }
    pos.update(extra)
    return pos


def _patch_lease(monkeypatch, *, enabled: bool, held: bool) -> None:
    monkeypatch.setattr("bus.writer_lease.lease_enabled", lambda: enabled)
    monkeypatch.setattr("bus.writer_lease.writer_lease_held", lambda: held)


def test_cached_flat_key_stays_flat_when_replay_positive():
    order_snap = {"PUMP_USDT_1h": _open_lot(80.0)}
    cache_doc = {"positions": {"PUMP_USDT_1h": _flat_lot()}}
    with patch("core.simulated_trading.uses_order_ledger_cash", return_value=True), patch(
        "data_manager.get_config", return_value={"live": {"dry_run": True}}
    ):
        merged = derive_positions_from_orders_and_cache(order_snap, cache_doc)
    assert "PUMP_USDT_1h" in merged
    assert float(merged["PUMP_USDT_1h"]["amount"]) == 0.0
    assert is_open_position(merged["PUMP_USDT_1h"]) is False


def test_already_open_cache_key_follows_replay_amount():
    order_snap = {"OPEN_USDT_1h": _open_lot(99.0)}
    cache_doc = {"positions": {"OPEN_USDT_1h": _open_lot(20.0)}}
    with patch("core.simulated_trading.uses_order_ledger_cash", return_value=True), patch(
        "data_manager.get_config", return_value={"live": {"dry_run": True}}
    ):
        merged = derive_positions_from_orders_and_cache(order_snap, cache_doc)
    assert float(merged["OPEN_USDT_1h"]["amount"]) == 99.0
    assert is_open_position(merged["OPEN_USDT_1h"]) is True


def test_replay_only_symbol_is_created():
    order_snap = {"NEW_USDT_1h": _open_lot(10.0)}
    cache_doc = {"positions": {}}
    with patch("core.simulated_trading.uses_order_ledger_cash", return_value=True), patch(
        "data_manager.get_config", return_value={"live": {"dry_run": True}}
    ):
        merged = derive_positions_from_orders_and_cache(order_snap, cache_doc)
    assert "NEW_USDT_1h" in merged
    assert float(merged["NEW_USDT_1h"]["amount"]) == 10.0
    assert is_open_position(merged["NEW_USDT_1h"]) is True


def test_rebuild_without_lease_does_not_save_or_raise(monkeypatch):
    from services.ledger_sync import rebuild_positions_from_orders

    saved: list = []
    flushed: list = []
    logged: list[tuple[str, str]] = []

    def boom_save(*_a, **_k):
        saved.append(1)
        raise LedgerWriteFailed("no writer lease", op="writer_lease")

    def boom_flush(*_a, **_k):
        flushed.append(1)
        raise LedgerWriteFailed("no writer lease", op="writer_lease")

    _patch_lease(monkeypatch, enabled=True, held=False)
    monkeypatch.setattr("data_manager.save_positions_document", boom_save)
    monkeypatch.setattr("strategies.positions.flush_positions", boom_flush)
    monkeypatch.setattr(
        "services.ledger_sync.log",
        lambda msg, level="INFO": logged.append((level, str(msg))),
    )
    monkeypatch.setattr(
        "data_manager.load_orders",
        lambda *_a, **_k: {
            "ledger_scope": "demo",
            "orders": [{"id": "1", "status": "filled"}],
        },
    )
    monkeypatch.setattr(
        "data_manager.load_positions_document",
        lambda *_a, **_k: {
            "ledger_scope": "demo",
            "positions": {
                "PUMP_USDT_1h": _flat_lot(),
                "FIL_USDT_1h": _flat_lot(),
            },
        },
    )
    monkeypatch.setattr(
        "services.ledger_sync._build_positions_snapshot_from_orders",
        lambda *_a, **_k: {"PUMP_USDT_1h": _open_lot(80.0)},
    )

    rebuild_positions_from_orders("demo")

    assert saved == []
    assert flushed == []
    assert any(
        level == "INFO" and "deferred" in msg and "writer lease" in msg
        for level, msg in logged
    )


def test_rebuild_with_lease_held_keeps_flat_and_saves_real_change(monkeypatch):
    from services.ledger_sync import rebuild_positions_from_orders

    saved: list[dict] = []
    flushed: list = []
    applied: dict = {}

    def capture_save(data, *_a, **_k):
        saved.append(data)
        return True

    def capture_apply(snapshot, scope=None):
        applied.clear()
        applied.update(snapshot)

    _patch_lease(monkeypatch, enabled=True, held=True)
    monkeypatch.setattr("data_manager.save_positions_document", capture_save)
    monkeypatch.setattr("strategies.positions.flush_positions", lambda *_a, **_k: flushed.append(1))
    monkeypatch.setattr("strategies.positions.apply_positions_snapshot", capture_apply)
    monkeypatch.setattr(
        "data_manager.load_orders",
        lambda *_a, **_k: {
            "ledger_scope": "demo",
            "orders": [{"id": "1", "status": "filled"}],
        },
    )
    monkeypatch.setattr(
        "data_manager.load_positions_document",
        lambda *_a, **_k: {
            "ledger_scope": "demo",
            "positions": {
                "PUMP_USDT_1h": _flat_lot(),
                "OPEN_USDT_1h": _open_lot(20.0),
                "FIL_USDT_1h": _flat_lot(),
            },
        },
    )
    monkeypatch.setattr(
        "services.ledger_sync._build_positions_snapshot_from_orders",
        lambda *_a, **_k: {
            "PUMP_USDT_1h": _open_lot(80.0),
            "OPEN_USDT_1h": _open_lot(99.0),
            "NEW_USDT_1h": _open_lot(10.0),
        },
    )
    monkeypatch.setattr("core.simulated_trading.uses_order_ledger_cash", lambda cfg=None: True)

    rebuild_positions_from_orders("demo")

    assert float(applied["PUMP_USDT_1h"]["amount"]) == 0.0
    assert is_open_position(applied["PUMP_USDT_1h"]) is False
    assert float(applied["OPEN_USDT_1h"]["amount"]) == 99.0
    assert "NEW_USDT_1h" in applied
    assert float(applied["NEW_USDT_1h"]["amount"]) == 10.0
    assert "FIL_USDT_1h" not in applied
    assert saved, "FIL-style orphan prune must still save when the lease is held"
    assert "FIL_USDT_1h" not in (saved[0].get("positions") or {})
    assert "PUMP_USDT_1h" in (saved[0].get("positions") or {})
    assert flushed, "a real rebuild must still flush when the lease is held"


def test_sidecar_sync_uses_rebuild_lease_gate(monkeypatch):
    sidecar = importlib.import_module("services.exit_radar.sidecar.__main__")
    rebuild_calls: list = []
    sync_calls: list = []

    monkeypatch.setattr("storage.mongo_client.ping_database", lambda: True)
    monkeypatch.setattr("storage.mongo_client.resolve_database_name", lambda: "xagent_pytest")
    monkeypatch.setattr("data_manager.resolve_ledger_scope", lambda: "demo")
    monkeypatch.setattr(
        "services.ledger_sync.rebuild_positions_from_orders",
        lambda *_a, **_k: rebuild_calls.append("rebuild") or 0,
    )
    monkeypatch.setattr(
        "services.ledger_sync.sync_positions_on_startup",
        lambda *_a, **_k: sync_calls.append("sync"),
    )
    _patch_lease(monkeypatch, enabled=True, held=False)

    sidecar._sync_ledger()

    assert rebuild_calls == ["rebuild"]
    assert sync_calls == []
