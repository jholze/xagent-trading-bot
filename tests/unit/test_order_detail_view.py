import unittest
from unittest.mock import patch

from notifications.telegram_commands.order_detail_view import format_order_detail_rich
from notifications.telegram_commands.position_ledger import replay_position_events


def _buy(seq, ts, usdt, price, amount, signal=""):
    return {
        "status": "filled",
        "display_seq": seq,
        "side": "buy",
        "signal": signal,
        "source": "auto",
        "execution": {"price": price, "amount": amount, "usdt": usdt},
        "timestamps": {"filled": ts},
    }


def _sell(seq, ts, usdt, price, amount, pnl, signal="SELL_30"):
    return {
        "status": "filled",
        "display_seq": seq,
        "side": "sell",
        "signal": signal,
        "source": "auto",
        "pnl": pnl,
        "execution": {"price": price, "amount": amount, "usdt": usdt},
        "timestamps": {"filled": ts},
    }


class TestOrderDetailView(unittest.TestCase):
    def test_replay_includes_display_seq(self):
        orders = [_buy(1, "2026-06-24T10:00:00", 1000, 1.0, 1000)]
        events = replay_position_events(orders, mark_price=1.2)
        self.assertEqual(events[0]["display_seq"], 1)

    def test_detail_shows_trail_and_highlight(self):
        order = {
            "display_seq": 2,
            "status": "filled",
            "side": "sell",
            "symbol": "TRUMP/USDT",
            "timeframe": "4h",
            "source": "auto",
            "signal": "SELL_30",
            "ledger_scope": "paper",
            "request": {"price": 1.1, "amount": 300},
            "execution": {"usdt": 330, "price": 1.1, "amount": 300},
            "pnl": 30,
            "timestamps": {"filled": "2026-06-25T10:00:00"},
        }
        filled = [
            _buy(1, "2026-06-24T10:00:00", 1000, 1.0, 1000),
            order,
        ]
        pos = {
            "amount": 700,
            "average_entry": 1.0,
            "realized_pnl": 30,
        }
        with patch(
            "notifications.telegram_commands.order_detail_view.orders_for_position",
            return_value=filled,
        ), patch("strategies.positions.get_position", return_value=pos), patch(
            "price_fetcher.get_prices_batch",
            return_value=({"TRUMP/USDT": 1.2}, {}),
        ):
            msg = format_order_detail_rich(order, scope="paper")

        self.assertIn("Order #2", msg)
        self.assertIn("Position-Trail", msg)
        self.assertIn("Gesamt", msg)
        self.assertIn("▶", msg)
        self.assertIn("#2", msg)
        self.assertIn("Entry", msg)
        self.assertIn("Verkauf 30%", msg)
        self.assertIn("Σ Zyklus", msg)

    def test_detail_shows_new_cycle_after_re_entry(self):
        order = {
            "display_seq": 132,
            "status": "filled",
            "side": "buy",
            "symbol": "VELVET/USDT",
            "timeframe": "4h",
            "source": "cmc",
            "signal": "BUY",
            "ledger_scope": "paper",
            "request": {"price": 1.58, "amount": 772},
            "execution": {"usdt": 1220, "price": 1.58, "amount": 772},
            "timestamps": {"filled": "2026-06-28T04:32:00"},
        }
        filled = [
            _buy(1, "2026-06-24T10:00:00", 1000, 1.0, 1000),
            _sell(74, "2026-06-26T18:00:00", 1100, 1.1, 1000, 100, signal="SELL"),
            order,
        ]
        pos = {"amount": 300, "average_entry": 1.58, "realized_pnl": 408}
        with patch(
            "notifications.telegram_commands.order_detail_view.orders_for_position",
            return_value=filled,
        ), patch("strategies.positions.get_position", return_value=pos), patch(
            "price_fetcher.get_prices_batch",
            return_value=({"VELVET/USDT": 1.6}, {}),
        ):
            msg = format_order_detail_rich(order, scope="paper")

        self.assertIn("Entry (neu)", msg)
        self.assertIn("Zyklus 2", msg)
        self.assertIn("frühere Zyklen ausgeblendet", msg)
        self.assertIn("▶", msg)


if __name__ == "__main__":
    unittest.main()