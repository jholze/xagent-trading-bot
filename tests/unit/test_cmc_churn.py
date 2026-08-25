import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from data.cmc_community_provider import CMCCommunityParser, CMCCommunitySignal, CMCProApiProvider, RawCMCPost
from data_manager import load_cmc_posts, save_cmc_posts
from hermes.cmc_replay import active_signals_for_symbols, signals_at_timestamp
from risk.risk_manager import RiskManager
from strategies.decision_engine import DecisionEngine
from core.models import TradeOrder


class TestCMCChurn(unittest.TestCase):
    def test_quote_post_id_stable_per_day(self):
        provider = CMCProApiProvider(api_key="test")
        with patch.object(provider, "_quote_thresholds", return_value=(-8.0, 5.0)):
            pid_a = provider._quote_post_id("STG", -7.5)
            pid_b = provider._quote_post_id("STG", -9.2)
            pid_c = provider._quote_post_id("STG", 6.0)
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.assertEqual(pid_a, f"cmc_quote_STG_neutral_{date}")
        self.assertEqual(pid_b, f"cmc_quote_STG_bear_{date}")
        self.assertEqual(pid_c, f"cmc_quote_STG_bull_{date}")

    def test_quote_fetch_skips_extreme_pump_pct(self):
        provider = CMCProApiProvider(api_key="test")
        fake_data = {
            "BOBO": {
                "quote": {"USDT": {"percent_change_24h": 199520.7, "price": 0.001, "volume_24h": 1}},
            },
            "STG": {
                "quote": {"USDT": {"percent_change_24h": 8.0, "price": 0.2, "volume_24h": 100000}},
            },
        }
        with patch.object(provider, "_quote_thresholds", return_value=(-8.0, 5.0)), \
             patch("data_manager.get_config", return_value={"cmc": {"quotes_max_bullish_pct": 35}}), \
             patch("data.cmc_community_provider.requests.get") as mock_get:
            mock_resp = mock_get.return_value
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"data": fake_data}
            posts = provider._fetch_quotes_sentiment(["BOBO", "STG"], limit=10)
        coins = [p.coin for p in posts]
        self.assertNotIn("BOBO", coins)
        self.assertIn("STG", coins)

    def test_parser_bearish_threshold_tightened(self):
        parser = CMCCommunityParser()
        hold_post = RawCMCPost("p1", "STG", "neutral", votes_bullish=32, votes_bearish=68)
        sell_post = RawCMCPost("p2", "STG", "bearish", votes_bullish=20, votes_bearish=80)
        self.assertEqual(parser.parse(hold_post).action, "HOLD")
        self.assertEqual(parser.parse(sell_post).action, "SELL")

    def test_cmc_sell_threshold_higher_than_buy(self):
        engine = DecisionEngine()
        buy = engine._cmc_buy_threshold({})
        sell = engine._cmc_sell_threshold({})
        self.assertGreater(sell, buy)

    def test_cmc_sell_requires_ta_blocks_pure_cmc(self):
        engine = DecisionEngine()
        cmc = CMCCommunitySignal("STG", "SELL", 90, votes_bullish=10, votes_bearish=90)
        pos = {"amount": 1000, "average_entry": 1.0}
        with patch.object(engine.market, "fetch_indicators", return_value={"rsi": 50.0, "lower_bb": 0.9, "vol_multiplier": 1.0}), \
             patch("strategies.decision_engine.get_position", return_value=pos):
            analysis = engine.evaluate(
                {"symbol": "STG/USDT", "timeframe": "4h", "source": "cmc_trending"},
                1.0,
                cmc_signals=[cmc],
            )
        self.assertNotIn("cmc", analysis.sources)

    def test_social_sell_min_notional_block(self):
        risk = RiskManager()
        order = TradeOrder(type="SELL", symbol="STG/USDT", price=1.0, amount=3.0, signal="SELL_PARTIAL_20")
        pos_patch = patch(
            "risk.risk_manager.get_position",
            return_value={"amount": 100.0, "average_entry": 1.0, "sold_percent": 0},
        )
        with pos_patch:
            blocked, reason = risk._social_sell_blocked(order, "4h", source="cmc")
        self.assertTrue(blocked)
        self.assertIn("notional", reason.lower())

    def test_auto_partial_sell_min_notional_block(self):
        risk = RiskManager()
        order = TradeOrder(type="SELL", symbol="STG/USDT", price=1.0, amount=8.0, signal="SELL_PARTIAL_30")
        pos_patch = patch(
            "risk.risk_manager.get_position",
            return_value={"amount": 40.0, "average_entry": 1.0, "sold_percent": 0.5},
        )
        with pos_patch:
            blocked, reason = risk._partial_sell_blocked(order, "4h", source="auto")
        self.assertTrue(blocked)
        self.assertIn("notional", reason.lower())

    def test_dust_sweep_upgrades_partial_to_full(self):
        risk = RiskManager()
        order = TradeOrder(
            type="SELL",
            symbol="HIGH/USDT",
            price=0.05,
            amount=60.0,
            signal="SELL_PARTIAL_20",
        )
        pos_patch = patch(
            "risk.risk_manager.get_position",
            return_value={"amount": 100.0, "average_entry": 0.05, "sold_percent": 0.97},
        )
        with pos_patch:
            resolved = risk._resolve_sell_order(order, "4h", source="auto")
        self.assertEqual(resolved.signal, "SELL_FULL")
        self.assertEqual(resolved.amount, 100.0)

    def test_partial_sell_blocked_after_max_sold_percent(self):
        risk = RiskManager()
        order = TradeOrder(
            type="SELL",
            symbol="TRX/USDT",
            price=1.0,
            amount=50.0,
            signal="SELL_PARTIAL_20",
        )
        pos_patch = patch(
            "risk.risk_manager.get_position",
            return_value={"amount": 200.0, "average_entry": 1.0, "sold_percent": 0.80},
        )
        limits = {
            "min_position_usdt": 1.0,
            "min_notional_usdt": 1.0,
            "max_sold_percent": 0.75,
            "dust_sweep_max_position_usdt": 15,
            "dust_sweep_sold_percent_min": 0.70,
            "dust_sweep_min_remainder_usdt": 10,
        }
        with pos_patch, patch.object(risk, "_partial_sell_limits", return_value=limits), patch(
            "strategies.exit_ladder.ladder_enabled", return_value=False
        ):
            blocked, reason = risk._partial_sell_blocked(order, "4h", source="cmc")
        self.assertTrue(blocked)
        self.assertIn("already sold", reason.lower())

    def test_refresh_cmc_signals_uses_ttl(self):
        from services.social_pipeline import SocialPipeline
        from x_analyzer import XAnalyzer

        backup = load_cmc_posts()
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        post_id = f"cmc_trend_STG_{date}"
        save_cmc_posts({
            "posts": [{
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "post_id": post_id,
                "coin": "STG",
                "action": "SELL",
                "confidence": 80,
                "rationale": "bearish",
                "votes_bullish": 10,
                "votes_bearish": 90,
                "source": "cmc",
            }]
        })
        try:
            pipeline = SocialPipeline(XAnalyzer())
            pipeline._cycle_cmc_signals = [
                type("S", (), {"coin": "STG", "confidence": 80, "quotes_fallback": False})()
            ]
            with patch(
                "services.social_pipeline.load_effective_watchlist",
                return_value=[{"symbol": "STG/USDT"}],
            ):
                signals = pipeline.refresh_cmc_signals()
            coins = {getattr(s, "coin", "") for s in signals}
            self.assertIn("STG", coins)
        finally:
            save_cmc_posts(backup)

    def test_active_signals_strongest_per_symbol(self):
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        posts = [
            {
                "_ts_ms": now_ms - 1000,
                "post_id": "a",
                "coin": "STG",
                "action": "SELL",
                "confidence": 70,
            },
            {
                "_ts_ms": now_ms - 2000,
                "post_id": "b",
                "coin": "STG",
                "action": "SELL",
                "confidence": 85,
            },
        ]
        active = signals_at_timestamp(posts, now_ms, ttl_ms=4 * 3600 * 1000)
        self.assertEqual(active[0].confidence, 85)


if __name__ == "__main__":
    unittest.main()