import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hermes"


@pytest.fixture(autouse=True)
def demo_mode_env(monkeypatch):
    """Unit tests run with isolated demo JSON paths when touching data files."""
    monkeypatch.setenv("DEMO_MODE", "1")


@pytest.fixture(autouse=True)
def telegram_credentials(monkeypatch):
    """Keep Telegram send paths testable after other tests clear env vars."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")


@pytest.fixture(autouse=True)
def isolate_bot_logs(tmp_path, monkeypatch):
    """Keep test runs from appending to logs/aria_log.txt while the bot is live."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr("logger.LOG_DIR", str(log_dir))
    monkeypatch.setattr("logger.LOG_FILE", str(log_dir / "aria_log.txt"))
    monkeypatch.setattr("logger.JSON_LOG_FILE", str(log_dir / "aria_log.jsonl"))
    monkeypatch.setattr("logger.DECISIONS_LOG_FILE", str(log_dir / "decisions.jsonl"))


@pytest.fixture
def hermes_memory_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    from hermes.memory import store

    monkeypatch.setattr(store, "MEMORY_DIR", tmp_path)
    yield tmp_path


@pytest.fixture
def sample_live_trade_history():
    with open(FIXTURES / "live_trade_history.sample.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def sample_positions_live():
    with open(FIXTURES / "positions.live.sample.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def sample_orders_live():
    with open(FIXTURES / "orders.live.sample.json", encoding="utf-8") as f:
        return json.load(f)