"""PR review gates for the paper bot.

Non-money-path pull requests pass when either:

- the diff adds or modifies a ``docs/evidence/*.md`` file that contains a
  markdown table whose header cells include ``before`` and ``after``, or
- the pull-request body has an ``Evidence`` heading (levels 1–3) followed
  by that same table.

Money-path pull requests pass only when both of the following hold:

- the pull request carries the label ``owner-approved``
- the diff adds or modifies ``docs/evidence/<id>.md`` (also ``pr-<id>.md``
  or ``issue-<id>.md``) whose ``<id>`` is the pull-request number or an
  issue number cited as ``#<id>`` in the title or body, and that file
  contains the before/after table.

Money-path files are listed in ``ci/money_paths.json``. ``config.json``
counts only when a changed key is under one of ``key_prefixes``.

Outside GitHub Actions the script skips (exit 0). Inside Actions a missing
pull-request payload fails closed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RULES_PATH = REPO_ROOT / "ci" / "money_paths.json"
OWNER_LABEL = "owner-approved"
_EVIDENCE_NAME = re.compile(r"^docs/evidence/(?:pr-|issue-)?(\d+)\.md$")
_HEADING = re.compile(r"^#{1,3}[ \t]+Evidence[ \t]*$", re.IGNORECASE)
_ISSUE_REF = re.compile(r"#(\d+)")
_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}")


@dataclass
class GateDecision:
    ok: bool
    money_path: bool
    failures: list[str] = field(default_factory=list)


def load_rules(path: Path = RULES_PATH) -> dict:
    with path.open(encoding="utf-8") as handle:
        rules = json.load(handle)
    if not isinstance(rules.get("prefixes"), list) or not rules["prefixes"]:
        raise ValueError("money path prefixes missing")
    config = rules.get("config_json")
    if not isinstance(config, dict):
        raise ValueError("config_json rules missing")
    if not isinstance(config.get("key_prefixes"), list) or not config["key_prefixes"]:
        raise ValueError("config_json key_prefixes missing")
    return rules


def changed_config_paths(before: object, after: object, prefix: str = "") -> set[str]:
    """Dotted keys whose JSON value changed. A replaced object lists itself."""
    if isinstance(before, dict) and isinstance(after, dict):
        paths: set[str] = set()
        for key in set(before) | set(after):
            child = f"{prefix}.{key}" if prefix else str(key)
            if key not in before or key not in after:
                paths.add(child)
                continue
            if before[key] != after[key]:
                nested = changed_config_paths(before[key], after[key], child)
                paths |= nested or {child}
        return paths
    if before != after:
        return {prefix or "<root>"}
    return set()


def is_money_config_path(path: str, key_prefixes: list[str]) -> bool:
    for prefix in key_prefixes:
        if path == prefix or path.startswith(prefix + "."):
            return True
    return False


def money_path_touched(
    files: list[str],
    config_paths: set[str] | None,
    rules: dict,
) -> bool:
    prefixes = tuple(rules["prefixes"])
    config_file = str(rules["config_json"]["path"])
    key_prefixes = list(rules["config_json"]["key_prefixes"])
    for name in files:
        normalized = name.replace("\\", "/")
        if normalized == config_file:
            if config_paths is None:
                return True
            if any(is_money_config_path(path, key_prefixes) for path in config_paths):
                return True
            continue
        if normalized.startswith(prefixes):
            return True
    return False


def has_before_after_table(text: str) -> bool:
    lines = text.splitlines()
    for index, line in enumerate(lines[:-1]):
        if "|" not in line:
            continue
        cells = [cell.strip().lower() for cell in line.strip().strip("|").split("|")]
        if "before" not in cells or "after" not in cells:
            continue
        if not _SEPARATOR.search(lines[index + 1]):
            continue
        if index + 2 < len(lines) and "|" in lines[index + 2]:
            return True
    return False


def body_has_evidence(body: str) -> bool:
    lines = (body or "").splitlines()
    for index, line in enumerate(lines):
        if _HEADING.match(line.strip()):
            return has_before_after_table("\n".join(lines[index + 1 :]))
    return False


def cited_issue_numbers(title: str, body: str) -> set[str]:
    return set(_ISSUE_REF.findall(f"{title or ''}\n{body or ''}"))


def matching_evidence_files(
    files: list[str],
    *,
    pr_number: int | None,
    issue_numbers: set[str],
) -> list[str]:
    allowed = set(issue_numbers)
    if pr_number is not None:
        allowed.add(str(pr_number))
    matched: list[str] = []
    for name in files:
        found = _EVIDENCE_NAME.match(name.replace("\\", "/"))
        if found and found.group(1) in allowed:
            matched.append(name)
    return matched


def decide_gate(
    *,
    files: list[str],
    config_paths: set[str] | None,
    rules: dict,
    labels: list[str],
    body: str,
    title: str,
    pr_number: int | None,
    evidence_text: dict[str, str],
) -> GateDecision:
    money = money_path_touched(files, config_paths, rules)
    failures: list[str] = []
    if money:
        if OWNER_LABEL not in labels:
            failures.append(f"money path requires label {OWNER_LABEL}")
        issues = cited_issue_numbers(title, body)
        named = matching_evidence_files(
            files, pr_number=pr_number, issue_numbers=issues
        )
        if not named:
            failures.append(
                "money path requires docs/evidence/<pr-or-issue>.md "
                "named with the PR number or an issue cited as #N"
            )
        else:
            ok_table = False
            for name in named:
                if has_before_after_table(evidence_text.get(name, "")):
                    ok_table = True
                    break
            if not ok_table:
                failures.append(
                    "money-path evidence file needs a before/after markdown table"
                )
    else:
        evidence_files = [
            name
            for name in files
            if name.replace("\\", "/").startswith("docs/evidence/")
            and name.endswith(".md")
        ]
        file_ok = any(
            has_before_after_table(evidence_text.get(name, ""))
            for name in evidence_files
        )
        if not file_ok and not body_has_evidence(body):
            failures.append(
                "non-money-path PR needs docs/evidence/*.md or an Evidence "
                "section in the PR body, with a before/after table"
            )
    return GateDecision(ok=not failures, money_path=money, failures=failures)


def _git_show(rev: str, path: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "show", f"{rev}:{path}"],
            cwd=REPO_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return None


def _changed_files(base: str, head: str) -> list[str]:
    output = subprocess.check_output(
        ["git", "diff", "--name-only", "--diff-filter=ACMRD", base, head],
        cwd=REPO_ROOT,
        text=True,
    )
    return [line.strip() for line in output.splitlines() if line.strip()]


def _config_paths(base: str, head: str, config_file: str) -> set[str] | None:
    before_text = _git_show(base, config_file)
    after_text = _git_show(head, config_file)
    if before_text is None and after_text is None:
        return set()
    try:
        before = json.loads(before_text) if before_text is not None else {}
        after = json.loads(after_text) if after_text is not None else {}
    except json.JSONDecodeError as exc:
        raise SystemExit(f"review gate: {config_file} is not valid JSON ({exc})") from exc
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise SystemExit(f"review gate: {config_file} must be a JSON object")
    return changed_config_paths(before, after)


def _load_event() -> dict:
    path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def main() -> int:
    event = _load_event()
    pr = event.get("pull_request")
    if not isinstance(pr, dict):
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(
                "review gate: Actions run has no pull_request payload",
                file=sys.stderr,
            )
            return 1
        print("review gate: not a pull request; skipped")
        return 0

    base = os.environ.get("REVIEW_GATE_BASE") or (pr.get("base") or {}).get("sha")
    head = os.environ.get("REVIEW_GATE_HEAD") or (pr.get("head") or {}).get("sha")
    if not base or not head:
        print("review gate: missing base or head sha", file=sys.stderr)
        return 1

    rules = load_rules()
    files = _changed_files(str(base), str(head))
    config_file = str(rules["config_json"]["path"])
    config_paths = None
    if config_file in files:
        config_paths = _config_paths(str(base), str(head), config_file)

    labels = [
        str(item.get("name"))
        for item in (pr.get("labels") or [])
        if isinstance(item, dict) and item.get("name")
    ]
    body = pr.get("body") or ""
    title = pr.get("title") or ""
    number = pr.get("number")
    pr_number = int(number) if isinstance(number, int) else None

    evidence_text: dict[str, str] = {}
    for name in files:
        if not name.endswith(".md") or not name.startswith("docs/evidence/"):
            continue
        text = _git_show(str(head), name)
        if text is None:
            disk = REPO_ROOT / name
            text = disk.read_text(encoding="utf-8") if disk.is_file() else ""
        evidence_text[name] = text

    decision = decide_gate(
        files=files,
        config_paths=config_paths,
        rules=rules,
        labels=labels,
        body=body,
        title=title,
        pr_number=pr_number,
        evidence_text=evidence_text,
    )
    kind = "money-path" if decision.money_path else "non-money-path"
    print(f"review gate: {kind} files={len(files)}")
    if decision.ok:
        print("review gate: pass")
        return 0
    for failure in decision.failures:
        print(f"review gate: FAIL {failure}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
