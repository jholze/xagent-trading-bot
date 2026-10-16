"""Lock-bug repro 16.10.2026 (staging): MCP xagent_lock -> bot_http -> set_position_lock.

Every test uses an in-memory fake ledger (no Mongo, no Redis). Thread interleavings
are forced deterministically with hooks so the outcome does not depend on timing:
a second thread runs *real* bot code (what the trading cycle does) inside the
window between two statements of set_position_lock.
"""

from __future__ import annotations

import copy
import threading
from contextlib import nullcontext

import pytest
from flask import Flask

import strategies.positions as P
from core.tenant_context import tenant_context

_PATH = "/internal/mcp/execute"
_CFG = {
    "trading_mode": "paper",
    "mcp": {
        "enabled": True,
        "allow_writes": True,
        "allow_live": False,
        "tenants": ["default", "henry", "ctexp"],
    },
}


def _lot(amount: float, avg: float) -> dict:
    pos = P._empty_position()
    pos.update(
        {
            "amount": amount,
            "peak_amount": amount,
            "average_entry": avg,
            "entry_price": avg,
            "last_buy_price": avg,
            "sold_percent": 0.0,
            "first_buy_at": "2026-10-10T00:00:00+00:00",
            "last_action": "BUY",
        }
    )
    return pos


class FakeLedger:
    """(tenant, scope) -> positions document; tenant -> order-derived lots."""

    def __init__(self):
        self.docs: dict[tuple[str, str], dict] = {}
        self.orders: dict[str, dict] = {
            # Real live shapes from xagent_lots 06:24 UTC (amount/avg rounded).
            "default": {"ZEC_USDT_4h": _lot(1.5945, 1534.20)},
            "henry": {
                "ZEC_USDT_4h": _lot(2.2322, 1487.31),
                "SKYAI_USDT_4h": _lot(96023.85, 0.06533),
            },
        }

    def load_doc(self, target, tenant_id=None, **_k):
        return copy.deepcopy(self.docs.get((tenant_id, target), {"positions": {}}))

    def save_doc(self, payload, target, tenant_id=None, **_k):
        self.docs[(tenant_id, target)] = copy.deepcopy(payload)
        return True

    def orders_snap(self, target, tenant_id=None, **_k):
        return copy.deepcopy(self.orders.get(tenant_id, {}))

    def persisted_lock(self, tenant: str, key: str):
        row = self.docs.get((tenant, "paper"), {}).get("positions", {}).get(key) or {}
        lock = row.get("lock")
        return lock if isinstance(lock, dict) and lock.get("enabled", True) else None


@pytest.fixture
def ledger(monkeypatch):
    fl = FakeLedger()
    monkeypatch.setattr("data_manager.get_config", lambda **_k: dict(_CFG))
    monkeypatch.setattr("data_manager.resolve_ledger_scope", lambda *_a, **_k: "paper")
    monkeypatch.setattr(P, "resolve_ledger_scope", lambda *_a, **_k: "paper")
    monkeypatch.setattr(P, "load_positions_document", fl.load_doc)
    monkeypatch.setattr(P, "save_positions_document", fl.save_doc)
    monkeypatch.setattr(
        "services.ledger_sync._build_positions_snapshot_from_orders", fl.orders_snap
    )
    monkeypatch.setattr("bus.locks.ledger_lock", lambda *_a, **_k: nullcontext())
    monkeypatch.setenv("MCP_BOT_TOKEN", "t0k")
    P.reset_all_position_stores_for_tests()
    # Bot start: default book in RAM; henry is loaded by its cycle.
    with tenant_context("default", scope="paper"):
        P.load_positions(scope="paper", tenant_id="default")
    yield fl
    P.reset_all_position_stores_for_tests()


def _client():
    from services.mcp.bot_http import register_mcp_bot_routes

    app = Flask(__name__)
    register_mcp_bot_routes(app)
    return app.test_client()


