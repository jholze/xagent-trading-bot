import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.build_info import get_build_info


class TestBuildInfo(unittest.TestCase):
    def test_prefers_git_commit_env_without_railway_git(self):
        with patch.dict(
            os.environ,
            {
                "GIT_COMMIT": "e44de14",
                "GIT_BRANCH": "staging",
                "RAILWAY_DEPLOY": "1",
            },
            clear=False,
        ):
            info = get_build_info()
        self.assertEqual(info["commit"], "e44de14")
        self.assertEqual(info["branch"], "staging")
        self.assertFalse(info["dirty"])

    def test_railway_git_overrides_stale_git_commit_env(self):
        with patch.dict(
            os.environ,
            {
                "GIT_COMMIT": "27ea307",
                "GIT_BRANCH": "staging",
                "RAILWAY_GIT_COMMIT_SHA": "6021428abc123def456789012345678901234",
                "RAILWAY_GIT_BRANCH": "staging",
                "RAILWAY_DEPLOY": "1",
            },
            clear=False,
        ):
            info = get_build_info()
        self.assertEqual(info["commit"], "6021428")
        self.assertEqual(info["branch"], "staging")

    def test_railway_git_sha_shortened(self):
        with patch.dict(
            os.environ,
            {
                "GIT_COMMIT": "",
                "RAILWAY_GIT_COMMIT_SHA": "d0beb8f5c55b36df7d674d55965a23b8d54ad69b",
                "RAILWAY_GIT_BRANCH": "main",
                "RAILWAY_DEPLOY": "1",
            },
            clear=False,
        ):
            info = get_build_info()
        self.assertEqual(info["commit"], "d0beb8f")
        self.assertEqual(info["branch"], "main")

    def test_reads_baked_meta_without_env_or_git(self):
        meta_path = Path(__file__).resolve().parents[2] / "core" / "build_meta.json"
        backup = meta_path.read_text(encoding="utf-8") if meta_path.exists() else None
        try:
            meta_path.write_text(
                '{"commit": "deadbeef", "branch": "feature/test"}\n',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"GIT_COMMIT": "", "GIT_BRANCH": ""}, clear=False), patch(
                "core.build_info._git", return_value=""
            ):
                info = get_build_info()
            self.assertEqual(info["commit"], "deadbeef")
            self.assertEqual(info["branch"], "feature/test")
        finally:
            if backup is None:
                meta_path.unlink(missing_ok=True)
            else:
                meta_path.write_text(backup, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()