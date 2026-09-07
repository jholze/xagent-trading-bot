"""Slice 1 of #307: fill quality, attribution, live metrics, commands."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

from hermes.metrics import compute_trade_quality, max_drawdown_pct
from notifications.telegram_commands.menu_i18n import set_user_language
from notifications.telegram_i18n import reload_messages
from services.position_metrics import format_exit_plan_line
from services.reporting.attribution import attribution_summary, order_to_closed_trade
from services.reporting.fills import fill_quality_summary, percentile, slippage_bps
from services.reporting.metrics import metrics_from_closed_trades


def _ts(hours_ago: float = 1.0) -> str:
    return (datetime.now() - timedelta(hours=hours_ago)).isoformat()


def _order(
    *,
    side: str = "buy",
    req: float = 100.0,
    exe: float = 100.0,
    fee: float = 0.0,
    venue: str | dict = "gate",
    status: str = "filled",
    source: str = "auto",
    exit_source: str = "",
    pnl: float | None = None,
    hours_ago: float = 1.0,
    oid: str = "o1",
) -> dict:
    v = {"exchange": venue} if isinstance(venue, str) else venue
    rec = {
        "id": oid,
        "status": status,
        "side": side,
        "symbol": "AAA/USDT",
        "source": source,
        "exit_source": exit_source or None,
        "request": {"price": req, "amount": 1, "usdt": req},
        "execution": {
            "price": exe,
            "amount": 1,
            "usdt": exe,
            "fee": fee,
            "venue": v,
        },
        "pnl": pnl,
        "timestamps": {"created": _ts(hours_ago), "filled": _ts(hours_ago)},
    }
    return rec


class TestSlippageBps:
    def test_buy_exec_above_request_is_positive(self):
        bps = slippage_bps(_order(side="buy", req=100.0, exe=101.0))
        assert bps == 100.0

    def test_sell_exec_below_request_is_positive(self):
        bps = slippage_bps(_order(side="sell", req=100.0, exe=99.0))
        assert bps == 100.0

    def test_sell_exec_above_request_is_negative(self):
        bps = slippage_bps(_order(side="sell", req=100.0, exe=101.0))
        assert bps == -100.0

    def test_cover_follows_buy_sign(self):
        bps = slippage_bps(_order(side="cover", req=50.0, exe=51.0))
        assert bps == 200.0

    def test_short_follows_sell_sign(self):
        bps = slippage_bps(_order(side="short", req=50.0, exe=49.0))
        assert bps == 200.0

    def test_missing_prices_return_none(self):
        assert slippage_bps({"side": "buy", "request": {}, "execution": {}}) is None
        assert slippage_bps(_order(req=0, exe=10)) is None


class TestFillQualitySummary:
    def test_median_and_p90_by_side_and_venue(self):
        buys = [
            _order(side="buy", req=100, exe=100.1, oid=f"b{i}", venue="gate")
            for i in range(5)
        ]
        # 10, 20, … 100 bps worse on sells at venue thin
        sells = [
            _order(
                side="sell",
                req=100,
                exe=100 - i,
                oid=f"s{i}",
                venue="thin",
                pnl=10 - i,
                fee=0.5,
            )
            for i in range(1, 11)
        ]
        summary = fill_quality_summary(buys + sells, days=7)
        assert summary["n_fills"] == 15
        assert summary["by_side"]["buy"]["median_bps"] == 10.0
        sell_med = summary["by_side"]["sell"]["median_bps"]
        sell_p90 = summary["by_side"]["sell"]["p90_bps"]
        xs = [float(i) * 100.0 for i in range(1, 11)]  # 100..1000 bps
        assert sell_med == percentile(xs, 50)
        assert sell_p90 == percentile(xs, 90)
        assert "gate" in summary["by_venue"]
        assert "thin" in summary["by_venue"]
        assert summary["fee_drag_pct"] is not None
        # 10 sells × $0.5 fee; gross pnl = sum(10-i for i=1..10) = 45
        assert abs(summary["total_fees"] - 5.0) < 1e-9
        assert abs(summary["gross_realized_pnl"] - 45.0) < 1e-9
        assert summary["fee_drag_pct"] == round(5.0 / 45.0 * 100, 4)

    def test_empty_orders(self):
        summary = fill_quality_summary([], days=7)
        assert summary["n_fills"] == 0
        assert summary["by_side"] == {}
        assert summary["fee_drag_pct"] is None

    def test_old_orders_excluded(self):
        old = _order(oid="old", hours_ago=24 * 20, exe=110)
        fresh = _order(oid="new", hours_ago=1, exe=101)
        summary = fill_quality_summary([old, fresh], days=7)
        assert summary["n_fills"] == 1


class TestAttribution:
    def test_groups_source_and_exit_source_win_rate(self):
        trades = [
            order_to_closed_trade(_order(
                side="sell", source="grid", exit_source="tail_idle", pnl=10, oid="a",
            )),
            order_to_closed_trade(_order(
                side="sell", source="grid", exit_source="bb_upper", pnl=6, oid="b",
            )),
            order_to_closed_trade(_order(
                side="sell", source="auto", exit_source="stop_loss", pnl=-4, oid="c",
            )),
            order_to_closed_trade(_order(
                side="sell", source="auto", exit_source="stop_loss", pnl=-2, oid="d",
            )),
        ]
        summary = attribution_summary(trades, days=7)
        assert summary["n_trades"] == 4
        assert summary["empty"] is False
        by_src = {r["name"]: r for r in summary["by_source"]}
        assert by_src["grid"]["n"] == 2
        assert by_src["grid"]["pnl"] == 16
        assert by_src["grid"]["win_rate"] == 100.0
        assert by_src["auto"]["n"] == 2
        assert by_src["auto"]["win_rate"] == 0.0
        by_ex = {r["name"]: r for r in summary["by_exit_source"]}
        assert by_ex["tail_idle"]["pnl"] == 10
        assert by_ex["stop_loss"]["n"] == 2
        # daily_auswertung.pnl_by_source is reused (SELL markdown table)
        assert "grid" in summary["source_markdown"]
        assert "auto" in summary["source_markdown"]

    def test_empty_trades(self):
        summary = attribution_summary([], days=7)
        assert summary["empty"] is True
        assert summary["n_trades"] == 0


class TestLiveMetrics:
    def test_matches_hermes_on_same_trades(self):
        trades = [
            {"type": "SELL", "pnl": 10, "usdt_received": 110},
            {"type": "SELL", "pnl": 5, "usdt_received": 105},
            {"type": "SELL", "pnl": -4, "usdt_received": 96},
            {"type": "BUY", "pnl": 0, "usdt_received": 0},
        ]
        start = 10_000.0
        got = metrics_from_closed_trades(trades, days=7, start_equity=start)
        tq = compute_trade_quality(trades)
        sells = [t for t in trades if t["type"] == "SELL"]
        assert got["n_trades"] == 3
        assert got["win_count"] == tq["win_count"]
        assert got["loss_count"] == tq["loss_count"]
        assert got["expectancy"] == tq["trade_quality"]
        assert got["hit_rate"] == round(tq["win_count"] / 3, 6)
        assert got["avg_win"] == tq["avg_win"]
        assert got["avg_loss"] == tq["avg_loss"]
        pnls = [t["pnl"] for t in sells]
        eq = [start]
        running = start
        for p in pnls:
            running += p
            eq.append(running)
        assert got["max_drawdown_pct"] == max_drawdown_pct(eq)
        # PF = 15 / 4
        assert abs(got["profit_factor"] - 15.0 / 4.0) < 1e-6

    def test_empty(self):
        got = metrics_from_closed_trades([], days=7)
        assert got["empty"] is True
        assert got["n_trades"] == 0
        assert got["hit_rate"] == 0.0


class TestExitPlanLine:
    def test_full_line(self):
        now = datetime(2026, 6, 10, 12, 0, 0)
        first = datetime(2026, 6, 7, 8, 0, 0).isoformat()
        p = {
            "symbol": "AAA/USDT",
            "average_entry": 1.0,
            "recent_high": 1.14,
            "exit_ladder_step": 2,
            "stop_loss_pct": 8,
            "first_buy_at": first,
            "strategy_tier": "volatile",
        }
        params = {
            "stop_loss_pct": 8,
            "exit_ladder": {"enabled": True, "tiers": [0.25, 0.25, 0.25, 0.25]},
            "trailing_take_profit": {
                "enabled": True,
                "arm_gain_pct": 10,
                "trail_pct": 6,
            },
        }
        line = format_exit_plan_line(p, 1.08, params, now=now)
        assert "Stop -8%" in line
        assert "$0.92" in line
        assert "Ladder 2/4" in line
        assert "Trail armed ✓" in line
        assert "Peak +14%" in line
        assert "-5% off" in line
        assert "Held 3d 4h" in line

    def test_empty_when_metrics_unavailable(self):
        p = {"symbol": "AAA/USDT", "average_entry": 1.0, "amount": 10}
        assert format_exit_plan_line(p, 1.05, {}) == ""


class TestPositionCardLine:
    def test_card_appends_exit_plan_without_dropping_existing_lines(self):
        from notifications.telegram_commands.position_display import format_position_card

        p = {
            "symbol": "ARIA/USDT",
            "amount": 880.0,
            "peak_amount": 1257.0,
            "average_entry": 0.0442,
            "sold_percent": 0.3,
            "last_action": "SELL",
            "recent_high": 0.0504,
            "first_buy_at": (datetime.now() - timedelta(days=3, hours=4)).isoformat(),
            "exit_ladder_step": 2,
            "stop_loss_pct": 8,
        }
        card = format_position_card(1, p, 0.0389, numbered=True)
        assert "ARIA" in card
        assert "Bereits verkauft" in card
        assert "Letzte Aktion" in card
        assert "Peak" in card
        assert "Held" in card
        assert "Ladder 2/" in card


class TestReportingCommands:
    def setup_method(self):
        reload_messages()
        set_user_language("de")

    def _send(self):
        messages: list[str] = []

        def _capture(msg, **kwargs):
            messages.append(msg)
            return True

        return messages, _capture

    def test_fills_empty_ledger(self):
        from data_manager import resolve_ledger_scope, save_orders
        from notifications.telegram_commands import reporting_commands

        scope = resolve_ledger_scope()
        save_orders({"ledger_scope": scope, "orders": []}, scope)
        messages, capture = self._send()
        with patch(
            "notifications.telegram_commands.reporting_commands.send_telegram_message",
            side_effect=capture,
        ):
            assert reporting_commands.handle("/fills") is True
        assert messages
        assert "Keine Daten" in messages[-1]

    def test_attribution_empty_ledger(self):
        from data_manager import resolve_ledger_scope, save_orders
        from notifications.telegram_commands import reporting_commands

        scope = resolve_ledger_scope()
        save_orders({"ledger_scope": scope, "orders": []}, scope)
        messages, capture = self._send()
        with patch(
            "notifications.telegram_commands.reporting_commands.send_telegram_message",
            side_effect=capture,
        ):
            assert reporting_commands.handle("/attribution") is True
        assert messages
        assert "Keine Daten" in messages[-1]

    def test_fills_renders_summary_and_escapes(self):
        from data_manager import resolve_ledger_scope, save_orders
        from notifications.telegram_commands import reporting_commands

        scope = resolve_ledger_scope()
        orders = [
            _order(side="buy", req=100, exe=101, fee=0.2, oid="b1", venue="gate"),
            _order(
                side="sell",
                req=100,
                exe=99,
                fee=0.2,
                oid="s1",
                pnl=8,
                venue={"exchange": "gate<script>"},
            ),
        ]
        save_orders({"ledger_scope": scope, "orders": orders}, scope)
        messages, capture = self._send()
        with patch(
            "notifications.telegram_commands.reporting_commands.send_telegram_message",
            side_effect=capture,
        ):
            assert reporting_commands.handle("/fills 7") is True
        text = messages[-1]
        assert "Fills" in text
        assert "BUY" in text
        assert "SELL" in text
        assert "bps" in text
        assert "Gebühren-Drag" in text
        assert "<script>" not in text
        assert "&lt;script&gt;" in text

    def test_attribution_renders_and_escapes(self):
        from data_manager import resolve_ledger_scope, save_orders
        from notifications.telegram_commands import reporting_commands

        scope = resolve_ledger_scope()
        orders = [
            _order(
                side="sell",
                source="<grid>",
                exit_source="tail_idle",
                pnl=12,
                oid="s1",
                req=10,
                exe=10,
            ),
            _order(
                side="sell",
                source="auto",
                exit_source="stop_loss",
                pnl=-3,
                oid="s2",
                req=10,
                exe=10,
            ),
        ]
        save_orders({"ledger_scope": scope, "orders": orders}, scope)
        messages, capture = self._send()
        with patch(
            "notifications.telegram_commands.reporting_commands.send_telegram_message",
            side_effect=capture,
        ):
            assert reporting_commands.handle("/attribution") is True
        text = messages[-1]
        assert "Attribution" in text
        assert "Nach source" in text
        assert "Nach exit_source" in text
        assert "tail_idle" in text
        assert "<grid>" not in text
        assert "&lt;grid&gt;" in text

    def test_unknown_is_not_handled(self):
        from notifications.telegram_commands import reporting_commands

        assert reporting_commands.handle("/orders") is False
        assert reporting_commands.handle("fills") is False
