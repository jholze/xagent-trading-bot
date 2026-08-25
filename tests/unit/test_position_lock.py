"""Position lock — block auto-sell / DCA / eviction; manual sell allowed."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from strategies.position_lock import (
    DEFAULT_MODES,
    MODE_NO_AUTO_SELL,
    MODE_NO_DCA,
    MODE_NO_EVICT,
    MODE_NO_MANUAL_SELL,
    auto_sell_blocked,
    build_lock,
    dca_blocked,
    eviction_blocked,
    is_manual_source,
    is_position_locked,
    lock_is_active,
    parse_duration_to_until,
    position_locks_enabled,
)


def _locked_pos(**kwargs):
    lock = build_lock(**kwargs) if "lock" not in kwargs else kwargs.pop("lock")
    pos = {
        "amount": Decimal("100"),
        "average_entry": 1.0,
        "lock": lock,
    }
    pos.update(kwargs)
    return pos


class TestPositionLockCore(unittest.TestCase):
    def test_enabled_by_default(self):
        self.assertTrue(position_locks_enabled({}))
        self.assertTrue(position_locks_enabled({"risk": {}}))
        self.assertFalse(
            position_locks_enabled({"risk": {"position_locks": {"enabled": False}}})
        )

    def test_default_modes_block_auto_not_manual(self):
        pos = _locked_pos(reason="hold", locked_by="test")
        blocked, msg = auto_sell_blocked(pos, "exit_ws")
        self.assertTrue(blocked)
        self.assertIn("position_locked", msg)
        blocked_m, _ = auto_sell_blocked(pos, "manual")
        self.assertFalse(blocked_m)
        blocked_t, _ = auto_sell_blocked(pos, "telegram")
        self.assertFalse(blocked_t)

    def test_no_manual_sell_mode(self):
        pos = _locked_pos(
            modes=[MODE_NO_AUTO_SELL, MODE_NO_MANUAL_SELL],
            reason="hard",
        )
        blocked, msg = auto_sell_blocked(pos, "manual")
        self.assertTrue(blocked)
        self.assertIn(MODE_NO_MANUAL_SELL, msg)

    def test_dca_and_eviction(self):
        # Default lock = sell-hold only: DCA allowed, eviction blocked
        pos = _locked_pos()
        self.assertFalse(dca_blocked(pos)[0])
        self.assertTrue(eviction_blocked(pos)[0])
        unlocked = {"amount": Decimal("1")}
        self.assertFalse(dca_blocked(unlocked)[0])
        self.assertFalse(eviction_blocked(unlocked)[0])
        # Explicit no_dca still blocks DCA (not the exact legacy triple)
        pos_nd = _locked_pos(modes=[MODE_NO_DCA])
        self.assertTrue(dca_blocked(pos_nd)[0])
        pos_both = _locked_pos(modes=[MODE_NO_AUTO_SELL, MODE_NO_DCA])
        self.assertTrue(dca_blocked(pos_both)[0])

    def test_ops_triple_with_no_dca_blocks_dca(self):
        """Explicit modes including no_dca are honored (BLESS ops lock / sniper)."""
        pos = _locked_pos(
            modes=[MODE_NO_AUTO_SELL, MODE_NO_DCA, MODE_NO_EVICT],
            reason="hold_after_revert",
        )
        self.assertTrue(auto_sell_blocked(pos, "exit_ws")[0])
        self.assertTrue(dca_blocked(pos)[0])
        self.assertTrue(eviction_blocked(pos)[0])

    def test_default_telegram_modes_allow_dca(self):
        """Telegram DEFAULT_MODES = sell-only; DCA/sniper still allowed."""
        pos = _locked_pos(modes=list(DEFAULT_MODES), reason="telegram_lock")
        self.assertTrue(auto_sell_blocked(pos, "exit_ws")[0])
        self.assertFalse(dca_blocked(pos)[0])
        self.assertTrue(eviction_blocked(pos)[0])

    def test_until_expiry(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        pos = _locked_pos(until=past)
        self.assertFalse(is_position_locked(pos))
        self.assertFalse(auto_sell_blocked(pos, "exit_ws")[0])

        future = datetime.now(timezone.utc) + timedelta(hours=2)
        pos2 = _locked_pos(until=future)
        self.assertTrue(is_position_locked(pos2, mode=MODE_NO_AUTO_SELL))

    def test_parse_duration(self):
        now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
        self.assertIsNone(parse_duration_to_until("permanent", now=now))
        self.assertIsNone(parse_duration_to_until("forever", now=now))
        h = parse_duration_to_until("24h", now=now)
        self.assertEqual(h, now + timedelta(hours=24))
        d = parse_duration_to_until("7d", now=now)
        self.assertEqual(d, now + timedelta(days=7))
        m = parse_duration_to_until("30m", now=now)
        self.assertEqual(m, now + timedelta(minutes=30))

    def test_manual_source_detect(self):
        self.assertTrue(is_manual_source("manual"))
        self.assertTrue(is_manual_source("telegram"))
        self.assertTrue(is_manual_source("manual_sell"))
        self.assertFalse(is_manual_source("exit_ws"))
        self.assertFalse(is_manual_source("trailing_take_profit"))
        self.assertFalse(is_manual_source("auto"))

    def test_kill_switch_disables_all(self):
        pos = _locked_pos()
        cfg = {"risk": {"position_locks": {"enabled": False}}}
        self.assertFalse(auto_sell_blocked(pos, "exit_ws", config=cfg)[0])
        self.assertFalse(dca_blocked(pos, config=cfg)[0])
        self.assertFalse(eviction_blocked(pos, config=cfg)[0])

    def test_partial_modes(self):
        pos = _locked_pos(modes=[MODE_NO_AUTO_SELL])
        self.assertTrue(auto_sell_blocked(pos, "exit_ws")[0])
        self.assertFalse(dca_blocked(pos)[0])
        self.assertFalse(eviction_blocked(pos)[0])

    def test_build_lock_shape(self):
        lock = build_lock(reason="ops", locked_by="cli")
        self.assertTrue(lock["enabled"])
        self.assertEqual(set(lock["modes"]), set(DEFAULT_MODES))
        self.assertEqual(lock["reason"], "ops")
        self.assertIsNone(lock["until"])
        self.assertTrue(lock_is_active(lock))


class TestRiskManagerPositionLock(unittest.TestCase):
    def test_risk_rejects_auto_sell(self):
        from core.models import TradeOrder
        from risk.risk_manager import RiskManager

        cfg = MagicMock()
        cfg.raw = {"risk": {"position_locks": {"enabled": True}}}
        cfg.risk_config = {}
        for attr in (
            "max_usdt_per_trade",
            "max_open_positions",
            "trade_cooldown_hours",
        ):
            setattr(cfg, attr, 100)

        rm = RiskManager(cfg)
        order = TradeOrder(
            type="SELL",
            symbol="BLESS/USDT",
            amount=100.0,
            price=0.03,
            source="exit_ws",
        )
        locked = _locked_pos(reason="hold_bless")
        with patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")), patch(
            "risk.risk_manager.get_position", return_value=locked
        ), patch("strategies.position_lock.log_lock_block"):
            decision = rm.evaluate(order, timeframe="1h", source="exit_ws")
        self.assertFalse(decision.approved)
        self.assertEqual(getattr(decision, "code", None) or "", "position_locked")

    def test_risk_allows_manual_sell(self):
        from core.models import TradeOrder
        from risk.risk_manager import RiskManager

        cfg = MagicMock()
        cfg.raw = {"risk": {"position_locks": {"enabled": True}}}
        cfg.risk_config = {}
        for attr in (
            "max_usdt_per_trade",
            "max_open_positions",
            "trade_cooldown_hours",
        ):
            setattr(cfg, attr, 100)

        rm = RiskManager(cfg)
        order = TradeOrder(
            type="SELL",
            symbol="BLESS/USDT",
            amount=50.0,
            price=0.03,
            source="manual",
        )
        locked = _locked_pos(reason="hold_bless")
        with patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")), patch(
            "risk.risk_manager.get_position", return_value=locked
        ), patch.object(
            rm, "_resolve_sell_order", return_value=order
        ), patch.object(
            rm, "_partial_sell_blocked", return_value=(False, "")
        ), patch.object(
            rm, "_effective_max_daily_sells", return_value=0
        ):
            decision = rm.evaluate(order, timeframe="1h", source="manual")
        self.assertTrue(decision.approved)
        self.assertNotEqual(getattr(decision, "code", None), "position_locked")


class TestExitExecuteLock(unittest.TestCase):
    def test_try_execute_trail_exit_blocked(self):
        from services.exit_realtime.execute import try_execute_trail_exit

        locked = _locked_pos(reason="hold")
        with patch(
            "strategies.positions.get_position", return_value=locked
        ), patch(
            "strategies.positions.is_open_position", return_value=True
        ), patch(
            "strategies.position_lock.log_lock_block"
        ), patch(
            "services.exit_realtime.config.exit_execute_url", return_value=""
        ):
            # clear inflight
            import services.exit_realtime.execute as ex

            with ex._inflight_lock:
                ex._inflight.discard("BLESS/USDT")
                ex._last_exit_at.pop("BLESS/USDT", None)
            out = try_execute_trail_exit(
                symbol="BLESS/USDT",
                timeframe="1h",
                price=0.03,
                action="SELL_FULL",
                exit_source="trailing_take_profit",
                force_local=True,
            )
        self.assertFalse(out.get("executed"))
        self.assertEqual(out.get("code"), "position_locked")


class TestDcaLock(unittest.TestCase):
    def test_should_dca_blocked(self):
        from strategies.dca import should_dca

        pos = _locked_pos(
            modes=[MODE_NO_AUTO_SELL, MODE_NO_DCA, MODE_NO_EVICT],
            reason="hold_after_revert",
        )
        market = MagicMock()
        market.current_price = 0.9
        market.average_entry = 1.0
        market.has_position = True
        dec = should_dca(market, pos, {})
        self.assertFalse(dec.should_dca)
        self.assertIn("position_locked", dec.blocked_reason or "")


class TestSerializeLock(unittest.TestCase):
    def test_deserialize_keeps_lock(self):
        from strategies.positions import _deserialize_position

        raw = {
            "amount": 10,
            "average_entry": 1.0,
            "lock": {
                "enabled": True,
                "modes": list(DEFAULT_MODES),
                "reason": "test",
                "locked_by": "unit",
                "locked_at": "2026-08-09T00:00:00+00:00",
                "until": None,
            },
        }
        pos = _deserialize_position(raw)
        self.assertIsInstance(pos.get("lock"), dict)
        self.assertEqual(pos["lock"]["reason"], "test")
        self.assertTrue(is_position_locked(pos))

    def test_preserve_does_not_restore_lock_after_explicit_unlock(self):
        from strategies.positions import _preserve_locks_from_existing_doc

        old_lock = {
            "enabled": True,
            "modes": list(DEFAULT_MODES),
            "reason": "ops restore H after grind trail BE dump 2026-08-21",
        }
        existing = {"positions": {"H_USDT_4h": {"amount": 1, "lock": old_lock}}}
        # Telegram /unlock pops-or-tombstones; serialize used to omit the key.
        unlocked_omit = {"positions": {"H_USDT_4h": {"amount": 1}}}
        out = _preserve_locks_from_existing_doc(unlocked_omit, existing)
        self.assertEqual(out["positions"]["H_USDT_4h"].get("lock"), old_lock)

        tombstone = {"enabled": False, "cleared_by": "unlock"}
        unlocked = {"positions": {"H_USDT_4h": {"amount": 1, "lock": tombstone}}}
        out2 = _preserve_locks_from_existing_doc(unlocked, existing)
        self.assertEqual(out2["positions"]["H_USDT_4h"]["lock"], tombstone)
        self.assertFalse(is_position_locked(out2["positions"]["H_USDT_4h"]))

    def test_deserialize_keeps_zero_entry_vol_ratio(self):
        from strategies.positions import _deserialize_position

        pos = _deserialize_position({"amount": 1, "average_entry": 1.0, "entry_15m_vol_ratio": 0.0})
        self.assertEqual(pos["entry_15m_vol_ratio"], 0.0)

    def test_active_lot_surfaces_lock_for_positions_cmd(self):
        """/positions uses _active_lot_from_store_key — must pass lock through."""
        from strategies.positions import _active_lot_from_store_key

        lock = build_lock(reason="hold_after_revert", locked_by="ops")
        lot = _active_lot_from_store_key(
            "BLESS_USDT_1h",
            {
                "amount": 181087.0,
                "average_entry": 0.023,
                "sold_percent": 0.0,
                "lock": lock,
            },
        )
        self.assertEqual(lot["symbol"], "BLESS/USDT")
        self.assertIsInstance(lot.get("lock"), dict)
        self.assertEqual(lot["lock"]["reason"], "hold_after_revert")
        self.assertTrue(is_position_locked(lot))


class TestDisplayLockBadge(unittest.TestCase):
    def test_compact_line_shows_lock(self):
        from notifications.telegram_commands.position_display import (
            format_position_compact_line,
        )

        p = {
            "symbol": "BLESS/USDT",
            "timeframe": "1h",
            "amount": 100.0,
            "average_entry": 0.03,
            "lock": build_lock(reason="hold", locked_by="test"),
        }
        line = format_position_compact_line(1, p, 0.03)
        self.assertIn("🔒", line)

    def test_card_shows_lock_line(self):
        from notifications.telegram_commands.position_display import format_position_card

        p = {
            "symbol": "BLESS/USDT",
            "amount": 100.0,
            "average_entry": 0.03,
            "lock": build_lock(reason="hold_bless", locked_by="test"),
        }
        card = format_position_card(1, p, 0.03, numbered=True)
        self.assertIn("🔒", card)
        self.assertIn("hold_bless", card)


if __name__ == "__main__":
    unittest.main()
