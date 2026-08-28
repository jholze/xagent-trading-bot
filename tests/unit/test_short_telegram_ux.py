"""Telegram short UX: 🔻 SHORT / 🔺 COVER must be visible on every surface."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from core.models import TradeResult
from notifications.telegram_commands.order_commands import (
    _body_lines_by_side,
    _order_number_buttons,
    _perf_lines,
)
from notifications.telegram_commands.position_display import (
    format_position_card,
    format_position_compact_line,
    format_portfolio_summary,
    format_positions_message,
    format_sell_list_message,
    format_trade_banner,
)
from notifications.terminal_dashboard import format_executed_cycle_line, format_recent_trade_line
from services.order_service import format_order_line


def _long(sym="ARIA/USDT", amount=100.0, entry=0.04):
    return {"symbol": sym, "amount": amount, "average_entry": entry, "side": "long", "timeframe": "4h"}


def _short(sym="H/USDT", amount=50.0, entry=0.05, lev=2.0):
    return {
        "symbol": sym,
        "amount": amount,
        "average_entry": entry,
        "side": "short",
        "leverage": lev,
        "timeframe": "4h",
    }


class TestShortTelegramUx(unittest.TestCase):
    def test_compact_line_uses_glyph_and_margin(self):
        line = format_position_compact_line(1, _short(), 0.04)
        self.assertIn("🔻", line)
        self.assertIn("Margin", line)
        self.assertNotIn("<b>S</b>", line)
        self.assertIn("H", line)

    def test_card_uses_short_meta_not_s_badge(self):
        card = format_position_card(1, _short(), 0.04, numbered=True)
        self.assertIn("🔻", card)
        self.assertIn("SHORT", card)
        self.assertIn("Margin", card)
        self.assertIn("Equity", card)
        self.assertIn("2×", card)
        self.assertNotIn("<b>S</b>", card)

    def test_long_card_unchanged_no_short_chrome(self):
        card = format_position_card(1, _long(), 0.05, numbered=True)
        self.assertNotIn("🔻", card)
        self.assertNotIn("SHORT", card)
        self.assertIn("Wert", card)

    def test_mixed_positions_get_sections_and_header(self):
        active = [_long(), _short()]
        prices = {"ARIA/USDT": 0.05, "H/USDT": 0.04}
        with patch(
            "notifications.telegram_commands.position_display.initial_capital",
            return_value=5000.0,
        ):
            msg = format_positions_message(
                active, prices, {"virtual_balance": 1000, "trades": []},
                detail_level="compact", include_trades=False,
            )
        self.assertIn("Longs", msg)
        self.assertIn("Shorts", msg)
        self.assertIn("1 Long", msg)
        self.assertIn("1 Short", msg)
        self.assertLess(msg.index("ARIA"), msg.index("H"))
        self.assertIn("🔻", msg)

    def test_long_only_has_no_section_headers(self):
        active = [_long(), _long("SOL/USDT", amount=10, entry=1.0)]
        prices = {"ARIA/USDT": 0.05, "SOL/USDT": 10.0}
        with patch(
            "notifications.telegram_commands.position_display.initial_capital",
            return_value=5000.0,
        ):
            msg = format_positions_message(
                active, prices, {"virtual_balance": 1000, "trades": []},
                detail_level="compact", include_trades=False,
            )
        self.assertNotIn("Longs", msg)
        self.assertNotIn("Shorts", msg)

    def test_portfolio_header_sides_only_when_shorts(self):
        with patch(
            "notifications.telegram_commands.position_display.initial_capital",
            return_value=5000.0,
        ):
            mixed = format_portfolio_summary(
                {"virtual_balance": 1000, "realized_pnl": 0},
                total_unreal=0.0,
                position_count=2,
                positions_market_value=100.0,
                short_count=1,
            )
            longs = format_portfolio_summary(
                {"virtual_balance": 1000, "realized_pnl": 0},
                total_unreal=0.0,
                position_count=2,
                positions_market_value=100.0,
                short_count=0,
            )
        self.assertIn("1 Long", mixed)
        self.assertIn("1 Short", mixed)
        self.assertNotIn("Short", longs)

    def test_sell_list_footer_when_only_shorts(self):
        msg = format_sell_list_message([_short()], {"H/USDT": 0.04})
        self.assertIn("Long-Position", msg)
        self.assertIn("Keine offenen Long", msg)
        self.assertIn("Short offen", msg)
        self.assertIn("/cover", msg)
        self.assertNotIn("<b>1.</b>", msg)

    def test_trade_banner_glyphs(self):
        short = format_trade_banner(
            TradeResult(True, "SHORT", "H/USDT", amount=10, price=0.04, usdt_amount=20)
        )
        cover = format_trade_banner(
            TradeResult(True, "COVER", "H/USDT", amount=10, price=0.03, usdt_amount=15, pnl=5)
        )
        self.assertTrue(short.startswith("🔻"))
        self.assertIn("Notional", short)
        self.assertTrue(cover.startswith("🔺"))
        self.assertIn("Cover", cover)

    def test_orders_split_shorts_and_covers_not_sonstige(self):
        orders = [
            {"side": "buy", "display_seq": 1, "status": "filled", "symbol": "AAA/USDT"},
            {"side": "sell", "display_seq": 2, "status": "filled", "symbol": "BBB/USDT"},
            {"side": "short", "display_seq": 3, "status": "filled", "symbol": "H/USDT"},
            {"side": "cover", "display_seq": 4, "status": "filled", "symbol": "H/USDT"},
        ]
        with patch(
            "notifications.telegram_commands.order_commands.format_order_line",
            side_effect=lambda o, **k: f"line-{o['display_seq']}",
        ):
            text = "\n".join(_body_lines_by_side(orders))
        self.assertIn("🔻 Shorts", text)
        self.assertIn("🔺 Cover", text)
        self.assertNotIn("Sonstige", text)
        self.assertLess(text.index("Käufe"), text.index("Shorts"))
        self.assertLess(text.index("line-3"), text.index("line-4"))

    def test_order_buttons_short_not_same_as_sell(self):
        orders = [
            {"side": "sell", "display_seq": 41, "status": "filled", "symbol": "H/USDT"},
            {"side": "short", "display_seq": 42, "status": "filled", "symbol": "H/USDT"},
        ]
        buttons = _order_number_buttons("day", "paper", orders)
        labels = [b["text"] for row in buttons for b in row]
        self.assertIn("#41 🔴", labels)
        self.assertIn("#42 🔻", labels)
        self.assertNotIn("#42 S", labels)

    def test_order_line_prefixes_glyph(self):
        line = format_order_line({
            "side": "short",
            "display_seq": 7,
            "status": "filled",
            "symbol": "H/USDT",
            "source": "auto",
            "execution": {"usdt": 28, "price": 0.07, "amount": 400},
        })
        self.assertIn("🔻", line)
        self.assertIn("SHORT", line)

    def test_perf_lines_append_shorts(self):
        lines = _perf_lines(
            {"buys": 1, "sells": 1, "shorts": 2, "covers": 1, "buy_usdt": 10, "sell_usdt": 10,
             "realized_pnl": 1, "sell_wins": 1, "sell_losses": 0},
            period_label="Tages-PnL",
        )
        self.assertIn("🔻 2 Shorts", lines[0])
        self.assertIn("🔺 1 Cover", lines[0])
        with_covers = _perf_lines(
            {"buys": 0, "sells": 0, "buy_usdt": 0, "sell_usdt": 0,
             "realized_pnl": 3, "sell_wins": 0, "sell_losses": 0,
             "wins": 2, "losses": 1},
            period_label="Tages-PnL",
        )
        self.assertIn("2W / 1L", with_covers[1])

    def test_cycle_executed_line_short(self):
        line = format_executed_cycle_line({
            "symbol": "H/USDT",
            "order_type": "SHORT",
            "usdt_amount": 28,
            "leverage": 2,
            "margin": 14,
        })
        self.assertIn("🔻", line)
        self.assertIn("SHORT", line)
        self.assertIn("2×", line)
        self.assertIn("Margin $14", line)

    def test_recent_trade_line_cover(self):
        line = format_recent_trade_line({
            "type": "COVER",
            "symbol": "H/USDT",
            "usdt_amount": 28,
            "pnl": 2.0,
            "source": "auto",
        })
        self.assertIn("🔺", line)
        self.assertIn("COVER", line)


if __name__ == "__main__":
    unittest.main()
