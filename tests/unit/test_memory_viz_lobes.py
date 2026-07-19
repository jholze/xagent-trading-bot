"""Unit tests for tools.memory_viz.lobes — real shipped classify_lobe."""

from __future__ import annotations

import unittest

from tools.memory_viz.lobes import (
    LOBE_COLORS,
    LOBE_ORDER,
    classify_lobe,
    lobe_color,
    lobe_legend,
)


class TestLobes(unittest.TestCase):
    def test_coin_facts_from_cmc_source(self):
        self.assertEqual(
            classify_lobe({"source": "cmc_pro_quotes", "type": "volume_breakout"}),
            "coin_facts",
        )
        self.assertEqual(classify_lobe({"source": "cmc_mcp_news"}), "coin_facts")
        self.assertEqual(classify_lobe({"source": "cmc_ai_updates"}), "coin_facts")
        self.assertEqual(classify_lobe({"kind": "coin_fact"}), "coin_facts")

    def test_trades_lessons_events_social_other(self):
        self.assertEqual(classify_lobe({"type": "trade", "source": "x"}), "trades")
        self.assertEqual(classify_lobe({"type": "lesson", "source": "dca_lesson"}), "lessons")
        self.assertEqual(classify_lobe({"type": "regime", "source": "fusion"}), "events")
        self.assertEqual(classify_lobe({"type": "community", "source": "social"}), "social")
        self.assertEqual(classify_lobe({"type": "misc", "source": "notes"}), "other")

    def test_explicit_lobe_wins(self):
        self.assertEqual(
            classify_lobe({"lobe": "lessons", "source": "cmc_pro_quotes"}),
            "lessons",
        )

    def test_colors_and_legend(self):
        for k in LOBE_ORDER:
            self.assertIn(k, LOBE_COLORS)
            c = lobe_color(k)
            self.assertEqual(len(c), 3)
            self.assertTrue(all(0.0 <= x <= 1.0 for x in c))
        legend = lobe_legend()
        self.assertEqual(len(legend), len(LOBE_ORDER))
        self.assertEqual(legend[0]["id"], "coin_facts")


if __name__ == "__main__":
    unittest.main()
