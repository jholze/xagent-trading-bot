import json
from pathlib import Path

from services.desk.hud import build_hud, desk_enabled, next_edge

_LAB_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "desk" / "lab_snapshot.json"
LAB = json.loads(_LAB_PATH.read_text(encoding="utf-8"))

_LAB_NEXT_EDGE = (
    "TA: dip miss; next edge is DCA when RSI<40 (RelVol cap is a different path)."
)


def _lab(**overrides):
    facts = dict(LAB)
    facts.update(overrides)
    return facts


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
    assert hud["ta"] == {
        "setup": "dip miss",
        "path": "DCA 1/2",
        "blocker": "not at lower BB",
        "stance": "MISS",
    }
    assert "relvol" not in hud["ta"]["blocker"].lower()
    assert hud["social"]["lead"] == "CMC 83×72 → 60"
    assert "muted" in hud["social"]["chorus"].lower()


def test_lab_next_edge_names_dca_not_relvol():
    hud = build_hud(LAB)
    line = next_edge(LAB, hud)
    assert line.startswith("TA:")
    assert "DCA" in line
    assert "RSI" in line
    assert line == _LAB_NEXT_EDGE


def test_lab_golden_copy():
    hud = build_hud(LAB)
    assert hud["ta"] == {
        "setup": "dip miss",
        "path": "DCA 1/2",
        "blocker": "not at lower BB",
        "stance": "MISS",
    }
    assert "relvol" not in hud["ta"]["blocker"].lower()
    assert hud["social"]["lead"] == "CMC 83×72 → 60"
    assert "muted" in hud["social"]["chorus"].lower()
    assert next_edge(LAB, hud) == _LAB_NEXT_EDGE


def test_nonsold_miss_is_not_dip_miss():
    facts = _lab(rsi=50)
    hud = build_hud(facts)
    assert hud["ta"]["setup"] == "miss"
    assert hud["ta"]["stance"] == "MISS"
    line = next_edge(facts, hud)
    assert "miss" in line
    assert "dip miss" not in line


def test_memory_structure_risk_blocks():
    facts = _lab(memory_flag="structure_risk")
    hud = build_hud(facts)
    assert hud["memory"]["stance"] == "BLOCK"
    assert hud["memory"]["flag"] == "structure_risk"


def test_memory_hard_negative_blocks():
    facts = _lab(memory_flag="hard_negative")
    hud = build_hud(facts)
    assert hud["memory"]["stance"] == "BLOCK"
    assert hud["memory"]["flag"] == "hard_negative"


def test_memory_unlock_blocks():
    facts = _lab(memory_flag="unlock")
    hud = build_hud(facts)
    assert hud["memory"]["stance"] == "BLOCK"
    assert hud["memory"]["flag"] == "unlock"


def test_memory_flow_only_size_down():
    facts = _lab(memory_flag="flow_only")
    hud = build_hud(facts)
    assert hud["memory"]["stance"] == "SIZE↓"
    assert hud["memory"]["flag"] == "flow_only"


def test_memory_soft_block_bias_blocks():
    facts = _lab(memory_bias="soft_block")
    hud = build_hud(facts)
    assert hud["memory"]["stance"] == "BLOCK"


def test_lab_memory_remains_idle():
    hud = build_hud(LAB)
    assert hud["memory"]["stance"] == "IDLE"
    assert hud["memory"]["flag"] is None


def test_missing_at_lower_bb_is_idle_not_miss():
    facts = dict(LAB)
    del facts["at_lower_bb"]
    hud = build_hud(facts)
    assert hud["ta"]["stance"] != "MISS"
    assert hud["ta"]["stance"] == "IDLE"
    assert hud["ta"]["blocker"] != "not at lower BB"

    facts_none = _lab(at_lower_bb=None)
    hud_none = build_hud(facts_none)
    assert hud_none["ta"]["stance"] == "IDLE"
    assert hud_none["ta"]["blocker"] != "not at lower BB"


def test_fusion_crash_does_not_social_add():
    facts = _lab(fusion_regime="CRASH")
    hud = build_hud(facts)
    assert hud["social"]["stance"] == "BLOCK"
    line = next_edge(facts, hud)
    assert not line.startswith("SOCIAL:")
    assert "→ add" not in line


def _social_add_candidate(**overrides):
    """TA not MISS and no remaining DCA — social would add if fusion were NEUTRAL."""
    return _lab(at_lower_bb=True, dca_rounds=2, dca_max_rounds=2, **overrides)


def _assert_no_social_add(facts):
    hud = build_hud(facts)
    assert hud["ta"]["stance"] != "MISS"
    assert hud["social"]["stance"] == "BLOCK"
    line = next_edge(facts, hud)
    assert not line.startswith("SOCIAL:")
    assert "→ add" not in line
    return hud, line


def test_fusion_crash_without_dca_does_not_social_add():
    _assert_no_social_add(_social_add_candidate(fusion_regime="CRASH"))


def test_fusion_risk_off_without_dca_does_not_social_add():
    _assert_no_social_add(_social_add_candidate(fusion_regime="RISK_OFF"))


def test_fusion_neutral_without_dca_social_armed():
    facts = _social_add_candidate(fusion_regime="NEUTRAL")
    hud = build_hud(facts)
    assert hud["ta"]["stance"] != "MISS"
    assert hud["social"]["stance"] == "ARMED"
    line = next_edge(facts, hud)
    assert line.startswith("SOCIAL:")
    assert "→ add" in line

