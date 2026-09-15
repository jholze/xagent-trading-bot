"""#397 Phase 2: xAI calls behind ``XAI_USE_SUBSCRIPTION`` (sidecar /v1 + trigger token) with
one-shot fallback to the metered ``XAI_API_KEY``. Flag off (default) must be today's behaviour.

No network: every OpenAI client is a mock; the sidecar itself is tested in
``services/xai_auth_sidecar/test/server.test.js``.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError

import grok_x_search
from intelligence import llm_client, xai_auth
from intelligence.llm_client import LlmError, ask_llm, ask_llm_json, llm_settings, reset_llm_clients
from intelligence.xai_auth import (
    FALLBACK_LOG_MARKER,
    XAI_DIRECT_BASE_URL,
    XaiEndpoint,
    direct_endpoint,
    fallback_endpoint_after,
    is_subscription_fallback_error,
    resolve_xai_endpoint,
    sidecar_base_url,
    subscription_endpoint,
)

SIDECAR = "http://xagent-xai-auth.railway.internal:8080"
TRIGGER = "trigger-secret-0123456789"
METERED = "xai-metered-key"
_FLAG_VARS = ("XAI_USE_SUBSCRIPTION", "XAI_AUTH_SIDECAR_URL", "XAI_AUTH_TRIGGER_TOKEN", "XAI_API_KEY", "LLM_BACKEND", "LLM_API_KEY", "LLM_BASE_URL")


def _status_error(code: int) -> APIStatusError:
    req = httpx.Request("POST", f"{SIDECAR}/v1/chat/completions")
    return APIStatusError(f"sidecar {code}", response=httpx.Response(code, request=req), body={"error": "no_credential"})


def _conn_error() -> APIConnectionError:
    return APIConnectionError(request=httpx.Request("POST", f"{SIDECAR}/v1/chat/completions"))


def _timeout_error() -> APITimeoutError:
    return APITimeoutError(request=httpx.Request("POST", f"{SIDECAR}/v1/chat/completions"))


def _chat_client(content: str = '{"a": 1}') -> MagicMock:
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=content))]
    client = MagicMock()
    client.chat.completions.create.return_value = resp
    return client


def _responses_client(text: str = '[{"post_id":"1","text":"Long ETH","created_at":"2026-06-01T10:00:00Z"}]') -> MagicMock:
    resp = MagicMock()
    resp.output = [MagicMock(type="message", content=[MagicMock(text=text)])]
    client = MagicMock()
    client.responses.create.return_value = resp
    return client


class _EnvCase(unittest.TestCase):
    def setUp(self):
        reset_llm_clients()
        self._env = dict(os.environ)
        for k in _FLAG_VARS:
            os.environ.pop(k, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        reset_llm_clients()

    def _flag_on(self, url: str = SIDECAR, token: str = TRIGGER, metered: str | None = METERED):
        os.environ["XAI_USE_SUBSCRIPTION"] = "1"
        os.environ["XAI_AUTH_SIDECAR_URL"] = url
        os.environ["XAI_AUTH_TRIGGER_TOKEN"] = token
        if metered is not None:
            os.environ["XAI_API_KEY"] = metered


class TestEndpointResolution(_EnvCase):
    def test_flag_off_by_default_is_metered_api_x_ai(self):
        os.environ["XAI_API_KEY"] = METERED
        os.environ["XAI_AUTH_SIDECAR_URL"] = SIDECAR
        os.environ["XAI_AUTH_TRIGGER_TOKEN"] = TRIGGER
        ep = resolve_xai_endpoint()
        self.assertEqual(ep, XaiEndpoint(base_url=XAI_DIRECT_BASE_URL, api_key=METERED, via_subscription=False))
        self.assertIsNone(subscription_endpoint())
        for off in ("0", "false", "no", "off", "", "  "):
            os.environ["XAI_USE_SUBSCRIPTION"] = off
            self.assertFalse(resolve_xai_endpoint().via_subscription, off)

    def test_flag_on_uses_sidecar_v1_and_trigger_token(self):
        self._flag_on()
        ep = resolve_xai_endpoint()
        self.assertEqual(ep.base_url, f"{SIDECAR}/v1")
        self.assertEqual(ep.api_key, TRIGGER, "the trigger token is the bearer, never the OAuth access token")
        self.assertTrue(ep.via_subscription)
        for on in ("true", "YES", " on "):
            os.environ["XAI_USE_SUBSCRIPTION"] = on
            self.assertTrue(resolve_xai_endpoint().via_subscription, on)

    def test_sidecar_url_normalisation(self):
        os.environ["XAI_AUTH_SIDECAR_URL"] = f"{SIDECAR}/"
        self.assertEqual(sidecar_base_url(), f"{SIDECAR}/v1")
        os.environ["XAI_AUTH_SIDECAR_URL"] = f"{SIDECAR}/v1/"
        self.assertEqual(sidecar_base_url(), f"{SIDECAR}/v1", "does not double /v1")
        os.environ["XAI_AUTH_SIDECAR_URL"] = "  "
        self.assertEqual(sidecar_base_url(), "")

    def test_flag_on_but_incomplete_config_falls_back_to_metered(self):
        with patch.object(xai_auth, "log") as mock_log:
            self._flag_on(url="", metered=METERED)
            self.assertEqual(resolve_xai_endpoint(), direct_endpoint())
            self._flag_on(token="  ")
            self.assertEqual(resolve_xai_endpoint(), direct_endpoint())
        self.assertTrue(mock_log.called)
        self.assertFalse(resolve_xai_endpoint().via_subscription)

    def test_fallback_error_classes(self):
        for code in (401, 403, 503):
            self.assertTrue(is_subscription_fallback_error(_status_error(code)), code)
        for code in (400, 404, 429, 500, 502):
            self.assertFalse(is_subscription_fallback_error(_status_error(code)), code)
        self.assertTrue(is_subscription_fallback_error(_conn_error()))
        self.assertFalse(is_subscription_fallback_error(_timeout_error()), "timeouts are not retried on the metered key")
        self.assertFalse(is_subscription_fallback_error(RuntimeError("x")))

    def test_fallback_endpoint_only_when_call_went_via_sidecar(self):
        os.environ["XAI_API_KEY"] = METERED
        sidecar = XaiEndpoint(base_url=f"{SIDECAR}/v1", api_key=TRIGGER, via_subscription=True)
        direct = direct_endpoint()
        with patch.object(xai_auth, "log") as mock_log:
            self.assertEqual(fallback_endpoint_after(_status_error(503), sidecar), direct)
            self.assertIsNone(fallback_endpoint_after(_status_error(503), direct), "a metered 503 is not a fallback")
            self.assertIsNone(fallback_endpoint_after(_status_error(500), sidecar))
        self.assertEqual(mock_log.call_count, 1)
        msg = mock_log.call_args.args[0]
        self.assertIn(FALLBACK_LOG_MARKER, msg)
        self.assertIn("HTTP 503", msg)


class TestLlmClientRouting(_EnvCase):
    def test_flag_off_settings_unchanged(self):
        os.environ["XAI_API_KEY"] = METERED
        s = llm_settings()
        self.assertEqual((s.backend, s.base_url, s.api_key, s.model, s.via_subscription), ("xai", XAI_DIRECT_BASE_URL, METERED, "grok-4", False))

    def test_flag_on_settings_point_at_sidecar(self):
        self._flag_on()
        s = llm_settings()
        self.assertEqual((s.base_url, s.api_key, s.model, s.via_subscription), (f"{SIDECAR}/v1", TRIGGER, "grok-4", True))

    def test_flag_on_client_constructed_with_sidecar_and_trigger(self):
        self._flag_on()
        client = _chat_client()
        with patch.object(llm_client, "OpenAI", return_value=client) as mock_openai:
            data = ask_llm_json("json please", retries=0, required_keys=["a"])
        self.assertEqual(data, {"a": 1})
        mock_openai.assert_called_once()
        kw = mock_openai.call_args.kwargs
        self.assertEqual(kw["base_url"], f"{SIDECAR}/v1")
        self.assertEqual(kw["api_key"], TRIGGER)

    def test_flag_off_client_constructed_with_api_x_ai_and_metered_key(self):
        os.environ["XAI_API_KEY"] = METERED
        with patch.object(llm_client, "OpenAI", return_value=_chat_client("ok")) as mock_openai:
            out = ask_llm("hi")
        self.assertEqual(out, "ok")
        kw = mock_openai.call_args.kwargs
        self.assertEqual((kw["base_url"], kw["api_key"]), (XAI_DIRECT_BASE_URL, METERED))

    def test_sidecar_503_falls_back_once_to_metered_and_logs(self):
        self._flag_on()
        sidecar_client = MagicMock()
        sidecar_client.chat.completions.create.side_effect = _status_error(503)
        metered_client = _chat_client('{"a": 2}')
        constructed: list[dict] = []

        def fake_openai(**kw):
            constructed.append(kw)
            return sidecar_client if kw["base_url"].startswith(SIDECAR) else metered_client

        with patch.object(llm_client, "OpenAI", side_effect=fake_openai), patch.object(xai_auth, "log") as mock_log:
            data = ask_llm_json("json please", retries=0, required_keys=["a"])
        self.assertEqual(data, {"a": 2})
        self.assertEqual([(c["base_url"], c["api_key"]) for c in constructed], [(f"{SIDECAR}/v1", TRIGGER), (XAI_DIRECT_BASE_URL, METERED)])
        self.assertEqual(sidecar_client.chat.completions.create.call_count, 1, "sidecar is not retried")
        self.assertEqual(metered_client.chat.completions.create.call_count, 1)
        self.assertIn(FALLBACK_LOG_MARKER, mock_log.call_args.args[0])

    def test_sidecar_connection_error_falls_back_but_timeout_does_not(self):
        self._flag_on()
        for exc, expect_fallback in ((_conn_error(), True), (_timeout_error(), False), (_status_error(401), True), (_status_error(500), False)):
            reset_llm_clients()
            sidecar_client = MagicMock()
            sidecar_client.chat.completions.create.side_effect = exc
            metered_client = _chat_client("fallback-ok")
            fake = lambda **kw: sidecar_client if kw["base_url"].startswith(SIDECAR) else metered_client  # noqa: E731
            with patch.object(llm_client, "OpenAI", side_effect=fake), patch.object(xai_auth, "log"):
                out = ask_llm("hi")
            if expect_fallback:
                self.assertEqual(out, "fallback-ok", type(exc).__name__)
            else:
                self.assertTrue(out.startswith("API-Fehler:"), type(exc).__name__)
                self.assertEqual(metered_client.chat.completions.create.call_count, 0)

    def test_sidecar_503_without_metered_key_fails_as_today(self):
        self._flag_on(metered=None)
        sidecar_client = MagicMock()
        sidecar_client.chat.completions.create.side_effect = _status_error(503)
        with patch.object(llm_client, "OpenAI", return_value=sidecar_client) as mock_openai, patch.object(xai_auth, "log") as mock_log:
            with self.assertRaises(LlmError) as ctx:
                ask_llm_json("json please", retries=0)
        self.assertIn("XAI_API_KEY not set", str(ctx.exception))
        self.assertEqual(mock_openai.call_count, 1, "no metered client without a key")
        self.assertIn("XAI_API_KEY not set", mock_log.call_args.args[0])

    def test_openai_compat_backend_ignores_flag(self):
        self._flag_on()
        os.environ["LLM_BACKEND"] = "openai_compat"
        os.environ["LLM_BASE_URL"] = "http://localhost:8000/v1"
        os.environ["LLM_API_KEY"] = "local-key"
        s = llm_settings()
        self.assertEqual((s.base_url, s.api_key, s.via_subscription), ("http://localhost:8000/v1", "local-key", False))


class TestGrokXSearchRouting(_EnvCase):
    def test_flag_off_no_key_returns_empty_as_today(self):
        with patch.object(grok_x_search, "OpenAI") as mock_openai:
            self.assertEqual(grok_x_search.fetch_posts_from_handle("Pentosh1"), [])
        mock_openai.assert_not_called()

    def test_flag_off_uses_api_x_ai_and_metered_key(self):
        os.environ["XAI_API_KEY"] = METERED
        with patch.object(grok_x_search, "OpenAI", return_value=_responses_client()) as mock_openai:
            posts = grok_x_search.fetch_posts_from_handle("Pentosh1", days=7, max_posts=5)
        self.assertEqual(len(posts), 1)
        kw = mock_openai.call_args.kwargs
        self.assertEqual((kw["base_url"], kw["api_key"]), (XAI_DIRECT_BASE_URL, METERED))

    def test_flag_on_trigger_token_is_enough_without_metered_key(self):
        self._flag_on(metered=None)
        client = _responses_client()
        with patch.object(grok_x_search, "OpenAI", return_value=client) as mock_openai:
            posts = grok_x_search.fetch_posts_from_handle("Pentosh1", days=7, max_posts=5)
        self.assertEqual(posts[0]["text"], "Long ETH")
        kw = mock_openai.call_args.kwargs
        self.assertEqual((kw["base_url"], kw["api_key"]), (f"{SIDECAR}/v1", TRIGGER))
        tools = client.responses.create.call_args.kwargs["tools"]
        self.assertEqual(tools[0]["type"], "x_search", "tool shape unchanged")
        self.assertEqual(client.responses.create.call_args.kwargs["model"], "grok-4")

    def test_flag_on_sidecar_503_falls_back_once_to_metered(self):
        self._flag_on()
        sidecar_client = MagicMock()
        sidecar_client.responses.create.side_effect = _status_error(503)
        metered_client = _responses_client()
        constructed: list[dict] = []

        def fake_openai(**kw):
            constructed.append(kw)
            return sidecar_client if kw["base_url"].startswith(SIDECAR) else metered_client

        with patch.object(grok_x_search, "OpenAI", side_effect=fake_openai), patch.object(xai_auth, "log") as mock_log:
            posts = grok_x_search.fetch_posts_from_handle("Pentosh1", days=7, max_posts=5)
        self.assertEqual(len(posts), 1)
        self.assertEqual([(c["base_url"], c["api_key"]) for c in constructed], [(f"{SIDECAR}/v1", TRIGGER), (XAI_DIRECT_BASE_URL, METERED)])
        self.assertEqual(sidecar_client.responses.create.call_count, 1)
        self.assertIn(FALLBACK_LOG_MARKER, mock_log.call_args.args[0])

    def test_flag_on_sidecar_503_without_metered_key_returns_empty(self):
        self._flag_on(metered=None)
        sidecar_client = MagicMock()
        sidecar_client.responses.create.side_effect = _status_error(503)
        with patch.object(grok_x_search, "OpenAI", return_value=sidecar_client) as mock_openai, patch.object(xai_auth, "log"):
            posts = grok_x_search.fetch_posts_from_handle("Pentosh1", days=7, max_posts=5)
        self.assertEqual(posts, [])
        self.assertEqual(mock_openai.call_count, 1, "never retry-loops, no metered client without a key")

    def test_flag_on_non_fallback_error_is_not_retried(self):
        self._flag_on()
        sidecar_client = MagicMock()
        sidecar_client.responses.create.side_effect = _status_error(429)
        with patch.object(grok_x_search, "OpenAI", return_value=sidecar_client) as mock_openai:
            posts = grok_x_search.fetch_posts_from_handle("Pentosh1", days=7, max_posts=5)
        self.assertEqual(posts, [])
        self.assertEqual(mock_openai.call_count, 1)


if __name__ == "__main__":
    unittest.main()
