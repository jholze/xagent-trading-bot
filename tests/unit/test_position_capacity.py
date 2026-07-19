"""Intelligent position capacity — pure resolver + risk gate."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.config import BotConfig
from core.models import TradeOrder
from data_manager import get_config
from risk.cash_policy import MODE_DEPLOY, MODE_HARVEST, MODE_STEADY
from risk.position_capacity import (
    count_open_book_memory_signals,
    format_capacity_reject_message,
    resolve_max_open_eff,
)
from risk.risk_manager import RiskManager


def _cap_cfg(**over) -> dict:
    base = {
        "enabled": True,
        "base": 24,
        "min_floor": 12,
        "max_ceiling": 36,
        "link_fusion_size_mult": True,
        "size_mult_slot_scale": 8,
        "cash_tight_threshold_usdt": 2000,
        "cash_tight_adj": -4,
        "cash_loose_threshold_usdt": 12000,
        "cash_loose_adj": 2,
        "risk_off_cap_to_base": True,
        "restart_warmup_min": 0,  # off unless test sets uptime
        "drawdown_adj": -3,
    }
    base.update(over)
    return {"position_capacity": base}


class TestResolveMaxOpenEff(unittest.TestCase):
    def test_disabled_returns_static_base(self):
        snap = resolve_max_open_eff(base=24, risk_config={})
        self.assertFalse(snap.enabled)
        self.assertEqual(snap.max_open_eff, 24)

    def test_risk_on_deploy_looser_than_risk_off_harvest(self):
        risk_on = resolve_max_open_eff(
            base=24,
            risk_config=_cap_cfg(),
            regime="RISK_ON",
            size_mult=1.1,
            cash_mode=MODE_DEPLOY,
            spendable_new=20_000,
            process_uptime_sec=3600,
        )
        risk_off = resolve_max_open_eff(
            base=24,
            risk_config=_cap_cfg(),
            regime="RISK_OFF",
            size_mult=0.5,
            cash_mode=MODE_HARVEST,
            spendable_new=1_500,
            process_uptime_sec=3600,
        )
        self.assertGreater(risk_on.max_open_eff, risk_off.max_open_eff)
        self.assertGreaterEqual(risk_on.max_open_eff, 24)
        self.assertLessEqual(risk_off.max_open_eff, 24)

    def test_crash_or_block_buys_hard_floor(self):
        snap = resolve_max_open_eff(
            base=24,
            risk_config=_cap_cfg(),
            regime="CRASH",
            size_mult=0.2,
            block_buys=True,
            cash_mode=MODE_HARVEST,
            process_uptime_sec=3600,
        )
        self.assertEqual(snap.max_open_eff, 12)
        self.assertIn("hard_floor_crash_or_block", snap.reason_codes)

    def test_risk_off_does_not_expand_above_base(self):
        # Even with loose cash / prefer, RISK_OFF should not exceed base
        snap = resolve_max_open_eff(
            base=24,
            risk_config=_cap_cfg(),
            regime="RISK_OFF",
            size_mult=1.2,
            cash_mode=MODE_DEPLOY,
            spendable_new=50_000,
            prefer_open=20,
            process_uptime_sec=3600,
        )
        self.assertLessEqual(snap.max_open_eff, 24)

    def test_clamped_to_ceiling(self):
        snap = resolve_max_open_eff(
            base=24,
            risk_config=_cap_cfg(max_ceiling=32),
            regime="RISK_ON",
            size_mult=1.5,
            cash_mode=MODE_DEPLOY,
            spendable_new=50_000,
            process_uptime_sec=3600,
        )
        self.assertLessEqual(snap.max_open_eff, 32)

    def test_warmup_tightens(self):
        warm = resolve_max_open_eff(
            base=24,
            risk_config=_cap_cfg(restart_warmup_min=15, restart_warmup_adj=-6),
            regime="NEUTRAL",
            cash_mode=MODE_STEADY,
            process_uptime_sec=60,  # 1 min < 15
        )
        cool = resolve_max_open_eff(
            base=24,
            risk_config=_cap_cfg(restart_warmup_min=15, restart_warmup_adj=-6),
            regime="NEUTRAL",
            cash_mode=MODE_STEADY,
            process_uptime_sec=3600,
        )
        self.assertLess(warm.max_open_eff, cool.max_open_eff)

    def test_memory_soft_blocks_tighten(self):
        clean = resolve_max_open_eff(
            base=24,
            risk_config=_cap_cfg(),
            regime="NEUTRAL",
            cash_mode=MODE_STEADY,
            soft_block_open=0,
            process_uptime_sec=3600,
        )
        dirty = resolve_max_open_eff(
            base=24,
            risk_config=_cap_cfg(),
            regime="NEUTRAL",
            cash_mode=MODE_STEADY,
            soft_block_open=10,
            toxic_open=6,
            process_uptime_sec=3600,
        )
        self.assertLess(dirty.max_open_eff, clean.max_open_eff)

    def test_tight_cash_tightens(self):
        rich = resolve_max_open_eff(
            base=24,
            risk_config=_cap_cfg(),
            regime="NEUTRAL",
            cash_mode=MODE_STEADY,
            spendable_new=20_000,
            process_uptime_sec=3600,
        )
        poor = resolve_max_open_eff(
            base=24,
            risk_config=_cap_cfg(),
            regime="NEUTRAL",
            cash_mode=MODE_STEADY,
            spendable_new=500,
            process_uptime_sec=3600,
        )
        self.assertLess(poor.max_open_eff, rich.max_open_eff)

    def test_free_slots_computed(self):
        snap = resolve_max_open_eff(
            base=24,
            risk_config=_cap_cfg(),
            regime="RISK_ON",
            cash_mode=MODE_DEPLOY,
            spendable_new=20_000,
            process_uptime_sec=3600,
            full_slots=22,
        )
        self.assertIsNotNone(snap.free_slots)
        self.assertEqual(snap.free_slots, max(0, snap.max_open_eff - 22))

    def test_reject_message_includes_eff(self):
        snap = resolve_max_open_eff(
            base=24,
            risk_config=_cap_cfg(),
            regime="RISK_OFF",
            cash_mode=MODE_HARVEST,
            process_uptime_sec=3600,
        )
        msg = format_capacity_reject_message(snap, 24)
        self.assertIn("eff", msg)
        self.assertIn(str(snap.max_open_eff), msg)


class TestOpenBookMemory(unittest.TestCase):
    def test_counts_soft_block_and_toxic(self):
        profiles = {
            "A/USDT": SimpleNamespace(entry_bias="soft_block", features={}),
            "B/USDT": SimpleNamespace(
                entry_bias="neutral", features={"structure_risk": True}
            ),
            "C/USDT": SimpleNamespace(entry_bias="prefer", features={}),
            "D/USDT": None,
        }
        positions = [{"symbol": s} for s in profiles]
        soft, toxic, prefer = count_open_book_memory_signals(
            positions, get_profile=profiles.get
        )
        self.assertEqual(soft, 1)
        self.assertEqual(toxic, 1)
        self.assertEqual(prefer, 1)


def _rm_cfg(**risk_over) -> BotConfig:
    raw = dict(get_config())
    raw["trading_mode"] = "paper"
    raw["initial_capital_usdt"] = 100_000
    raw["max_open_positions"] = 24
    raw["max_usdt_per_trade"] = 2500
    risk = raw.setdefault("risk", {})
    risk["cash_floor_pct"] = 18
    risk["min_trade_usdt"] = 100.0
    # Disable cash_policy noise for capacity gate tests unless overridden
    risk["cash_policy"] = {"enabled": False}
    risk["position_capacity"] = {
        "enabled": True,
        "base": 24,
        "min_floor": 12,
        "max_ceiling": 36,
        "restart_warmup_min": 0,
        "link_fusion_size_mult": True,
        "size_mult_slot_scale": 8,
        "cash_tight_threshold_usdt": 2000,
        "cash_tight_adj": -4,
        "cash_loose_threshold_usdt": 12000,
        "cash_loose_adj": 2,
    }
    risk.update(risk_over)
    return BotConfig(raw)


class TestRiskManagerCapacityGate(unittest.TestCase):
    def test_risk_on_allows_beyond_base_24(self):
        """With 24 full slots, RISK_ON+DEPLOY should not max_open-reject if eff > 24."""
        rm = RiskManager(_rm_cfg())
        order = TradeOrder(
            type="BUY",
            symbol="BANK/USDT",
            price=1.0,
            amount=0,
            usdt_amount=500,
            signal="BUY",
        )
        snap_mock = resolve_max_open_eff(
            base=24,
            risk_config=_cap_cfg(),
            regime="RISK_ON",
            size_mult=1.1,
            cash_mode=MODE_DEPLOY,
            spendable_new=20_000,
            process_uptime_sec=3600,
            full_slots=24,
        )
        self.assertGreater(snap_mock.max_open_eff, 24)

        _neutral_bias = {
            "block_buys": False,
            "size_mult": 1.1,
            "regime": "RISK_ON",
            "active": True,
        }
        with patch(
            "risk.risk_manager.count_open_full_slots", return_value=24
        ), patch(
            "risk.risk_manager.get_position",
            return_value={"amount": 0},
        ), patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")), patch.object(
            rm, "_resolve_position_capacity", return_value=snap_mock
        ), patch.object(rm, "_available_usdt", return_value=50_000.0), patch.object(
            rm, "_portfolio_equity", return_value=100_000.0
        ), patch.object(rm, "_initial_capital", return_value=100_000.0), patch.object(
            rm, "_equity_drawdown_pct", return_value=0.0
        ), patch.object(rm, "_daily_buy_limit_blocked", return_value=None), patch.object(
            rm, "_cash_floor_blocked", return_value=None
        ), patch.object(rm, "_spendable_usdt", return_value=50_000.0), patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value=_neutral_bias,
        ):
            decision = rm.evaluate(order, "4h", source="manual")
        self.assertNotEqual(decision.code, "max_open_positions")
        self.assertTrue(decision.approved)

    def test_blocks_when_over_eff(self):
        rm = RiskManager(_rm_cfg())
        order = TradeOrder(
            type="BUY",
            symbol="NEW/USDT",
            price=1.0,
            amount=0,
            usdt_amount=500,
            signal="BUY",
        )
        tight = resolve_max_open_eff(
            base=24,
            risk_config=_cap_cfg(),
            regime="RISK_OFF",
            size_mult=0.5,
            cash_mode=MODE_HARVEST,
            spendable_new=500,
            process_uptime_sec=3600,
            full_slots=20,
        )
        self.assertLessEqual(tight.max_open_eff, 20)
        with patch(
            "risk.risk_manager.count_open_full_slots", return_value=20
        ), patch(
            "risk.risk_manager.get_position",
            return_value={"amount": 0},
        ), patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")), patch.object(
            rm, "_resolve_position_capacity", return_value=tight
        ), patch.object(rm, "_available_usdt", return_value=50_000.0), patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value={"block_buys": False, "size_mult": 0.5, "regime": "RISK_OFF"},
        ):
            decision = rm.evaluate(order, "4h", source="auto")
        self.assertFalse(decision.approved)
        self.assertEqual(decision.code, "max_open_positions")
        self.assertIn("eff", decision.message)

    def test_status_summary_exposes_capacity(self):
        rm = RiskManager(_rm_cfg())
        with patch(
            "risk.risk_manager.count_open_positions", return_value=10
        ), patch(
            "risk.risk_manager.count_open_full_slots", return_value=8
        ), patch.object(rm, "_primary_history", return_value={}), patch.object(
            rm, "_portfolio_equity", return_value=100_000.0
        ), patch.object(rm, "_initial_capital", return_value=100_000.0), patch.object(
            rm, "_equity_drawdown_pct", return_value=0.0
        ), patch.object(rm, "_available_usdt", return_value=30_000.0), patch.object(
            rm, "_daily_trades_count", return_value=0
        ), patch.object(rm, "_daily_buys_count", return_value=0), patch.object(
            rm, "_daily_dca_buys_count", return_value=0
        ), patch.object(rm, "_daily_dca_usdt_sum", return_value=0.0), patch.object(
            rm, "_daily_sells_count", return_value=0
        ), patch.object(rm, "_market_bias_for_cash", return_value={
            "regime": "RISK_ON",
            "size_mult": 1.1,
            "block_buys": False,
        }), patch.object(rm, "_open_book_memory_counts", return_value=(0, 0, 0)), patch.object(
            rm, "_process_uptime_sec", return_value=3600.0
        ):
            st = rm.status_summary()
        self.assertTrue(st.get("position_capacity_enabled"))
        self.assertIn("max_open_eff", st)
        self.assertGreaterEqual(st["max_open_eff"], 24)
        self.assertEqual(st["open_full_slots"], 8)


if __name__ == "__main__":
    unittest.main()
