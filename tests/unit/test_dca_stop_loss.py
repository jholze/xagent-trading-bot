"""DCA-aware stop-loss thresholds in technical_rsi_bb."""

from datetime import datetime, timedelta
from unittest import TestCase

from core.models import MarketContext
from strategies.dca import effective_stop_loss_thresholds, should_pause_partial_stop
from strategies.technical_rsi_bb import TechnicalRSIStrategy


class EffectiveStopLossThresholdsTests(TestCase):
    def test_no_dca_uses_base_thresholds(self):
        full, partial, grace = effective_stop_loss_thresholds({}, {}, 50.0)
        self.assertEqual(full, 50.0)
        self.assertAlmostEqual(partial, 33.5)
        self.assertFalse(grace)

    def test_dca_rounds_widen_full_stop_and_pause_partial(self):
        # Full stop still widens per round. The partial is paused only inside
        # the post-DCA grace window (#695), not for the life of the lot.
        recent = (datetime.now() - timedelta(hours=1)).isoformat()
        params = {
            "dca": {
                "interval_hours": 12,
                "stop_loss_widen_pct_per_round": 6,
                "pause_partial_stop_during_dca": True,
            }
        }
        full, partial, grace = effective_stop_loss_thresholds(
            {"dca_rounds": 2, "last_dca_at": recent}, params, 50.0
        )
        self.assertEqual(full, 62.0)
        self.assertIsNone(partial)
        self.assertTrue(grace)

        full_old, partial_old, grace_old = effective_stop_loss_thresholds(
            {"dca_rounds": 2, "last_dca_at": "2026-08-09T16:42:32"}, params, 50.0
        )
        self.assertEqual(full_old, 62.0)
        self.assertAlmostEqual(partial_old, 33.5)
        self.assertFalse(grace_old)

    def test_grace_period_blocks_stop_after_recent_dca(self):
        recent = (datetime.now() - timedelta(hours=3)).isoformat()
        pos = {"dca_rounds": 1, "last_dca_at": recent}
        params = {"dca": {"interval_hours": 12, "grace_hours_after_dca": 12}}
        _, _, grace = effective_stop_loss_thresholds(pos, params, 50.0)
        self.assertTrue(grace)


