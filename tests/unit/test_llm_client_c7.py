"""Epic #72 C7: pluggable LLM backend settings + ask_llm_json."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from intelligence.llm_client import (
    LlmError,
    ask_llm,
    ask_llm_json,
    llm_settings,
    parse_llm_json,
    reset_llm_clients,
)


class TestLlmSettings(unittest.TestCase):
    def setUp(self):
        reset_llm_clients()
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        reset_llm_clients()

    def test_default_xai(self):
        os.environ.pop("LLM_BACKEND", None)
        os.environ["XAI_API_KEY"] = "xai-test-key"
        os.environ.pop("LLM_BASE_URL", None)
        s = llm_settings()
        self.assertEqual(s.backend, "xai")
        self.assertEqual(s.base_url, "https://api.x.ai/v1")
        self.assertEqual(s.api_key, "xai-test-key")

    def test_openai_compat_requires_base_url(self):
        os.environ["LLM_BACKEND"] = "openai_compat"
        os.environ.pop("LLM_BASE_URL", None)
        with self.assertRaises(LlmError):
            llm_settings()

    def test_openai_compat_settings(self):
        os.environ["LLM_BACKEND"] = "openai_compat"
        os.environ["LLM_BASE_URL"] = "http://localhost:8000/v1"
        os.environ["LLM_API_KEY"] = "local-key"
        os.environ["LLM_MODEL"] = "my-open-model"
        s = llm_settings()
        self.assertEqual(s.backend, "openai_compat")
        self.assertEqual(s.base_url, "http://localhost:8000/v1")
        self.assertEqual(s.model, "my-open-model")
        self.assertEqual(s.api_key, "local-key")


class TestAskLlmJson(unittest.TestCase):
    def setUp(self):
        reset_llm_clients()
        self._env = dict(os.environ)
        os.environ["LLM_BACKEND"] = "xai"
        os.environ["XAI_API_KEY"] = "test-key"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        reset_llm_clients()

    def test_parse_llm_json(self):
        data = parse_llm_json('{"variable": "rsi", "new_value": 28}', required_keys=["variable"])
        self.assertEqual(data["variable"], "rsi")

    def test_ask_llm_json_uses_settings_model(self):
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content='{"a": 1}'))]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_resp
        with patch("intelligence.llm_client._get_client", return_value=mock_client):
            data = ask_llm_json("return json", required_keys=["a"])
        self.assertEqual(data["a"], 1)
        mock_client.chat.completions.create.assert_called()
        kwargs = mock_client.chat.completions.create.call_args.kwargs
        self.assertIn("model", kwargs)

    def test_ask_llm_returns_error_string_not_raise(self):
        with patch("intelligence.llm_client._get_client", side_effect=RuntimeError("down")):
            out = ask_llm("hi")
        self.assertTrue(out.startswith("API-Fehler:"))

    def test_grok_json_module_reexports(self):
        from intelligence.grok_json import GrokError, ask_grok_json, parse_grok_json

        self.assertIs(GrokError, LlmError)
        data = parse_grok_json('{"ok": true}')
        self.assertTrue(data["ok"])


if __name__ == "__main__":
    unittest.main()
