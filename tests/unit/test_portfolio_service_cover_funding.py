"""#373: COVER funding errors must be logged and marked, not swallowed."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from core.config import BotConfig
from data_manager import load_trade_history, save_trade_history
from services.portfolio_service import PortfolioService
from strategies.positions import (
    clear_positions_memory,
    get_position,
    set_position_field,
    update_position,
)
from strategies.short_math import (
    funding_cost_usdt,
    is_short,
    notional_usdt,
    unrealized_pnl,
)

SYMBOL = "COV373/USDT"
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


def _cfg() -> BotConfig:
    return BotConfig(
        {
            "max_usdt_per_trade": 1000,
            "shorts": {
                "enabled": True,
                "leverage_default": 2,
                "leverage_cap": 5,
                "funding_rate_8h": RATE,
            },
        }
    )


@pytest.fixture
def cover_svc(monkeypatch):
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
    yield PortfolioService(_cfg())
    clear_positions_memory()


def _open_short(*, entry_at: str) -> None:
    update_position(SYMBOL, TF, "SHORT", ENTRY, QTY, leverage=2)
    set_position_field(SYMBOL, TF, "entry_at", entry_at)
    set_position_field(SYMBOL, TF, "first_buy_at", entry_at)


def _cover_row() -> dict:
    trades = load_trade_history()["trades"]
    covers = [t for t in trades if t.get("type") == "COVER"]
    assert covers, "expected a COVER trade record"
    return covers[-1]


def _warning_messages(mock_log) -> list[str]:
    out = []
    for args, kwargs in mock_log.call_args_list:
        level = kwargs.get("level")
        if level is None and len(args) >= 2:
            level = args[1]
        if str(level).upper() == "WARNING":
            out.append(str(args[0] if args else ""))
    return out


def _assert_cover_executed(result, *, expected_pnl: float) -> dict:
    assert result.executed
    assert result.pnl == pytest.approx(expected_pnl)
    pos = get_position(SYMBOL, TF)
    assert not (is_short(pos) and float(pos.get("amount") or 0) > 0)
    rec = _cover_row()
    assert rec["type"] == "COVER"
    assert rec["symbol"] == SYMBOL
    assert rec["price"] == pytest.approx(COVER_PX)
    assert rec["amount"] == pytest.approx(QTY)
    assert rec["pnl"] == pytest.approx(expected_pnl)
    assert "usdt_amount" in rec
    assert "margin_usdt" in rec
    assert "source" in rec
    assert "timestamp" in rec
    assert "cost_model" in rec
    return rec


def test_cover_records_funding_when_computation_succeeds(cover_svc, monkeypatch):
    """Old code never wrote funding_usdt/funding_unknown → KeyError on rec['funding_unknown']."""
    monkeypatch.setattr("services.portfolio_service.datetime", _FrozenDateTime)
    _open_short(entry_at=OPENED)
    expected_fund = funding_cost_usdt(notional_usdt(QTY, ENTRY), HOLD_HOURS, RATE)
    raw_pnl = unrealized_pnl("short", QTY, ENTRY, COVER_PX)
    expected_pnl = raw_pnl - expected_fund
    assert expected_fund == pytest.approx(10.0)
    assert expected_pnl == pytest.approx(90.0)

    result = cover_svc.execute_cover(SYMBOL, TF, COVER_PX, amount=QTY, source="manual")
    rec = _assert_cover_executed(result, expected_pnl=expected_pnl)

    assert rec["funding_unknown"] is False
    assert rec["funding_usdt"] == pytest.approx(expected_fund)
    assert rec["pnl"] == pytest.approx(raw_pnl - rec["funding_usdt"])


def test_cover_marks_funding_unknown_when_entry_at_unparsable(cover_svc):
    """Old inner `except Exception: hours = 0.0` never logged and never set funding_unknown."""
    _open_short(entry_at="not-a-timestamp")
    expected_pnl = unrealized_pnl("short", QTY, ENTRY, COVER_PX)

    with patch("services.portfolio_service.log") as mock_log:
        result = cover_svc.execute_cover(SYMBOL, TF, COVER_PX, amount=QTY, source="manual")

    rec = _assert_cover_executed(result, expected_pnl=expected_pnl)
    assert rec["funding_usdt"] is None
    assert rec["funding_unknown"] is True

    warnings = _warning_messages(mock_log)
    assert warnings, "old code swallowed the parse error with no WARNING"
    msg = warnings[-1]
    assert SYMBOL in msg
    assert TF in msg
    assert "treated as 0" in msg
    assert "ValueError" in msg


def test_cover_marks_funding_unknown_when_funding_cost_raises(cover_svc):
    """Old outer `except Exception: pass` recorded COVER with no marker and no log."""
    _open_short(entry_at=OPENED)
    expected_pnl = unrealized_pnl("short", QTY, ENTRY, COVER_PX)

    with patch(
        "strategies.short_math.funding_cost_usdt",
        side_effect=RuntimeError("bad funding_rate_8h"),
    ), patch("services.portfolio_service.log") as mock_log:
        result = cover_svc.execute_cover(SYMBOL, TF, COVER_PX, amount=QTY, source="manual")

    rec = _assert_cover_executed(result, expected_pnl=expected_pnl)
    assert rec["funding_usdt"] is None
    assert rec["funding_unknown"] is True

    warnings = _warning_messages(mock_log)
    assert warnings, "old code swallowed funding_cost_usdt with except Exception: pass"
    msg = warnings[-1]
    assert SYMBOL in msg
    assert TF in msg
    assert "treated as 0" in msg
    assert "RuntimeError" in msg
    assert "bad funding_rate_8h" in msg
