"""Lena (2nd review PR #704) race and fresh-thread tests.

X1, X3, X4, X4b, X5 are her probes. Buy and sell go through
/internal/mcp/execute while a foreign cycle thread runs. C3 checks that a
failed lock read-back does not leave the new lock in RAM.
"""

from __future__ import annotations

import sys
import threading
from types import SimpleNamespace

import pytest

import strategies.positions as P
from core.tenant_context import resolve_tenant_id, tenant_context
from tests.unit.test_mcp_lock_tenant_race_lockbug import (  # noqa: F401
    _client,
    _henry_cycle_ran,
    ledger,
)

N = 100


def _post(client, action, tenant, symbol, tf, **extra):
    body = {
        "action": action,
        "tenant_id": tenant,
        "symbol": symbol,
        "timeframe": tf,
        "actor_id": "owner",
        "reason": "lena",
    }
    body.update(extra)
    return client.post(
        "/internal/mcp/execute",
        json=body,
        headers={"X-Exit-Ws-Token": "t0k"},
    )


def _doc_lock_enabled(fl, tenant, key):
    row = fl.docs.get((tenant, "paper"), {}).get("positions", {}).get(key) or {}
    lock = row.get("lock")
    return isinstance(lock, dict) and bool(lock.get("enabled", True))


def _doc_amount(fl, tenant, key):
    row = fl.docs.get((tenant, "paper"), {}).get("positions", {}).get(key) or {}
    return float(row.get("amount") or 0)


def _run_bg(target):
    stop = threading.Event()

    def _loop():
        while not stop.is_set():
            target()

    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-5)
    thread = threading.Thread(target=_loop, name="bg-cycle", daemon=True)
    thread.start()
    return stop, thread, old


def _stop_bg(stop, thread, old):
    stop.set()
    thread.join(10)
    sys.setswitchinterval(old)
    assert not thread.is_alive(), "bg cycle thread hung"


def _alternate(fl, tenant, sym, key):
    client = _client()
    bad_resp = bad_persist = 0
    for i in range(N):
        action = "lock" if i % 2 == 0 else "unlock"
        rv = _post(client, action, tenant, sym, "4h")
        body = rv.get_json() or {}
        if rv.status_code != 200 or body.get("ok") is not True:
            bad_resp += 1
            continue
        if _doc_lock_enabled(fl, tenant, key) != (action == "lock"):
            bad_persist += 1
    return bad_resp, bad_persist


def test_X1_henry_lock_unlock_vs_henry_reload_stress(ledger):
    """B2 without hooks: henry reload thread vs alternating lock/unlock."""
    _henry_cycle_ran()

    def _reload():
        with tenant_context("henry", scope="paper"):
            P.activate_tenant_positions(scope="paper", tenant_id="henry")
            P.get_position("BTC/USDT", "4h")

    background = _run_bg(_reload)
    try:
        bad_resp, bad_persist = _alternate(ledger, "henry", "SKYAI/USDT", "SKYAI_USDT_4h")
    finally:
        _stop_bg(*background)
    assert (bad_resp, bad_persist) == (0, 0), (
        f"X1 non-ok={bad_resp}/{N} persisted-mismatch={bad_persist}/{N}"
    )


def test_X3_field_write_vs_foreign_cycle_stress(ledger):
    """Default field write vs a henry cycle. The field must stay off henry's book."""
    _henry_cycle_ran()

    def _henry():
        with tenant_context("henry", scope="paper"):
            P.get_position("BTC/USDT", "4h")

    background = _run_bg(_henry)
    landed_on_henry = 0
    try:
        for i in range(200):
            with tenant_context("default", scope="paper"):
                P.set_position_field("ZEC/USDT", "4h", "lena_probe", i)
            with P._positions_lock:
                henry = P._store_for_key(("henry", "paper")).get("ZEC_USDT_4h") or {}
                if "lena_probe" in henry:
                    landed_on_henry += 1
                    henry.pop("lena_probe", None)
    finally:
        _stop_bg(*background)
    assert landed_on_henry == 0, f"X3 {landed_on_henry}/200 default field writes landed on henry"


def _go_live(monkeypatch):
    monkeypatch.setattr("data_manager.resolve_ledger_scope", lambda *_a, **_k: "live")
    monkeypatch.setattr(P, "resolve_ledger_scope", lambda *_a, **_k: "live")
    P.reset_all_position_stores_for_tests()
    with tenant_context("default", scope="live"):
        P.load_positions(scope="live", tenant_id="default")


def _in_fresh_thread(fn):
    out = {}

    def _run():
        out["v"] = fn()

    thread = threading.Thread(target=_run)
    thread.start()
    thread.join(10)
    return out.get("v")


def test_X4_fresh_thread_scope_is_config_scope_not_hardcoded_paper(ledger, monkeypatch):
    """A thread that never activated reads the configured scope, not paper."""
    _go_live(monkeypatch)
    scope = _in_fresh_thread(P.get_active_scope)
    assert scope == "live", f"X4 fresh-thread get_active_scope()={scope!r} with trading scope live"


def test_X4b_shutdown_flush_in_fresh_thread_targets_live_doc(ledger, monkeypatch):
    """flush_positions(scope=get_active_scope()) on a fresh thread writes the live doc."""
    _go_live(monkeypatch)
    ledger.docs.pop(("default", "paper"), None)
    ledger.docs.pop(("default", "live"), None)
    _in_fresh_thread(lambda: P.flush_positions(scope=P.get_active_scope(), force=True))
    wrote = sorted(k for k in ledger.docs if k[0] == "default")
    assert wrote == [("default", "live")], f"X4b shutdown flush wrote docs {wrote}"


