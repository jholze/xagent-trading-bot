import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.models import MarketContext
from services.config_fingerprint import config_fingerprint, extract_rule_snapshot
from services.observability_store import append_jsonl, load_decisions, load_snapshots, tail_jsonl
from services.position_metrics import position_metrics
from services.stack_compare import (
    build_stack_compare_report,
    format_stack_compare_telegram,
    would_sell_divergences,
)


class TestStackObservability(unittest.TestCase):
    def test_config_fingerprint_stable(self):
        cfg = {"exit_sensor": {"mode": "live"}, "volatile_altcoin": {"trailing_take_profit": {"enabled": True}}}
        self.assertEqual(config_fingerprint(cfg), config_fingerprint(cfg))
        self.assertTrue(extract_rule_snapshot(cfg))

    def test_position_metrics_peak_and_trail(self):
        market = MarketContext(
            symbol="MAGMA/USDT",
            timeframe="4h",
            current_price=0.40,
            has_position=True,
            average_entry=0.35,
            atr_pct=8.0,
        )
        pos = {"recent_high": 0.42, "strategy_tier": "volatile"}
        params = {
            "strategy_profile": "volatile_altcoin",
            "trailing_take_profit": {
                "enabled": True,
                "dynamic_trail": True,
                "arm_gain_pct": 12,
                "trail_pct_min": 3,
                "trail_pct_max": 12,
                "trail_pct_scale_start_pct": 18,
                "trail_pct_scale_peak_pct": 45,
            },
        }
        m = position_metrics(market, pos, params)
        self.assertGreater(m["peak_gain_pct"], 15)
        self.assertTrue(m["trail_armed"])
        self.assertIsNotNone(m["trail_pct_resolved"])

    def test_tail_jsonl_reads_last_records_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            for i in range(120):
                append_jsonl(str(path), {"idx": i, "symbol": f"COIN{i}/USDT"})
            tail = tail_jsonl(path, 8)
            self.assertEqual(len(tail), 8)
            self.assertEqual([row["idx"] for row in tail], list(range(112, 120)))

    def test_load_decisions_filters_stack(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            append_jsonl(str(path), {"timestamp": datetime.now().isoformat(), "bot_stack": "staging", "symbol": "A/USDT"})
            append_jsonl(str(path), {"timestamp": datetime.now().isoformat(), "bot_stack": "production", "symbol": "B/USDT"})
            st = load_decisions(bot_stack="staging", paths=[path])
            self.assertEqual(len(st), 1)
            self.assertEqual(st[0]["symbol"], "A/USDT")

    def test_stack_compare_report_aggregates(self):
        now = datetime.now()
        since = now - timedelta(hours=1)
        with tempfile.TemporaryDirectory() as tmp:
            dec = Path(tmp) / "decisions.jsonl"
            ts = now.isoformat()
            append_jsonl(str(dec), {
                "timestamp": ts,
                "bot_stack": "staging",
                "normalized_action": "SELL_PARTIAL_30",
                "sources": ["trailing_take_profit"],
                "has_position": True,
                "trail_armed": True,
                "would_source": "trailing_take_profit",
                "executed": True,
            })
            snap = Path(tmp) / "snapshots.jsonl"
            append_jsonl(str(snap), {
                "ts": ts,
                "bot_stack": "staging",
                "positions": [{
                    "key": "MAGMA_USDT_4h",
                    "symbol": "MAGMA/USDT",
                    "would_action": "HOLD",
                    "peak_gain_pct": 20,
                }],
            })
            report = build_stack_compare_report(
                since=since,
                staging_decision_paths=[dec],
                staging_snapshot_paths=[snap],
            )
            self.assertEqual(report["staging"]["decisions"]["sell_signals"], 1)
            self.assertEqual(report["staging"]["open_positions_latest"], 1)

    def test_would_sell_divergences(self):
        st = {"MAGMA_USDT_4h": {"symbol": "MAGMA/USDT", "would_action": "SELL_PARTIAL_30", "would_source": "trail"}}
        pr = {"MAGMA_USDT_4h": {"symbol": "MAGMA/USDT", "would_action": "HOLD", "would_source": ""}}
        divs = would_sell_divergences(st, pr)
        self.assertEqual(len(divs), 1)

    def test_format_stack_compare_telegram(self):
        report = {
            "since": "2026-07-08T10:00:00",
            "until": "2026-07-09T10:00:00",
            "staging": {
                "decisions": {
                    "evals": 5,
                    "with_position": 3,
                    "sell_signals": 1,
                    "executed_sells": 1,
                    "trail_armed_evals": 2,
                    "sources": __import__("collections").Counter({"trailing_take_profit": 1}),
                    "would_sources": __import__("collections").Counter(),
                    "trail_exclusive_blocked": __import__("collections").Counter(),
                },
                "open_positions_latest": 2,
                "build_commits": ["abc123"],
            },
            "production": {
                "decisions": {
                    "evals": 4,
                    "with_position": 2,
                    "sell_signals": 0,
                    "executed_sells": 0,
                    "trail_armed_evals": 1,
                    "sources": __import__("collections").Counter(),
                    "would_sources": __import__("collections").Counter(),
                    "trail_exclusive_blocked": __import__("collections").Counter(),
                },
                "open_positions_latest": 2,
                "build_commits": ["def456"],
            },
            "divergences": [{
                "symbol": "MAGMA/USDT",
                "staging_would": "SELL_PARTIAL_30",
                "prod_would": "HOLD",
                "staging_source": "trail",
                "prod_source": "",
            }],
        }
        chunks = format_stack_compare_telegram(report, local_stack="staging")
        text = "\n".join(chunks)
        self.assertIn("Stack Compare", text)
        self.assertIn("MAGMA/USDT", text)
        self.assertIn("abc123", text)
        self.assertIn("staging", text)

    def test_stack_command_handler(self):
        from notifications.telegram_commands.stack_commands import handle
        from unittest.mock import patch

        # Fresh patch each time to avoid pollution from other tests that mock threading
        with patch("notifications.telegram_commands.stack_commands.threading.Thread") as mock_thread:
            self.assertTrue(handle("/stack"))
            self.assertTrue(handle("/stack 48"))
            self.assertFalse(handle("/stack foo"))
            self.assertFalse(handle("/morning"))
            self.assertEqual(mock_thread.call_count, 2)

    def test_audit_trail_includes_stack_context(self):
        from core.models import SignalAnalysis
        from services.audit_trail import AuditTrail

        analysis = SignalAnalysis(
            action="HOLD",
            symbol="TST/USDT",
            timeframe="4h",
            rsi=50.0,
            lower_bb=1.0,
            vol_multiplier=1.0,
            ampel_emoji="🟡",
            ampel_text="neutral",
            should_notify=False,
            notify_reason="",
            normalized_action="HOLD",
            rationale="test",
            sell_policy_audit={"would_sell": "", "trail_exclusive_blocked": []},
        )
        class _Cfg:
            raw = {"observability": {"decisions_audit": True}}
            trading_mode = "demo"

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "decisions.jsonl"
            with patch("logger.DECISIONS_LOG_FILE", str(log_path)), \
                 patch("services.observability_store.mongo_sync_enabled", return_value=False), \
                 patch("services.observability_store.persist_decision"), \
                 patch("strategies.positions.get_position", return_value={"amount": 0}):
                AuditTrail(config=_Cfg()).record(
                    {"symbol": "TST/USDT", "timeframe": "4h"},
                    analysis,
                    price=1.0,
                )
            line = log_path.read_text(encoding="utf-8").strip()
            rec = json.loads(line)
            self.assertIn("bot_stack", rec)
            self.assertIn("config_fingerprint", rec)


if __name__ == "__main__":
    unittest.main()