"""#345: persist ctx_oracle_state / ctx_coin_regime / ctx_volume_rel (additive)."""

from __future__ import annotations

import pytest

from core.config import BotConfig
from core.models import RiskDecision, TradeOrder, ctx_float_or_none, trade_ctx_fields
from data_manager import load_trade_history, save_trade_history
from services.order_service import OrderService
from services.portfolio_service import PortfolioService, _ctx_ledger_fields
from strategies.positions import clear_positions_memory

_CTX = {
    "ctx_oracle_state": "RISK_ON",
    "ctx_coin_regime": "RANGING",
    "ctx_volume_rel": 1.37,
}


def _assert_ctx(record: dict, *, oracle, regime, volume) -> None:
    assert "ctx_oracle_state" in record
    assert "ctx_coin_regime" in record
    assert "ctx_volume_rel" in record
    assert record["ctx_oracle_state"] == oracle
    assert record["ctx_coin_regime"] == regime
    if volume is None:
        assert record["ctx_volume_rel"] is None
    else:
        assert record["ctx_volume_rel"] == pytest.approx(volume)


def _order_with_ctx(**kwargs) -> TradeOrder:
    return TradeOrder(
        "BUY",
        "CTX/USDT",
        10.0,
        0,
        usdt_amount=100,
        ctx_oracle_state=_CTX["ctx_oracle_state"],
        ctx_coin_regime=_CTX["ctx_coin_regime"],
        ctx_volume_rel=_CTX["ctx_volume_rel"],
        **kwargs,
    )


@pytest.fixture
def order_svc(monkeypatch):
    from services import order_service
    from storage.order_ledger_v2 import reset_order_ledger_v2_for_tests

    order_service._ORDERS_READ_CACHE.clear()
    monkeypatch.setenv("ORDER_LEDGER_V2", "1")
    monkeypatch.setenv("ORDER_LEDGER_V2_READS", "1")
    monkeypatch.setenv("ORDER_LEDGER_V2_BACKEND", "memory")
    reset_order_ledger_v2_for_tests()
    yield OrderService("paper")
    reset_order_ledger_v2_for_tests()
    order_service._ORDERS_READ_CACHE.clear()


def _cfg() -> BotConfig:
    return BotConfig(
        {
            "max_usdt_per_trade": 1000,
            "costs": {
                "fee_source": "config",
                "gate": {
                    "spot": {
                        "fee_maker_pct": 0.0,
                        "fee_taker_pct": 0.0,
                        "slippage_pct": 0.0,
                        "fee_side_buy": "base",
                        "fee_side_sell": "quote",
                    }
                },
            },
        }
    )


@pytest.fixture
def portfolio_svc(monkeypatch):
    monkeypatch.setattr(
        "data_manager._reconcile_scoped_trade_history",
        lambda history, scope, config=None, **kwargs: (history, False),
    )
    monkeypatch.setattr(
        "data_manager._ledger_reads_mongo_trade_history", lambda *a, **k: False
    )
    monkeypatch.setattr("data_manager._ledger_writes_mongo", lambda *a, **k: False)
    clear_positions_memory()
    save_trade_history(
        {"virtual_balance": 5000.0, "realized_pnl": 0.0, "open_positions": 0, "trades": []}
    )
    yield PortfolioService(_cfg())
    clear_positions_memory()


def test_create_from_request_persists_ctx_values(order_svc):
    record = order_svc.create_from_request(
        _order_with_ctx(), telegram_token="ctx-filled"
    )
    _assert_ctx(record, oracle="RISK_ON", regime="RANGING", volume=1.37)
    assert "ctx_oracle_state" not in (record.get("request") or {})
    loaded = order_svc.get_by_id("ctx-filled")
    assert loaded is not None
    _assert_ctx(loaded, oracle="RISK_ON", regime="RANGING", volume=1.37)
    assert "ctx_oracle_state" not in (loaded.get("request") or {})


def test_create_from_request_ctx_keys_none_when_absent(order_svc):
    order = TradeOrder("BUY", "CTX/USDT", 10.0, 0, usdt_amount=100)
    record = order_svc.create_from_request(order, telegram_token="ctx-empty")
    _assert_ctx(record, oracle=None, regime=None, volume=None)
    loaded = order_svc.get_by_id("ctx-empty")
    assert loaded is not None
    _assert_ctx(loaded, oracle=None, regime=None, volume=None)


def test_record_rejected_carries_ctx_keys(order_svc):
    order = _order_with_ctx()
    decision = RiskDecision(
        approved=False, message="Max positions", code="max_open_positions", order=order
    )
    record = order_svc.record_rejected(order, decision)
    _assert_ctx(record, oracle="RISK_ON", regime="RANGING", volume=1.37)
    loaded = order_svc.get_by_id(record["id"])
    assert loaded is not None
    _assert_ctx(loaded, oracle="RISK_ON", regime="RANGING", volume=1.37)


def test_execute_order_buy_writes_ctx_into_trade_history(portfolio_svc):
    order = _order_with_ctx()
    result = portfolio_svc.execute_order(order, "4h")
    assert result.executed
    trades = load_trade_history()["trades"]
    assert trades
    _assert_ctx(trades[-1], oracle="RISK_ON", regime="RANGING", volume=1.37)


