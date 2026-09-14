"""GateExecutionAdapter testnet SHORT/COVER (#347). Fixture-only, no network."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import ccxt
import pytest

from core.config import BotConfig
from core.models import OrderStatus, TradeOrder
from core.tenant_context import tenant_context
from data_manager import save_trade_history
from execution.gate_adapter import GateExecutionAdapter, _GATE_TESTNET_HOST
from services.portfolio_service import PortfolioService
from strategies.positions import (
    clear_positions_memory,
    get_position,
    set_position_field,
    update_position,
)
from strategies.short_math import is_short, stop_price

SYMBOL = "BASE/USDT"
SWAP = "BASE/USDT:USDT"
REJECT_V0 = "shorts.allow_live=false — no Gate futures in v0"


def _shorts_block(
    *,
    enabled: bool = True,
    tenants: list | None = None,
    leverage_cap: float = 2,
) -> dict:
    return {
        "enabled": True,
        "allow_live": False,
        "leverage_default": 2,
        "leverage_cap": leverage_cap,
        "testnet_futures": {
            "enabled": enabled,
            "tenants": list(tenants) if tenants is not None else ["default"],
        },
    }


def _cfg(
    *,
    mode: str = "testnet",
    enabled: bool = True,
    tenants: list | None = None,
    leverage_cap: float = 2,
) -> BotConfig:
    return BotConfig(
        {
            "trading_mode": "live",
            "live_confirmed": True,
            "max_usdt_per_trade": 1000,
            "shorts": _shorts_block(
                enabled=enabled, tenants=tenants, leverage_cap=leverage_cap
            ),
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
                "execution": mode,
                "dry_run": False,
                "api_key_env": "GATE_API_KEY",
                "api_secret_env": "GATE_API_SECRET",
            },
        }
    )


def _closed_raw(
    *,
    filled: float,
    average: float = 100.0,
    oid: str = "tn-1",
    status: str = "closed",
    fee=None,
    extra: dict | None = None,
    contract_size: float = 1.0,
) -> dict:
    raw = {
        "id": oid,
        "status": status,
        "average": average,
        "filled": filled,
        "cost": filled * contract_size * average,
        "timestamp": 1_700_000_000_000,
        "fee": fee,
    }
    if extra:
        raw.update(extra)
    return raw


def _mock_swap_exchange(
    *,
    contract_size: float = 1.0,
    min_amount: float = 1.0,
    free_usdt: float = 10_000.0,
    filled_contracts: float = 10.0,
    average: float = 100.0,
    status: str = "closed",
    fee=None,
):
    ex = MagicMock(name="ccxt.gate")
    market = {
        "symbol": SWAP,
        "contractSize": contract_size,
        "limits": {"amount": {"min": min_amount}, "cost": {"min": 0}},
    }
    ex.amount_to_precision.side_effect = lambda _s, a: a
    ex.price_to_precision.side_effect = lambda _s, p: p
    ex.load_markets.return_value = {SWAP: market}
    ex.market.return_value = market
    ex.fetch_balance.return_value = {
        "USDT": {"free": free_usdt},
        "free": {"USDT": free_usdt},
    }
    ex.set_leverage.return_value = {"leverage": "2"}
    ex.create_order.return_value = _closed_raw(
        filled=filled_contracts,
        average=average,
        status=status,
        fee=fee,
        contract_size=contract_size,
    )
    ex.fetch_positions.return_value = [
        {
            "symbol": SWAP,
            "contracts": filled_contracts,
            "contractSize": contract_size,
            "side": "short",
        }
    ]
    ex.fetch_my_trades.return_value = []
    ex.fetch_open_orders.return_value = []
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


def _adapter(
    monkeypatch,
    *,
    mode: str = "testnet",
    enabled: bool = True,
    tenants=None,
    leverage_cap: float = 2,
    **ex_kw,
):
    monkeypatch.setenv("GATE_API_KEY", "k")
    monkeypatch.setenv("GATE_API_SECRET", "s")
    _isolate_ledger(monkeypatch)
    monkeypatch.setattr("execution.gate_adapter.record_live_trade", lambda rec: None)
    cfg = _cfg(
        mode=mode, enabled=enabled, tenants=tenants, leverage_cap=leverage_cap
    )
    adapter = GateExecutionAdapter(cfg, PortfolioService(cfg), mode=mode)
    adapter._exchange = _mock_swap_exchange(**ex_kw)
    return adapter


def _short_order(*, leverage: float | None = 2.0, qty: float = 10, usdt: float = 1000.0) -> TradeOrder:
    return TradeOrder(
        "SHORT",
        SYMBOL,
        100.0,
        qty,
        usdt_amount=usdt,
        leverage=leverage,
        signal="SHORT",
        source="manual",
    )


def test_short_testnet_guard_satisfied_places_isolated_sell(monkeypatch):
    adapter = _adapter(monkeypatch)
    with tenant_context("default"):
        result = adapter.execute(_short_order(), "4h")
    assert result.executed, result.message
    ex = adapter._exchange
    ex.set_leverage.assert_called_once()
    lev_args, lev_kwargs = ex.set_leverage.call_args
    assert lev_args[0] == 2
    assert lev_args[1] == SWAP
    assert lev_args[2] == {"marginMode": "isolated"}
    assert ex.create_order.call_count == 2
    args, _kwargs = ex.create_order.call_args_list[0]
    assert args[0] == SWAP
    assert args[1] == "market"
    assert args[2] == "sell"
    assert args[3] == pytest.approx(10)
    assert args[4] is None
    params = args[5]
    assert str(params.get("text") or "").startswith("t-")
    stop_args, _stop_kw = ex.create_order.call_args_list[1]
    assert stop_args[0] == SWAP
    assert stop_args[1] == "market"
    assert stop_args[2] == "buy"
    assert stop_args[3] == pytest.approx(10)
    assert stop_args[4] is None
    stop_params = stop_args[5]
    assert stop_params.get("reduceOnly") is True
    expected_stop = stop_price("short", 100.0, 0.12, 2)
    assert stop_params.get("triggerPrice") == pytest.approx(expected_stop)
    assert stop_params.get("stopPrice") == pytest.approx(expected_stop)
    assert stop_params.get("stopLossPrice") == pytest.approx(expected_stop)
    stop_text = str(stop_params.get("text") or "")
    assert stop_text.startswith("t-")
    assert len(stop_text.encode("utf-8")) <= 28
    pos = get_position(SYMBOL, "4h")
    assert is_short(pos)
    assert float(pos["amount"]) == pytest.approx(10)
    assert float(pos["leverage"]) == pytest.approx(2)
    assert pos.get("exchange_stop_order_id")


def test_short_leverage_5_clamped_to_cap_2(monkeypatch):
    adapter = _adapter(monkeypatch)
    with tenant_context("default"):
        result = adapter.execute(_short_order(leverage=5), "4h")
    assert result.executed, result.message
    lev_args, _ = adapter._exchange.set_leverage.call_args
    assert lev_args[0] == 2
    assert 5 not in lev_args
    pos = get_position(SYMBOL, "4h")
    assert float(pos["leverage"]) == pytest.approx(2)


def test_cover_testnet_reduce_only_buy_closes_short(monkeypatch):
    adapter = _adapter(monkeypatch)
    update_position(SYMBOL, "4h", "SHORT", 100.0, 10, leverage=2)
    assert is_short(get_position(SYMBOL, "4h"))
    order = TradeOrder(
        "COVER",
        SYMBOL,
        90.0,
        10,
        signal="COVER",
        source="manual",
        leverage=2,
    )
    with tenant_context("default"):
        result = adapter.execute(order, "4h")
    assert result.executed, result.message
    ex = adapter._exchange
    ex.set_leverage.assert_not_called()
    ex.create_order.assert_called_once()
    args, _ = ex.create_order.call_args
    assert args[0] == SWAP
    assert args[1] == "market"
    assert args[2] == "buy"
    assert args[4] is None
    params = args[5]
    assert params.get("reduceOnly") is True
    assert str(params.get("text") or "").startswith("t-")
    pos = get_position(SYMBOL, "4h")
    assert not (is_short(pos) and float(pos.get("amount") or 0) > 0)


def test_real_mode_short_rejected_unchanged(monkeypatch):
    adapter = _adapter(monkeypatch, mode="real")
    result = adapter.execute(_short_order(), "4h")
    assert not result.executed
    assert result.message == REJECT_V0
    adapter._exchange.create_order.assert_not_called()
    adapter._exchange.set_leverage.assert_not_called()


def test_shadow_mode_short_now_fills(monkeypatch):
    adapter = _adapter(monkeypatch, mode="shadow")
    result = adapter.execute(_short_order(), "4h")
    assert result.executed, result.message
    assert str(result.exchange_order_id).startswith("shadow-")
    adapter._exchange.create_order.assert_not_called()
    adapter._exchange.set_leverage.assert_not_called()


def test_testnet_futures_disabled_rejects_before_exchange(monkeypatch):
    adapter = _adapter(monkeypatch, enabled=False)
    with tenant_context("default"):
        result = adapter.execute(_short_order(), "4h")
    assert not result.executed
    assert "testnet futures disabled" in result.message
    assert "shorts.testnet_futures.enabled=false" in result.message
    adapter._exchange.create_order.assert_not_called()
    adapter._exchange.set_leverage.assert_not_called()


def test_testnet_tenant_not_in_allowlist_rejected(monkeypatch):
    adapter = _adapter(monkeypatch, tenants=["henry"])
    with tenant_context("default"):
        result = adapter.execute(_short_order(), "4h")
    assert not result.executed
    assert "testnet futures disabled" in result.message
    assert "tenant 'default' not in shorts.testnet_futures.tenants" in result.message
    adapter._exchange.create_order.assert_not_called()
    adapter._exchange.set_leverage.assert_not_called()


def test_testnet_empty_tenants_rejected(monkeypatch):
    adapter = _adapter(monkeypatch, tenants=[])
    with tenant_context("default"):
        result = adapter.execute(_short_order(), "4h")
    assert not result.executed
    assert "testnet futures disabled" in result.message
    assert "tenants" in result.message
    adapter._exchange.create_order.assert_not_called()
    adapter._exchange.set_leverage.assert_not_called()


def test_apply_testnet_fallback_host_without_set_sandbox_mode():
    import execution.gate_adapter as ga

    class BareExchange:
        def __init__(self):
            self.urls = {
                "api": {
                    "public": {
                        "spot": "https://api.gateio.ws/api/v4",
                        "futures": "https://api.gateio.ws/api/v4",
                    },
                    "private": {
                        "spot": "https://api.gateio.ws/api/v4",
                        "futures": "https://api.gateio.ws/api/v4",
                    },
                }
            }

    ex = BareExchange()
    GateExecutionAdapter._apply_testnet(ex)
    expected = "https://api-testnet.gateapi.io/api/v4"
    api = ex.urls["api"]
    assert isinstance(api, dict)
    assert api["public"]["spot"] == expected
    assert api["public"]["futures"] == expected
    assert api["private"]["spot"] == expected
    assert api["private"]["futures"] == expected
    assert _GATE_TESTNET_HOST == "https://api-testnet.gateapi.io"
    src = Path(ga.__file__).read_text(encoding="utf-8")
    assert "fx-api-testnet.gateio.ws" not in src
    assert "https://api-testnet.gateapi.io" in src


def test_insufficient_futures_margin_rejects_without_create(monkeypatch):
    adapter = _adapter(monkeypatch, free_usdt=1.0)
    with tenant_context("default"):
        result = adapter.execute(_short_order(), "4h")
    assert not result.executed
    assert "Insufficient futures USDT margin" in result.message
    adapter._exchange.create_order.assert_not_called()
    adapter._exchange.set_leverage.assert_not_called()


def test_short_fill_price_below_request_ledger_matches_filled_base(monkeypatch):
    adapter = _adapter(monkeypatch, average=95.0, filled_contracts=10.0)
    with tenant_context("default"):
        result = adapter.execute(_short_order(), "4h")
    assert result.executed, result.message
    filled_base = 10.0
    assert result.amount == pytest.approx(filled_base)
    pos = get_position(SYMBOL, "4h")
    assert float(pos["amount"]) == pytest.approx(result.amount)
    assert float(pos["amount"]) == pytest.approx(filled_base)


def test_short_partial_fill_ledger_amount_is_filled_base(monkeypatch):
    adapter = _adapter(
        monkeypatch, filled_contracts=4.0, average=100.0, status="open"
    )
    with tenant_context("default"):
        result = adapter.execute(_short_order(), "4h")
    assert result.executed, result.message
    assert result.order_status is OrderStatus.PARTIALLY_FILLED
    assert result.pending is True
    assert result.filled_qty == pytest.approx(4.0)
    assert result.amount == pytest.approx(4.0)
    pos = get_position(SYMBOL, "4h")
    assert float(pos["amount"]) == pytest.approx(4.0)


def test_short_contract_size_10_ledger_amount_is_100_base(monkeypatch):
    adapter = _adapter(
        monkeypatch,
        contract_size=10.0,
        filled_contracts=10.0,
        free_usdt=20_000.0,
    )
    with tenant_context("default"):
        result = adapter.execute(_short_order(qty=100, usdt=10_000.0), "4h")
    assert result.executed, result.message
    args, _ = adapter._exchange.create_order.call_args_list[0]
    assert args[3] == pytest.approx(10)
    assert result.amount == pytest.approx(100)
    pos = get_position(SYMBOL, "4h")
    assert float(pos["amount"]) == pytest.approx(100)


def test_uncertain_create_recovers_from_swap_book(monkeypatch):
    adapter = _adapter(monkeypatch)
    order = _short_order()
    order.client_order_id = "abc-key"
    order.idempotency_key = "abc-key"
    found = _closed_raw(
        filled=10.0, oid="tn-found", extra={"clientOrderId": "abc-key"}
    )
    adapter._exchange.create_order.side_effect = ccxt.RequestTimeout("t")
    adapter._exchange.fetch_open_orders.return_value = [found]
    with tenant_context("default"):
        result = adapter.execute(order, "4h")
    assert result.executed, result.message
    adapter._exchange.fetch_open_orders.assert_called()
    assert adapter._exchange.fetch_open_orders.call_args[0][0] == SWAP
    assert result.exchange_order_id == "tn-found"
    pos = get_position(SYMBOL, "4h")
    assert is_short(pos)
    assert float(pos["amount"]) == pytest.approx(10)


def test_uncertain_create_empty_swap_lookups_not_placed(monkeypatch):
    adapter = _adapter(monkeypatch)
    order = _short_order()
    order.client_order_id = "abc-key"
    order.idempotency_key = "abc-key"
    adapter._exchange.create_order.side_effect = ccxt.RequestTimeout("t")
    adapter._exchange.fetch_open_orders.return_value = []
    adapter._exchange.fetch_order.return_value = {}
    with tenant_context("default"):
        result = adapter.execute(order, "4h")
    assert not result.executed
    assert result.message == "not placed"
    adapter._exchange.fetch_open_orders.assert_called()
    assert adapter._exchange.fetch_open_orders.call_args[0][0] == SWAP
    assert adapter._exchange.fetch_order.called
    for call in adapter._exchange.fetch_order.call_args_list:
        assert call[0][1] == SWAP


def test_swap_fee_from_matching_my_trades(monkeypatch):
    adapter = _adapter(monkeypatch)
    adapter._exchange.fetch_my_trades.return_value = [
        {"order": "tn-1", "fee": {"cost": 0.12, "currency": "USDT"}},
        {"order": "tn-1", "fee": {"cost": 0.08, "currency": "USDT"}},
        {"order": "other", "fee": {"cost": 9.0, "currency": "USDT"}},
    ]
    with tenant_context("default"):
        result = adapter.execute(_short_order(), "4h")
    assert result.executed, result.message
    assert result.fee_unknown is False
    assert result.fee == pytest.approx(0.20)
    assert adapter._exchange.fetch_my_trades.call_args[0][0] == SWAP


def test_swap_fee_missing_marks_fee_unknown(monkeypatch):
    adapter = _adapter(monkeypatch)
    adapter._exchange.fetch_my_trades.return_value = []
    with tenant_context("default"):
        result = adapter.execute(_short_order(), "4h")
    assert result.executed, result.message
    assert result.fee_unknown is True
    assert result.needs_reconcile is True
    assert result.fee == pytest.approx(0.0)
    assert "(fee_unknown)" in (result.message or "")


def test_no_tenant_context_rejected_even_if_default_allowlisted(monkeypatch):
    adapter = _adapter(monkeypatch, tenants=["default"])
    result = adapter.execute(_short_order(), "4h")
    assert not result.executed
    assert "testnet futures disabled" in result.message
    assert "no tenant context" in result.message
    adapter._exchange.create_order.assert_not_called()
    adapter._exchange.set_leverage.assert_not_called()


def test_tenant_leverage_cap_10_hard_capped_to_2(monkeypatch):
    adapter = _adapter(monkeypatch, leverage_cap=10)
    with tenant_context("default"):
        result = adapter.execute(_short_order(leverage=10), "4h")
    assert result.executed, result.message
    lev_args, _ = adapter._exchange.set_leverage.call_args
    assert lev_args[0] == 2
    assert 10 not in lev_args
    pos = get_position(SYMBOL, "4h")
    assert float(pos["leverage"]) == pytest.approx(2)


def test_swap_filled_missing_does_not_merge_unconverted_contracts(monkeypatch):
    adapter = _adapter(monkeypatch, contract_size=10.0, free_usdt=20_000.0)
    created = {
        "id": "tn-1",
        "status": "closed",
        "average": 100.0,
        "timestamp": 1_700_000_000_000,
        "fee": None,
    }
    later = _closed_raw(
        filled=10.0, oid="tn-1", average=100.0, contract_size=10.0
    )
    adapter._exchange.create_order.return_value = created
    adapter._exchange.fetch_order.side_effect = [
        Exception("transient"),
        later,
    ]
    with tenant_context("default"):
        result = adapter.execute(_short_order(qty=100, usdt=10_000.0), "4h")
    assert not result.executed
    assert result.needs_reconcile is True
    assert result.pending is True
    assert result.order_status is OrderStatus.ACTIVE
    assert "swap filled unavailable" in result.message
    assert adapter._exchange.fetch_order.call_count <= 1
    pos = get_position(SYMBOL, "4h")
    assert not (is_short(pos) and float(pos.get("amount") or 0) > 0)
    assert float(pos.get("amount") or 0) == pytest.approx(0)


def test_cover_no_swap_short_needs_reconcile(monkeypatch):
    adapter = _adapter(monkeypatch)
    adapter._exchange.fetch_positions.return_value = [
        {
            "symbol": SWAP,
            "contracts": 10,
            "contractSize": 1,
            "side": "long",
        }
    ]
    update_position(SYMBOL, "4h", "SHORT", 100.0, 10, leverage=2)
    order = TradeOrder("COVER", SYMBOL, 90.0, 10, signal="COVER", source="manual")
    with tenant_context("default"):
        result = adapter.execute(order, "4h")
    assert not result.executed
    assert result.needs_reconcile is True
    assert result.pending is True
    assert result.order_status is OrderStatus.ACTIVE
    assert "no swap position to cover" in result.message
    assert "ledger/exchange mismatch" in result.message
    adapter._exchange.create_order.assert_not_called()
    pos = get_position(SYMBOL, "4h")
    assert is_short(pos)
    assert float(pos["amount"]) == pytest.approx(10)


def _cover_order(*, qty: float = 10, price: float = 90.0) -> TradeOrder:
    return TradeOrder(
        "COVER",
        SYMBOL,
        price,
        qty,
        signal="COVER",
        source="manual",
        leverage=2,
    )


def _seed_short_with_stop(oid: str = "stop-99") -> None:
    update_position(SYMBOL, "4h", "SHORT", 100.0, 10, leverage=2)
    set_position_field(SYMBOL, "4h", "exchange_stop_order_id", oid)


def test_stop_distance_reject_no_naked_close(monkeypatch):
    warnings: list[tuple[str, str]] = []

    def _capture(msg, level="INFO"):
        warnings.append((level, str(msg)))

    monkeypatch.setattr("execution.gate_adapter.log", _capture)
    adapter = _adapter(
        monkeypatch, fee={"cost": 0.02, "currency": "USDT"}
    )
    adapter._exchange.create_order.side_effect = [
        _closed_raw(
            filled=10.0,
            average=100.0,
            fee={"cost": 0.02, "currency": "USDT"},
        ),
        ccxt.InvalidOrder("INVALID_PARAM_VALUE stop price too close to last"),
    ]
    with tenant_context("default"):
        result = adapter.execute(_short_order(), "4h")
    assert result.executed, result.message
    assert result.order_status is OrderStatus.EXECUTED
    assert result.needs_reconcile is False
    assert result.pending is False
    assert adapter._exchange.create_order.call_count == 2
    pos = get_position(SYMBOL, "4h")
    assert is_short(pos)
    assert float(pos["amount"]) == pytest.approx(10)
    assert pos.get("exchange_stop_order_id") is None
    assert "too close" in str(pos.get("stop_placement_failed") or "")
    assert any(
        lvl == "WARNING" and "reduce-only stop" in msg for lvl, msg in warnings
    )


def test_partial_fill_stop_sized_to_filled_base(monkeypatch):
    adapter = _adapter(
        monkeypatch, filled_contracts=4.0, average=100.0, status="open"
    )
    with tenant_context("default"):
        result = adapter.execute(_short_order(), "4h")
    assert result.executed, result.message
    assert result.order_status is OrderStatus.PARTIALLY_FILLED
    ex = adapter._exchange
    assert ex.create_order.call_count == 2
    stop_args, _ = ex.create_order.call_args_list[1]
    assert stop_args[0] == SWAP
    assert stop_args[1] == "market"
    assert stop_args[2] == "buy"
    assert stop_args[3] == pytest.approx(4.0)
    stop_params = stop_args[5]
    assert stop_params.get("reduceOnly") is True
    expected_stop = stop_price("short", 100.0, 0.12, 2)
    assert stop_params.get("triggerPrice") == pytest.approx(expected_stop)
    assert stop_params.get("stopPrice") == pytest.approx(expected_stop)
    assert stop_params.get("stopLossPrice") == pytest.approx(expected_stop)
    pos = get_position(SYMBOL, "4h")
    assert float(pos["amount"]) == pytest.approx(4.0)
    assert pos.get("exchange_stop_order_id")


def test_active_needs_reconcile_does_not_place_stop(monkeypatch):
    adapter = _adapter(monkeypatch, contract_size=10.0, free_usdt=20_000.0)
    created = {
        "id": "tn-1",
        "status": "closed",
        "average": 100.0,
        "timestamp": 1_700_000_000_000,
        "fee": None,
    }
    later = _closed_raw(
        filled=10.0, oid="tn-1", average=100.0, contract_size=10.0
    )
    adapter._exchange.create_order.return_value = created
    adapter._exchange.fetch_order.side_effect = [Exception("transient"), later]
    with tenant_context("default"):
        result = adapter.execute(_short_order(qty=100, usdt=10_000.0), "4h")
    assert not result.executed
    assert result.needs_reconcile is True
    assert result.order_status is OrderStatus.ACTIVE
    assert adapter._exchange.create_order.call_count == 1
    pos = get_position(SYMBOL, "4h")
    assert not pos.get("exchange_stop_order_id")


def test_uncertain_create_recovered_does_not_place_stop(monkeypatch):
    adapter = _adapter(monkeypatch)
    order = _short_order()
    order.client_order_id = "abc-key"
    order.idempotency_key = "abc-key"
    found = _closed_raw(
        filled=10.0, oid="tn-found", extra={"clientOrderId": "abc-key"}
    )
    adapter._exchange.create_order.side_effect = ccxt.RequestTimeout("t")
    adapter._exchange.fetch_open_orders.return_value = [found]
    with tenant_context("default"):
        result = adapter.execute(order, "4h")
    assert result.executed, result.message
    assert adapter._exchange.create_order.call_count == 1
    pos = get_position(SYMBOL, "4h")
    assert is_short(pos)
    assert not pos.get("exchange_stop_order_id")
    assert not pos.get("stop_placement_failed")


def test_cover_cancels_stored_stop_id(monkeypatch):
    adapter = _adapter(monkeypatch)
    _seed_short_with_stop("stop-full")
    with tenant_context("default"):
        result = adapter.execute(_cover_order(), "4h")
    assert result.executed, result.message
    ex = adapter._exchange
    ex.create_order.assert_called_once()
    ex.cancel_order.assert_called_once()
    cargs, ckwargs = ex.cancel_order.call_args
    assert cargs[0] == "stop-full"
    assert cargs[1] == SWAP
    cancel_params = cargs[2] if len(cargs) > 2 else (ckwargs.get("params") or {})
    assert cancel_params.get("trigger") is True
    pos = get_position(SYMBOL, "4h")
    assert not pos.get("exchange_stop_order_id")


def test_capped_cover_cancels_stored_stop_id(monkeypatch):
    adapter = _adapter(monkeypatch)
    adapter._exchange.fetch_positions.return_value = [
        {
            "symbol": SWAP,
            "contracts": 6,
            "contractSize": 1.0,
            "side": "short",
        }
    ]
    adapter._exchange.create_order.return_value = _closed_raw(
        filled=6.0, average=90.0, oid="cover-capped"
    )
    _seed_short_with_stop("stop-capped")
    with tenant_context("default"):
        result = adapter.execute(_cover_order(qty=10), "4h")
    assert result.executed, result.message
    ex = adapter._exchange
    buy_args, _ = ex.create_order.call_args
    assert buy_args[2] == "buy"
    assert buy_args[3] == pytest.approx(6)
    ex.cancel_order.assert_called_once()
    cargs, ckwargs = ex.cancel_order.call_args
    assert cargs[0] == "stop-capped"
    assert cargs[1] == SWAP
    cancel_params = cargs[2] if len(cargs) > 2 else (ckwargs.get("params") or {})
    assert cancel_params.get("trigger") is True


def test_partial_cover_cancels_stored_stop_id(monkeypatch):
    adapter = _adapter(monkeypatch)
    adapter._exchange.create_order.return_value = _closed_raw(
        filled=4.0, average=90.0, oid="cover-partial"
    )
    _seed_short_with_stop("stop-partial")
    with tenant_context("default"):
        result = adapter.execute(_cover_order(qty=4), "4h")
    assert result.executed, result.message
    ex = adapter._exchange
    buy_args, _ = ex.create_order.call_args
    assert buy_args[2] == "buy"
    assert buy_args[3] == pytest.approx(4)
    ex.cancel_order.assert_called_once()
    cargs, ckwargs = ex.cancel_order.call_args
    assert cargs[0] == "stop-partial"
    assert cargs[1] == SWAP
    cancel_params = cargs[2] if len(cargs) > 2 else (ckwargs.get("params") or {})
    assert cancel_params.get("trigger") is True
    pos = get_position(SYMBOL, "4h")
    assert is_short(pos)
    assert float(pos["amount"]) == pytest.approx(6)
    assert not pos.get("exchange_stop_order_id")


def test_stop_uses_clamped_leverage_not_requested(monkeypatch):
    adapter = _adapter(monkeypatch)
    with tenant_context("default"):
        result = adapter.execute(_short_order(leverage=5), "4h")
    assert result.executed, result.message
    stop_params = adapter._exchange.create_order.call_args_list[1][0][5]
    expected = stop_price("short", 100.0, 0.12, 2)
    not_unclamped = stop_price("short", 100.0, 0.12, 5)
    assert stop_params.get("triggerPrice") == pytest.approx(expected)
    assert stop_params.get("triggerPrice") != pytest.approx(not_unclamped)
