import unittest
from unittest.mock import patch

from notifications.telegram_commands.position_ledger import (
    build_position_trade_tree,
    cycle_for_display_seq,
    events_for_cycle,
    orders_for_position,
    replay_position_events,
)


def _buy(ts, usdt, price, amount, signal="", source="auto"):
    return {
        "status": "filled",
        "side": "buy",
        "signal": signal,
        "source": source,
        "execution": {"price": price, "amount": amount, "usdt": usdt},
        "timestamps": {"filled": ts},
    }


def _sell(ts, usdt, price, amount, pnl, signal="SELL_30", source="auto"):
    return {
        "status": "filled",
        "side": "sell",
        "signal": signal,
        "source": source,
        "pnl": pnl,
        "execution": {"price": price, "amount": amount, "usdt": usdt},
        "timestamps": {"filled": ts},
    }


class TestPositionLedger(unittest.TestCase):
    def test_replay_fifo_partial_sell_reduces_lot_open_qty(self):
        orders = [
            _buy("2026-06-24T10:00:00", 1000, 1.0, 1000),
            _sell("2026-06-25T10:00:00", 300, 1.1, 300, 30),
        ]
        events = replay_position_events(orders, mark_price=1.2)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["kind"], "entry")
        self.assertAlmostEqual(events[0]["open_qty"], 700.0)
        self.assertAlmostEqual(events[0]["open_usd"], 140.0, places=1)
        self.assertEqual(events[1]["kind"], "sell")
        self.assertAlmostEqual(events[1]["realized_usd"], 30.0)

    def test_dca_labels_increment(self):
        orders = [
            _buy("2026-06-24T10:00:00", 1000, 1.0, 1000),
            _buy("2026-06-25T10:00:00", 400, 1.1, 400, signal="BUY_DCA"),
        ]
        events = replay_position_events(orders, mark_price=1.2)
        self.assertEqual(events[0]["label"], "Entry")
        self.assertEqual(events[1]["label"], "DCA #1")

    def test_build_tree_contains_gesamt_and_wide_event_line(self):
        pos = {
            "symbol": "TRUMP/USDT",
            "timeframe": "4h",
            "amount": 700,
            "average_entry": 1.0,
            "realized_pnl": 30,
        }
        orders = [
            _buy("2026-06-24T10:00:00", 1000, 1.0, 1000),
            _sell("2026-06-25T10:00:00", 330, 1.1, 300, 30, signal="SELL_30"),
        ]
        lines = build_position_trade_tree(pos, mark_price=1.2, orders=orders, max_events=6)
        text = "\n".join(lines)
        self.assertIn("Gesamt", text)
        self.assertIn("Σ", text)
        self.assertIn("Entry", text)
        self.assertIn("Verkauf 30%", text)
        self.assertIn("real <b>$+30</b>", text)
        self.assertIn("├─", text)
        self.assertIn("└─", text)

    def test_re_entry_after_full_close_starts_new_cycle(self):
        orders = [
            _buy("2026-06-24T10:00:00", 1000, 1.0, 1000),
            _sell("2026-06-25T10:00:00", 1100, 1.1, 1000, 100, signal="SELL"),
            _buy("2026-06-28T04:00:00", 1220, 1.58, 772),
        ]
        events = replay_position_events(orders, mark_price=1.6)
        self.assertEqual(events[0]["label"], "Entry")
        self.assertEqual(events[0]["cycle"], 1)
        self.assertTrue(events[1].get("cycle_closed"))
        self.assertEqual(events[2]["label"], "Entry (neu)")
        self.assertEqual(events[2]["cycle"], 2)
        self.assertEqual(len(events_for_cycle(events, 2)), 1)

    def test_build_tree_hides_prior_cycles(self):
        pos = {
            "symbol": "VELVET/USDT",
            "timeframe": "4h",
            "amount": 300,
            "average_entry": 1.58,
            "realized_pnl": 408,
        }
        orders = [
            _buy("2026-06-24T10:00:00", 1000, 1.0, 1000),
            _sell("2026-06-25T10:00:00", 1100, 1.1, 1000, 100, signal="SELL"),
            _buy("2026-06-28T04:00:00", 1220, 1.58, 772),
        ]
        lines = build_position_trade_tree(pos, mark_price=1.6, orders=orders, max_events=6)
        text = "\n".join(lines)
        self.assertIn("Entry (neu)", text)
        self.assertIn("frühere Zyklen ausgeblendet", text)
        self.assertNotIn("geschlossen", text)

    def test_cycle_for_display_seq_maps_order_to_cycle(self):
        orders = [
            _buy("2026-06-24T10:00:00", 1000, 1.0, 1000),
            _sell("2026-06-25T10:00:00", 1100, 1.1, 1000, 100, signal="SELL"),
            {**_buy("2026-06-28T04:00:00", 1220, 1.58, 772), "display_seq": 132},
        ]
        events = replay_position_events(orders, mark_price=1.6)
        self.assertEqual(cycle_for_display_seq(events, 132), 2)

    def test_orders_for_position_filters_symbol_and_tf(self):
        doc = {
            "orders": [
                {"status": "filled", "symbol": "BTC/USDT", "timeframe": "4h", "side": "buy",
                 "timestamps": {"filled": "2026-06-24T10:00:00"}, "execution": {"usdt": 1}},
                {"status": "filled", "symbol": "ETH/USDT", "timeframe": "4h", "side": "buy",
                 "timestamps": {"filled": "2026-06-24T11:00:00"}, "execution": {"usdt": 2}},
            ]
        }
        with patch("notifications.telegram_commands.position_ledger.load_orders", return_value=doc):
            out = orders_for_position("BTC/USDT", "4h", "demo")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["symbol"], "BTC/USDT")


if __name__ == "__main__":
    unittest.main()