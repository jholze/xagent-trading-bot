"""DCA-aware stop-loss thresholds in technical_rsi_bb."""

from datetime import datetime, timedelta
from unittest import TestCase

from core.models import MarketContext
from strategies.dca import effective_stop_loss_thresholds
from strategies.technical_rsi_bb import TechnicalRSIStrategy


class EffectiveStopLossThresholdsTests(TestCase):
    def test_no_dca_uses_base_thresholds(self):
        full, partial, grace = effective_stop_loss_thresholds({}, {}, 50.0)
        self.assertEqual(full, 50.0)
        self.assertAlmostEqual(partial, 33.5)
        self.assertFalse(grace)

    def test_dca_rounds_widen_full_stop_and_pause_partial(self):
        pos = {"dca_rounds": 2, "last_dca_at": None}
        params = {
            "dca": {
                "interval_hours": 12,
                "stop_loss_widen_pct_per_round": 6,
                "pause_partial_stop_during_dca": True,
            }
        }
        full, partial, grace = effective_stop_loss_thresholds(pos, params, 50.0)
        self.assertEqual(full, 62.0)
        self.assertIsNone(partial)
        self.assertFalse(grace)

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
        # -38% loss would trigger partial at 33.5% without DCA pause
        result = self._analyze_with_position(
            entry=1.0,
            price=0.62,
            dca_rounds=1,
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

    def test_volatile_partial_stop_pct_override(self):
        full, partial, grace = effective_stop_loss_thresholds(
            {},
            {"partial_stop_pct": 25, "dca": {"pause_partial_stop_during_dca": False}},
            50.0,
        )
        self.assertEqual(full, 50.0)
        self.assertEqual(partial, 25.0)
        self.assertFalse(grace)