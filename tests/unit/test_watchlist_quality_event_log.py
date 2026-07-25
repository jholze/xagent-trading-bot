"""WQE event log — durable soak data."""

from __future__ import annotations

import json
from pathlib import Path

from services.watchlist_quality.event_log import (
    WQE_EVENTS_LOG,
    log_buy_block,
    log_sync_summary,
    log_wqe_event,
)


def test_log_wqe_event_writes_jsonl(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "services.watchlist_quality.event_log.WQE_EVENTS_LOG",
        str(tmp_path / "logs" / "wqe_events.jsonl"),
    )
    # re-import path via module attr
    import services.watchlist_quality.event_log as el

    el.WQE_EVENTS_LOG = str(tmp_path / "logs" / "wqe_events.jsonl")
    log_wqe_event(
        "wqe_sync",
        {"mode": "shadow", "n_in": 3, "scored": 3},
        config={"watchlist_quality": {"mode": "shadow", "event_log": True}},
    )
    path = Path(el.WQE_EVENTS_LOG)
    assert path.is_file()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 1
    rec = json.loads(lines[-1])
    assert rec["type"] == "wqe_sync"
    assert rec["mode"] == "shadow"
    assert rec["n_in"] == 3


def test_log_sync_summary_writes_coin_rows(tmp_path, monkeypatch):
    import services.watchlist_quality.event_log as el

    el.WQE_EVENTS_LOG = str(tmp_path / "logs" / "wqe_events.jsonl")
    summary = {
        "mode": "shadow",
        "n_in": 1,
        "scored": 1,
        "behavior_change": False,
        "coins": [
            {
                "symbol": "A/USDT",
                "quality_score": 0.7,
                "quality_shadow_ai": 0.65,
                "tier_hint": "T1",
                "flags": [],
                "memory": {"entry_bias": "neutral"},
                "ai": {"stance": "keep", "source": "ok", "adjust": 0, "confidence": 0.5},
                "metrics": {"quote_vol_24h": 1e6, "source": "cmc_trending"},
            }
        ],
    }
    log_sync_summary(
        summary, config={"watchlist_quality": {"mode": "shadow", "event_log": True}}
    )
    text = Path(el.WQE_EVENTS_LOG).read_text(encoding="utf-8")
    assert "wqe_sync" in text
    assert "wqe_coin" in text
    assert "A/USDT" in text


def test_log_buy_block(tmp_path, monkeypatch):
    import services.watchlist_quality.event_log as el

    el.WQE_EVENTS_LOG = str(tmp_path / "logs" / "wqe_events.jsonl")
    log_buy_block(
        "BAD/USDT",
        "min_buy_score",
        source="ta",
        mode="enforce",
        quality_score=0.1,
        config={"watchlist_quality": {"mode": "enforce", "event_log": True}},
    )
    rec = json.loads(Path(el.WQE_EVENTS_LOG).read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["type"] == "wqe_buy_block"
    assert rec["symbol"] == "BAD/USDT"