class TechnicalDcaStopLossTests(TestCase):
    def _analyze_with_position(
        self,
        *,
        entry: float,
        price: float,
        dca_rounds: int = 0,
        last_dca_at: str | None = None,
        dca_cfg: dict | None = None,
    ):
        strategy = TechnicalRSIStrategy()
        sim_state = {
            "last_rsi": 45.0,
            "dca_rounds": dca_rounds,
            "last_dca_at": last_dca_at,
        }
        params = {"stop_loss_pct": 50.0}
        if dca_cfg:
            params["dca"] = dca_cfg
        market = MarketContext(
            symbol="SKYAI/USDT",
            timeframe="4h",
            current_price=price,
            rsi=29.0,
            lower_bb=price * 0.9,
            vol_multiplier=1.0,
            has_position=True,
            open_positions=5,
            average_entry=entry,
            sim_state=sim_state,
            strategy_params=params,
        )
        return strategy.analyze({"symbol": "SKYAI/USDT", "timeframe": "4h"}, market)

    def test_partial_stop_suppressed_during_dca(self):
        # -38% would be a partial. It holds only while the grace window is
        # open. A missing fill time is not a permanent pause (#695).
        recent = (datetime.now() - timedelta(hours=2)).isoformat()
        result = self._analyze_with_position(
            entry=1.0,
            price=0.62,
            dca_rounds=1,
            last_dca_at=recent,
            dca_cfg={
                "interval_hours": 12,
                "stop_loss_widen_pct_per_round": 6,
                "pause_partial_stop_during_dca": True,
                "grace_hours_after_dca": 0,
            },
        )
        self.assertEqual(result.action, "HOLD")

    def test_grace_period_holds_after_dca(self):
        recent = (datetime.now() - timedelta(hours=2)).isoformat()
        result = self._analyze_with_position(
            entry=1.0,
            price=0.55,
            dca_rounds=1,
            last_dca_at=recent,
            dca_cfg={
                "interval_hours": 12,
                "grace_hours_after_dca": 12,
                "stop_loss_widen_pct_per_round": 6,
                "pause_partial_stop_during_dca": True,
            },
        )
        self.assertEqual(result.action, "HOLD")

    def test_full_stop_still_triggers_beyond_widened_threshold(self):
        result = self._analyze_with_position(
            entry=1.0,
            price=0.35,
            dca_rounds=1,
            dca_cfg={
                "interval_hours": 12,
                "grace_hours_after_dca": 0,
                "stop_loss_widen_pct_per_round": 6,
                "pause_partial_stop_during_dca": True,
            },
        )
        self.assertEqual(result.action, "SELL_STOP_FULL")

    def test_pause_while_dca_rounds_remain_even_if_flag_off(self):
        # LAB 14.08 paused while rounds remained even with the flag off.
        # #695: flag off means the partial is not paused. Remaining rounds
        # are not a pause; dca_guard_below_avg blocks those adds anyway.
        pos = {"dca_rounds": 1, "dca_max_rounds": 2, "last_dca_at": "2026-08-09T16:42:32"}
        self.assertFalse(
            should_pause_partial_stop(
                pos, {"dca": {"pause_partial_stop_during_dca": False, "max_rounds": 2}}
            )
        )
        done = {"dca_rounds": 2, "dca_max_rounds": 2}
        self.assertFalse(
            should_pause_partial_stop(
                done, {"dca": {"pause_partial_stop_during_dca": False, "max_rounds": 2}}
            )
        )

    def test_last_dca_at_counts_as_open_dca_without_rounds_field(self):
        # A fill time with no rounds field still counts as DCA, but only
        # inside the grace window. An old fill does not keep the partial off.
        recent = (datetime.now() - timedelta(hours=1)).isoformat()
        paused = {"last_dca_at": recent}
        self.assertTrue(should_pause_partial_stop(paused, {"dca": {}}))
        _, partial_paused, _ = effective_stop_loss_thresholds(
            paused, {"partial_stop_pct": 25}, 50.0
        )
        self.assertIsNone(partial_paused)

        old = {"last_dca_at": "2026-08-09T16:42:32"}
        self.assertFalse(should_pause_partial_stop(old, {"dca": {}}))
        full, partial, _ = effective_stop_loss_thresholds(
            old, {"partial_stop_pct": 25}, 50.0
        )
        self.assertEqual(partial, 25)
        self.assertGreaterEqual(full, 50.0)

    def test_lab_style_minus_35_holds_when_dca_round_open(self):
        # LAB 14.08 held a -35% lot while a DCA round was still open.
        # #695 replaces that with the grace window: this fill is months old,
        # so the partial fires. A fresh DCA fill still holds.
        dca_cfg = {
            "max_rounds": 2,
            "pause_partial_stop_during_dca": True,
            "stop_loss_widen_pct_per_round": 5,
            "grace_hours_after_dca": 0,
        }
        stale = self._analyze_with_position(
            entry=0.1319,
            price=0.0848,
            dca_rounds=1,
            last_dca_at="2026-08-09T16:42:32",
            dca_cfg=dca_cfg,
        )
        self.assertEqual(stale.action, "SELL_STOP_PARTIAL")
        recent = (datetime.now() - timedelta(hours=2)).isoformat()
        fresh = self._analyze_with_position(
            entry=0.1319,
            price=0.0848,
            dca_rounds=1,
            last_dca_at=recent,
            dca_cfg=dca_cfg,
        )
        self.assertEqual(fresh.action, "HOLD")

    def test_volatile_partial_stop_pct_override(self):
        full, partial, grace = effective_stop_loss_thresholds(
            {},
            {"partial_stop_pct": 25, "dca": {"pause_partial_stop_during_dca": False}},
            50.0,
        )
        self.assertEqual(full, 50.0)
        self.assertEqual(partial, 25.0)
        self.assertFalse(grace)