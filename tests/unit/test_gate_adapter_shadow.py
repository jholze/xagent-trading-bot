"""GateExecutionAdapter shadow mode (#312): one execution path, no Paper adapter."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.config import BotConfig
from core.costs import COST_MODEL_VERSION
from core.models import TradeOrder
from data_manager import load_trade_history, save_trade_history
from execution.factory import get_execution_adapter
from execution.gate_adapter import GateExecutionAdapter
from services.portfolio_service import PortfolioService
from strategies.positions import clear_positions_memory, get_position


@pytest.fixture(autouse=True)
def _reset_shadow_market_cache():
    GateExecutionAdapter._shadow_markets_cache = None
    GateExecutionAdapter._shadow_markets_failed = False
    GateExecutionAdapter._shadow_markets_warned = False
    yield
    GateExecutionAdapter._shadow_markets_cache = None
    GateExecutionAdapter._shadow_markets_failed = False
    GateExecutionAdapter._shadow_markets_warned = False


def test_paper_execution_adapter_module_gone():
    with pytest.raises(ImportError):
        from execution.paper_adapter import PaperExecutionAdapter  # noqa: F401


def test_factory_paper_builds_gate_shadow():
    cfg = BotConfig({"trading_mode": "paper", "live": {"execution": "real", "dry_run": False}})
    adapter = get_execution_adapter(cfg, PortfolioService(cfg))
    assert isinstance(adapter, GateExecutionAdapter)
    assert adapter.mode == "shadow"


def test_factory_demo_mode_live_real_raises(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("GATE_API_KEY", "k")
    monkeypatch.setenv("GATE_API_SECRET", "s")
    cfg = BotConfig(
        {
            "trading_mode": "live",
            "live_confirmed": True,
            "live": {"execution": "real", "dry_run": False},
        }
    )
    with pytest.raises(RuntimeError, match="DEMO_MODE"):
        get_execution_adapter(cfg, PortfolioService(cfg))


SYMBOL = "COST/USDT"


def _cost_cfg() -> BotConfig:
    return BotConfig(
        {
            "trading_mode": "paper",
            "max_usdt_per_trade": 1000,
            "costs": {
                "fee_source": "config",
                "gate": {
                    "spot": {
                        "fee_maker_pct": 0.2,
                        "fee_taker_pct": 0.2,
                        "slippage_pct": 0.0,
                        "fee_side_buy": "base",
                        "fee_side_sell": "quote",
                    }
                },
            },
            "live": {"execution": "shadow", "dry_run": False, "simulated_balance_usdt": 5000},
        }
    )


def _mock_exchange(symbol: str = SYMBOL, *, min_amount: float = 0.0, min_cost: float = 0.0):
    ex = MagicMock(name="ccxt.gate")
    ex.amount_to_precision.side_effect = lambda _sym, amt: amt
    ex.load_markets.return_value = {
        symbol: {
            "limits": {
                "amount": {"min": min_amount},
                "cost": {"min": min_cost},
            }
        }
    }
    return ex


def _isolate_ledger(monkeypatch):
    monkeypatch.setattr(
        "data_manager._reconcile_scoped_trade_history",
        lambda history, scope, config=None, **kwargs: (history, False),
    )
    monkeypatch.setattr(
        "data_manager._ledger_reads_mongo_trade_history", lambda *a, **k: False,
    )
    monkeypatch.setattr("data_manager._ledger_writes_mongo", lambda *a, **k: False)
    clear_positions_memory()
    save_trade_history(
        {"virtual_balance": 5000.0, "realized_pnl": 0.0, "open_positions": 0, "trades": []}
    )


def _shadow_adapter(monkeypatch, *, min_amount=0.0, min_cost=0.0, usdt_balance=5000.0):
    _isolate_ledger(monkeypatch)
    cfg = _cost_cfg()
    captured: list[dict] = []
    monkeypatch.setattr(
        "execution.gate_adapter.record_live_trade",
        lambda rec: captured.append(rec),
    )
    adapter = GateExecutionAdapter(cfg, PortfolioService(cfg), mode="shadow")
    adapter._exchange = _mock_exchange(SYMBOL, min_amount=min_amount, min_cost=min_cost)
    adapter._fetch_usdt_balance = lambda: usdt_balance
    return adapter, captured


def test_shadow_buy_calls_precision_never_create_order(monkeypatch):
    adapter, captured = _shadow_adapter(monkeypatch)
    result = adapter.execute(TradeOrder("BUY", SYMBOL, 100.0, 0, usdt_amount=1000.0), "4h")
    assert result.executed, result.message
    adapter._exchange.amount_to_precision.assert_called()
    adapter._exchange.create_market_buy_order.assert_not_called()
    adapter._exchange.create_market_sell_order.assert_not_called()
    adapter._exchange.create_order.assert_not_called()
    assert adapter.mode == "shadow"
    assert captured
    rec = captured[-1]
    assert rec["exchange_order_id"].startswith("shadow-")
    assert rec["cost_model"] == COST_MODEL_VERSION
    assert rec["precision_unverified"] is False
    assert "fee_base" in rec
    assert "fee_quote" in rec
    assert "fee_usdt" in rec
    assert result.exchange_order_id.startswith("shadow-")
    assert result.precision_unverified is False
    assert result.message.startswith("shadow ")


def test_shadow_sell_calls_validate_and_precision_never_create_order(monkeypatch):
    adapter, captured = _shadow_adapter(monkeypatch)
    buy = adapter.execute(TradeOrder("BUY", SYMBOL, 100.0, 0, usdt_amount=1000.0), "4h")
    assert buy.executed, buy.message
    qty = float(get_position(SYMBOL, "4h")["amount"])
    orig = adapter._validate_sell_amount
    spy = MagicMock(wraps=orig)
    adapter._validate_sell_amount = spy
    result = adapter.execute(TradeOrder("SELL", SYMBOL, 100.0, qty, signal="SELL_FULL"), "4h")
    assert result.executed, result.message
    spy.assert_called()
    adapter._exchange.amount_to_precision.assert_called()
    adapter._exchange.load_markets.assert_called()
    adapter._exchange.create_market_buy_order.assert_not_called()
    adapter._exchange.create_market_sell_order.assert_not_called()
    adapter._exchange.create_order.assert_not_called()
    rec = captured[-1]
    assert rec["exchange_order_id"].startswith("shadow-")
    assert rec["cost_model"] == COST_MODEL_VERSION
    assert rec["type"] == "SELL"


def test_shadow_buy_sell_pnl_equals_cash(monkeypatch):
    adapter, captured = _shadow_adapter(monkeypatch)
    adapter.execute(TradeOrder("BUY", SYMBOL, 100.0, 0, usdt_amount=1000.0), "4h")
    qty = float(get_position(SYMBOL, "4h")["amount"])
    assert qty == pytest.approx(9.98)
    adapter.execute(TradeOrder("SELL", SYMBOL, 100.0, qty, signal="SELL_FULL"), "4h")

    history = load_trade_history()
    trades = history["trades"]
    spent = sum(float(t.get("usdt_amount") or 0) for t in trades if t.get("type") == "BUY")
    received = sum(float(t.get("usdt_received") or 0) for t in trades if t.get("type") == "SELL")
    pnl = sum(float(t.get("pnl") or 0) for t in trades if t.get("type") == "SELL")
    assert abs((received - spent) - pnl) < 1e-9
    assert pnl == pytest.approx(-3.996)
    for t in captured:
        assert t.get("cost_model") == COST_MODEL_VERSION
        assert t["exchange_order_id"].startswith("shadow-")
        assert "fee_base" in t
        assert "fee_quote" in t
        assert "fee_usdt" in t


def test_shadow_sell_below_min_notional_rejected_like_real(monkeypatch):
    adapter, _ = _shadow_adapter(monkeypatch, min_cost=10.0)
    order = TradeOrder("SELL", SYMBOL, 100.0, 0.01, signal="SELL")
    shadow = adapter.execute(order, "4h")
    real_adapter, _ = _shadow_adapter(monkeypatch, min_cost=10.0)
    real_adapter._adapter_mode = "real"
    real = real_adapter.execute(order, "4h")
    assert not shadow.executed
    assert not real.executed
    assert shadow.message == real.message
    assert "minimum" in shadow.message.lower()
    adapter._exchange.create_market_sell_order.assert_not_called()
    real_adapter._exchange.create_market_sell_order.assert_not_called()


def test_shadow_sell_above_exchange_balance_rejected_like_real(monkeypatch):
    adapter, _ = _shadow_adapter(monkeypatch, min_cost=10.0)
    adapter._fetch_base_balance = lambda _ex, _sym: 0.001
    order = TradeOrder("SELL", SYMBOL, 100.0, 5.0, signal="SELL")
    shadow = adapter.execute(order, "4h")
    real_adapter, _ = _shadow_adapter(monkeypatch, min_cost=10.0)
    real_adapter._adapter_mode = "real"
    real_adapter._fetch_base_balance = lambda _ex, _sym: 0.001
    real = real_adapter.execute(order, "4h")
    assert not shadow.executed
    assert not real.executed
    assert shadow.message == real.message
    assert "minimum" in shadow.message.lower()
    adapter._exchange.create_market_sell_order.assert_not_called()
    real_adapter._exchange.create_order.assert_not_called()


def test_testnet_sets_sandbox_mode(monkeypatch):
    created = {}

    class FakeGate:
        def __init__(self, params):
            created["params"] = params
            self.set_sandbox_mode = MagicMock()

    monkeypatch.setattr("execution.gate_adapter.ccxt.gate", FakeGate)
    monkeypatch.setenv("GATE_API_KEY", "k")
    monkeypatch.setenv("GATE_API_SECRET", "s")
    cfg = BotConfig(
        {
            "trading_mode": "live",
            "live_confirmed": True,
            "live": {
                "execution": "testnet",
                "dry_run": False,
                "api_key_env": "GATE_API_KEY",
                "api_secret_env": "GATE_API_SECRET",
            },
        }
    )
    adapter = GateExecutionAdapter(cfg, mode="testnet")
    exchange = adapter._get_exchange()
    exchange.set_sandbox_mode.assert_called_once_with(True)
    assert created["params"]["apiKey"] == "k"


def test_shadow_fills_when_load_markets_raises(monkeypatch):
    adapter, captured = _shadow_adapter(monkeypatch)
    adapter._exchange.load_markets.side_effect = RuntimeError("network down")
    adapter._exchange.load_markets.return_value = None
    logged: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "execution.gate_adapter.log",
        lambda msg, level="INFO": logged.append((str(msg), str(level))),
    )
    GateExecutionAdapter._shadow_markets_cache = None
    GateExecutionAdapter._shadow_markets_failed = False
    GateExecutionAdapter._shadow_markets_warned = False

    result = adapter.execute(TradeOrder("BUY", SYMBOL, 100.0, 0, usdt_amount=1000.0), "4h")
    assert result.executed, result.message
    assert result.precision_unverified is True
    assert captured[-1]["precision_unverified"] is True
    assert result.message.startswith("shadow ")
    adapter._exchange.amount_to_precision.assert_not_called()
    adapter._exchange.create_market_buy_order.assert_not_called()
    adapter._exchange.create_order.assert_not_called()
    unavailable = [
        msg for msg, lvl in logged
        if lvl == "WARNING" and "gate markets unavailable" in msg
    ]
    assert len(unavailable) == 1

    adapter.execute(TradeOrder("BUY", SYMBOL, 100.0, 0, usdt_amount=100.0), "4h")
    unavailable = [
        msg for msg, lvl in logged
        if lvl == "WARNING" and "gate markets unavailable" in msg
    ]
    assert len(unavailable) == 1


def test_shadow_precision_verified_when_markets_available(monkeypatch):
    adapter, captured = _shadow_adapter(monkeypatch)
    result = adapter.execute(TradeOrder("BUY", SYMBOL, 100.0, 0, usdt_amount=1000.0), "4h")
    assert result.executed, result.message
    assert result.precision_unverified is False
    assert captured[-1]["precision_unverified"] is False
    adapter._exchange.amount_to_precision.assert_called()
    adapter._exchange.load_markets.assert_called()
    adapter._exchange.create_market_buy_order.assert_not_called()
