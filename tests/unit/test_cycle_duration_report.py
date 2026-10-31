"""Parser for scripts/analysis/cycle_duration.py. No production log required."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parents[2] / "scripts" / "analysis" / "cycle_duration.py"
    spec = importlib.util.spec_from_file_location("cycle_duration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_own_time_splits_a_shared_cycle_start(tmp_path):
    mod = _load()
    lines = [
        {
            "message": "[2026-10-11 00:02:05] [INFO] cycle_summary mode=soft wl=74 pos=53 dur=95.0s regime=NEUTRAL",
            "timestamp": "2026-10-11T00:02:05Z",
        },
        {
            "message": "[2026-10-11 00:03:28] [INFO] cycle_summary mode=soft wl=68 pos=36 dur=178.0s regime=NEUTRAL",
            "timestamp": "2026-10-11T00:03:28Z",
        },
        {
            "message": "[2026-10-11 00:04:07] [INFO] cycle_summary mode=soft wl=64 pos=35 dur=217.0s regime=NEUTRAL",
            "timestamp": "2026-10-11T00:04:07Z",
        },
        {
            "message": "[2026-10-11 00:05:37] [INFO] Cycle completed in 89s (eval_queue, 53 positions first)",
            "timestamp": "2026-10-11T00:05:37Z",
        },
        {
            "message": "[2026-10-11 00:05:37] [INFO] cycle_summary mode=soft wl=74 pos=53 dur=89.0s regime=NEUTRAL",
            "timestamp": "2026-10-11T00:05:37Z",
        },
    ]
    path = tmp_path / "cycles.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in lines), encoding="utf-8")
    summaries, completed = mod.load_events(path)
    loops = mod.segment_loops(summaries)
    assert len(loops) == 2
    assert [round(v) for v in mod.own_times(loops[0])] == [95, 83, 39]
    report = mod.daily_report(loops, completed)
    assert report[0]["day"] == "2026-10-11"
    assert report[0]["slot0_p50"] == 92 or abs(report[0]["slot0_p50"] - 92) < 1
    assert report[0]["completed_n"] == 1
    assert report[0]["completed_p50"] == 89
