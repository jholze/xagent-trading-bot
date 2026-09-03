from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import data_manager


class TestDataFilePaths(unittest.TestCase):
    def test_basename_resolves_under_data_dir(self):
        with patch.object(data_manager, "is_demo_mode", return_value=False):
            path = data_manager.get_data_file("watchlist.json")
        self.assertTrue(path.replace("\\", "/").endswith("data/watchlist.json"))
        self.assertFalse(os.path.isabs(path) and os.path.dirname(path) == os.getcwd())

    def test_absolute_path_unchanged(self):
        abs_path = os.path.join(tempfile.gettempdir(), "orders.demo.json")
        with patch.object(data_manager, "is_demo_mode", return_value=False):
            self.assertEqual(data_manager.get_data_file(abs_path), abs_path)

    def test_demo_suffix_stays_under_data(self):
        with patch.object(data_manager, "is_demo_mode", return_value=True):
            path = data_manager.get_data_file("watchlist.json")
        self.assertTrue(path.endswith("watchlist.demo.json"))
        self.assertIn(os.path.join("data", "watchlist.demo.json"), path.replace("\\", "/"))

    def test_legacy_root_fallback_when_data_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            legacy = root / "lc_signals.json"
            legacy.write_text("{}", encoding="utf-8")
            with patch.object(data_manager, "_ROOT_DIR", str(root)), patch.object(
                data_manager, "_DATA_DIR", str(data)
            ), patch.object(data_manager, "is_demo_mode", return_value=False):
                path = data_manager.resolve_data_path("lc_signals.json")
            self.assertEqual(path, str(legacy))

    def test_new_writes_prefer_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            with patch.object(data_manager, "_ROOT_DIR", str(root)), patch.object(
                data_manager, "_DATA_DIR", str(data)
            ):
                path = data_manager.resolve_data_path("brand_new.json")
            self.assertEqual(path, str(data / "brand_new.json"))


if __name__ == "__main__":
    unittest.main()