def _post_lock(client, tenant, symbol, tf):
    return client.post(
        _PATH,
        json={
            "action": "lock",
            "tenant_id": tenant,
            "symbol": symbol,
            "timeframe": tf,
            "actor_id": "owner",
            "reason": "lockbug repro",
        },
        headers={"X-Exit-Ws-Token": "t0k"},
    )


def _in_cycle_thread(fn):
    """Run *fn* on another thread (the trading-cycle thread) and wait for it."""
    err: list[BaseException] = []

    def _run():
        try:
            fn()
        except BaseException as e:  # pragma: no cover - surfaced below
            err.append(e)

    t = threading.Thread(target=_run, name="cycle")
    t.start()
    t.join(10)
    assert not t.is_alive(), "cycle thread hung"
    if err:
        raise err[0]


def _henry_cycle_touches_position():
    # What _run_tenant_price_cycle does for every symbol under
    # tenant_cycle_context("henry"): any positions accessor re-activates henry.
    with tenant_context("henry", scope="paper"):
        P.get_position("BTC/USDT", "4h")


def _default_cycle_touches_position():
    with tenant_context("default", scope="paper"):
        P.get_position("BTC/USDT", "4h")


def _henry_cycle_ran():
    """Prod precondition: henry's cycle has loaded henry's RAM book, then restored the global."""
    prev = P._active_key
    with tenant_context("henry", scope="paper"):
        P.activate_tenant_positions(scope="paper", tenant_id="henry")
    P._activate(prev)


# ---------------------------------------------------------------- defect A
def test_A_lock_for_default_must_not_land_on_henry_while_henry_cycle_runs(ledger, monkeypatch):
    """06:23:55 UTC: xagent_lock(ZEC/USDT, default, 4h) -> ok, lock landed on henry ZEC 4h."""
    # henry cycle already active (it reloads henry from the ledger).
    with tenant_context("henry", scope="paper"):
        P.activate_tenant_positions(scope="paper", tenant_id="henry")

    orig_init = P.init_position

    def init_then_cycle_switch(symbol, timeframe):
        orig_init(symbol, timeframe)  # HTTP thread: _activate((default, paper))
        _in_cycle_thread(_henry_cycle_touches_position)  # cycle thread: _activate((henry, paper))

    monkeypatch.setattr(P, "init_position", init_then_cycle_switch)

    rv = _post_lock(_client(), "default", "ZEC/USDT", "4h")
    assert rv.status_code == 200 and rv.get_json()["ok"] is True

    assert ledger.persisted_lock("henry", "ZEC_USDT_4h") is None, (
        "lock for tenant=default was written to henry ZEC_USDT_4h"
    )
    assert ledger.persisted_lock("default", "ZEC_USDT_4h") is not None, (
        "default ZEC_USDT_4h not locked although bot_http answered ok"
    )


# ---------------------------------------------------------------- defect B
def test_B1_henry_lock_survives_when_default_cycle_switches_before_flush(ledger, monkeypatch):
    """SKYAI (henry 4h) lost: flush_positions pins the *global* _active_key tenant."""
    _henry_cycle_ran()
    orig_flush = P.flush_positions

    def cycle_switch_then_flush(*a, **k):
        _in_cycle_thread(_default_cycle_touches_position)
        return orig_flush(*a, **k)

    monkeypatch.setattr(P, "flush_positions", cycle_switch_then_flush)

    rv = _post_lock(_client(), "henry", "SKYAI/USDT", "4h")
    assert rv.status_code == 200 and rv.get_json()["ok"] is True

    # Next henry cycle: tenant_cycle_context -> activate_tenant_positions reloads from ledger.
    monkeypatch.setattr(P, "flush_positions", orig_flush)
    with tenant_context("henry", scope="paper"):
        P.activate_tenant_positions(scope="paper", tenant_id="henry")
        ram_lock = (P.get_position("SKYAI/USDT", "4h") or {}).get("lock")
    assert ledger.persisted_lock("henry", "SKYAI_USDT_4h") is not None, (
        "henry SKYAI lock never persisted (flush went to another tenant)"
    )
    assert ram_lock, "henry SKYAI lock gone after henry cycle reload"


