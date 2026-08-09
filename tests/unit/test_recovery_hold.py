"""#223 recovery_hold — block auto sells, peak epoch, BE+ promote, integration."""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_spec = importlib.util.spec_from_file_location(
    "recovery_hold",
    _ROOT / "strategies" / "recovery_hold.py",
)
rh = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(rh)


class TestRecoveryHoldCore(unittest.TestCase):
    def test_active_flags(self):
        self.assertTrue(rh.is_recovery_hold_active({"recovery_hold": True}))
        self.assertTrue(rh.is_recovery_hold_active({"sniper_focus": True}))
        self.assertFalse(rh.is_recovery_hold_active({}))

    def test_enforce_kill_via_env(self):
        pos = {"recovery_hold": True}
        with patch.dict(os.environ, {"RECOVERY_HOLD_ENFORCE": "0"}):
            cfg = rh.recovery_hold_config()
            self.assertFalse(cfg["enforce"])
            self.assertFalse(rh.is_recovery_hold_active(pos))
            self.assertIsNone(rh.auto_sells_blocked_reason(pos, "trailing_stop"))

    def test_sources_blocked_and_allowed(self):
        self.assertFalse(rh.source_allowed_under_recovery_hold("trailing_stop"))
        self.assertFalse(rh.source_allowed_under_recovery_hold("trailing_take_profit"))
        self.assertFalse(rh.source_allowed_under_recovery_hold("bb_upper"))
        self.assertFalse(rh.source_allowed_under_recovery_hold("cmc"))
        self.assertFalse(rh.source_allowed_under_recovery_hold("technical"))
        self.assertFalse(rh.source_allowed_under_recovery_hold("partial_stop"))
        self.assertFalse(rh.source_allowed_under_recovery_hold("unknown_xyz"))
        self.assertTrue(rh.source_allowed_under_recovery_hold("stop_loss"))
        self.assertTrue(rh.source_allowed_under_recovery_hold("hard_stop"))
        self.assertTrue(rh.source_allowed_under_recovery_hold("manual"))
        self.assertTrue(rh.source_allowed_under_recovery_hold("x_stop_loss"))

    def test_filter_keeps_hard_sl_only(self):
        pos = {"recovery_hold": True, "average_entry": 1.0}
        cands = [
            ("SELL_FULL", 5, "trailing_stop"),
            ("SELL_PARTIAL_30", 3, "bb_upper"),
            ("SELL_FULL", 7, "stop_loss"),
            ("SELL_PARTIAL_50", 7, "partial_stop"),
            ("SELL_FULL", 4, "technical"),
        ]
        kept, blocked = rh.filter_sell_candidates_for_recovery_hold(cands, pos)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0][2], "stop_loss")
        self.assertIn("trailing_stop", blocked)
        self.assertIn("technical", blocked)
        self.assertIn("partial_stop", blocked)

    def test_filter_noop_without_hold(self):
        cands = [("SELL_FULL", 5, "trailing_stop")]
        kept, blocked = rh.filter_sell_candidates_for_recovery_hold(cands, {})
        self.assertEqual(len(kept), 1)
        self.assertEqual(blocked, [])

    def test_be_plus_promote(self):
        pos = {
            "recovery_hold": True,
            "sniper_focus": True,
            "average_entry": 1.0,
        }
        self.assertFalse(rh.maybe_promote_recovery_hold(pos, 1.01))
        self.assertTrue(pos["recovery_hold"])
        self.assertTrue(rh.maybe_promote_recovery_hold(pos, 1.025))
        self.assertFalse(pos["recovery_hold"])
        self.assertFalse(pos["sniper_focus"])
        self.assertEqual(pos.get("recovery_hold_clear_reason"), "be_plus")

    def test_be_plus_exact_threshold(self):
        pos = {"recovery_hold": True, "average_entry": 100.0}
        # exactly +2%
        self.assertTrue(rh.maybe_promote_recovery_hold(pos, 102.0))

    def test_stamp_peak_epoch_clamps_stale_high(self):
        pos = {"average_entry": 2.20, "recent_high": 2.80}
        epoch = rh.stamp_peak_epoch_on_dca(pos, 2.18)
        self.assertAlmostEqual(epoch, 2.20)
        self.assertAlmostEqual(pos["peak_epoch_high"], 2.20)
        # Must clamp pre-DCA peak down (BEAT-class)
        self.assertAlmostEqual(pos["recent_high"], 2.20)

    def test_stamp_peak_epoch_fill_above_avg(self):
        pos = {"average_entry": 1.0, "recent_high": 0.9}
        epoch = rh.stamp_peak_epoch_on_dca(pos, 1.05)
        self.assertAlmostEqual(epoch, 1.05)
        self.assertAlmostEqual(pos["recent_high"], 1.05)

    def test_set_recovery_hold(self):
        pos = {}
        rh.set_recovery_hold(pos, sniper_focus=True, heavy=True)
        self.assertTrue(pos["recovery_hold"])
        self.assertTrue(pos["sniper_focus"])
        self.assertTrue(pos["dca_heavy_used"])

    def test_auto_sells_blocked_reason(self):
        pos = {"recovery_hold": True}
        self.assertIsNotNone(rh.auto_sells_blocked_reason(pos, "trailing_stop"))
        self.assertIsNone(rh.auto_sells_blocked_reason(pos, "stop_loss"))

    def test_any_hold_flags(self):
        self.assertEqual(rh.any_hold_flags([{}, {"recovery_hold": True}, {"sniper_focus": 1}]), 2)


