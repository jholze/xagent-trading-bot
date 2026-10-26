#!/usr/bin/env python3
"""Allowlist for skipping pytest on a documentation-only pull request.

Pytest may be skipped only when every changed path is documentation:

* a ``*.md`` file that is not under ``tests/``, ``.github/``, a fixture
  directory, or a workflow path
* a file under ``docs/`` whose name ends in ``.md`` or an image extension
  (``.png``, ``.jpg``, ``.jpeg``, ``.gif``, ``.svg``, ``.webp``)

Anything else runs the suite, including other files under ``docs/``
(``.py``, ``.sh``, ``Dockerfile``, ``Procfile``, ``.env``-style names,
YAML/JSON/TOML, files with no extension). Config names match ``.env*``,
``*.env``, and ``*.env.*``, not only a ``.env`` prefix.

A docs path whose repo path or basename appears under ``tests/`` is not
documentation-only. An empty diff, a file list that does not match
``changed_files``, a list truncated at 3000 files, or a head SHA that
does not match the pull_request event also runs the suite.

Changes to ``scripts/ci_docs_only.py``, ``tests/unit/test_ci_docs_only.py``,
or anything under ``.github/`` always run the suite.

The ``unit`` status check uses :func:`required_check_should_pass`. A
docs-only skip is the only case that passes without pytest succeeding.
Classify failure, cancel, or skip never passes unless pytest itself
succeeded.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import sys
import urllib.error
import urllib.request
from typing import Callable, Iterable, Mapping, Sequence

# Extensions that are config even outside the docs/ allowlist.
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
        "procfile",
        "pyproject.toml",
        "requirements.txt",
        "setup.cfg",
        "setup.py",
    }
)

# Under docs/, only these extensions may skip pytest.
_DOCS_SKIP_EXTENSIONS = (
    ".md",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
)

_FIXTURE_DIRS = frozenset({"fixture", "fixtures", "__fixtures__"})

_CLASSIFIER_PATHS = frozenset(
    {
        "scripts/ci_docs_only.py",
        "tests/unit/test_ci_docs_only.py",
    }
)

# GitHub's pull-request files API returns at most this many files.
_FILES_CAP = 3000
_FILES_PAGE = 100

UrlOpen = Callable[..., object]


class PullFileList:
    """Changed files from the pull-request API, plus the metadata used to trust them."""

    def __init__(
        self,
        paths: Sequence[str],
        fetched_count: int,
        changed_files: int,
        head_sha: str,
        truncated: bool,
    ) -> None:
        self.paths = list(paths)
        self.fetched_count = fetched_count
        self.changed_files = changed_files
        self.head_sha = head_sha
        self.truncated = truncated


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


def _forces_full_pytest(parts: Sequence[str]) -> bool:
    """Classifier, its test, and anything under .github/ always run pytest."""
    rel = "/".join(parts)
    if rel in _CLASSIFIER_PATHS:
        return True
    return parts[0].lower() == ".github"


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


def is_env_filename(name: str) -> bool:
    """True for ``.env*``, ``*.env``, and ``*.env.*``.

    Matching only a ``.env`` prefix misses ``prod.env`` and
    ``railway.env.example``.
    """
    lower = name.lower()
    return lower.startswith(".env") or lower.endswith(".env") or ".env." in lower


def is_config_path(path: str) -> bool:
    """YAML/JSON/TOML and other config or dependency manifests."""
    parts = _parts(path)
    if parts is None:
        return False
    lower = parts[-1].lower()
    if lower in _CONFIG_BASENAMES or is_env_filename(lower):
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
    This does not look at tests/; :func:`pytest_required` does that.
    """
    parts = _parts(path)
    if parts is None:
        return False
    if _forces_full_pytest(parts):
        return False
    if is_workflow_path(path) or is_fixture_path(path) or _under_dir(parts, "tests"):
        return False
    if is_config_path(path) or is_bundle_spec(path):
        return False
    name = parts[-1].lower()
    if parts[0] == "docs":
        return name.endswith(_DOCS_SKIP_EXTENSIONS)
    return name.endswith(".md")


