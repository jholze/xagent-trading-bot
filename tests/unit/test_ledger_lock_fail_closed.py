"""#306 slice 1: ledger lock fails closed; Redis cooldown instead of sticky latch."""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from bus.locks import LedgerLock, ledger_lock
from core.models import TradeOrder
from storage.errors import LedgerLockUnavailable, LedgerUnavailable


@pytest.fixture(autouse=True)
def _reset_redis_client_state():
    from bus.redis_client import reset_redis_client

    reset_redis_client()
    yield
    reset_redis_client()


class _NeverLock:
    def set(self, *a, **k):
        return False

    def get(self, *a, **k):
        return None

    def delete(self, *a, **k):
        raise AssertionError("timeout path must not delete a redis key")


class _BoomLock:
    def set(self, *a, **k):
        raise ConnectionError("nx failed")

    def get(self, *a, **k):
        return None

    def delete(self, *a, **k):
        raise AssertionError("redis_error path must not delete a redis key")


class _TokenMismatch:
    def __init__(self):
        self.deleted: list = []

    def set(self, key, val, nx=True, ex=None):
        return True

    def get(self, key):
        return "foreign-token"

    def delete(self, key):
        self.deleted.append(key)


def _client_for(reason: str):
    if reason == "redis_unavailable":
        return None
    if reason == "redis_error":
        return _BoomLock()
    return _NeverLock()


def test_ledger_lock_unavailable_is_ledger_unavailable():
    exc = LedgerLockUnavailable(
        scope="demo",
        tenant_id="lk306",
        reason="timeout",
        waited_sec=1.5,
    )
    assert isinstance(exc, LedgerUnavailable)
    assert exc.scope == "demo"
    assert exc.tenant_id == "lk306"
    assert exc.reason == "timeout"
    assert exc.waited_sec == pytest.approx(1.5)


@pytest.mark.parametrize("reason", ["timeout", "redis_error", "redis_unavailable"])
def test_fail_closed_raises_and_releases_thread_lock(reason):
    tid = f"lk306-{reason}"
    client = _client_for(reason)
    with patch("bus.locks.get_redis", return_value=client):
        with pytest.raises(LedgerLockUnavailable) as ei:
            with LedgerLock("demo", tenant_id=tid, wait_sec=0.0, fail_closed=True):
                pytest.fail("must not proceed without the redis lock")
    assert ei.value.reason == reason
    assert ei.value.scope == "demo"
    assert ei.value.tenant_id == tid

    entered = threading.Event()

    def _second():
        with LedgerLock("demo", tenant_id=tid, enabled=False):
            entered.set()

    t = threading.Thread(target=_second, daemon=True)
    t.start()
    assert entered.wait(timeout=1.0), f"thread lock leaked after fail-closed {reason}"
    t.join(timeout=1.0)
    assert not t.is_alive()


@pytest.mark.parametrize("reason", ["timeout", "redis_error", "redis_unavailable"])
def test_fail_open_proceeds_with_warning(reason):
    tid = f"lk306-open-{reason}"
    client = _client_for(reason)
    ran = {"ok": False}
    with patch("bus.locks.get_redis", return_value=client), patch("bus.locks.log") as mock_log:
        with LedgerLock("demo", tenant_id=tid, wait_sec=0.0, fail_closed=False):
            ran["ok"] = True
    assert ran["ok"] is True
    texts = [str(c.args[0]) for c in mock_log.call_args_list if c.args]
    assert any("fail_open" in t for t in texts)


def test_exit_token_mismatch_logs_lost_while_held_and_does_not_delete():
    client = _TokenMismatch()
    with patch("bus.locks.get_redis", return_value=client), patch("bus.locks.log") as mock_log:
        with LedgerLock("demo", tenant_id="lk306-lost", fail_closed=True, wait_sec=1.0):
            time.sleep(0.02)
    texts = [str(c.args[0]) for c in mock_log.call_args_list if c.args]
    assert any("lost while held" in t and "demo" in t for t in texts)
    assert client.deleted == []