def test_execute_buy_without_ctx_keys_present_none(portfolio_svc):
    result = portfolio_svc.execute_buy("PLAIN/USDT", "4h", 10.0, 50.0)
    assert result.executed
    trades = load_trade_history()["trades"]
    assert trades
    _assert_ctx(trades[-1], oracle=None, regime=None, volume=None)


def test_trade_order_positional_construction_ctx_defaults_none():
    order = TradeOrder("BUY", "X/USDT", 1.0, 2.0)
    assert order.type == "BUY"
    assert order.symbol == "X/USDT"
    assert order.price == 1.0
    assert order.qty == 2.0
    assert order.ctx_oracle_state is None
    assert order.ctx_coin_regime is None
    assert order.ctx_volume_rel is None
    _assert_ctx(trade_ctx_fields(order), oracle=None, regime=None, volume=None)
    _assert_ctx(trade_ctx_fields(None), oracle=None, regime=None, volume=None)


# --- #365: ctx_volume_window_days + structural float coercion (add-only) ---


def test_ctx_float_or_none_coercion_cases():
    assert ctx_float_or_none(None) is None
    assert ctx_float_or_none(1.37) == pytest.approx(1.37)
    assert ctx_float_or_none("1.5") == pytest.approx(1.5)
    assert ctx_float_or_none("abc") is None
    assert ctx_float_or_none(object()) is None
    assert ctx_float_or_none(float("nan")) is None
    assert ctx_float_or_none(float("inf")) is None
    assert ctx_float_or_none(float("-inf")) is None


def test_trade_ctx_fields_garbage_volume_rel_is_none_not_raise():
    order = TradeOrder("BUY", "X/USDT", 1.0, 2.0, ctx_volume_rel="not-a-number")
    fields = trade_ctx_fields(order)
    assert fields["ctx_volume_rel"] is None
    assert fields["ctx_volume_window_days"] is None


def test_trade_ctx_fields_garbage_window_days_is_none_not_raise():
    order = TradeOrder("BUY", "X/USDT", 1.0, 2.0, ctx_volume_window_days="not-a-number")
    fields = trade_ctx_fields(order)
    assert fields["ctx_volume_window_days"] is None


def test_trade_ctx_fields_window_days_passthrough():
    order = TradeOrder("BUY", "X/USDT", 1.0, 2.0, ctx_volume_window_days=12.5)
    fields = trade_ctx_fields(order)
    assert fields["ctx_volume_window_days"] == pytest.approx(12.5)


def test_trade_ctx_fields_none_order_has_all_four_keys_none():
    fields = trade_ctx_fields(None)
    assert fields == {
        "ctx_oracle_state": None,
        "ctx_coin_regime": None,
        "ctx_volume_rel": None,
        "ctx_volume_window_days": None,
    }


def test_ctx_ledger_fields_garbage_volume_rel_is_none_not_raise():
    fields = _ctx_ledger_fields({"ctx_volume_rel": "not-a-number"})
    assert fields["ctx_volume_rel"] is None
    assert "ctx_volume_window_days" in fields
    assert fields["ctx_volume_window_days"] is None


def test_ctx_ledger_fields_window_key_none_when_absent():
    fields = _ctx_ledger_fields({"ctx_oracle_state": "RISK_ON"})
    assert "ctx_volume_window_days" in fields
    assert fields["ctx_volume_window_days"] is None


def test_ctx_ledger_fields_window_passthrough():
    fields = _ctx_ledger_fields({"ctx_volume_window_days": 12.5})
    assert fields["ctx_volume_window_days"] == pytest.approx(12.5)


def test_trade_order_positional_construction_window_days_defaults_none():
    order = TradeOrder("BUY", "X/USDT", 1.0, 2.0)
    assert order.ctx_volume_window_days is None


def test_execute_order_buy_writes_ctx_volume_window_days(portfolio_svc):
    order = TradeOrder(
        "BUY",
        "CTX/USDT",
        10.0,
        0,
        usdt_amount=100,
        ctx_oracle_state=_CTX["ctx_oracle_state"],
        ctx_coin_regime=_CTX["ctx_coin_regime"],
        ctx_volume_rel=_CTX["ctx_volume_rel"],
        ctx_volume_window_days=12.5,
    )
    result = portfolio_svc.execute_order(order, "4h")
    assert result.executed
    trades = load_trade_history()["trades"]
    assert trades
    rec = trades[-1]
    assert "ctx_volume_window_days" in rec
    assert rec["ctx_volume_window_days"] == pytest.approx(12.5)
    _assert_ctx(rec, oracle="RISK_ON", regime="RANGING", volume=1.37)


def test_execute_buy_without_ctx_still_writes_window_days_key_none(portfolio_svc):
    result = portfolio_svc.execute_buy("PLAIN/USDT", "4h", 10.0, 50.0)
    assert result.executed
    rec = load_trade_history()["trades"][-1]
    assert "ctx_volume_window_days" in rec
    assert rec["ctx_volume_window_days"] is None
