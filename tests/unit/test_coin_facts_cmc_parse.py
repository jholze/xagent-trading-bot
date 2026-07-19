"""#103 fixture-based CMC AI HTML parse (no network)."""

from __future__ import annotations

import unittest
from pathlib import Path

from intelligence.memory.coin_facts_cmc import (
    build_cmc_ai_urls,
    parse_latest_updates_html,
    parse_price_analysis_html,
    parse_price_prediction_html,
    resolve_cmc_slug,
)

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cmc_ai"


class TestCmcParse(unittest.TestCase):
    def test_slug_allo(self):
        self.assertEqual(resolve_cmc_slug("ALLO/USDT"), "allora")

    def test_urls(self):
        urls = build_cmc_ai_urls("allora")
        self.assertIn("latest-updates", urls["latest_updates"])
        self.assertIn("price-analysis", urls["price_analysis"])
        self.assertIn("price-prediction", urls["price_prediction"])

    def test_parse_updates_yields_facts(self):
        html = (FIX / "allora_latest_updates.html").read_text()
        facts = parse_latest_updates_html(html, symbol="ALLO/USDT", slug="allora")
        self.assertTrue(facts)
        types = {f.event_type for f in facts}
        self.assertTrue(
            types & {"profit_taking_narrative", "unlock", "supply_overhang", "partnership"}
        )

    def test_parse_analysis(self):
        html = (FIX / "allora_price_analysis.html").read_text()
        facts = parse_price_analysis_html(html, symbol="ALLO/USDT", slug="allora")
        types = {f.event_type for f in facts}
        self.assertTrue(types & {"flow_only_move", "volume_breakout", "structure_bias"})

    def test_prediction_ignores_numeric_targets(self):
        html = (FIX / "allora_price_prediction.html").read_text()
        facts = parse_price_prediction_html(html, symbol="ALLO/USDT", slug="allora")
        self.assertTrue(all(f.event_type != "price_target" for f in facts))
        self.assertTrue(all(f.event_type != "ignore_target" for f in facts))
        types = {f.event_type for f in facts}
        self.assertTrue(
            types & {"utility_adoption", "unlock", "supply_overhang", "sector_rotation"}
        )


if __name__ == "__main__":
    unittest.main()
