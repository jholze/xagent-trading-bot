"""Ledger I/O errors (#318): failed reads/writes must not look like empty state."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.models import RiskDecision, TradeOrder
from storage.errors import LedgerUnavailable, LedgerWriteFailed


@pytest.fixture(autouse=True)
def _reset_ledger_io_episode_state():
    from services import trading_service as ts
    from strategies import positions as pos

    pos._positions_state.clear()
    pos._positions_unknown_logged.clear()
    pos._flush_unknown_logged.clear()
    ts._ledger_unavailable_notified.clear()
    yield
    pos._positions_state.clear()
    pos._positions_unknown_logged.clear()
    pos._flush_unknown_logged.clear()
    ts._ledger_unavailable_notified.clear()


def _raise_unavailable(op="load_orders"):
    raise LedgerUnavailable(op=op)


def _raise_write_failed(op="save_orders"):
    raise LedgerWriteFailed(op=op)


# --- 8. Negative test against over-correction (written first) ---


def test_legitimate_empty_orders_no_exception(monkeypatch):
    """New tenant, mongo reachable, no orders → empty container, no exception."""

    class EmptyStore:
        def load_orders(self, scope, tenant_id=None):
            return {
                "ledger_scope": scope,
                "orders": [],
                "migrated_from_trades": False,
                "tenant_id": tenant_id,
            }

    monkeypatch.setattr("data_manager._mongo_ledger_store", lambda *a, **k: EmptyStore())
    monkeypatch.setattr("data_manager._ledger_reads_mongo_orders", lambda *a, **k: True)

    from data_manager import load_orders

    doc = load_orders("demo")
    assert doc["orders"] == []


def test_failed_positions_load_keeps_ram_and_refuses_flush(monkeypatch):
    from strategies.positions import (
        apply_positions_snapshot,
        clear_positions_memory,
        flush_positions,
        is_positions_state_unknown,
        load_positions,
        positions,
    )

    scope = "demo"
    clear_positions_memory(scope=scope)
    apply_positions_snapshot(
        {
            "BTC_USDT_4h": {
                "amount": 1.0,
                "average_entry": 50_000.0,
                "sold_percent": 0.0,
                "peak_amount": 1.0,
            }
        },
        scope=scope,
    )
    assert "BTC_USDT_4h" in positions

    monkeypatch.setattr(
        "services.ledger_sync._build_positions_snapshot_from_orders",
        lambda *a, **k: {"BTC_USDT_4h": {"amount": 1.0, "average_entry": 50_000.0}},
    )
    monkeypatch.setattr(
        "strategies.positions.load_positions_document",
        lambda *a, **k: _raise_unavailable("load_positions_document"),
    )

    load_positions(scope=scope)
    assert "BTC_USDT_4h" in positions
    assert float(positions["BTC_USDT_4h"]["amount"]) == 1.0
    assert is_positions_state_unknown(scope=scope) is True

    replace_one = MagicMock()
    with patch("strategies.positions.save_positions_document") as save_doc, patch(
        "pymongo.collection.Collection.replace_one", replace_one
    ):
        flush_positions(scope, force=True)
        save_doc.assert_not_called()
        replace_one.assert_not_called()


def test_execute_order_ledger_unavailable_buy_and_sell(monkeypatch):
    from services.trading_service import TradingService

    monkeypatch.setattr(
        "services.order_service.load_orders",
        lambda *a, **k: _raise_unavailable("load_orders"),
    )
    monkeypatch.setattr(
        "services.trading_engine_runtime.should_queue_intent",
        lambda *a, **k: False,
    )

    svc = TradingService()
    buy = TradeOrder(type="BUY", symbol="BTC/USDT", price=1.0, amount=0, usdt_amount=50)
    sell = TradeOrder(type="SELL", symbol="BTC/USDT", price=1.0, amount=1, signal="SELL")

    def _approve(order, *a, **k):
        return RiskDecision(approved=True, order=order)

    with patch("core.operator_notify.notify_operator") as notify, patch.object(
        svc, "can_execute", return_value=(True, "")
    ), patch.object(svc.risk, "evaluate", side_effect=_approve), patch.object(
        svc.adapter, "execute"
    ) as adapter:
        buy_result = svc.execute_order(buy, "4h", source="manual")
        sell_result = svc.execute_order(sell, "4h", source="manual")

    assert getattr(buy_result, "code", None) == "ledger_unavailable"
    assert getattr(buy_result, "approved", None) is False
    assert getattr(sell_result, "code", None) == "ledger_unavailable"
    assert getattr(sell_result, "approved", None) is False
    assert notify.call_count == 1
    adapter.assert_not_called()


def test_save_orders_failure_propagates_and_skips_adapter(monkeypatch):
    from services.order_service import OrderService
    from services.trading_service import TradingService

    existing = {
        "ledger_scope": "paper",
        "orders": [
            {
                "id": "x1",
                "ledger_scope": "paper",
                "status": "executing",
                "timestamps": {"created": "2026-01-01T00:00:00"},
            }
        ],
    }
    monkeypatch.setattr("services.order_service.load_orders", lambda *a, **k: existing)
    monkeypatch.setattr(
        "services.order_service.save_orders",
        lambda *a, **k: _raise_write_failed("save_orders"),
    )
    monkeypatch.setattr(
        "services.trading_engine_runtime.should_queue_intent",
        lambda *a, **k: False,
    )

    ledger = OrderService("paper")
    with pytest.raises(LedgerWriteFailed):
        ledger.update_status("x1", "filled")

    trading = TradingService()
    buy = TradeOrder(type="BUY", symbol="ETH/USDT", price=1.0, amount=0, usdt_amount=40)

    def _approve(order, *a, **k):
        return RiskDecision(approved=True, order=order)

    with patch.object(trading, "can_execute", return_value=(True, "")), patch.object(
        trading.risk, "evaluate", side_effect=_approve
    ), patch.object(trading.adapter, "execute") as adapter, patch(
        "core.operator_notify.notify_operator"
    ):
        trading.execute_order(buy, "4h", source="manual")
        adapter.assert_not_called()


def test_downgrade_guard_read_failure_blocks(monkeypatch):
    from data_manager import _reject_demo_mongo_orders_downgrade

    class BrokenStore:
        def load_orders(self, scope, tenant_id=None):
            raise ConnectionError("mongo down")

    monkeypatch.setattr("data_manager._mongo_ledger_store", lambda *a, **k: BrokenStore())
    monkeypatch.setattr("data_manager._demo_ledger_backend_is_mongo", lambda *a, **k: True)
    cfg = {"demo": {"backend": "mongo"}}
    assert _reject_demo_mongo_orders_downgrade({"orders": []}, "demo", cfg) is True


def test_prune_skips_when_orders_load_fails(monkeypatch):
    from services.ledger_sync import rebuild_positions_from_orders

    original = {
        "ledger_scope": "demo",
        "positions": {"BTC_USDT_4h": {"amount": 1.0, "average_entry": 50_000.0}},
    }
    saved: list = []

    monkeypatch.setattr(
        "data_manager.load_orders",
        lambda *a, **k: _raise_unavailable("load_orders"),
    )
    monkeypatch.setattr(
        "data_manager.load_positions_document", lambda *a, **k: dict(original)
    )
    monkeypatch.setattr(
        "data_manager.save_positions_document",
        lambda data, *a, **k: saved.append(data) or True,
    )

    with pytest.raises(LedgerUnavailable):
        rebuild_positions_from_orders("demo")
    assert saved == []


def test_unreadable_config_raises_no_fallback(monkeypatch):
    from data_manager import _load_default_config_from_disk

    real_open = open

    def fake_open(path, *args, **kwargs):
        if str(path) == "config.json" or str(path).endswith("/config.json"):
            raise OSError("unreadable")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    with pytest.raises(LedgerUnavailable):
        _load_default_config_from_disk()


def _operator_cfg():
    return {"max_usdt_per_trade": 999, "max_open_positions": 99, "stop_loss_pct": 1.0}


def test_tenant_without_stored_body_is_base_only(monkeypatch):
    """No stored tenant document is the normal case: profile layering applies the
    base config with empty overrides. Not an error (#318 audit)."""
    from data_manager import load_config

    monkeypatch.setattr("data_manager._load_default_config_from_disk", lambda: _operator_cfg())
    monkeypatch.setattr("data_manager._should_use_mongo_for_tenant_config", lambda cfg=None: True)
    monkeypatch.setattr("data_manager._load_tenant_config_body", lambda *a, **k: None)

    cfg = load_config(tenant_id="henry")
    assert cfg.get("max_open_positions") == 99


def test_tenant_config_mongo_failure_is_ledger_unavailable(monkeypatch):
    """A Mongo failure while reading the tenant body must surface — never fall
    back to the operator config."""
    from data_manager import load_config

    monkeypatch.setattr("data_manager._load_default_config_from_disk", lambda: _operator_cfg())
    monkeypatch.setattr("data_manager._should_use_mongo_for_tenant_config", lambda cfg=None: True)

    def _boom(*a, **k):
        raise LedgerUnavailable(op="load_tenant_config_body", tenant_id="henry", cause=ConnectionError("mongo down"))

    monkeypatch.setattr("data_manager._load_tenant_config_body", _boom)

    with pytest.raises(LedgerUnavailable, match="mongo down"):
        load_config(tenant_id="henry")


def test_should_refuse_json_fallback_mongo_vs_local(monkeypatch):
    from data_manager import _should_refuse_json_fallback

    monkeypatch.delenv("DEMO_LEDGER_JSON_FALLBACK", raising=False)
    monkeypatch.setenv("MULTI_TENANT_ENABLED", "0")
    mongo_cfg = {
        "architecture": {"ledger_backend": "mongo", "ledger_dual_write": False},
        "paper": {"backend": "mongo"},
    }
    local_cfg = {
        "architecture": {"ledger_backend": "local", "ledger_dual_write": False},
        "paper": {"backend": "local"},
    }
    assert _should_refuse_json_fallback("live", mongo_cfg) is True
    assert _should_refuse_json_fallback("paper", local_cfg) is False


def test_filled_order_with_failing_ledger_write_is_not_reported_as_denial():
    """Review PR #322: the exchange call happened -- never report "denied"."""
    from unittest.mock import patch

    from core.models import TradeOrder, TradeResult
    from services import trading_service as ts
    from services.trading_service import TradingService
    from storage.errors import LedgerWriteFailed

    ts._ledger_unavailable_notified.clear()
    svc = TradingService()
    svc.config._raw.setdefault("architecture", {})["ledger_lock_fail_closed"] = False
    svc.config._raw["architecture"]["single_writer_lease_enabled"] = False
    order = TradeOrder(type="BUY", symbol="BTC/USDT", price=100.0, amount=0, usdt_amount=50)
    filled = TradeResult(True, "BUY", "BTC/USDT", amount=0.5, price=100.0, usdt_amount=50)

    with patch.object(svc.adapter, "execute", return_value=filled), patch.object(
        svc.risk, "evaluate", return_value=__import__("core.models", fromlist=["RiskDecision"]).RiskDecision(
            approved=True, message="", order=order
        )
    ), patch(
        "services.order_service.OrderService.link_execution_result",
        side_effect=LedgerWriteFailed("disk full", op="save_orders"),
    ), patch("core.operator_notify.notify_operator") as notify:
        result = svc.execute_order(order, "4h", source="manual")

    assert result.executed is True, "a real fill must not be reported as denied"
    assert result.needs_reconcile is True
    assert getattr(result, "code", "") != "ledger_unavailable"
    assert notify.call_count == 1
