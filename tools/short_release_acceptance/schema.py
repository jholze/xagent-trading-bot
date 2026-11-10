"""Evidence rows for D-E2E. Fixture output cannot become an authenticated pass."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from tools.short_release_acceptance.constants import (
    AC_REVISION,
    CANONICAL_AC_TEXT,
    CODER_MODEL,
    DRAFT_ID,
    EVIDENCE_KINDS,
    ISSUE_412_STATE,
    OFFLINE_PRODUCERS,
    REQUIRED_AC_IDS,
    REQUIRED_REVIEWERS,
    SCHEMA_VERSION,
    VERDICT_ACCEPTED,
    VERDICT_NO_GO,
    VERDICT_NOT_VERIFIED,
)
from tools.short_release_acceptance.manifest import funding_proof_ok

_SHA40 = re.compile(r"^[0-9a-f]{40}$")


def canonical_ac_hash() -> str:
    return hashlib.sha256(CANONICAL_AC_TEXT.encode("utf-8")).hexdigest()


def _result(verdict: str, reason: str, authenticated: bool) -> dict[str, Any]:
    return {
        "release_verdict": verdict,
        "reason": reason,
        "authenticated_testnet_pass": authenticated,
    }


def _stale(row: dict[str, Any], index_sha: str) -> bool:
    if row.get("candidate_sha") != index_sha:
        return True
    reviewers = row.get("reviewers") or []
    if isinstance(reviewers, list):
        for review in reviewers:
            if isinstance(review, dict) and review.get("head") not in (None, index_sha):
                return True
    if row.get("tester_head") not in (None, index_sha):
        return True
    return False


def _reviews_ok(row: dict[str, Any], index_sha: str) -> bool:
    reviews = row.get("reviewers")
    if not isinstance(reviews, list):
        return False
    found: set[str] = set()
    for review in reviews:
        if not isinstance(review, dict):
            return False
        model = review.get("model")
        if not isinstance(model, str) or model == CODER_MODEL or model not in REQUIRED_REVIEWERS:
            return False
        if review.get("head") != index_sha:
            return False
        found.add(model)
    return all(model in found for model in REQUIRED_REVIEWERS)


def _tester_ok(row: dict[str, Any], index_sha: str) -> bool:
    model = row.get("tester_model")
    if row.get("tester_status") != "passed":
        return False
    if row.get("tester_head") != index_sha:
        return False
    if not isinstance(model, str) or not model or model == CODER_MODEL:
        return False
    if model in REQUIRED_REVIEWERS:
        return False
    return True


def classify_row(row: Any, *, index_sha: str, ac_hash: str | None = None) -> dict[str, Any]:
    """Classify one evidence row. This function does not contact a venue."""
    expected_hash = canonical_ac_hash() if ac_hash is None else ac_hash
    if not isinstance(row, dict):
        return _result(VERDICT_NO_GO, "row_malformed", False)
    if row.get("acceptance_executed_by_this_package") is True:
        return _result(VERDICT_NO_GO, "this_package_cannot_execute", False)
    if row.get("issue_412_action") in {"reopen", "close"}:
        return _result(VERDICT_NO_GO, "issue_412_untouched", False)
    kind = row.get("kind")
    if kind not in EVIDENCE_KINDS:
        return _result(VERDICT_NO_GO, "kind_invalid", False)
    if not isinstance(index_sha, str) or _SHA40.fullmatch(index_sha) is None:
        return _result(VERDICT_NO_GO, "index_sha_invalid", False)
    if _stale(row, index_sha):
        return _result(VERDICT_NO_GO, "stale_sha", False)
    if row.get("authenticated_testnet_pass") is True and kind != "actual":
        return _result(VERDICT_NO_GO, "fixture_claimed_authenticated_pass", False)
    if kind != "actual":
        return _result(VERDICT_NOT_VERIFIED, f"{kind}_is_not_authenticated_pass", False)
    if row.get("producer") in OFFLINE_PRODUCERS:
        return _result(VERDICT_NOT_VERIFIED, "offline_producer", False)
    missing: list[str] = []
    if row.get("ac_revision_hash") != expected_hash:
        missing.append("ac_revision")
    if row.get("test_status") != "passed":
        missing.append("test_status")
    if not _reviews_ok(row, index_sha):
        missing.append("review")
    if not _tester_ok(row, index_sha):
        missing.append("tester")
    if not funding_proof_ok(row.get("funding_proof")):
        missing.append("funding_proof")
    if missing:
        return _result(VERDICT_NOT_VERIFIED, "missing_" + "+".join(missing), False)
    return _result(VERDICT_ACCEPTED, "actual_shape_complete", True)


def classify_index(index: Any) -> dict[str, Any]:
    if not isinstance(index, dict):
        return {
            "release_verdict": VERDICT_NO_GO,
            "reason": "index_malformed",
            "authenticated_testnet_pass": False,
            "authenticated_passes": 0,
            "rows": [],
        }
    index_sha = index.get("candidate_sha")
    ac_hash = canonical_ac_hash()
    if index.get("ac_revision_hash") not in (None, ac_hash):
        return {
            "release_verdict": VERDICT_NOT_VERIFIED,
            "reason": "ac_revision_mismatch",
            "authenticated_testnet_pass": False,
            "authenticated_passes": 0,
            "rows": [],
        }
    rows = index.get("rows")
    if not isinstance(rows, list):
        rows = []
    classified = [classify_row(row, index_sha=index_sha, ac_hash=ac_hash) for row in rows]
    if any(item["release_verdict"] == VERDICT_NO_GO for item in classified):
        verdict = VERDICT_NO_GO
        reason = "row_no_go"
    elif not rows or any(item["release_verdict"] != VERDICT_ACCEPTED for item in classified):
        verdict = VERDICT_NOT_VERIFIED
        reason = "not_all_rows_accepted"
    else:
        present = {
            row.get("ac_id")
            for row in rows
            if isinstance(row, dict)
        }
        if not set(REQUIRED_AC_IDS) <= present:
            verdict = VERDICT_NOT_VERIFIED
            reason = "ac_coverage_incomplete"
        else:
            verdict = VERDICT_ACCEPTED
            reason = "actual_shape_complete"
    authenticated_passes = 0
    if verdict == VERDICT_ACCEPTED:
        authenticated_passes = sum(1 for item in classified if item["authenticated_testnet_pass"])
    return {
        "release_verdict": verdict,
        "reason": reason,
        "authenticated_testnet_pass": verdict == VERDICT_ACCEPTED and authenticated_passes > 0,
        "authenticated_passes": authenticated_passes,
        "rows": classified,
    }


def phase1_evidence_index(candidate_sha: str) -> dict[str, Any]:
    """The only index this package produces: fixture rows, nothing accepted."""
    ac_hash = canonical_ac_hash()
    rows = []
    for ac_id in REQUIRED_AC_IDS:
        rows.append(
            {
                "row_id": ac_id.lower(),
                "ac_id": ac_id,
                "kind": "fixture",
                "candidate_sha": candidate_sha,
                "ac_revision": AC_REVISION,
                "ac_revision_hash": ac_hash,
                "test_status": "not_run",
                "reviewers": [],
                "tester_status": "not_run",
                "tester_head": None,
                "tester_model": None,
                "funding_proof": None,
                "producer": "short_release_acceptance.compose",
                "acceptance_executed_by_this_package": False,
                "issue_412_action": "leave_closed_not_planned",
                "authenticated_testnet_pass": False,
                "notes": "Offline fixture. Not an authenticated testnet result.",
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "draft_id": DRAFT_ID,
        "issued": False,
        "ac_revision": AC_REVISION,
        "ac_revision_hash": ac_hash,
        "candidate_sha": candidate_sha,
        "integration_wired": False,
        "issue_412": ISSUE_412_STATE,
        "authenticated_passes_established": 0,
        "rows": rows,
    }
