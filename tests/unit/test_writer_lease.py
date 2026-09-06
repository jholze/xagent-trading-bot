"""#306 slice 2: single-writer lease, fencing token, readonly, /health, shutdown."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bus.writer_lease import (
    require_lease_for_order,
    WriterLease,
    fence_redis_key,
    lease_redis_key,
    mongo_fence_filter,
    prepare_json_ledger_write,
    reset_writer_lease_for_tests,
    set_process_lease_for_tests,
    set_lease_redis_for_tests,
    shutdown_writer_lease,
    writer_lease_fence,
    writer_lease_held,
    writer_lease_status,
)
from core.models import TradeOrder
from storage.errors import LedgerUnavailable, LedgerWriteFailed, WriterLeaseLost


@pytest.fixture(autouse=True)
def _reset_lease_state():
    reset_writer_lease_for_tests()
    yield
    reset_writer_lease_for_tests()


def _enable_lease():
    from data_manager import get_config

    get_config().setdefault("architecture", {})["single_writer_lease_enabled"] = True


class FakeClock:
    def __init__(self, t: float = 1_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class FakeRedis:
    """Dict-backed Redis: SET NX / GET / DEL / INCR / EXPIRE + injectable clock."""

    def __init__(self, clock=None):
        self.clock = clock or FakeClock()
        self._store: dict[str, tuple[str, float | None]] = {}

    def _purge(self, key: str) -> None:
        item = self._store.get(key)
        if item is None:
            return
        _val, exp = item
        if exp is not None and self.clock() >= exp:
            self._store.pop(key, None)

    def set(self, key, value, nx=False, xx=False, ex=None, **_k):
        self._purge(key)
        if nx and key in self._store:
            return False
        if xx and key not in self._store:
            return False
        exp = None if ex is None else self.clock() + float(ex)
        self._store[key] = (str(value), exp)
        return True

    def get(self, key):
        self._purge(key)
        item = self._store.get(key)
        return None if item is None else item[0]

    def delete(self, key):
        self._purge(key)
        if key in self._store:
            del self._store[key]
            return 1
        return 0

    def incr(self, key):
        self._purge(key)
        item = self._store.get(key)
        current = 0
        if item is not None:
            try:
                current = int(item[0])
            except (TypeError, ValueError):
                current = 0
        current += 1
        exp = item[1] if item is not None else None
        self._store[key] = (str(current), exp)
        return current

    def expire(self, key, ttl):
        self._purge(key)
        item = self._store.get(key)
        if item is None:
            return False
        self._store[key] = (item[0], self.clock() + float(ttl))
        return True


class FakeCollection:
    def __init__(self):
        self.docs: dict = {}
        self.calls: list = []

    def replace_one(self, filt, payload, upsert=False):
        self.calls.append((dict(filt), dict(payload), upsert))
        doc_id = filt.get("_id")
        existing = self.docs.get(doc_id)
        if existing is not None and _filter_matches(filt, existing):
            self.docs[doc_id] = dict(payload)
            return SimpleNamespace(matched_count=1, upserted_id=None)
        if existing is not None:
            if upsert:
                err = type("DuplicateKeyError", (Exception,), {})("E11000 duplicate key")
                raise err
            return SimpleNamespace(matched_count=0, upserted_id=None)
        if upsert:
            self.docs[doc_id] = dict(payload)
            return SimpleNamespace(matched_count=0, upserted_id=doc_id)
        return SimpleNamespace(matched_count=0, upserted_id=None)


def _filter_matches(filt: dict, existing: dict) -> bool:
    if existing.get("_id") != filt.get("_id") and filt.get("_id") != existing.get("_id"):
        if existing.get("_id") != filt.get("_id"):
            # _id is the lookup key; FakeCollection already selected by _id.
            pass
    or_clause = filt.get("$or")
    if not or_clause:
        return True
    has_fence = "fence" in existing
    fence = existing.get("fence") if has_fence else None
    for clause in or_clause:
        cond = clause.get("fence")
        if not isinstance(cond, dict):
            continue
        if cond.get("$exists") is False and not has_fence:
            return True
        if "$lte" in cond and has_fence:
            try:
                if int(fence) <= int(cond["$lte"]):
                    return True
            except (TypeError, ValueError):
                pass
    return False


def _lease(redis, clock, token="aaa", tenant="wl306", scope="demo", **kw) -> WriterLease:
    return WriterLease(
        tenant,
        scope,
        redis=redis,
        clock=clock,
        token=token,
        host="testhost",
        ttl_sec=kw.get("ttl_sec", 30),
        renew_sec=kw.get("renew_sec", 10),
        pid=1,
    )


def test_writer_lease_lost_is_ledger_unavailable():
    exc = WriterLeaseLost(reason="lost", tenant_id="wl306", scope="demo")
    assert isinstance(exc, LedgerUnavailable)
    assert exc.reason == "lost"
    assert exc.op == "writer_lease"


def test_acquire_renew_expiry():
    clock = FakeClock()
    redis = FakeRedis(clock)
    lease = _lease(redis, clock)
    assert lease.acquire(start_renewer=False) is True
    assert lease.held() is True
    assert lease.fence == 1
    raw = json.loads(redis.get(lease.lease_key))
    assert raw["token"] == "aaa"
    assert raw["fence"] == 1
    assert raw["host"] == "testhost"
    assert redis.get(lease.fence_key) == "1"
    assert lease.renew() is True
    clock.advance(30)
    assert lease.held() is False
    clock.advance(1)
    assert redis.get(lease.lease_key) is None


def test_second_process_cannot_acquire():
    clock = FakeClock()
    redis = FakeRedis(clock)
    a = _lease(redis, clock, token="holder")
    b = _lease(redis, clock, token="challenger")
    assert a.acquire(start_renewer=False) is True
    assert b.acquire(start_renewer=False) is False
    assert b.held() is False
    assert b.holder_label().startswith("holder/")
    assert a.held() is True


def test_takeover_after_expiry_increments_fence():
    clock = FakeClock()
    redis = FakeRedis(clock)
    a = _lease(redis, clock, token="old")
    b = _lease(redis, clock, token="new")
    assert a.acquire(start_renewer=False) is True
    assert a.fence == 1
    clock.advance(31)
    assert a.held() is False
    assert redis.get(a.lease_key) is None
    assert b.acquire(start_renewer=False) is True
    assert b.fence == 2
    assert json.loads(redis.get(b.lease_key))["token"] == "new"


def test_renew_and_release_only_if_ours():
    clock = FakeClock()
    redis = FakeRedis(clock)
    a = _lease(redis, clock, token="ours")
    assert a.acquire(start_renewer=False) is True
    redis.set(a.lease_key, json.dumps({"token": "foreign", "fence": 9, "host": "x"}))
    assert a.renew() is False
    assert a.held() is False
    assert a.release() is False
    assert redis.get(a.lease_key) is not None
    b = _lease(redis, clock, token="foreign")
    b.fence = 9
    b._mark_held(9)
    assert b.release() is True
    assert redis.get(a.lease_key) is None


def test_json_stale_fence_rejected(tmp_path):
    _enable_lease()
    clock = FakeClock()
    redis = FakeRedis(clock)
    path = str(tmp_path / "orders.paper.json")
    newer = _lease(redis, clock, token="new")
    assert newer.acquire(start_renewer=False) is True
    newer.fence = 5
    set_process_lease_for_tests(newer)
    payload = prepare_json_ledger_write({"orders": []}, path)
    assert payload["fence"] == 5
    path_obj = tmp_path / "orders.paper.json"
    path_obj.write_text(json.dumps(payload), encoding="utf-8")

    stale = _lease(redis, clock, token="old")
    stale.fence = 1
    stale._mark_held(1)
    set_process_lease_for_tests(stale)
    with pytest.raises(LedgerWriteFailed, match="stale fence"):
        prepare_json_ledger_write({"orders": [1]}, path)


def test_json_store_write_without_lease_fail_closed():
    _enable_lease()
    from storage.ledger_router import JsonLedgerStore

    store = JsonLedgerStore()
    with pytest.raises(LedgerWriteFailed, match="no writer lease"):
        store.save_orders({"orders": []}, "paper")


def test_json_store_stale_writer_rejected():
    _enable_lease()
    clock = FakeClock()
    redis = FakeRedis(clock)
    from storage.ledger_router import JsonLedgerStore

    store = JsonLedgerStore()
    holder = _lease(redis, clock, token="h1")
    assert holder.acquire(start_renewer=False) is True
    holder.fence = 4
    holder._last_confirmed_at = clock()
    set_process_lease_for_tests(holder)
    assert store.save_orders({"orders": [{"id": "a"}]}, "paper") is True

    stale = _lease(redis, clock, token="h0")
    stale.fence = 1
    stale._mark_held(1)
    set_process_lease_for_tests(stale)
    with pytest.raises(LedgerWriteFailed, match="stale fence"):
        store.save_orders({"orders": [{"id": "b"}]}, "paper")


def test_mongo_stale_fence_rejected_fake_collection():
    _enable_lease()
    clock = FakeClock()
    redis = FakeRedis(clock)
    holder = _lease(redis, clock, token="mongo-h")
    assert holder.acquire(start_renewer=False) is True
    holder.fence = 3
    set_process_lease_for_tests(holder)

    from storage.mongo_ledger import MongoLedgerStore
    from storage.tenant_keys import compound_ledger_id

    coll = FakeCollection()
    doc_id = compound_ledger_id("default", "paper")
    coll.docs[doc_id] = {"_id": doc_id, "fence": 9, "orders": []}
    store = MongoLedgerStore(test=True)
    store._guard_dev_db = lambda: None
    store._collection = lambda name: coll

    with pytest.raises(LedgerWriteFailed, match="stale fence"):
        store.save_orders({"orders": [{"id": "x"}]}, "paper", tenant_id="default")
    assert coll.docs[doc_id]["fence"] == 9
    filt = coll.calls[0][0]
    assert filt == mongo_fence_filter(doc_id, 3)


def test_mongo_write_without_lease_fail_closed():
    _enable_lease()
    from storage.mongo_ledger import MongoLedgerStore

    store = MongoLedgerStore(test=True)
    store._guard_dev_db = lambda: None
    store._collection = lambda name: FakeCollection()
    with pytest.raises(LedgerWriteFailed, match="no writer lease"):
        store.save_orders({"orders": []}, "paper", tenant_id="default")


def test_execute_order_denied_no_writer_lease_notifies_once_then_recovery(
    monkeypatch,
):
    from services import trading_service as ts
    from services.trading_service import TradingService

    _enable_lease()
    ts._ledger_unavailable_notified.clear()
    clock = FakeClock()
    redis = FakeRedis(clock)
    lease = _lease(redis, clock, tenant="default", scope="demo", token="bot")
    assert lease.acquire(start_renewer=False) is True
    set_process_lease_for_tests(lease)
    monkeypatch.setattr(
        "services.trading_engine_runtime.should_queue_intent",
        lambda *a, **k: False,
    )

    svc = TradingService()
    buy = TradeOrder(type="BUY", symbol="BTC/USDT", price=1.0, amount=0, usdt_amount=50)
    sell = TradeOrder(type="SELL", symbol="BTC/USDT", price=1.0, amount=1, signal="SELL")

    lease._mark_lost()
    with patch("core.operator_notify.notify_operator") as notify, patch.object(
        svc.adapter, "execute"
    ) as adapter:
        first = svc.execute_order(buy, "4h", source="manual")
        second = svc.execute_order(sell, "4h", source="manual")

    assert getattr(first, "code", None) == "no_writer_lease"
    assert getattr(first, "approved", None) is False
    assert first.executed is False
    assert getattr(second, "code", None) == "no_writer_lease"
    assert second.executed is False
    assert notify.call_count == 1
    adapter.assert_not_called()

    lease._mark_held(lease.fence)
    with patch("core.operator_notify.notify_operator") as notify2:
        from bus.writer_lease import _notify_reacquired

        _notify_reacquired(lease)
    assert notify2.call_count == 1
    assert "Schreibrecht wiederhergestellt" in str(notify2.call_args[0][0])

    lease._mark_lost()
    with patch("core.operator_notify.notify_operator") as notify3, patch.object(
        svc.adapter, "execute"
    ):
        third = svc.execute_order(buy, "4h", source="manual")
    assert getattr(third, "code", None) == "no_writer_lease"
    assert notify3.call_count == 1


def test_health_503_until_lease_held_and_recovery_done():
    from core.cycle_health import health_payload, reset_cycle_health_for_tests
    import services.architecture_runtime as rt

    _enable_lease()
    reset_cycle_health_for_tests()
    rt.reset_recovery_state_for_tests()
    clock = FakeClock()
    redis = FakeRedis(clock)
    lease = _lease(redis, clock, tenant="default", scope="demo")
    set_process_lease_for_tests(lease)

    # Review (#306): never held in this process -> standby -> 200, so a new
    # Railway container passes the deploy healthcheck while the old holder lives.
    body, status = health_payload(update_interval=60)
    assert status == 200
    assert body["writer_lease"] == "standby"
    assert body["status"] == "standby"
    assert body["fence"] is None

    assert lease.acquire(start_renewer=False) is True
    set_process_lease_for_tests(lease)
    body, status = health_payload(update_interval=60)
    assert status == 503
    assert body["writer_lease"] == "held"
    assert body["fence"] == 1

    with rt._recovery_lock:
        rt._recovered.add(("default", "demo"))
    body, status = health_payload(update_interval=60)
    assert status == 200
    assert body["writer_lease"] == "held"
    assert body["fence"] == 1
    assert body["status"] == "OK"


def test_health_disabled_stays_200_on_startup():
    from core.cycle_health import health_payload, reset_cycle_health_for_tests

    reset_cycle_health_for_tests()
    body, status = health_payload(update_interval=60)
    assert status == 200
    assert body["writer_lease"] == "disabled"
    assert body["fence"] is None


def test_shutdown_flushes_only_when_held(monkeypatch):
    from aria_bot import _flush_positions_on_exit

    _enable_lease()
    flushed: list = []
    monkeypatch.setattr(
        "strategies.positions.flush_positions",
        lambda **k: flushed.append(k),
    )
    monkeypatch.setattr("strategies.positions.get_active_scope", lambda: "demo")

    clock = FakeClock()
    redis = FakeRedis(clock)
    lease = _lease(redis, clock, tenant="default", scope="demo", token="shut")
    set_process_lease_for_tests(lease)
    _flush_positions_on_exit()
    assert flushed == []
    assert redis.get(lease.lease_key) is None

    flushed.clear()
    assert lease.acquire(start_renewer=False) is True
    set_process_lease_for_tests(lease)
    assert redis.get(lease.lease_key) is not None
    _flush_positions_on_exit()
    assert len(flushed) == 1
    assert flushed[0].get("force") is True
    assert redis.get(lease.lease_key) is None
    assert writer_lease_held() is False


def test_renewer_thread_joined_by_reset_hook():
    clock = FakeClock()
    redis = FakeRedis(clock)
    lease = _lease(redis, clock, renew_sec=60)
    assert lease.acquire(start_renewer=True) is True
    names = {t.name for t in threading.enumerate() if t.is_alive()}
    assert any(n.startswith("writer-lease-renewer-") for n in names)
    reset_writer_lease_for_tests()
    names = {t.name for t in threading.enumerate() if t.is_alive()}
    assert not any(n.startswith("writer-lease-renewer-") for n in names)


def test_ensure_writer_lease_readonly_when_held_elsewhere(monkeypatch):
    from bus.writer_lease import ensure_writer_lease

    _enable_lease()
    clock = FakeClock()
    redis = FakeRedis(clock)
    set_lease_redis_for_tests(redis)
    key = lease_redis_key("default", "demo")
    redis.set(
        key,
        json.dumps({"token": "railway", "fence": 4, "host": "box-1", "pid": 9}),
        ex=30,
    )
    with patch("core.operator_notify.notify_operator") as notify:
        ok = ensure_writer_lease()
        assert ok is False
        assert writer_lease_status() == "standby"
        assert writer_lease_held() is False
        assert notify.call_count == 1
        text = str(notify.call_args[0][0])
        assert "Kein Schreibrecht" in text
        assert "railway" in text
        assert "box-1" in text
        assert "Lesemodus" in text
        ensure_writer_lease()
        assert notify.call_count == 1


def test_ensure_writer_lease_redis_unavailable_is_readonly(monkeypatch):
    from bus.writer_lease import ensure_writer_lease

    _enable_lease()
    set_lease_redis_for_tests(None)
    monkeypatch.setattr("bus.writer_lease.get_redis", lambda *a, **k: None)
    with patch("core.operator_notify.notify_operator") as notify:
        ok = ensure_writer_lease()
    assert ok is False
    assert writer_lease_status() == "standby"
    assert notify.call_count == 1


def test_positions_flush_reenters_held_ledger_lock():
    from bus.locks import LedgerLock
    from strategies.positions import flush_positions

    done = threading.Event()
    errors: list = []

    def _run():
        try:
            with LedgerLock("demo", tenant_id="default", enabled=False):
                flush_positions(scope="demo", force=True)
            done.set()
        except BaseException as exc:
            errors.append(exc)

    t = threading.Thread(target=_run)
    t.start()
    assert done.wait(timeout=2.0), f"re-entrant flush deadlocked: {errors}"
    t.join(timeout=1.0)
    assert not t.is_alive()
    assert errors == []


def test_lease_key_uses_redis_key_prefix():
    from bus.redis_keys import redis_key_prefix

    prefix = redis_key_prefix()
    assert lease_redis_key("default", "demo") == f"{prefix}lease:writer:default:demo"
    assert fence_redis_key("default", "demo") == f"{prefix}lease:fence:default:demo"


def test_shutdown_writer_lease_is_noop_when_disabled():
    shutdown_writer_lease()
    assert writer_lease_status() == "disabled"
    assert writer_lease_fence() is None


def test_health_503_when_lease_lost_after_being_held():
    from core.cycle_health import health_payload, reset_cycle_health_for_tests
    import services.architecture_runtime as rt

    _enable_lease()
    reset_cycle_health_for_tests()
    rt.reset_recovery_state_for_tests()
    clock = FakeClock()
    redis = FakeRedis(clock)
    lease = _lease(redis, clock, tenant="default", scope="demo")
    set_process_lease_for_tests(lease)
    assert lease.acquire(start_renewer=False) is True
    with rt._recovery_lock:
        rt._recovered.add(("default", "demo"))
    assert health_payload(update_interval=60)[1] == 200
    lease._mark_lost()
    body, status = health_payload(update_interval=60)
    assert status == 503
    assert body["writer_lease"] == "lost"
    assert body["status"] == "not_ready"


def test_require_lease_waits_for_recovery_callback_after_takeover():
    """Review (#306): held lease + registered on_acquired -> orders wait for the reconcile."""
    import bus.writer_lease as wl
    import services.architecture_runtime as rt

    _enable_lease()
    rt.reset_recovery_state_for_tests()
    clock = FakeClock()
    redis = FakeRedis(clock)
    lease = _lease(redis, clock, tenant="default", scope="demo")
    set_process_lease_for_tests(lease)
    assert lease.acquire(start_renewer=False) is True
    wl._on_acquired = lambda: None  # a recovery callback is registered
    with pytest.raises(WriterLeaseLost) as ei:
        require_lease_for_order()
    assert ei.value.reason == "recovery_pending"
    with rt._recovery_lock:
        rt._recovered.add(("default", "demo"))
    require_lease_for_order()  # passes once recovery is recorded
    wl._on_acquired = None
    require_lease_for_order()  # no callback registered -> lease alone suffices


def test_on_acquired_runs_in_side_thread_and_reset_joins_it():
    import bus.writer_lease as wl

    seen: dict = {}
    started = threading.Event()
    release = threading.Event()

    def cb():
        seen["thread"] = threading.current_thread().name
        started.set()
        release.wait(timeout=5)

    wl._on_acquired = cb
    wl._fire_on_acquired_async()
    assert started.wait(timeout=5)
    assert seen["thread"] == "writer-lease-on-acquired"
    release.set()
    wl.reset_writer_lease_for_tests()
    assert not any(t.name == "writer-lease-on-acquired" and t.is_alive() for t in threading.enumerate())

