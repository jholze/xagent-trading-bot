import json
from pathlib import Path

import pytest

from scripts.daily_auswertung import generate_report


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "hermes"


@pytest.fixture
def bot_dir(tmp_path):
    for name in (
        "live_trade_history.json",
        "orders.live.json",
        "positions.live.json",
        "config.json",
        "cmc_posts.json",
    ):
        src = Path(__file__).resolve().parents[2] / name
        if src.exists():
            (tmp_path / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "live_trade_history.json").write_text(
        (FIXTURES / "live_trade_history.sample.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "orders.live.json").write_text(
        (FIXTURES / "orders.live.sample.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "positions.live.json").write_text(
        (FIXTURES / "positions.live.sample.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    hermes_dir = tmp_path / "hermes" / "memory"
    hermes_dir.mkdir(parents=True)
    (hermes_dir / "experiments.json").write_text(
        json.dumps({"experiments": [{"verdict": "rejected", "symbol": "H/USDT", "verdict_reason": "0/4"}]}),
        encoding="utf-8",
    )
    (hermes_dir / "skills.json").write_text(json.dumps({"skills": []}), encoding="utf-8")
    (hermes_dir / "baseline.json").write_text(
        json.dumps({"version": 2, "profiles": {}, "active_pool": {"symbols": ["ARIA/USDT", "STG/USDT"]}}),
        encoding="utf-8",
    )
    return tmp_path


def test_generate_report_contains_hermes_section(bot_dir):
    from datetime import datetime

    report = generate_report(bot_dir, datetime(2026, 6, 14, 12, 0, 0))
    assert "# Tages-Auswertung Trading Bot" in report
    assert "## Hermes" in report
    assert "Experimente gesamt" in report
    assert "Promoted" in report