def test_B2_henry_lock_survives_cycle_reload_between_write_and_flush(ledger, monkeypatch):
    """Henry cycle reload (store.clear() + rebuild from ledger) lands in the write->flush gap."""
    _henry_cycle_ran()
    orig_flush = P.flush_positions

    def henry_reload_then_flush(*a, **k):
        def _reload():
            with tenant_context("henry", scope="paper"):
                P.activate_tenant_positions(scope="paper", tenant_id="henry")

        _in_cycle_thread(_reload)
        return orig_flush(*a, **k)

    monkeypatch.setattr(P, "flush_positions", henry_reload_then_flush)
    rv = _post_lock(_client(), "henry", "SKYAI/USDT", "4h")
    assert rv.status_code == 200 and rv.get_json()["ok"] is True
    assert ledger.persisted_lock("henry", "SKYAI_USDT_4h") is not None, (
        "lock wiped by henry reload before flush; bot_http still answered ok"
    )


def test_B0_sequential_lock_without_interleaving_persists(ledger):
    """Control (GREEN): no concurrent cycle -> henry lock persists. The bug needs the race."""
    _henry_cycle_ran()
    rv = _post_lock(_client(), "henry", "SKYAI/USDT", "4h")
    assert rv.get_json()["ok"] is True
    assert ledger.persisted_lock("henry", "SKYAI_USDT_4h") is not None
    rv = _post_lock(_client(), "default", "ZEC/USDT", "4h")
    assert ledger.persisted_lock("default", "ZEC_USDT_4h") is not None
    assert ledger.persisted_lock("henry", "ZEC_USDT_4h") is None


def test_B3_lock_for_tenant_whose_ram_book_is_not_loaded_persists(ledger):
    """henry RAM book not loaded yet (bot restart, before henry's first cycle):
    set_position_lock creates an empty shell, _position_persistable() drops it,
    the flush rewrites henry's doc from an empty book -- and bot_http says ok."""
    rv = _post_lock(_client(), "henry", "SKYAI/USDT", "4h")
    assert rv.get_json()["ok"] is True
    assert ledger.persisted_lock("henry", "SKYAI_USDT_4h") is not None, (
        "lock dropped: HTTP path never loads the tenant book (no activate/bootstrap)"
    )


# ---------------------------------------------------------------- defect C
def test_C_lock_on_missing_lot_is_not_ok(ledger):
    """Lock on a symbol/tf without a lot: answers ok, persists nothing (empty shell)."""
    rv = _post_lock(_client(), "default", "NOPE/USDT", "4h")
    body = rv.get_json()
    assert ledger.persisted_lock("default", "NOPE_USDT_4h") is None  # nothing stored
    assert body.get("ok") is False, f"ok without a lot/persist: {body}"


def test_C2_ok_response_names_tenant_and_is_read_back(ledger):
    """bot_http answers ok without saying which tenant got the lock (no read-back)."""
    _henry_cycle_ran()
    body = _post_lock(_client(), "henry", "SKYAI/USDT", "4h").get_json()
    assert body.get("tenant_id") == "henry", f"no tenant echo/read-back in {body}"