def test_execute_order_lock_timeout_rejects_and_notifies_once(monkeypatch):
    from services import trading_service as ts
    from services.trading_service import TradingService

    ts._ledger_unavailable_notified.clear()
    monkeypatch.setattr("bus.locks.get_redis", lambda *a, **k: _NeverLock())
    monkeypatch.setattr(
        "services.trading_engine_runtime.should_queue_intent",
        lambda *a, **k: False,
    )

    svc = TradingService()
    svc.config._raw.setdefault("architecture", {})["ledger_lock_wait_sec"] = 0
    svc.config._raw["architecture"]["ledger_lock_fail_closed"] = True
    buy = TradeOrder(type="BUY", symbol="BTC/USDT", price=1.0, amount=0, usdt_amount=50)
    sell = TradeOrder(type="SELL", symbol="BTC/USDT", price=1.0, amount=1, signal="SELL")

    with patch("core.operator_notify.notify_operator") as notify, patch.object(
        svc.adapter, "execute"
    ) as adapter:
        first = svc.execute_order(buy, "4h", source="manual")
        second = svc.execute_order(sell, "4h", source="manual")

    assert getattr(first, "code", None) == "ledger_unavailable"
    assert getattr(first, "approved", None) is False
    assert first.executed is False
    assert getattr(second, "code", None) == "ledger_unavailable"
    assert second.executed is False
    assert notify.call_count == 1
    adapter.assert_not_called()


def test_get_redis_retries_after_cooldown(monkeypatch):
    import redis as redis_mod

    import bus.redis_client as mod

    mod.reset_redis_client()
    now = {"t": 1_000.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: now["t"])
    calls = {"n": 0}

    class Conn:
        def ping(self):
            return True

    def fake_from_url(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("refused")
        return Conn()

    monkeypatch.setattr(redis_mod, "from_url", fake_from_url)

    assert mod.get_redis() is None
    assert calls["n"] == 1
    assert mod.get_redis() is None
    assert calls["n"] == 1

    now["t"] += mod.REDIS_RETRY_COOLDOWN_SEC
    client = mod.get_redis()
    assert client is not None
    assert calls["n"] == 2
    assert mod.get_redis() is client
    assert calls["n"] == 2

    mod.reset_redis_client()
    calls["n"] = 0

    def always_fail(*a, **k):
        calls["n"] += 1
        raise ConnectionError("refused")

    monkeypatch.setattr(redis_mod, "from_url", always_fail)
    assert mod.get_redis() is None
    assert calls["n"] == 1
    mod.reset_redis_client()
    assert mod.get_redis() is None
    assert calls["n"] == 2
    mod.reset_redis_client()


def test_ledger_lock_factory_defaults_and_resolve_redis_url(monkeypatch):
    from core.config import BotConfig

    seen = {}

    def fake_resolve(url):
        seen["cfg_url"] = url
        return "redis://resolved:6379/0"

    def fake_get(url=None, key_prefix="aria:"):
        seen["get_url"] = url
        return None

    monkeypatch.setattr("bus.locks.resolve_redis_url", fake_resolve)
    monkeypatch.setattr("bus.locks.get_redis", fake_get)

    cfg = BotConfig(
        raw={
            "architecture": {
                "redis_url": "redis://from-config:6379/0",
                "ledger_lock_fail_closed": False,
            }
        }
    )
    lock = ledger_lock("demo", cfg=cfg, tenant_id="lk306-factory")
    assert lock.ttl_sec == 60
    assert lock.fail_closed is False
    with lock:
        pass
    assert seen["cfg_url"] == "redis://from-config:6379/0"
    assert seen["get_url"] == "redis://resolved:6379/0"


def test_engine_runtime_lock_unavailable_is_rejection(monkeypatch):
    from bus.trade_intents import TradeIntent, trade_intent_queue
    from services import trading_service as ts
    from services.trading_engine_runtime import ensure_started, reset_trading_engine_for_tests

    ts._ledger_unavailable_notified.clear()
    reset_trading_engine_for_tests()

    def _boom(*a, **k):
        raise LedgerLockUnavailable(
            reason="timeout",
            scope="paper",
            tenant_id="default",
            waited_sec=0.0,
        )

    monkeypatch.setattr("services.trading_engine_runtime.ledger_lock", _boom)

    with patch("core.operator_notify.notify_operator") as notify:
        ensure_started()
        order = TradeOrder(type="BUY", symbol="BTC/USDT", price=1.0, amount=0, usdt_amount=10)
        intent = TradeIntent(
            intent_id="lk306e",
            idempotency_key="lk306e",
            scope="paper",
            order=order,
            timeframe="4h",
            source="auto",
        )
        trade_intent_queue.submit(intent)
        result = intent.wait(timeout=5)

    try:
        assert result.executed is False
        assert getattr(result, "code", "") == "ledger_unavailable"
        assert notify.call_count == 1
    finally:
        reset_trading_engine_for_tests()
