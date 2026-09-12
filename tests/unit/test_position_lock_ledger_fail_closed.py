"""#369 attach_lock_from_ledger fail-closed: synthetic lock on ledger-read failure.

Would have failed under the old swallow (`except Exception: pass` → treat as
unlocked): auto-sell/eviction proceeded when Mongo/ledger hiccuped.
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from risk.slot_eviction import VictimCandidate, plan_slot_eviction, score_entry_demand
from strategies.position_lock import (
    MODE_NO_AUTO_SELL,
    MODE_NO_EVICT,
    _LEDGER_READ_FAIL_LOGGED,
    attach_lock_from_ledger,
    auto_sell_blocked,
    build_lock,
    dca_blocked,
    eviction_blocked,
    get_lock,
)


_LOCK_CFG = {"risk": {"position_locks": {"enabled": True}}}
_LEDGER_DOWN = RuntimeError("mongo down")


def _open_pos(**extra):
    pos = {"amount": Decimal("100"), "average_entry": 1.0}
    pos.update(extra)
    return pos


def _slot_cfg() -> dict:
    return {
        "slot_eviction": {
            "enabled": True,
            "mode": "live",
            "min_entry_score": 4,
            "min_victim_score": 0.15,
            "min_hold_hours": 3,
            "protect_peak_gain_pct": 12,
            "max_evict_notional_usdt": 8000,
            "prefer_reduce_to_tail": True,
            "require_sensor_source": True,
            "skip_if_warmup": True,
            "memory": {
                "min_entry_keep_edge": 0.12,
                "prefer_keep_floor": 0.7,
                "prefer_is_hard_keep": True,
                "missing_profile_keep": 0.5,
            },
            "rag": {"mode": "off", "apply_to_plan": False},
            "weights": {"memory": 0.55, "idle": 0.2, "pnl_flat": 0.1, "tail_ready": 0.15},
            "sources": ["entry_sensor_15m", "vol_spike_15m"],
        }
    }


def _cand(symbol: str, *, keep_profile: float, gain: float = 4.0, prefer: bool = False):
    return VictimCandidate(
        symbol=symbol,
        timeframe="4h",
        gain_pct=gain,
        peak_gain_pct=gain,
        idle_hours=40.0,
        sold_percent=0.0,
        notional_usdt=1000.0,
        amount=10.0,
        price=1.0,
        keep_profile=keep_profile,
        keep_rag=keep_profile,
        keep_final=keep_profile,
        trail_armed=False,
        rotation_eligible=True,
        prefer=prefer,
        age_hours=48.0,
        class_name="A",
    )


class TestLedgerReadFailClosed(unittest.TestCase):
    def setUp(self):
        _LEDGER_READ_FAIL_LOGGED.clear()

    def tearDown(self):
        _LEDGER_READ_FAIL_LOGGED.clear()

    def _attach_on_raise(self, pos, symbol="FOO/USDT", timeframe="1h"):
        with patch(
            "data_manager.load_positions_document",
            side_effect=_LEDGER_DOWN,
        ), patch("strategies.positions.set_position_lock") as mock_set:
            result = attach_lock_from_ledger(pos, symbol, timeframe)
        mock_set.assert_not_called()
        return result

    def test_a_synthetic_lock_on_copy_not_input(self):
        pos = _open_pos()
        snapshot = dict(pos)

        result = self._attach_on_raise(pos)

        self.assertIsInstance(result, dict)
        self.assertIsNot(result, pos)
        self.assertEqual(pos, snapshot)
        self.assertNotIn("lock", pos)
        lock = result.get("lock")
        self.assertIsInstance(lock, dict)
        self.assertTrue(lock.get("enabled"))
        self.assertEqual(set(lock.get("modes") or []), {MODE_NO_AUTO_SELL, MODE_NO_EVICT})
        self.assertEqual(lock.get("reason"), "ledger_read_failed")
        self.assertEqual(lock.get("locked_by"), "system")
        self.assertIsNone(lock.get("until"))

    def test_b_auto_sell_blocked_auto_not_manual(self):
        result = self._attach_on_raise(_open_pos())

        blocked_ws, msg_ws = auto_sell_blocked(
            result, source="exit_ws", config=_LOCK_CFG
        )
        self.assertTrue(blocked_ws)
        self.assertIn("position_locked", msg_ws)

        blocked_trail, _ = auto_sell_blocked(result, source="trail", config=_LOCK_CFG)
        self.assertTrue(blocked_trail)

        blocked_manual, msg_manual = auto_sell_blocked(
            result, source="manual", config=_LOCK_CFG
        )
        self.assertFalse(blocked_manual)
        self.assertEqual(msg_manual, "")

        blocked_tg, _ = auto_sell_blocked(result, source="telegram", config=_LOCK_CFG)
        self.assertFalse(blocked_tg)

    def test_c_eviction_blocked_dca_allowed(self):
        result = self._attach_on_raise(_open_pos())
        self.assertTrue(eviction_blocked(result, config=_LOCK_CFG)[0])
        self.assertFalse(dca_blocked(result, config=_LOCK_CFG)[0])

    def test_d_risk_manager_exit_ws_denied_manual_approved(self):
        from core.models import TradeOrder
        from risk.risk_manager import RiskManager

        cfg = MagicMock()
        cfg.raw = {"risk": {"position_locks": {"enabled": True}}}
        cfg.risk_config = {}
        for attr in ("max_usdt_per_trade", "max_open_positions", "trade_cooldown_hours"):
            setattr(cfg, attr, 100)

        rm = RiskManager(cfg)
        unlocked = _open_pos()

        order_ws = TradeOrder(
            type="SELL",
            symbol="FOO/USDT",
            amount=100.0,
            price=0.03,
            source="exit_ws",
        )
        with patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")), patch(
            "risk.risk_manager.get_position", return_value=unlocked
        ), patch(
            "data_manager.load_positions_document", side_effect=_LEDGER_DOWN
        ), patch("strategies.position_lock.log_lock_block"):
            decision_ws = rm.evaluate(order_ws, timeframe="1h", source="exit_ws")
        self.assertFalse(decision_ws.approved)
        self.assertEqual(getattr(decision_ws, "code", None) or "", "position_locked")

        order_manual = TradeOrder(
            type="SELL",
            symbol="FOO/USDT",
            amount=50.0,
            price=0.03,
            source="manual",
        )
        with patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")), patch(
            "risk.risk_manager.get_position", return_value=unlocked
        ), patch(
            "data_manager.load_positions_document", side_effect=_LEDGER_DOWN
        ), patch.object(
            rm, "_resolve_sell_order", return_value=order_manual
        ), patch.object(
            rm, "_partial_sell_blocked", return_value=(False, "")
        ), patch.object(
            rm, "_effective_max_daily_sells", return_value=0
        ):
            decision_m = rm.evaluate(order_manual, timeframe="1h", source="manual")
        self.assertTrue(decision_m.approved)
        self.assertNotEqual(getattr(decision_m, "code", None), "position_locked")

    def test_e_plan_slot_eviction_drops_candidate(self):
        demand = score_entry_demand(
            symbol="BANK/USDT",
            source="entry_sensor_15m",
            free_full_slots=0,
            spike_multiple=5.0,
            risk_config=_slot_cfg(),
        )
        entry = _cand("BANK/USDT", keep_profile=0.65, gain=0.0)
        cands = [
            _cand("AAA/USDT", keep_profile=0.82, gain=5.0, prefer=True),
            _cand("BBB/USDT", keep_profile=0.32, gain=4.0),
            VictimCandidate(**{**entry.to_dict(), "veto": "entry_self"}),
        ]

        # Control: successful empty ledger read still picks BBB (old-rule baseline).
        with patch(
            "strategies.positions.get_position",
            side_effect=lambda sym, tf: {"amount": 10.0, "symbol": sym},
        ), patch(
            "data_manager.load_positions_document",
            return_value={"positions": {}},
        ):
            plan_ok = plan_slot_eviction(
                demand=demand, candidates=cands, risk_config=_slot_cfg()
            )
        self.assertTrue(plan_ok.ok, plan_ok.veto_reason)
        self.assertEqual(plan_ok.victim_symbol, "BBB/USDT")

        _LEDGER_READ_FAIL_LOGGED.clear()
        with patch(
            "strategies.positions.get_position",
            side_effect=lambda sym, tf: {"amount": 10.0, "symbol": sym},
        ), patch(
            "data_manager.load_positions_document",
            side_effect=_LEDGER_DOWN,
        ):
            plan_fail = plan_slot_eviction(
                demand=demand, candidates=cands, risk_config=_slot_cfg()
            )
        self.assertFalse(plan_fail.ok)
        self.assertEqual(plan_fail.veto_reason, "no_candidate")
        self.assertFalse(plan_fail.victim_symbol)
        pool = [c["symbol"] for c in plan_fail.candidates if not c.get("veto")]
        self.assertNotIn("BBB/USDT", pool)
        self.assertNotIn("AAA/USDT", pool)

    def test_f_one_error_log_per_failure_episode(self):
        pos = _open_pos()
        logs: list[tuple[str, str]] = []

        def _capture(message, level="INFO"):
            logs.append((str(level), str(message)))

        with patch("strategies.position_lock.log", side_effect=_capture):
            with patch(
                "data_manager.load_positions_document", side_effect=_LEDGER_DOWN
            ):
                attach_lock_from_ledger(pos, "FOO/USDT", "1h")
                attach_lock_from_ledger(pos, "FOO/USDT", "1h")
                attach_lock_from_ledger(pos, "FOO/USDT", "1h")
            errors = [
                msg
                for lvl, msg in logs
                if lvl.upper() == "ERROR" and "FOO/USDT" in msg
            ]
            self.assertEqual(len(errors), 1)
            self.assertIn("1h", errors[0])
            self.assertIn("mongo down", errors[0])

            # Successful read resets the episode marker.
            with patch(
                "data_manager.load_positions_document",
                return_value={"positions": {}},
            ):
                attach_lock_from_ledger(pos, "FOO/USDT", "1h")

            with patch(
                "data_manager.load_positions_document", side_effect=_LEDGER_DOWN
            ):
                attach_lock_from_ledger(pos, "FOO/USDT", "1h")
            errors = [
                msg
                for lvl, msg in logs
                if lvl.upper() == "ERROR" and "FOO/USDT" in msg
            ]
            self.assertEqual(len(errors), 2)

    def test_g_non_latching_successful_read_returns_no_lock(self):
        pos = _open_pos()
        failed = self._attach_on_raise(pos)
        self.assertIsNotNone(get_lock(failed))

        with patch(
            "data_manager.load_positions_document",
            return_value={"positions": {}},
        ), patch("strategies.positions.set_position_lock") as mock_set:
            recovered = attach_lock_from_ledger(pos, "FOO/USDT", "1h")
        mock_set.assert_not_called()
        self.assertIsNone(get_lock(recovered))
        self.assertNotIn("lock", pos)

    def test_early_returns_unchanged_when_ledger_would_raise(self):
        locked = _open_pos(lock=build_lock(reason="ops", locked_by="cli"))
        with patch(
            "data_manager.load_positions_document", side_effect=_LEDGER_DOWN
        ):
            self.assertIs(attach_lock_from_ledger(None, "FOO/USDT", "1h"), None)
            same = attach_lock_from_ledger(locked, "FOO/USDT", "1h")
            self.assertIs(same, locked)
            self.assertEqual(same["lock"]["reason"], "ops")

            dust = {"amount": 0, "average_entry": 1.0}
            dust_out = attach_lock_from_ledger(dust, "FOO/USDT", "1h")
            self.assertIs(dust_out, dust)
            self.assertNotIn("lock", dust)

    def test_ram_sync_failure_warns_keeps_ledger_lock(self):
        pos = _open_pos()
        ledger_lock = build_lock(reason="ops", locked_by="cli")
        doc = {
            "positions": {
                "FOO_USDT_1h": {"amount": 100, "lock": ledger_lock},
            }
        }
        logs: list[tuple[str, str]] = []

        def _capture(message, level="INFO"):
            logs.append((str(level), str(message)))

        with patch(
            "data_manager.load_positions_document", return_value=doc
        ), patch(
            "strategies.positions.set_position_lock",
            side_effect=RuntimeError("ram boom"),
        ), patch("strategies.position_lock.log", side_effect=_capture):
            result = attach_lock_from_ledger(pos, "FOO/USDT", "1h")

        self.assertEqual(result["lock"]["reason"], "ops")
        self.assertNotIn("lock", pos)
        warnings = [
            msg
            for lvl, msg in logs
            if lvl.upper() == "WARNING" and "desync" in msg.lower()
        ]
        self.assertEqual(len(warnings), 1)
        self.assertIn("FOO/USDT", warnings[0])
        self.assertIn("ram boom", warnings[0])


if __name__ == "__main__":
    unittest.main()
