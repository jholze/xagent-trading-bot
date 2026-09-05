"""Daily risk counters derived from one orders document must match the five old paths (#304)."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from core.config import BotConfig
from core.models import TradeOrder
from risk.risk_manager import RiskManager

NOW = datetime(2026, 9, 5, 12, 0, 0)


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return NOW
        return NOW.replace(tzinfo=tz)


def _cfg(
    *,
    max_daily_dca_buys: int = 0,
    max_daily_dca_usdt: float = 0.0,
    max_daily_sells: int = 0,
) -> BotConfig:
    cfg = BotConfig()
    risk = {
        "max_daily_dca_buys": max_daily_dca_buys,
        "max_daily_dca_usdt": max_daily_dca_usdt,
        "max_daily_sells": max_daily_sells,
        "cash_policy": {"enabled": False},
        "position_capacity": {"enabled": False},
        "slot_eviction": {"enabled": False},
        "venue_quality": {"enabled": False},
        "min_trade_usdt": 1.0,
        "max_daily_loss_pct": 0,
        "cash_floor_pct": 0,
    }
    cfg._raw = {
        "trading_mode": "paper",
        "max_daily_trades": 10,
        "max_daily_sells": max_daily_sells,
        "max_usdt_per_trade": 1000,
        "max_position_percent": 80,
        "max_open_positions": 50,
        "risk": risk,
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


def _buy_order() -> TradeOrder:
    return TradeOrder(
        type="BUY",
        symbol="AAA/USDT",
        price=1.0,
        amount=0,
        usdt_amount=20.0,
        signal="BUY_DCA",
        source="dca",
    )


def _sell_order() -> TradeOrder:
    return TradeOrder(
        type="SELL",
        symbol="AAA/USDT",
        price=1.0,
        amount=50.0,
        signal="SELL_FULL",
        source="manual",
    )


@contextmanager
def _patch_load_orders(doc_or_fn):
    calls = []

    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        if callable(doc_or_fn):
            return doc_or_fn()
        return doc_or_fn

    with patch("data_manager.load_orders", side_effect=fake):
        yield calls


@contextmanager
def _buy_dca_eval_env(rm: RiskManager):
    pos = {"amount": 1.0, "average_entry": 1.0, "sold_percent": 0}
    with ExitStack() as stack:
        stack.enter_context(patch("risk.risk_manager.get_position", return_value=pos))
        stack.enter_context(
            patch("risk.risk_manager.find_open_position_for_symbol", return_value=None)
        )
        stack.enter_context(patch("risk.risk_manager.count_open_full_slots", return_value=0))
        stack.enter_context(patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")))
        stack.enter_context(patch.object(rm, "_cash_floor_blocked", return_value=None))
        stack.enter_context(patch.object(rm, "_portfolio_equity", return_value=100_000.0))
        stack.enter_context(patch.object(rm, "_spendable_usdt", return_value=50_000.0))
        stack.enter_context(patch.object(rm, "_available_usdt", return_value=50_000.0))
        stack.enter_context(patch.object(rm, "_equity_drawdown_pct", return_value=0.0))
        stack.enter_context(patch.object(rm, "_daily_loss_limit_blocked", return_value=None))
        stack.enter_context(
            patch("strategies.position_lock.dca_blocked", return_value=(False, ""))
        )
        stack.enter_context(
            patch(
                "services.market_policy_fusion.get_global_market_bias",
                return_value={"active": False, "apply_size_mult": False},
            )
        )
        yield


@contextmanager
def _sell_eval_env(rm: RiskManager):
    pos = {"amount": 50.0, "average_entry": 1.0, "sold_percent": 0}
    with ExitStack() as stack:
        stack.enter_context(patch("risk.risk_manager.get_position", return_value=pos))
        stack.enter_context(
            patch("risk.risk_manager.find_open_position_for_symbol", return_value=None)
        )
        stack.enter_context(patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")))
        stack.enter_context(patch.object(rm, "_partial_sell_blocked", return_value=(False, "")))
        stack.enter_context(
            patch("strategies.position_lock.auto_sell_blocked", return_value=(False, ""))
        )
        stack.enter_context(
            patch(
                "strategies.position_lock.attach_lock_from_ledger",
                side_effect=lambda p, *a, **k: p,
            )
        )
        yield


def _spy_method(rm: RiskManager, name: str, seen: dict):
    orig = getattr(rm, name)

    def wrapped(*args, **kwargs):
        val = orig(*args, **kwargs)
        seen[name] = val
        return val

    setattr(rm, name, wrapped)


def test_evaluate_buy_loads_orders_document_once():
    """DCA buy hits buy-limit and DCA-USDT guards; both must share one load (#304)."""
    rm = RiskManager(
        _cfg(max_daily_dca_buys=10, max_daily_dca_usdt=10_000.0)
    )
    doc = _synthetic_doc()
    with patch("risk.risk_manager.datetime", _FrozenDateTime), _patch_load_orders(
        doc
    ) as calls, _buy_dca_eval_env(rm):
        decision = rm.evaluate(_buy_order(), "4h", source="dca")
    assert decision.approved, decision.message
    assert len(calls) == 1


def test_evaluate_sell_loads_orders_document_once():
    rm = RiskManager(_cfg(max_daily_sells=10))
    doc = _synthetic_doc()
    with patch("risk.risk_manager.datetime", _FrozenDateTime), _patch_load_orders(
        doc
    ) as calls, _sell_eval_env(rm):
        decision = rm.evaluate(_sell_order(), "4h", source="manual")
    assert decision.approved, decision.message
    assert len(calls) == 1


def test_evaluate_buy_guards_see_counters_from_same_document():
    rm = RiskManager(
        _cfg(max_daily_dca_buys=10, max_daily_dca_usdt=10_000.0)
    )
    doc = _synthetic_doc()
    seen = {}
    _spy_method(rm, "_daily_dca_buys_count", seen)
    _spy_method(rm, "_daily_dca_usdt_sum", seen)
    with patch("risk.risk_manager.datetime", _FrozenDateTime), _patch_load_orders(
        doc
    ), _buy_dca_eval_env(rm):
        expected = rm._daily_counters_from_orders(doc)
        decision = rm.evaluate(_buy_order(), "4h", source="dca")
    assert decision.approved, decision.message
    assert seen["_daily_dca_buys_count"] == expected["daily_dca_buys"]
    assert seen["_daily_dca_usdt_sum"] == pytest.approx(expected["daily_dca_usdt"])


def test_evaluate_sell_guards_see_counters_from_same_document():
    rm = RiskManager(_cfg(max_daily_sells=10))
    doc = _synthetic_doc()
    seen = {}
    _spy_method(rm, "_daily_sells_count", seen)
    with patch("risk.risk_manager.datetime", _FrozenDateTime), _patch_load_orders(
        doc
    ), _sell_eval_env(rm):
        expected = rm._daily_counters_from_orders(doc)
        decision = rm.evaluate(_sell_order(), "4h", source="manual")
    assert decision.approved, decision.message
    assert seen["_daily_sells_count"] == expected["daily_sells"]


def test_consecutive_evaluate_calls_see_fill_recorded_in_between():
    rm = RiskManager(_cfg(max_daily_sells=1))
    empty = {"orders": []}
    filled = {
        "orders": [_filled("sell", 1.0, usdt=10.0)],
    }
    docs = [empty, filled]

    def next_doc(*_a, **_k):
        return docs.pop(0)

    with patch("risk.risk_manager.datetime", _FrozenDateTime), _patch_load_orders(
        next_doc
    ), _sell_eval_env(rm):
        first = rm.evaluate(_sell_order(), "4h", source="manual")
        second = rm.evaluate(_sell_order(), "4h", source="manual")
    assert first.approved, first.message
    assert not second.approved
    assert second.code == "max_daily_sells"


def test_eval_orders_doc_cleared_after_evaluate_returns():
    rm = RiskManager(_cfg(max_daily_sells=10))
    during = []
    orig = rm._evaluate_impl

    def wrap(*args, **kwargs):
        during.append(getattr(rm, "_eval_orders_doc", None))
        return orig(*args, **kwargs)

    rm._evaluate_impl = wrap
    with patch("risk.risk_manager.datetime", _FrozenDateTime), _patch_load_orders(
        _synthetic_doc()
    ), _sell_eval_env(rm):
        decision = rm.evaluate(_sell_order(), "4h", source="manual")
    assert decision.approved, decision.message
    assert during and during[0] is not None
    assert getattr(rm, "_eval_orders_doc", None) is None


def test_eval_orders_doc_cleared_after_evaluate_raises():
    rm = RiskManager(_cfg())
    during = []

    def boom(*_a, **_k):
        during.append(getattr(rm, "_eval_orders_doc", None))
        raise RuntimeError("eval-boom")

    rm._evaluate_impl = boom
    with patch("risk.risk_manager.datetime", _FrozenDateTime), _patch_load_orders(
        _synthetic_doc()
    ):
        with pytest.raises(RuntimeError, match="eval-boom"):
            rm.evaluate(_sell_order(), "4h", source="manual")
    assert during and during[0] is not None
    assert getattr(rm, "_eval_orders_doc", None) is None


def test_nested_evaluate_does_not_reuse_outer_orders_document():
    rm = RiskManager(_cfg(max_daily_sells=10))
    outer = {"orders": [_filled("sell", 1.0)], "tag": "outer"}
    inner = {
        "orders": [_filled("sell", 1.0), _filled("sell", 2.0)],
        "tag": "inner",
    }
    queue = [outer, inner]
    seen_tags = []
    orig = rm._evaluate_impl

    def wrap(*args, **kwargs):
        doc = getattr(rm, "_eval_orders_doc", None) or {}
        seen_tags.append(doc.get("tag"))
        if len(seen_tags) == 1:
            nested = rm.evaluate(_sell_order(), "4h", source="manual")
            assert nested.approved, nested.message
            assert (getattr(rm, "_eval_orders_doc", None) or {}).get("tag") == "outer"
        return orig(*args, **kwargs)

    rm._evaluate_impl = wrap
    with patch("risk.risk_manager.datetime", _FrozenDateTime), _patch_load_orders(
        lambda *_a, **_k: queue.pop(0)
    ) as calls, _sell_eval_env(rm):
        decision = rm.evaluate(_sell_order(), "4h", source="manual")
    assert decision.approved, decision.message
    assert seen_tags == ["outer", "inner"]
    assert len(calls) == 2
    assert getattr(rm, "_eval_orders_doc", None) is None
