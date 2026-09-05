"""Daily risk counters derived from one orders document must match the five old paths (#304)."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from core.config import BotConfig
from risk.risk_manager import RiskManager

NOW = datetime(2026, 9, 5, 12, 0, 0)


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return NOW
        return NOW.replace(tzinfo=tz)


def _cfg(*, max_daily_dca_buys: int = 0) -> BotConfig:
    cfg = BotConfig()
    cfg._raw = {
        "trading_mode": "paper",
        "max_daily_trades": 10,
        "risk": {
            "max_daily_dca_buys": max_daily_dca_buys,
            "cash_policy": {"enabled": False},
            "position_capacity": {"enabled": False},
        },
    }
    return cfg


def _filled(
    side: str,
    hours_ago: float,
    *,
    source: str = "auto",
    signal: str = "",
    usdt: float | None = 10.0,
    status: str = "filled",
    ts_field: str = "filled",
    type_field: str | None = None,
    ts_raw: str | None = None,
) -> dict:
    ts = ts_raw if ts_raw is not None else (NOW - timedelta(hours=hours_ago)).isoformat()
    order = {
        "status": status,
        "side": side,
        "source": source,
        "signal": signal,
        "timestamps": {ts_field: ts},
    }
    if type_field is not None:
        order["type"] = type_field
        if not side:
            order.pop("side", None)
    if usdt is not None:
        order["risk"] = {"approved_usdt": usdt}
    return order


def _synthetic_doc() -> dict:
    return {
        "orders": [
            _filled("buy", 1.0, usdt=40.0),
            _filled("buy", 2.0, source="dca", usdt=25.5),
            _filled("buy", 3.0, source="auto", signal="BUY_DCA", usdt=12.0),
            _filled("sell", 4.0, usdt=99.0),
            _filled("sell", 5.0, type_field="SELL", usdt=1.0),
            _filled("buy", 30.0, usdt=1000.0),
            _filled("buy", 0.5, status="open", usdt=8.0),
            _filled("buy", 0.2, usdt=7.0, ts_raw="not-a-timestamp"),
            _filled("", 1.5, type_field="BUY", usdt=5.0),
            _filled("buy", 6.0, source="dca_sniper", usdt=3.25),
        ]
    }


def _old_five(rm: RiskManager) -> dict:
    dca_only = False if rm._dca_limits_enabled() else None
    return {
        "daily_trades": rm._daily_trades_count(),
        "daily_buys": rm._daily_buys_count(dca_only=dca_only),
        "daily_dca_buys": rm._daily_dca_buys_count(),
        "daily_dca_usdt": rm._daily_dca_usdt_sum(),
        "daily_sells": rm._daily_sells_count(),
    }


@pytest.mark.parametrize("max_daily_dca_buys", [0, 4])
def test_daily_counters_from_orders_match_five_old_paths(max_daily_dca_buys):
    rm = RiskManager(_cfg(max_daily_dca_buys=max_daily_dca_buys))
    doc = _synthetic_doc()
    with patch("risk.risk_manager.datetime", _FrozenDateTime), patch.object(
        rm, "_load_orders_document", return_value=doc
    ):
        old = _old_five(rm)
        new = rm._daily_counters_from_orders(doc)
    assert new == old
    assert old["daily_trades"] == 7
    assert old["daily_sells"] == 2
    assert old["daily_dca_buys"] == 3
    assert old["daily_dca_usdt"] == pytest.approx(25.5 + 12.0 + 3.25)
    if max_daily_dca_buys > 0:
        assert old["daily_buys"] == 2
    else:
        assert old["daily_buys"] == 5


def test_empty_orders_document_is_all_zeros():
    rm = RiskManager(_cfg(max_daily_dca_buys=2))
    with patch("risk.risk_manager.datetime", _FrozenDateTime):
        new = rm._daily_counters_from_orders({"orders": []})
        with patch.object(rm, "_load_orders_document", return_value={"orders": []}):
            old = _old_five(rm)
    assert new == old
    assert new == {
        "daily_trades": 0,
        "daily_buys": 0,
        "daily_dca_buys": 0,
        "daily_dca_usdt": 0,
        "daily_sells": 0,
    }
