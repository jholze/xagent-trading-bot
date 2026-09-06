import json
from pathlib import Path

import pytest

from scripts.daily_auswertung import build_telegram_daily_summary, generate_report


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
        root = Path(__file__).resolve().parents[2]
        src = root / "data" / name
        if not src.exists():
            src = root / name
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


def test_build_telegram_daily_summary(bot_dir):
    from datetime import datetime

    summary = build_telegram_daily_summary(bot_dir, datetime(2026, 6, 14, 12, 0, 0))
    assert "Tages-Auswertung 2026-06-14" in summary
    assert "Portfolio" in summary
    assert "DCA" in summary


def test_generate_report_reads_isolated_ledger_not_legacy_files(bot_dir):
    """#307: trades/orders come from data_manager, not live_trade_history.json."""
    from datetime import datetime

    from data_manager import (
        resolve_ledger_scope,
        save_orders,
        save_trade_history_document,
    )

    ts = "2026-06-14T10:15:00"
    scope = resolve_ledger_scope()
    save_trade_history_document(
        {
            "trades": [
                {
                    "type": "SELL",
                    "symbol": "AAA/USDT",
                    "pnl": 12.5,
                    "source": "grid",
                    "timestamp": ts,
                    "usdt_amount": 100,
                    "usdt_received": 100,
                }
            ],
            "virtual_balance": 4242,
            "realized_pnl": 12.5,
        },
        scope,
    )
    save_orders(
        {
            "ledger_scope": scope,
            "orders": [
                {
                    "symbol": "AAA/USDT",
                    "status": "filled",
                    "timestamps": {"created": ts},
                }
            ],
        },
        scope,
    )
    (bot_dir / "live_trade_history.json").write_text(
        json.dumps(
            {
                "trades": [
                    {
                        "type": "SELL",
                        "symbol": "LEGACY/USDT",
                        "pnl": 999,
                        "source": "x",
                        "timestamp": ts,
                        "usdt_amount": 1,
                    }
                ],
                "virtual_balance": 1,
                "realized_pnl": 999,
            }
        ),
        encoding="utf-8",
    )
    (bot_dir / "orders.live.json").write_text(
        json.dumps(
            {
                "orders": [
                    {
                        "symbol": "LEGACY/USDT",
                        "status": "filled",
                        "timestamps": {"created": ts},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = generate_report(bot_dir, datetime(2026, 6, 14, 12, 0, 0))
    summary = build_telegram_daily_summary(bot_dir, datetime(2026, 6, 14, 12, 0, 0))
    assert "AAA/USDT" in report
    assert "LEGACY/USDT" not in report
    assert "grid" in report
    assert "AAA/USDT" in summary
    assert "LEGACY/USDT" not in summary