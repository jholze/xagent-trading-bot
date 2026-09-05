"""#327: unit tests must not resolve or write data files in the checkout."""

from __future__ import annotations

from pathlib import Path

import data_manager


def test_resolve_data_path_stays_in_tmp(isolate_data_dir):
    test_data = isolate_data_dir["test_data"]
    orig_data = Path(isolate_data_dir["orig_data"]).resolve()
    orig_root = Path(isolate_data_dir["orig_root"]).resolve()

    path = Path(data_manager.resolve_data_path("positions.json"))
    assert path == test_data / "positions.json"
    assert orig_data not in path.resolve().parents
    assert path.resolve() != orig_root / "positions.json"

    missing = Path(data_manager.resolve_data_path("brand_new_isolation.json"))
    assert missing == test_data / "brand_new_isolation.json"
    assert data_manager._ROOT_DIR == str(isolate_data_dir["test_root"])
    assert data_manager._DATA_DIR == str(test_data)


def test_legacy_root_fallback_is_tmp_not_checkout(isolate_data_dir, tmp_path):
    """resolve_data_path must not fall back to the real repo root in tests."""
    leftover = Path(isolate_data_dir["orig_root"]) / "lc_signals.json"
    # Even if a legacy file existed at repo root, tests must not pick it up.
    path = Path(data_manager.resolve_data_path("lc_signals.json"))
    assert path.parent == isolate_data_dir["test_data"]
    assert path.resolve() != leftover.resolve()
