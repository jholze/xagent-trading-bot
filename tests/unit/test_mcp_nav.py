"""xagent_nav: read-only NAV / peak / drawdown pack, tenant scoping, no writes."""

from __future__ import annotations

import asyncio

import pytest

from services.mcp.authz import Actor
from services.mcp.nav import curve_max_drawdown, load_nav_points_readonly, nav_pack
from services.mcp.tools import tool_nav

OWNER = Actor("jens", "owner", ("*",), ("read", "trade", "lock", "config_read", "kill"))
HENRY = Actor("henry-op", "operator", ("henry",), ("read", "trade", "lock"))
NO_READ = Actor("x", "operator", ("henry",), ("trade",))

_ENABLED = {"mcp": {"enabled": True, "allow_writes": False}}


def _pt(day, nav, *, scope="demo", cash=10_000.0, as_of=None, init=100_000.0, **extra):
    return {
        "tenant_id": "henry",
        "ledger_scope": scope,
        "date": day,
        "nav": nav,
        "cash": cash,
        "positions_mtm": nav - cash,
        "initial_capital": init,
        "as_of": as_of or f"{day}T22:00:00+00:00",
        **extra,
    }


POINTS = [
    _pt("2026-10-30", 100_000.0),
    _pt("2026-10-31", 104_000.0),
    _pt("2026-11-01", 90_000.0),
    _pt("2026-11-02", 95_000.0),
    _pt("2026-11-03", 92_000.0, cash=11_775.0, as_of="2026-11-03T08:59:00+00:00"),
]


def _peaks(live=None, demo=None):
    table = {
        "live": {"peak_equity": live, "peak_equity_at": "2026-10-20T10:00:00+00:00"},
        "demo": {"peak_equity": demo, "peak_equity_at": None},
    }
    return lambda tenant, scope: table[scope]


LOTS = [
    {"symbol": "A/USDT", "lock": None},
    {"symbol": "B/USDT", "lock": {"enabled": True, "modes": ["no_auto_sell"]}},
    {"symbol": "C/USDT", "lock": {"enabled": False}},
    {"symbol": "D/USDT", "lock": {"enabled": True, "until": "2000-01-01T00:00:00+00:00"}},
]


def _pack(**kw):
    kw.setdefault("points_fn", lambda t: list(POINTS))
    kw.setdefault("peak_fn", _peaks())
    kw.setdefault("lots_fn", lambda t: list(LOTS))
    return nav_pack("henry", **kw)


def test_curve_max_drawdown_seeded_and_unseeded():
    assert curve_max_drawdown([100, 110, 99, 120]) == (pytest.approx(10.0), 120)
    dd, peak = curve_max_drawdown([90, 95], seed_peak=100)
    assert dd == pytest.approx(10.0) and peak == 100


def test_nav_from_newest_point_and_drawdown_from_curve():
    out = _pack()
    assert out["ok"] is True
    assert out["nav"] == 92_000.0 and out["cash"] == 11_775.0
    assert out["positions_mtm"] == 80_225.0
    assert out["as_of"] == "2026-11-03T08:59:00+00:00"
    assert out["initial_capital"] == 100_000.0
    assert out["return_pct"] == -8.0
    assert out["peak_nav"] == 104_000.0 and out["peak_source"] == "nav_curve"
    assert out["current_drawdown_pct"] == pytest.approx(11.54, abs=0.01)
    # 104k -> 90k
    assert out["max_drawdown_curve_pct"] == pytest.approx(13.46, abs=0.01)
    assert out["max_drawdown_pct"] == pytest.approx(13.46, abs=0.01)
    assert out["nav_points"] == 5 and out["first_date"] == "2026-10-30"
    assert "curve" not in out


def test_live_peak_equity_is_the_high_water_mark():
    out = _pack(peak_fn=_peaks(live=118_949.0, demo=101_000.0))
    assert out["peak_nav"] == 118_949.0
    assert out["peak_source"] == "trade_history_live"
    assert out["peak_equity_by_scope"]["live"]["peak_equity"] == 118_949.0
    assert out["peak_candidates"]["trade_history_demo"] == 101_000.0
    cur = (118_949.0 - 92_000.0) / 118_949.0 * 100
    assert out["current_drawdown_pct"] == pytest.approx(cur, abs=0.01)
    # current drawdown vs the live peak is deeper than the daily curve drawdown
    assert out["max_drawdown_pct"] == pytest.approx(cur, abs=0.01)


