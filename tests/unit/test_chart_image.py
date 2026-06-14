import unittest
from unittest.mock import MagicMock, patch

import pandas as pd


class TestChartImage(unittest.TestCase):
    @patch("notifications.chart_image.coin_links_config")
    @patch("services.market_service.MarketService._fetch_ohlcv")
    def test_render_chart_returns_path(self, mock_fetch, mock_cfg):
        pytest = __import__("pytest")
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            pytest.skip("matplotlib not installed")

        mock_cfg.return_value = {"chart_bars": 10, "chart_timeframe": "4h"}
        mock_fetch.return_value = pd.DataFrame({
            "close": [1.0, 1.1, 1.2, 1.15, 1.3],
            "low": [0.9, 1.0, 1.1, 1.05, 1.2],
            "high": [1.1, 1.2, 1.3, 1.25, 1.4],
        })
        from notifications.chart_image import render_ohlcv_chart_png
        import os

        path = render_ohlcv_chart_png("BTC/USDT", bars=5, current_price=1.25)
        self.assertIsNotNone(path)
        self.assertTrue(os.path.isfile(path))
        os.unlink(path)

    @patch("notifications.chart_image.coin_links_config")
    def test_skips_when_not_executed(self, mock_cfg):
        from notifications.chart_image import send_trade_chart_if_enabled

        mock_cfg.return_value = {"chart_image_on_executed_trades": True}
        self.assertFalse(send_trade_chart_if_enabled("H/USDT", executed=False))


if __name__ == "__main__":
    unittest.main()