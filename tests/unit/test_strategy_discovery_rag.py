"""Epic #72 C4 remainder: strategy_discovery Grok prompt includes RAG evidence."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from intelligence.strategy_discovery import StrategyDiscovery


class TestStrategyDiscoveryRag(unittest.TestCase):
    def setUp(self):
        cfg = MagicMock()
        cfg.raw = {
            "use_mock_x_data": False,
            "memory": {"rag": {"enabled": True, "embedding_backend": "hash"}},
            "stop_loss_pct": 12,
        }
        cfg.stop_loss_pct = 12
        self.sd = StrategyDiscovery(config=cfg)

    def test_build_prompt_includes_injected_retrieved_section(self):
        prompt = self.sd.build_discovery_prompt(
            "RSI divergence on ARIA/USDT 4h with volume breakout",
            "MacroCRG",
            retrieved_section="RETRIEVED_MEMORY:\n- ARIA weak after volume chase unique_discovery_marker",
        )
        self.assertIn("RETRIEVED_MEMORY", prompt)
        after = prompt.split("RETRIEVED_MEMORY", 1)[1]
        self.assertIn("unique_discovery_marker", after)
        self.assertIn("ARIA/USDT", prompt)
        self.assertIn("MacroCRG", prompt)

    def test_build_prompt_uses_real_retriever_hits(self):
        from hermes.memory.rag_retriever import RagRetriever

        r = RagRetriever.in_memory(
            config={"memory": {"rag": {"enabled": True, "embedding_backend": "hash"}}}
        )
        r.add_to_memory(
            "ARIA/USDT volume breakout failed after social hype unique_sd_hit",
            {"type": "lesson", "symbol": "ARIA/USDT", "source_id": "les_sd"},
        )
        with patch("intelligence.memory.rag_config.rag_enabled", return_value=True):
            prompt = self.sd.build_discovery_prompt(
                "RSI divergence volume breakout ARIA/USDT",
                "trader",
                retriever=r,
            )
        self.assertIn("RETRIEVED_MEMORY", prompt)
        after = prompt.split("RETRIEVED_MEMORY", 1)[1]
        self.assertIn("unique_sd_hit", after)

    def test_build_prompt_fail_open_when_rag_disabled(self):
        with patch("intelligence.memory.rag_config.rag_enabled", return_value=False):
            prompt = self.sd.build_discovery_prompt(
                "RSI divergence volume breakout ARIA/USDT",
                "trader",
            )
        self.assertNotIn("RETRIEVED_MEMORY", prompt)
        self.assertIn("RSI divergence", prompt)

    def test_discover_from_tweet_passes_rag_prompt_to_ask_grok(self):
        captured = {}

        def _ask(prompt, temperature=0.7, model=None):
            captured["prompt"] = prompt
            return (
                '{"name":"RSI div 4h","timeframe":"4h","symbol":"ARIA/USDT",'
                '"conditions":"div","rationale":"r","params":{"rsi_buy_low":28,'
                '"rsi_buy_high":45,"volume_multiplier":1.4,"rsi_sell_30":70,'
                '"rsi_sell_20":80,"stop_loss_pct":12}}'
            )

        with patch("intelligence.strategy_discovery.ask_grok", side_effect=_ask), patch.object(
            self.sd,
            "build_discovery_prompt",
            return_value="PROMPT_WITH_RETRIEVED_MEMORY unique_wire",
        ) as bdp:
            hyp = self.sd.discover_from_tweet(
                "RSI divergence volume breakout ARIA/USDT",
                "acct",
                post_id="p1",
            )
        self.assertIsNotNone(hyp)
        bdp.assert_called_once()
        self.assertIn("RETRIEVED_MEMORY", captured["prompt"])
        self.assertIn("unique_wire", captured["prompt"])

    def test_discover_heuristic_when_mock_x(self):
        self.sd.config.raw["use_mock_x_data"] = True
        hyp = self.sd.discover_from_tweet(
            "RSI divergence volume breakout ARIA/USDT",
            "acct",
        )
        self.assertIsNotNone(hyp)
        self.assertIn("Heuristic", hyp.rationale or "")


if __name__ == "__main__":
    unittest.main()
