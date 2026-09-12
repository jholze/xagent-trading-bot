"""#373 part 2b: shadow COVER live-row carries funding_usdt / funding_unknown.

Fails on current code with KeyError on rec["funding_unknown"] — _sync_local_ledger
builds its own live row and does not copy TradeResult funding fields.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from core.config import BotConfig
from core.models import TradeOrder, TradeResult
from data_manager import save_trade_history
from execution.gate_adapter import GateExecutionAdapter
from services.portfolio_service import PortfolioService
from strategies.positions import (
    clear_positions_memory,
    get_position,
    set_position_field,
)
from strategies.short_math import (
    funding_cost_usdt,
    is_short,
    notional_usdt,
    unrealized_pnl,
)

SYMBOL = "FUNDROW/USDT"
TF = "4h"
ENTRY = 100.0
QTY = 10.0
COVER_PX = 90.0
RATE = 0.01
OPENED = "2026-09-12T04:00:00+00:00"
HOLD_HOURS = 8.0


class _FrozenDateTime(datetime):
    """Pin now() so 04:00Z → 12:00Z is exactly 8h of funding."""

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return datetime(2026, 9, 12, 12, 0, 0)
        return datetime(2026, 9, 12, 12, 0, 0, tzinfo=tz)


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
            "trading_mode": "live",
            "max_usdt_per_trade": 1000,
            "shorts": {
                "enabled": True,
                "allow_live": False,
                "leverage_default": 2,
                "leverage_cap": 2,
                "funding_rate_8h": RATE,
            },
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
            "live": {
                "execution": "shadow",
                "dry_run": False,
                "simulated_balance_usdt": 5000,
            },
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


def _adapter(monkeypatch, *, mode: str = "shadow", usdt_balance: float = 5000.0):
    _isolate_ledger(monkeypatch)
    cfg = _cost_cfg()
    captured: list[dict] = []
    monkeypatch.setattr(
        "execution.gate_adapter.record_live_trade",
        lambda rec: captured.append(rec),
    )
    adapter = GateExecutionAdapter(cfg, PortfolioService(cfg), mode=mode)
    adapter._exchange = _mock_exchange(SYMBOL)
    adapter._fetch_usdt_balance = lambda: usdt_balance
    return adapter, captured


def _short_order(*, leverage: float | None = 2.0, qty: float = QTY, usdt: float = 1000.0, price: float = ENTRY) -> TradeOrder:
    return TradeOrder(
        "SHORT",
        SYMBOL,
        price,
        qty,
        usdt_amount=usdt,
        leverage=leverage,
        signal="SHORT",
        source="manual",
    )


def _cover_order(*, qty: float = QTY, price: float = COVER_PX) -> TradeOrder:
    return TradeOrder(
        "COVER",
        SYMBOL,
        price,
        qty,
        signal="COVER",
        source="manual",
        leverage=2,
    )


def _stamp_entry_at(entry_at: str) -> None:
    set_position_field(SYMBOL, TF, "entry_at", entry_at)
    set_position_field(SYMBOL, TF, "first_buy_at", entry_at)


def _assert_exchange_untouched(ex):
    ex.create_order.assert_not_called()
    ex.create_market_buy_order.assert_not_called()
    ex.create_market_sell_order.assert_not_called()
    ex.set_leverage.assert_not_called()


def _open_shadow_short(adapter):
    short = adapter.execute(_short_order(), TF)
    assert short.executed, short.message
    pos = get_position(SYMBOL, TF)
    assert is_short(pos)
    qty = float(pos["amount"])
    entry = float(pos["average_entry"])
    return qty, entry


def test_shadow_cover_live_row_records_funding_when_computation_succeeds(monkeypatch):
    """Current code never copies funding_* onto rec → KeyError on rec['funding_unknown']."""
    monkeypatch.setattr("services.portfolio_service.datetime", _FrozenDateTime)
    adapter, captured = _adapter(monkeypatch)
    qty, entry = _open_shadow_short(adapter)
    _stamp_entry_at(OPENED)

    result = adapter.execute(_cover_order(qty=qty, price=COVER_PX), TF)
    assert result.executed, result.message
    _assert_exchange_untouched(adapter._exchange)

    pos = get_position(SYMBOL, TF)
    assert not (is_short(pos) and float(pos.get("amount") or 0) > 0)

    rec = captured[-1]
    assert rec["type"] == "COVER"
    assert rec["mode"] == "shadow"
    expected_fund = funding_cost_usdt(notional_usdt(qty, entry), HOLD_HOURS, RATE)
    raw_pnl = unrealized_pnl("short", qty, entry, float(rec["price"]))
    assert expected_fund == pytest.approx(10.0)
    assert rec["funding_unknown"] is False
    assert rec["funding_usdt"] == pytest.approx(expected_fund)
    assert rec["pnl"] == pytest.approx(raw_pnl - rec["funding_usdt"])

    buy = adapter.execute(
        TradeOrder("BUY", SYMBOL, ENTRY, 0, usdt_amount=1000.0, source="manual"), TF,
    )
    assert buy.executed, buy.message
    long_qty = float(get_position(SYMBOL, TF)["amount"])
    sell = adapter.execute(
        TradeOrder("SELL", SYMBOL, ENTRY, long_qty, signal="SELL_FULL", source="manual"), TF,
    )
    assert sell.executed, sell.message

    by_type = {t["type"]: t for t in captured}
    assert "funding_usdt" not in by_type["SHORT"]
    assert "funding_unknown" not in by_type["SHORT"]
    assert "funding_usdt" not in by_type["BUY"]
    assert "funding_unknown" not in by_type["BUY"]
    assert "funding_usdt" not in by_type["SELL"]
    assert "funding_unknown" not in by_type["SELL"]


def test_shadow_cover_live_row_marks_funding_unknown_when_entry_at_unparsable(monkeypatch):
    """Current code never copies funding_* onto rec → KeyError on rec['funding_unknown']."""
    adapter, captured = _adapter(monkeypatch)
    qty, entry = _open_shadow_short(adapter)
    _stamp_entry_at("not-a-timestamp")

    result = adapter.execute(_cover_order(qty=qty, price=COVER_PX), TF)
    assert result.executed, result.message
    _assert_exchange_untouched(adapter._exchange)

    rec = captured[-1]
    assert rec["type"] == "COVER"
    raw_pnl = unrealized_pnl("short", qty, entry, float(rec["price"]))
    assert rec["funding_unknown"] is True
    assert rec["funding_usdt"] is None
    assert rec["pnl"] == pytest.approx(raw_pnl)


def _sync_cover_with_local(monkeypatch, local: TradeResult) -> dict:
    """Drive `_sync_local_ledger` COVER with a stubbed `execute_cover` result."""
    adapter, captured = _adapter(monkeypatch)
    monkeypatch.setattr(
        adapter.portfolio, "execute_cover", lambda *a, **k: local
    )
    adapter._sync_local_ledger(_cover_order(), TF)
    assert captured, "record_live_trade was not called"
    rec = captured[-1]
    assert rec["type"] == "COVER"
    return rec


def test_sync_local_ledger_cover_non_executed_marks_funding_unknown(monkeypatch):
    """#388: default TradeResult (executed=False, (None, False)) is not 'known zero'."""
    rec = _sync_cover_with_local(
        monkeypatch, TradeResult(False, "COVER", SYMBOL)
    )
    assert rec["funding_usdt"] is None
    assert rec["funding_unknown"] is True


def test_sync_local_ledger_cover_executed_known_funding_unchanged(monkeypatch):
    """#388 regression: executed COVER with known funding still writes (1.23, False)."""
    rec = _sync_cover_with_local(
        monkeypatch,
        TradeResult(
            True, "COVER", SYMBOL, funding_usdt=1.23, funding_unknown=False
        ),
    )
    assert rec["funding_usdt"] == pytest.approx(1.23)
    assert rec["funding_unknown"] is False


def test_sync_local_ledger_cover_executed_unknown_funding_unchanged(monkeypatch):
    """#388 regression: executed COVER with (None, True) still writes (None, True)."""
    rec = _sync_cover_with_local(
        monkeypatch,
        TradeResult(
            True, "COVER", SYMBOL, funding_usdt=None, funding_unknown=True
        ),
    )
    assert rec["funding_usdt"] is None
    assert rec["funding_unknown"] is True

