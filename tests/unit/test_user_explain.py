import unittest

from notifications.user_explain import (
    explain_hermes_cycle,
    explain_rationale,
    explain_risk,
    explain_sell_tier,
    explain_trade,
    format_decision_entry,
)


class DummyAnalysis:
    def __init__(self, **kwargs):
        self.action = kwargs.get("action", "HOLD")
        self.normalized_action = kwargs.get("normalized_action", "HOLD")
        self.rationale = kwargs.get("rationale", "")
        self.sources = kwargs.get("sources", [])
        self.rsi = kwargs.get("rsi", 0)
        self.ampel_text = kwargs.get("ampel_text", "")
        self.confidence = kwargs.get("confidence", 0)


class DummyTradeResult:
    def __init__(self, executed=False, message=""):
        self.executed = executed
        self.message = message


class TestUserExplain(unittest.TestCase):
    def test_explain_rationale_sell_30(self):
        text = explain_rationale("TA→SELL_30 | TA: RSI=74.0 Vol=1.10x")
        self.assertIn("30 %", text)
        self.assertIn("überkauft", text.lower())

    def test_explain_rationale_cmc_buy(self):
        text = explain_rationale("CMC→BUY(82%)")
        self.assertIn("CMC", text)
        self.assertIn("82", text)

    def test_explain_risk_max_positions(self):
        de = explain_risk("Max open positions reached (5)")
        self.assertIn("offener Positionen", de)

    def test_explain_sell_tier_stop(self):
        self.assertIn("Verlustgrenze", explain_sell_tier("SELL_STOP_FULL"))

    def test_explain_trade_buy(self):
        analysis = DummyAnalysis(
            action="BUY",
            normalized_action="BUY",
            rationale="TA→BUY | CMC→BUY(80%)",
            sources=["technical", "cmc"],
            rsi=42.5,
        )
        result = explain_trade(analysis)
        self.assertIn("Kauf", result["why_de"])
        self.assertIn("RSI=42.5", result["tech_line"])
        self.assertIn("CMC", result["source_de"])

    def test_explain_trade_blocked(self):
        analysis = DummyAnalysis(action="BUY", rationale="TA→BUY", sources=["technical"])
        trade = DummyTradeResult(executed=False, message="Max open positions reached (5)")
        result = explain_trade(analysis, trade)
        self.assertIn("offener Positionen", result["blocks"]["risk_de"])

    def test_explain_hermes_rejected(self):
        record = {
            "verdict": "rejected",
            "variable": "rsi_sell_30",
            "old_value": 70,
            "new_value": 68,
            "symbol": "H/USDT",
            "verdict_reason": "Won 1/4 folds (25% < 55%)",
            "folds_won": 1,
            "folds_total": 4,
        }
        msg = explain_hermes_cycle(record)
        self.assertIn("abgelehnt", msg.lower())
        self.assertIn("1/4", msg)

    def test_explain_hermes_promoted(self):
        record = {
            "verdict": "promoted",
            "variable": "take_profit_pct",
            "old_value": 8,
            "new_value": 10,
            "symbol": "ARIA/USDT",
        }
        msg = explain_hermes_cycle(record)
        self.assertTrue("angepasst" in msg.lower() or "übernommen" in msg.lower())

    def test_format_decision_entry(self):
        entry = {
            "symbol": "H/USDT",
            "action": "SELL_30",
            "rationale": "TA→SELL_30",
            "executed": True,
            "timestamp": "2026-06-14T12:00:00",
        }
        line = format_decision_entry(entry)
        self.assertIn("H", line)
        self.assertIn("TA→SELL_30", line)


if __name__ == "__main__":
    unittest.main()