def test_open_and_locked_lot_counts():
    out = _pack()
    assert out["open_positions"] == 4
    # B active; C disabled; D expired
    assert out["locked_lots"] == 1


def test_scope_of_newest_point_wins_and_other_scope_ignored():
    pts = list(POINTS) + [_pt("2026-11-03", 50_000.0, scope="paper", as_of="2026-11-01T00:00:00+00:00")]
    out = _pack(points_fn=lambda t: pts)
    assert out["ledger_scope"] == "demo" and out["nav"] == 92_000.0 and out["nav_points"] == 5


def test_unknown_fields_never_leak():
    pts = [dict(p, mongo_uri="mongodb://u:pw@h", token="t") for p in POINTS]
    out = _pack(points_fn=lambda t: pts, curve=True)
    text = repr(out)
    assert "pw@" not in text and "token" not in text and "mongo_uri" not in text


def test_curve_range_with_running_peak_from_before_range():
    out = _pack(curve=True, curve_from="2026-11-01", curve_to="2026-11-02")
    assert [r["date"] for r in out["curve"]] == ["2026-11-01", "2026-11-02"]
    # peak 104k reached on 31.10. is carried into the range
    assert out["curve"][0]["drawdown_pct"] == pytest.approx(13.46, abs=0.01)
    assert out["curve_truncated"] is False


def test_bad_date_rejected():
    out = _pack(curve=True, curve_from="03.11.2026")
    assert out == {"ok": False, "error": "bad_date", "hint": "use YYYY-MM-DD"}


def test_source_failures_degrade_with_errors():
    def boom(*a, **k):
        raise RuntimeError("down")

    out = _pack(points_fn=boom, peak_fn=boom, lots_fn=boom)
    assert out["ok"] is True and out["nav"] is None and out["peak_nav"] is None
    assert set(out["errors"]) >= {"nav_history_unavailable", "peak_demo_unavailable", "peak_live_unavailable", "lots_unavailable", "no_nav_points"}


def test_points_loader_only_reads():
    calls = []

    class Coll:
        def __getattr__(self, name):
            calls.append(name)
            if name == "find":
                return lambda flt, proj: iter([dict(POINTS[0])])
            raise AssertionError(f"write or unexpected call: {name}")

    class Db:
        def __getitem__(self, name):
            assert name == "portfolio_nav_daily"
            return Coll()

    rows = load_nav_points_readonly("henry", db=Db())
    assert calls == ["find"] and rows[0]["nav"] == 100_000.0


def test_peak_loader_only_reads_trade_history(monkeypatch):
    import data_manager
    from services.mcp import nav as nav_mod

    seen = []

    class Store:
        def load_trade_history(self, scope, tenant_id=None):
            seen.append((scope, tenant_id))
            return {
                "peak_equity": 108_500.0,
                "peak_equity_at": "x",
                "trades": [1, 2],
                "ledger_epoch": {"epoch_id": "e1", "started_at": "s", "backup_dir": "/secret/path"},
            }

        def __getattr__(self, name):
            raise AssertionError(f"unexpected store call: {name}")

    monkeypatch.setattr(data_manager, "_mongo_ledger_store", lambda cfg=None: Store())
    assert nav_mod.load_peak_equity_readonly("henry", "live") == {
        "peak_equity": 108_500.0,
        "peak_equity_at": "x",
        "ledger_epoch": {"epoch_id": "e1", "started_at": "s"},
    }
    assert seen == [("live", "henry")]


def test_tool_scoping_operator_forced_to_own_tenant():
    seen = []

    def fake(tenant_id, **kw):
        seen.append(tenant_id)
        return {"ok": True}

    out = tool_nav(HENRY, tenant="default", nav_fn=fake)
    assert out["tenant_id"] == "henry" and seen == ["henry"]


