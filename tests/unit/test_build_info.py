import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.build_info import get_build_info


class TestBuildInfo(unittest.TestCase):
    def test_prefers_git_commit_env(self):
        with patch.dict(
            os.environ,
            {
                "GIT_COMMIT": "e44de14",
                "GIT_BRANCH": "feature/entry-guard-15m",
                "RAILWAY_DEPLOY": "1",
            },
            clear=False,
        ):
            info = get_build_info()
        self.assertEqual(info["commit"], "e44de14")
        self.assertEqual(info["branch"], "feature/entry-guard-15m")
        self.assertFalse(info["dirty"])

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


if __name__ == "__main__":
    unittest.main()