"""AC4 (cycle-silence spec, mandatory): per-tenant cycle-silence watchdog with TWO cases.
  no_auto_order  N consecutive meta cycles without a NEW ledger order with source == "auto" (ANY status, incl.
                 rejected/risk_rejected)          -> config observability.cycle_watchdog.auto_silent_cycles
  no_fill        N consecutive meta cycles without a NEW executed ledger row (status filled, ANY source)
                                                   -> config observability.cycle_watchdog.fill_silent_cycles
Alarm sink = Learning Loop inbox (Redis list <prefix>learning_loop:inbox) + ERROR log (concrete consumer UNCLEAR).
N start value UNCLEAR (derived from measured cycle duration, Kay decides) -> every test is parametrised over N.
Driven through services.cycle_silence_watchdog.note_tenant_cycle against the REAL tenant ledger (mongo :27696) and
through the aria_bot tenant-loop hook _run_one_tenant_cycle.
"""
from __future__ import annotations

import contextlib
import importlib
import inspect
import json
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from cs_helpers import QCFG, FakeRedis, tenant

NS = [pytest.param(1, id="N1"), pytest.param(3, id="N3"), pytest.param(50, id="N50")]
TENANTS = ("default", "henry")
_T0 = datetime(2026, 10, 26, 12, 0, 0)
_seq = [0]


def wcfg(n_auto, n_fill=None, **extra):
    c = {"auto_silent_cycles": n_auto, "fill_silent_cycles": n_auto if n_fill is None else n_fill}
    c.update(extra)
    return {"architecture": dict(QCFG["architecture"]), "observability": {"cycle_watchdog": c}}


def add_order(tid, *, source="auto", status="rejected", symbol="CS4X/USDT"):
    """Append one ledger row for tenant tid (real data_manager ledger, monotonically increasing created)."""
    from data_manager import load_orders, resolve_ledger_scope, save_orders

    _seq[0] += 1
    ts = (_T0 + timedelta(seconds=_seq[0])).isoformat()
    with tenant(tid):
        scope = resolve_ledger_scope()
        doc = load_orders(scope, tenant_id=tid) or {}
        doc.setdefault("orders", []).append({
            "id": f"cs4-{uuid.uuid4().hex[:10]}", "tenant_id": tid, "symbol": symbol, "signal": "SELL_STOP_PARTIAL",
            "source": source, "status": status, "timestamps": {"created": ts, **({"filled": ts} if status == "filled" else {})},
        })
        assert save_orders(doc, scope, tenant_id=tid)


@contextlib.contextmanager
def wd(redis=None):
    import services.cycle_silence_watchdog as m

    r = redis or FakeRedis()
    with patch.object(m, "_client", return_value=r):
        yield m, r


def inbox(r):
    return [json.loads(x) for x in r.lrange("cstest:learning_loop:inbox", 0, -1)]


def cycle(m, tid, cfg):
    with tenant(tid):
        return m.note_tenant_cycle(tid, config_raw=cfg)


def kinds(alarms, tid=None):
    return sorted(a["kind"] for a in alarms if a.get("type") == "cycle_silence" and (tid is None or a["tenant_id"] == tid))


