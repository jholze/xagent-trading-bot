"""kanban_status: bare #N refs that point at PRs must not abort the run.

Regression for the failed "Kanban status" runs on 2026-10-15 (merges of
#685, #686, #688, #691): repository.issue(number: <PR>) returns NOT_FOUND
and the whole job exited 1. No network: the gh subprocess layer is mocked.
"""

import json

import pytest

from scripts import kanban_status as ks

PR_REF = 686  # a PR mentioned in the body, not an issue
ISSUE = 671  # the real linked issue


def _issue_payload(number: int) -> dict:
    return {
        "data": {
            "repository": {
                "issue": {
                    "number": number,
                    "title": f"Issue {number}",
                    "state": "OPEN",
                    "labels": {"nodes": [{"name": "bug"}]},
                    "projectItems": {"nodes": []},
                }
            }
        }
    }


def _fake_run(issue_errors: dict[int, str], calls: list[int]):
    """Stand-in for ks._run: answers `gh api graphql --input <file>`."""

    def run(args, *, input_text=None):
        assert args[:4] == ["gh", "api", "graphql", "--input"], args
        with open(args[4], encoding="utf-8") as fh:
            payload = json.load(fh)
        query = payload["query"]
        n = payload["variables"]["n"]
        if "pullRequest(number:" in query:
            return json.dumps(
                {"data": {"repository": {"pullRequest": {
                    "closingIssuesReferences": {"nodes": []}}}}}
            )
        calls.append(n)
        if n in issue_errors:
            # Same shape as the real failure: gh exits 1, _run raises.
            raise SystemExit(
                "command failed (1): gh api graphql --input\n" + issue_errors[n]
            )
        return json.dumps(_issue_payload(n))

    return run


def _write_event(tmp_path, monkeypatch) -> str:
    event = {
        "action": "closed",
        "pull_request": {
            "number": 700,
            "merged": True,
            "base": {"ref": "staging"},
            "title": f"Cap buy size (includes #{PR_REF})",
            "body": f"Implements #{ISSUE}. Stays open for shadow soak.",
        },
    }
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    return str(path)


def test_pr_reference_not_found_is_skipped_and_loop_continues(tmp_path, monkeypatch):
    calls: list[int] = []
    not_found = (
        '{"data":{"repository":{"issue":null}},"errors":[{"type":"NOT_FOUND",'
        f'"message":"Could not resolve to an Issue with the number of {PR_REF}."}}]}}'
        f"gh: Could not resolve to an Issue with the number of {PR_REF}."
    )
    monkeypatch.setattr(ks, "_run", _fake_run({PR_REF: not_found}, calls))
    event_path = _write_event(tmp_path, monkeypatch)

    results = ks.apply_github_event(event_path=event_path, dry_run=True)

    by_issue = {r.get("issue"): r for r in results}
    assert by_issue[PR_REF]["changed"] is False
    assert "not an issue" in by_issue[PR_REF]["reason"]
    # The loop went on to the real issue after the PR reference.
    assert calls[0] == PR_REF
    assert ISSUE in calls
    assert ISSUE in by_issue
    assert all(r.get("labels_written") is False for r in results)


def test_other_graphql_error_still_exits_nonzero(tmp_path, monkeypatch):
    calls: list[int] = []
    forbidden = (
        '{"errors":[{"type":"FORBIDDEN","message":"Resource not accessible '
        'by personal access token"}]}'
    )
    monkeypatch.setattr(ks, "_run", _fake_run({PR_REF: forbidden}, calls))
    event_path = _write_event(tmp_path, monkeypatch)

    with pytest.raises(SystemExit) as exc:
        ks.main(["apply", "--event-path", event_path, "--dry-run"])

    # A string SystemExit code makes the interpreter exit with status 1.
    assert exc.value.code not in (0, None)
    assert "FORBIDDEN" in str(exc.value.code)
    assert ISSUE not in calls