def docs_referenced_by_tests(paths: Sequence[str], tests_root: os.PathLike[str] | str) -> list[str]:
    """Docs paths whose path or basename is found under ``tests_root``.

    The scan is a content search, not a hardcoded list. If ``tests_root``
    cannot be read, every docs path is returned so the caller runs pytest.
    """
    interesting: list[tuple[str, bytes, bytes]] = []
    for path in paths:
        parts = _parts(path)
        if parts is None or parts[0] != "docs":
            continue
        rel = "/".join(parts)
        base = parts[-1]
        if not base:
            continue
        interesting.append((path, rel.encode("utf-8"), base.encode("utf-8")))
    if not interesting:
        return []
    root = os.fspath(tests_root)
    if not os.path.isdir(root):
        return [item[0] for item in interesting]
    found: list[str] = []
    pending = list(interesting)
    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                try:
                    with open(os.path.join(dirpath, filename), "rb") as handle:
                        data = handle.read()
                except OSError:
                    return [item[0] for item in interesting]
                still: list[tuple[str, bytes, bytes]] = []
                for original, rel, base in pending:
                    if rel in data or base in data:
                        found.append(original)
                    else:
                        still.append((original, rel, base))
                pending = still
                if not pending:
                    return found
    except OSError:
        return [item[0] for item in interesting]
    return found


def pytest_required(
    paths: Iterable[str], tests_root: os.PathLike[str] | str | None = None
) -> bool:
    """True unless every changed path is documentation. Empty fails closed.

    When ``tests_root`` is set, a docs path referenced from that tree also
    requires pytest. A missing tests directory fails closed for docs paths.
    """
    items = list(paths)
    if not items:
        return True
    if any(not is_documentation_path(path) for path in items):
        return True
    if tests_root is None:
        return False
    return bool(docs_referenced_by_tests(items, tests_root))


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


def _open_json(opener: UrlOpen, url: str, token: str) -> tuple[object, str]:
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
        with opener(request, timeout=30) as response:
            body = json.load(response)
            headers = getattr(response, "headers", {}) or {}
            link = headers.get("Link", "") if hasattr(headers, "get") else ""
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise SystemExit(
            f"listing pull request files failed: HTTP {exc.code} {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"listing pull request files failed: {exc.reason}") from exc
    return body, link or ""


def fetch_pull_request_paths(
    repo: str,
    number: int,
    token: str,
    *,
    opener: UrlOpen | None = None,
) -> PullFileList:
    """Changed paths for a pull request, including removed files and rename sources.

    Raises ``SystemExit`` when the API cannot be read. Callers must not
    treat that as an empty (docs-only) diff. A list of ``_FILES_CAP`` files
    is marked truncated: GitHub does not return more than that.
    """
    if not repo or "/" not in repo:
        raise SystemExit("GITHUB_REPOSITORY is missing")
    if number <= 0:
        raise SystemExit("pull request number is missing")
    if not token:
        raise SystemExit("GITHUB_TOKEN is missing")
    opener = opener or urllib.request.urlopen

    meta, _link = _open_json(
        opener, f"https://api.github.com/repos/{repo}/pulls/{number}", token
    )
    if not isinstance(meta, dict) or "head" not in meta:
        message = meta.get("message") if isinstance(meta, dict) else "not an object"
        raise SystemExit(f"pull request metadata unusable: {message}")
    head = meta.get("head")
    head_sha = head.get("sha") if isinstance(head, dict) else None
    changed = meta.get("changed_files")
    if not isinstance(head_sha, str) or not head_sha or not isinstance(changed, int):
        raise SystemExit("pull request metadata missing head sha or changed_files")

    files: list[dict] = []
    page = 1
    truncated = False
    while len(files) < _FILES_CAP:
        url = (
            f"https://api.github.com/repos/{repo}/pulls/{number}/files"
            f"?per_page={_FILES_PAGE}&page={page}"
        )
        body, link = _open_json(opener, url, token)
        if not isinstance(body, list):
            raise SystemExit("pull request files response was not a list")
        if not body:
            break
        room = _FILES_CAP - len(files)
        files.extend(body[:room])
        if len(body) > room or len(files) >= _FILES_CAP:
            truncated = True
            break
        if 'rel="next"' not in link:
            break
        page += 1
    if len(files) >= _FILES_CAP:
        truncated = True
    try:
        paths = paths_from_file_objects(files)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return PullFileList(
        paths=paths,
        fetched_count=len(files),
        changed_files=changed,
        head_sha=head_sha,
        truncated=truncated,
    )


def pull_requires_pytest(
    listing: PullFileList,
    expected_head: str,
    tests_root: os.PathLike[str] | str | None = None,
) -> tuple[bool, str]:
    """Whether pytest must run for this fetched file list.

    A count mismatch, a 3000-file truncation, or a head SHA mismatch
    requires pytest even when every fetched path looks like documentation.
    """
    if (
        listing.truncated
        or listing.fetched_count >= _FILES_CAP
        or listing.changed_files >= _FILES_CAP
    ):
        return True, "pull request file list is truncated at 3000"
    if listing.fetched_count != listing.changed_files:
        return (
            True,
            f"fetched {listing.fetched_count} files, changed_files is {listing.changed_files}",
        )
    if not expected_head or listing.head_sha != expected_head:
        return True, "head sha does not match the pull_request event"
    if not listing.paths:
        return True, "empty diff"
    if pytest_required(listing.paths, tests_root=tests_root):
        if tests_root is not None:
            referenced = docs_referenced_by_tests(listing.paths, tests_root)
            if referenced:
                return True, "referenced by tests: " + ", ".join(referenced)
        return True, "non-documentation path"
    return False, "documentation allowlist"


def required_check_should_pass(
    classify_result: str, pytest_result: str, run_pytest: str
) -> bool:
    """Whether the always-reporting ``unit`` check is allowed to succeed.

    The only pass that does not require pytest to succeed is a successful
    classify that explicitly skipped pytest. Classify failure, cancel, or
    skip is run-or-fail: pytest must have succeeded, and a skipped pytest
    is never a pass.
    """
    if (
        classify_result == "success"
        and run_pytest == "false"
        and pytest_result == "skipped"
    ):
        return True
    if classify_result in {"failure", "cancelled", "skipped"} or classify_result != "success":
        return pytest_result == "success"
    if run_pytest == "true":
        return pytest_result == "success"
    return False


def _write_output(path: str, run_pytest: bool) -> None:
    line = f"run_pytest={'true' if run_pytest else 'false'}\n"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line)