def test_tool_requires_read_cap_and_actor():
    called = []
    fake = lambda *a, **k: called.append(1) or {"ok": True}  # noqa: E731
    assert tool_nav(NO_READ, tenant="henry", nav_fn=fake)["error"] == "forbidden"
    assert tool_nav(None, tenant="henry", nav_fn=fake)["error"] == "unauthorized"
    assert called == []


def test_tool_wraps_exceptions():
    def boom(*a, **k):
        raise RuntimeError("secret detail")

    out = tool_nav(OWNER, tenant="ctexp", nav_fn=boom)
    assert out == {"ok": False, "error": "nav_failed", "tenant_id": "ctexp"}


def test_sidecar_registers_and_routes_xagent_nav(monkeypatch):
    monkeypatch.setenv("MCP_OWNER_TOKEN", "owner-secret")
    monkeypatch.delenv("MCP_ACTORS_JSON", raising=False)
    from services.mcp import nav as nav_mod
    from services.mcp.sidecar.app import build_mcp, invoke_tool

    names = [t.name for t in asyncio.run(build_mcp().list_tools())]
    assert "xagent_nav" in names

    monkeypatch.setattr(nav_mod, "nav_pack", lambda tenant_id, **kw: {"ok": True, "kw": kw})
    out = invoke_tool(
        "xagent_nav",
        authorization="Bearer owner-secret",
        config_raw=_ENABLED,
        tenant="ctexp",
        curve=True,
        curve_from="2026-11-01",
    )
    assert out["ok"] is True and out["tenant_id"] == "ctexp"
    assert out["kw"] == {"curve": True, "curve_from": "2026-11-01", "curve_to": None}
    assert invoke_tool("xagent_nav", authorization="", config_raw=_ENABLED)["error"] == "unauthorized"


def test_pick_scope_ignores_null_as_of():
    from services.mcp.nav import _pick_scope

    pts = [
        {"as_of": None, "date": "2026-11-03", "ledger_scope": "live"},
        {"as_of": "2026-11-02T10:00:00+00:00", "date": "2026-11-02", "ledger_scope": "demo"},
    ]
    assert _pick_scope(pts) == "demo"


def test_lot_locked_fallback_skips_expiring_lock(monkeypatch):
    import builtins

    from services.mcp import nav as navmod

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "strategies.position_lock":
            raise ImportError("x")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert navmod._lot_locked({"lock": {"enabled": True}}) is True
    assert navmod._lot_locked({"lock": {"enabled": True, "until": "2020-01-01T00:00:00Z"}}) is False
    assert navmod._lot_locked({"lock": {"enabled": False}}) is False


# --- ledger epoch (reset 2026-11-04, scripts/ledger_epoch.py) -----------------

EPOCH = {"epoch_id": "e20261104T095950Z", "started_at": "2026-11-04T09:59:50.947632+00:00"}

# Shape of henry on 2026-11-04: old curve peaked at 109,989.71, reset to 100k.
EPOCH_POINTS = [
    _pt("2026-10-20", 109_989.71),
    _pt("2026-11-02", 83_877.89),
    _pt("2026-11-03", 84_264.56),
    _pt("2026-11-04", 99_999.16, cash=96_935.12, as_of="2026-11-04T18:18:44+00:00"),
]


def _epoch_peaks(live=100_075.37, live_at="2026-11-04T15:19:11+00:00", demo=100_000.0, epoch=EPOCH):
    table = {
        "live": {"peak_equity": live, "peak_equity_at": live_at, "ledger_epoch": dict(epoch)},
        "demo": {"peak_equity": demo, "peak_equity_at": EPOCH["started_at"], "ledger_epoch": dict(epoch)},
    }
    return lambda tenant, scope: table[scope]


def test_peak_and_drawdown_only_from_current_epoch():
    out = _pack(points_fn=lambda t: list(EPOCH_POINTS), peak_fn=_epoch_peaks())
    assert out["ledger_epoch"] == EPOCH
    assert out["nav"] == 99_999.16
    assert out["peak_nav"] == 100_075.37 and out["peak_source"] == "trade_history_live"
    assert out["current_drawdown_pct"] == pytest.approx(0.08, abs=0.01)
    assert out["max_drawdown_pct"] < 1.0
    assert out["nav_points"] == 1 and out["first_date"] == "2026-11-04"
    assert out["peak_candidates"]["nav_curve"] == 100_000.0