def test_X5_count_open_positions_in_fresh_thread_after_restore_pattern(ledger, monkeypatch):
    """Restoring a key on a fresh thread must not zero the next thread's open count."""
    _go_live(monkeypatch)
    with tenant_context("default", scope="live"):
        expected = P.count_open_positions()

    def _restore_pattern():
        prev = tuple(P._active_key)
        P._activate(prev)

    _in_fresh_thread(_restore_pattern)
    got = _in_fresh_thread(P.count_open_positions)
    assert expected >= 1 and got == expected, f"X5 fresh-thread count={got} expected={expected}"


def _install_trade(monkeypatch, *, side: str, orders: list[dict]):
    def _trade(self, symbol, timeframe, *args, **kwargs):
        tid = resolve_tenant_id()
        if side == "buy":
            P.update_position(symbol, timeframe, "BUY", 100.0, 0.05)
        else:
            amount = kwargs.get("amount")
            if amount is None and len(args) >= 3:
                amount = args[2]
            P.update_position(symbol, timeframe, "SELL", 100.0, float(amount or 0))
        orders.append({"tenant_id": tid, "symbol": symbol, "side": side})
        return SimpleNamespace(executed=True, message="ok")

    name = "execute_buy" if side == "buy" else "execute_sell"
    monkeypatch.setattr(f"services.trading_service.TradingService.{name}", _trade)


def _amount(store_key, pos_key) -> float:
    row = P._store_for_key(store_key).get(pos_key) or {}
    return float(row.get("amount") or 0)


def test_buy_for_default_while_henry_cycle_runs(ledger, monkeypatch):
    """POST /internal/mcp/execute buy stays on default while henry's cycle runs."""
    _henry_cycle_ran()
    pos_key = "ZEC_USDT_4h"
    start_default = _amount(("default", "paper"), pos_key)
    start_henry = _amount(("henry", "paper"), pos_key)
    orders: list[dict] = []
    _install_trade(monkeypatch, side="buy", orders=orders)

    def _henry():
        with tenant_context("henry", scope="paper"):
            P.get_position("BTC/USDT", "4h")

    background = _run_bg(_henry)
    bad = 0
    try:
        client = _client()
        for _i in range(40):
            rv = _post(client, "buy", "default", "ZEC/USDT", "4h", price=100, usdt=5)
            body = rv.get_json() or {}
            if rv.status_code != 200 or body.get("ok") is not True:
                bad += 1
                continue
            if abs(_amount(("henry", "paper"), pos_key) - start_henry) > 1e-9:
                bad += 1
            if abs(_doc_amount(ledger, "default", pos_key) - _amount(("default", "paper"), pos_key)) > 1e-6:
                bad += 1
    finally:
        _stop_bg(*background)
    assert bad == 0, f"buy race mismatches={bad}"
    assert abs(_amount(("default", "paper"), pos_key) - (start_default + 2.0)) < 1e-6
    assert abs(_amount(("henry", "paper"), pos_key) - start_henry) < 1e-9
    assert orders and {row["tenant_id"] for row in orders} == {"default"}
    assert abs(_doc_amount(ledger, "henry", pos_key) - start_henry) < 1e-6 or _doc_amount(
        ledger, "henry", pos_key
    ) == 0.0


def test_sell_for_default_while_henry_cycle_runs(ledger, monkeypatch):
    """POST /internal/mcp/execute sell stays on default while henry's cycle runs."""
    _henry_cycle_ran()
    pos_key = "ZEC_USDT_4h"
    start_default = _amount(("default", "paper"), pos_key)
    start_henry = _amount(("henry", "paper"), pos_key)
    orders: list[dict] = []
    _install_trade(monkeypatch, side="sell", orders=orders)

    def _henry():
        with tenant_context("henry", scope="paper"):
            P.get_position("BTC/USDT", "4h")

    background = _run_bg(_henry)
    bad = 0
    try:
        client = _client()
        for _i in range(40):
            rv = _post(client, "sell", "default", "ZEC/USDT", "4h", price=100, amount=0.01)
            body = rv.get_json() or {}
            if rv.status_code != 200 or body.get("ok") is not True:
                bad += 1
                continue
            if abs(_amount(("henry", "paper"), pos_key) - start_henry) > 1e-9:
                bad += 1
            if abs(_doc_amount(ledger, "default", pos_key) - _amount(("default", "paper"), pos_key)) > 1e-6:
                bad += 1
    finally:
        _stop_bg(*background)
    assert bad == 0, f"sell race mismatches={bad}"
    assert abs(_amount(("default", "paper"), pos_key) - (start_default - 0.4)) < 1e-6
    assert abs(_amount(("henry", "paper"), pos_key) - start_henry) < 1e-9
    assert orders and {row["tenant_id"] for row in orders} == {"default"}


def test_C3_failed_readback_rolls_back_ram_lock(ledger, monkeypatch):
    """A lock that does not read back is removed from RAM."""
    _henry_cycle_ran()
    pos_key = "SKYAI_USDT_4h"

    def _drop_lock(payload, target, tenant_id=None, **_k):
        body = dict(payload)
        rows = dict(body.get("positions") or {})
        row = dict(rows.get(pos_key) or {})
        row.pop("lock", None)
        rows[pos_key] = row
        body["positions"] = rows
        ledger.docs[(tenant_id, target)] = body
        return True

    monkeypatch.setattr(P, "save_positions_document", _drop_lock)
    rv = _post(_client(), "lock", "henry", "SKYAI/USDT", "4h")
    assert rv.status_code == 500
    ram = P._store_for_key(("henry", "paper")).get(pos_key) or {}
    lock = ram.get("lock")
    assert not (isinstance(lock, dict) and lock.get("enabled", True)), (
        "RAM kept a lock the document did not read back"
    )
