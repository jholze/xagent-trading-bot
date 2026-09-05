"""load_json must not crash on a missing optional file."""

from notifications.daily_stats import load_json


def test_load_json_missing_file_returns_empty_dict(tmp_path, monkeypatch):
    missing = tmp_path / "cmc_posts.json"
    logged = []

    def _log(message, level="INFO"):
        logged.append((str(message), str(level)))

    monkeypatch.setattr("notifications.daily_stats.log", _log)
    assert missing.exists() is False
    assert load_json(missing) == {}
    assert any(level == "WARNING" for _, level in logged)


def test_load_json_missing_file_uses_explicit_default(tmp_path, monkeypatch):
    missing = tmp_path / "cmc_posts.json"
    monkeypatch.setattr("notifications.daily_stats.log", lambda *a, **k: None)
    assert load_json(missing, default=[]) == []


def test_load_json_reads_existing_file(tmp_path):
    path = tmp_path / "present.json"
    path.write_text('{"posts": [1]}', encoding="utf-8")
    assert load_json(path) == {"posts": [1]}
