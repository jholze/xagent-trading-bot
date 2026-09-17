"""#480 — daily Telegram portfolio line must use morning NAV, not unlabeled book.

Same 08:00 tick used to send cost basis as ``Cash $… · Positionen N (~$pos_value)``
while morning printed mark-to-market NAV. Fixture has entry ≠ price so the two
formulas disagree; both reports must share ``_portfolio_snapshot()`` to the cent.
"""

from __future__ import annotations

import json
import re
from contextlib import ExitStack
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from core.config import BotConfig
from core.tenant_context import DEFAULT_TENANT, tenant_context
from notifications.morning_briefing import build_morning_briefing
from notifications.terminal_dashboard import _portfolio_snapshot
from scripts.daily_auswertung import build_telegram_daily_summary


REPORT_DATE = datetime(2026, 9, 17, 12, 0, 0)
SYMBOL = "AAA/USDT"
POS_KEY = "AAA_USDT_4h"
CASH = 1_000.25
AMOUNT = 10.0
ENTRY = 50.00
PRICE = 41.37
BOOK = AMOUNT * ENTRY  # 500.00
MTM = AMOUNT * PRICE  # 413.70
NAV = CASH + MTM  # 1_413.95

_NAV_RE = re.compile(r"NAV\s+(?:<b>)?\$([0-9,]+\.?\d*)")
_BUCH_RE = re.compile(r"Buchwert\s+\$([0-9,]+\.?\d*)")


def _money(match: re.Match[str]) -> float:
    return float(match.group(1).replace(",", ""))


def _parse_nav(text: str) -> float:
    match = _NAV_RE.search(text)
    assert match, f"NAV not found in:\n{text}"
    return _money(match)


def _parse_buchwert(text: str) -> float:
    match = _BUCH_RE.search(text)
    assert match, f"Buchwert not found in:\n{text}"
    return _money(match)


def _cfg() -> BotConfig:
    return BotConfig(
        {
            "trading_mode": "paper",
            "virtual_trading": True,
            "live": {"dry_run": True},
            "observability": {
                "morning_briefing_enabled": True,
                "daily_report_telegram": True,
            },
        }
    )


def _lot() -> dict:
    return {
        "symbol": SYMBOL,
        "timeframe": "4h",
        "amount": AMOUNT,
        "average_entry": ENTRY,
        "entry_price": ENTRY,
        "sold_percent": 0,
        "last_rsi": 40,
    }


def _history() -> dict:
    return {"trades": [], "virtual_balance": CASH, "realized_pnl": 0.0}


def _positions_doc() -> dict:
    return {
        "positions": {
            POS_KEY: {
                "amount": AMOUNT,
                "average_entry": ENTRY,
                "sold_percent": 0,
                "last_rsi": 40,
            }
        }
    }


