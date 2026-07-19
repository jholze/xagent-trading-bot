"""#99 live DCA policy snapshot for /ask."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from strategies.dca_policy import DcaContext, DcaPolicyResult


class TestDcaAskSnapshot(unittest.TestCase):
    def test_empty_without_symbol(self):
        from strategies.dca_ask_snapshot import format_live_dca_policy_snapshot

        self.assertEqual(format_live_dca_policy_snapshot(""), "")
        self.assertEqual(format_live_dca_policy_snapshot(None), "")

    def test_snapshot_block_when_policy_enabled(self):
        from strategies.dca_ask_snapshot import format_live_dca_policy_snapshot

        with patch(
            "strategies.dca_ask_snapshot._policy_cfg_for_symbol",
            return_value={
                "enabled": True,
                "ask_snapshot": True,
                "shadow": True,
                "policy_version": "1",
                "max_policy_mult": 2.0,
                "harvest_mode": "skip",
                "deploy_mult": 1.35,
                "harvest_mult": 0.4,
                "calendar_mult": 0.5,
                "session_mult": 0.7,
                "drawdown_mult": 0.5,
                "score_boost_mult": 1.25,
                "score_boost_ratio": 0.8,
                "soft_block_mult": 0.6,
                "size_mult_harvest": 0.7,
                "size_mult_deploy": 1.0,
            },
        ), patch(
            "strategies.dca_context.build_dca_context",
            return_value=DcaContext(
                symbol="ZBT/USDT",
                cash_mode="HARVEST",
                fusion_size_mult=0.4,
                spendable_dca=800,
            ),
        ), patch(
            "strategies.dca_policy.evaluate_dca_policy",
            return_value=DcaPolicyResult(
                size_mult=1.0,
                skip=True,
                reason_codes=("harvest_skip",),
            ),
        ):
            block = format_live_dca_policy_snapshot("ZBT")
        self.assertIn("LIVE_DCA_POLICY", block)
        self.assertIn("ZBT/USDT", block)
        self.assertIn("harvest_skip", block)
        self.assertIn("SKIP", block)
        self.assertIn("spendable_dca=800", block)

    def test_ask_prompt_includes_snapshot(self):
        from hermes.memory.rag_retriever import RagRetriever
        from services.telegram_ask_bridge import _build_ask_rag_prompt

        r = RagRetriever.in_memory(
            config={"memory": {"rag": {"enabled": True, "embedding_backend": "hash"}}}
        )
        r.add_to_memory(
            "Trade ZBT/USDT sell pnl=10",
            {"type": "trade", "symbol": "ZBT/USDT", "source_id": "z1"},
        )
        with patch("intelligence.memory.rag_config.rag_enabled", return_value=True), patch(
            "strategies.dca_ask_snapshot.format_live_dca_policy_snapshot",
            return_value="LIVE_DCA_POLICY (test)\n  skip=True\n",
        ):
            prompt = _build_ask_rag_prompt(
                "warum ZBT verkauft und DCA?",
                {"symbol": "ZBT/USDT"},
                retriever=r,
            )
        self.assertIn("LIVE_DCA_POLICY", prompt)
        self.assertIn("RETRIEVED_MEMORY", prompt)


class TestTradeHistoryMemory(unittest.TestCase):
    def test_history_rows_map_to_memories(self):
        from intelligence.memory.rebuild import trade_history_to_trade_memories

        rows = [
            {
                "type": "BUY",
                "symbol": "ZBT/USDT",
                "price": 0.09,
                "amount": 100,
                "usdt_amount": 9,
                "source": "entry_sensor_15m",
                "timestamp": "2026-07-15T12:00:00",
                "order_id": "abc1",
            },
            {
                "type": "SELL",
                "symbol": "ZBT/USDT",
                "price": 0.10,
                "pnl": 32.8,
                "source": "grid",
                "timestamp": "2026-07-18T18:15:48",
                "order_id": "abc2",
            },
        ]
        mems = trade_history_to_trade_memories(
            rows, ledger_scope="demo", tenant_id="default", lookback_days=90
        )
        self.assertEqual(len(mems), 2)
        sells = [m for m in mems if m.direction == "sell"]
        self.assertEqual(len(sells), 1)
        self.assertAlmostEqual(sells[0].pnl_usdt or 0, 32.8)
        self.assertIn("th:", sells[0].trade_id)


if __name__ == "__main__":
    unittest.main()
