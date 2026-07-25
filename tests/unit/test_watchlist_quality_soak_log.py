"""R15 soak observability — cycle_summary, risk_reject, boot fingerprint."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import services.watchlist_quality.soak_log as soak_log
from core.models import RiskDecision, TradeOrder


def _point_logs(tmp_path, monkeypatch):
    cpath = str(tmp_path / "logs" / "cycle_summary.jsonl")
    rpath = str(tmp_path / "logs" / "risk_rejects.jsonl")
    monkeypatch.setattr(soak_log, "CYCLE_SUMMARY_LOG", cpath)
    monkeypatch.setattr(soak_log, "RISK_REJECTS_LOG", rpath)
    monkeypatch.setattr(soak_log, "LOG_DIR", str(tmp_path / "logs"))
    return cpath, rpath


def test_cycle_summary_appends_valid_jsonl(tmp_path, monkeypatch):
    cpath, _ = _point_logs(tmp_path, monkeypatch)
    cfg = {"watchlist_quality": {"mode": "shadow", "cycle_summary_log": True}}
    soak_log.log_cycle_summary(
        {
            "tenant_id": "default",
            "wqe_mode": "shadow",
            "n_watchlist": 10,
            "n_open_positions": 2,
            "duration_sec": 12.5,
        },
        config=cfg,
    )
    lines = Path(cpath).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["type"] == "cycle_summary"
    assert rec["wqe_mode"] == "shadow"
    assert rec["n_watchlist"] == 10
    assert "ts" in rec


def test_build_cycle_summary_without_network(monkeypatch):
    """Record builder uses passed inputs; no live market calls required."""
    monkeypatch.setattr(
        "services.watchlist_quality.config.wqe_mode",
        lambda _c=None: "shadow",
    )
    monkeypatch.setattr(
        "core.build_info.get_build_info",
        lambda: {"commit": "abc1234", "branch": "epic", "dirty": False},
    )
    monkeypatch.setattr(
        "core.runtime_identity.resolve_bot_stack",
        lambda: "local",
    )
    rec = soak_log.build_cycle_summary_record(
        config={"watchlist_quality": {"mode": "shadow"}},
        duration_sec=3.2,
        n_watchlist=5,
        n_open_positions=1,
        coin_results=[
            {"normalized_action": "HOLD", "symbol": "A/USDT"},
            {"normalized_action": "BUY", "executed": False, "blocked": True},
        ],
        eval_queue_depth=0,
        tenant_id="default",
    )
    assert rec["wqe_mode"] == "shadow"
    assert rec["n_watchlist"] == 5
    assert rec["commit"] == "abc1234"
    assert rec["duration_sec"] == 3.2
    assert rec["buys_attempted"] >= 1


def test_risk_reject_writes_code(tmp_path, monkeypatch):
    _, rpath = _point_logs(tmp_path, monkeypatch)
    cfg = {"watchlist_quality": {"mode": "shadow", "risk_reject_log": True}}
    soak_log.log_risk_reject(
        symbol="ARIA/USDT",
        side="BUY",
        source="entry_sensor_15m",
        code="market_block",
        message="Market RISK_OFF",
        quality_score=0.31,
        quality_shadow_ai=0.28,
        tenant_id="default",
        config=cfg,
        wqe_mode_value="shadow",
    )
    rec = json.loads(Path(rpath).read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["type"] == "risk_reject"
    assert rec["code"] == "market_block"
    assert rec["symbol"] == "ARIA/USDT"
    assert rec["quality_score"] == 0.31


def test_risk_manager_evaluate_logs_reject_once(tmp_path, monkeypatch):
    """BUY deny path calls soak log once with decision.code."""
    _point_logs(tmp_path, monkeypatch)
    monkeypatch.setenv("WQE_RISK_REJECT_LOG", "1")
    monkeypatch.setenv("WATCHLIST_QUALITY_MODE", "shadow")

    from risk.risk_manager import RiskManager

    calls = []

    def capture(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(
        "services.watchlist_quality.soak_log.log_risk_reject",
        capture,
    )

    rm = RiskManager.__new__(RiskManager)
    rm.config = MagicMock()
    rm.config.raw = {"watchlist_quality": {"mode": "shadow", "risk_reject_log": True}}

    decision = RiskDecision(
        approved=False,
        message="Market RISK_OFF",
        code="market_block",
    )
    monkeypatch.setattr(rm, "_evaluate_impl", lambda *a, **k: decision)

    order = TradeOrder(
        type="BUY",
        symbol="X/USDT",
        price=1.0,
        amount=0,
        usdt_amount=25,
        signal="BUY",
        source="ta",
    )
    out = rm.evaluate(order, source="ta")
    assert out.approved is False
    assert len(calls) == 1
    assert calls[0]["code"] == "market_block"
    assert calls[0]["symbol"] == "X/USDT"


def test_boot_fingerprint_writes_jsonl(tmp_path, monkeypatch):
    cpath, _ = _point_logs(tmp_path, monkeypatch)
    monkeypatch.setenv("WATCHLIST_QUALITY_MODE", "shadow")
    monkeypatch.setenv("WQE_CYCLE_SUMMARY", "1")
    monkeypatch.setattr(
        "core.build_info.get_build_info",
        lambda: {"commit": "deadbee", "branch": "epic/wqe", "dirty": False},
    )
    monkeypatch.setattr(
        "core.runtime_identity.resolve_bot_stack",
        lambda: "staging",
    )
    soak_log.log_boot_fingerprint(
        config={"watchlist_quality": {"mode": "shadow", "cycle_summary_log": True}}
    )
    text = Path(cpath).read_text(encoding="utf-8")
    rec = json.loads(text.strip().splitlines()[-1])
    assert rec["type"] == "boot_fingerprint"
    assert rec["commit"] == "deadbee"
    assert rec["wqe_mode"] == "shadow"
    assert rec["stack"] == "staging"


def test_soak_log_fail_open_on_write_error(tmp_path, monkeypatch):
    """Disk/permission errors must not raise."""
    monkeypatch.setattr(soak_log, "CYCLE_SUMMARY_LOG", "/nonexistent_root_dir_xyz/nope.jsonl")
    monkeypatch.setattr(
        "services.observability_store.append_jsonl",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    # Should not raise
    soak_log.log_cycle_summary(
        {"wqe_mode": "shadow"},
        config={"watchlist_quality": {"mode": "shadow", "cycle_summary_log": True}},
    )
    soak_log.log_risk_reject(
        symbol="Z/USDT",
        code="test",
        config={"watchlist_quality": {"mode": "shadow", "risk_reject_log": True}},
        wqe_mode_value="shadow",
    )


def test_risk_reject_disabled_when_env_off(tmp_path, monkeypatch):
    _, rpath = _point_logs(tmp_path, monkeypatch)
    monkeypatch.setenv("WQE_RISK_REJECT_LOG", "0")
    soak_log.log_risk_reject(
        symbol="A/USDT",
        code="market_block",
        config={"watchlist_quality": {"mode": "shadow", "risk_reject_log": True}},
        wqe_mode_value="shadow",
    )
    assert not Path(rpath).exists()
