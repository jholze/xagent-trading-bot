import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture(autouse=True)
def demo_mode_env(monkeypatch):
    """Unit tests run with isolated demo JSON paths when touching data files."""
    monkeypatch.setenv("DEMO_MODE", "1")


@pytest.fixture(autouse=True)
def isolate_bot_logs(tmp_path, monkeypatch):
    """Keep test runs from appending to logs/aria_log.txt while the bot is live."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr("logger.LOG_DIR", str(log_dir))
    monkeypatch.setattr("logger.LOG_FILE", str(log_dir / "aria_log.txt"))
    monkeypatch.setattr("logger.JSON_LOG_FILE", str(log_dir / "aria_log.jsonl"))
    monkeypatch.setattr("logger.DECISIONS_LOG_FILE", str(log_dir / "decisions.jsonl"))