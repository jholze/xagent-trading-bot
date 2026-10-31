"""Allowlist: pytest runs unless every changed path is documentation.

Under docs/, only .md and image files can skip. Env-style names, code,
shell, Docker/Procfile, and any other extension run pytest. A docs path
referenced from tests/ also runs pytest. The fetched file list must match
pull_request.changed_files and the event head SHA.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]


def _load():
    path = ROOT / "scripts" / "ci_docs_only.py"
    spec = importlib.util.spec_from_file_location("ci_docs_only", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


ci = _load()


# (changed paths, pytest runs?)
_CASES = [
    (["README.md"], False),
    (["README.MD"], False),
    (["README.Md"], False),
    (["docs/foo.md"], False),
    (["docs/img.png"], False),
    (["docs/photo.jpg"], False),
    (["docs/photo.jpeg"], False),
    (["docs/anim.gif"], False),
    (["docs/diagram.svg"], False),
    (["docs/diagram.webp"], False),
    (["plans/idea.md", "DOCUMENTATION.en.md"], False),
    (["docs/guide.md", "docs/sample.json"], True),
    (["docs/prod.env"], True),
    (["docs/prod.ENV"], True),
    (["docs/.env"], True),
    (["docs/railway.env.example"], True),
    (["docs/config.env.sample"], True),
    (["docs/x.env.example"], True),
    (["docs/sub/.env.production"], True),
    (["docs/envfile"], True),
    (["docs/x.py"], True),
    (["docs/x.PY"], True),
    (["docs/sub/x.py"], True),
    (["docs/x.md.py"], True),
    (["docs/x.html"], True),
    (["docs/x.pdf"], True),
    (["docs/x.unknownext"], True),
    (["docs/foo.md", "docs/x.py"], True),
    (["docs/foo.md", "README.md"], False),
    (["docs/example.py"], True),
    (["docs/.env.local"], True),
    (["docs/foo.env"], True),
    (["docs/conftest.py"], True),
    (["docs/sub/conftest.py"], True),
    (["docs/run.sh"], True),
    (["docs/Dockerfile"], True),
    (["docs/Dockerfile.dev"], True),
    (["docs/Procfile"], True),
    (["docs/notes.txt"], True),
    (["docs/noext"], True),
    (["docs/.python-version"], True),
    (["docs/settings.yaml"], True),
    (["docs/pyproject.toml"], True),
    (["docs/bundle.spec"], True),
    (["docs/agent.bundle"], True),
    (["docs/requirements.txt"], True),
    (["docs/fixtures/sample.md"], True),
    (["docs/tests/notes.md"], True),
    (["GUIDE.markdown"], True),
    (["logo.png"], True),
    (["tests/README.md"], True),
    (["tests/unit/test_ci_docs_only.py"], True),
    (["tests/fixtures/macro/btc_bars.json"], True),
    (["scripts/ci_docs_only.py"], True),
    ([".github/workflows/unit-tests.yml"], True),
    ([".github/workflows/README.md"], True),
    ([".github/actions/local/action.yml"], True),
    (["README.md", "scripts/ci_docs_only.py"], True),
    (["config.json"], True),
    (["config/fees.json"], True),
    (["requirements.txt"], True),
    (["pyproject.toml"], True),
    (["Dockerfile"], True),
    (["Makefile"], True),
    (["scripts/run_unit_tests.sh"], True),
    (["railway.json"], True),
    (["railway.toml"], True),
    ([], True),
    ([""], True),
    (["docs/../scripts/ci_docs_only.py"], True),
    (["Docs/notes.txt"], True),
]


class _Resp:
    def __init__(self, payload, link=""):
        self._raw = json.dumps(payload).encode("utf-8")
        self.headers = {"Link": link}

    def read(self):
        data = self._raw
        self._raw = b""
        return data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _opener(meta, pages):
    """pages: page number -> (file objects, link header)."""

    def opener(request, timeout=30):
        url = request.full_url
        if "/files?" not in url:
            return _Resp(meta)
        page = int(parse_qs(urlparse(url).query)["page"][0])
        body, link = pages(page)
        return _Resp(body, link)

    return opener


class TestDocsAllowlist(unittest.TestCase):
    def test_table_cases(self):
        for paths, runs in _CASES:
            with self.subTest(paths=paths):
                self.assertEqual(ci.pytest_required(paths), runs)

    def test_env_names_are_config_not_prefix_only(self):
        names = [
            "docs/prod.env",
            "docs/railway.env.example",
            "docs/config.env.sample",
            ".env",
            ".env.local",
            "prod.env",
        ]
        for name in names:
            with self.subTest(name=name):
                self.assertTrue(ci.is_env_filename(name.split("/")[-1]))
                self.assertTrue(ci.is_config_path(name))
                self.assertTrue(ci.pytest_required([name]))
        self.assertFalse("prod.env".startswith(".env"))
        self.assertTrue(ci.is_env_filename("prod.env"))
        self.assertFalse(ci.is_env_filename("readme.md"))

    def test_docs_file_referenced_from_tests_runs_pytest(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tests = Path(tmp) / "tests" / "unit"
            tests.mkdir(parents=True)
            (tests / "test_read.py").write_text(
                'doc = root / "docs" / "topic.md"\n',
                encoding="utf-8",
            )
            self.assertTrue(
                ci.pytest_required(["docs/topic.md"], tests_root=Path(tmp) / "tests")
            )
            self.assertTrue(
                ci.pytest_required(
                    ["docs/sub/topic.md"], tests_root=Path(tmp) / "tests"
                )
            )
            self.assertFalse(
                ci.pytest_required(["docs/foo.md"], tests_root=Path(tmp) / "tests")
            )
            self.assertFalse(
                ci.pytest_required(["docs/img.png"], tests_root=Path(tmp) / "tests")
            )
            self.assertFalse(
                ci.pytest_required(["README.md"], tests_root=Path(tmp) / "tests")
            )

    def test_real_tests_reference_universe_membership_doc(self):
        import shutil
        import tempfile

        product = ROOT / "tests" / "unit" / "test_universe_membership_revise_631.py"
        self.assertIn("universe_membership_631.md", product.read_text(encoding="utf-8"))
        self.assertNotIn(
            "universe_membership",
            (ROOT / "scripts" / "ci_docs_only.py").read_text(encoding="utf-8"),
        )
        path = "docs/universe_membership_631.md"
        self.assertTrue(ci.is_documentation_path(path))
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "unit"
            dest.mkdir()
            shutil.copy(product, dest / product.name)
            self.assertIn(path, ci.docs_referenced_by_tests([path], tmp))
            self.assertTrue(ci.pytest_required([path], tests_root=tmp))

    def test_pull_requires_pytest_runs_for_doc_read_by_tests(self):
        """CI decision path, not pytest_required alone.

        Commit 15e56b0f added this for mutant M3d. That mutant calls
        ``pytest_required`` from ``pull_requires_pytest`` without
        ``tests_root``. ``docs/universe_membership_631.md`` stays allowlisted
        markdown, so the mutant returns a docs-only skip and this assertion
        is red. The fixed call passes ``tests_root`` and the assertion is green.
        """
        path = "docs/universe_membership_631.md"
        listing = ci.PullFileList(
            paths=[path],
            fetched_count=1,
            changed_files=1,
            head_sha="abc123",
            truncated=False,
        )
        run, reason = ci.pull_requires_pytest(
            listing, "abc123", tests_root=ROOT / "tests"
        )
        self.assertTrue(run)
        self.assertIn(path, reason)
        skipped, skip_reason = ci.pull_requires_pytest(
            listing, "abc123", tests_root=None
        )
        self.assertFalse(skipped)
        self.assertEqual(skip_reason, "documentation allowlist")

    def test_missing_tests_tree_fails_closed_for_docs(self):
        missing = ROOT / "tests" / "does-not-exist-ci-docs"
        self.assertTrue(ci.pytest_required(["docs/foo.md"], tests_root=missing))

    def test_rename_from_code_to_markdown_runs_pytest(self):
        text = "R100\tscripts/old.py\tdocs/old.md\n"
        paths = ci.paths_from_name_status(text)
        self.assertEqual(paths, ["scripts/old.py", "docs/old.md"])
        self.assertTrue(ci.pytest_required(paths))

    def test_rename_between_markdown_files_skips_pytest(self):
        text = "R095\tREADME.md\tdocs/README.md\n"
        paths = ci.paths_from_name_status(text)
        self.assertFalse(ci.pytest_required(paths))

    def test_deleted_python_runs_pytest(self):
        paths = ci.paths_from_name_status("D\tscripts/old.py\n")
        self.assertTrue(ci.pytest_required(paths))

    def test_unparsed_name_status_fails_closed(self):
        self.assertTrue(ci.pytest_required(ci.paths_from_name_status("not-a-diff\n")))

    def test_github_rename_payload_includes_previous_filename(self):
        payload = [
            {
                "filename": "docs/guide.md",
                "status": "renamed",
                "previous_filename": "aria_bot.py",
            }
        ]
        paths = ci.paths_from_file_objects(payload)
        self.assertTrue(ci.pytest_required(paths))

    def test_github_slurp_pages_of_markdown_skip_pytest(self):
        payload = [
            [{"filename": "README.md", "status": "modified"}],
            [{"filename": "docs/guide.md", "status": "added"}],
        ]
        paths = ci.paths_from_file_objects(ci.flatten_file_pages(payload))
        self.assertEqual(paths, ["README.md", "docs/guide.md"])
        self.assertFalse(ci.pytest_required(paths))

    def test_github_error_object_is_rejected(self):
        with self.assertRaises(ValueError):
            ci.flatten_file_pages({"message": "Not Found"})

    def test_missing_filename_is_rejected(self):
        with self.assertRaises(ValueError):
            ci.paths_from_file_objects([{"status": "added"}])


def _listing(paths, *, fetched=None, changed=None, sha="abc123"):
    count = len(paths) if fetched is None else fetched
    changed_files = count if changed is None else changed
    return ci.PullFileList(list(paths), count, changed_files, sha, False)


class TestPullDecisionScenarios(unittest.TestCase):
    """Cases the re-review listed, through pull_requires_pytest."""

    def test_docs_with_code_env_and_mixed_diffs(self):
        cases = [
            (["docs/x.py"], True),
            (["docs/sub/x.py"], True),
            (["docs/prod.env"], True),
            (["docs/.env"], True),
            (["docs/prod.ENV"], True),
            (["docs/foo.md", "docs/x.py"], True),
            (["docs/foo.md", "README.md"], False),
        ]
        for paths, runs in cases:
            with self.subTest(paths=paths):
                run, reason = ci.pull_requires_pytest(_listing(paths), "abc123")
                self.assertEqual(run, runs, reason)

    def test_deletions(self):
        cases = [
            ("D\tdocs/prod.env\n", True),
            ("D\tdocs/x.py\n", True),
            ("D\tdocs/foo.md\n", False),
        ]
        for line, runs in cases:
            with self.subTest(line=line.strip()):
                paths = ci.paths_from_name_status(line)
                run, reason = ci.pull_requires_pytest(_listing(paths), "abc123")
                self.assertEqual(run, runs, reason)

    def test_renames_both_directions(self):
        both_ways = [
            ("R100\tdocs/old.py\tdocs/new.md\n", True),
            ("R100\tdocs/old.md\tdocs/new.py\n", True),
            ("R100\tREADME.md\tdocs/guide.md\n", False),
        ]
        for line, runs in both_ways:
            with self.subTest(line=line.strip()):
                paths = ci.paths_from_name_status(line)
                listing = _listing(paths, fetched=1, changed=1)
                run, reason = ci.pull_requires_pytest(listing, "abc123")
                self.assertEqual(run, runs, reason)
        code_to_md = ci.paths_from_file_objects(
            [
                {
                    "filename": "docs/new.md",
                    "status": "renamed",
                    "previous_filename": "docs/old.py",
                }
            ]
        )
        run, _reason = ci.pull_requires_pytest(
            _listing(code_to_md, fetched=1, changed=1), "abc123"
        )
        self.assertTrue(run)
        md_to_md = ci.paths_from_file_objects(
            [
                {
                    "filename": "docs/guide.md",
                    "status": "renamed",
                    "previous_filename": "README.md",
                }
            ]
        )
        run, reason = ci.pull_requires_pytest(
            _listing(md_to_md, fetched=1, changed=1), "abc123"
        )
        self.assertFalse(run, reason)

    def test_doc_read_by_tests_on_delete_rename_and_subdir(self):
        root = ROOT / "tests"
        path = "docs/universe_membership_631.md"
        run, reason = ci.pull_requires_pytest(_listing([path]), "abc123", tests_root=root)
        self.assertTrue(run)
        self.assertIn(path, reason)
        sub = "docs/sub/universe_membership_631.md"
        run, reason = ci.pull_requires_pytest(_listing([sub]), "abc123", tests_root=root)
        self.assertTrue(run)
        self.assertIn(sub, reason)
        renamed = _listing([path, "docs/renamed.md"], fetched=1, changed=1)
        run, reason = ci.pull_requires_pytest(renamed, "abc123", tests_root=root)
        self.assertTrue(run)
        self.assertIn(path, reason)


class TestPullFileList(unittest.TestCase):
    def test_pagination_includes_removed_markdown(self):
        def pages(page):
            if page == 1:
                return (
                    [{"filename": "docs/keep.md", "status": "modified"}],
                    '<https://example.test?page=2>; rel="next"',
                )
            if page == 2:
                return ([{"filename": "docs/old.md", "status": "removed"}], "")
            raise AssertionError(page)

        listing = ci.fetch_pull_request_paths(
            "acme/repo",
            9,
            "token",
            opener=_opener({"head": {"sha": "abc123"}, "changed_files": 2}, pages),
        )
        self.assertEqual(listing.paths, ["docs/keep.md", "docs/old.md"])
        self.assertEqual(listing.fetched_count, 2)
        self.assertFalse(listing.truncated)
        run, reason = ci.pull_requires_pytest(listing, "abc123", tests_root=None)
        self.assertFalse(run, reason)

    def test_removed_code_file_runs_pytest(self):
        def pages(page):
            self.assertEqual(page, 1)
            return (
                [{"filename": "scripts/ci_docs_only.py", "status": "removed"}],
                "",
            )

        listing = ci.fetch_pull_request_paths(
            "acme/repo",
            9,
            "token",
            opener=_opener({"head": {"sha": "abc123"}, "changed_files": 1}, pages),
        )
        self.assertEqual(listing.paths, ["scripts/ci_docs_only.py"])
        run, _reason = ci.pull_requires_pytest(listing, "abc123")
        self.assertTrue(run)

    def test_changed_files_mismatch_runs_pytest(self):
        def pages(page):
            return ([{"filename": "docs/foo.md", "status": "added"}], "")

        listing = ci.fetch_pull_request_paths(
            "acme/repo",
            9,
            "token",
            opener=_opener({"head": {"sha": "abc123"}, "changed_files": 4}, pages),
        )
        self.assertEqual(listing.fetched_count, 1)
        self.assertEqual(listing.changed_files, 4)
        run, reason = ci.pull_requires_pytest(listing, "abc123")
        self.assertTrue(run)
        self.assertIn("changed_files", reason)

    def test_head_sha_mismatch_runs_pytest(self):
        def pages(page):
            return ([{"filename": "README.md", "status": "modified"}], "")

        listing = ci.fetch_pull_request_paths(
            "acme/repo",
            9,
            "token",
            opener=_opener({"head": {"sha": "from-api"}, "changed_files": 1}, pages),
        )
        run, reason = ci.pull_requires_pytest(listing, "from-event")
        self.assertTrue(run)
        self.assertIn("head sha", reason)

    def test_truncation_at_3000_runs_pytest(self):
        def pages(page):
            files = [
                {"filename": f"docs/page-{page}-{i}.md", "status": "added"}
                for i in range(100)
            ]
            link = 'rel="next"' if page < 30 else ""
            return files, link

        listing = ci.fetch_pull_request_paths(
            "acme/repo",
            9,
            "token",
            opener=_opener({"head": {"sha": "abc123"}, "changed_files": 3000}, pages),
        )
        self.assertEqual(listing.fetched_count, 3000)
        self.assertTrue(listing.truncated)
        run, reason = ci.pull_requires_pytest(listing, "abc123")
        self.assertTrue(run)
        self.assertIn("truncated", reason)

    def test_http_error_raises(self):
        def opener(request, timeout=30):
            raise urllib.error.HTTPError(
                request.full_url,
                503,
                "unavailable",
                Message(),
                io.BytesIO(b"down"),
            )

        with self.assertRaises(SystemExit) as ctx:
            ci.fetch_pull_request_paths("acme/repo", 3, "token", opener=opener)
        self.assertIn("HTTP 503", str(ctx.exception))

    def test_url_error_raises(self):
        def opener(request, timeout=30):
            raise urllib.error.URLError("network down")

        with self.assertRaises(SystemExit) as ctx:
            ci.fetch_pull_request_paths("acme/repo", 3, "token", opener=opener)
        self.assertIn("network down", str(ctx.exception))

    def test_missing_token_raises_before_network(self):
        def opener(request, timeout=30):
            raise AssertionError("network used")

        with self.assertRaises(SystemExit):
            ci.fetch_pull_request_paths("acme/repo", 3, "", opener=opener)

    def test_main_pull_without_token_exits(self):
        with patch.dict(
            os.environ,
            {"GITHUB_TOKEN": "", "GITHUB_REPOSITORY": "acme/repo"},
            clear=False,
        ):
            with self.assertRaises(SystemExit):
                ci.main(["--pull", "4", "--head-sha", "abc"])


class TestRequiredCheck(unittest.TestCase):
    def test_code_change_needs_a_green_pytest_job(self):
        self.assertTrue(ci.required_check_should_pass("success", "success", "true"))
        self.assertFalse(ci.required_check_should_pass("success", "failure", "true"))
        self.assertFalse(ci.required_check_should_pass("success", "skipped", "true"))

    def test_docs_only_accepts_a_skipped_pytest_job(self):
        self.assertTrue(ci.required_check_should_pass("success", "skipped", "false"))
        self.assertFalse(ci.required_check_should_pass("success", "success", "false"))
        self.assertFalse(ci.required_check_should_pass("success", "cancelled", "false"))

    def test_classify_failure_cancelled_or_skipped_is_run_or_fail(self):
        for classify in ("failure", "cancelled", "skipped"):
            with self.subTest(classify=classify):
                self.assertTrue(
                    ci.required_check_should_pass(classify, "success", "true")
                )
                self.assertTrue(ci.required_check_should_pass(classify, "success", ""))
                self.assertFalse(
                    ci.required_check_should_pass(classify, "skipped", "false")
                )
                self.assertFalse(
                    ci.required_check_should_pass(classify, "failure", "true")
                )
                self.assertFalse(
                    ci.required_check_should_pass(classify, "cancelled", "true")
                )

    def test_unknown_flag_does_not_count_as_a_docs_skip(self):
        self.assertFalse(ci.required_check_should_pass("success", "skipped", ""))
        self.assertFalse(ci.required_check_should_pass("success", "success", "maybe"))


class TestWorkflowWiring(unittest.TestCase):
    def test_unit_workflow_gates_pytest_and_always_reports_unit(self):
        text = (ROOT / ".github" / "workflows" / "unit-tests.yml").read_text(
            encoding="utf-8"
        )
        folded = " ".join(text.replace("\\", " ").split())
        self.assertNotIn("paths-ignore", text)
        self.assertNotIn("paths:", text)
        self.assertIn(
            "types: [opened, synchronize, reopened, edited]",
            text,
        )
        self.assertIn(
            "!cancelled() && needs.classify.outputs.run_pytest != 'false'",
            folded,
        )
        self.assertNotIn("run_pytest == 'true'", text)
        self.assertIn("github.event.pull_request.base.sha", text)
        self.assertGreaterEqual(text.count("git show"), 2)
        self.assertGreaterEqual(text.count("python3 /tmp/ci_docs_only.py"), 2)
        self.assertNotIn("python3 scripts/ci_docs_only.py", text)
        self.assertIn('echo "run_pytest=true"', text)
        self.assertIn("--pull", folded)
        self.assertIn("--head-sha", folded)
        self.assertIn("--tests-root", folded)
        self.assertIn("--required-check", folded)
        self.assertIn("\n  pytest:\n", text)
        self.assertIn("base classifier missing or unreadable", text)
        unit = text.split("\n  unit:\n", 1)[1]
        header, _steps = unit.split("steps:", 1)
        # Review gates, hypothesis JSON, and changed-file Ruff are part of
        # the required unit result. They are not behind the pytest skip.
        self.assertIn(
            "needs: [classify, pytest, review-gates, hypothesis-evidence, ruff]",
            header,
        )
        self.assertIn("\n  review-gates:\n", text)
        self.assertIn("\n  hypothesis-evidence:\n", text)
        self.assertIn("\n  ruff:\n", text)
        # The docs-only pytest skip stays on the pytest job only.
        self.assertEqual(
            text.count(
                "!cancelled() && needs.classify.outputs.run_pytest != 'false'"
            ),
            1,
        )
        self.assertIn("GATES:", unit)
        self.assertIn("HYPOTHESIS:", unit)
        self.assertIn("RUFF:", unit)
        self.assertIn("if: ${{ always() }}", header)
        self.assertNotIn("!cancelled()", unit)
        self.assertNotIn("continue-on-error", unit)
        self.assertIn("if: always()", unit)
        self.assertIn("CLASSIFY:", unit)
        self.assertIn("PYTEST:", unit)
        self.assertIn("RUN_PYTEST:", unit)
        self.assertIn("--classify-result", unit)
        self.assertIn("--pytest-result", unit)
        self.assertIn("--run-pytest", unit)


class TestCli(unittest.TestCase):
    def test_required_check_exit_codes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                ci.main(
                    [
                        "--required-check",
                        "--classify-result",
                        "success",
                        "--pytest-result",
                        "skipped",
                        "--run-pytest",
                        "false",
                    ]
                ),
                0,
            )
            self.assertEqual(
                ci.main(
                    [
                        "--required-check",
                        "--classify-result",
                        "failure",
                        "--pytest-result",
                        "success",
                        "--run-pytest",
                        "",
                    ]
                ),
                0,
            )
            self.assertEqual(
                ci.main(
                    [
                        "--required-check",
                        "--classify-result",
                        "failure",
                        "--pytest-result",
                        "skipped",
                        "--run-pytest",
                        "false",
                    ]
                ),
                1,
            )

    def test_github_files_json_and_output(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "files.json"
            output = Path(tmp) / "out.txt"
            payload.write_text(
                json.dumps([{"filename": "docs/foo.md", "status": "added"}]),
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                code = ci.main(
                    ["--github-files", str(payload), "--github-output", str(output)]
                )
            self.assertEqual(code, 0)
            self.assertEqual(output.read_text(encoding="utf-8"), "run_pytest=false\n")


if __name__ == "__main__":
    unittest.main()