def _window_stats() -> dict:
    return {
        "trades": [],
        "orders": [],
        "buys": 0,
        "sells": 0,
        "dca_buys": 0,
        "sell_pnl": 0.0,
        "filled_orders": 0,
        "rejected_orders": 0,
        "cash": CASH,
        "realized_total": 0.0,
        "open_count": 1,
        "pos_value": BOOK,
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


def _trading_service():
    svc = MagicMock()
    svc.risk.status_summary.return_value = {
        "portfolio_equity": NAV,
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


def _nav_stack():
    cfg = _cfg()
    return ExitStack(), [
        patch("core.config.get_bot_config", return_value=cfg),
        patch("notifications.terminal_dashboard.get_bot_config", return_value=cfg),
        patch("notifications.morning_briefing.get_bot_config", return_value=cfg),
        patch("core.simulated_trading.uses_order_ledger_cash", return_value=False),
        patch("data_manager.uses_exchange_ledger", return_value=False),
        patch(
            "notifications.telegram_commands.position_display.load_trade_history_safe",
            return_value=_history(),
        ),
        patch("data_manager.load_trade_history_document", return_value=_history()),
        patch("data_manager.load_orders", return_value={"orders": []}),
        patch("data_manager.load_positions_document", return_value=_positions_doc()),
        patch(
            "strategies.positions.list_active_positions_from_ledger",
            return_value=[_lot()],
        ),
        patch("price_fetcher.get_prices_batch", return_value={SYMBOL: PRICE}),
        patch(
            "notifications.morning_briefing.window_stats",
            return_value=_window_stats(),
        ),
        patch(
            "services.trading_service.TradingService",
            side_effect=lambda *a, **k: _trading_service(),
        ),
        patch("services.reporting.metrics.format_live_metrics_block", return_value=""),
    ]


def test_daily_telegram_nav_matches_morning_to_the_cent(bot_dir):
    """entry ≠ price → book ≠ NAV; daily Telegram NAV equals morning snapshot cents."""
    stack, patches = _nav_stack()
    with stack:
        for p in patches:
            stack.enter_context(p)
        with tenant_context(DEFAULT_TENANT, scope="demo"):
            snap = _portfolio_snapshot("paper")
            morning = "\n".join(build_morning_briefing("op-480"))
            daily = build_telegram_daily_summary(bot_dir, REPORT_DATE)

    morning_nav = float(snap["total_value"])
    daily_nav = _parse_nav(daily)
    book = _parse_buchwert(daily)

    assert morning_nav == pytest.approx(NAV, abs=0.005)
    assert daily_nav == pytest.approx(morning_nav, abs=0.005)
    assert daily_nav == pytest.approx(NAV, abs=0.005)
    assert f"NAV ${NAV:,.2f}" in daily
    assert f"NAV <b>${NAV:,.0f}</b>" in morning
    assert book == pytest.approx(BOOK, abs=0.005)
    assert daily_nav != pytest.approx(book, abs=0.005)
    assert "(~$" not in daily
    assert "Buchwert" in daily


def test_daily_telegram_unlabeled_tilde_book_is_gone_when_snapshot_fails(bot_dir):
    with patch(
        "notifications.terminal_dashboard._portfolio_snapshot",
        side_effect=RuntimeError("prices unavailable"),
    ), patch("data_manager.load_trade_history_document", return_value=_history()), patch(
        "data_manager.load_orders", return_value={"orders": []}
    ), patch(
        "data_manager.load_positions_document", return_value=_positions_doc()
    ):
        daily = build_telegram_daily_summary(bot_dir, REPORT_DATE)

    assert "(~$" not in daily
    assert "NAV $" not in daily
    assert "Buchwert" in daily
    assert _parse_buchwert(daily) == pytest.approx(BOOK, abs=0.005)


def test_daily_telegram_nav_does_not_bootstrap_or_mutate_active_key(bot_dir):
    import strategies.positions as posmod

    before = posmod._active_key
    stack, patches = _nav_stack()
    with stack:
        for p in patches:
            stack.enter_context(p)
        boot = stack.enter_context(patch("strategies.positions.bootstrap_positions"))
        with tenant_context(DEFAULT_TENANT, scope="demo"):
            daily = build_telegram_daily_summary(bot_dir, REPORT_DATE)

    boot.assert_not_called()
    assert posmod._active_key == before
    assert _parse_nav(daily) == pytest.approx(NAV, abs=0.005)


def test_daily_telegram_nav_honors_tenant_context(bot_dir):
    henry_cash = 99.0
    henry_amount = 10.0
    henry_entry = 2.0
    henry_price = 1.51
    henry_nav = henry_cash + henry_amount * henry_price
    default_nav = NAV

    def history(*_a, **_k):
        from core.tenant_context import resolve_tenant_id

        if resolve_tenant_id() == "henry":
            return {"trades": [], "virtual_balance": henry_cash, "realized_pnl": 0.0}
        return _history()

    def positions(*_a, **_k):
        from core.tenant_context import resolve_tenant_id

        if resolve_tenant_id() == "henry":
            return {
                "positions": {
                    "NPC_USDT_4h": {
                        "amount": henry_amount,
                        "average_entry": henry_entry,
                        "sold_percent": 0,
                        "last_rsi": 40,
                    }
                }
            }
        return _positions_doc()

    def lots(*_a, **_k):
        from core.tenant_context import resolve_tenant_id

        if resolve_tenant_id() == "henry":
            return [
                {
                    "symbol": "NPC/USDT",
                    "timeframe": "4h",
                    "amount": henry_amount,
                    "average_entry": henry_entry,
                    "entry_price": henry_entry,
                    "sold_percent": 0,
                }
            ]
        return [_lot()]

    def prices(symbols, fallbacks=None, **_k):
        from core.tenant_context import resolve_tenant_id

        if resolve_tenant_id() == "henry":
            return {"NPC/USDT": henry_price}
        return {SYMBOL: PRICE}

    cfg = _cfg()
    shared = [
        patch("core.config.get_bot_config", return_value=cfg),
        patch("notifications.terminal_dashboard.get_bot_config", return_value=cfg),
        patch("core.simulated_trading.uses_order_ledger_cash", return_value=False),
        patch("data_manager.uses_exchange_ledger", return_value=False),
        patch(
            "notifications.telegram_commands.position_display.load_trade_history_safe",
            side_effect=history,
        ),
        patch("data_manager.load_trade_history_document", side_effect=history),
        patch("data_manager.load_orders", return_value={"orders": []}),
        patch("data_manager.load_positions_document", side_effect=positions),
        patch("strategies.positions.list_active_positions_from_ledger", side_effect=lots),
        patch("price_fetcher.get_prices_batch", side_effect=prices),
    ]
    with ExitStack() as stack:
        for p in shared:
            stack.enter_context(p)
        with tenant_context(DEFAULT_TENANT, scope="demo"):
            default_msg = build_telegram_daily_summary(bot_dir, REPORT_DATE)
        with tenant_context("henry", scope="demo", owner_chat_id="henry-owner"):
            henry_msg = build_telegram_daily_summary(bot_dir, REPORT_DATE)

    assert "tenant <code>default</code>" in default_msg
    assert "tenant <code>henry</code>" in henry_msg
    assert _parse_nav(default_msg) == pytest.approx(default_nav, abs=0.005)
    assert _parse_nav(henry_msg) == pytest.approx(henry_nav, abs=0.005)
    assert _parse_nav(default_msg) != pytest.approx(_parse_nav(henry_msg), abs=0.005)
    assert f"NAV ${henry_nav:,.2f}" not in default_msg
    assert f"NAV ${NAV:,.2f}" not in henry_msg
    assert "(~$" not in default_msg
    assert "(~$" not in henry_msg