class TestAC4Watchdog:
    @pytest.mark.parametrize("n", NS)
    @pytest.mark.parametrize("tid", TENANTS)
    def test_silent_tenant_alarms_both_cases_exactly_at_n(self, n, tid):
        cfg = wcfg(n, repeat_every_cycles=10_000)
        add_order(tid, source="auto", status="filled")  # some history; then silence
        with wd() as (m, r):
            assert cycle(m, tid, cfg) == []  # first observation arms, never alarms
            for i in range(1, n):
                assert cycle(m, tid, cfg) == [], i
            got = cycle(m, tid, cfg)
            assert kinds(got) == ["no_auto_order", "no_fill"], got
            assert all(a["tenant_id"] == tid and a["cycles"] == n and a["n"] == n for a in got)
            assert kinds(inbox(r)) == ["no_auto_order", "no_fill"]  # Learning Loop sink received both
            assert cycle(m, tid, cfg) == []  # no alarm storm on N+1

    @pytest.mark.parametrize("n", NS)
    @pytest.mark.parametrize("tid", TENANTS)
    def test_rejected_only_tenant_alarms_no_fill_not_no_auto(self, n, tid):
        """Steering 2 (c): auto orders are created every cycle but all are risk-rejected (the state since 23.10.):
        'no fill' fires, 'no auto order' does not."""
        cfg = wcfg(n, repeat_every_cycles=10_000)
        with wd() as (m, r):
            add_order(tid)
            cycle(m, tid, cfg)
            seen = []
            for _ in range(n):
                add_order(tid, source="auto", status="rejected")
                seen += cycle(m, tid, cfg)
            assert kinds(seen) == ["no_fill"], seen

    @pytest.mark.parametrize("n", NS)
    def test_tenant_isolation(self, n):
        """henry silent, default active (one auto fill per cycle): only henry alarms; default state untouched."""
        cfg = wcfg(n, repeat_every_cycles=10_000)
        with wd() as (m, r):
            for t in TENANTS:
                add_order(t, status="filled")
                cycle(m, t, cfg)
            seen = []
            for _ in range(n):
                add_order("default", source="auto", status="filled")
                seen += cycle(m, "default", cfg)
                seen += cycle(m, "henry", cfg)
            assert kinds(seen, "default") == [] and kinds(seen, "henry") == ["no_auto_order", "no_fill"], seen
            assert r.get("cstest:watchdog:cycle_silence:default:no_auto_order:count") == "0"

    @pytest.mark.parametrize("n", [pytest.param(3, id="N3")])
    def test_new_auto_order_resets_counter(self, n):
        cfg = wcfg(n, repeat_every_cycles=10_000)
        with wd() as (m, r):
            add_order("henry")
            cycle(m, "henry", cfg)
            for _ in range(n - 1):
                assert kinds(cycle(m, "henry", cfg)) == []
            add_order("henry", source="auto", status="rejected")
            assert "no_auto_order" not in kinds(cycle(m, "henry", cfg))  # reset by the new row
            for _ in range(n - 1):
                assert "no_auto_order" not in kinds(cycle(m, "henry", cfg))
            assert "no_auto_order" in kinds(cycle(m, "henry", cfg))

    def test_non_auto_orders_do_not_satisfy_no_auto_order(self):
        cfg = wcfg(2, 10_000, repeat_every_cycles=10_000)
        with wd() as (m, r):
            add_order("default")
            cycle(m, "default", cfg)
            seen = []
            for src in ("manual", "grid"):
                add_order("default", source=src, status="filled")
                seen += cycle(m, "default", cfg)
            assert kinds(seen) == ["no_auto_order"], seen

    def test_repeat_and_restart_persistence(self):
        cfg = wcfg(2, 10_000, repeat_every_cycles=3)
        r = FakeRedis()
        add_order("henry")
        with wd(r) as (m, _):
            cycle(m, "henry", cfg)
            got = [kinds(cycle(m, "henry", cfg)) for _ in range(2)]
        import services.cycle_silence_watchdog as mod

        importlib.reload(mod)  # process restart: state must come from Redis
        with wd(r) as (m, _):
            got += [kinds(cycle(m, "henry", cfg)) for _ in range(4)]
        assert got == [[], ["no_auto_order"], [], [], ["no_auto_order"], []], got

    @pytest.mark.parametrize("bad", [None, 0, -1, "3", True])
    def test_unconfigured_n_alarms_once_never_guesses(self, bad):
        cfg = wcfg(bad, 5)
        with wd() as (m, r):
            add_order("default")
            first = cycle(m, "default", cfg)
            assert [(a["type"], a["kind"]) for a in first] == [("watchdog_unconfigured", "no_auto_order")]
            for _ in range(10):
                assert [a for a in cycle(m, "default", cfg) if a["type"] == "watchdog_unconfigured"] == []

    def test_disabled_writes_nothing(self):
        cfg = wcfg(1, enabled=False)
        with wd() as (m, r):
            for _ in range(5):
                assert cycle(m, "henry", cfg) == []
            assert r.writes == 0 and inbox(r) == []


class TestAC4Hook:
    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("raises", [False, True])
    def test_tenant_cycle_calls_watchdog_in_same_tenant_context(self, tid, raises):
        import aria_bot
        from core.tenant_context import resolve_tenant_id

        seen = {}

        def fake_cycle(*a, **k):
            seen["cycle"] = resolve_tenant_id()
            if raises:
                raise RuntimeError("cycle boom")

        def fake_note(t=None, **k):
            seen["note"] = (t, resolve_tenant_id())

        with patch.object(aria_bot, "_run_tenant_price_cycle", side_effect=fake_cycle), \
                patch("services.cycle_silence_watchdog.note_tenant_cycle", side_effect=fake_note):
            ctx = pytest.raises(RuntimeError) if raises else contextlib.nullcontext()
            with ctx:
                aria_bot._run_one_tenant_cycle(tid, 0.0, False, analyzer=None, orchestrator=None,
                                               social_pipeline=None, sandbox=None, trend_engine=None,
                                               shared_signals=None)
        assert seen == {"cycle": tid, "note": (tid, tid)}, seen

    def test_watchdog_error_never_breaks_the_cycle(self):
        import aria_bot

        with patch.object(aria_bot, "_run_tenant_price_cycle", return_value=None), \
                patch("services.cycle_silence_watchdog.note_tenant_cycle", side_effect=RuntimeError("wd boom")):
            aria_bot._run_one_tenant_cycle("henry", 0.0, False)

    def test_price_loop_uses_the_hook_for_every_tenant(self):
        import aria_bot

        src = inspect.getsource(aria_bot.price_loop)
        assert "_run_one_tenant_cycle(" in src and "_run_tenant_price_cycle(" not in src
