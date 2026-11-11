"""#764 preparation only. Does not set main config and does not close the issue.

Jens chooses the live value. The fixture records a proposed explicit ``deny``
and the code's existing missing-key / explicit-log results. This test does
not write ``config.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

from risk.risk_manager import _fail_closed_guards_mode

ROOT = Path(__file__).resolve().parents[2]
PROPOSAL = ROOT / "tests/fixtures/fail_closed_guards_764/proposal.json"


def test_proposal_recommends_explicit_deny_and_is_not_applied():
    proposal = json.loads(PROPOSAL.read_text(encoding="utf-8"))
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    assert proposal["issue"] == 764
    assert proposal["status"] == "not_applied"
    assert proposal["approved"] is False
    assert proposal["issue_remains_open"] is True
    assert proposal["decided_by"] is None
    assert proposal["target_ref"] == "main"
    assert proposal["proposed_value"] == "deny"
    assert proposal["main_has_key_on_2026_11_10"] is False
    assert '"fail_closed_guards": "deny"' in proposal["proposed_diff"]
    assert "not applied" in proposal["proposed_diff"]
    assert config["risk"]["fail_closed_guards"] == "deny"
    assert _fail_closed_guards_mode({"risk": {}}) == "deny"
    assert _fail_closed_guards_mode({"risk": {"fail_closed_guards": "log"}}) == "log"
    assert _fail_closed_guards_mode({"risk": {"fail_closed_guards": "deny"}}) == "deny"
