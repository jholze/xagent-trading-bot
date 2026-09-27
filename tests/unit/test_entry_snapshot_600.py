"""#600: freeze an entry_snapshot on first fill without blocking the fill.

No Telegram. No close/PnL/MAE/MFE fields. Nothing written under data/.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from strategies.positions import (
    _ENTRY_SNAPSHOT_KEYS,
    _deserialize_position,
    _serialize_positions,
    clear_positions_memory,
    get_position,
    set_position_field,
    update_position,
)


def _utc_offset(stamp: object) -> None:
    parsed = datetime.fromisoformat(str(stamp))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


class TestEntrySnapshot600(unittest.TestCase):
    def setUp(self):
        clear_positions_memory()

    def tearDown(self):
        clear_positions_memory()

    def _open_long(self, symbol="SNAP/USDT", **kwargs):
        update_position(symbol, "4h", "BUY", 1.25, 10, **kwargs)
        return get_position(symbol, "4h")

    def _open_short(self, symbol="SHRT/USDT", **kwargs):
        kwargs.setdefault("leverage", 2)
        update_position(symbol, "4h", "SHORT", 0.4, 10, **kwargs)
        return get_position(symbol, "4h")

    def test_new_long_stores_entry_snapshot_once_only_those_keys(self):
        pos = self._open_long(
            fee=0.01,
            source="auto",
            strategy_profile="volatile_altcoin",
            strategy_tier="volatile",
            rationale_codes=["rsi_dip"],
            rsi=28.5,
            volume_factor=1.2,
            atr=0.04,
            volatility_tier="volatile",
            size_before_mult=100.0,
            size_after_mult=80.0,
            cap_applied=True,
        )
        snap = pos["entry_snapshot"]
        self.assertEqual(set(snap), set(_ENTRY_SNAPSHOT_KEYS))
        _utc_offset(snap["captured_at"])
        self.assertEqual(snap["fill_price"], 1.25)
        self.assertEqual(snap["fee"], 0.01)
        self.assertEqual(snap["source"], "auto")
        self.assertEqual(snap["timeframe"], "4h")
        self.assertEqual(snap["strategy_profile"], "volatile_altcoin")
        self.assertEqual(snap["strategy_tier"], "volatile")
        self.assertEqual(snap["rationale_codes"], ["rsi_dip"])
        self.assertEqual(snap["rsi"], 28.5)
        self.assertEqual(snap["volume_factor"], 1.2)
        self.assertEqual(snap["atr"], 0.04)
        self.assertEqual(snap["volatility_tier"], "volatile")
        self.assertEqual(snap["size_before_mult"], 100.0)
        self.assertEqual(snap["size_after_mult"], 80.0)
        self.assertTrue(snap["cap_applied"])
        first = dict(snap)
        update_position("SNAP/USDT", "4h", "BUY", 1.40, 5, rsi=99.0, strategy_tier="stable")
        self.assertEqual(get_position("SNAP/USDT", "4h")["entry_snapshot"], first)

    def test_new_short_stores_entry_snapshot_once_only_those_keys(self):
        pos = self._open_short(
            fee=0.02,
            source="shorts",
            strategy_profile="hermes_baseline",
            strategy_tier="stable",
            rationale_codes=["overbought"],
            rsi=72.0,
            volume_factor=0.8,
            atr=0.01,
            volatility_tier="stable",
            size_before_mult=50.0,
            size_after_mult=50.0,
            cap_applied=False,
        )
        snap = pos["entry_snapshot"]
        self.assertEqual(set(snap), set(_ENTRY_SNAPSHOT_KEYS))
        _utc_offset(snap["captured_at"])
        self.assertEqual(snap["fill_price"], 0.4)
        self.assertEqual(snap["fee"], 0.02)
        self.assertEqual(snap["source"], "shorts")
        self.assertEqual(snap["timeframe"], "4h")
        self.assertEqual(snap["strategy_profile"], "hermes_baseline")
        self.assertEqual(snap["strategy_tier"], "stable")
        self.assertEqual(snap["rationale_codes"], ["overbought"])
        first = dict(snap)
        update_position("SHRT/USDT", "4h", "SHORT_ADD", 0.35, 4, leverage=2, rsi=10.0)
        self.assertEqual(get_position("SHRT/USDT", "4h")["entry_snapshot"], first)

    def test_passed_strategy_tier_is_not_read_from_the_lot(self):
        set_position_field("TIER/USDT", "4h", "strategy_tier", "stable")
        pos = self._open_long(
            symbol="TIER/USDT",
            strategy_tier="volatile",
        )
        self.assertIsNone(pos["strategy_tier"])
        self.assertEqual(pos["entry_snapshot"]["strategy_tier"], "volatile")

    def test_missing_inputs_are_null_rationale_codes_empty(self):
        pos = self._open_long()
        snap = pos["entry_snapshot"]
        self.assertEqual(set(snap), set(_ENTRY_SNAPSHOT_KEYS))
        self.assertIsNone(snap["fee"])
        self.assertIsNone(snap["source"])
        self.assertIsNone(snap["strategy_profile"])
        self.assertIsNone(snap["strategy_tier"])
        self.assertEqual(snap["rationale_codes"], [])
        self.assertIsNone(snap["rsi"])
        self.assertIsNone(snap["volume_factor"])
        self.assertIsNone(snap["atr"])
        self.assertIsNone(snap["volatility_tier"])
        self.assertIsNone(snap["size_before_mult"])
        self.assertIsNone(snap["size_after_mult"])
        self.assertIsNone(snap["cap_applied"])
        self.assertEqual(snap["fill_price"], 1.25)
        self.assertEqual(snap["timeframe"], "4h")
        _utc_offset(snap["captured_at"])

    def test_dca_add_does_not_replace_snapshot(self):
        pos = self._open_long(rsi=30.0, strategy_tier="volatile", source="auto")
        first = dict(pos["entry_snapshot"])
        update_position(
            "SNAP/USDT",
            "4h",
            "BUY_DCA",
            1.10,
            20,
            rsi=12.0,
            strategy_tier="stable",
            source="dca",
            fee=9.9,
        )
        after = get_position("SNAP/USDT", "4h")
        self.assertEqual(after["entry_snapshot"], first)
        self.assertEqual(after["dca_rounds"], 1)
        self.assertGreater(float(after["amount"]), 10)

    def test_attach_error_leaves_fill_and_logs_warning_with_symbol(self):
        with patch("strategies.positions.save_positions_document", return_value=True) as mock_save:
            with patch(
                "strategies.positions._build_entry_snapshot",
                side_effect=RuntimeError("snapshot boom"),
            ):
                with patch("strategies.positions.log") as mock_log:
                    update_position("FAIL/USDT", "4h", "BUY", 2.0, amount_traded=7)
        pos = get_position("FAIL/USDT", "4h")
        self.assertEqual(pos["amount"], Decimal("7"))
        self.assertEqual(float(pos["average_entry"]), 2.0)
        self.assertNotIn("entry_snapshot", pos)
        self.assertTrue(mock_save.called)
        warnings = [
            args
            for args, _kwargs in mock_log.call_args_list
            if len(args) >= 2 and args[1] == "WARNING"
        ]
        self.assertTrue(warnings, "expected WARNING log on attach failure")
        self.assertTrue(
            any("FAIL/USDT" in str(args[0]) for args in warnings),
            warnings,
        )

    def test_lot_loaded_without_entry_snapshot_keeps_amount_and_fields(self):
        raw = {
            "amount": 7,
            "peak_amount": 7,
            "sold_percent": 0.0,
            "average_entry": 1.5,
            "realized_pnl": 0.25,
            "last_buy_price": 1.5,
            "first_buy_at": "2026-09-01T12:00:00",
            "entry_source": "auto",
            "entry_at": "2026-09-01T12:00:00",
            "strategy_tier": "volatile",
            "dca_rounds": 2,
            "dca_total_usdt": 40.0,
            "side": "long",
        }
        self.assertNotIn("entry_snapshot", raw)
        pos = _deserialize_position(raw)
        self.assertNotIn("entry_snapshot", pos)
        self.assertEqual(pos["amount"], Decimal("7"))
        self.assertEqual(float(pos["average_entry"]), 1.5)
        self.assertEqual(pos["strategy_tier"], "volatile")
        self.assertEqual(pos["first_buy_at"], "2026-09-01T12:00:00")
        self.assertEqual(pos["entry_source"], "auto")
        self.assertEqual(pos["entry_at"], "2026-09-01T12:00:00")
        self.assertEqual(pos["dca_rounds"], 2)
        self.assertEqual(float(pos["dca_total_usdt"]), 40.0)
        self.assertEqual(float(pos["realized_pnl"]), 0.25)
        self.assertEqual(pos["side"], "long")

        clear_positions_memory()
        loaded = get_position("OLD/USDT", "4h")
        loaded.update(_deserialize_position(raw))
        payload = _serialize_positions()
        stored = payload["positions"].get("OLD_USDT_4h") or {}
        self.assertNotIn("entry_snapshot", stored)
        self.assertEqual(stored["amount"], 7.0)
        self.assertEqual(stored["average_entry"], 1.5)
        self.assertEqual(stored["strategy_tier"], "volatile")
        self.assertEqual(stored["dca_rounds"], 2)

    def test_reopen_long_fill_save_drops_old_snapshot(self):
        update_position("REOP/USDT", "4h", "BUY", 1.25, 10, rsi=30.0)
        self.assertIn("entry_snapshot", get_position("REOP/USDT", "4h"))
        update_position("REOP/USDT", "4h", "SELL_FULL", 1.25, 10)
        with patch("strategies.positions.save_positions_document", return_value=True) as mock_save:
            with patch(
                "strategies.positions._build_entry_snapshot",
                side_effect=RuntimeError("reopen boom"),
            ):
                update_position("REOP/USDT", "4h", "BUY", 2.0, 8)
        self.assertGreaterEqual(mock_save.call_count, 1)
        first_lot = mock_save.call_args_list[0].args[0]["positions"]["REOP_USDT_4h"]
        self.assertNotIn("entry_snapshot", first_lot)
        pos = get_position("REOP/USDT", "4h")
        self.assertEqual(pos["amount"], Decimal("8"))
        self.assertNotIn("entry_snapshot", pos)

    def test_reopen_short_fill_save_drops_old_snapshot(self):
        update_position("SROP/USDT", "4h", "SHORT", 0.4, 10, leverage=2, rsi=70.0)
        self.assertIn("entry_snapshot", get_position("SROP/USDT", "4h"))
        update_position("SROP/USDT", "4h", "COVER_FULL", 0.4, 10)
        with patch("strategies.positions.save_positions_document", return_value=True) as mock_save:
            with patch(
                "strategies.positions._build_entry_snapshot",
                side_effect=RuntimeError("reopen boom"),
            ):
                update_position("SROP/USDT", "4h", "SHORT", 0.5, 6, leverage=2)
        self.assertGreaterEqual(mock_save.call_count, 1)
        first_lot = mock_save.call_args_list[0].args[0]["positions"]["SROP_USDT_4h"]
        self.assertNotIn("entry_snapshot", first_lot)
        pos = get_position("SROP/USDT", "4h")
        self.assertEqual(pos["amount"], Decimal("6"))
        self.assertNotIn("entry_snapshot", pos)


if __name__ == "__main__":
    unittest.main()
