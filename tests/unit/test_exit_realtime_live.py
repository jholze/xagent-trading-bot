"""Fat tests for exit_realtime live sell path (mocked trading, no Gate)."""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from services.exit_realtime.execute import (
    recently_exited,
    try_execute_trail_exit,
)
from services.exit_realtime.hub import ExitRealtimeHub
from services.exit_realtime.shadow_eval import evaluate_would_sells


def _trail_params(**over):
    base = {
        "trailing_stop": {
            "enabled": True,
            "mode": "live",
            "activation_gain_pct": 5,
            "min_trail_pct": 8,
            "max_trail_pct": 25,
            "atr_multiplier": 2.0,
        },
        "trailing_take_profit": {
            "enabled": True,
            "mode": "live",
            "arm_gain_pct": 12,
            "min_gain_pct": 10,
            "min_gain_pct_floor": 8,
            "trail_pct": 6,
            "dynamic_trail": True,
            "trail_pct_min": 3,
            "trail_pct_max": 12,
            "trail_pct_scale_start_pct": 18,
            "trail_pct_scale_peak_pct": 45,
            "max_steps": 1,
            "cooldown_hours": 0,
        },
    }
    base.update(over)
    return base


class TestTryExecuteTrailExit(unittest.TestCase):
    def setUp(self):
        import services.exit_realtime.execute as ex

        ex._inflight.clear()
        ex._last_exit_at.clear()

    def test_no_position_fails(self):
        with patch(
            "strategies.positions.get_position",
            return_value={"amount": 0},
        ), patch(
            "strategies.positions.is_open_position",
            return_value=False,
        ):
            r = try_execute_trail_exit(
                symbol="TAG/USDT",
                timeframe="1h",
                price=0.0014,
                action="SELL_FULL",
                exit_source="trailing_stop",
            )
        self.assertFalse(r["executed"])
        self.assertEqual(r["message"], "no_open_position")

    def test_execute_success_calls_trading(self):
        trading = MagicMock()
        trading.execute_order.return_value = SimpleNamespace(
            executed=True, message="ok filled"
        )
        pos = {
            "amount": 1000.0,
            "average_entry": 1.0,
            "recent_high": 1.2,
        }
        with patch(
            "strategies.positions.get_position", return_value=pos
        ), patch(
            "strategies.positions.is_open_position", return_value=True
        ), patch(
            "strategies.positions.mark_trailing_take_profit_step"
        ) as mark_tp, patch(
            "strategies.positions.flush_positions"
        ):
            r = try_execute_trail_exit(
                symbol="TAG/USDT",
                timeframe="1h",
                price=1.1,
                action="SELL_FULL",
                exit_source="trailing_take_profit",
                rationale="Trail->take profit",
                trading=trading,
            )
        self.assertTrue(r["executed"])
        trading.execute_order.assert_called_once()
        order = trading.execute_order.call_args[0][0]
        self.assertEqual(order.type, "SELL")
        self.assertEqual(order.symbol, "TAG/USDT")
        self.assertEqual(order.amount, 1000.0)
        self.assertEqual(order.source, "exit_ws")
        self.assertEqual(order.exit_source, "trailing_take_profit")
        mark_tp.assert_called_once()
        self.assertTrue(recently_exited("TAG/USDT", within_sec=120))

    def test_inflight_blocks_second(self):
        import services.exit_realtime.execute as ex

        ex._inflight.add("X/USDT")
        r = try_execute_trail_exit(
            symbol="X/USDT",
            timeframe="1h",
            price=1.0,
            action="SELL_FULL",
            exit_source="trailing_stop",
        )
        self.assertEqual(r["message"], "inflight")
        ex._inflight.discard("X/USDT")

    def test_risk_block_not_executed(self):
        trading = MagicMock()
        trading.execute_order.return_value = SimpleNamespace(
            executed=False, message="partial_sell_guard"
        )
        with patch(
            "strategies.positions.get_position",
            return_value={"amount": 50.0, "average_entry": 1.0},
        ), patch(
            "strategies.positions.is_open_position", return_value=True
        ):
            r = try_execute_trail_exit(
                symbol="Y/USDT",
                timeframe="1h",
                price=1.1,
                action="SELL_FULL",
                exit_source="trailing_stop",
                trading=trading,
            )
        self.assertFalse(r["executed"])
        self.assertIn("partial_sell", r["message"])


