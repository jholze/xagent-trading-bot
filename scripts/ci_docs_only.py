#!/usr/bin/env python3
"""Allowlist for skipping pytest on a documentation-only pull request.

Pytest may be skipped only when every changed path is documentation:

* a ``*.md`` file that is not under ``tests/``, not a fixture, and not a
  workflow file
* a file under ``docs/`` that is not config

Config, YAML/JSON/TOML, bundle specs, fixtures, anything under ``tests/``,
workflow files, and any other non-markdown file require the full suite.
An empty or unreadable change list also requires the suite (fail closed).

The ``unit`` status check uses :func:`required_check_should_pass` so a
skipped pytest job does not leave the required check missing.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import sys
import urllib.error
import urllib.request
from typing import Iterable, Mapping, Sequence

# Extensions that are config even when the file sits under docs/.
_CONFIG_EXTENSIONS = (
    ".yml",
    ".yaml",
    ".json",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".lock",
)

# Basenames (lowercase) that are config or dependency/bundle manifests.
_CONFIG_BASENAMES = frozenset(
    {
        ".env",
        "bundle.json",
        "bundle.spec",
        "bundle.toml",
        "bundle.yaml",
        "bundle.yml",
        "cargo.lock",
        "cargo.toml",
        "compose.yaml",
        "compose.yml",
        "docker-compose.yaml",
        "docker-compose.yml",
        "dockerfile",
        "gemfile",
        "gemfile.lock",
        "go.mod",
        "go.sum",
        "makefile",
        "package-lock.json",
        "package.json",
        "pipfile",
        "pipfile.lock",
        "poetry.lock",
        "pyproject.toml",
        "requirements.txt",
        "setup.cfg",
        "setup.py",
    }
)

_FIXTURE_DIRS = frozenset({"fixture", "fixtures", "__fixtures__"})


def normalize_path(path: str) -> str:
    """Repo-relative posix path. Empty when the input is not a file path."""
    text = path.strip().replace("\\", "/")
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    if not text:
        return ""
    text = posixpath.normpath(text)
    if text in {".", ""}:
        return ""
    return text


def _parts(path: str) -> tuple[str, ...] | None:
    """Path segments, or None when the path is not a safe repo-relative file."""
    text = normalize_path(path)
    if not text or text.startswith("../") or text == ".." or text.startswith("/"):
        return None
    parts = tuple(part for part in text.split("/") if part not in {"", "."})
    if not parts or any(part == ".." for part in parts):
        return None
    return parts


def _under_dir(parts: Sequence[str], name: str) -> bool:
    return any(part.lower() == name.lower() for part in parts[:-1])


def is_workflow_path(path: str) -> bool:
    parts = _parts(path)
    if parts is None or len(parts) < 3:
        return False
    return parts[0].lower() == ".github" and parts[1].lower() == "workflows"


def is_fixture_path(path: str) -> bool:
    parts = _parts(path)
    if parts is None:
        return False
    return any(part.lower() in _FIXTURE_DIRS for part in parts[:-1])


def is_config_path(path: str) -> bool:
    """YAML/JSON/TOML and other config or dependency manifests."""
    parts = _parts(path)
    if parts is None:
        return False
    lower = parts[-1].lower()
    if lower in _CONFIG_BASENAMES or lower.startswith(".env"):
        return True
    if lower.startswith("requirements") and lower.endswith(".txt"):
        return True
    return lower.endswith(_CONFIG_EXTENSIONS)


def is_bundle_spec(path: str) -> bool:
    """Bundle / package spec files, including ones checked in under docs/."""
    parts = _parts(path)
    if parts is None:
        return False
    lower = parts[-1].lower()
    if lower.endswith((".spec", ".bundle", ".gemspec")):
        return True
    if "bundle" in lower and lower.endswith(
        (".json", ".yaml", ".yml", ".toml", ".xml", ".txt", ".spec")
    ):
        return True
    return False


def is_documentation_path(path: str) -> bool:
    """True only for an allowlisted documentation path.

    Anything that does not positively match is not documentation.
    """
    parts = _parts(path)
    if parts is None:
        return False
    if is_workflow_path(path) or is_fixture_path(path) or _under_dir(parts, "tests"):
        return False
    if is_config_path(path) or is_bundle_spec(path):
        return False
    lower = parts[-1].lower()
    if lower.endswith(".md"):
        return True
    # Non-config file under docs/ (the only non-markdown exception).
    return parts[0] == "docs"


def pytest_required(paths: Iterable[str]) -> bool:
    """True unless every changed path is documentation. Empty fails closed."""
    items = list(paths)
    if not items:
        return True
    return any(not is_documentation_path(path) for path in items)


def paths_from_name_status(text: str) -> list[str]:
    """Paths from ``git diff --name-status`` (both sides of a rename or copy)."""
    found: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        columns = line.split("\t")
        status = columns[0]
        rest = columns[1:]
        if status[:1] in {"R", "C"} and len(rest) >= 2:
            found.extend(rest[:2])
            continue
        if rest:
            found.append(rest[0])
            continue
        # Not a name-status row. Keep the raw line so it fails closed.
        found.append(line)
    return found


def paths_from_file_objects(objects: Iterable[Mapping[str, object]]) -> list[str]:
    """Paths from GitHub's pull-request files API, including rename sources."""
    found: list[str] = []
    for item in objects:
        filename = item.get("filename")
        if not isinstance(filename, str) or not filename:
            raise ValueError("pull request file entry has no filename")
        found.append(filename)
        previous = item.get("previous_filename")
        if isinstance(previous, str) and previous:
            found.append(previous)
    return found


