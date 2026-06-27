import unittest
from unittest.mock import MagicMock, patch

from services import webhook_watchdog as wd


class TestWebhookWatchdog(unittest.TestCase):
    def test_probe_webhook_url_accepts_200(self):
        resp = MagicMock(status_code=200)
        with patch("services.webhook_watchdog.requests.post", return_value=resp):
            self.assertTrue(wd.probe_webhook_url("https://example.ngrok-free.dev"))

    def test_probe_webhook_url_rejects_non_200(self):
        resp = MagicMock(status_code=404)
        with patch("services.webhook_watchdog.requests.post", return_value=resp):
            self.assertFalse(wd.probe_webhook_url("https://example.ngrok-free.dev"))

    def test_ensure_skips_reregister_when_probe_ok(self):
        info = {
            "url": "https://example.ngrok-free.dev/",
            "last_error_message": "Wrong response from the webhook: 404 Not Found",
        }
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok"}), \
             patch.object(wd, "get_ngrok_public_url", return_value="https://example.ngrok-free.dev"), \
             patch.object(wd, "probe_webhook_url", return_value=True), \
             patch("services.webhook_watchdog.requests.get") as mock_get, \
             patch("services.webhook_watchdog.requests.post") as mock_post:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {"result": info}
            self.assertTrue(wd.ensure_webhook_registered())
            mock_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()