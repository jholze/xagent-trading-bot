"""Ledger SHORT rows must store leverage next to margin computed at that leverage.

#372: margin_usdt used to re-clamp to 2.0 even when the caller had already
resolved a higher cap, so execute_short wrote leverage=3 next to margin at 2x.
"""

from __future__ import annotations

import pytest

from core.config import BotConfig
from data_manager import load_trade_history, save_trade_history
from services.portfolio_service import PortfolioService
from strategies.positions import clear_positions_memory, get_position


def _cfg(*, leverage_cap: float = 3) -> BotConfig:
    return BotConfig(
        {
            "max_usdt_per_trade": 1000,
            "shorts": {
                "enabled": True,
                "allow_live": False,
                "leverage_default": 2,
                "leverage_cap": leverage_cap,
            },
        }
    )


def _isolate_history(monkeypatch) -> None:
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


def test_execute_short_row_margin_matches_stored_leverage(monkeypatch):
    """leverage_cap=3, leverage=3 → margin_usdt == amount * entry / row leverage.

    Old code: clamp inside margin_usdt made margin = amount*entry/2.0 (150)
    while the row stored leverage=3, so amount*entry/row['leverage'] == 100.
    """
    _isolate_history(monkeypatch)
    svc = PortfolioService(_cfg(leverage_cap=3))
    price = 10.0
    usdt_amount = 300.0
    out = svc.execute_short(
        "ROW372/USDT", "4h", price, usdt_amount=usdt_amount, leverage=3,
    )
    assert out.executed, out.message

    rec = load_trade_history()["trades"][-1]
    assert rec["type"] == "SHORT"
    lev = float(rec["leverage"])
    assert lev == pytest.approx(3.0)
    amount = float(rec["amount"])
    entry = float(rec["price"])
    assert rec["margin_usdt"] == pytest.approx(amount * entry / lev)
    pos = get_position("ROW372/USDT", "4h")
    assert float(pos["leverage"]) == pytest.approx(3.0)
