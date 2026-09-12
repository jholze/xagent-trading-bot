"""#345 slice 3: Gate adapter carries ctx axes into both live and local ledgers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.config import BotConfig
from core.models import SignalAnalysis, TradeOrder, TradeResult
from data_manager import load_trade_history, save_trade_history
from execution.gate_adapter import GateExecutionAdapter
from services.portfolio_service import PortfolioService
from services.signal_orchestrator import SignalOrchestrator
from strategies.positions import clear_positions_memory, get_position

SYMBOL = "CTXAX/USDT"

_CTX = {
    "ctx_oracle_state": "RISK_ON",
    "ctx_coin_regime": "RANGING",
    "ctx_volume_rel": 1.37,
}

_SELL_CTX = {
    "ctx_oracle_state": "RISK_OFF",
    "ctx_coin_regime": "TRENDING",
    "ctx_volume_rel": 0.42,
}

EXISTING_REC_KEYS = {
    "type",
    "symbol",
    "price",
    "amount",
    "usdt_amount",
    "usdt_received",
    "pnl",
    "exchange_order_id",
    "order_id",
    "fee",
    "source",
    "timestamp",
    "mode",
    "cost_model",
}


@pytest.fixture(autouse=True)
def _reset_shadow_market_cache():
    GateExecutionAdapter._shadow_markets_cache = None
    GateExecutionAdapter._shadow_markets_failed = False
    GateExecutionAdapter._shadow_markets_warned = False
    yield
    GateExecutionAdapter._shadow_markets_cache = None
    GateExecutionAdapter._shadow_markets_failed = False
    GateExecutionAdapter._shadow_markets_warned = False


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


def _assert_existing_rec_keys(rec: dict) -> None:
    missing = EXISTING_REC_KEYS - set(rec)
    assert not missing, f"live-trade rec missing keys: {sorted(missing)}"


def _assert_window(record: dict, days) -> None:
    assert "ctx_volume_window_days" in record
    if days is None:
        assert record["ctx_volume_window_days"] is None
    else:
        assert record["ctx_volume_window_days"] == pytest.approx(days)


def _last_history_of_type(side: str) -> dict:
    trades = [t for t in load_trade_history()["trades"] if t.get("type") == side]
    assert trades, f"no {side} row in trade history"
    return trades[-1]


def test_buy_order_ctx_reaches_live_record_and_trade_history(monkeypatch):
    adapter, captured = _shadow_adapter(monkeypatch)
    order = TradeOrder(
        "BUY",
        SYMBOL,
        100.0,
        0,
        usdt_amount=1000.0,
        ctx_oracle_state=_CTX["ctx_oracle_state"],
        ctx_coin_regime=_CTX["ctx_coin_regime"],
        ctx_volume_rel=_CTX["ctx_volume_rel"],
    )
    result = adapter.execute(order, "4h")
    assert result.executed, result.message
    assert captured
    rec = captured[-1]
    _assert_ctx(rec, oracle="RISK_ON", regime="RANGING", volume=1.37)
    _assert_existing_rec_keys(rec)
    _assert_ctx(
        _last_history_of_type("BUY"),
        oracle="RISK_ON",
        regime="RANGING",
        volume=1.37,
    )


def test_sell_order_ctx_reaches_live_record_and_trade_history(monkeypatch):
    adapter, captured = _shadow_adapter(monkeypatch)
    buy = adapter.execute(TradeOrder("BUY", SYMBOL, 100.0, 0, usdt_amount=1000.0), "4h")
    assert buy.executed, buy.message
    qty = float(get_position(SYMBOL, "4h")["amount"])
    sell = TradeOrder(
        "SELL",
        SYMBOL,
        100.0,
        qty,
        signal="SELL_FULL",
        ctx_oracle_state=_SELL_CTX["ctx_oracle_state"],
        ctx_coin_regime=_SELL_CTX["ctx_coin_regime"],
        ctx_volume_rel=_SELL_CTX["ctx_volume_rel"],
    )
    result = adapter.execute(sell, "4h")
    assert result.executed, result.message
    rec = captured[-1]
    assert rec["type"] == "SELL"
    _assert_ctx(rec, oracle="RISK_OFF", regime="TRENDING", volume=0.42)
    _assert_existing_rec_keys(rec)
    _assert_ctx(
        _last_history_of_type("SELL"),
        oracle="RISK_OFF",
        regime="TRENDING",
        volume=0.42,
    )


def test_order_without_ctx_writes_none_keys_on_both_ledgers(monkeypatch):
    adapter, captured = _shadow_adapter(monkeypatch)
    result = adapter.execute(TradeOrder("BUY", SYMBOL, 100.0, 0, usdt_amount=1000.0), "4h")
    assert result.executed, result.message
    assert captured
    rec = captured[-1]
    _assert_ctx(rec, oracle=None, regime=None, volume=None)
    _assert_existing_rec_keys(rec)
    _assert_ctx(_last_history_of_type("BUY"), oracle=None, regime=None, volume=None)


def test_live_record_keeps_preexisting_key_set(monkeypatch):
    adapter, captured = _shadow_adapter(monkeypatch)
    result = adapter.execute(
        TradeOrder(
            "BUY",
            SYMBOL,
            100.0,
            0,
            usdt_amount=1000.0,
            ctx_oracle_state="NEUTRAL",
            ctx_coin_regime="RANGING",
            ctx_volume_rel=1.0,
        ),
        "4h",
    )
    assert result.executed, result.message
    rec = captured[-1]
    _assert_existing_rec_keys(rec)
    _assert_ctx(rec, oracle="NEUTRAL", regime="RANGING", volume=1.0)


def test_buy_order_ctx_volume_window_days_reaches_both_ledgers(monkeypatch):
    adapter, captured = _shadow_adapter(monkeypatch)
    order = TradeOrder(
        "BUY",
        SYMBOL,
        100.0,
        0,
        usdt_amount=1000.0,
        ctx_oracle_state=_CTX["ctx_oracle_state"],
        ctx_coin_regime=_CTX["ctx_coin_regime"],
        ctx_volume_rel=_CTX["ctx_volume_rel"],
        ctx_volume_window_days=30.0,
    )
    result = adapter.execute(order, "4h")
    assert result.executed, result.message
    assert captured
    rec = captured[-1]
    _assert_window(rec, 30.0)
    _assert_existing_rec_keys(rec)
    _assert_window(_last_history_of_type("BUY"), 30.0)


def test_sell_order_ctx_volume_window_days_reaches_both_ledgers(monkeypatch):
    adapter, captured = _shadow_adapter(monkeypatch)
    buy = adapter.execute(TradeOrder("BUY", SYMBOL, 100.0, 0, usdt_amount=1000.0), "4h")
    assert buy.executed, buy.message
    qty = float(get_position(SYMBOL, "4h")["amount"])
    sell = TradeOrder(
        "SELL",
        SYMBOL,
        100.0,
        qty,
        signal="SELL_FULL",
        ctx_oracle_state=_SELL_CTX["ctx_oracle_state"],
        ctx_coin_regime=_SELL_CTX["ctx_coin_regime"],
        ctx_volume_rel=_SELL_CTX["ctx_volume_rel"],
        ctx_volume_window_days=12.5,
    )
    result = adapter.execute(sell, "4h")
    assert result.executed, result.message
    rec = captured[-1]
    assert rec["type"] == "SELL"
    _assert_window(rec, 12.5)
    _assert_existing_rec_keys(rec)
    _assert_window(_last_history_of_type("SELL"), 12.5)


def test_order_without_ctx_volume_window_days_writes_none_on_both_ledgers(monkeypatch):
    adapter, captured = _shadow_adapter(monkeypatch)
    result = adapter.execute(TradeOrder("BUY", SYMBOL, 100.0, 0, usdt_amount=1000.0), "4h")
    assert result.executed, result.message
    assert captured
    rec = captured[-1]
    _assert_window(rec, None)
    _assert_existing_rec_keys(rec)
    _assert_window(_last_history_of_type("BUY"), None)


def test_orchestrator_buy_order_carries_ctx_volume_window_days_from_analysis():
    trading = MagicMock()
    captured = {}

    def _exec(order, *a, **k):
        captured["order"] = order
        return TradeResult(True, "BUY", order.symbol, amount=1, price=1.0)

    trading.execute_order.side_effect = _exec
    trading.refresh = MagicMock()
    orch = SignalOrchestrator()
    orch.trading = trading

    with patch(
        "services.signal_orchestrator.get_position",
        return_value={"amount": 0},
    ), patch(
        "services.signal_orchestrator.resolve_coin_config",
        return_value={"strategy_params": {}},
    ):
        orch.execute_if_needed(
            SignalAnalysis(
                action="BUY",
                symbol="CTX/USDT",
                timeframe="4h",
                rsi=40.0,
                lower_bb=1.0,
                vol_multiplier=1.0,
                ampel_emoji="",
                ampel_text="",
                sources=["technical"],
                normalized_action="BUY",
                recommended=True,
                regime="RANGING",
                ctx_oracle_state="RISK_ON",
                ctx_volume_rel=1.37,
                ctx_volume_window_days=12.5,
            ),
            coin={"symbol": "CTX/USDT", "timeframe": "4h"},
            current_price=1.0,
        )

    order = captured.get("order")
    assert order is not None
    assert order.type == "BUY"
    assert order.ctx_volume_window_days == pytest.approx(12.5)


def test_orchestrator_sell_order_carries_ctx_volume_window_days_from_analysis():
    trading = MagicMock()
    captured = {}

    def _exec(order, *a, **k):
        captured["order"] = order
        return TradeResult(True, "SELL", order.symbol, amount=1, price=1.0)

    trading.execute_order.side_effect = _exec
    trading.refresh = MagicMock()
    orch = SignalOrchestrator()
    orch.trading = trading
    analysis = SignalAnalysis(
        action="SELL_30",
        symbol="LAB/USDT",
        timeframe="4h",
        rsi=70.0,
        lower_bb=1.0,
        vol_multiplier=1.0,
        ampel_emoji="",
        ampel_text="",
        sources=["time_profit_exit", "technical"],
        normalized_action="SELL_PARTIAL_50",
        rationale="Time->profit exit",
        sell_source="time_profit_exit",
        recommended=True,
        regime="RANGING",
        ctx_oracle_state="RISK_ON",
        ctx_volume_rel=1.37,
        ctx_volume_window_days=12.5,
    )
    with patch(
        "services.signal_orchestrator.find_open_position_for_symbol",
        return_value=("4h", {"amount": 100.0}),
    ), patch(
        "services.signal_orchestrator.get_position",
        return_value={"amount": 100.0, "side": "long"},
    ), patch(
        "services.signal_orchestrator.resolve_coin_config",
        return_value={"strategy_params": {}},
    ), patch(
        "strategies.positions.sell_fraction_for_signal",
        return_value=0.5,
    ):
        orch.execute_if_needed(
            analysis,
            coin={"symbol": "LAB/USDT", "timeframe": "4h"},
            current_price=0.15,
        )

    order = captured.get("order")
    assert order is not None
    assert order.type == "SELL"
    assert order.ctx_volume_window_days == pytest.approx(12.5)
