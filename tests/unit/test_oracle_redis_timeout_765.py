"""#765 Rev 1: stop-write latency before and after the order-path read.

The clock starts immediately before the protective stop write and stops
immediately after it. A bare Redis GET is not the measurement. A timeout
does not use the in-process cache, does not send BUY or SHORT, and does not
block SELL or COVER.
"""

from __future__ import annotations

import math
import time

import pytest
import redis

from core.config import BotConfig
from core.models import OrderStatus, TradeOrder, TradeResult, trade_ctx_fields
from core.tenant_context import tenant_context
from execution.gate_adapter import GateExecutionAdapter
from services.market_oracle import store
from services.market_oracle.store import OracleReadTimeout, reset_order_path_client
from services.portfolio_service import PortfolioService
from strategies.ctx_axes import entry_blocked_by_oracle_timeout, read_oracle_state
from tests.unit.test_gate_adapter_testnet_short import SWAP, _adapter

# Injected into the order-path GET so the after sample can show the read
# sitting inside the stop-write bracket. Not a product latency budget.
_READ_DELAY_SEC = 0.02
_SAMPLES = 15


@pytest.fixture(autouse=True)
def _reset_oracle_client():
    reset_order_path_client()
    previous = store._LATEST
    yield
    store._LATEST = previous
    reset_order_path_client()


def _percentile(samples: list[float], q: float) -> float:
    ordered = sorted(samples)
    index = math.ceil(q * len(ordered)) - 1
    return ordered[max(0, index)]


class _DelayRedis:
    def __init__(self) -> None:
        self.gets = 0

    def get(self, _key):
        self.gets += 1
        time.sleep(_READ_DELAY_SEC)
        return '{"state": "RISK_OFF"}'


class _HungRedis:
    def get(self, _key):
        raise redis.TimeoutError("oracle get timed out")


def _filled_short(symbol: str) -> TradeResult:
    result = TradeResult(True, "SHORT", symbol, amount=10.0, price=100.0)
    result.filled_qty = 10.0
    result.order_status = OrderStatus.EXECUTED
    return result


def _bracket_stop_write(adapter, order: TradeOrder, client: _DelayRedis) -> float:
    """Time one protective stop write. GET, when it happens, is inside."""
    exchange = adapter._exchange
    before_creates = exchange.create_order.call_count
    before_gets = client.gets
    result = _filled_short(order.symbol)
    t_before = time.perf_counter()
    fields = trade_ctx_fields(order)
    adapter._place_reduce_only_stop(
        exchange,
        order,
        result,
        swap_symbol=SWAP,
        contract_size=1.0,
        lev=2.0,
        timeframe="4h",
    )
    t_after = time.perf_counter()
    assert exchange.create_order.call_count == before_creates + 1
    _args, _kwargs = exchange.create_order.call_args
    assert _args[5].get("reduceOnly") is True
    elapsed_ms = (t_after - t_before) * 1000.0
    assert t_after > t_before
    if order.ctx_oracle_state:
        assert client.gets == before_gets
        assert fields["ctx_oracle_state"] == order.ctx_oracle_state
    else:
        assert client.gets == before_gets + 1
        assert fields["ctx_oracle_state"] == "RISK_OFF"
        assert elapsed_ms >= _READ_DELAY_SEC * 1000.0 * 0.9
    return elapsed_ms


def test_stop_write_latency_before_and_after(monkeypatch):
    """AC1/AC2: before = stop write with no GET; after = GET inside the write."""
    store._LATEST = {"state": "RISK_ON", "regime": "RISK_ON"}
    client = _DelayRedis()
    monkeypatch.setattr(store, "_order_path_client", lambda: client)
    adapter = _adapter(monkeypatch)
    symbol = "BASE/USDT"
    before: list[float] = []
    after: list[float] = []
    with tenant_context("default"):
        for _ in range(_SAMPLES):
            priced = TradeOrder(
                "SHORT",
                symbol,
                100.0,
                10.0,
                usdt_amount=1000.0,
                leverage=2.0,
                ctx_oracle_state="RISK_ON",
            )
            before.append(_bracket_stop_write(adapter, priced, client))
        gets_after_before = client.gets
        assert gets_after_before == 0
        for _ in range(_SAMPLES):
            bare = TradeOrder(
                "SHORT",
                symbol,
                100.0,
                10.0,
                usdt_amount=1000.0,
                leverage=2.0,
            )
            after.append(_bracket_stop_write(adapter, bare, client))
    assert client.gets == _SAMPLES
    before_p95 = _percentile(before, 0.95)
    after_p95 = _percentile(after, 0.95)
    print(
        "stop-write before "
        f"n={_SAMPLES} p95_ms={before_p95:.3f} gets=0"
    )
    print(
        "stop-write after "
        f"n={_SAMPLES} p95_ms={after_p95:.3f} "
        f"gets={_SAMPLES} read_delay_ms={_READ_DELAY_SEC * 1000.0:.1f}"
    )
    assert after_p95 > before_p95
    assert after_p95 >= _READ_DELAY_SEC * 1000.0


