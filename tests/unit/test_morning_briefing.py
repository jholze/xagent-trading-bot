import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from notifications.daily_stats import decision_stats, window_stats
from notifications.morning_briefing import (
    _STATE_FILE,
    build_morning_briefing,
    can_send_morning,
    mark_morning_sent,
)
from notifications.telegram_commands.morning_commands import handle


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "hermes"


class TestMorningBriefing(unittest.TestCase):
    def setUp(self):
        self._state_backup = None
        if _STATE_FILE.exists():
            self._state_backup = _STATE_FILE.read_text(encoding="utf-8")
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps({"by_chat": {}}), encoding="utf-8")

    def tearDown(self):
        if self._state_backup is None:
            if _STATE_FILE.exists():
                _STATE_FILE.unlink()
        else:
            _STATE_FILE.write_text(self._state_backup, encoding="utf-8")

    def test_can_send_blocks_same_day(self):
        mark_morning_sent("chat-1")
        allowed, sent_time = can_send_morning("chat-1")
        self.assertFalse(allowed)
        self.assertIsNotNone(sent_time)

    def test_can_send_allows_new_day(self):
        state = {"by_chat": {"chat-1": {"date": "2020-01-01", "sent_at": "2020-01-01T08:00:00"}}}
        _STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
        allowed, _ = can_send_morning("chat-1")
        self.assertTrue(allowed)

    def test_build_morning_contains_key_sections(self):
        with patch("notifications.morning_briefing.window_stats") as mock_stats:
            mock_stats.return_value = {
                "trades": [],
                "orders": [],
                "buys": 0,
                "sells": 0,
                "dca_buys": 0,
                "sell_pnl": 0.0,
                "filled_orders": 0,
                "rejected_orders": 0,
                "cash": 5000.0,
                "realized_total": 12.5,
                "open_count": 2,
                "pos_value": 500.0,
                "decisions": {
                    "total": 10,
                    "buy_dca": 3,
                    "buy_dca_executed": 1,
                    "buy_dca_shadow": 2,
                },
                "highlights": [],
                "social": [],
                "hermes": "Hermes: 5 Experimente, 0 promoted",
            }
            with patch("notifications.terminal_dashboard._portfolio_snapshot", return_value={
                "total_value": 5500.0,
                "balance": 5000.0,
                "open_positions": 2,
            }), patch("services.trading_service.TradingService") as mock_trading:
                mock_trading.return_value.risk.status_summary.return_value = {
                    "portfolio_equity": 5500.0,
                    "drawdown_pct": 1.2,
                    "daily_buys": 2,
                    "max_daily_buys": 15,
                    "daily_sells": 0,
                    "max_daily_sells": 0,
                }
                chunks = build_morning_briefing("chat-1")
        text = "\n".join(chunks)
        self.assertIn("Morning Briefing", text)
        self.assertIn("Portfolio jetzt", text)
        self.assertIn("Aktivität 24h", text)
        self.assertIn("DCA", text)
        self.assertIn("Version:", text)

    def test_morning_command_handler(self):
        self.assertTrue(handle("/morning"))
        self.assertFalse(handle("/morning force"))
        self.assertFalse(handle("/decisions"))


class TestDailyStatsWindow(unittest.TestCase):
    def test_decision_stats_counts_dca(self, bot_dir=None):
        bot_dir = Path(__file__).resolve().parents[2]
        decisions = bot_dir / "logs" / "decisions.jsonl"
        if not decisions.exists():
            self.skipTest("no decisions.jsonl")
        now = datetime.now()
        since = now - timedelta(hours=24)
        stats = decision_stats(bot_dir, since, now)
        self.assertIn("buy_dca", stats)
        self.assertIn("total", stats)


if __name__ == "__main__":
    unittest.main()