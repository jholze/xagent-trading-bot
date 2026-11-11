"""Epic #72 C7: pluggable LLM backend settings + ask_llm_json."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from intelligence.llm_client import (
    LlmError,
    LlmSettings,
    ask_llm,
    ask_llm_json,
    llm_settings,
    parse_llm_json,
    reset_llm_clients,
)
from intelligence.xai_auth import RETIRED_XAI_MODEL_IDS


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
        os.environ.pop("GROK_PARSE_MODEL", None)
        os.environ.pop("LLM_MODEL", None)
        os.environ["XAI_API_KEY"] = "xai-test-key"
        os.environ.pop("LLM_BASE_URL", None)
        s = llm_settings()
        self.assertEqual(s.backend, "xai")
        self.assertEqual(s.base_url, "https://api.x.ai/v1")
        self.assertEqual(s.api_key, "xai-test-key")
        # grok-4 is delisted (404 not-found, no redirect). grok-4.3 is the catalog successor.
        self.assertEqual(s.model, "grok-4.3")
        self.assertNotEqual(s.model, "grok-4")

    def test_grok_parse_model_overrides_retired_default(self):
        os.environ.pop("LLM_BACKEND", None)
        os.environ["XAI_API_KEY"] = "xai-test-key"
        os.environ["GROK_PARSE_MODEL"] = "grok-4.7"
        os.environ.pop("LLM_MODEL", None)
        self.assertEqual(llm_settings().model, "grok-4.7")

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
        os.environ.pop("GROK_PARSE_MODEL", None)
        os.environ.pop("LLM_MODEL", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        reset_llm_clients()

    def test_parse_llm_json(self):
        data = parse_llm_json('{"variable": "rsi", "new_value": 28}', required_keys=["variable"])
        self.assertEqual(data["variable"], "rsi")

    def test_ask_llm_json_uses_settings_model(self):
        mock_resp = MagicMock()
        mock_resp.output_text = '{"a": 1}'
        mock_client = MagicMock()
        mock_client.responses.create.return_value = mock_resp
        with patch("intelligence.llm_client._get_client", return_value=mock_client):
            data = ask_llm_json("return json", required_keys=["a"])
        self.assertEqual(data["a"], 1)
        mock_client.chat.completions.create.assert_not_called()
        kwargs = mock_client.responses.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "grok-4.3")
        self.assertNotIn(kwargs["model"], RETIRED_XAI_MODEL_IDS)

    def test_staging_env_names_resolve_live_model_on_responses(self):
        """xagent-test sets these names and no *MODEL* / LLM_* override."""
        os.environ.pop("LLM_BACKEND", None)
        os.environ.pop("LLM_MODEL", None)
        os.environ.pop("LLM_BASE_URL", None)
        os.environ.pop("LLM_API_KEY", None)
        os.environ.pop("GROK_PARSE_MODEL", None)
        os.environ.pop("GROK_MODEL", None)
        os.environ["XAI_API_KEY"] = "xai-test-key"
        os.environ["OPENAI_API_KEY"] = "openai-test-key"
        os.environ["XAI_USE_SUBSCRIPTION"] = "1"
        os.environ["XAI_AUTH_SIDECAR_URL"] = "http://xagent-xai-auth.railway.internal:8080"
        os.environ["XAI_AUTH_TRIGGER_TOKEN"] = "trigger-token"
        settings = llm_settings()
        self.assertEqual(settings.model, "grok-4.3")
        self.assertNotIn(settings.model, RETIRED_XAI_MODEL_IDS)
        self.assertEqual(settings.base_url, "http://xagent-xai-auth.railway.internal:8080/v1")
        self.assertTrue(settings.via_subscription)
        mock_resp = MagicMock()
        mock_resp.output_text = '{"a": 1}'
        mock_client = MagicMock()
        mock_client.responses.create.return_value = mock_resp
        with patch("intelligence.llm_client._get_client", return_value=mock_client):
            data = ask_llm_json("return json", retries=0, required_keys=["a"])
        self.assertEqual(data["a"], 1)
        mock_client.chat.completions.create.assert_not_called()
        sent = mock_client.responses.create.call_args.kwargs["model"]
        self.assertEqual(sent, settings.model)
        self.assertNotIn(sent, RETIRED_XAI_MODEL_IDS)
        for retired in RETIRED_XAI_MODEL_IDS:
            self.assertNotEqual(sent, retired)

    def test_failure_log_names_model_and_host_not_the_key(self):
        os.environ["XAI_API_KEY"] = "xai-test-key"
        mock_client = MagicMock()
        mock_client.responses.create.side_effect = RuntimeError("Error code: 404")
        with patch("intelligence.llm_client._get_client", return_value=mock_client), patch(
            "intelligence.llm_client.log"
        ) as mock_log:
            with self.assertRaises(LlmError):
                ask_llm_json("return json", retries=0, required_keys=["a"])
        line = mock_log.call_args.args[0]
        self.assertIn("LLM failed after 1 attempts", line)
        self.assertIn("model=grok-4.3", line)
        self.assertIn("host=api.x.ai", line)
        self.assertNotIn("xai-test-key", line)

    def test_ask_llm_returns_error_string_not_raise(self):
        with patch("intelligence.llm_client._get_client", side_effect=RuntimeError("down")):
            out = ask_llm("hi")
        self.assertTrue(out.startswith("API-Fehler:"))

    def test_grok_json_module_reexports(self):
        from intelligence.grok_json import GrokError, ask_grok_json, parse_grok_json

        self.assertIs(GrokError, LlmError)
        data = parse_grok_json('{"ok": true}')
        self.assertTrue(data["ok"])


class TestAskLlmPerCallOverride(unittest.TestCase):
    def setUp(self):
        reset_llm_clients()
        self._env = dict(os.environ)
        os.environ["LLM_BACKEND"] = "xai"
        os.environ["XAI_API_KEY"] = "global-xai-key"
        os.environ.pop("LLM_BASE_URL", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        reset_llm_clients()

    def test_ask_llm_base_url_override_constructs_openai_client(self):
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="local-ok"))]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_resp
        with patch("intelligence.llm_client.OpenAI", return_value=mock_client) as mock_openai:
            with patch("intelligence.llm_client.llm_settings") as mock_ls:
                out = ask_llm(
                    "summarize",
                    temperature=0.2,
                    model="qwen3.5:9b",
                    base_url="http://localhost:11434/v1",
                )
        mock_ls.assert_not_called()
        mock_openai.assert_called()
        kwargs = mock_openai.call_args.kwargs
        self.assertEqual(kwargs["base_url"], "http://localhost:11434/v1")
        self.assertEqual(kwargs["api_key"], "local")
        self.assertEqual(out, "local-ok")
        create_kwargs = mock_client.chat.completions.create.call_args.kwargs
        self.assertEqual(create_kwargs["model"], "qwen3.5:9b")

    def test_ask_llm_without_base_url_uses_global_settings(self):
        mock_resp = MagicMock()
        mock_resp.output_text = "global-ok"
        mock_client = MagicMock()
        mock_client.responses.create.return_value = mock_resp
        with patch("intelligence.llm_client.llm_settings") as mock_ls:
            mock_ls.return_value = LlmSettings(
                backend="xai",
                base_url="https://api.x.ai/v1",
                api_key="global-xai-key",
                model="grok-4.3",
            )
            with patch("intelligence.llm_client._get_client", return_value=mock_client) as mock_gc:
                out = ask_llm("hi")
        mock_ls.assert_called()
        mock_gc.assert_called()
        settings = mock_gc.call_args.args[0]
        self.assertEqual(settings.backend, "xai")
        self.assertEqual(settings.base_url, "https://api.x.ai/v1")
        self.assertEqual(out, "global-ok")


if __name__ == "__main__":
    unittest.main()
