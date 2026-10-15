"""#679 Gate futures fee estimate, delta booking, short PnL, missing-pnl block.

No network. Ledger writes stay on the per-test tmp data dir.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from core.config import BotConfig
from core.models import OrderStatus, TradeOrder, TradeResult
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
        "SHORT", symbol, 100.0, cost / 100.0, usdt_amount=cost, source="manual",
        order_id=f"null-{symbol.split('/')[0]}",
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
    adapter, ex = _adapter(_cfg(fee_taker_pct=0, fee_taker_conservative_pct=0))
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

        estimated_adapter, ex = _adapter()
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
            adapter=estimated_adapter,
            config=estimated_adapter.config,
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
            fee_usdt=1.0, fee_estimated=True, fee_rate_source="config_conservative", rate=RATE,
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
            fee_usdt=0.2, fee_estimated=True, fee_rate_source="config_conservative", rate=RATE,
        )
        gross_p = unrealized_pnl("short", 4.0, 100.0, 80.0)
        fund_p = float(partial.funding_usdt or 0)
        assert partial.open_fee_usdt == pytest.approx(0.4)
        assert partial.pnl == pytest.approx(gross_p - fund_p - 0.4 - 0.2)
        part_row = _rows("cv-part", "COVER")[-1]
        assert part_row.get("open_fee_estimated") is True
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
            fee_usdt=0.75, fee_estimated=True, fee_rate_source="config_conservative", rate=RATE,
        )
        covered = svc.execute_cover(
            symbol, TF, 100.0, amount=10.0, source="manual", order_id="br-c",
            fee_usdt=0.75, fee_estimated=True, fee_rate_source="config_conservative", rate=RATE,
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


def _ccxt_swap_trade(**fields) -> dict:
    """Real ccxt.gate parse_trade output. No network."""
    import ccxt

    gate = ccxt.gate()
    trade = {
        "id": "1",
        "contract": "SYN_USDT",
        "order_id": "ex-gt",
        "size": -2,
        "price": "100",
        "role": "taker",
        "fee": "0",
        "fee_currency": "USDT",
        "gt_fee": "0",
        "point_fee": "0",
    }
    trade.update(fields)
    parsed = gate.parse_trade(trade)
    parsed["order"] = fields.get("order_id", trade["order_id"])
    return parsed


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
    # Same trade: USDT leg plus GT. The USDT leg is the fee.
    mixed = _ccxt_swap_trade(fee="0.15", gt_fee="0.01", size=-2, price="100")
    ex.fetch_my_trades.return_value = [mixed]
    with tenant_context("default", scope="demo"):
        result = adapter._finalize_exchange_order(
            ex, order, raw, side="sell", qty=2, timeframe=TF,
        )
    assert result.fee_unknown is False
    assert result.fee == pytest.approx(0.15)
    assert result.fee_estimated is not True

    # Pure GT fill: fee cost 0 plus a GT leg. Estimate the notional.
    only, only_ex = _adapter()
    only_order = TradeOrder(
        "SHORT", "GTONLY/USDT", 100.0, 2.0, usdt_amount=200.0, source="manual", order_id="gt-2",
    )
    pure = _ccxt_swap_trade(
        id="2", order_id="ex-gtonly", fee="0", gt_fee="0.05", size=-2, price="100",
    )
    only_ex.fetch_my_trades.return_value = [pure]
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

    # One USDT trade and one GT-only trade: the GT notional is not booked as 0.
    both, both_ex = _adapter()
    both_order = TradeOrder(
        "SHORT", "GTMIX/USDT", 100.0, 3.0, usdt_amount=300.0, source="manual", order_id="gt-3",
    )
    usdt_leg = _ccxt_swap_trade(
        id="3", order_id="ex-mix", fee="0.10", gt_fee="0", size=-1, price="100",
    )
    gt_leg = _ccxt_swap_trade(
        id="4", order_id="ex-mix", fee="0", gt_fee="0.04", size=-2, price="100",
    )
    both_ex.fetch_my_trades.return_value = [usdt_leg, gt_leg]
    raw_both = _closed_raw(filled=3, oid="ex-mix", fee=None, cost=300)
    with tenant_context("default", scope="demo"):
        mixed_orders = both._finalize_exchange_order(
            both_ex, both_order, raw_both, side="sell", qty=3, timeframe=TF,
        )
    assert mixed_orders.fee_estimated is True
    assert mixed_orders.fee == pytest.approx(0.10 + 200.0 * RATE)
    assert mixed_orders.fee > 0.10


def _stamp_missing_pnl(order_id: str = "miss-1", pnl=None, *, tracked: bool = True) -> None:
    from services import order_service
    from storage.order_ledger_v2 import PNL_TRACKED_FIELD

    now = datetime.now().isoformat()
    order_service._ORDERS_READ_CACHE.clear()
    row = {
        "id": order_id,
        "status": "filled",
        "side": "cover",
        "symbol": "MISS/USDT",
        "ledger_scope": "demo",
        "pnl": pnl,
        "needs_reconcile": pnl is None,
        "execution": {"usdt": 500.0, "price": 10.0, "amount": 50.0},
        "timestamps": {"created": now, "filled": now, "updated": now},
    }
    if tracked:
        row[PNL_TRACKED_FIELD] = True
    save_orders({"ledger_scope": "demo", "orders": [row]}, "demo")
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


def _live_ledger(monkeypatch):
    """Live trade rows stay in memory. Paper history is not the booking source."""
    rows: list[dict] = []

    def _uses(_mode=None):
        return True

    def _record(trade):
        rows.append(dict(trade))

    def _load():
        return {"trades": list(rows)}

    monkeypatch.setattr("data_manager.uses_exchange_ledger", _uses)
    monkeypatch.setattr("execution.gate_adapter.uses_exchange_ledger", _uses)
    monkeypatch.setattr("execution.gate_adapter.record_live_trade", _record)
    monkeypatch.setattr("data_manager.load_live_trade_history", _load)
    return rows


def _live_rows(rows: list[dict], order_id: str, typ: str) -> list[dict]:
    return [
        row for row in rows
        if str(row.get("order_id") or "") == order_id
        and str(row.get("type") or "").upper() == typ
    ]


def test_live_three_partials_book_each_fee_increment(monkeypatch):
    rows = _live_ledger(monkeypatch)
    adapter, _ex = _adapter()
    symbol = "LIVE/USDT"
    order = TradeOrder(
        "SHORT", symbol, 100.0, 10.0, usdt_amount=1000.0, source="manual", order_id="live-s",
    )
    reports = [(2.0, 0.20, "open"), (5.0, 0.50, "open"), (10.0, 1.00, "closed")]
    with tenant_context("default", scope="demo"):
        for filled, fee, status in reports:
            raw = _closed_raw(
                filled=filled, oid="ex-live-s", fee={"cost": fee, "currency": "USDT"}, status=status,
            )
            adapter._finalize_exchange_order(_ex, order, raw, side="sell", qty=10, timeframe=TF)
        again = _closed_raw(
            filled=10, oid="ex-live-s", fee={"cost": 1.00, "currency": "USDT"}, status="closed",
        )
        adapter._finalize_exchange_order(_ex, order, again, side="sell", qty=10, timeframe=TF)
        lot = get_position(symbol, TF)
        booked = _live_rows(rows, "live-s", "SHORT")
        assert len(booked) == 3
        assert [float(row["fee_usdt"]) for row in booked] == pytest.approx([0.20, 0.30, 0.50])
        assert [float(row["fee"]) for row in booked] == pytest.approx([0.20, 0.30, 0.50])
        assert sum(float(row["amount"]) for row in booked) == pytest.approx(10)
        assert float(lot["open_fee_usdt"]) == pytest.approx(1.00)
        assert float(lot["amount"]) == pytest.approx(10)

        update_position("LIVC/USDT", TF, "SHORT", 100.0, 10.0, leverage=2)
        cover = TradeOrder(
            "COVER", "LIVC/USDT", 90.0, 10.0, usdt_amount=900.0, source="manual", order_id="live-c",
        )
        cover_reports = [(4.0, 0.20, "open"), (7.0, 0.45, "open"), (10.0, 0.80, "closed")]
        for filled, fee, status in cover_reports:
            raw = _closed_raw(
                filled=filled,
                oid="ex-live-c",
                average=90.0,
                fee={"cost": fee, "currency": "USDT"},
                status=status,
                cost=filled * 90.0,
            )
            adapter._finalize_exchange_order(
                _ex, cover, raw, side="buy", qty=10, timeframe=TF,
            )
        replay = _closed_raw(
            filled=10, oid="ex-live-c", average=90.0, fee={"cost": 0.80, "currency": "USDT"},
            status="closed", cost=900.0,
        )
        adapter._finalize_exchange_order(_ex, cover, replay, side="buy", qty=10, timeframe=TF)
        cover_rows = _live_rows(rows, "live-c", "COVER")
        assert len(cover_rows) == 3
        assert [float(row["fee_usdt"]) for row in cover_rows] == pytest.approx([0.20, 0.25, 0.35])
        assert sum(float(row["amount"]) for row in cover_rows) == pytest.approx(10)
        assert float(get_position("LIVC/USDT", TF).get("amount") or 0) == pytest.approx(0)


def test_cover_partial_then_remainder(monkeypatch):
    rows = _live_ledger(monkeypatch)
    adapter, _ex = _adapter()
    with tenant_context("default", scope="demo"):
        update_position("CREM/USDT", TF, "SHORT", 100.0, 10.0, leverage=2)
        cover = TradeOrder(
            "COVER", "CREM/USDT", 90.0, 10.0, usdt_amount=900.0, source="manual", order_id="crem-1",
        )
        first = _closed_raw(
            filled=4, oid="ex-crem", average=90, fee={"cost": 0.30, "currency": "USDT"},
            status="open", cost=360,
        )
        adapter._finalize_exchange_order(_ex, cover, first, side="buy", qty=10, timeframe=TF)
        second = _closed_raw(
            filled=10, oid="ex-crem", average=90, fee={"cost": 0.75, "currency": "USDT"},
            status="closed", cost=900,
        )
        adapter._finalize_exchange_order(_ex, cover, second, side="buy", qty=10, timeframe=TF)
        booked = _live_rows(rows, "crem-1", "COVER")
        assert len(booked) == 2
        assert sum(float(row["amount"]) for row in booked) == pytest.approx(10)
        assert [float(row["fee_usdt"]) for row in booked] == pytest.approx([0.30, 0.45])
        assert [float(row["pnl"]) for row in booked] == pytest.approx([39.70, 59.55])
        assert float(get_position("CREM/USDT", TF).get("amount") or 0) == pytest.approx(0)


def test_v1_missing_and_zero_fee_are_estimated_on_open_and_close():
    adapter, ex = _adapter()
    ex.fetch_my_trades.return_value = []
    short = TradeOrder(
        "SHORT", "V1S/USDT", 100.0, 10.0, usdt_amount=1000.0, source="manual", order_id="v1-s",
    )
    raw = _closed_raw(filled=10, oid="ex-v1s", fee=None, cost=1000)
    with tenant_context("default", scope="demo"):
        opened = adapter._finalize_exchange_order(
            ex, short, raw, side="sell", qty=10, timeframe=TF,
        )
        lot = get_position("V1S/USDT", TF)
    assert opened.fee_estimated is True
    assert opened.needs_reconcile is True
    assert opened.fee == pytest.approx(1000.0 * RATE)
    assert float(lot["open_fee_usdt"]) == pytest.approx(1000.0 * RATE)

    zero_ad, zero_ex = _adapter()
    zero_ex.fetch_my_trades.return_value = [
        {
            "order": "ex-v1z",
            "cost": 1000.0,
            "price": 100.0,
            "amount": 10.0,
            "fee": {"cost": 0, "currency": "USDT"},
        }
    ]
    zero_order = TradeOrder(
        "SHORT", "V1Z/USDT", 100.0, 10.0, usdt_amount=1000.0, source="manual", order_id="v1-z",
    )
    zero_raw = _closed_raw(
        filled=10, oid="ex-v1z", fee={"cost": 0, "currency": "USDT"}, cost=1000,
    )
    with tenant_context("default", scope="demo"):
        zeroed = zero_ad._finalize_exchange_order(
            zero_ex, zero_order, zero_raw, side="sell", qty=10, timeframe=TF,
        )
    assert zeroed.fee_estimated is True
    assert zeroed.needs_reconcile is True
    assert zeroed.fee == pytest.approx(1000.0 * RATE)
    assert float(get_position("V1Z/USDT", TF)["open_fee_usdt"]) == pytest.approx(1000.0 * RATE)

    cover_ad, cover_ex = _adapter()
    cover_ex.fetch_my_trades.return_value = []
    with tenant_context("default", scope="demo"):
        update_position("V1C/USDT", TF, "SHORT", 100.0, 10.0, leverage=2)
        cover = TradeOrder(
            "COVER", "V1C/USDT", 90.0, 10.0, usdt_amount=900.0, source="manual", order_id="v1-c",
        )
        cover_raw = _closed_raw(
            filled=10, oid="ex-v1c", average=90, fee=None, cost=900,
        )
        closed = cover_ad._finalize_exchange_order(
            cover_ex, cover, cover_raw, side="buy", qty=10, timeframe=TF,
        )
        gross = unrealized_pnl("short", 10, 100, 90)
        assert closed.fee_estimated is True
        assert closed.needs_reconcile is True
        assert closed.fee == pytest.approx(900.0 * RATE)
        assert closed.pnl == pytest.approx(gross - 900.0 * RATE)

        update_position("V1D/USDT", TF, "SHORT", 100.0, 10.0, leverage=2)
        zero_cover_ex_trades = [
            {
                "order": "ex-v1d",
                "cost": 900.0,
                "price": 90.0,
                "amount": 10.0,
                "fee": {"cost": 0, "currency": "USDT"},
            }
        ]
    zero_cover_ad, zero_cover_ex = _adapter()
    zero_cover_ex.fetch_my_trades.return_value = zero_cover_ex_trades
    zero_cover = TradeOrder(
        "COVER", "V1D/USDT", 90.0, 10.0, usdt_amount=900.0, source="manual", order_id="v1-d",
    )
    zero_cover_raw = _closed_raw(
        filled=10, oid="ex-v1d", average=90, fee={"cost": 0, "currency": "USDT"}, cost=900,
    )
    with tenant_context("default", scope="demo"):
        zero_closed = zero_cover_ad._finalize_exchange_order(
            zero_cover_ex, zero_cover, zero_cover_raw, side="buy", qty=10, timeframe=TF,
        )
    assert zero_closed.fee_estimated is True
    assert zero_closed.needs_reconcile is True
    assert zero_closed.fee == pytest.approx(900.0 * RATE)
    assert zero_closed.pnl == pytest.approx(gross - 900.0 * RATE)


def test_c1_legacy_missing_pnl_does_not_block_and_marker_is_same_write(monkeypatch):
    import copy

    from services import order_service
    from storage.order_ledger_v2 import PNL_TRACKED_FIELD

    cfg = _cfg()
    rm = RiskManager(cfg)
    logs: list[str] = []
    monkeypatch.setattr("logger.log", lambda msg, level="INFO": logs.append(str(msg)))
    with tenant_context("default", scope="demo"):
        _stamp_missing_pnl(order_id="legacy-1", pnl=None, tracked=False)
        buy = rm.evaluate(
            TradeOrder("BUY", "LEGX/USDT", 1.0, 0, usdt_amount=25, source="manual"),
            TF,
            source="manual",
        )
        assert buy.code != "pnl_missing"
        assert buy.approved is True, buy.message
        short = rm.evaluate(
            TradeOrder("SHORT", "LEGS/USDT", 1.0, 0, usdt_amount=50, source="manual", leverage=2),
            TF,
            source="manual",
        )
        assert short.code != "pnl_missing"
        assert short.approved is True, short.message
        assert any(msg.startswith("pnl_missing_legacy count=") for msg in logs)

        _stamp_missing_pnl(order_id="new-1", pnl=None, tracked=True)
        blocked = rm.evaluate(
            TradeOrder("BUY", "LEGX/USDT", 1.0, 0, usdt_amount=25, source="manual"),
            TF,
            source="manual",
        )
        assert blocked.approved is False
        assert blocked.code == "pnl_missing"

        snapshots: list[dict] = []
        real_save = order_service.save_orders

        def _spy(data, scope):
            snapshots.append(copy.deepcopy(data))
            return real_save(data, scope)

        monkeypatch.setattr(order_service, "save_orders", _spy)
        svc = OrderService("demo")
        created = svc.create_from_request(
            TradeOrder("SELL", "SYN/USDT", 2.0, 1.0, usdt_amount=2.0, source="manual"),
            status=OrderStatus.ACTIVE,
            timeframe=TF,
        )
        snapshots.clear()
        filled = TradeResult(True, "SELL", "SYN/USDT", amount=1.0, price=2.0, usdt_amount=2.0)
        filled.pnl = None
        filled.order_status = OrderStatus.EXECUTED
        approved = TradeOrder("SELL", "SYN/USDT", 2.0, 1.0, usdt_amount=2.0, source="manual")
        svc.link_execution_result(created["id"], filled, approved)
        filled_saves = []
        for snap in snapshots:
            for row in snap.get("orders") or []:
                if row.get("id") == created["id"] and row.get("status") == "filled":
                    filled_saves.append(row)
                    break
        assert filled_saves, "sell fill was not saved"
        first = filled_saves[0]
        assert first.get(PNL_TRACKED_FIELD) is True
        assert first.get("needs_reconcile") is True
        assert first.get("pnl") is None


def test_v2_recovery_writes_pnl_and_lifts_the_block():
    from services import order_service
    from storage.order_ledger_v2 import PNL_TRACKED_FIELD

    cfg = _cfg()
    rm = RiskManager(cfg)
    adapter, ex = _adapter(cfg)
    with tenant_context("default", scope="demo"):
        update_position("LIFT/USDT", TF, "SHORT", 100.0, 10.0, leverage=2)
        svc = OrderService("demo")
        rec = _active_order(
            svc, typ="COVER", symbol="LIFT/USDT", qty=10, price=90, usdt=900, ex_id="ex-lift",
        )
        data = svc._load()
        stored = svc._find(data, order_id=rec["id"])
        stored[PNL_TRACKED_FIELD] = True
        stored["needs_reconcile"] = True
        stored["status"] = "filled"
        stored.pop("pnl", None)
        svc._save(data)
        order_service._ORDERS_READ_CACHE.clear()
        blocked = rm.evaluate(
            TradeOrder("BUY", "LIFTN/USDT", 1.0, 0, usdt_amount=25, source="manual"),
            TF,
            source="manual",
        )
        assert blocked.code == "pnl_missing"
        ex.fetch_order.return_value = _closed_raw(
            filled=10, oid="ex-lift", average=90, fee={"cost": 0.2, "currency": "USDT"}, cost=900,
        )
        _resolve_in_flight_orders(
            [svc.get_by_id(rec["id"])],
            exchange=ex,
            adapter=adapter,
            config=cfg,
            scope="demo",
        )
        order_service._ORDERS_READ_CACHE.clear()
        after = svc.get_by_id(rec["id"])
        assert after.get("pnl") is not None
        lifted = rm.evaluate(
            TradeOrder("BUY", "LIFTN/USDT", 1.0, 0, usdt_amount=25, source="manual"),
            TF,
            source="manual",
        )
        assert lifted.code != "pnl_missing"
        assert lifted.approved is True, lifted.message


def test_pnl_missing_fail_closed_even_when_guards_log(monkeypatch):
    cfg = _cfg()
    raw = dict(cfg.raw)
    risk = dict(raw.get("risk") or {})
    risk["fail_closed_guards"] = "log"
    raw["risk"] = risk
    cfg = BotConfig(raw)
    rm = RiskManager(cfg)
    logs: list[str] = []
    monkeypatch.setattr(
        "logger.log",
        lambda msg, level="INFO": logs.append(f"{level}:{msg}"),
    )

    def _boom():
        raise RuntimeError("ledger down")

    monkeypatch.setattr(rm, "_filled_pnl_missing_count", lambda: _boom())
    with tenant_context("default", scope="demo"):
        update_position("LONGX/USDT", TF, "BUY", 1.0, 20)
        update_position("SHRTX/USDT", TF, "SHORT", 1.0, 8, leverage=2)
        buy = rm.evaluate(
            TradeOrder("BUY", "NEWX/USDT", 1.0, 0, usdt_amount=25, source="manual"),
            TF,
            source="manual",
        )
        assert buy.approved is False
        assert buy.code == "pnl_missing"
        assert any(
            line.startswith("ERROR:")
            and "risk guard pnl_missing failed" in line
            and "NEWX/USDT" in line
            for line in logs
        )
        short = rm.evaluate(
            TradeOrder("SHORT", "NEWSH/USDT", 1.0, 0, usdt_amount=50, source="manual", leverage=2),
            TF,
            source="manual",
        )
        assert short.approved is False
        assert short.code == "pnl_missing"
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
        plain = rm.evaluate(
            TradeOrder("SELL", "LONGX/USDT", 1.1, 5, signal="SELL", source="manual"),
            TF,
            source="manual",
        )
        assert plain.code != "pnl_missing"
        assert plain.approved is True, plain.message


class _NotGate:
    """Recovery must take the no-adapter path. The type name is the switch."""

    portfolio = None


def test_c5_tracked_lot_missing_open_fee_is_estimated_legacy_stays_zero():
    svc = PortfolioService(_cfg())
    with tenant_context("default", scope="demo"):
        opened = svc.execute_short(
            "C5S/USDT", TF, 100.0, usdt_amount=1000.0, source="manual",
            order_id="c5-s", fee_usdt=1.5,
        )
        assert opened.executed
        lot = get_position("C5S/USDT", TF)
        assert lot.get("open_fee_tracked") is True
        lot.pop("open_fee_usdt", None)
        lot.pop("open_fee_estimated", None)
        covered = svc.execute_cover(
            "C5S/USDT", TF, 90.0, amount=10.0, source="manual",
            order_id="c5-c", fee_usdt=0.4,
        )
        est = 1000.0 * RATE
        gross = unrealized_pnl("short", 10.0, 100.0, 90.0)
        fund = float(covered.funding_usdt or 0)
        assert covered.open_fee_usdt == pytest.approx(est)
        assert covered.open_fee_usdt != pytest.approx(0)
        assert covered.open_fee_usdt != pytest.approx(1.5)
        assert covered.needs_reconcile is True
        assert covered.pnl == pytest.approx(gross - fund - est - 0.4)

        update_position("C5L/USDT", TF, "SHORT", 100.0, 10.0, leverage=2)
        legacy_lot = get_position("C5L/USDT", TF)
        assert not legacy_lot.get("open_fee_tracked")
        legacy = svc.execute_cover(
            "C5L/USDT", TF, 90.0, amount=10.0, source="manual",
            order_id="c5-l", fee_usdt=0.4,
        )
        assert legacy.open_fee_usdt == pytest.approx(0)
        assert legacy.needs_reconcile is not True


def test_c3_unpriced_missing_pnl_flags_reconcile_priced_does_not(caplog):
    from storage.order_ledger_v2 import stats_from_filled_orders

    bare = {"id": "unpriced-1", "status": "filled", "side": "cover"}
    with caplog.at_level("ERROR"):
        stats = stats_from_filled_orders([bare])
    assert stats["realized_pnl"] == pytest.approx(-1e-12)
    assert bare.get("needs_reconcile") is True
    assert "order_pnl_missing_unpriced" in caplog.text

    priced = {
        "id": "priced-1",
        "status": "filled",
        "side": "cover",
        "execution": {"usdt": 1000.0},
    }
    priced_stats = stats_from_filled_orders([priced])
    assert priced.get("needs_reconcile") is not True
    assert priced_stats["realized_pnl"] == pytest.approx(-(1000.0 * RATE * 2))


def test_c10_result_fee_stays_the_fill_increment(monkeypatch):
    _live_ledger(monkeypatch)
    adapter, _ex = _adapter()
    with tenant_context("default", scope="demo"):
        short = TradeOrder(
            "SHORT", "C10S/USDT", 100.0, 10.0, usdt_amount=1000.0,
            source="manual", order_id="c10-s",
        )
        first = adapter._finalize_exchange_order(
            _ex, short,
            _closed_raw(filled=2, oid="ex-c10s", fee={"cost": 0.20, "currency": "USDT"}, status="open"),
            side="sell", qty=10, timeframe=TF,
        )
        second = adapter._finalize_exchange_order(
            _ex, short,
            _closed_raw(filled=5, oid="ex-c10s", fee={"cost": 0.50, "currency": "USDT"}, status="open"),
            side="sell", qty=10, timeframe=TF,
        )
        assert first.fee == pytest.approx(0.20)
        assert second.fee == pytest.approx(0.30)
        svc = OrderService("demo")
        rec = svc.create_from_request(
            TradeOrder("SHORT", "C10S/USDT", 100.0, 10.0, usdt_amount=1000.0, source="manual"),
            status=OrderStatus.ACTIVE,
            timeframe=TF,
        )
        svc.link_execution_result(rec["id"], second, short)
        from services import order_service

        order_service._ORDERS_READ_CACHE.clear()
        stored = svc.get_by_id(rec["id"])
        assert float(stored["execution"]["fee"]) == pytest.approx(0.30)

        update_position("C10C/USDT", TF, "SHORT", 100.0, 10.0, leverage=2)
        cover = TradeOrder(
            "COVER", "C10C/USDT", 90.0, 10.0, usdt_amount=900.0,
            source="manual", order_id="c10-c",
        )
        adapter._finalize_exchange_order(
            _ex, cover,
            _closed_raw(
                filled=4, oid="ex-c10c", average=90,
                fee={"cost": 0.20, "currency": "USDT"}, status="open", cost=360,
            ),
            side="buy", qty=10, timeframe=TF,
        )
        closed = adapter._finalize_exchange_order(
            _ex, cover,
            _closed_raw(
                filled=10, oid="ex-c10c", average=90,
                fee={"cost": 0.45, "currency": "USDT"}, status="closed", cost=900,
            ),
            side="buy", qty=10, timeframe=TF,
        )
        assert closed.fee == pytest.approx(0.25)
        gross = unrealized_pnl("short", 6.0, 100.0, 90.0)
        fund = float(closed.funding_usdt or 0)
        assert closed.pnl == pytest.approx(gross - fund - 0.25)


def _cover_unknown_fee(adapter, ex, symbol: str, oid: str, trades: list, *, fee):
    ex.fetch_my_trades.return_value = trades
    with tenant_context("default", scope="demo"):
        update_position(symbol, TF, "SHORT", 100.0, 10.0, leverage=2)
        cover = TradeOrder(
            "COVER", symbol, 90.0, 10.0, usdt_amount=900.0, source="manual", order_id=oid,
        )
        return adapter._finalize_exchange_order(
            ex, cover,
            _closed_raw(filled=10, oid=oid, average=90, fee=fee, cost=900),
            side="buy", qty=10, timeframe=TF,
        )


def test_v1_unknown_currency_estimates_instead_of_zero():
    adapter, ex = _adapter()
    closed = _cover_unknown_fee(
        adapter, ex, "V1U/USDT", "ex-v1u",
        [{
            "order": "ex-v1u",
            "cost": 900.0,
            "price": 90.0,
            "amount": 10.0,
            "fee": {"cost": 4.0, "currency": "BTC"},
        }],
        fee=None,
    )
    assert closed.fee == pytest.approx(900.0 * RATE)
    assert closed.needs_reconcile is True
    gross = unrealized_pnl("short", 10.0, 100.0, 90.0)
    assert closed.pnl == pytest.approx(gross - 900.0 * RATE)


def test_v1_gt_order_fee_estimates_and_ignores_usdt_trades():
    adapter, ex = _adapter()
    closed = _cover_unknown_fee(
        adapter, ex, "V1G/USDT", "ex-v1g",
        [{
            "order": "ex-v1g",
            "cost": 900.0,
            "price": 90.0,
            "amount": 10.0,
            "fee": {"cost": 50.0, "currency": "USDT"},
        }],
        fee={"cost": 0.5, "currency": "GT"},
    )
    assert closed.fee == pytest.approx(900.0 * RATE)
    assert closed.fee != pytest.approx(50.0)
    assert closed.needs_reconcile is True


def test_v1_null_fee_without_trade_notional_estimates_from_order():
    adapter, ex = _adapter()
    closed = _cover_unknown_fee(
        adapter, ex, "V1N/USDT", "ex-v1n",
        [{"order": "ex-v1n", "fee": {"cost": None, "currency": "USDT"}}],
        fee=None,
    )
    assert closed.fee == pytest.approx(900.0 * RATE)
    assert closed.needs_reconcile is True


def test_v1_settled_plus_unpriced_trade_does_not_book_zero():
    adapter, ex = _adapter()
    closed = _cover_unknown_fee(
        adapter, ex, "V1M/USDT", "ex-v1m",
        [
            {
                "order": "ex-v1m",
                "cost": 450.0,
                "price": 90.0,
                "amount": 5.0,
                "fee": {"cost": 0.20, "currency": "USDT"},
            },
            {"order": "ex-v1m", "fee": {"cost": None, "currency": "USDT"}},
        ],
        fee=None,
    )
    assert closed.fee == pytest.approx(900.0 * RATE)
    assert closed.fee != pytest.approx(0.20)
    assert closed.needs_reconcile is True


def _recover_cover_pnl_none(*, fee, cost=1000.0, ex_id="ex-pnl-none"):
    cfg = _cfg()
    ex = MagicMock()
    ex.fetch_order.return_value = _closed_raw(
        filled=10, oid=ex_id, average=100.0, fee=fee, cost=cost,
    )
    with tenant_context("default", scope="demo"):
        svc = OrderService("demo")
        rec = _active_order(
            svc, typ="COVER", symbol="PNLN/USDT", qty=10, price=100, usdt=1000, ex_id=ex_id,
        )
        _resolve_in_flight_orders(
            [rec], exchange=ex, adapter=_NotGate(), config=cfg, scope="demo",
        )
        from services import order_service

        order_service._ORDERS_READ_CACHE.clear()
        stored = svc.get_by_id(rec["id"])
        return cfg, stored


def test_cover_pnl_none_loss_is_at_least_two_fees():
    """Real recovery path. The test does not assign pnl."""
    from storage.order_ledger_v2 import stats_from_filled_orders

    close_fee = 0.30
    cfg, stored = _recover_cover_pnl_none(
        fee={"cost": close_fee, "currency": "USDT"}, ex_id="ex-pnl-fee",
    )
    assert stored.get("pnl") is None
    assert stored.get("needs_reconcile") is True
    stats = stats_from_filled_orders([stored])
    assert stats["realized_pnl"] <= -2 * close_fee
    notional = float(stored["execution"]["usdt"])
    assert stats["realized_pnl"] == pytest.approx(-(close_fee + notional * RATE))
    assert RiskManager(cfg)._trailing_24h_realized_pnl() <= -2 * close_fee


def test_cover_pnl_none_without_fee_uses_config_rate_both_sides():
    from storage.order_ledger_v2 import stats_from_filled_orders

    _cfg_obj, stored = _recover_cover_pnl_none(fee=None, ex_id="ex-pnl-nofee")
    assert stored.get("pnl") is None
    stats = stats_from_filled_orders([stored])
    assert stats["realized_pnl"] == pytest.approx(-(1000.0 * RATE * 2))