def _print_paths(paths: Sequence[str], tests_root: os.PathLike[str] | str | None) -> None:
    referenced = set(docs_referenced_by_tests(paths, tests_root)) if tests_root else set()
    if not paths:
        print("  (no changed files — fail closed, pytest runs)")
        return
    shown = paths[:40]
    for path in shown:
        if path in referenced:
            kind = "referenced"
        elif is_documentation_path(path):
            kind = "docs"
        else:
            kind = "ci"
        print(f"  {kind}\t{path}")
    if len(paths) > len(shown):
        print(f"  ... {len(paths) - len(shown)} more")


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
        "--head-sha",
        default="",
        help="pull_request head SHA. A mismatch runs pytest.",
    )
    parser.add_argument(
        "--tests-root",
        default="",
        help="Directory to scan for references to changed docs paths.",
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

    tests_root = args.tests_root or None
    reason = ""
    if args.pull is not None:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        token = os.environ.get("GITHUB_TOKEN", "")
        listing = fetch_pull_request_paths(repo, args.pull, token)
        if not tests_root:
            tests_root = "tests"
        run, reason = pull_requires_pytest(listing, args.head_sha, tests_root)
        paths = listing.paths
    else:
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
        run = pytest_required(paths, tests_root=tests_root)
        if not paths:
            reason = "empty diff"
        elif run and tests_root:
            referenced = docs_referenced_by_tests(paths, tests_root)
            reason = (
                "referenced by tests: " + ", ".join(referenced)
                if referenced
                else "non-documentation path"
            )
        elif run:
            reason = "non-documentation path"
        else:
            reason = "documentation allowlist"

    print(f"run_pytest={'true' if run else 'false'}")
    if reason:
        print(f"reason: {reason}")
    _print_paths(paths, tests_root if not (args.pull is not None and run and "truncated" in reason) else None)
    if args.github_output:
        _write_output(args.github_output, run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
