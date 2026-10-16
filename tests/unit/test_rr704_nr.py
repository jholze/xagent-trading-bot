"""Fresh-thread regressions from the #704 write raise (NR1, NR2).

The pytest main thread receives a positions key from
``reset_all_position_stores_for_tests``. Child threads do not inherit
contextvars, so every check below runs on a new ``threading.Thread``.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

import strategies.positions as P
from core.tenant_context import DEFAULT_TENANT, tenant_context
from tests.unit.test_mcp_lock_tenant_race_lockbug import (  # noqa: F401
    _lot,
    ledger,
)

_ZEC = "ZEC/USDT"
_TF = "4h"
_ZEC_KEY = "ZEC_USDT_4h"


def _fresh(fn, *, stop: threading.Event | None = None, wake: threading.Event | None = None):
    out = {}

    def _run():
        try:
            out["v"] = fn()
        except BaseException as exc:  # noqa: BLE001
            out["exc"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(10)
    if thread.is_alive() and (stop is not None or wake is not None):
        if stop is not None:
            stop.set()
        if wake is not None:
            wake.set()
        thread.join(2)
    assert not thread.is_alive()
    return out


def _locked_lot() -> dict:
    pos = _lot(1.5945, 1534.20)
    pos["lock"] = {
        "enabled": True,
        "modes": ["no_auto_sell", "no_dca", "no_evict"],
        "reason": "ops",
        "until": None,
    }
    return pos


def _seed_locked_default() -> None:
    P.reset_all_position_stores_for_tests()
    P.positions[_ZEC_KEY] = _locked_lot()


def _dca_payload() -> dict:
    return {"symbol": _ZEC, "timeframe": _TF, "usdt": 10, "price": 100}


def test_NR1_fresh_thread_sees_open_zec_and_not_an_empty_symbol(monkeypatch):
    """Open ZEC is present. A symbol with no lot is not. Both from a fresh thread."""
    import services.entry_sensor_loop as sensor

    P.reset_all_position_stores_for_tests()
    P.positions[_ZEC_KEY] = _lot(1.5945, 1534.20)
    monkeypatch.setattr("core.tenant_context.multi_tenant_enabled", lambda: False)
    seen: dict = {}
    sensor._stop_event.clear()

    def once(_orch):
        from core.tenant_context import resolve_tenant_id

        if resolve_tenant_id() != DEFAULT_TENANT:
            return
        seen["zec"] = sensor._has_open_position(_ZEC, {"timeframe": _TF})
        seen["absent"] = sensor._has_open_position("NOPE/USDT", {"timeframe": _TF})
        sensor._stop_event.set()

    monkeypatch.setattr(sensor, "_poll_once", once)

    def worker():
        sensor._loop_main(None)

    try:
        out = _fresh(worker, stop=sensor._stop_event)
    finally:
        sensor._stop_event.set()
    assert "exc" not in out, out.get("exc")
    assert seen.get("zec") is True, seen
    assert seen.get("absent") is False, seen


def test_NR1_unreadable_book_does_not_request_a_second_entry(monkeypatch):
    """Context is set, so execute_if_needed will not skip on positions_key_missing.

    Pre-fix, _has_open_position turns the read error into False and
    process_entry_sensor runs: a would-be double entry on an open long.
    """
    import services.entry_sensor_loop as sensor

    calls: list[str] = []

    class _Orch:
        def process_entry_sensor(self, coin, price, sensor_metrics=None, quiet=True):
            calls.append(str(coin.get("symbol") or ""))
            return {
                "executed": True,
                "action": "BUY",
                "sources": ["entry_sensor_15m"],
            }

    def worker():
        with tenant_context(DEFAULT_TENANT, scope="paper"):
            with patch.object(sensor, "get_position", side_effect=RuntimeError("book unread")):
                with patch.object(sensor.watch_15m_state, "clear_watch"):
                    sensor._active_trigger(
                        _Orch(),
                        _ZEC,
                        {"symbol": _ZEC, "timeframe": _TF},
                        50.0,
                        {"vol": 1},
                    )
        return list(calls)

    out = _fresh(worker)
    assert "exc" not in out, out.get("exc")
    assert out["v"] == [], (
        "would-be double entry: process_entry_sensor ran while the book was "
        "unreadable and tenant context was already set, so execute_if_needed "
        f"would not skip the long. calls={out['v']}"
    )


def test_NR2_execute_fresh_thread_locked_lot_is_409():
    from services.dca_sniper.bot_http import execute_sniper_dca

    _seed_locked_default()
    out = _fresh(lambda: execute_sniper_dca(_dca_payload()))
    assert "exc" not in out, out.get("exc")
    body, status = out["v"]
    assert status == 409, body
    assert body.get("code") == "position_locked", body


def test_NR2_fund_sell_fresh_thread_locked_lot_is_409():
    from services.dca_sniper.bot_http import execute_fund_sell

    _seed_locked_default()
    out = _fresh(
        lambda: execute_fund_sell({"symbol": _ZEC, "timeframe": _TF, "price": 1600})
    )
    assert "exc" not in out, out.get("exc")
    body, status = out["v"]
    assert status == 409, body
    assert body.get("code") == "position_locked", body
    assert "position_locked" in str(body.get("message") or ""), body


def test_NR2_promote_fresh_thread_locked_lot_is_409():
    from services.dca_sniper.bot_http import promote_position

    _seed_locked_default()
    out = _fresh(lambda: promote_position({"symbol": _ZEC, "timeframe": _TF, "price": 1600}))
    assert "exc" not in out, out.get("exc")
    body, status = out["v"]
    assert status == 409, body
    assert body.get("code") == "position_locked", body


def test_NR2_local_client_execute_fresh_thread_locked_lot_is_409():
    from services.dca_sniper.local_client import LocalBotClient

    _seed_locked_default()
    out = _fresh(lambda: LocalBotClient().execute(_dca_payload()))
    assert "exc" not in out, out.get("exc")
    body = out["v"]
    assert body.get("code") == "position_locked", body
    assert body.get("code") != "position_lock_check_error"


def test_NR2_sniper_loop_fresh_thread_binds_allowlisted_tenant(monkeypatch):
    from services.dca_sniper.loop import DcaSniperLoop

    monkeypatch.setattr(
        "services.dca_sniper.loop.dca_sniper_enabled", lambda config=None: True
    )
    monkeypatch.setattr(DcaSniperLoop, "_interval", lambda self: 0.05)
    loop = DcaSniperLoop(config={"dca_sniper": {"enabled": True, "tenants": ["default"]}})
    seen: dict = {}

    def fake_cycle(config=None):
        from core.tenant_context import current_tenant_context

        ctx = current_tenant_context()
        seen["tid"] = None if ctx is None else ctx.tenant_id
        loop._stop.set()
        loop._wake.set()
        return {"ranked_top": [], "n_candidates": 0, "n_focus": 0, "actions": []}

    monkeypatch.setattr("services.dca_sniper.loop.run_cycle", fake_cycle)
    out = _fresh(loop._run, stop=loop._stop, wake=loop._wake)
    assert "exc" not in out, out.get("exc")
    assert seen.get("tid") == DEFAULT_TENANT, seen


def test_delayed_scope_guard_does_not_cross_write_default_book(ledger, monkeypatch):
    """Removing the _delayed default-scope guard writes the new RAM book into the old doc."""
    monkeypatch.setattr(P, "_FLUSH_DEBOUNCE_SEC", 60.0)
    with tenant_context(DEFAULT_TENANT, scope="paper"):
        P.flush_positions(scope="paper")
    timer = P._flush_timer
    assert timer is not None
    P.positions.clear()
    P.positions["LIVEONLY_USDT_1h"] = _lot(3.0, 3.0)
    P._default_ram_scope = "live"
    try:
        timer.function()
    finally:
        timer.cancel()
    paper = (ledger.docs.get((DEFAULT_TENANT, "paper"), {}) or {}).get("positions") or {}
    assert "LIVEONLY_USDT_1h" not in paper, sorted(paper)


def test_force_flush_leaves_a_foreign_debounce_timer(ledger, monkeypatch):
    """force=True must cancel only the timer for the book it writes."""
    monkeypatch.setattr(P, "_FLUSH_DEBOUNCE_SEC", 60.0)

    def _arm():
        with tenant_context("henry", scope="paper"):
            P.flush_positions(scope="paper")

    out = _fresh(_arm)
    assert "exc" not in out, out.get("exc")
    timer = P._flush_timer
    assert timer is not None
    try:
        with tenant_context(DEFAULT_TENANT, scope="paper"):
            P.flush_positions(scope="paper", force=True)
        assert P._flush_timer is timer, "force flush cancelled a foreign debounce"
        assert P._pending_flush_key == ("henry", "paper")
    finally:
        timer.cancel()
