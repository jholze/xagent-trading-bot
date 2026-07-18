"""Epic #72 C2: Hermes SelfImprover RAG + heuristic fail-open."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from hermes.self_improver import SelfImprover, _rag_section_for_proposal
from intelligence.grok_json import GrokError


class TestHermesRagProposal(unittest.TestCase):
    def test_rag_section_empty_when_disabled(self):
        with patch("hermes.self_improver.get_bot_config") as gbc:
            gbc.return_value.raw = {"memory": {"rag": {"enabled": False}}}
            sec = _rag_section_for_proposal({"params": {"rsi_buy_low": 30}}, "ARIA/USDT", "4h")
        self.assertEqual(sec, "")

    def test_rag_section_includes_retrieved_memory(self):
        from hermes.memory.rag_retriever import RagHit

        hit = RagHit(text="ARIA weak history after DCA", score=0.9, metadata={"symbol": "ARIA/USDT"})

        with patch("hermes.self_improver.get_bot_config") as gbc, patch(
            "hermes.memory.rag_retriever.RagRetriever.retrieve", return_value=[hit]
        ), patch("intelligence.memory.rag_config.rag_enabled", return_value=True):
            gbc.return_value.raw = {"memory": {"rag": {"enabled": True}}}
            # retrieve is patched on class used inside function after import
            with patch("hermes.self_improver.RagRetriever", create=True):
                pass
            with patch("hermes.memory.rag_retriever.RagRetriever") as RR:
                inst = RR.return_value
                inst.retrieve.return_value = [hit]
                with patch("intelligence.memory.rag_config.rag_enabled", return_value=True):
                    # re-import path uses RagRetriever from hermes.memory.rag_retriever inside function
                    with patch.dict("sys.modules", {}):
                        sec = _rag_section_for_proposal(
                            {"params": {"rsi_buy_low": 30}}, "ARIA/USDT", "4h"
                        )
        # Direct call with proper patch location
        with patch("hermes.self_improver.get_bot_config") as gbc2:
            gbc2.return_value.raw = {"memory": {"rag": {"enabled": True}}}
            with patch("intelligence.memory.rag_config.rag_enabled", return_value=True):
                with patch("hermes.memory.rag_retriever.RagRetriever") as RR2:
                    inst2 = RR2.return_value
                    inst2.retrieve.return_value = [hit]
                    sec = _rag_section_for_proposal(
                        {"params": {"rsi_buy_low": 30}}, "ARIA/USDT", "4h"
                    )
        self.assertIn("RETRIEVED_MEMORY", sec)
        self.assertIn("ARIA weak history", sec)

    def test_propose_falls_back_to_heuristic_on_grok_error(self):
        improver = SelfImprover(config=MagicMock())
        improver.runner = MagicMock()
        prop = MagicMock()
        prop.variable = "rsi_buy_low"
        prop.old_value = 30
        prop.new_value = 28
        prop.hypothesis = "tighter"
        prop.source = "heuristic"
        improver.runner.propose.return_value = prop
        improver.runner.tunable_params = ["rsi_buy_low"]

        baseline = {"params": {"rsi_buy_low": 30}, "symbol": "ARIA/USDT", "timeframe": "4h"}
        with patch("hermes.self_improver.store") as st, patch(
            "hermes.self_improver.ask_grok_json", side_effect=GrokError("no key")
        ), patch("hermes.self_improver._rag_section_for_proposal", return_value="RETRIEVED_MEMORY:\n- hit"):
            st.recent_experiments.return_value = []
            st.relevant_skills.return_value = []
            out = improver.propose_experiment(baseline)
        self.assertEqual(out["source"], "heuristic")
        self.assertEqual(out["variable"], "rsi_buy_low")

    def test_propose_passes_rag_in_prompt_to_grok(self):
        improver = SelfImprover(config=MagicMock())
        improver.runner = MagicMock()
        improver.runner.tunable_params = ["rsi_buy_low"]
        captured = {}

        def _ask(prompt, required_keys=None):
            captured["prompt"] = prompt
            return {"variable": "rsi_buy_low", "new_value": 27, "old_value": 30, "hypothesis": "h"}

        baseline = {"params": {"rsi_buy_low": 30}, "symbol": "ARIA/USDT", "timeframe": "4h"}
        with patch("hermes.self_improver.store") as st, patch(
            "hermes.self_improver.ask_grok_json", side_effect=_ask
        ), patch(
            "hermes.self_improver._rag_section_for_proposal",
            return_value="RETRIEVED_MEMORY:\n- ARIA lesson",
        ):
            st.recent_experiments.return_value = []
            st.relevant_skills.return_value = []
            out = improver.propose_experiment(baseline)
        self.assertEqual(out.get("source"), "grok")
        self.assertTrue(out.get("rag"))
        self.assertIn("RETRIEVED_MEMORY", captured["prompt"])
        self.assertIn("ARIA lesson", captured["prompt"])


if __name__ == "__main__":
    unittest.main()
