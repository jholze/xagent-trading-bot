"""#305 item 1: every engine rationale literal and risk code has an explanation.

Does not change assertions in tests/unit/test_user_explain.py.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from notifications.user_explain import explain_rationale, explain_risk

_ROOT = Path(__file__).resolve().parents[2]
_ENGINE = _ROOT / "strategies" / "decision_engine.py"
_RISK = _ROOT / "risk" / "risk_manager.py"

_FSTRING_SAMPLES = {
    "technical.action": "BUY",
    "x_signal.action": "BUY",
    "x_signal.account": "alice",
    "x_signal.confidence": "80",
    "cmc_signal.action": "SELL",
    "cmc_signal.confidence": "70",
    "lc_signal.action": "BUY",
    "lc_signal.confidence": "65",
    "shadow_action": "BUY",
}

_CODE_RE = re.compile(r'\bcode="([a-z][a-z0-9_]*)"')


def _render_joined(node: ast.JoinedStr) -> str:
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        elif isinstance(value, ast.FormattedValue):
            expr = ast.unparse(value.value)
            if expr not in _FSTRING_SAMPLES:
                raise AssertionError(f"no sample for f-string expr {expr!r}")
            parts.append(_FSTRING_SAMPLES[expr])
        else:
            raise AssertionError(f"unhandled f-string piece {ast.dump(value)}")
    return "".join(parts)


def _samples_from_append_arg(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return [_render_joined(node)]
    src = ast.unparse(node)
    if "consensus" in src:
        return [
            "X+CMC consensus",
            "X+LC consensus",
            "CMC+LC consensus",
            "X+CMC+LC consensus",
        ]
    raise AssertionError(f"unhandled rationale_parts.append arg: {src}")


def collect_rationale_literals() -> list[tuple[int, str]]:
    src = _ENGINE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "append":
            continue
        if not isinstance(func.value, ast.Name) or func.value.id != "rationale_parts":
            continue
        lineno = getattr(node, "lineno", 0)
        for sample in _samples_from_append_arg(node.args[0]):
            found.append((lineno, sample))
    return found


def collect_risk_codes() -> list[str]:
    src = _RISK.read_text(encoding="utf-8")
    return sorted(set(_CODE_RE.findall(src)))


def _is_raw_fallback(sample: str, explained: str) -> bool:
    stripped = explained.strip()
    variants = {sample.strip(), sample.strip().replace("->", "→")}
    return stripped in variants


class TestRationaleLiteralCoverage(unittest.TestCase):
    def test_every_engine_rationale_literal_resolves(self):
        literals = collect_rationale_literals()
        self.assertGreaterEqual(len(literals), 16, "decision_engine rationale_parts.append literals")
        missing = []
        for lineno, sample in literals:
            explained = explain_rationale(sample)
            if _is_raw_fallback(sample, explained):
                missing.append((lineno, sample, explained))
        self.assertEqual(
            missing,
            [],
            f"{len(literals)} rationale literals; unresolved: {missing}",
        )


class TestRiskCodeCoverage(unittest.TestCase):
    def test_every_risk_code_literal_has_explanation(self):
        codes = collect_risk_codes()
        self.assertGreaterEqual(len(codes), 30, "risk_manager.py code= literals")
        sentinel = "UNMATCHED_RAW_XYZ_305"
        missing = []
        for code in codes:
            explained = explain_risk(sentinel, code=code)
            if explained == sentinel or not explained.strip():
                missing.append(code)
        self.assertEqual(
            missing,
            [],
            f"{len(codes)} risk codes; unresolved: {missing}",
        )


class TestRationaleNormalization(unittest.TestCase):
    def test_ascii_arrow_ta_buy_matches_unicode_entry(self):
        text = explain_rationale("TA->BUY")
        self.assertIn("Kaufchance", text)
        self.assertNotEqual(text.strip(), "TA->BUY")
        self.assertNotEqual(text.strip(), "TA→BUY")

    def test_cmc_percent_stripped_for_lookup_keeps_score(self):
        text = explain_rationale("CMC->SELL(70%)")
        self.assertIn("CMC", text)
        self.assertIn("70", text)
        self.assertIn("Verkauf", text)

    def test_multi_source_consensus_maps(self):
        text = explain_rationale("multi-source consensus")
        self.assertIn("Signalquellen", text)
        self.assertNotEqual(text.strip(), "multi-source consensus")
        self.assertNotEqual(text.strip(), "multi_source")

    def test_lc_signal_resolves(self):
        text = explain_rationale("LC->BUY(65%)")
        self.assertIn("LunarCrush", text)
        self.assertIn("65", text)

    def test_explain_risk_prefers_code_over_unrelated_message(self):
        text = explain_risk("totally unknown english", code="market_bias_degraded")
        self.assertNotEqual(text, "totally unknown english")
        self.assertIn("Markt", text)


if __name__ == "__main__":
    unittest.main()
