"""Tenant-race bundle (#698 positions timers, #699 config snapshots).

AC ids follow spec_bundle_tenant_race_v2 plus the annex and the erratum
(Lena PASS). Short names in the docstrings match the review (T698, T699,
ax_d2, ax_e1, ax_e4, ax_b, ax_c).

No production coin names. Overlays below are test values, not config.json.
"""

from __future__ import annotations

import copy
import threading
import traceback
from contextlib import nullcontext

import pytest

import strategies.positions as P
from core.config import BotConfig
from core.tenant_context import tenant_context
from core.tenant_routing import tenant_cycle_context

OVERLAY_DEFAULT = 111.0
OVERLAY_HENRY = 222.0
SYM = "LAB/USDT"
TF = "1h"
LOT_KEY = "LAB_USDT_1h"


class _FakeTimer:
    """Stand-in for threading.Timer. The test fires ``function`` itself."""

    def __init__(self, interval, function):
        self.interval = interval
        self.function = function
        self.daemon = False
        self.cancelled = False
        self.started = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


def _reload(tenant_id=None):
    from core.tenant_context import resolve_tenant_id

    tid = tenant_id or resolve_tenant_id()
    cap = OVERLAY_HENRY if tid == "henry" else OVERLAY_DEFAULT
    return {
        "max_usdt_per_trade": cap,
        "virtual_trading": True,
        "trading_mode": "paper",
        "tenant_marker": tid,
    }


def _fresh_config() -> BotConfig:
    return BotConfig(
        {
            "max_usdt_per_trade": 1,
            "virtual_trading": True,
            "trading_mode": "paper",
            "tenant_marker": "unset",
        }
    )


def _patch_cycle(monkeypatch):
    monkeypatch.setattr("core.config.reload_config", _reload)
    monkeypatch.setattr(
        "storage.tenant_registry.get_tenant",
        lambda tid, test=False: {
            "tenant_id": tid,
            "telegram": {"headless": True},
            "defaults": {"ledger_scope": "paper"},
        },
    )

    def _activate(**_k):
        return None

    monkeypatch.setattr(P, "activate_tenant_positions", _activate)


