"""Circuit breaker: N consecutive LLM failures skip calls for M minutes.

Logs once on open and once on close. Callers stay fail-neutral and do not hit
the client while the breaker is open.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

import grok_x_search
from intelligence.llm_circuit import CLOSED_LOG, OPEN_LOG, reset_llm_circuit
from intelligence.llm_client import LlmError, ask_llm, ask_llm_json, reset_llm_clients


class TestLlmCircuit(unittest.TestCase):
    def setUp(self):
        reset_llm_clients()
        reset_llm_circuit()
        self._env = dict(os.environ)
        os.environ["LLM_BACKEND"] = "xai"
        os.environ["XAI_API_KEY"] = "xai-test-key"
        os.environ.pop("XAI_USE_SUBSCRIPTION", None)
        os.environ["LLM_CIRCUIT_FAILURES"] = "2"
        os.environ["LLM_CIRCUIT_OPEN_MINUTES"] = "10"
        self.clock = [1_000.0]

        def _now():
            return self.clock[0]

        self._now_patch = patch("intelligence.llm_circuit.now", _now)
        self._now_patch.start()

    def tearDown(self):
        self._now_patch.stop()
        os.environ.clear()
        os.environ.update(self._env)
        reset_llm_circuit()
        reset_llm_clients()

    def test_breaker_opens_skips_then_closes(self):
        calls = {"n": 0}
        client = MagicMock()

        def create(**_kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("Error code: 404")
            return MagicMock(output_text='{"a": 1}')

        client.responses.create.side_effect = create

        with patch("intelligence.llm_client.OpenAI", return_value=client), patch(
            "grok_x_search.OpenAI", return_value=client
        ), patch("intelligence.llm_circuit.log") as circuit_log, patch("intelligence.llm_client.log"):
            with self.assertRaises(LlmError):
                ask_llm_json("json", retries=0, required_keys=["a"])
            self.assertEqual([c.args[0] for c in circuit_log.call_args_list], [])
            with self.assertRaises(LlmError):
                ask_llm_json("json", retries=0, required_keys=["a"])
            self.assertEqual(
                [c.args[0] for c in circuit_log.call_args_list],
                [f"{OPEN_LOG}: 2 consecutive failures, skipping LLM calls for 10 minutes"],
            )
            self.assertEqual(circuit_log.call_args.args[1], "WARNING")

            before = calls["n"]
            with self.assertRaises(LlmError) as ctx:
                ask_llm_json("json", retries=0, required_keys=["a"])
            self.assertIn(OPEN_LOG, str(ctx.exception))
            self.assertEqual(calls["n"], before, "open breaker does not call the LLM")
            self.assertEqual(circuit_log.call_count, 1, "open is logged once")

            skipped = ask_llm("hi")
            self.assertTrue(skipped.startswith("API-Fehler:"))
            self.assertIn(OPEN_LOG, skipped)
            self.assertEqual(calls["n"], before)

            self.assertEqual(grok_x_search.fetch_posts_from_handle("Pentosh1"), [])
            self.assertEqual(calls["n"], before, "x_search is skipped too")

            self.clock[0] += 10 * 60 + 1
            data = ask_llm_json("json", retries=0, required_keys=["a"])
            self.assertEqual(data, {"a": 1})
            self.assertEqual(calls["n"], before + 1)
            self.assertEqual(
                [c.args[0] for c in circuit_log.call_args_list],
                [
                    f"{OPEN_LOG}: 2 consecutive failures, skipping LLM calls for 10 minutes",
                    CLOSED_LOG,
                ],
            )
            self.assertEqual(circuit_log.call_args.args[1], "INFO")

            again = ask_llm_json("json", retries=0, required_keys=["a"])
            self.assertEqual(again, {"a": 1})
            self.assertEqual(circuit_log.call_count, 2, "a later success does not log again")

    def test_half_open_failure_does_not_log_open_again(self):
        client = MagicMock()
        client.responses.create.side_effect = RuntimeError("Error code: 404")
        with patch("intelligence.llm_client.OpenAI", return_value=client), patch(
            "intelligence.llm_circuit.log"
        ) as circuit_log, patch("intelligence.llm_client.log"):
            for _ in range(2):
                with self.assertRaises(LlmError):
                    ask_llm_json("json", retries=0)
            self.assertEqual(circuit_log.call_count, 1)
            self.clock[0] += 10 * 60 + 1
            with self.assertRaises(LlmError):
                ask_llm_json("json", retries=0)
            self.assertEqual(circuit_log.call_count, 1, "still open, no second open line")
            with self.assertRaises(LlmError) as ctx:
                ask_llm_json("json", retries=0)
            self.assertIn(OPEN_LOG, str(ctx.exception))
            self.assertEqual(circuit_log.call_count, 1)

    def test_disabled_when_failures_is_zero(self):
        os.environ["LLM_CIRCUIT_FAILURES"] = "0"
        client = MagicMock()
        client.responses.create.side_effect = RuntimeError("Error code: 404")
        with patch("intelligence.llm_client.OpenAI", return_value=client), patch(
            "intelligence.llm_circuit.log"
        ) as circuit_log, patch("intelligence.llm_client.log"):
            for _ in range(4):
                with self.assertRaises(LlmError):
                    ask_llm_json("json", retries=0)
        self.assertEqual(circuit_log.call_count, 0)
        self.assertEqual(client.responses.create.call_count, 4)


if __name__ == "__main__":
    unittest.main()
