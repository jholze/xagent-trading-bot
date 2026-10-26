"""Allowlist: pytest runs unless every changed path is documentation.

The cases here are the ones listed in the pull request that added this
check. A path is documentation only when it matches the allowlist in
``scripts/ci_docs_only.py``. Everything else, including an empty diff,
runs pytest.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path

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
    (["docs/requirements-engineering-guide.md"], False),
    (["docs/notes.txt"], False),
    (["docs/example.py"], False),
    (["docs/diagram.png"], False),
    (["plans/idea.md", "DOCUMENTATION.en.md"], False),
    (["docs/guide.md", "docs/sample.json"], True),
    (["docs/settings.yaml"], True),
    (["docs/pyproject.toml"], True),
    (["docs/bundle.spec"], True),
    (["docs/agent.bundle"], True),
    (["docs/requirements.txt"], True),
    (["docs/fixtures/sample.md"], True),
    (["tests/README.md"], True),
    (["tests/unit/test_ci_docs_only.py"], True),
    (["tests/fixtures/macro/btc_bars.json"], True),
    ([".github/workflows/unit-tests.yml"], True),
    (["README.md", "scripts/ci_docs_only.py"], True),
    (["config.json"], True),
    (["requirements.txt"], True),
    (["railway.toml"], True),
    ([], True),
    ([""], True),
    (["docs/../scripts/ci_docs_only.py"], True),
    (["docs/tests/notes.md"], True),
    (["Docs/notes.txt"], True),
    ([".github/workflows/README.md"], True),
]


class TestDocsAllowlist(unittest.TestCase):
    def test_table_cases(self):
        for paths, runs in _CASES:
            with self.subTest(paths=paths):
                self.assertEqual(ci.pytest_required(paths), runs)

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


class TestRequiredCheck(unittest.TestCase):
    def test_code_change_needs_a_green_pytest_job(self):
        self.assertTrue(ci.required_check_should_pass("success", "success", "true"))
        self.assertFalse(ci.required_check_should_pass("success", "failure", "true"))
        self.assertFalse(ci.required_check_should_pass("success", "skipped", "true"))

    def test_docs_only_accepts_a_skipped_pytest_job(self):
        self.assertTrue(ci.required_check_should_pass("success", "skipped", "false"))
        self.assertFalse(ci.required_check_should_pass("success", "success", "false"))
        self.assertFalse(ci.required_check_should_pass("success", "cancelled", "false"))

    def test_classify_failure_or_unknown_flag_fails_closed(self):
        self.assertFalse(ci.required_check_should_pass("failure", "skipped", "false"))
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
        self.assertIn("needs.classify.outputs.run_pytest == 'true'", folded)
        self.assertIn("!cancelled()", text)
        self.assertIn("scripts/ci_docs_only.py --pull", folded)
        self.assertIn("scripts/ci_docs_only.py --required-check", folded)
        self.assertIn("\n  unit:\n", text)
        self.assertIn("\n  pytest:\n", text)


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
                        "success",
                        "--pytest-result",
                        "failure",
                        "--run-pytest",
                        "true",
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
                json.dumps([{"filename": "docs/notes.txt", "status": "added"}]),
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
