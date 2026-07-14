import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from services.grid_status_service import (
    _classify_mode,
    _parse_regime,
    build_grid_status_report,
    format_grid_status_telegram,
)


class TestGridStatus(unittest.TestCase):
    def test_parse_regime(self):
        self.assertEqual(_parse_regime("TA ok | regime=RANGING | vol=1.2"), "RANGING")
        self.assertIsNone(_parse_regime("no regime here"))

    def test_classify_modes(self):
        self.assertEqual(_classify_mode(forced=True, state=None, last=None), "forced")
        self.assertEqual(
            _classify_mode(
                forced=False,
                state=None,
                last={"strategy_profile": "grid", "sources": ["grid"]},
            ),
            "active",
        )
        self.assertEqual(
            _classify_mode(forced=False, state={"center_price": 100}, last=None),
            "tracking",
        )
        self.assertEqual(
            _classify_mode(forced=False, state=None, last={"sources": ["technical", "grid"]}),
            "partial",
        )
        self.assertEqual(
            _classify_mode(forced=False, state=None, last={"sources": ["technical"]}),
            "off",
        )

    @patch("services.grid_status_service.tail_jsonl")
    @patch("services.grid_status_service.load_effective_watchlist")
    @patch("services.grid_status_service.get_config")
    @patch("services.grid_status_service.resolve_coin_config")
    def test_build_report_groups_modes(self, mock_resolve, mock_cfg, mock_wl, mock_tail):
        mock_wl.return_value = [
            {"symbol": "BTC/USDT", "timeframe": "4h", "active": True},
            {"symbol": "ETH/USDT", "timeframe": "4h", "active": True},
        ]
        mock_cfg.return_value = {
            "grid_states": {"BTC/USDT_4h": {"center_price": 100000, "spacing": 500, "levels": []}},
            "grid": {"enabled": True},
            "strategy_allocator": {"enabled": True},
            "regime_detector": {"enabled": True},
        }
        mock_resolve.side_effect = lambda c: dict(c)
        mock_tail.return_value = [
            {
                "symbol": "BTC/USDT",
                "strategy_profile": "grid",
                "sources": ["grid"],
                "rationale": "Grid buy | regime=RANGING",
                "normalized_action": "HOLD",
                "timestamp": "2026-07-14T12:00:00",
            },
            {
                "symbol": "ETH/USDT",
                "strategy_profile": "technical_rsi_bb",
                "sources": ["technical"],
                "rationale": "regime=STRONG_UPTREND",
                "normalized_action": "HOLD",
                "timestamp": "2026-07-14T12:00:00",
            },
        ]

        report = build_grid_status_report()
        self.assertEqual(report["watchlist_active"], 2)
        self.assertIn("active", report["by_mode"])
        self.assertIn("off", report["by_mode"])
        msg = format_grid_status_telegram(report)
        self.assertIn("BTC/USDT", msg)
        self.assertIn("Grid", msg)


if __name__ == "__main__":
    unittest.main()