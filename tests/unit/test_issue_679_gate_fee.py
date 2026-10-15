"""#679 Gate futures fee estimate, delta booking, short PnL, missing-pnl block.

No network. Ledger writes stay on the per-test tmp data dir.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from core.config import BotConfig
from core.models import OrderStatus, TradeOrder
from core.tenant_context import tenant_context
from data_manager import load_trade_history, save_orders
from execution.gate_adapter import GateExecutionAdapter
from execution.recovery import _resolve_in_flight_orders
from risk.risk_manager import RiskManager
from services.order_service import OrderService
from services.portfolio_service import PortfolioService
from strategies.positions import (
    clear_positions_memory,
    get_position,
    load_positions,
    save_positions,
    update_position,
)
from strategies.short_math import is_short, unrealized_pnl

SYMBOL = "IDEM/USDT"
TF = "4h"
RATE = 0.00075


def _cfg(**swap_overrides) -> BotConfig:
    swap = {
        "fee_maker_pct": 0.02,
        "fee_taker_pct": 0.05,
        "fee_taker_conservative_pct": 0.075,
        "slippage_pct": 0.0,
        "fee_side_buy": "quote",
        "fee_side_sell": "quote",
    }
    swap.update(swap_overrides)
    return BotConfig(
        {
            "trading_mode": "paper",
            "max_usdt_per_trade": 5000,
            "max_open_positions": 20,
            "initial_capital_usdt": 100000,
            "shorts": {
                "enabled": True,
                "allow_live": False,
                "leverage_default": 2,
                "leverage_cap": 2,
                "max_open": 6,
                "max_margin_pct": 50,
                "funding_rate_8h": 0.0,
                "volatile": {"market_cap_min_usd": 0},
            },
            "risk": {"max_daily_loss_pct": 0, "max_daily_sells": 0},
            "costs": {
                "fee_source": "config",
                "gate": {
                    "spot": {
                        "fee_maker_pct": 0.2,
                        "fee_taker_pct": 0.2,
                        "slippage_pct": 0.0,
                        "fee_side_buy": "base",
                        "fee_side_sell": "quote",
                    },
                    "swap": swap,
                },
            },
        }
    )


def _adapter(cfg: BotConfig | None = None) -> tuple[GateExecutionAdapter, MagicMock]:
    cfg = cfg or _cfg()
    adapter = GateExecutionAdapter(cfg, PortfolioService(cfg), mode="real")
    ex = MagicMock(name="ccxt.gate")
    ex.fetch_my_trades.return_value = []
    ex.fetch_order.return_value = {}
    adapter._exchange = ex
    return adapter, ex


def _rows(order_id: str, typ: str) -> list[dict]:
    history = load_trade_history() or {}
    return [
        row
        for row in history.get("trades") or []
        if str(row.get("order_id") or "") == order_id
        and str(row.get("type") or "").upper() == typ
    ]


def _closed_raw(*, filled: float, oid: str, average: float = 100.0, fee: dict | None, status: str = "closed", cost: float | None = None) -> dict:
    raw = {
        "id": oid,
        "status": status,
        "filled": filled,
        "average": average,
        "cost": cost if cost is not None else filled * average,
    }
    if fee is not None:
        raw["fee"] = fee
    return raw


def _active_order(svc: OrderService, *, typ: str, symbol: str, qty: float, price: float, usdt: float, ex_id: str) -> dict:
    rec = svc.create_from_request(
        TradeOrder(
            typ,
            symbol,
            price,
            qty,
            usdt_amount=usdt,
            source="manual",
            exchange_order_id=ex_id,
            leverage=2,
        ),
        status=OrderStatus.ACTIVE,
        timeframe=TF,
    )
    data = svc._load()
    found = svc._find(data, order_id=rec["id"])
    found["needs_reconcile"] = True
    found["exchange_order_id"] = ex_id
    found["qty"] = qty
    svc._save(data)
    return svc.get_by_id(rec["id"])


def _resolve_twice(adapter, ex, rec: dict) -> None:
    cfg = adapter.config

    def _once():
        fresh = OrderService("demo").get_by_id(rec["id"])
        _resolve_in_flight_orders(
            [fresh],
            exchange=ex,
            adapter=adapter,
            config=cfg,
            scope="demo",
        )

    _once()
    _once()


def test_t1_short_and_cover_booked_once_across_two_resolves():
    adapter, ex = _adapter()
    symbol = "IDEM/USDT"
    with tenant_context("default", scope="demo"):
        svc = OrderService("demo")
        short = _active_order(
            svc, typ="SHORT", symbol=symbol, qty=10, price=100, usdt=1000, ex_id="ex-short",
        )
        ex.fetch_order.return_value = _closed_raw(
            filled=10, oid="ex-short", fee={"cost": None, "currency": "USDT"},
        )
        ex.fetch_my_trades.return_value = [
            {
                "order": "ex-short",
                "cost": 1000.0,
                "price": 100.0,
                "takerOrMaker": "taker",
                "fee": {"cost": None, "currency": "USDT"},
            }
        ]
        _resolve_twice(adapter, ex, short)
        lot = get_position(symbol, TF)
        assert is_short(lot)
        assert float(lot["amount"]) == pytest.approx(10)
        assert len(_rows(short["id"], "SHORT")) == 1

        cover = _active_order(
            svc, typ="COVER", symbol=symbol, qty=10, price=90, usdt=900, ex_id="ex-cover",
        )
        ex.fetch_order.return_value = _closed_raw(
            filled=10, oid="ex-cover", average=90, fee={"cost": None, "currency": "USDT"}, cost=900,
        )
        ex.fetch_my_trades.return_value = [
            {
                "order": "ex-cover",
                "cost": 900.0,
                "price": 90.0,
                "takerOrMaker": "taker",
                "fee": {"cost": None, "currency": "USDT"},
            }
        ]
        _resolve_twice(adapter, ex, cover)
        lot = get_position(symbol, TF)
        assert float(lot.get("amount") or 0) == pytest.approx(0)
        assert len(_rows(cover["id"], "COVER")) == 1


def test_t1b_partial_then_remainder_and_duplicate_cumulative():
    adapter, _ex = _adapter()
    symbol = "PART/USDT"
    order = TradeOrder("SHORT", symbol, 100.0, 10.0, usdt_amount=1000.0, source="manual", order_id="part-1")
    with tenant_context("default", scope="demo"):
        first = _closed_raw(
            filled=4, oid="ex-part", status="open", fee={"cost": 0.30, "currency": "USDT"},
        )
        adapter._finalize_exchange_order(
            _ex, order, first, side="sell", qty=10, timeframe=TF,
        )
        second = _closed_raw(
            filled=10, oid="ex-part", status="closed", fee={"cost": 0.75, "currency": "USDT"},
        )
        adapter._finalize_exchange_order(
            _ex, order, second, side="sell", qty=10, timeframe=TF,
        )
        lot = get_position(symbol, TF)
        assert float(lot["amount"]) == pytest.approx(10)
        assert len(_rows("part-1", "SHORT")) == 2

        again = TradeOrder(
            "SHORT", "DUP/USDT", 50.0, 8.0, usdt_amount=400.0, source="manual", order_id="dup-1",
        )
        raw = _closed_raw(filled=8, oid="ex-dup", average=50, fee={"cost": 0.2, "currency": "USDT"})
        adapter._finalize_exchange_order(_ex, again, raw, side="sell", qty=8, timeframe=TF)
        adapter._finalize_exchange_order(_ex, again, dict(raw), side="sell", qty=8, timeframe=TF)
        dup = get_position("DUP/USDT", TF)
        assert float(dup["amount"]) == pytest.approx(8)
        assert len(_rows("dup-1", "SHORT")) == 1


def _finalize_null_fee(adapter, *, cost: float, role: str, symbol: str = "NULL/USDT") -> object:
    ex = adapter._exchange
    order = TradeOrder(
        "SHORT", symbol, 100.0, cost / 100.0, usdt_amount=cost, source="manual", order_id="null-1",
    )
    raw = _closed_raw(
        filled=cost / 100.0,
        oid="ex-null",
        fee={"cost": None, "currency": "USDT"},
        cost=cost,
    )
    ex.fetch_my_trades.return_value = [
        {
            "order": "ex-null",
            "cost": cost,
            "price": 100.0,
            "takerOrMaker": role,
            "fee": {"cost": None, "currency": "USDT"},
        }
    ]
    return adapter._finalize_exchange_order(
        ex, order, raw, side="sell", qty=cost / 100.0, timeframe=TF,
    )


def test_t2_null_fee_uses_conservative_and_max_rule():
    adapter, _ex = _adapter()
    cost = 1000.0
    with tenant_context("default", scope="demo"):
        result = _finalize_null_fee(adapter, cost=cost, role="taker", symbol="NULLA/USDT")
    assert result.fee == pytest.approx(cost * RATE)
    assert result.fee_estimated is True
    assert result.fee_rate_source == "config_conservative"
    assert result.fee_rate == pytest.approx(RATE)
    assert result.needs_reconcile is True

    higher, _ex_hi = _adapter(_cfg(fee_taker_pct=0.20))
    with tenant_context("default", scope="demo"):
        clear_positions_memory()
        result_hi = _finalize_null_fee(higher, cost=cost, role="taker", symbol="NULLB/USDT")
    assert result_hi.fee == pytest.approx(cost * 0.002)
    assert result_hi.fee_rate_source == "config_fallback"
    assert result_hi.fee_estimated is True
    assert result_hi.needs_reconcile is True

    exchanged, _ex_x = _adapter()
    exchanged._exchange_swap_fee_fractions = lambda: (0.002, -0.0001)
    with tenant_context("default", scope="demo"):
        clear_positions_memory()
        result_ex = _finalize_null_fee(exchanged, cost=cost, role="taker", symbol="NULLC/USDT")
    assert result_ex.fee == pytest.approx(cost * 0.002)
    assert result_ex.fee_rate_source == "exchange"


def test_t8_maker_fill_ignores_exchange_maker_rate():
    cost = 1000.0
    adapter, _ex = _adapter()
    adapter._exchange_swap_fee_fractions = lambda: (0.0002, -0.0001)
    with tenant_context("default", scope="demo"):
        result = _finalize_null_fee(adapter, cost=cost, role="maker", symbol="MAKER/USDT")
    assert result.fee == pytest.approx(cost * RATE)
    assert result.fee_rate_source == "config_conservative"
    assert result.fee_estimated is True

    # A maker rate above every taker candidate must still lose. Otherwise M7
    # (adding the maker rate to the max) cannot change the -0.01% case.
    high, _ex_hi = _adapter()
    high._exchange_swap_fee_fractions = lambda: (0.0002, 0.01)
    with tenant_context("default", scope="demo"):
        clear_positions_memory()
        result_hi = _finalize_null_fee(high, cost=cost, role="maker", symbol="MAKER2/USDT")
    assert result_hi.fee == pytest.approx(cost * RATE)
    assert result_hi.fee_rate_source == "config_conservative"


def test_t3_no_positive_rate_stays_fee_unknown(monkeypatch):
    logs: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "execution.gate_adapter.log",
        lambda msg, level="INFO": logs.append((level, str(msg))),
    )
    missing, _ex = _adapter(_cfg())
    # Drop both rate keys and also cover an explicit zero.
    raw = missing.config.raw
    raw["costs"]["gate"]["swap"].pop("fee_taker_conservative_pct", None)
    raw["costs"]["gate"]["swap"].pop("fee_taker_pct", None)
    with tenant_context("default", scope="demo"):
        result = _finalize_null_fee(missing, cost=800.0, role="taker", symbol="NORATE/USDT")
    assert result.fee_unknown is True
    assert result.needs_reconcile is True
    assert result.fee_estimated is not True
    assert any(
        level == "ERROR" and "futures_fee_unresolved" in msg and "order_id=" in msg and "symbol=" in msg and "tenant=" in msg
        for level, msg in logs
    )

    logs.clear()
    zeroed, _ex0 = _adapter(_cfg(fee_taker_pct=0, fee_taker_conservative_pct=0))
    with tenant_context("default", scope="demo"):
        clear_positions_memory()
        result0 = _finalize_null_fee(zeroed, cost=800.0, role="taker", symbol="ZERORATE/USDT")
    assert result0.fee_unknown is True
    assert result0.needs_reconcile is True
    assert any(level == "ERROR" and "futures_fee_unresolved" in msg for level, msg in logs)


def test_t4_recovery_keeps_needs_reconcile_for_unknown_and_estimated():
    adapter, ex = _adapter()
    with tenant_context("default", scope="demo"):
        svc = OrderService("demo")
        unknown = _active_order(
            svc, typ="SHORT", symbol="UNK/USDT", qty=2, price=50, usdt=100, ex_id="ex-unk",
        )
        ex.fetch_order.return_value = _closed_raw(
            filled=2, oid="ex-unk", average=50, fee=None, cost=100,
        )
        ex.fetch_my_trades.return_value = []
        _resolve_in_flight_orders(
            [svc.get_by_id(unknown["id"])],
            exchange=ex,
            adapter=adapter,
            config=adapter.config,
            scope="demo",
        )
        stored = svc.get_by_id(unknown["id"])
        assert stored.get("needs_reconcile") is True
        assert stored.get("fee_unknown") is True

        estimated = _active_order(
            svc, typ="SHORT", symbol="EST/USDT", qty=2, price=50, usdt=100, ex_id="ex-est",
        )
        ex.fetch_order.return_value = _closed_raw(
            filled=2, oid="ex-est", average=50, fee={"cost": None}, cost=100,
        )
        ex.fetch_my_trades.return_value = [
            {
                "order": "ex-est",
                "cost": 100.0,
                "price": 50.0,
                "takerOrMaker": "taker",
                "fee": {"cost": None, "currency": "USDT"},
            }
        ]
        _resolve_in_flight_orders(
            [svc.get_by_id(estimated["id"])],
            exchange=ex,
            adapter=adapter,
            config=adapter.config,
            scope="demo",
        )
        stored_est = svc.get_by_id(estimated["id"])
        assert stored_est.get("needs_reconcile") is True
        assert stored_est.get("fee_estimated") is True


def test_t7_cover_pnl_nets_open_and_close_fees_including_partial():
    cfg = _cfg()
    svc = PortfolioService(cfg)
    symbol = "PNL/USDT"
    with tenant_context("default", scope="demo"):
        opened = svc.execute_short(
            symbol, TF, 100.0, usdt_amount=1000.0, source="manual", order_id="sh-fee",
            fee_usdt=1.5, fee_estimated=False,
        )
        assert opened.executed
        lot = get_position(symbol, TF)
        assert float(lot["open_fee_usdt"]) == pytest.approx(1.5)
        assert float(lot["average_entry"]) == pytest.approx(100.0)
        assert lot.get("open_fee_estimated") is not True

        covered = svc.execute_cover(
            symbol, TF, 90.0, amount=10.0, source="manual", order_id="cv-fee",
            fee_usdt=0.4,
        )
        gross = unrealized_pnl("short", 10.0, 100.0, 90.0)
        fund = float(covered.funding_usdt or 0)
        assert covered.pnl == pytest.approx(gross - fund - 1.5 - 0.4)
        assert covered.fee == pytest.approx(0.4)
        assert covered.open_fee_usdt == pytest.approx(1.5)
        row = _rows("cv-fee", "COVER")[-1]
        assert row["fee"] == pytest.approx(0.4)
        assert row["open_fee_usdt"] == pytest.approx(1.5)

        symbol_p = "PARTPNL/USDT"
        svc.execute_short(
            symbol_p, TF, 100.0, usdt_amount=1000.0, source="manual", order_id="sh-part",
            fee_usdt=1.0, fee_estimated=True, fee_rate_source="config_conservative", fee_rate=RATE,
        )
        lot_p = get_position(symbol_p, TF)
        assert float(lot_p["open_fee_usdt"]) == pytest.approx(1.0)
        assert lot_p.get("open_fee_estimated") is True
        assert float(lot_p["average_entry"]) == pytest.approx(100.0)
        # Demo reload rebuilds the lot from filled orders and overlays the
        # saved fee. Without the order, the cache-only lot is dropped.
        now = datetime.now().isoformat()
        save_orders(
            {
                "ledger_scope": "demo",
                "orders": [
                    {
                        "id": "sh-part",
                        "status": "filled",
                        "side": "short",
                        "symbol": symbol_p,
                        "timeframe": TF,
                        "ledger_scope": "demo",
                        "leverage": 2,
                        "execution": {"usdt": 1000.0, "price": 100.0, "amount": 10.0},
                        "request": {"amount": 10.0, "price": 100.0, "usdt": 1000.0, "leverage": 2},
                        "timestamps": {"created": now, "filled": now, "updated": now},
                    }
                ],
            },
            "demo",
        )
        from services import order_service

        order_service._ORDERS_READ_CACHE.clear()
        save_positions()
        clear_positions_memory()
        load_positions()
        reloaded = get_position(symbol_p, TF)
        assert float(reloaded["open_fee_usdt"]) == pytest.approx(1.0)
        assert reloaded.get("open_fee_estimated") is True

        partial = svc.execute_cover(
            symbol_p, TF, 80.0, amount=4.0, source="manual", order_id="cv-part",
            fee_usdt=0.2, fee_estimated=True, fee_rate_source="config_conservative", fee_rate=RATE,
        )
        gross_p = unrealized_pnl("short", 4.0, 100.0, 80.0)
        fund_p = float(partial.funding_usdt or 0)
        assert partial.open_fee_usdt == pytest.approx(0.4)
        assert partial.pnl == pytest.approx(gross_p - fund_p - 0.4 - 0.2)
        left = get_position(symbol_p, TF)
        assert float(left["amount"]) == pytest.approx(6)
        assert float(left["open_fee_usdt"]) == pytest.approx(0.6)
        assert left.get("open_fee_estimated") is True


def test_t5_brake_inputs_include_estimated_fees():
    cfg = _cfg()
    svc = PortfolioService(cfg)
    symbol = "BRAKE/USDT"
    with tenant_context("default", scope="demo"):
        svc.execute_short(
            symbol, TF, 100.0, usdt_amount=1000.0, source="manual", order_id="br-s",
            fee_usdt=0.75, fee_estimated=True, fee_rate_source="config_conservative", fee_rate=RATE,
        )
        covered = svc.execute_cover(
            symbol, TF, 100.0, amount=10.0, source="manual", order_id="br-c",
            fee_usdt=0.75, fee_estimated=True, fee_rate_source="config_conservative", fee_rate=RATE,
        )
        gross = unrealized_pnl("short", 10.0, 100.0, 100.0)
        fund = float(covered.funding_usdt or 0)
        assert covered.pnl == pytest.approx(gross - fund - 0.75 - 0.75)
        assert covered.pnl < gross - fund
        now = datetime.now().isoformat()
        save_orders(
            {
                "ledger_scope": "demo",
                "orders": [
                    {
                        "id": "br-c",
                        "status": "filled",
                        "side": "cover",
                        "symbol": symbol,
                        "ledger_scope": "demo",
                        "pnl": covered.pnl,
                        "fee_estimated": True,
                        "open_fee_usdt": 0.75,
                        "execution": {"usdt": 1000.0, "fee": 0.75, "price": 100.0, "amount": 10.0},
                        "timestamps": {"created": now, "filled": now, "updated": now},
                    }
                ],
            },
            "demo",
        )
        from services import order_service

        order_service._ORDERS_READ_CACHE.clear()
        rm = RiskManager(cfg)
        assert rm._trailing_24h_realized_pnl() == pytest.approx(covered.pnl)
        assert rm._realized_pnl_operator_day() == pytest.approx(covered.pnl)


def test_t10_base_fee_converts_at_fill_price():
    adapter, ex = _adapter()
    symbol = "BASEX/USDT"
    price = 100.0
    qty_fee = 0.5
    order = TradeOrder(
        "SHORT", symbol, price, 2.0, usdt_amount=200.0, source="manual", order_id="base-1",
    )
    raw = _closed_raw(filled=2, oid="ex-base", average=price, fee=None, cost=200)
    ex.fetch_my_trades.return_value = [
        {
            "order": "ex-base",
            "cost": 200.0,
            "price": price,
            "amount": 2.0,
            "takerOrMaker": "taker",
            "fee": {"cost": qty_fee, "currency": "BASEX"},
        }
    ]
    with tenant_context("default", scope="demo"):
        result = adapter._finalize_exchange_order(
            ex, order, raw, side="sell", qty=2, timeframe=TF,
        )
    assert result.fee_unknown is False
    assert result.fee == pytest.approx(qty_fee * price)


def test_t11_gt_and_point_do_not_mix_out_usdt_fee(monkeypatch):
    logs: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "execution.gate_adapter.log",
        lambda msg, level="INFO": logs.append((level, str(msg))),
    )
    adapter, ex = _adapter()
    symbol = "GTFEE/USDT"
    order = TradeOrder(
        "SHORT", symbol, 100.0, 2.0, usdt_amount=200.0, source="manual", order_id="gt-1",
    )
    raw = _closed_raw(filled=2, oid="ex-gt", fee=None, cost=200)
    ex.fetch_my_trades.return_value = [
        {"order": "ex-gt", "cost": 200.0, "price": 100.0, "fee": {"cost": 0.2, "currency": "USDT"}},
        {"order": "ex-gt", "cost": 200.0, "price": 100.0, "fee": {"cost": 0.01, "currency": "GT"}},
        {"order": "ex-gt", "cost": 0.0, "price": 100.0, "fee": {"cost": 1.0, "currency": "GATEPOINT"}},
    ]
    with tenant_context("default", scope="demo"):
        result = adapter._finalize_exchange_order(
            ex, order, raw, side="sell", qty=2, timeframe=TF,
        )
    assert result.fee_unknown is False
    assert result.fee == pytest.approx(0.2)
    assert result.fee_estimated is not True

    only, only_ex = _adapter()
    only_order = TradeOrder(
        "SHORT", "GTONLY/USDT", 100.0, 2.0, usdt_amount=200.0, source="manual", order_id="gt-2",
    )
    only_ex.fetch_my_trades.return_value = [
        {
            "order": "ex-gtonly",
            "cost": 200.0,
            "price": 100.0,
            "takerOrMaker": "taker",
            "fee": {"cost": 0.05, "currency": "GT"},
        }
    ]
    raw_only = _closed_raw(filled=2, oid="ex-gtonly", fee=None, cost=200)
    with tenant_context("default", scope="demo"):
        estimated = only._finalize_exchange_order(
            only_ex, only_order, raw_only, side="sell", qty=2, timeframe=TF,
        )
    assert estimated.fee_estimated is True
    assert estimated.fee_rate_source == "config_conservative"
    assert estimated.fee == pytest.approx(200.0 * RATE)
    assert estimated.needs_reconcile is True
    assert any("futures_fee_non_usdt" in msg for _level, msg in logs)


def _stamp_missing_pnl(order_id: str = "miss-1", pnl=None) -> None:
    from services import order_service

    now = datetime.now().isoformat()
    order_service._ORDERS_READ_CACHE.clear()
    save_orders(
        {
            "ledger_scope": "demo",
            "orders": [
                {
                    "id": order_id,
                    "status": "filled",
                    "side": "cover",
                    "symbol": "MISS/USDT",
                    "ledger_scope": "demo",
                    "pnl": pnl,
                    "execution": {"usdt": 500.0, "price": 10.0, "amount": 50.0},
                    "timestamps": {"created": now, "filled": now, "updated": now},
                }
            ],
        },
        "demo",
    )
    order_service._ORDERS_READ_CACHE.clear()


def test_t12_missing_pnl_blocks_entries_not_exits_and_lifts():
    cfg = _cfg()
    rm = RiskManager(cfg)
    with tenant_context("default", scope="demo"):
        _stamp_missing_pnl(pnl=None)
        update_position("LONGX/USDT", TF, "BUY", 1.0, 20)
        update_position("SHRTX/USDT", TF, "SHORT", 1.0, 8, leverage=2)
        buy = rm.evaluate(
            TradeOrder("BUY", "NEWX/USDT", 1.0, 0, usdt_amount=25, source="manual"),
            TF,
            source="manual",
        )
        assert buy.approved is False
        assert buy.code == "pnl_missing"
        short = rm.evaluate(
            TradeOrder("SHORT", "NEWSH/USDT", 1.0, 0, usdt_amount=50, source="manual", leverage=2),
            TF,
            source="manual",
        )
        assert short.approved is False
        assert short.code == "pnl_missing"

        sell = rm.evaluate(
            TradeOrder("SELL", "LONGX/USDT", 1.1, 5, signal="SELL", source="manual"),
            TF,
            source="manual",
        )
        assert sell.code != "pnl_missing"
        assert sell.approved is True, sell.message

        stop = rm.evaluate(
            TradeOrder(
                "SELL",
                "LONGX/USDT",
                0.8,
                5,
                signal="SELL_STOP_FULL",
                source="auto",
                exit_source="stop_loss",
            ),
            TF,
            source="auto",
        )
        assert stop.code != "pnl_missing"
        assert stop.approved is True, stop.message

        cover = rm.evaluate(
            TradeOrder("COVER", "SHRTX/USDT", 0.9, 4, source="manual"),
            TF,
            source="manual",
        )
        assert cover.code != "pnl_missing"
        assert cover.approved is True, cover.message

        _stamp_missing_pnl(pnl=1.0)
        lifted = rm.evaluate(
            TradeOrder("BUY", "NEWX/USDT", 1.0, 0, usdt_amount=25, source="manual"),
            TF,
            source="manual",
        )
        assert lifted.code != "pnl_missing"
        assert lifted.approved is True, lifted.message
