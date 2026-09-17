"""#478 — 08:00 Telegram daily/morning reports must be per-tenant.

The background tick used to load the default ledger with no tenant_context.
Default mail must not include Henry lots; each tenant gets a tagged message
on the operator chat, inside one morning send window.
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from core.tenant_context import DEFAULT_TENANT, resolve_tenant_id, tenant_context
from notifications.daily_stats import (
    load_orders_doc,
    load_trade_history_doc,
    open_positions_summary,
)
from notifications.morning_briefing import build_morning_briefing
from scripts.daily_auswertung import (
    build_telegram_daily_summary,
    iter_daily_report_tenants,
)
from storage.errors import LedgerUnavailable
import services.background_runtime as bg


HENRY_LOT = "NPC/USDT"
DEFAULT_LOT = "BTC/USDT"
OPERATOR = "op-chat-478"
REPORT_TS = "2026-09-17T07:15:00"


def _now(hour=8, minute=0):
    return datetime(2026, 9, 17, hour, minute, tzinfo=ZoneInfo("Europe/Berlin"))


def _report_date():
    return datetime(2026, 9, 17, 12, 0, 0)


def _henry_doc():
    return {
        "tenant_id": "henry",
        "status": "active",
        "telegram": {"owner_chat_id": "henry-owner"},
    }


def _obs(**overrides):
    cfg = {
        "morning_briefing_enabled": True,
        "morning_briefing_hour": 8,
        "daily_report_telegram": True,
    }
    cfg.update(overrides)
    return cfg


def _th_for(tid: str) -> dict:
    lot = HENRY_LOT if tid == "henry" else DEFAULT_LOT
    return {
        "trades": [
            {
                "type": "BUY",
                "symbol": lot,
                "timestamp": REPORT_TS,
                "usdt_amount": 100,
                "source": "grid",
            }
        ],
        "virtual_balance": 99.0 if tid == "henry" else 1536.0,
        "realized_pnl": 0.0,
    }


def _positions_for(tid: str) -> dict:
    if tid == "henry":
        return {
            "positions": {
                "NPC_USDT_4h": {
                    "amount": 10,
                    "average_entry": 2.0,
                    "sold_percent": 0,
                    "last_rsi": 40,
                }
            }
        }
    return {
        "positions": {
            "BTC_USDT_4h": {
                "amount": 0.1,
                "average_entry": 50000,
                "sold_percent": 0,
                "last_rsi": 50,
            }
        }
    }


def _fake_load_th(scope, config=None, tenant_id=None):
    return _th_for(resolve_tenant_id(tenant_id))


def _fake_load_orders(scope, tenant_id=None):
    return {"orders": []}


def _fake_load_positions(scope=None, config=None, tenant_id=None):
    return _positions_for(resolve_tenant_id(tenant_id))


def _window_stats_for_tenant(*_a, **_k):
    tid = resolve_tenant_id()
    lot = HENRY_LOT if tid == "henry" else DEFAULT_LOT
    return {
        "trades": [
            {
                "type": "BUY",
                "symbol": lot,
                "timestamp": REPORT_TS,
                "usdt_amount": 100,
                "source": "grid",
                "pnl": None,
            }
        ],
        "orders": [],
        "buys": 1,
        "sells": 0,
        "dca_buys": 0,
        "sell_pnl": 0.0,
        "filled_orders": 0,
        "rejected_orders": 0,
        "cash": 99.0 if tid == "henry" else 1536.0,
        "realized_total": 0.0,
        "open_count": 1,
        "pos_value": 20.0 if tid == "henry" else 5000.0,
        "decisions": {
            "total": 0,
            "buy_dca": 0,
            "buy_dca_executed": 0,
            "buy_dca_shadow": 0,
        },
        "highlights": [],
        "social": [],
        "hermes": "Hermes: —",
    }


def _portfolio_for_tenant(*_a, **_k):
    tid = resolve_tenant_id()
    if tid == "henry":
        return {"total_value": 99022.0, "balance": 99.0, "open_positions": 1}
    return {"total_value": 103587.0, "balance": 1536.0, "open_positions": 1}


def _trading_service():
    svc = MagicMock()
    svc.risk.status_summary.return_value = {
        "portfolio_equity": 1000.0,
        "drawdown_pct": 0.0,
        "daily_buys": 0,
        "max_daily_buys": 15,
        "daily_sells": 0,
        "max_daily_sells": 0,
    }
    return svc


@pytest.fixture
def bot_dir(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps({"live": {"dry_run": True}, "observability": {"daily_report_telegram": True}}),
        encoding="utf-8",
    )
    return tmp_path


class TestIterDailyReportTenants:
    def test_default_only_when_mt_off(self, monkeypatch):
        monkeypatch.setenv("MULTI_TENANT_ENABLED", "0")
        tenants = iter_daily_report_tenants()
        assert tenants == [(DEFAULT_TENANT, "")]

    def test_skips_empty_inactive_no_owner_and_does_not_dup_default(self, monkeypatch):
        monkeypatch.setenv("MULTI_TENANT_ENABLED", "1")
        docs = [
            {
                "tenant_id": DEFAULT_TENANT,
                "status": "active",
                "telegram": {"owner_chat_id": "default-owner"},
            },
            {"tenant_id": "", "status": "active", "telegram": {"owner_chat_id": "x"}},
            {"tenant_id": "paused", "status": "paused", "telegram": {"owner_chat_id": "x"}},
            {"tenant_id": "noowner", "status": "active", "telegram": {}},
            _henry_doc(),
        ]
        with patch("storage.tenant_registry.list_active_tenants", return_value=docs):
            tenants = iter_daily_report_tenants()
        assert tenants[0] == (DEFAULT_TENANT, "")
        assert ("henry", "henry-owner") in tenants
        ids = [tid for tid, _owner in tenants]
        assert ids.count(DEFAULT_TENANT) == 1
        assert "paused" not in ids
        assert "noowner" not in ids
        assert "" not in ids


class TestBuilderTenantIsolation:
    def test_daily_summary_tags_tenant_and_default_omits_henry_lots(self, bot_dir):
        with patch("data_manager.load_trade_history_document", side_effect=_fake_load_th), patch(
            "data_manager.load_orders", side_effect=_fake_load_orders
        ), patch("data_manager.load_positions_document", side_effect=_fake_load_positions):
            with tenant_context(DEFAULT_TENANT, scope="demo"):
                default_msg = build_telegram_daily_summary(bot_dir, _report_date())
            with tenant_context("henry", scope="demo", owner_chat_id="henry-owner"):
                henry_msg = build_telegram_daily_summary(bot_dir, _report_date())

        assert "tenant <code>default</code>" in default_msg
        assert "Tages-Auswertung 2026-09-17" in default_msg
        assert DEFAULT_LOT in default_msg
        assert HENRY_LOT not in default_msg

        assert "tenant <code>henry</code>" in henry_msg
        assert HENRY_LOT in henry_msg
        assert DEFAULT_LOT not in henry_msg

    def test_morning_briefing_tags_tenant_and_default_omits_henry_lots(self):
        with patch(
            "notifications.morning_briefing.window_stats",
            side_effect=_window_stats_for_tenant,
        ), patch(
            "notifications.terminal_dashboard._portfolio_snapshot",
            side_effect=_portfolio_for_tenant,
        ), patch(
            "services.trading_service.TradingService",
            side_effect=lambda *a, **k: _trading_service(),
        ), patch(
            "services.reporting.metrics.format_live_metrics_block",
            return_value="",
        ):
            with tenant_context(DEFAULT_TENANT, scope="demo"):
                default_msg = "\n".join(build_morning_briefing(OPERATOR))
            with tenant_context("henry", scope="demo", owner_chat_id="henry-owner"):
                henry_msg = "\n".join(build_morning_briefing(OPERATOR))

        assert "tenant <code>default</code>" in default_msg
        assert DEFAULT_LOT in default_msg
        assert HENRY_LOT not in default_msg

        assert "tenant <code>henry</code>" in henry_msg
        assert HENRY_LOT in henry_msg
        assert DEFAULT_LOT not in henry_msg


class TestDailyTickFanout:
    def setup_method(self):
        bg._last_daily_tick_day = None

    def teardown_method(self):
        bg._last_daily_tick_day = None

    def test_two_tagged_sends_to_operator_default_omits_henry_lots(self, monkeypatch):
        monkeypatch.setenv("MULTI_TENANT_ENABLED", "1")
        morning_chunks: list[tuple[str, str]] = []
        daily_msgs: list[tuple[str | None, str]] = []

        def capture_chunk(chat_id, chunk):
            morning_chunks.append((str(chat_id), chunk))
            return True

        def capture_daily(text, reply_markup=None, *, chat_id=None, parse_mode="HTML"):
            daily_msgs.append((None if chat_id is None else str(chat_id), text))
            return True

        with patch(
            "storage.tenant_registry.list_active_tenants",
            return_value=[_henry_doc()],
        ), patch.object(bg, "_operator_chat_id", return_value=OPERATOR), patch(
            "notifications.morning_briefing._send_chunk", side_effect=capture_chunk
        ), patch(
            "telegram_notifier._send_telegram_direct", side_effect=capture_daily
        ), patch(
            "notifications.morning_briefing.window_stats",
            side_effect=_window_stats_for_tenant,
        ), patch(
            "notifications.terminal_dashboard._portfolio_snapshot",
            side_effect=_portfolio_for_tenant,
        ), patch(
            "services.trading_service.TradingService",
            side_effect=lambda *a, **k: _trading_service(),
        ), patch(
            "services.reporting.metrics.format_live_metrics_block",
            return_value="",
        ), patch(
            "data_manager.load_trade_history_document", side_effect=_fake_load_th
        ), patch(
            "data_manager.load_orders", side_effect=_fake_load_orders
        ), patch(
            "data_manager.load_positions_document", side_effect=_fake_load_positions
        ):
            result = bg._maybe_tick_daily_reports(_now(8, 0), cfg=_obs())

        assert result["fired"] is True
        assert result["morning"] is True
        assert result["daily"] is True

        assert len(morning_chunks) == 2
        assert all(cid == OPERATOR for cid, _text in morning_chunks)
        morning_by_tenant = {
            "default": [t for _c, t in morning_chunks if "tenant <code>default</code>" in t],
            "henry": [t for _c, t in morning_chunks if "tenant <code>henry</code>" in t],
        }
        assert len(morning_by_tenant["default"]) == 1
        assert len(morning_by_tenant["henry"]) == 1
        assert HENRY_LOT not in morning_by_tenant["default"][0]
        assert DEFAULT_LOT in morning_by_tenant["default"][0]
        assert HENRY_LOT in morning_by_tenant["henry"][0]
        assert DEFAULT_LOT not in morning_by_tenant["henry"][0]

        assert len(daily_msgs) == 2
        assert all(cid == OPERATOR for cid, _text in daily_msgs)
        daily_by_tenant = {
            "default": [t for _c, t in daily_msgs if "tenant <code>default</code>" in t],
            "henry": [t for _c, t in daily_msgs if "tenant <code>henry</code>" in t],
        }
        assert len(daily_by_tenant["default"]) == 1
        assert len(daily_by_tenant["henry"]) == 1
        assert HENRY_LOT not in daily_by_tenant["default"][0]
        assert DEFAULT_LOT in daily_by_tenant["default"][0]
        assert HENRY_LOT in daily_by_tenant["henry"][0]
        assert DEFAULT_LOT not in daily_by_tenant["henry"][0]

    def test_morning_marker_does_not_drop_second_tenant(self, monkeypatch):
        """can_send_morning is keyed on operator chat — both tenants still send."""
        monkeypatch.setenv("MULTI_TENANT_ENABLED", "1")
        morning_chunks: list[str] = []
        mark_calls: list[str] = []

        def capture_chunk(chat_id, chunk):
            morning_chunks.append(chunk)
            return True

        def capture_mark(chat_id=None, *, now=None):
            mark_calls.append(str(chat_id or ""))

        with patch(
            "storage.tenant_registry.list_active_tenants",
            return_value=[_henry_doc()],
        ), patch.object(bg, "_operator_chat_id", return_value=OPERATOR), patch(
            "notifications.morning_briefing._send_chunk", side_effect=capture_chunk
        ), patch(
            "notifications.morning_briefing.mark_morning_sent", side_effect=capture_mark
        ), patch(
            "telegram_notifier._send_telegram_direct", return_value=True
        ), patch(
            "notifications.morning_briefing.window_stats",
            side_effect=_window_stats_for_tenant,
        ), patch(
            "notifications.terminal_dashboard._portfolio_snapshot",
            side_effect=_portfolio_for_tenant,
        ), patch(
            "services.trading_service.TradingService",
            side_effect=lambda *a, **k: _trading_service(),
        ), patch(
            "services.reporting.metrics.format_live_metrics_block",
            return_value="",
        ), patch(
            "data_manager.load_trade_history_document", side_effect=_fake_load_th
        ), patch(
            "data_manager.load_orders", side_effect=_fake_load_orders
        ), patch(
            "data_manager.load_positions_document", side_effect=_fake_load_positions
        ):
            result = bg._maybe_tick_daily_reports(
                _now(8, 0), cfg=_obs(daily_report_telegram=False)
            )

        assert result["fired"] is True
        assert result["morning"] is True
        tagged = [c for c in morning_chunks if "tenant <code>" in c]
        assert any("tenant <code>default</code>" in c for c in tagged)
        assert any("tenant <code>henry</code>" in c for c in tagged)
        # Marker is applied once after both tenants, not after the first.
        assert mark_calls == [OPERATOR]

    def test_tick_leaves_active_key_unchanged(self, monkeypatch):
        """B1: 08:00 fan-out must not leave strategies.positions._active_key on henry."""
        monkeypatch.setenv("MULTI_TENANT_ENABLED", "1")
        import strategies.positions as posmod

        before = posmod._active_key
        morning_ok: list[bool] = []

        def capture_chunk(chat_id, chunk):
            morning_ok.append(True)
            return True

        with patch(
            "storage.tenant_registry.list_active_tenants",
            return_value=[_henry_doc()],
        ), patch.object(bg, "_operator_chat_id", return_value=OPERATOR), patch(
            "notifications.morning_briefing._send_chunk", side_effect=capture_chunk
        ), patch(
            "telegram_notifier._send_telegram_direct", return_value=True
        ), patch(
            "services.trading_service.TradingService",
            side_effect=lambda *a, **k: _trading_service(),
        ), patch(
            "services.reporting.metrics.format_live_metrics_block",
            return_value="",
        ), patch(
            "data_manager.load_trade_history_document", side_effect=_fake_load_th
        ), patch(
            "data_manager.load_orders", side_effect=_fake_load_orders
        ), patch(
            "data_manager.load_positions_document", side_effect=_fake_load_positions
        ), patch(
            "services.ledger_sync._build_positions_snapshot_from_orders",
            return_value={},
        ), patch(
            "price_fetcher.get_prices_batch", return_value={}
        ):
            result = bg._maybe_tick_daily_reports(
                _now(8, 0), cfg=_obs(daily_report_telegram=False)
            )

        assert result["fired"] is True
        assert result["morning"] is True
        assert morning_ok
        assert posmod._active_key == before


class TestLedgerReadFailClosed:
    """B2: Mongo blip must not render as NAV $0 / 0 positions / Trades 0."""

    def test_load_trade_history_doc_raises_not_silent_zeros(self, monkeypatch):
        monkeypatch.setenv("MULTI_TENANT_ENABLED", "1")
        logged: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "notifications.daily_stats.log",
            lambda message, level="INFO": logged.append((str(message), str(level))),
        )
        with tenant_context("henry", scope="demo"), patch(
            "data_manager.load_trade_history_document",
            side_effect=RuntimeError("mongo blip"),
        ):
            with pytest.raises(LedgerUnavailable) as info:
                load_trade_history_doc()
        assert info.value.tenant_id == "henry"
        assert info.value.op == "load_trade_history_document"
        assert any(level == "WARNING" and "henry" in msg for msg, level in logged)

    def test_load_orders_doc_raises_not_silent_zeros(self, monkeypatch):
        monkeypatch.setenv("MULTI_TENANT_ENABLED", "1")
        logged: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "notifications.daily_stats.log",
            lambda message, level="INFO": logged.append((str(message), str(level))),
        )
        with tenant_context("henry", scope="demo"), patch(
            "data_manager.load_orders",
            side_effect=RuntimeError("mongo blip"),
        ):
            with pytest.raises(LedgerUnavailable) as info:
                load_orders_doc()
        assert info.value.tenant_id == "henry"
        assert info.value.op == "load_orders"
        assert any(level == "WARNING" and "henry" in msg for msg, level in logged)

    def test_open_positions_summary_raises_not_silent_zeros(self, monkeypatch):
        monkeypatch.setenv("MULTI_TENANT_ENABLED", "1")
        logged: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "notifications.daily_stats.log",
            lambda message, level="INFO": logged.append((str(message), str(level))),
        )
        with tenant_context("henry", scope="demo"), patch(
            "strategies.positions.list_active_positions_from_ledger",
            side_effect=RuntimeError("mongo blip"),
        ), patch(
            "strategies.positions.bootstrap_positions",
        ) as boot:
            with pytest.raises(LedgerUnavailable) as info:
                open_positions_summary()
            boot.assert_not_called()
        assert info.value.tenant_id == "henry"
        assert info.value.op == "list_active_positions_from_ledger"
        assert any(level == "WARNING" and "henry" in msg for msg, level in logged)

    def test_henry_does_not_inherit_default_json_on_ledger_failure(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("MULTI_TENANT_ENABLED", "1")
        default_json = tmp_path / "trade_history.demo.json"
        default_json.write_text(
            json.dumps({"trades": [{"type": "BUY", "symbol": DEFAULT_LOT}]}),
            encoding="utf-8",
        )
        monkeypatch.setattr("notifications.daily_stats.BOT_ROOT", tmp_path)
        with tenant_context("henry", scope="demo"), patch(
            "data_manager.load_trade_history_document",
            side_effect=RuntimeError("mongo blip"),
        ):
            with pytest.raises(LedgerUnavailable):
                load_trade_history_doc()