def flatten_file_pages(payload: object) -> list[dict]:
    """Accept one page, a list of pages, or a single file object."""
    if isinstance(payload, dict):
        if "filename" not in payload:
            message = payload.get("message", "unexpected GitHub response")
            raise ValueError(f"pull request files API error: {message}")
        return [payload]
    if not isinstance(payload, list):
        raise ValueError("pull request files payload is not a list")
    pages: list[dict] = []
    for item in payload:
        if isinstance(item, list):
            pages.extend(flatten_file_pages(item))
        elif isinstance(item, dict):
            pages.extend(flatten_file_pages(item))
        else:
            raise ValueError("pull request files payload has a non-object entry")
    return pages


def fetch_pull_request_paths(repo: str, number: int, token: str) -> list[str]:
    """Every changed path on a pull request, including rename sources.

    Raises ``SystemExit`` when the API cannot be read. Callers must not
    treat that as an empty (docs-only) diff.
    """
    if not repo or "/" not in repo:
        raise SystemExit("GITHUB_REPOSITORY is missing")
    if number <= 0:
        raise SystemExit("pull request number is missing")
    if not token:
        raise SystemExit("GITHUB_TOKEN is missing")

    paths: list[str] = []
    page = 1
    while page <= 40:
        url = (
            f"https://api.github.com/repos/{repo}/pulls/{number}/files"
            f"?per_page=100&page={page}"
        )
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "ci-docs-only",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.load(response)
                link = response.headers.get("Link", "")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise SystemExit(
                f"listing pull request files failed: HTTP {exc.code} {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise SystemExit(f"listing pull request files failed: {exc.reason}") from exc
        if not isinstance(body, list):
            raise SystemExit("pull request files response was not a list")
        if not body:
            break
        try:
            paths.extend(paths_from_file_objects(body))
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        if 'rel="next"' not in link:
            break
        page += 1
    else:
        raise SystemExit("pull request files listing exceeded 40 pages")
    return paths


def required_check_should_pass(
    classify_result: str, pytest_result: str, run_pytest: str
) -> bool:
    """Whether the always-reporting ``unit`` check is allowed to succeed.

    Docs-only: classify succeeded, pytest was skipped, ``run_pytest`` is false.
    Code change: classify succeeded and pytest succeeded.
    """
    if classify_result != "success":
        return False
    if run_pytest == "true":
        return pytest_result == "success"
    if run_pytest == "false":
        return pytest_result == "skipped"
    return False


def _write_output(path: str, run_pytest: bool) -> None:
    line = f"run_pytest={'true' if run_pytest else 'false'}\n"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line)


def _print_decision(paths: Sequence[str], run: bool) -> None:
    print(f"run_pytest={'true' if run else 'false'}")
    if not paths:
        print("  (no changed files — fail closed, pytest runs)")
        return
    for path in paths:
        kind = "docs" if is_documentation_path(path) else "ci"
        print(f"  {kind}\t{path}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        help="Changed path. Repeat for each file. Empty still runs pytest.",
    )
    parser.add_argument(
        "--name-status",
        help="File of git diff --name-status output, or - for stdin.",
    )
    parser.add_argument(
        "--github-files",
        help="JSON file from the pull request files API (one page or slurp).",
    )
    parser.add_argument(
        "--pull",
        type=int,
        help="Read changed files from this pull request number.",
    )
    parser.add_argument(
        "--github-output",
        help="Append run_pytest=true|false for GITHUB_OUTPUT.",
    )
    parser.add_argument(
        "--required-check",
        action="store_true",
        help="Exit 0 when the unit check should pass, 1 otherwise.",
    )
    parser.add_argument("--classify-result", default="")
    parser.add_argument("--pytest-result", default="")
    parser.add_argument("--run-pytest", default="")
    args = parser.parse_args(argv)

    if args.required_check:
        ok = required_check_should_pass(
            args.classify_result, args.pytest_result, args.run_pytest
        )
        print(
            "unit check "
            f"{'pass' if ok else 'fail'}: "
            f"classify={args.classify_result or '-'} "
            f"pytest={args.pytest_result or '-'} "
            f"run_pytest={args.run_pytest or '-'}"
        )
        return 0 if ok else 1

    paths = list(args.path)
    if args.name_status:
        if args.name_status == "-":
            text = sys.stdin.read()
        else:
            with open(args.name_status, encoding="utf-8") as handle:
                text = handle.read()
        paths.extend(paths_from_name_status(text))
    if args.github_files:
        with open(args.github_files, encoding="utf-8") as handle:
            payload = json.load(handle)
        try:
            paths.extend(paths_from_file_objects(flatten_file_pages(payload)))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if args.pull is not None:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        token = os.environ.get("GITHUB_TOKEN", "")
        paths.extend(fetch_pull_request_paths(repo, args.pull, token))

    run = pytest_required(paths)
    _print_decision(paths, run)
    if args.github_output:
        _write_output(args.github_output, run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
