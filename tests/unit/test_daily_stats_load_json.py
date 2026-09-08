"""load_json: required files raise, optional files (explicit default) warn and fall back."""

import pytest

from notifications.daily_stats import load_json


def test_load_json_missing_file_without_default_raises(tmp_path):
    missing = tmp_path / "config.json"
    assert missing.exists() is False
    with pytest.raises(FileNotFoundError):
        load_json(missing)


def test_load_json_missing_file_uses_explicit_default_and_warns(tmp_path, monkeypatch):
    missing = tmp_path / "cmc_posts.json"
    logged = []
    monkeypatch.setattr(
        "notifications.daily_stats.log",
        lambda message, level="INFO": logged.append((str(message), str(level))),
    )
    assert load_json(missing, default=[]) == []
    assert any(level == "WARNING" for _, level in logged)


def test_load_json_explicit_empty_dict_default_is_returned_as_is(tmp_path, monkeypatch):
    missing = tmp_path / "optional.json"
    monkeypatch.setattr("notifications.daily_stats.log", lambda *a, **k: None)
    result = load_json(missing, default={})
    assert result == {} and isinstance(result, dict)


def test_load_json_reads_existing_file(tmp_path):
    path = tmp_path / "present.json"
    path.write_text('{"posts": [1]}', encoding="utf-8")
    assert load_json(path) == {"posts": [1]}
