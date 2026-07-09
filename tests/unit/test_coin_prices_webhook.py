import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from aria_bot import app


class TestCoinPricesWebhook(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_get_coin_prices(self):
        with patch("services.coin_query_service.query_coin_prices") as mock_query, \
             patch("services.coin_query_service.webhook_token_ok", return_value=True), \
             patch("core.config.get_bot_config") as mock_cfg:
            from services.coin_query_service import CoinPriceResult, CoinQueryResponse

            mock_cfg.return_value.architecture_config = {"coin_query_webhook_enabled": True}
            mock_cfg.return_value.raw = {"architecture": {}}
            mock_query.return_value = CoinQueryResponse(
                prices={"BTC/USDT": CoinPriceResult("BTC/USDT", 64000.0, "redis", 3.0)},
                redis_available=True,
                cache_hits=1,
                fetched=0,
            )
            resp = self.client.get("/api/coins/prices?symbols=BTC")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["redis_available"])
        self.assertAlmostEqual(data["prices"]["BTC/USDT"]["price"], 64000.0)

    def test_unauthorized_when_token_required(self):
        with patch("services.coin_query_service.webhook_token_ok", return_value=False), \
             patch("core.config.get_bot_config") as mock_cfg:
            mock_cfg.return_value.architecture_config = {"coin_query_webhook_enabled": True}
            mock_cfg.return_value.raw = {"architecture": {}}
            resp = self.client.get("/api/coins/prices?symbols=BTC&token=bad")
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()