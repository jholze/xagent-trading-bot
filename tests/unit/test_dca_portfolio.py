import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.actions import BUY_DCA
from core.models import MarketContext
from strategies.dca import DCACandidate
from strategies.dca_portfolio import (
    _build_market,
    _target_priority,
    build_portfolio_dca_plan,
    find_funding_sell,
    portfolio_config,
)
from strategies.positions import get_key, get_position, positions, update_position


class TestDCAPortfolio(unittest.TestCase):
    def setUp(self):
        self._backup = dict(positions)
        positions.clear()

    def tearDown(self):
        positions.clear()
        positions.update(self._backup)

    def test_target_priority_favors_higher_score(self):
        hi = DCACandidate(BUY_DCA, "dca", "x", 200, score=9)
        lo = DCACandidate(BUY_DCA, "dca", "x", 200, score=6)
        self.assertGreater(_target_priority(hi, -10), _target_priority(lo, -10))

    def test_find_funding_sell_tail_idle(self):
        update_position("TAIL/USDT", "4h", "BUY", 1.0, 500)
        tail = get_position("TAIL/USDT", "4h")
        tail["sold_percent"] = 0.6
        tail["average_entry"] = 1.0
        tail["last_trade_at"] = (datetime.now() - timedelta(hours=30)).isoformat()
        tail["last_trade_type"] = "SELL"

        update_position("TARGET/USDT", "4h", "BUY", 1.0, 400)
        tgt = get_position("TARGET/USDT", "4h")
        tgt["average_entry"] = 1.0

        from strategies.dca_portfolio import DCATarget

        target = DCATarget(
            symbol="TARGET/USDT",
            timeframe="4h",
            source="dca",
            candidate=DCACandidate(BUY_DCA, "dca", "dip", 350, score=8),
            priority=10,
            usdt_needed=350,
            loss_pct=-8,
            score=8,
        )
        coins = [{"symbol": "TAIL/USDT"}, {"symbol": "TARGET/USDT"}]
        price_map = {"TAIL/USDT": 1.05, "TARGET/USDT": 0.92}

        with patch("strategies.dca_portfolio._build_market") as mock_market:
            mock_market.side_effect = lambda sym, tf, price, pos, sp: MarketContext(
                symbol=sym,
                timeframe=tf,
                current_price=price,
                rsi=45,
                lower_bb=price * 0.9,
                atr_pct=3,
                has_position=True,
                average_entry=float(pos.get("average_entry", price)),
                open_positions=1,
                strategy_params=sp or {},
            )
            funding = find_funding_sell(
                target, coins, price_map, cash_available=50, cash_needed=350, config_raw={},
            )
        self.assertIsNotNone(funding)
        self.assertEqual(funding.symbol, "TAIL/USDT")

    def test_portfolio_config_defaults(self):
        cfg = portfolio_config({}, config_raw={})
        self.assertIn("enabled", cfg)
        self.assertIn("cash_buffer_usdt", cfg)
        self.assertFalse(cfg["enabled"])

    def test_portfolio_config_inherits_global_when_coin_missing_portfolio(self):
        config_raw = {
            "volatile_altcoin": {
                "dca": {
                    "portfolio": {
                        "enabled": True,
                        "mode": "live",
                        "min_dca_score": 6,
                        "cash_buffer_usdt": 400,
                    }
                }
            }
        }
        cfg = portfolio_config({"enabled": True, "mode": "live"}, config_raw=config_raw)
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["mode"], "live")
        self.assertEqual(cfg["min_dca_score"], 6)
        self.assertEqual(cfg["cash_buffer_usdt"], 400)

    def test_portfolio_config_coin_can_disable_global(self):
        config_raw = {
            "volatile_altcoin": {
                "dca": {"portfolio": {"enabled": True, "mode": "live"}},
            }
        }
        cfg = portfolio_config({"portfolio": {"enabled": False}}, config_raw=config_raw)
        self.assertFalse(cfg["enabled"])

    def test_collect_dca_targets_includes_coin_without_per_coin_portfolio_block(self):
        from strategies.dca_portfolio import collect_dca_targets

        update_position("GNC/USDT", "1h", "BUY", 1000.0, 360.0)
        pos = get_position("GNC/USDT", "1h")
        pos["average_entry"] = 1.0

        coins = [{"symbol": "GNC/USDT", "timeframe": "1h", "source": "cmc_trending", "active": True}]
        price_map = {"GNC/USDT": 0.85}
        config_raw = {
            "volatile_altcoin": {
                "dca": {
                    "portfolio": {"enabled": True, "mode": "live", "min_dca_score": 6},
                }
            }
        }
        candidate = DCACandidate(BUY_DCA, "dca", "dip", 360.0, score=7)

        with patch("strategies.dca_portfolio.evaluate_dca_addon", return_value=candidate), patch(
            "strategies.dca_portfolio._build_market",
            return_value=MarketContext(
                symbol="GNC/USDT",
                timeframe="1h",
                current_price=0.85,
                rsi=35.0,
                lower_bb=0.8,
                atr_pct=3.0,
                has_position=True,
                average_entry=1.0,
                open_positions=1,
                strategy_params={},
            ),
        ):
            targets = collect_dca_targets(coins, price_map, config_raw=config_raw)

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].symbol, "GNC/USDT")
        self.assertEqual(targets[0].score, 7)

    def test_build_market_fetches_funding_and_btc_underperf(self):
        strategy_params = {
            "dca": {
                "enabled": True,
                "scoring": {"enabled": True, "btc_lookback_hours": 8},
            },
        }
        position = {"average_entry": 1.0, "amount": 100.0}
        with patch("services.market_service.MarketService") as mock_cls:
            svc = mock_cls.return_value
            svc.fetch_indicators.return_value = {
                "rsi": 28.0,
                "lower_bb": 0.9,
                "atr_pct": 3.0,
            }
            svc.fetch_funding_rate.return_value = -0.04
            svc.btc_underperformance_ratio.return_value = 2.0
            ctx = _build_market("ARIA/USDT", "4h", 0.92, position, strategy_params)

        svc.fetch_funding_rate.assert_called_once_with("ARIA/USDT")
        svc.btc_underperformance_ratio.assert_called_once_with("ARIA/USDT", "4h", lookback_hours=8)
        self.assertEqual(ctx.funding_rate_pct, -0.04)
        self.assertEqual(ctx.btc_underperf_ratio, 2.0)


if __name__ == "__main__":
    unittest.main()