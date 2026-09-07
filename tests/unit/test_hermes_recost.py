"""End-to-end tests for scripts/hermes_recost.py (#316).

Stubbed OHLCV only — no Gate, no Hermes promotion, no writes to the input path.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import hermes_recost as recost


CREATED = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

ARIA_PROFILE = {
    "symbol": "ARIA/USDT",
    "timeframe": "4h",
    "params": {
        "rsi_buy_low": 25,
        "rsi_buy_high": 55,
        "volume_multiplier": 0.85,
        "rsi_sell_30": 72,
        "rsi_sell_20": 84,
        "take_profit_pct": 12,
        "stop_loss_pct": 50,
        "buy_regime": "both",
        "reversal_rsi_cross_low": 32,
        "reversal_rsi_cross_high": 38,
        "reversal_volume_multiplier": 1.3,
        "cmc_trust_score": 65.0,
        "cmc_min_confidence": 55.0,
    },
    "metrics": {},
}

ZERO_METRICS = {
    "win_rate": 0.0,
    "sharpe": 0.0,
    "max_drawdown_pct": 0.0,
    "trades": 0,
    "realized_pnl": 0.0,
    "equity": 0.0,
    "trade_quality": 0.0,
    "opportunity_score": 0.0,
    "buy_signals": 0,
}


def _exp(
    *,
    exp_id: str,
    symbol: str,
    variable: str,
    old_value,
    new_value,
    source: str,
    created_at: datetime = CREATED,
) -> dict:
    folds = [
        {
            "fold_id": i,
            "bars": 18,
            "win_rate": 0.0,
            "sharpe": 0.0,
            "max_drawdown_pct": 0.0,
            "trades": 0,
            "realized_pnl": 0.0,
            "equity": 0.0,
            "trade_quality": 0.0,
            "opportunity_score": 0.0,
            "buy_signals": 0,
        }
        for i in range(4)
    ]
    return {
        "id": exp_id,
        "symbol": symbol,
        "timeframe": "4h",
        "variable": variable,
        "old_value": old_value,
        "new_value": new_value,
        "hypothesis": f"{variable} {old_value}→{new_value}",
        "source": source,
        "created_at": created_at.isoformat(),
        "folds_total": 4,
        "folds_won": 0,
        "baseline_metrics": dict(ZERO_METRICS),
        "variant_metrics": dict(ZERO_METRICS),
        "baseline_fold_metrics": folds,
        "fold_metrics": folds,
        "verdict": "rejected",
        "verdict_reason": "Won 0/4 folds (0% < 55%)",
        "validation_mode": "walk_forward",
        "live_metrics": {"live_sell_pnl": 0.0, "live_trades": 0},
    }


def write_fixture(input_dir: Path) -> None:
    input_dir.mkdir(parents=True, exist_ok=True)
    live_exps = [
        _exp(
            exp_id="exp_live_rsi",
            symbol="ARIA/USDT",
            variable="rsi_buy_low",
            old_value=25.0,
            new_value=23,
            source="heuristic",
        ),
        _exp(
            exp_id="exp_live_regime",
            symbol="ARIA/USDT",
            variable="buy_regime",
            old_value="both",
            new_value="dip",
            source="grok",
        ),
    ]
    demo_exps = [
        _exp(
            exp_id="exp_demo_missing",
            symbol="MISSING/USDT",
            variable="volume_multiplier",
            old_value=0.85,
            new_value=1.0,
            source="grok",
        ),
    ]
    baseline = {
        "version": 2,
        "profiles": {"ARIA/USDT|4h": ARIA_PROFILE},
        "rotation_index": 0,
        "active_key": "ARIA/USDT|4h",
    }
    (input_dir / "experiments.json").write_text(
        json.dumps({"experiments": live_exps}, indent=2), encoding="utf-8"
    )
    (input_dir / "experiments.demo.json").write_text(
        json.dumps({"experiments": demo_exps}, indent=2), encoding="utf-8"
    )
    (input_dir / "baseline.json").write_text(json.dumps(baseline, indent=2), encoding="utf-8")
    (input_dir / "baseline.demo.json").write_text(
        json.dumps(baseline, indent=2), encoding="utf-8"
    )
    (input_dir / "skills.json").write_text(json.dumps({"skills": []}, indent=2), encoding="utf-8")
    (input_dir / "SNAPSHOT.txt").write_text("fixture snapshot for #316\n", encoding="utf-8")


def stub_bars(symbol: str, timeframe: str, start: datetime, end: datetime) -> list:
    if symbol == "MISSING/USDT":
        return []
    bar_ms = 4 * 3600 * 1000
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    bars = []
    ts = start_ms
    i = 0
    while ts <= end_ms:
        price = 100.0 + (i % 20) * 0.4
        bars.append([ts, price, price + 1.0, price - 1.0, price, 2000.0])
        ts += bar_ms
        i += 1
    return bars


def _fingerprint(path: Path) -> dict[str, str]:
    out = {}
    for p in sorted(path.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(path))] = p.read_bytes().hex()
    return out


REPORT_SECTIONS = (
    "## Gesamt",
    "## Flips rejected → promoted nach Variable",
    "## Flips nach Symbol",
    "## Flips nach Quelle (grok / heuristic)",
    "## Die 10 größten realized_pnl-Deltas",
    "## Was das für die Baseline bedeutet",
    "## Caveat: Sharpe und Win-Rate sind erstmals netto",
)


def test_recost_end_to_end_tags_legacy_and_leaves_input_untouched(tmp_path):
    input_dir = tmp_path / "snapshot"
    out_dir = tmp_path / "tagged"
    report = tmp_path / "hermes-recost.md"
    write_fixture(input_dir)
    before = _fingerprint(input_dir)

    summary = recost.run_recost(
        input_dir=input_dir,
        out_path=report,
        out_dir=out_dir,
        fetch_bars=stub_bars,
        progress=False,
    )

    after = _fingerprint(input_dir)
    assert after == before
    assert (input_dir / "experiments.json").read_text(encoding="utf-8").find(
        "cost_model"
    ) == -1

    tagged_live = json.loads((out_dir / "experiments.json").read_text(encoding="utf-8"))
    tagged_demo = json.loads((out_dir / "experiments.demo.json").read_text(encoding="utf-8"))
    assert len(tagged_live["experiments"]) == 2
    assert len(tagged_demo["experiments"]) == 1
    for exp in tagged_live["experiments"] + tagged_demo["experiments"]:
        assert exp["cost_model"] == "legacy"

    assert (out_dir / "SNAPSHOT.txt").is_file()
    assert (out_dir / "baseline.json").is_file()
    assert report.is_file()
    text = report.read_text(encoding="utf-8")
    for section in REPORT_SECTIONS:
        assert section in text, section
    assert "2026-09-v1" in text
    assert "legacy" in text

    assert summary.considered == 3
    by_id = {r.experiment_id: r for r in summary.rows}
    assert by_id["exp_demo_missing"].unresolvable is True
    assert by_id["exp_demo_missing"].new_verdict == "unresolvable"
    assert by_id["exp_live_rsi"].unresolvable is False
    assert by_id["exp_live_rsi"].old_verdict == "rejected"
    assert by_id["exp_live_rsi"].cost_model == "2026-09-v1"
    assert set(by_id["exp_live_rsi"].old_baseline) == set(recost.METRIC_KEYS)
    assert set(by_id["exp_live_rsi"].new_variant) == set(recost.METRIC_KEYS)


def test_cli_end_to_end(tmp_path, monkeypatch):
    input_dir = tmp_path / "snapshot"
    out_dir = tmp_path / "tagged"
    report = tmp_path / "out.md"
    write_fixture(input_dir)
    monkeypatch.setattr(recost, "fetch_ohlcv_bars", stub_bars)

    rc = recost.main(
        [
            "--input",
            str(input_dir),
            "--out",
            str(report),
            "--out-dir",
            str(out_dir),
            "--quiet",
        ]
    )
    assert rc == 0
    assert report.is_file()
    for section in REPORT_SECTIONS:
        assert section in report.read_text(encoding="utf-8")
    demo = json.loads((out_dir / "experiments.demo.json").read_text(encoding="utf-8"))
    assert demo["experiments"][0]["cost_model"] == "legacy"
    raw_input = (input_dir / "experiments.demo.json").read_text(encoding="utf-8")
    assert "cost_model" not in raw_input


def test_refuses_out_dir_inside_input(tmp_path):
    input_dir = tmp_path / "snapshot"
    write_fixture(input_dir)
    nested = input_dir / "oops"
    report = tmp_path / "out.md"
    with pytest.raises(SystemExit, match="inside the read-only input snapshot"):
        recost.run_recost(
            input_dir=input_dir,
            out_path=report,
            out_dir=nested,
            fetch_bars=stub_bars,
            progress=False,
        )
    assert not nested.exists()
    assert "cost_model" not in (input_dir / "experiments.json").read_text(encoding="utf-8")


def test_refuses_out_dir_inside_hermes_memory(tmp_path):
    input_dir = tmp_path / "snapshot"
    write_fixture(input_dir)
    mem = recost.HERMES_MEMORY_DIR
    before = {p.name for p in mem.iterdir()} if mem.is_dir() else set()
    with pytest.raises(SystemExit, match="inside hermes/memory"):
        recost.run_recost(
            input_dir=input_dir,
            out_path=tmp_path / "out.md",
            out_dir=mem,
            fetch_bars=stub_bars,
            progress=False,
        )
    after = {p.name for p in mem.iterdir()} if mem.is_dir() else set()
    assert after == before


def test_rebuild_params_overlays_old_and_new_value():
    baseline, variant = recost.rebuild_params(
        ARIA_PROFILE["params"], "rsi_buy_low", 25.0, 23
    )
    assert baseline["rsi_buy_low"] == 25.0
    assert variant["rsi_buy_low"] == 23
    assert baseline["buy_regime"] == "both"
    assert variant["volume_multiplier"] == 0.85


def test_window_matches_hermes_agent_created_at_minus_days():
    created = datetime(2026, 7, 8, 0, 12, 29, tzinfo=timezone.utc)
    start, end = recost.window_for_experiment(created, 14)
    assert end == created
    assert start == created - timedelta(days=14)


def test_stratify_round_robins_variables():
    exps = []
    for i in range(4):
        exps.append({"variable": "rsi_buy_low", "id": f"a{i}", "symbol": "ARIA/USDT"})
    for i in range(4):
        exps.append({"variable": "buy_regime", "id": f"b{i}", "symbol": "ETH/USDT"})
    picked, skipped = recost.stratify_experiments(exps, limit=4, symbols=None)
    vars_ = [e["variable"] for e in picked]
    assert vars_.count("rsi_buy_low") == 2
    assert vars_.count("buy_regime") == 2
    assert skipped == 4
