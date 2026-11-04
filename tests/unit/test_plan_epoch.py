"""/plan restarts with the ledger epoch (reset 2026-11-04, epoch e20261104T095950Z)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from services import ledger_epoch_view as lev
from services.portfolio_plan import resolve_plan_start_date
from services.reporting.benchmark import benchmark_from_nav_rows, format_btc_benchmark_line

BERLIN = ZoneInfo("Europe/Berlin")
EPOCH = {
    "epoch_id": "e20261104T095950Z",
    "kind": "reset",
    "started_at": "2026-11-04T09:59:50+00:00",
    "initial_capital": 100000.0,
}
START = datetime(2026, 11, 4, 9, 59, 50, tzinfo=timezone.utc)


def _pt(day: str, nav: float, btc: float | None, as_of: str | None = None) -> dict:
    p = {"date": day, "nav": nav, "btc_close": btc, "initial_capital": 100000.0}
    if as_of:
        p["as_of"] = as_of
    return p


def _history() -> list[dict]:
    """Old epoch from 2026-07-28 (btc_close from 2026-09-07), plus the reset day."""
    pts = [_pt("2026-07-28", 100000.0, None, "2026-07-28T22:00:00+00:00")]
    d = date(2026, 9, 7)
    for i in range(58):
        day = d + timedelta(days=i)
        pts.append(_pt(day.isoformat(), 107600.0 - 120 * i, 60000.0 + 15 * i, f"{day}T21:00:00+00:00"))
    pts.append(_pt("2026-11-04", 100237.0, 70000.0, "2026-11-04T19:00:00+00:00"))
    return pts


class TestPickEpoch:
    def test_marker_in_trade_history(self):
        assert lev.pick_current_epoch({"ledger_epoch": EPOCH}, [])["epoch_id"] == EPOCH["epoch_id"]

    def test_falls_back_to_ledger_epochs_doc(self):
        assert lev.pick_current_epoch({}, [EPOCH])["epoch_id"] == EPOCH["epoch_id"]

    def test_newest_started_at_wins(self):
        old = dict(EPOCH, epoch_id="e1", started_at="2026-07-28T06:00:00Z")
        got = lev.pick_current_epoch({"ledger_epoch": old}, [EPOCH, {"started_at": "bad"}])
        assert got["epoch_id"] == EPOCH["epoch_id"]

    def test_none(self):
        assert lev.pick_current_epoch(None, None) == {}
        assert lev.epoch_start({}) is None

    def test_start_date_is_berlin_day(self):
        late = {"started_at": "2026-11-04T23:30:00+00:00"}
        assert lev.epoch_start_date(late, tz=BERLIN) == date(2026, 11, 5)
        assert lev.epoch_start_date(EPOCH, tz=BERLIN) == date(2026, 11, 4)


class TestSplit:
    def test_reset_day_point_written_after_start_is_current(self):
        cur, pre = lev.split_points(_history(), START)
        assert [p["date"] for p in cur] == ["2026-11-04"]
        assert len(pre) == 59 and pre[0]["date"] == "2026-07-28"

    def test_point_without_as_of_uses_day(self):
        assert lev.point_in_epoch({"date": "2026-11-05"}, START)
        assert not lev.point_in_epoch({"date": "2026-11-04"}, START)

    def test_point_without_as_of_compares_berlin_day(self):
        # 23:30 UTC on 2026-11-04 is 00:30 Berlin on 2026-11-05: the reset day
        # (Berlin) is 11-05, so the 11-05 point without as_of is not "after" it.
        late = datetime(2026, 11, 4, 23, 30, tzinfo=timezone.utc)
        assert not lev.point_in_epoch({"date": "2026-11-05"}, late, tz=BERLIN)
        assert lev.point_in_epoch({"date": "2026-11-06"}, late, tz=BERLIN)

    def test_unresolvable_tenant_returns_no_epoch(self, monkeypatch):
        import core.tenant_context as tc

        def boom(*_a, **_k):
            raise RuntimeError("no tenant context")

        monkeypatch.setattr(tc, "resolve_tenant_id", boom)
        assert lev.load_current_epoch() == {}

    def test_no_epoch_keeps_everything(self):
        cur, pre = lev.split_points(_history(), None)
        assert len(cur) == 60 and pre == []

    def test_trade_naive_berlin_timestamp(self):
        # 10:59:49 Berlin = 09:59:49 UTC, one second before the reset
        assert not lev.trade_in_epoch({"timestamp": "2026-11-04T10:59:49"}, START, tz=BERLIN)
        assert lev.trade_in_epoch({"timestamp": "2026-11-04T10:59:51"}, START, tz=BERLIN)
        assert not lev.trade_in_epoch({"timestamp": ""}, START)
        assert lev.trade_in_epoch({}, None)


class TestPlanStart:
    CFG = {"portfolio_plan": {"plan_start_date": "2026-07-28"}}

    def test_epoch_beats_older_configured_date(self):
        got = resolve_plan_start_date(config_raw=self.CFG, epoch_start=date(2026, 11, 4))
        assert got == date(2026, 11, 4)

    def test_later_configured_date_still_wins(self):
        cfg = {"portfolio_plan": {"plan_start_date": "2026-11-10"}}
        assert resolve_plan_start_date(config_raw=cfg, epoch_start=date(2026, 11, 4)) == date(2026, 11, 10)

    def test_without_epoch_unchanged(self):
        assert resolve_plan_start_date(config_raw=self.CFG) == date(2026, 7, 28)

    def test_repo_config_has_no_hardcoded_start(self):
        import json
        from pathlib import Path

        raw = json.loads((Path(__file__).resolve().parents[2] / "config.json").read_text())
        assert raw["portfolio_plan"].get("plan_start_date") in (None, "")


class TestBenchmarkStartNav:
    def test_start_nav_override(self):
        rows = [_pt("2026-11-04", 100237.0, 70000.0)]
        b = benchmark_from_nav_rows(rows, start_nav=100000.0)
        assert b["start_nav"] == 100000.0
        assert round(b["bot_return_pct"], 3) == 0.237
        assert format_btc_benchmark_line(rows, start_nav=100000.0).startswith("BTC HODL seit 2026-11-04")
        assert format_btc_benchmark_line(rows) == ""


class TestPlanPayload:
    """Before: Plan-Start 2026-07-28, Tag 99, HODL seit 2026-09-07, 7d KPIs with old trades."""

    @pytest.fixture
    def payload(self, monkeypatch):
        import core.config
        import core.portfolio_baseline
        import core.time_utils
        import notifications.terminal_dashboard as td
        import services.reporting.attribution as attr
        from notifications.telegram_commands import plan_commands as pc

        cfg = SimpleNamespace(
            raw={"portfolio_plan": {"daily_return_pct": 0.5, "compound": True, "horizon_days": 365,
                                    "plan_start_date": "2026-07-28"}},
            trading_mode="demo",
        )
        monkeypatch.setattr(core.config, "get_bot_config", lambda: cfg)
        monkeypatch.setattr(core.portfolio_baseline, "initial_capital", lambda: 100000.0)
        monkeypatch.setattr(td, "_portfolio_snapshot", lambda: {"total_value": 100237.0, "initial_capital": 100000.0})
        monkeypatch.setattr(pc, "capture_current_nav_snapshot", lambda: None)
        monkeypatch.setattr(pc, "load_nav_history", _history)
        monkeypatch.setattr(pc, "render_plan_chart_for_current", lambda **kw: None)
        monkeypatch.setattr(core.time_utils, "now_display", lambda: datetime(2026, 11, 4, 20, 0, tzinfo=BERLIN))
        monkeypatch.setattr(core.time_utils, "display_tz", lambda: BERLIN)
        trades = [
            {"type": "SELL", "pnl": -50.0, "timestamp": "2026-11-01T12:00:00"},
            {"type": "SELL", "pnl": 80.0, "timestamp": "2026-11-04T09:00:00"},
        ]
        monkeypatch.setattr(attr, "list_closed_trades", lambda days=7: list(trades))
        import services.reporting.metrics as met

        monkeypatch.setattr(met, "list_closed_trades", lambda days=7: list(trades))

        def run(epoch):
            monkeypatch.setattr(lev, "load_current_epoch", lambda *a, **k: dict(epoch))
            return pc.build_plan_payload()

        return run

    def test_before_without_epoch(self, payload):
        html, _chart, gap = payload({})
        assert "Plan-Start: <code>2026-07-28</code>" in html
        assert gap.day_index == 99
        assert "Kennzahlen" in html or "Hit" in html

    def test_after_with_epoch(self, payload):
        html, _chart, gap = payload(EPOCH)
        assert "Plan-Start: <code>2026-11-04</code>" in html
        assert gap.day_index == 0 and gap.days_remaining == 365
        assert round(gap.nav_plan) == 100000
        assert round(gap.delta_usd) == 237
        assert "e20261104T095950Z" in html and "2026-11-04 10:59" in html
        assert "vorher 2026-07-28…2026-11-03" in html
        # both closed trades are before the reset → no old KPIs leak in
        assert "noch keine geschlossenen Trades seit Epoch-Start" in html
        assert "BTC HODL seit 2026-11-04" in html
        assert "2026-09-07" not in html
