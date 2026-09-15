"""#397: /xai_login — operator-only trigger for the SuperGrok device-code login sidecar.

No network: urllib is patched at module level; the sidecar reply is a fixture.
"""

from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

from notifications.telegram_commands import xai_auth_commands as xc
from notifications.telegram_commands.router import dispatch_command

OPERATOR = "111222333"
SIDECAR = "http://xagent-xai-auth.railway.internal:8787"
TRIGGER = "trigger-secret-0123456789"
ACCESS = "ACCESS_TOKEN_SUPER_SECRET_0123456789abcdef"

MOD = "notifications.telegram_commands.xai_auth_commands"


class _Resp:
    def __init__(self, status: int, body: dict):
        self.status = status
        self._raw = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code: int, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("u", code, "err", {}, io.BytesIO(json.dumps(body).encode("utf-8")))


def _status_body(present: bool = True) -> dict:
    cred = (
        {"present": True, "expiresAt": "2026-09-15T13:00:00.000Z", "expiresInSec": 3600, "expiringSoon": False}
        if present
        else {"present": False}
    )
    return {
        "ok": True,
        "service": "xai-auth-sidecar",
        "credential": cred,
        "login": {"running": False, "lastResult": "ok", "lastError": None, "startedAt": None},
        "keepalive": {"enabled": True, "failures": 0},
        "triggerEnabled": True,
        # a misbehaving sidecar must not be able to smuggle a token into the chat
        "access": ACCESS,
    }