class TestRecoveryHoldTrailIntegration(unittest.TestCase):
    """Uses real trailing_stop if importable (needs project deps)."""

    @classmethod
    def setUpClass(cls):
        cls.skip_reason = None
        try:
            from core.models import MarketContext  # noqa: F401
            from strategies.trailing_stop import evaluate_trailing_stop  # noqa: F401
            from strategies.dca import trail_exits_paused_after_dca  # noqa: F401
        except Exception as e:
            cls.skip_reason = str(e)

    def setUp(self):
        if self.skip_reason:
            self.skipTest(f"deps unavailable: {self.skip_reason}")

    def _mkt(self, price: float, entry: float):
        from core.models import MarketContext

        return MarketContext(
            symbol="HOLD/USDT",
            timeframe="1h",
            current_price=price,
            has_position=True,
            average_entry=entry,
            atr_pct=5.0,
            strategy_params={},
        )

    def test_hold_blocks_trailing_stop_after_grace(self):
        """Even after DCA grace expired, recovery_hold must block trail.

        Price must stay below BE+ promote (+2%) so hold remains active.
        Peak is high enough that trail would fire if hold did not block.
        """
        from strategies.trailing_stop import evaluate_trailing_stop

        now = datetime(2026, 8, 9, 12, 0, 0)
        entry = 1.0
        pos = {
            "average_entry": entry,
            "recent_high": 1.25,
            "peak_epoch_high": 1.0,
            "dca_rounds": 1,
            "last_dca_at": (now - timedelta(hours=48)).isoformat(),
            "recovery_hold": True,
            "sniper_focus": True,
        }
        params = {
            "trailing_stop": {
                "enabled": True,
                "activation_gain_pct": 5.0,
                "min_trail_pct": 5.0,
                "max_trail_pct": 25.0,
                "atr_multiplier": 1.0,
                "floor_at_entry": True,
                "arm_on_peak": True,
            },
            "dca": {
                "pause_trail_exits_after_dca": True,
                "trail_grace_hours_after_dca": 12,
            },
        }
        # +1% vs avg → still under BE+ 2%; peak drop would fire trail without hold
        cand = evaluate_trailing_stop(self._mkt(1.01, entry), pos, params, now=now)
        self.assertIsNone(cand)
        self.assertTrue(pos.get("recovery_hold"))

    def test_hold_clears_on_be_plus_then_trail_can_eval(self):
        from strategies.trailing_stop import evaluate_trailing_stop

        now = datetime(2026, 8, 9, 12, 0, 0)
        entry = 1.0
        pos = {
            "average_entry": entry,
            "recent_high": 1.20,
            "peak_epoch_high": 1.02,
            "dca_rounds": 1,
            "last_dca_at": (now - timedelta(hours=48)).isoformat(),
            "recovery_hold": True,
            "sniper_focus": True,
        }
        params = {
            "trailing_stop": {
                "enabled": True,
                "activation_gain_pct": 5.0,
                "min_trail_pct": 5.0,
                "max_trail_pct": 25.0,
                "atr_multiplier": 1.0,
                "floor_at_entry": True,
                "arm_on_peak": True,
            },
            "dca": {
                "pause_trail_exits_after_dca": True,
                "trail_grace_hours_after_dca": 12,
            },
        }
        # Price +3% over avg → promote clears hold inside evaluate
        market = self._mkt(1.03, entry)
        evaluate_trailing_stop(market, pos, params, now=now)
        self.assertFalse(pos.get("recovery_hold"))
        self.assertFalse(pos.get("sniper_focus"))


class TestRecoveryHoldExecuteGate(unittest.TestCase):
    def setUp(self):
        try:
            import services.exit_realtime.execute as ex  # noqa: F401
        except Exception as e:
            self.skipTest(f"deps: {e}")

    def test_execute_blocks_trail_under_hold(self):
        from services.exit_realtime import execute as ex
        from strategies.positions import get_key, positions

        sym, tf = "RHTEST/USDT", "1h"
        key = get_key(sym, tf)
        backup = dict(positions)
        positions.clear()
        try:
            positions[key] = {
                "symbol": sym,
                "timeframe": tf,
                "amount": 10.0,
                "average_entry": 1.0,
                "recovery_hold": True,
                "sniper_focus": True,
            }
            with patch.object(ex, "exit_execute_url", create=True):
                # force_local path after hold check
                pass
            result = ex.try_execute_trail_exit(
                symbol=sym,
                timeframe=tf,
                price=0.95,
                action="SELL_FULL",
                exit_source="trailing_stop",
                force_local=True,
            )
            self.assertFalse(result.get("executed"))
            self.assertIn("recovery_hold", str(result.get("message") or ""))
        finally:
            positions.clear()
            positions.update(backup)


if __name__ == "__main__":
    unittest.main()
