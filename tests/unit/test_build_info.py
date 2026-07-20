import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.build_info import get_build_info


def _load_write_build_meta():
    path = ROOT / "scripts" / "write_build_meta.py"
    spec = importlib.util.spec_from_file_location("write_build_meta", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


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
        meta_path = ROOT / "core" / "build_meta.json"
        backup = meta_path.read_text(encoding="utf-8") if meta_path.exists() else None
        try:
            meta_path.write_text(
                '{"commit": "deadbeef", "branch": "feature/test"}\n',
                encoding="utf-8",
            )
            env = {
                "GIT_COMMIT": "",
                "GIT_BRANCH": "",
                "RAILWAY_GIT_COMMIT_SHA": "",
                "RAILWAY_GIT_BRANCH": "",
            }
            with patch.dict(os.environ, env, clear=False), patch(
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

    def test_write_build_meta_does_not_clobber_with_unknown(self):
        wbm = _load_write_build_meta()
        out = wbm.OUT
        backup = out.read_text(encoding="utf-8") if out.exists() else None
        try:
            out.write_text(
                '{"commit": "1b466dc", "branch": "staging"}\n', encoding="utf-8"
            )
            env = {
                "RAILWAY_GIT_COMMIT_SHA": "",
                "RAILWAY_GIT_BRANCH": "",
                "GIT_COMMIT": "",
                "GIT_BRANCH": "",
            }
            with patch.dict(os.environ, env, clear=False), patch.object(
                wbm, "_git", return_value=""
            ):
                self.assertEqual(wbm.main(), 0)
            data = out.read_text(encoding="utf-8")
            self.assertIn("1b466dc", data)
            self.assertIn("staging", data)
        finally:
            if backup is None:
                out.unlink(missing_ok=True)
            else:
                out.write_text(backup, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
