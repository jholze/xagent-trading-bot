"""Pins for the money-path and evidence gate. No GitHub calls."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.ci.review_gates import (
    OWNER_LABEL,
    body_has_evidence,
    changed_config_paths,
    decide_gate,
    has_before_after_table,
    load_rules,
    money_path_touched,
)

ROOT = Path(__file__).resolve().parents[2]
TABLE = """
| metric | before | after |
| --- | --- | --- |
| unit tests | 1 | 2 |
"""


def _rules():
    return load_rules()


def test_rules_file_matches_the_tree():
    rules = _rules()
    for prefix in rules["prefixes"]:
        assert (ROOT / prefix).is_dir(), prefix
    config = json.loads((ROOT / rules["config_json"]["path"]).read_text(encoding="utf-8"))
    for key in rules["config_json"]["key_prefixes"]:
        assert key in config, key


def test_config_key_diff_is_dotted():
    before = {"risk": {"cash_floor_pct": 12, "nested": {"a": 1}}, "debug": False}
    after = {"risk": {"cash_floor_pct": 10, "nested": {"a": 1}}, "debug": True}
    paths = changed_config_paths(before, after)
    assert "risk.cash_floor_pct" in paths
    assert "debug" in paths
    assert "risk.nested.a" not in paths


def test_only_listed_config_keys_are_money_path():
    rules = _rules()
    assert money_path_touched(["docs/domain/spec-writing.md"], None, rules) is False
    assert money_path_touched(["risk/risk_manager.py"], None, rules) is True
    assert money_path_touched(["strategies/dca_policy.py"], None, rules) is True
    assert money_path_touched(["execution/factory.py"], None, rules) is True
    assert money_path_touched(["services/gainer_signal/pure.py"], None, rules) is True
    assert money_path_touched(["services/dca_sniper/loop.py"], None, rules) is True
    assert (
        money_path_touched(["config.json"], {"cmc.api"}, rules) is False
    )
    assert money_path_touched(["config.json"], {"risk.cash_floor_pct"}, rules) is True
    assert money_path_touched(["config.json"], {"max_usdt_per_trade"}, rules) is True
    assert money_path_touched(["config.json"], {"costs.taker"}, rules) is True


def test_table_and_evidence_heading():
    assert has_before_after_table(TABLE)
    assert not has_before_after_table("| before | after |\nno separator\n")
    body = "## Summary\n\n## Evidence\n" + TABLE
    assert body_has_evidence(body)
    assert not body_has_evidence("## Evidence\nno table")
    assert not body_has_evidence(TABLE)


def _decide(**kwargs):
    params = dict(
        files=["docs/domain/spec-writing.md"],
        config_paths=None,
        rules=_rules(),
        labels=[],
        body="",
        title="",
        pr_number=12,
        evidence_text={},
    )
    params.update(kwargs)
    return decide_gate(**params)


def test_non_money_path_accepts_evidence_file_or_body():
    missing = _decide()
    assert missing.ok is False
    assert missing.money_path is False
    filed = _decide(
        files=["docs/evidence/review-gates.md"],
        evidence_text={"docs/evidence/review-gates.md": TABLE},
    )
    assert filed.ok is True
    bodied = _decide(body="## Evidence\n" + TABLE)
    assert bodied.ok is True


def test_money_path_requires_label_and_numbered_evidence():
    files = ["risk/risk_manager.py", "docs/evidence/12.md"]
    text = {"docs/evidence/12.md": TABLE}
    blocked = _decide(files=files, evidence_text=text, labels=[])
    assert blocked.money_path is True
    assert blocked.ok is False
    assert any(OWNER_LABEL in item for item in blocked.failures)

    unnamed = _decide(
        files=["risk/denylist.py", "docs/evidence/notes.md"],
        evidence_text={"docs/evidence/notes.md": TABLE},
        labels=[OWNER_LABEL],
    )
    assert unnamed.ok is False

    issue = _decide(
        files=["strategies/dca.py", "docs/evidence/issue-620.md"],
        evidence_text={"docs/evidence/issue-620.md": TABLE},
        labels=[OWNER_LABEL],
        body="See #620",
        pr_number=99,
    )
    assert issue.ok is True

    pr = _decide(
        files=["services/dca_sniper/x.py", "docs/evidence/pr-12.md"],
        evidence_text={"docs/evidence/pr-12.md": TABLE},
        labels=[OWNER_LABEL],
    )
    assert pr.ok is True

    no_table = _decide(
        files=files,
        evidence_text={"docs/evidence/12.md": "no table"},
        labels=[OWNER_LABEL],
    )
    assert no_table.ok is False