class _Base(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {"TELEGRAM_CHAT_ID": OPERATOR, xc.SIDECAR_URL_VAR: SIDECAR, xc.TRIGGER_TOKEN_VAR: TRIGGER},
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.chat = patch(f"{MOD}._is_operator", return_value=True)
        self.chat.start()
        self.addCleanup(self.chat.stop)
        self.send = patch(f"{MOD}.send_telegram_message", return_value=True)
        self.sent = self.send.start()
        self.addCleanup(self.send.stop)

    def last_text(self) -> str:
        return self.sent.call_args[0][0]


class TestGate(_Base):
    def test_not_our_command(self):
        self.assertFalse(xc.handle("/xai"))
        self.assertFalse(xc.handle("/xai_loginx"))
        self.assertFalse(xc.handle("xai_login"))
        self.sent.assert_not_called()

    def test_non_operator_is_refused_without_touching_sidecar(self):
        with patch(f"{MOD}._is_operator", return_value=False), patch(f"{MOD}._request") as req:
            self.assertTrue(xc.handle("/xai_login"))
            self.assertTrue(xc.handle("/xai_login status"))
            req.assert_not_called()
        self.assertIn("operator only", self.last_text())

    def test_operator_gate_is_the_onboarding_gate(self):
        from notifications.telegram_commands import onboarding_commands as oc

        self.chat.stop()  # look at the real attribute, not the setUp mock
        self.assertIs(xc._is_operator, oc._is_operator)

    def test_unconfigured_explains_and_never_calls_out(self):
        with patch.dict(os.environ, {xc.SIDECAR_URL_VAR: ""}), patch(f"{MOD}._request") as req:
            self.assertTrue(xc.handle("/xai_login"))
            req.assert_not_called()
        text = self.last_text()
        self.assertIn("not configured", text)
        self.assertIn(xc.SIDECAR_URL_VAR, text)
        self.assertIn("railway ssh", text)

    def test_dispatch_routes_to_handler_not_unknown(self):
        with patch(f"{MOD}._request", return_value=(200, _status_body())):
            self.assertTrue(dispatch_command("/xai_login status"))
        self.assertIn("SuperGrok session", self.last_text())
        with patch(f"{MOD}._request", return_value=(202, {"ok": True, "started": True, "telegram": True})):
            self.assertTrue(dispatch_command("/xai_login@XAgentBot"))
        self.assertIn("login started", self.last_text())


class TestStatus(_Base):
    def test_status_present(self):
        with patch(f"{MOD}._request", return_value=(200, _status_body())) as req:
            self.assertTrue(xc.handle("/xai_login status"))
            req.assert_called_once_with("GET", "/status")
        text = self.last_text()
        self.assertIn("stored, expires 2026-09-15T13:00:00.000Z", text)
        self.assertIn("Last login: ok", text)
        self.assertIn("Keepalive: on", text)
        self.assertNotIn(ACCESS, text)

    def test_status_absent_and_running(self):
        body = _status_body(present=False)
        body["login"] = {"running": True, "startedAt": "2026-09-15T12:00:00.000Z"}
        body["keepalive"] = {"enabled": True, "failures": 2}
        body["triggerEnabled"] = False
        with patch(f"{MOD}._request", return_value=(200, body)):
            xc.handle("/xai_login status")
        text = self.last_text()
        self.assertIn("none", text)
        self.assertIn("running since 2026-09-15T12:00:00.000Z", text)
        self.assertIn("2 refresh failure(s)", text)
        self.assertIn("Trigger: disabled", text)

    def test_status_unreachable(self):
        with patch(f"{MOD}._request", return_value=(0, {"error": "URLError"})):
            xc.handle("/xai_login status")
        self.assertIn("unreachable", self.last_text())


class TestTrigger(_Base):
    def test_202_started_with_telegram(self):
        with patch(f"{MOD}._request", return_value=(202, {"ok": True, "started": True, "running": True, "telegram": True})) as req:
            self.assertTrue(xc.handle("/xai_login"))
            req.assert_called_once_with("POST", "/login", token=TRIGGER)
        text = self.last_text()
        self.assertIn("login started", text)
        self.assertIn("separate message", text)

    def test_202_started_without_sidecar_telegram_points_to_logs(self):
        with patch(f"{MOD}._request", return_value=(202, {"ok": True, "started": True, "telegram": False})):
            xc.handle("/xai_login")
        self.assertIn("Railway logs", self.last_text())

    def test_409_403_401_0_other(self):
        cases = {
            409: "already running",
            403: "trigger disabled",
            401: "differs between services",
            0: "unreachable",
            500: "HTTP 500",
        }
        for code, expect in cases.items():
            with patch(f"{MOD}._request", return_value=(code, {"ok": False, "error": "boom"})):
                self.assertTrue(xc.handle("/xai_login"))
            self.assertIn(expect, self.last_text(), msg=f"code {code}")

    def test_missing_trigger_token_on_bot_side(self):
        with patch.dict(os.environ, {xc.TRIGGER_TOKEN_VAR: ""}), patch(f"{MOD}._request") as req:
            self.assertTrue(xc.handle("/xai_login"))
            req.assert_not_called()
        self.assertIn(xc.TRIGGER_TOKEN_VAR, self.last_text())


class TestRequestTransport(_Base):
    def test_ok_response_and_bearer_header(self):
        seen = {}

        def fake_urlopen(req, timeout):
            seen["url"] = req.full_url
            seen["method"] = req.get_method()
            seen["auth"] = req.get_header("Authorization")
            seen["timeout"] = timeout
            return _Resp(202, {"ok": True})

        with patch(f"{MOD}.urllib.request.urlopen", side_effect=fake_urlopen):
            code, body = xc._request("POST", "/login", token=TRIGGER)
        self.assertEqual((code, body), (202, {"ok": True}))
        self.assertEqual(seen["url"], f"{SIDECAR}/login")
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["auth"], f"Bearer {TRIGGER}")
        self.assertEqual(seen["timeout"], xc._TIMEOUT_SEC)

    def test_get_without_token_has_no_auth_header_and_trailing_slash_is_stripped(self):
        seen = {}

        def fake_urlopen(req, timeout):
            seen["url"] = req.full_url
            seen["auth"] = req.get_header("Authorization")
            return _Resp(200, {"ok": True})

        with patch.dict(os.environ, {xc.SIDECAR_URL_VAR: SIDECAR + "/"}), patch(
            f"{MOD}.urllib.request.urlopen", side_effect=fake_urlopen
        ):
            xc._request("GET", "/status")
        self.assertEqual(seen["url"], f"{SIDECAR}/status")
        self.assertIsNone(seen["auth"])

    def test_http_error_returns_code_and_body(self):
        with patch(f"{MOD}.urllib.request.urlopen", side_effect=_http_error(409, {"running": True})):
            self.assertEqual(xc._request("POST", "/login", token=TRIGGER), (409, {"running": True}))

    def test_transport_error_returns_zero(self):
        with patch(f"{MOD}.urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            code, body = xc._request("GET", "/status")
        self.assertEqual(code, 0)
        self.assertEqual(body["error"], "URLError")

    def test_non_dict_json_is_ignored(self):
        with patch(f"{MOD}.urllib.request.urlopen", return_value=_Resp(200, ["not", "a", "dict"])):
            self.assertEqual(xc._request("GET", "/status"), (200, {}))


if __name__ == "__main__":
    unittest.main()