# ---------------------------------------------------------------- MCP side / retry
def test_D_rate_limited_retry_keeps_requested_tenant():
    """GREEN: rate_limited short-circuits before the bot; the retry sends tenant_id=default."""
    from services.mcp.authz import Actor, reset_write_rate
    from services.mcp.tokens import OWNER_CAPS
    from services.mcp.tools import _write

    reset_write_rate()
    owner = Actor("owner", "owner", ("*",), OWNER_CAPS)
    sent: list[dict] = []

    def fake_exec(**body):
        sent.append(body)
        return {"ok": True}

    payload = {"symbol": "ZEC/USDT", "reason": "x", "timeframe": "4h"}
    t0 = 1_000_000.0
    first = _write(owner, "lock", "lock", "henry", fake_exec, payload, rate_per_min=1, now=t0)
    limited = _write(owner, "lock", "lock", "default", fake_exec, payload, rate_per_min=1, now=t0 + 1)
    retry = _write(owner, "lock", "lock", "default", fake_exec, payload, rate_per_min=1, now=t0 + 81)
    reset_write_rate()
    assert first["ok"] and retry["ok"]
    assert limited == {"ok": False, "error": "rate_limited"}
    assert [b["tenant_id"] for b in sent] == ["henry", "default"]


def test_D2_scoped_actor_requesting_other_tenant_is_rejected_not_remapped():
    """Side finding: a single-tenant operator token silently rewrites tenant (tools.py:58-59)."""
    from services.mcp.authz import Actor, reset_write_rate
    from services.mcp.tools import _write

    reset_write_rate()
    op = Actor("kay", "operator", ("henry",), ("read", "trade", "lock"))
    sent: list[dict] = []
    out = _write(
        op, "lock", "lock", "default",
        lambda **b: sent.append(b) or {"ok": True},
        {"symbol": "ZEC/USDT", "timeframe": "4h"},
        rate_per_min=100, now=2_000_000.0,
    )
    reset_write_rate()
    assert out.get("ok") is False and out.get("error") == "tenant_forbidden", (
        f"tenant=default silently remapped to {[b['tenant_id'] for b in sent]}"
    )


# ---------------------------------------------------------------- verification path
def test_E_xagent_lots_reads_requested_tenant_from_ledger(ledger, monkeypatch):
    """GREEN: sidecar lots path reads the requested tenant from the ledger, not _active_key."""
    from services.desk.snapshot import _load_lots

    ledger.docs[("henry", "paper")] = {
        "positions": {"ZEC_USDT_4h": {"lock": {"enabled": True, "locked_by": "mcp:owner"}}}
    }
    P.reset_all_position_stores_for_tests()  # sidecar process: empty RAM
    P._activate(("default", "paper"))  # global points elsewhere
    lots = {(l["symbol"], l["timeframe"]): l for l in _load_lots("henry")}
    assert ("SKYAI/USDT", "4h") in lots
    assert (lots[("ZEC/USDT", "4h")].get("lock") or {}).get("enabled") is True
    assert not (lots[("SKYAI/USDT", "4h")].get("lock") or {}).get("enabled")
    dflt = {(l["symbol"], l["timeframe"]): l for l in _load_lots("default")}
    assert not (dflt[("ZEC/USDT", "4h")].get("lock") or {}).get("enabled")


def test_A_stress_no_hooks_real_threads(ledger):
    """Same as A without injected hooks: a henry cycle thread keeps touching positions
    while the HTTP thread locks default ZEC 4h 200x. Any lock on henry = defect A."""
    import sys

    _henry_cycle_ran()
    stop = threading.Event()

    def _cycle():
        with tenant_context("henry", scope="paper"):
            while not stop.is_set():
                P.get_position("BTC/USDT", "4h")

    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-5)
    t = threading.Thread(target=_cycle, name="cycle-henry")
    t.start()
    client = _client()
    misrouted = 0
    try:
        for _ in range(200):
            _post_lock(client, "default", "ZEC/USDT", "4h")
            if ledger.persisted_lock("henry", "ZEC_USDT_4h") is not None:
                misrouted += 1
                ledger.docs[("henry", "paper")]["positions"]["ZEC_USDT_4h"].pop("lock", None)
                with P._positions_lock:
                    P._store_for_key(("henry", "paper"))["ZEC_USDT_4h"].pop("lock", None)
    finally:
        stop.set()
        t.join(10)
        sys.setswitchinterval(old)
    assert misrouted == 0, f"{misrouted}/200 default locks landed on henry ZEC_USDT_4h"