def test_pre_epoch_values_kept_as_history():
    out = _pack(points_fn=lambda t: list(EPOCH_POINTS), peak_fn=_epoch_peaks())
    pre = out["pre_epoch"]
    assert pre["peak_nav"] == 109_989.71
    assert pre["max_drawdown_pct"] == pytest.approx(23.74, abs=0.01)
    assert pre["nav_points"] == 3 and pre["first_date"] == "2026-10-20" and pre["last_date"] == "2026-11-03"
    assert pre["last_nav"] == 84_264.56
    assert pre["trade_history_peaks_before_epoch"] == {}


def test_trade_history_peak_set_before_epoch_is_history_not_current():
    # A HWM whose timestamp predates the epoch (e.g. a scope the reset missed) must not count.
    out = _pack(
        points_fn=lambda t: list(EPOCH_POINTS),
        peak_fn=_epoch_peaks(live=118_949.22, live_at="2026-10-15T10:00:00+00:00"),
    )
    assert out["peak_nav"] == 100_000.0
    assert out["peak_candidates"]["trade_history_live"] is None
    assert out["pre_epoch"]["trade_history_peaks_before_epoch"] == {"live": 118_949.22}


def test_epoch_from_ledger_epochs_collection_when_marker_missing():
    out = _pack(
        points_fn=lambda t: list(EPOCH_POINTS),
        peak_fn=_epoch_peaks(epoch={}),
        epoch_fn=lambda t: dict(EPOCH),
    )
    assert out["ledger_epoch"]["epoch_id"] == "e20261104T095950Z"
    assert out["peak_nav"] == 100_075.37


def test_without_epoch_behaviour_unchanged():
    out = _pack(points_fn=lambda t: list(EPOCH_POINTS), peak_fn=_peaks(live=100_075.37))
    assert out["ledger_epoch"] is None and "pre_epoch" not in out
    assert out["peak_nav"] == 109_989.71


def test_reset_day_point_written_before_reset_is_pre_epoch():
    pts = EPOCH_POINTS[:3] + [_pt("2026-11-04", 84_000.0, as_of="2026-11-04T09:00:00+00:00")]
    out = _pack(points_fn=lambda t: pts, peak_fn=_epoch_peaks(live=100_000.0, live_at=EPOCH["started_at"]))
    assert out["pre_epoch"]["nav_points"] == 4
    assert out["peak_nav"] == 100_000.0 and out["nav_points"] == 0


def test_curve_running_peak_restarts_at_epoch():
    out = _pack(points_fn=lambda t: list(EPOCH_POINTS), peak_fn=_epoch_peaks(), curve=True, curve_from="2026-11-03")
    rows = {r["date"]: r for r in out["curve"]}
    assert rows["2026-11-03"]["epoch"] == "pre" and rows["2026-11-03"]["drawdown_pct"] == pytest.approx(23.39, abs=0.01)
    assert rows["2026-11-04"]["epoch"] == "current" and rows["2026-11-04"]["drawdown_pct"] == pytest.approx(0.0, abs=0.01)


def test_epoch_loader_only_reads_and_picks_newest():
    calls = []

    class Coll:
        def find(self, flt, proj):
            calls.append((flt, proj))
            return [
                {"epoch_id": "e1", "started_at": "2026-10-01T00:00:00+00:00"},
                {"epoch_id": "e2", "started_at": "2026-11-04T09:59:50+00:00"},
            ]

        def __getattr__(self, name):
            raise AssertionError(f"unexpected call {name}")

    from services.mcp.nav import load_epoch_readonly

    got = load_epoch_readonly("henry", db={"ledger_epochs": Coll()})
    assert got == {"epoch_id": "e2", "started_at": "2026-11-04T09:59:50+00:00"}
    assert calls[0][0] == {"tenant_id": "henry"}
