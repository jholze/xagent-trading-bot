"""AC4-R1 (Rev1, Kay on #737): watchdog N is a per-tenant config key, never a code literal.
  observability.cycle_watchdog.auto_silent_cycles / fill_silent_cycles, read from the EFFECTIVE config of the tenant
  being counted (config.json -> profile -> tenant body). Start value in config.json:
      N = ceil(7200 s / update_interval) = ceil(7200 / 120) = 60   (update_interval at config.json:831)
Driven through note_tenant_cycle(tid) WITHOUT config_raw (the exact call of the aria_bot tenant-loop hook), the real
config layering (data_manager.load_config / get_config -> apply_effective_config; only the tenant body loader is
patched, as in tests/unit/test_issue_644_live_caps.py) and the real tenant ledger (mongo :27696).
"""
from __future__ import annotations

import copy
import math
from unittest.mock import patch

import pytest

from cs_helpers import repo_cfg
from test_cs_ac4_watchdog import add_order, kinds, wd

START = 60


def _cycle(m, tid):
    from cs_helpers import tenant

    with tenant(tid):
        return m.note_tenant_cycle(tid)


def _layered(henry_body, default_override=None):
    """Patch only the config SOURCES: default = config.json on disk (+ optional override), henry = tenant body."""
    import data_manager

    disk = repo_cfg()
    if default_override:
        disk["observability"]["cycle_watchdog"].update(default_override)
    stack = [
        patch.object(data_manager, "_config_cache", None),
        patch.object(data_manager, "_load_default_config_from_disk", lambda: copy.deepcopy(disk)),
        patch.object(data_manager, "_should_use_mongo_for_tenant_config", lambda *_a, **_k: True),
        patch.object(data_manager, "_load_tenant_config_body",
                     lambda tid, default_cfg: copy.deepcopy(henry_body) if tid == "henry" else None),
    ]
    return stack


class _Stack:
    def __init__(self, ps):
        self.ps = ps

    def __enter__(self):
        for p in self.ps:
            p.start()

    def __exit__(self, *exc):
        for p in reversed(self.ps):
            p.stop()


def _run(m, n_cycles, tids=("default", "henry")):
    """Arm (cycle 0), then n_cycles silent cycles. Returns {tid: {kind: first cycle that alarmed}}."""
    first = {t: {} for t in tids}
    for t in tids:
        add_order(t, source="auto", status="filled")
        assert _cycle(m, t) == []
    for i in range(1, n_cycles + 1):
        for t in tids:
            for k in kinds(_cycle(m, t)):
                first[t].setdefault(k, i)
    return first


class TestAC4R1PerTenantN:
    def test_start_value_in_config_is_two_hours_of_cycles(self):
        cfg = repo_cfg()
        assert cfg["update_interval"] == 120  # config.json:831
        cw = cfg["observability"]["cycle_watchdog"]
        assert cw["auto_silent_cycles"] == cw["fill_silent_cycles"] == math.ceil(7200 / cfg["update_interval"]) == START
        assert cw.get("enabled", True) is True

    def test_per_tenant_n_differs_from_start_value(self):
        """default: start value 60 from config.json; henry: tenant body auto 7 / fill 5. Each alarms at ITS N."""
        body = {"observability": {"cycle_watchdog": {"auto_silent_cycles": 7, "fill_silent_cycles": 5}}}
        with _Stack(_layered(body)), wd() as (m, r):
            got = _run(m, START)
        assert got["henry"] == {"no_fill": 5, "no_auto_order": 7}, got
        assert got["default"] == {"no_auto_order": START, "no_fill": START}, got

    def test_default_n7_from_config_henry_n3(self):
        """N different from the start value for BOTH tenants, and different between tenants."""
        body = {"observability": {"cycle_watchdog": {"auto_silent_cycles": 3, "fill_silent_cycles": 3}}}
        with _Stack(_layered(body, {"auto_silent_cycles": 7, "fill_silent_cycles": 7})), wd() as (m, r):
            got = _run(m, 8)
        assert got == {"default": {"no_auto_order": 7, "no_fill": 7}, "henry": {"no_auto_order": 3, "no_fill": 3}}, got

    def test_hook_call_uses_counted_tenant_config_not_active_context(self):
        """note_tenant_cycle('henry') reads henry's N even when called from the default context."""
        body = {"observability": {"cycle_watchdog": {"auto_silent_cycles": 2, "fill_silent_cycles": 2}}}
        with _Stack(_layered(body)), wd() as (m, r):
            add_order("henry", source="auto", status="filled")
            seen = [m.note_tenant_cycle("henry") for _ in range(3)]
        assert [kinds(s) for s in seen] == [[], [], ["no_auto_order", "no_fill"]], seen
