"""B-1 preview: a lots export names what the grace deploy would sell.

Symbols in the fixture are synthetic. Stops live on each lot's
strategy_params. Risk floors are the loaded config, not literals.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from core.config import get_bot_config
from scripts.preview_dca_partial_band import classify_lots, load_export
from strategies.position_lock import position_locks_enabled
from strategies.positions import sell_fraction_for_signal

_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "dca_grace_deploy_lots.json"
)


def _row(report: dict, symbol: str) -> dict:
    matches = [row for row in report["lots"] if row["symbol"] == symbol]
    assert len(matches) == 1
    return matches[0]


def test_fixture_lists_partial_band_after_deploy():
    lots, as_of, prices = load_export(_FIXTURE)
    report = classify_lots(lots, as_of=as_of, prices=prices)
    assert report["read_only"] is True
    fraction = sell_fraction_for_signal("SELL_STOP_PARTIAL")
    floors_risk = get_bot_config().risk_config

    outside = _row(report, "SYNA/USDT")
    assert outside["in_partial_band"] is True
    assert outside["in_grace"] is False
    assert outside["unlocked"] is True
    assert outside["signal"] == "SELL_STOP_PARTIAL"
    assert outside["fraction"] == fraction
    assert outside["before_deploy_signal"] == "HOLD"
    position = outside["position_usdt"]
    notional = outside["sell_notional_usdt"]
    min_position = float(floors_risk["min_position_usdt_for_partial_sell"])
    min_notional = float(floors_risk["min_sell_notional_usdt"])
    if position >= min_position and notional >= min_notional:
        assert outside["disposition"] == "partial_would_fill"
    assert any(row["symbol"] == "SYNA/USDT" for row in report["partial_band_after_deploy"])

    deep = _row(report, "SYNB/USDT")
    assert deep["in_grace"] is True
    assert deep["signal"] == "SELL_STOP_FULL"
    assert deep["disposition"] == "full_would_fill"
    assert deep["in_partial_band"] is False

    paused = _row(report, "SYNC/USDT")
    assert paused["in_grace"] is True
    assert paused["in_partial_band"] is True
    assert paused["signal"] == "HOLD"
    assert paused["disposition"] == "partial_paused_grace"

    locked = _row(report, "SYND/USDT")
    if position_locks_enabled(get_bot_config().raw):
        assert locked["unlocked"] is False
        assert locked["signal"] == "SELL_STOP_PARTIAL"
        assert locked["disposition"] == "locked"
        assert locked["symbol"] not in {
            row["symbol"] for row in report["partial_band_after_deploy"]
        }

    tiny = _row(report, "SYNE/USDT")
    assert tiny["signal"] == "SELL_STOP_PARTIAL"
    if tiny["position_usdt"] < min_position:
        assert tiny["disposition"] == "partial_rejected_position_floor"
    elif tiny["sell_notional_usdt"] < min_notional:
        assert tiny["disposition"] == "partial_rejected_notional_floor"

    missing = _row(report, "SYNF/USDT")
    assert missing["disposition"] == "price_missing"

    quiet = _row(report, "SYNG/USDT")
    assert quiet["in_partial_band"] is False
    assert quiet["signal"] == "HOLD"
    assert quiet["disposition"] == "hold"

    summary = report["summary"]
    assert summary["lots"] == len(lots)
    assert summary["would_partial_fill"] == sum(
        1 for row in report["lots"] if row.get("disposition") == "partial_would_fill"
    )


def test_cli_is_read_only(tmp_path: Path):
    destination = tmp_path / "out"
    before = _FIXTURE.read_bytes()
    proc = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[2] / "scripts" / "preview_dca_partial_band.py"),
            str(_FIXTURE),
            "--format",
            "json",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(proc.stdout)
    assert report["summary"]["lots"] == 7
    assert report["summary"]["in_partial_band_unlocked_outside_grace"] >= 1
    assert _FIXTURE.read_bytes() == before
    written = [path.name for path in tmp_path.iterdir()]
    assert "orders.json" not in written
    assert not destination.exists()
