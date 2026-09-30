#!/usr/bin/env python3
"""Move Trading Bot Kanban *Status* columns. Never write labels.

Board: https://github.com/users/jholze/projects/1
Dev branch: staging. Prod branch: main — this script never merges, pushes,
or otherwise touches main.

Kay alone sets ``omnigent:ready`` after the Ready chain. This program only
updates the project Status field.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any

os.environ["GH_NO_COLOR"] = "1"
os.environ.pop("FORCE_COLOR", None)

REPO = "jholze/xagent-trading-bot"
PROJECT_TITLE = "Trading Bot Kanban"
PROJECT_ID = "PVT_kwHOAGiMAs4BdyQO"
STATUS_FIELD = "PVTSSF_lAHOAGiMAs4BdyQOzhYRRbQ"
DEV_BRANCH = "staging"
PROD_BRANCH = "main"

# Live option ids (verified 2026-09-30). Same ids as the old
# Backlog: Tickets / Ready / In progress / In review names.
STATUSES: dict[str, tuple[str, str]] = {
    "backlog": ("28178680", "Backlog"),
    "backlog-epics": ("f75ad846", "Backlog: Epics"),
    "ready-to-work": ("61e4505c", "Ready to Work"),
    "shadow": ("47fc9ee4", "Shadow"),
    "spec-acceptance": ("df73e18b", "Spec/Acceptance"),
    "done": ("98236657", "Done"),
}
BY_NAME = {label.lower(): key for key, (_oid, label) in STATUSES.items()}
BY_NAME.update({key: key for key in STATUSES})
BY_NAME.update(
    {
        "backlog tickets": "backlog",
        "backlog: tickets": "backlog",
        "backlog-tickets": "backlog",
        "backlog epics": "backlog-epics",
        "backlog: epics": "backlog-epics",
        "ready": "ready-to-work",
        "ready to work": "ready-to-work",
        "spec": "spec-acceptance",
        "spec/acceptance": "spec-acceptance",
        "spec-acceptance": "spec-acceptance",
    }
)
RETIRED_STATUSES = {
    "in-progress": "retired: coding no longer has its own column; soak uses shadow",
    "in-review": "retired: use spec-acceptance for spec/fixround, not PR review",
}

READY_LABEL = "omnigent:ready"
REVIEW_LENA = "review:lena"
REVIEW_VIKTOR = "review:viktor"
VETO_LENA = "veto:lena"
VETO_VIKTOR = "veto:viktor"
EPIC_LABEL = "epic"
PARKED_LABEL = "parked"
SHADOW_MODUS_LABEL = "shadow-modus"

# Explicit freeze / fix-round language. Issue templates that already
# contain "## Acceptance criteria" must NOT promote on open.
SPEC_EVENT_RE = re.compile(
    r"(?is)("
    r"spec\s*freeze|"
    r"frozen\s+(?:the\s+)?(?:spec|acceptance)|"
    r"acceptance\s+(?:criteria\s+)?(?:frozen|finalized)|"
    r"marcus\s+spec|"
    r"fix\s*-?\s*round"
    r")"
)
SOAK_RE = re.compile(
    r"(?is)("
    r"paper[\s-]*soak|"
    r"shadow[\s-]*soak|"
    r"\bsoak(?:ing)?\b|"
    r"28[\s-]*days?|"
    r"stays\s+open|"
    r"leave(?:s)?\s+(?:the\s+)?issue\s+open|"
    r"do\s+not\s+close"
    r")"
)
ISSUE_REF_RE = re.compile(r"(?:(?:clos|fix|resolv)(?:e|es|ed)?\s+)?#(\d+)", re.I)
ANSI = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True)
class Decision:
    want: str | None
    reason: str
    """Canonical status key (e.g. ready-to-work) or None = do not move."""


def normalize_status(name: str) -> str:
    raw = (name or "").strip()
    key = BY_NAME.get(raw.lower())
    if key:
        return key
    if raw.lower() in RETIRED_STATUSES:
        raise SystemExit(
            f"unknown status {name!r}; {RETIRED_STATUSES[raw.lower()]}. "
            f"want {', '.join(STATUSES)}"
        )
    raise SystemExit(f"unknown status {name!r}; want {', '.join(STATUSES)}")


def status_label(key: str) -> str:
    return STATUSES[key][1]


def labels_set(labels: list[str] | None) -> set[str]:
    return {str(x) for x in (labels or [])}


def ready_chain_green(labels: set[str]) -> bool:
    return (
        READY_LABEL in labels
        and REVIEW_LENA in labels
        and REVIEW_VIKTOR in labels
        and VETO_LENA not in labels
        and VETO_VIKTOR not in labels
    )


def has_veto(labels: set[str]) -> bool:
    return VETO_LENA in labels or VETO_VIKTOR in labels


def is_spec_event_text(text: str | None) -> bool:
    return bool(text and SPEC_EVENT_RE.search(text))


def is_soak_text(text: str | None) -> bool:
    return bool(text and SOAK_RE.search(text))


def is_soak_issue(labels: set[str], *texts: str | None) -> bool:
    if SHADOW_MODUS_LABEL in labels:
        return True
    blob = "\n".join(t or "" for t in texts)
    return is_soak_text(blob)


def decide_status(
    *,
    issue_state: str,
    labels: list[str] | None,
    current: str | None,
    new_issue: bool = False,
    spec_event: bool = False,
    soak_merged_staging: bool = False,
) -> Decision:
    """Pure Status policy. Never implies a label write or an issue close.

    Priority (first match wins):
      1. Epic → Backlog: Epics (only when unset / wrongly on Backlog)
      2. Hold Shadow / Done
      3. Soak PR merged to staging → Shadow (issue stays OPEN)
      4. omnigent:ready + both reviews, no veto → Ready to Work
      5. Ready to Work with broken chain / veto → Spec/Acceptance
      6. Spec freeze / fix-round event on Backlog → Spec/Acceptance
      7. New issue or no Status → Backlog (parked stays Backlog)
    """
    labs = labels_set(labels)
    state = (issue_state or "").upper()
    current_key = BY_NAME.get((current or "").strip().lower()) if current else None

    if EPIC_LABEL in labs:
        if current_key in (None, "backlog"):
            return Decision("backlog-epics", "epic lives in Backlog: Epics")
        return Decision(None, "retain Backlog: Epics / ignore epic card")

    if current_key == "done":
        return Decision(None, "retain Done")

    if current_key == "shadow":
        return Decision(None, "retain Shadow (soak stays open)")

    if state == "CLOSED":
        return Decision(None, "closed issue: do not move Status")

    # Caller sets soak_merged_staging only after checking soak text/labels
    # on the linked issue + PR. Move to Shadow; do not close.
    if soak_merged_staging:
        return Decision("shadow", "PR merged to staging for paper/shadow soak")

    parked = PARKED_LABEL in labs

    if ready_chain_green(labs) and not parked:
        if current_key == "ready-to-work":
            return Decision(None, "already Ready to Work")
        return Decision(
            "ready-to-work",
            "omnigent:ready + review:lena + review:viktor, no veto",
        )

    if current_key == "ready-to-work":
        if has_veto(labs):
            return Decision("spec-acceptance", "veto present: leave Ready to Work")
        if READY_LABEL not in labs:
            return Decision("spec-acceptance", "omnigent:ready removed")
        if REVIEW_LENA not in labs or REVIEW_VIKTOR not in labs:
            return Decision("spec-acceptance", "Ready chain incomplete")
        if parked:
            return Decision("backlog", "parked: not Ready to Work")

    if parked:
        if current_key is None:
            return Decision("backlog", "parked new issue → Backlog")
        return Decision(None, "parked: stay put")

    if spec_event and current_key in (None, "backlog"):
        return Decision("spec-acceptance", "spec freeze or acceptance/fixround posted")

    if current_key is None or (new_issue and current_key is None):
        return Decision("backlog", "new issue / no Status → Backlog")

    return Decision(None, "no Status change")


def _run(args: list[str], *, input_text: str | None = None) -> str:
    try:
        raw = subprocess.check_output(
            args,
            input=None if input_text is None else input_text.encode(),
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as e:
        err = ANSI.sub("", (e.output or b"").decode())
        raise SystemExit(
            f"command failed ({e.returncode}): {' '.join(args[:4])}\n{err[:1200]}"
        ) from e
    return ANSI.sub("", raw.decode())


def gql(query: str, **variables: Any) -> dict:
    payload = {
        "query": query,
        "variables": {k: v for k, v in variables.items() if v is not None},
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(payload, fh)
        path = fh.name
    try:
        text = _run(["gh", "api", "graphql", "--input", path])
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    try:
        body = json.loads(text)
    except json.JSONDecodeError as e:
        raise SystemExit(f"graphql: not JSON: {text[:400]!r}") from e
    if body.get("errors"):
        raise SystemExit(f"graphql errors: {body['errors']}")
    return body["data"]


def issue_project_item(number: int) -> tuple[str | None, str | None, dict]:
    data = gql(
        """
        query($n: Int!) {
          repository(owner: "jholze", name: "xagent-trading-bot") {
            issue(number: $n) {
              number title state
              labels(first: 40) { nodes { name } }
              projectItems(first: 10) {
                nodes {
                  id
                  project { id title }
                  fieldValues(first: 20) {
                    nodes {
                      ... on ProjectV2ItemFieldSingleSelectValue {
                        name
                        field { ... on ProjectV2SingleSelectField { name } }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """,
        n=number,
    )
    issue = (data.get("repository") or {}).get("issue")
    if not issue:
        raise SystemExit(f"issue #{number} not found")
    item_id = None
    status = None
    for node in (issue.get("projectItems") or {}).get("nodes") or []:
        proj = node.get("project") or {}
        if proj.get("id") == PROJECT_ID or proj.get("title") == PROJECT_TITLE:
            item_id = node.get("id")
            for fv in (node.get("fieldValues") or {}).get("nodes") or []:
                if (fv.get("field") or {}).get("name") == "Status":
                    status = fv.get("name")
            break
    return item_id, status, issue


def ensure_on_board(number: int) -> str:
    item_id, _status, _issue = issue_project_item(number)
    if item_id:
        return item_id
    _run(
        [
            "gh",
            "issue",
            "edit",
            str(number),
            "--repo",
            REPO,
            "--add-project",
            PROJECT_TITLE,
        ]
    )
    item_id, _status, _issue = issue_project_item(number)
    if not item_id:
        raise SystemExit(f"#{number} still not on {PROJECT_TITLE} after add-project")
    return item_id


def set_status(number: int, status_key: str) -> dict:
    """Set Status only. Does not add/remove labels or close the issue."""
    status_key = normalize_status(status_key)
    option_id, label = STATUSES[status_key]
    if not option_id:
        raise SystemExit("refusing empty option id")
    item_id, current, issue = issue_project_item(number)
    if current and BY_NAME.get(current.lower()) == status_key and item_id:
        return {
            "issue": number,
            "status": label,
            "changed": False,
            "title": issue.get("title"),
            "labels_written": False,
        }
    if not item_id:
        item_id = ensure_on_board(number)
    gql(
        """
        mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
          updateProjectV2ItemFieldValue(input: {
            projectId: $project
            itemId: $item
            fieldId: $field
            value: { singleSelectOptionId: $option }
          }) { projectV2Item { id } }
        }
        """,
        project=PROJECT_ID,
        item=item_id,
        field=STATUS_FIELD,
        option=option_id,
    )
    _id2, now, _issue = issue_project_item(number)
    if not now or BY_NAME.get((now or "").lower()) != status_key:
        raise SystemExit(f"#{number} Status did not stick (wanted {label}, got {now!r})")
    return {
        "issue": number,
        "status": label,
        "changed": True,
        "from": current,
        "title": issue.get("title"),
        "labels_written": False,
    }


def apply_decision(number: int, decision: Decision, *, dry_run: bool) -> dict:
    if not decision.want:
        return {
            "issue": number,
            "changed": False,
            "reason": decision.reason,
            "labels_written": False,
        }
    if dry_run:
        return {
            "issue": number,
            "changed": False,
            "dry_run": True,
            "want": status_label(decision.want),
            "reason": decision.reason,
            "labels_written": False,
        }
    result = set_status(number, decision.want)
    result["reason"] = decision.reason
    result["labels_written"] = False
    return result


def _issue_labels(issue: dict) -> list[str]:
    return [n["name"] for n in (issue.get("labels") or {}).get("nodes") or []]


def linked_issue_numbers(*texts: str | None) -> list[int]:
    found: list[int] = []
    seen: set[int] = set()
    for text in texts:
        for m in ISSUE_REF_RE.finditer(text or ""):
            n = int(m.group(1))
            if n not in seen:
                seen.add(n)
                found.append(n)
    return found


def closing_issue_numbers(pr_number: int) -> list[int]:
    data = gql(
        """
        query($n: Int!) {
          repository(owner: "jholze", name: "xagent-trading-bot") {
            pullRequest(number: $n) {
              closingIssuesReferences(first: 20) { nodes { number } }
            }
          }
        }
        """,
        n=pr_number,
    )
    pr = ((data.get("repository") or {}).get("pullRequest") or {})
    nodes = ((pr.get("closingIssuesReferences") or {}).get("nodes") or [])
    return [int(n["number"]) for n in nodes if n.get("number")]


def apply_issue(
    number: int,
    *,
    new_issue: bool = False,
    spec_event: bool = False,
    soak_merged_staging: bool = False,
    dry_run: bool = False,
) -> dict:
    item_id, current, issue = issue_project_item(number)
    labels = _issue_labels(issue)
    decision = decide_status(
        issue_state=issue.get("state") or "",
        labels=labels,
        current=current,
        new_issue=new_issue or not item_id or current is None,
        spec_event=spec_event,
        soak_merged_staging=soak_merged_staging,
    )
    result = apply_decision(number, decision, dry_run=dry_run)
    result["title"] = issue.get("title")
    result["from"] = current
    result["state"] = issue.get("state")
    return result


def _load_event(path: str | None) -> tuple[str, dict]:
    event_name = os.environ.get("GITHUB_EVENT_NAME") or ""
    event_path = path or os.environ.get("GITHUB_EVENT_PATH") or ""
    if not event_path or not os.path.isfile(event_path):
        raise SystemExit("GITHUB_EVENT_PATH missing")
    with open(event_path, encoding="utf-8") as fh:
        event = json.load(fh)
    return event_name, event


def apply_github_event(*, event_path: str | None, dry_run: bool) -> list[dict]:
    event_name, event = _load_event(event_path)
    results: list[dict] = []

    if event_name == "pull_request":
        pr = event.get("pull_request") or {}
        if not pr.get("merged"):
            return [{"skipped": True, "reason": "PR not merged", "labels_written": False}]
        base = ((pr.get("base") or {}).get("ref")) or ""
        if base == PROD_BRANCH:
            return [
                {
                    "skipped": True,
                    "reason": "PR targets main — never touch main",
                    "labels_written": False,
                }
            ]
        if base != DEV_BRANCH:
            return [
                {
                    "skipped": True,
                    "reason": f"PR base {base!r} is not staging",
                    "labels_written": False,
                }
            ]
        pr_number = int(pr["number"])
        nums = closing_issue_numbers(pr_number)
        for n in linked_issue_numbers(pr.get("title"), pr.get("body")):
            if n not in nums:
                nums.append(n)
        soak_blob = f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
        for n in nums:
            _item, _cur, issue = issue_project_item(n)
            labs = labels_set(_issue_labels(issue))
            issue_blob = f"{issue.get('title') or ''}"
            if not is_soak_issue(labs, soak_blob, issue_blob):
                results.append(
                    {
                        "issue": n,
                        "changed": False,
                        "reason": "staging merge is not a paper/shadow soak",
                        "labels_written": False,
                    }
                )
                continue
            results.append(
                apply_issue(
                    n,
                    soak_merged_staging=True,
                    dry_run=dry_run,
                )
            )
        if not nums:
            results.append(
                {
                    "skipped": True,
                    "reason": "merged staging PR has no linked issue (PRs stay off the board)",
                    "labels_written": False,
                }
            )
        return results

    if event_name in ("issues", "issue_comment"):
        issue = event.get("issue") or {}
        number = int(issue["number"])
        new_issue = event_name == "issues" and (event.get("action") == "opened")
        spec_event = False
        if event_name == "issue_comment" and event.get("action") == "created":
            spec_event = is_spec_event_text((event.get("comment") or {}).get("body"))
        results.append(
            apply_issue(
                number,
                new_issue=new_issue,
                spec_event=spec_event,
                dry_run=dry_run,
            )
        )
        return results

    if event_name == "workflow_dispatch":
        inputs = event.get("inputs") or {}
        raw = (inputs.get("issue") or "").strip()
        if not raw:
            return [
                {
                    "skipped": True,
                    "reason": "workflow_dispatch without issue number",
                    "labels_written": False,
                }
            ]
        results.append(apply_issue(int(raw), dry_run=dry_run))
        return results

    return [
        {
            "skipped": True,
            "reason": f"unhandled event {event_name!r}",
            "labels_written": False,
        }
    ]


def cmd_show(number: int) -> int:
    item_id, status, issue = issue_project_item(number)
    labels = _issue_labels(issue)
    print(
        json.dumps(
            {
                "issue": number,
                "title": issue.get("title"),
                "state": issue.get("state"),
                "labels": labels,
                "on_board": bool(item_id),
                "status": status,
            },
            indent=2,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_show = sub.add_parser("show", help="print issue Status + labels")
    p_show.add_argument("issue", type=int)

    p_set = sub.add_parser("set", help="explicit Status move (no labels)")
    p_set.add_argument("issue", type=int)
    p_set.add_argument("status")

    p_apply = sub.add_parser("apply", help="apply policy to an issue or GitHub event")
    p_apply.add_argument("--issue", type=int)
    p_apply.add_argument("--event", action="store_true")
    p_apply.add_argument("--event-path")
    p_apply.add_argument("--dry-run", action="store_true")
    p_apply.add_argument("--new-issue", action="store_true")
    p_apply.add_argument("--spec-event", action="store_true")
    p_apply.add_argument("--soak-merged-staging", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "show":
        return cmd_show(args.issue)

    if args.cmd == "set":
        result = set_status(args.issue, args.status)
        print(json.dumps(result, indent=2))
        return 0

    if args.cmd == "apply":
        if args.event or args.event_path:
            results = apply_github_event(
                event_path=args.event_path, dry_run=args.dry_run
            )
            print(json.dumps(results, indent=2))
            return 0
        if not args.issue:
            raise SystemExit("apply requires --issue or --event")
        result = apply_issue(
            args.issue,
            new_issue=args.new_issue,
            spec_event=args.spec_event,
            soak_merged_staging=args.soak_merged_staging,
            dry_run=args.dry_run,
        )
        print(json.dumps(result, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