class TestHubLivePath(unittest.TestCase):
    def test_correlated_tier_watch_survives_gainer_watch_update(self):
        raw = {
            "exit_realtime": {"enabled": True, "mode": "shadow"},
            "sell_policy": {
                "correlated_tier": {
                    "enabled": True,
                    "groups": {
                        "crypto_market": {
                            "proxy_symbols": ["BTC/USDT", "ETH/USDT"],
                            "member_symbols": "*",
                            "drawdown_pct": 4.0,
                            "window_sec": 900,
                            "min_confirming": 1,
                        }
                    },
                    "eval_interval_sec": 5,
                    "flag_ttl_sec": 30,
                }
            },
        }
        hub = ExitRealtimeHub(raw)
        self.assertIn("BTC/USDT", hub._ct_watch_symbols)
        pairs = hub._desired_gate_pairs()
        self.assertIn("BTC_USDT", pairs)
        hub.update_watch_set(["SOL/USDT"])
        self.assertIn("BTC/USDT", hub._ct_watch_symbols)
        self.assertIn("SOL/USDT", hub._watch_symbols)
        pairs2 = hub._desired_gate_pairs()
        self.assertIn("BTC_USDT", pairs2)
        self.assertIn("SOL_USDT", pairs2)

    def test_on_ticker_live_executes(self):
        raw = {
            "exit_realtime": {
                "enabled": True,
                "mode": "live",
                "sources": ["trailing_stop"],
                "live_cooldown_sec": 0.01,
                "default_atr_pct": 3.0,
            }
        }
        hub = ExitRealtimeHub(raw)
        pos = {
            "amount": 100.0,
            "average_entry": 1.0,
            "recent_high": 1.25,
        }
        params = _trail_params()
        # force stop trail to fire: gain 10%, drop from high 12%
        hub.update_book(
            [
                {
                    "symbol": "H/USDT",
                    "timeframe": "1h",
                    "position": pos,
                    "average_entry": 1.0,
                    "recent_high": 1.25,
                    "strategy_params": params,
                    "atr_pct": 3.0,
                }
            ]
        )
        with patch(
            "services.exit_realtime.hub.try_execute_trail_exit",
            return_value={"ok": True, "executed": True, "message": "ok"},
        ) as mock_ex, patch(
            "services.exit_realtime.hub._log_event"
        ):
            # price 1.10 → gain 10%, drop from 1.25 = 12% > 8% trail
            hub.on_ticker("H_USDT", 1.10)
        mock_ex.assert_called()
        self.assertGreaterEqual(hub.stats()["executed"], 1)
        # removed from book after exec
        self.assertNotIn("H/USDT", hub._book)

    def test_on_ticker_non_live_does_not_execute(self):
        raw = {
            "exit_realtime": {
                "enabled": True,
                "mode": "shadow",
                "sources": ["trailing_stop"],
                "live_cooldown_sec": 0.01,
            }
        }
        hub = ExitRealtimeHub(raw)
        hub.update_book(
            [
                {
                    "symbol": "H/USDT",
                    "timeframe": "1h",
                    "position": {
                        "amount": 100.0,
                        "average_entry": 1.0,
                        "recent_high": 1.25,
                    },
                    "average_entry": 1.0,
                    "recent_high": 1.25,
                    "strategy_params": _trail_params(),
                    "atr_pct": 3.0,
                }
            ]
        )
        with patch(
            "services.exit_realtime.hub.try_execute_trail_exit"
        ) as mock_ex, patch(
            "services.exit_realtime.hub._log_event"
        ):
            hub.on_ticker("H_USDT", 1.10)
        mock_ex.assert_not_called()


class TestWouldSellIntegration(unittest.TestCase):
    def test_ttp_would_fire_with_high_peak(self):
        pos = {
            "average_entry": 1.0,
            "recent_high": 1.20,
            "amount": 10,
            "trail_tp_steps": 0,
        }
        params = _trail_params()
        # gain 11% >= floor 8, peak 20% >= arm 12, drop from high 7.5% — need drop >= trail
        # at peak 20%, dynamic trail ~3.7%; price 1.11, high 1.20, drop = 7.5% > 3.7
        events = evaluate_would_sells(
            symbol="Z/USDT",
            timeframe="1h",
            price=1.11,
            position=pos,
            strategy_params=params,
            atr_pct=3.0,
            sources=frozenset({"trailing_take_profit"}),
        )
        self.assertTrue(any(e.get("source") == "trailing_take_profit" for e in events))


if __name__ == "__main__":
    unittest.main()
