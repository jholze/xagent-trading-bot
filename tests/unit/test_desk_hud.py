import json
from pathlib import Path

from services.desk.hud import build_hud, desk_enabled, next_edge

_LAB_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "desk" / "lab_snapshot.json"
LAB = json.loads(_LAB_PATH.read_text(encoding="utf-8"))


def test_desk_disabled_by_default_without_flag():
    assert desk_enabled({}) is False


def test_desk_enabled_flag():
    assert desk_enabled({"desk": {"enabled": True}}) is True
    assert desk_enabled({"desk": {"enabled": False}}) is False


def test_lab_ta_miss_social_armed_memory_idle():
    hud = build_hud(LAB)
    assert hud["ta"]["stance"] == "MISS"
    assert hud["social"]["stance"] == "ARMED"
    assert hud["memory"]["stance"] == "IDLE"
    assert hud["social"]["lead"].startswith("CMC 83")
    assert "muted" in hud["social"]["chorus"].lower() or "thin" in hud["social"]["chorus"].lower()


def test_lab_next_edge_names_dca_not_relvol():
    line = next_edge(LAB, build_hud(LAB))
    assert line.startswith("TA:")
    assert "DCA" in line
    assert "RSI" in line