def test_timeout_is_fail_closed_and_does_not_use_the_cache(monkeypatch):
    store._LATEST = {"state": "RISK_ON", "regime": "RISK_ON"}
    monkeypatch.setattr(store, "_order_path_client", lambda: _HungRedis())
    with pytest.raises(OracleReadTimeout):
        read_oracle_state()

    buy = TradeOrder("BUY", "ABC/USDT", 1.0, 1.0, usdt_amount=10.0)
    sell = TradeOrder("SELL", "ABC/USDT", 1.0, 1.0)
    assert entry_blocked_by_oracle_timeout(buy) is True
    assert entry_blocked_by_oracle_timeout(sell) is False

    svc = PortfolioService(BotConfig({"trading_mode": "paper", "max_usdt_per_trade": 100}))

    def _must_not_buy(*_a, **_k):
        raise AssertionError("buy was sent on a timed-out oracle read")

    monkeypatch.setattr(svc, "execute_buy", _must_not_buy)
    result = svc.execute_order(buy)
    assert result.executed is False
    assert "fail closed" in result.message


def test_timeout_blocks_buy_and_short_and_lets_sell_and_cover_reduce(monkeypatch):
    store._LATEST = {"state": "RISK_ON", "regime": "RISK_ON"}
    monkeypatch.setattr(store, "_order_path_client", lambda: _HungRedis())
    sent: list[str] = []

    def _ok(name: str):
        def _inner(*_a, **_k):
            sent.append(name)
            return TradeResult(True, name, "ABC/USDT", message="sent")

        return _inner

    svc = PortfolioService(BotConfig({"trading_mode": "paper", "max_usdt_per_trade": 100}))
    monkeypatch.setattr(svc, "execute_buy", _ok("BUY"))
    monkeypatch.setattr(svc, "execute_short", _ok("SHORT"))
    monkeypatch.setattr(svc, "execute_sell", _ok("SELL"))
    monkeypatch.setattr(svc, "execute_cover", _ok("COVER"))

    blocked_buy = svc.execute_order(TradeOrder("BUY", "ABC/USDT", 1.0, 1.0, usdt_amount=10))
    blocked_short = svc.execute_order(
        TradeOrder("SHORT", "ABC/USDT", 1.0, 1.0, usdt_amount=10)
    )
    assert blocked_buy.executed is False and "fail closed" in blocked_buy.message
    assert blocked_short.executed is False and "fail closed" in blocked_short.message

    sell = svc.execute_order(TradeOrder("SELL", "ABC/USDT", 1.0, 1.0, exit_source="stop_loss"))
    cover = svc.execute_order(TradeOrder("COVER", "ABC/USDT", 1.0, 1.0))
    assert sell.executed is True
    assert cover.executed is True
    assert sent == ["SELL", "COVER"]

    cfg = BotConfig({"trading_mode": "paper", "max_usdt_per_trade": 100})
    adapter = GateExecutionAdapter(cfg, PortfolioService(cfg), mode="shadow")

    def _no_exchange():
        raise AssertionError("exchange contacted on a timed-out entry")

    monkeypatch.setattr(adapter, "_get_exchange", _no_exchange)
    for side in ("BUY", "SHORT"):
        rejected = adapter.execute(TradeOrder(side, "ABC/USDT", 1.0, 1.0, usdt_amount=10))
        assert rejected.executed is False
        assert rejected.order_status is OrderStatus.REJECTED
        assert "fail closed" in rejected.message
