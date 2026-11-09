"""#765: order-path Redis read latency and fail-closed timeout.

The measured path is ``get_order_path_redis_snapshot`` (one GET, reused
client, no in-process cache). A timeout must not return the cache and must
not send a BUY.
"""

from __future__ import annotations

import math
import time

import pytest
import redis

from core.config import BotConfig
from core.models import TradeOrder
from services.market_oracle import store
from services.market_oracle.store import (
    OracleReadTimeout,
    get_order_path_redis_snapshot,
    reset_order_path_client,
)
from services.portfolio_service import PortfolioService
from strategies.ctx_axes import entry_blocked_by_oracle_timeout, read_oracle_state


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


def test_measured_redis_get_p95_and_p99():
    client = redis.Redis.from_url(
        "redis://127.0.0.1:6379/0",
        socket_timeout=0.19,
        socket_connect_timeout=0.19,
        decode_responses=True,
    )
    assert client.ping() is True
    samples = []
    for _ in range(200):
        started = time.perf_counter()
        get_order_path_redis_snapshot()
        samples.append((time.perf_counter() - started) * 1000.0)
    p95 = _percentile(samples, 0.95)
    p99 = _percentile(samples, 0.99)
    print(f"oracle redis GET n=200 p95_ms={p95:.3f} p99_ms={p99:.3f}")
    assert p95 < 50.0
    assert p99 < 200.0


def test_timeout_is_fail_closed_and_does_not_use_the_cache(monkeypatch):
    store._LATEST = {"state": "RISK_ON", "regime": "RISK_ON"}

    class Hung:
        def get(self, _key):
            raise redis.TimeoutError("oracle get timed out")

    monkeypatch.setattr(store, "_order_path_client", lambda: Hung())
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
