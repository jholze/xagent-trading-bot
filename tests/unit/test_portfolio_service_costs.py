"""PortfolioService cash identity: Σ received − Σ spent == Σ pnl."""

from __future__ import annotations

import pytest

from core.config import BotConfig
from core.costs import COST_MODEL_VERSION
from data_manager import load_trade_history, save_trade_history
from services.portfolio_service import PortfolioService
from strategies.positions import clear_positions_memory, get_position


def _cfg() -> BotConfig:
    return BotConfig(
        {
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
        }
    )


def test_buy_sell_cash_equals_pnl(monkeypatch):
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
    svc = PortfolioService(_cfg())
    symbol = "COST/USDT"
    svc.execute_buy(symbol, "4h", 100.0, 1000.0, source="manual")
    pos = get_position(symbol, "4h")
    qty = float(pos["amount"])
    assert qty == pytest.approx(9.98)
    svc.execute_sell(symbol, "4h", 100.0, "SELL_FULL", qty, source="manual")

    history = load_trade_history()
    trades = history["trades"]
    spent = sum(float(t.get("usdt_amount") or 0) for t in trades if t.get("type") == "BUY")
    received = sum(float(t.get("usdt_received") or 0) for t in trades if t.get("type") == "SELL")
    pnl = sum(float(t.get("pnl") or 0) for t in trades if t.get("type") == "SELL")
    assert abs((received - spent) - pnl) < 1e-9
    assert pnl == pytest.approx(-3.996)
    for t in trades:
        assert t.get("cost_model") == COST_MODEL_VERSION
        assert "fee_base" in t
        assert "fee_quote" in t
        assert "fee_usdt" in t
        assert "slippage_usdt" in t