def _run_pair(fn):
    errors: list[BaseException] = []
    threads = [
        threading.Thread(target=_guard, args=(fn, "default", errors), name="t-default"),
        threading.Thread(target=_guard, args=(fn, "henry", errors), name="t-henry"),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    if errors:
        raise errors[0]


def _guard(fn, tenant, errors):
    try:
        fn(tenant)
    except BaseException as exc:  # surfaced by the caller
        errors.append(exc)


def _lot() -> dict:
    pos = P._empty_position()
    pos.update(
        {
            "amount": 2.0,
            "peak_amount": 2.0,
            "average_entry": 10.0,
            "entry_price": 10.0,
            "last_buy_price": 10.0,
            "sold_percent": 0.0,
            "first_buy_at": "2026-01-02T00:00:00+00:00",
            "last_action": "BUY",
        }
    )
    return pos


def _install_ledger(monkeypatch):
    docs: dict[tuple[str, str], dict] = {}

    def load_doc(target, tenant_id=None, **_k):
        return copy.deepcopy(docs.get((tenant_id, target), {"positions": {}}))

    def save_doc(payload, target, tenant_id=None, **_k):
        docs[(tenant_id, target)] = copy.deepcopy(payload)
        return True

    monkeypatch.setattr(P, "load_positions_document", load_doc)
    monkeypatch.setattr(P, "save_positions_document", save_doc)
    monkeypatch.setattr("bus.locks.ledger_lock", lambda *_a, **_k: nullcontext())
    monkeypatch.setattr(P.threading, "Timer", _FakeTimer)
    monkeypatch.setattr(P, "_FLUSH_DEBOUNCE_SEC", 60.0)
    return docs


def _seed_known(tenant: str, *, lock: bool = False) -> None:
    with tenant_context(tenant, scope="paper"):
        key = (tenant, "paper")
        P._activate(key)
        lot = _lot()
        if lock:
            lot["lock"] = {"enabled": True, "reason": tenant}
        P._store_for_key(key)[LOT_KEY] = lot
        P._mark_positions_known(key)


def _flush_as(tenant: str, *, force: bool = False) -> None:
    """Arm or force a flush on ``tenant``'s book, not the leftover thread key."""
    with tenant_context(tenant, scope="paper"):
        P._activate((tenant, "paper"))
        P.flush_positions(scope="paper", force=force)


def _lock_enabled(docs, tenant: str) -> bool:
    row = (docs.get((tenant, "paper"), {}).get("positions") or {}).get(LOT_KEY) or {}
    lock = row.get("lock")
    return bool(isinstance(lock, dict) and lock.get("enabled", True))


@pytest.fixture
def books(monkeypatch):
    P.reset_all_position_stores_for_tests()
    docs = _install_ledger(monkeypatch)
    yield docs
    P.reset_all_position_stores_for_tests()


def test_ac_001a_t698_per_key_debounce_does_not_cancel_the_other(books):
    """AC-001a / T698: two debounced flushes stay armed together."""
    _flush_as("default")
    _flush_as("henry")
    assert set(P._flush_timers) == {("default", "paper"), ("henry", "paper")}
    assert all(not timer.cancelled for timer in P._flush_timers.values())


def test_ac_001b_t698b_debounce_callback_keeps_scheduled_tenant(books):
    """AC-001b / T698b: the callback saves the tenant captured at schedule time."""
    _seed_known("henry", lock=True)
    _flush_as("henry")
    P._activate(("default", "paper"))
    P._flush_timers[("henry", "paper")].function()
    assert _lock_enabled(books, "henry")
    assert ("default", "paper") not in books


def test_ac_001c_ax_d2_force_flush_leaves_the_other_timer(books):
    """AC-001c / ax_d2: force flush writes one book and leaves the other timer."""
    _seed_known("default", lock=True)
    _seed_known("henry", lock=True)
    _flush_as("default")
    _flush_as("henry")
    assert set(P._flush_timers) == {("default", "paper"), ("henry", "paper")}
    _flush_as("default", force=True)
    assert _lock_enabled(books, "default")
    assert not _lock_enabled(books, "henry")
    assert ("henry", "paper") in P._flush_timers
    assert not P._flush_timers[("henry", "paper")].cancelled
    assert ("default", "paper") not in P._flush_timers


def test_ac_001d_ax_e1_shutdown_flushes_every_known_book(books):
    """AC-001d / ax_e1: shutdown persists every known book and only those timers."""
    _seed_known("default", lock=True)
    _seed_known("henry", lock=True)
    _flush_as("default")
    _flush_as("henry")
    assert len(P._flush_timers) == 2
    assert books == {}
    P.flush_known_position_stores()
    assert _lock_enabled(books, "default")
    assert _lock_enabled(books, "henry")
    assert P._flush_timers == {}


def test_ac_001d_v2a_force_flush_does_not_cancel_a_foreign_timer(books):
    """AC-001d / V2a: a foreign debounce survives force=True."""
    _flush_as("henry")
    foreign = P._flush_timers[("henry", "paper")]
    _flush_as("default", force=True)
    assert P._flush_timers.get(("henry", "paper")) is foreign
    assert foreign.cancelled is False


def test_ac_001d_v2b_shutdown_leaves_a_timer_it_does_not_flush(books):
    """AC-001d / V2b: a debounce whose book is not known stays armed."""
    _flush_as("henry")
    foreign = P._flush_timers[("henry", "paper")]
    P.flush_known_position_stores()
    assert P._flush_timers.get(("henry", "paper")) is foreign
    assert foreign.cancelled is False
    assert ("henry", "paper") not in books


def test_ac_002a_t699_shared_refresh_reads_own_overlay(monkeypatch):
    """AC-002a / T699: two threads sharing one BotConfig see their own cap."""
    _patch_cycle(monkeypatch)
    cfg = _fresh_config()
    barrier = threading.Barrier(2)
    seen: dict[str, float] = {}

    def worker(tenant: str):
        with tenant_cycle_context(tenant):
            barrier.wait(5)
            cfg.refresh()
            barrier.wait(5)
            seen[tenant] = cfg.max_usdt_per_trade

    _run_pair(worker)
    assert seen["default"] == OVERLAY_DEFAULT
    assert seen["henry"] == OVERLAY_HENRY


def test_ac_002b_t699b_second_refresh_does_not_clobber(monkeypatch):
    """AC-002b / T699b: a later refresh on the other thread keeps the first cap."""
    _patch_cycle(monkeypatch)
    cfg = _fresh_config()
    barrier = threading.Barrier(2)
    seen: dict[str, float] = {}

    def worker(tenant: str):
        with tenant_cycle_context(tenant):
            barrier.wait(5)
            cfg.refresh()
            barrier.wait(5)
            cfg.refresh()
            barrier.wait(5)
            seen[tenant] = float(cfg.raw["max_usdt_per_trade"])

    _run_pair(worker)
    assert seen["default"] == OVERLAY_DEFAULT
    assert seen["henry"] == OVERLAY_HENRY


def test_ac_002c_t699c_separate_configs_do_not_share_a_raw_dict(monkeypatch):
    """AC-002c / T699c: two BotConfig objects do not alias one overlay dict."""
    _patch_cycle(monkeypatch)
    barrier = threading.Barrier(2)
    seen: dict[str, tuple[float, int]] = {}

    def worker(tenant: str):
        cfg = _fresh_config()
        with tenant_context(tenant, scope="paper"):
            barrier.wait(5)
            cfg.refresh()
            barrier.wait(5)
            seen[tenant] = (cfg.max_usdt_per_trade, id(cfg.raw))

    _run_pair(worker)
    assert seen["default"][0] == OVERLAY_DEFAULT
    assert seen["henry"][0] == OVERLAY_HENRY
    assert seen["default"][1] != seen["henry"][1]


def test_ac_003a_ax_b_serialize_hook_fires_once(books):
    """AC-003a / ax_b: the save serializes the pinned book once (B1 == 1)."""
    _seed_known("henry")
    calls = {"n": 0}
    orig = P._serialize_positions

    def _wrapped(*args, **kwargs):
        calls["n"] += 1
        return orig(*args, **kwargs)

    books_patch = books  # keep the fixture's save path
    assert books_patch is books
    with P._positions_lock:
        pass
    orig_serialize = P._serialize_positions
    P._serialize_positions = _wrapped
    try:
        with tenant_context("henry", scope="paper"):
            P.set_position_lock(
                SYM, TF, {"enabled": True, "reason": "ax-b"}, persist=True
            )
    finally:
        P._serialize_positions = orig_serialize
    assert calls["n"] == 1


def test_ac_003b_ax_c_get_key_hook_runs_inside_set_position_lock(books):
    """AC-003b / ax_c: the A hook is the get_key call inside set_position_lock."""
    _seed_known("default")
    with tenant_context("default", scope="paper"):
        P.get_position(SYM, TF)
    seen: list[bool] = []
    orig = P.get_key

    def _wrapped(symbol, timeframe):
        stack = "".join(traceback.format_stack())
        seen.append("set_position_lock" in stack)
        return orig(symbol, timeframe)

    P.get_key = _wrapped
    try:
        with tenant_context("default", scope="paper"):
            P.set_position_lock(
                SYM, TF, {"enabled": True, "reason": "ax-c"}, persist=True
            )
    finally:
        P.get_key = orig
    assert seen, "set_position_lock did not call get_key"
    assert seen == [True]
    assert len(seen) == 1


def test_ac_003c_v3_lock_stays_on_the_request_tenant(books):
    """AC-003c / V3: a concurrent henry reader does not take default's lock."""
    _seed_known("default")
    _seed_known("henry")
    stop = threading.Event()

    def _cycle():
        with tenant_context("henry", scope="paper"):
            while not stop.is_set():
                P.get_position(SYM, TF)

    thread = threading.Thread(target=_cycle, name="cycle-henry")
    thread.start()
    try:
        for _ in range(40):
            with tenant_context("default", scope="paper"):
                P.set_position_lock(
                    SYM, TF, {"enabled": True, "reason": "v3"}, persist=True
                )
            assert _lock_enabled(books, "default")
            assert not _lock_enabled(books, "henry")
    finally:
        stop.set()
        thread.join(10)
    assert not thread.is_alive()


def test_ac_001e_ax_e4_shutdown_persists_both_ram_locks(books):
    """AC-001e / ax_e4: both known RAM locks are absent until shutdown, then both saved.

    The per-key registry holds two timers. The lock assertion is the shutdown
    flush: the spy is empty before it and holds both tenants after it.
    """
    _seed_known("default", lock=True)
    _seed_known("henry", lock=True)
    _flush_as("default")
    _flush_as("henry")
    assert len(P._flush_timers) == 2
    assert books == {}
    P.flush_known_position_stores()
    assert _lock_enabled(books, "default")
    assert _lock_enabled(books, "henry")


def _mongo_cfg(base: dict) -> dict:
    cfg = copy.deepcopy(base)
    cfg["trading_mode"] = "paper"
    cfg["virtual_trading"] = True
    cfg.setdefault("architecture", {})
    cfg["architecture"]["ledger_backend"] = "mongo"
    cfg["architecture"]["ledger_dual_write"] = False
    cfg["architecture"]["single_writer_lease_enabled"] = False
    cfg.setdefault("paper", {})
    cfg["paper"]["backend"] = "mongo"
    return cfg


def test_ac_001e_ram_lock_roundtrip_uses_mongo(monkeypatch):
    """AC-001e RAM variant: both locks land in the positions collection, reload once.

    Removing henry's lock in the raw collection clears it on the next load.
    """
    from storage.mongo_client import drop_database, get_database
    from storage.mongo_ledger import POSITIONS_COLLECTION
    from storage.tenant_keys import compound_ledger_id

    drop_database(test=True)
    try:
        from data_manager import get_config

        cfg = _mongo_cfg(get_config())
        monkeypatch.setattr("data_manager.get_config", lambda *a, **k: cfg)
        monkeypatch.setattr("core.config.get_config", lambda *a, **k: cfg)
        monkeypatch.setattr("bus.writer_lease.lease_enabled", lambda: False)
        loads = {"n": 0}

        def _orders(*_a, **_k):
            loads["n"] += 1
            return {LOT_KEY: _lot()}

        monkeypatch.setattr(
            "services.ledger_sync._build_positions_snapshot_from_orders", _orders
        )
        P.reset_all_position_stores_for_tests()
        for tenant in ("default", "henry"):
            _seed_known(tenant, lock=True)
            _flush_as(tenant, force=True)

        db = get_database()
        coll = db[POSITIONS_COLLECTION]
        for tenant in ("default", "henry"):
            doc = coll.find_one({"_id": compound_ledger_id(tenant, "paper")})
            assert doc, tenant
            lock = (doc.get("positions") or {}).get(LOT_KEY, {}).get("lock") or {}
            assert lock.get("enabled") is True, doc

        loads["n"] = 0
        with tenant_context("henry", scope="paper"):
            P.load_positions(scope="paper", tenant_id="henry")
            ram = P.get_position(SYM, TF)
        assert loads["n"] == 1
        assert (ram.get("lock") or {}).get("enabled") is True

        doc = coll.find_one({"_id": compound_ledger_id("henry", "paper")})
        doc["positions"][LOT_KEY].pop("lock", None)
        coll.replace_one({"_id": doc["_id"]}, doc)
        with tenant_context("henry", scope="paper"):
            P.load_positions(scope="paper", tenant_id="henry")
            ram = P.get_position(SYM, TF)
        assert not (ram.get("lock") or {}).get("enabled")
    finally:
        P.reset_all_position_stores_for_tests()
        drop_database(test=True)
