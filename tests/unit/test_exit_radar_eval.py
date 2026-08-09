"""Exit radar domain eval + policy alignment."""

from __future__ import annotations

import sys
import unittest
import unittest.mock
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services.exit_radar.eval import evaluate_position, resolve_trail_pct
from services.dca_sniper.policy import dd_band_ok, reclaim_allows_dca
from services.dca_sniper.checklist import analyze_candidate
from services.dca_sniper.engine import _size_for_row
from strategies.position_gates import (
    auto_exit_blocked,
    dca_add_blocked,
    filter_would_sources_for_hold,
)
from strategies.position_lock import apply_lock, build_lock, DEFAULT_MODES, MODE_NO_DCA


def _pos(**kw):
    base = {
        "symbol": "T/USDT",
        "timeframe": "1h",
        "entry": 1.0,
        "amount": 1000,
        "recent_high": 1.2,
        "ttp": {
            "enabled": True,
            "arm_gain_pct": 5,
            "min_gain_pct": 1,
            "trail_pct": 3,
            "trail_pct_min": 3,
            "trail_pct_max": 12,
            "trail_pct_scale_start_pct": 18,
            "trail_pct_scale_peak_pct": 45,
            "dynamic_trail": True,
        },
        "trailing_stop": {
            "enabled": True,
            "activation_gain_pct": 2,
            "min_trail_pct": 5,
            "max_trail_pct": 20,
            "atr_multiplier": 2,
        },
        "life": {
            "enabled": True,
            "arm_gain_pct": 1,
            "max_hours": 96,
            "min_gain_pct": 0.5,
        },
        "stop_loss_pct": 50,
        "partial_stop_pct": 25,
        "prefer_full_close": True,
        "first_buy_at": "2026-01-01T00:00:00",
    }
    base.update(kw)
    return base


class TestExitRadarEval(unittest.TestCase):
    def test_hold_blocks_trail_not_hard_sl(self):
        pos = _pos(recovery_hold=True, sniper_focus=True, peak_epoch_high=1.05)
        r = evaluate_position(pos, 1.01, atr_pct_est=5.0)
        self.assertEqual(r["status"], "recovery_hold")
        self.assertFalse(r["ttp"]["would"])
        r2 = evaluate_position(pos, 0.4, atr_pct_est=5.0)
        self.assertTrue(r2["would_exit"])
        self.assertIn("stop_loss", r2["would_sources"])

    def test_trail_pct_dynamic(self):
        ttp = _pos()["ttp"]
        self.assertLess(resolve_trail_pct(10, ttp), resolve_trail_pct(50, ttp))


class TestDdPolicyAlignment(unittest.TestCase):
    def test_band_shared(self):
        cfg = {"min_dd_pct_for_dca": 12, "max_dd_pct_for_dca": 55}
        ok, _ = dd_band_ok(-30, cfg)
        self.assertTrue(ok)
        ok2, why = dd_band_ok(-60, cfg)
        self.assertFalse(ok2)
        self.assertEqual(why, "loss_too_deep")
        ok3, why3 = dd_band_ok(-5, cfg)
        self.assertFalse(ok3)
        self.assertEqual(why3, "loss_too_shallow")

    def test_checklist_and_sizer_agree_on_deep_bag(self):
        cfg = {"min_dd_pct_for_dca": 12, "max_dd_pct_for_dca": 55}
        cand = {
            "loss_pct": -52,
            "dca_rounds": 2,
            "max_rounds": 4,
            "notional": 2000,
            "sniper_cfg": cfg,
            "spendable_dca": 1500,
            "reclaim_ok": None,
            "free_fall": None,
        }
        a = analyze_candidate(cand, {"spendable_dca": 1500})
        # within 55% band → position layer can pass (was hard-fail at -40)
        self.assertNotIn(
            "position",
            [x.split(":")[0] for x in (a.get("hard_fail") or []) if x.startswith("position")]
            if a.get("hard_fail")
            else [],
        )
        # if position fails for rounds/notional only - check band itself
        ok, _ = dd_band_ok(-52, cfg)
        self.assertTrue(ok)
        usdt, reason = _size_for_row(
            cand,
            a,
            {"spendable_dca": 1500, "equity": 100000},
            {**cfg, "small_dca_usdt": 500, "min_meaningful_usdt": 200, "max_single_add_usdt": 2500,
             "heavy_min_score": 6.5, "prefer_small_before_heavy": True, "heavy_only_on_reclaim": True,
             "require_reclaim_for_dca": True, "profile_f": {"default": 0.65}, "max_bag_pct_equity": 5},
        )
        self.assertGreater(usdt, 0)
        self.assertIn(reason, ("DCA_SMALL", "DCA_HEAVY"))

    def test_reclaim_false_blocks(self):
        ok, why = reclaim_allows_dca(reclaim_ok=False, free_fall=None, require_reclaim=True)
        self.assertFalse(ok)
        self.assertEqual(why, "no_reclaim")


class TestPositionGates(unittest.TestCase):
    def test_default_lock_allows_dca(self):
        pos = {"amount": 1}
        apply_lock(pos, build_lock(modes=DEFAULT_MODES, reason="test"))
        blocked, _ = dca_add_blocked(pos)
        self.assertFalse(blocked)

    def test_explicit_no_dca(self):
        pos = {"amount": 1}
        apply_lock(pos, build_lock(modes=[MODE_NO_DCA], reason="test"))
        blocked, msg = dca_add_blocked(pos)
        self.assertTrue(blocked)
        self.assertIn("no_dca", msg)

    def test_legacy_triple_allows_dca(self):
        pos = {"amount": 1}
        apply_lock(
            pos,
            build_lock(
                modes=["no_auto_sell", "no_dca", "no_evict"],
                reason="telegram",
            ),
        )
        blocked, _ = dca_add_blocked(pos)
        self.assertFalse(blocked)

    def test_hold_blocks_auto_exit(self):
        pos = {"recovery_hold": True, "sniper_focus": True, "amount": 1}
        blocked, why = auto_exit_blocked(pos, "exit_ws")
        self.assertTrue(blocked)
        self.assertEqual(why, "recovery_hold")
        # hard SL source allowed past hold gate (lock may still apply)
        blocked2, _ = auto_exit_blocked(pos, "stop_loss")
        self.assertFalse(blocked2)

    def test_filter_would(self):
        allowed, blocked = filter_would_sources_for_hold(
            ["trailing_take_profit", "stop_loss"], recovery_hold=True
        )
        self.assertEqual(allowed, ["stop_loss"])
        self.assertIn("trailing_take_profit", blocked)


class TestLocalClientSurface(unittest.TestCase):
    def test_local_client_methods(self):
        from services.dca_sniper.local_client import LocalBotClient

        c = LocalBotClient()
        for m in ("cash", "candidates", "status", "execute", "fund_sell", "promote"):
            self.assertTrue(callable(getattr(c, m)))


class TestInprocessThin(unittest.TestCase):
    def test_inprocess_skips_when_flag_off(self):
        from services.dca_sniper import inprocess

        with unittest.mock.patch(
            "services.dca_sniper.inprocess.dca_sniper_enabled", return_value=True
        ), unittest.mock.patch(
            "services.dca_sniper.inprocess.dca_sniper_config",
            return_value={"in_process_tick": False, "poll_interval_sec": 1},
        ):
            self.assertIsNone(inprocess.maybe_tick_dca_sniper(force=False))


if __name__ == "__main__":
    import unittest.mock  # noqa: F401

    unittest.main()
