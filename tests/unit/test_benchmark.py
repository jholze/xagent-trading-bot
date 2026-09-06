"""#307 slice 2: BTC HODL benchmark math and /plan line."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from services.reporting.benchmark import (
    benchmark_from_nav_rows,
    format_btc_benchmark_line,
    hodl_nav_by_plan_day,
)


def _row(day: str, nav: float, btc_close=None) -> dict:
    return {"date": day, "nav": nav, "btc_close": btc_close}


class TestBenchmarkMath:
    def test_skips_rows_without_btc_close(self):
        rows = [
            _row("2026-07-01", 100_000, 50_000),
            _row("2026-07-02", 110_000, None),
            _row("2026-07-03", 105_000, 52_500),
            _row("2026-07-04", 108_000),  # missing key
            {"date": "2026-07-05", "nav": 109_000, "btc_close": 0},
        ]
        bench = benchmark_from_nav_rows(rows)
        assert bench is not None
        assert bench["n"] == 2
        assert bench["start_date"] == "2026-07-01"
        assert bench["start_nav"] == 100_000
        assert bench["start_btc"] == 50_000
        dates = [p["date"] for p in bench["points"]]
        assert dates == ["2026-07-01", "2026-07-03"]

    def test_alpha_positive_when_bot_beats_btc(self):
        rows = [
            _row("2026-07-01", 100_000, 50_000),
            _row("2026-07-11", 110_000, 52_500),  # bot +10%, btc +5%
        ]
        bench = benchmark_from_nav_rows(rows)
        assert abs(bench["bot_return_pct"] - 10.0) < 1e-9
        assert abs(bench["btc_return_pct"] - 5.0) < 1e-9
        assert abs(bench["alpha_pp"] - 5.0) < 1e-9
        assert abs(bench["btc_hodl_nav"] - 105_000) < 1e-9
        assert abs(bench["points"][-1]["btc_hodl_nav"] - 105_000) < 1e-9

    def test_alpha_negative_when_btc_beats_bot(self):
        rows = [
            _row("2026-07-01", 100_000, 40_000),
            _row("2026-07-11", 102_000, 48_000),  # bot +2%, btc +20%
        ]
        bench = benchmark_from_nav_rows(rows)
        assert abs(bench["bot_return_pct"] - 2.0) < 1e-9
        assert abs(bench["btc_return_pct"] - 20.0) < 1e-9
        assert abs(bench["alpha_pp"] - (-18.0)) < 1e-9

    def test_hodl_uses_first_btc_row_nav_as_start_capital(self):
        rows = [
            _row("2026-07-01", 80_000, None),
            _row("2026-07-02", 90_000, 45_000),
            _row("2026-07-04", 99_000, 49_500),  # btc +10% → hodl 99_000
        ]
        bench = benchmark_from_nav_rows(rows)
        assert bench["start_nav"] == 90_000
        assert bench["start_date"] == "2026-07-02"
        assert abs(bench["points"][-1]["btc_hodl_nav"] - 99_000) < 1e-9
        assert abs(bench["bot_return_pct"] - 10.0) < 1e-9
        assert abs(bench["alpha_pp"] - 0.0) < 1e-9

    def test_empty_and_single_row(self):
        assert benchmark_from_nav_rows([]) is None
        assert benchmark_from_nav_rows([_row("2026-07-01", 100_000, None)]) is None
        one = benchmark_from_nav_rows([_row("2026-07-01", 100_000, 50_000)])
        assert one is not None
        assert one["n"] == 1
        assert one["alpha_pp"] == 0.0


class TestBenchmarkLine:
    def test_empty_without_two_btc_rows(self):
        assert format_btc_benchmark_line([]) == ""
        assert format_btc_benchmark_line([_row("2026-07-01", 100_000, 50_000)]) == ""
        assert format_btc_benchmark_line([_row("2026-07-01", 100_000, None)]) == ""

    def test_line_matches_ticket_shape(self):
        from notifications.telegram_commands.menu_i18n import set_user_language
        from notifications.telegram_i18n import reload_messages

        reload_messages()
        set_user_language("de")
        rows = [
            _row("2026-07-01", 100_000, 50_000),
            _row("2026-07-11", 110_000, 52_500),
        ]
        line = format_btc_benchmark_line(rows)
        assert "BTC HODL seit 2026-07-01" in line
        assert "+5.0%" in line  # btc
        assert "+10.0%" in line  # bot
        assert "+5.0 pp" in line
        assert "Bot:" in line
        assert "Alpha:" in line


class TestHodlByPlanDay:
    def test_maps_day_index_and_requires_two_for_chart(self):
        rows = [
            _row("2026-07-01", 100_000, 50_000),
            _row("2026-07-02", 101_000, None),
            _row("2026-07-03", 102_000, 51_000),
        ]
        mapped = hodl_nav_by_plan_day(rows, date(2026, 7, 1))
        assert set(mapped) == {0, 2}
        assert abs(mapped[0] - 100_000) < 1e-9
        assert abs(mapped[2] - 100_000 * (51_000 / 50_000)) < 1e-9


class TestPlanChartBtcLine:
    def test_render_with_btc_series(self):
        from notifications.plan_chart import render_plan_vs_actual_png

        path = render_plan_vs_actual_png(
            start_capital=100_000,
            plan_start=date(2026, 1, 1),
            actual_by_day={0: 100_000, 5: 101_000, 10: 99_500},
            today_day_index=10,
            horizon_days=365,
            btc_by_day={0: 100_000, 5: 102_000, 10: 103_000},
        )
        if path is None:
            return
        try:
            assert Path(path).is_file()
            assert Path(path).stat().st_size > 1000
        finally:
            Path(path).unlink(missing_ok=True)
