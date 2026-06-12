import pytest

from hermes.memory import store


@pytest.fixture
def isolated_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setattr(store, "MEMORY_DIR", tmp_path)
    yield tmp_path


def test_upsert_skill_deduplicates(isolated_memory):
    skill = {
        "pattern": "rsi_buy_low down failed",
        "confidence": 0.4,
        "applies_to": {"symbol": "ARIA/USDT", "timeframe": "4h"},
        "variable": "rsi_buy_low",
        "promoted": False,
    }
    store.upsert_skill(skill, old_value=28, new_value=26)
    store.upsert_skill(skill, old_value=28, new_value=26)
    data = store.load_skills()
    assert len(data["skills"]) == 1
    assert data["skills"][0]["evidence_count"] == 2


def test_prune_skills_removes_low_confidence(isolated_memory):
    store.upsert_skill({
        "pattern": "low",
        "confidence": 0.1,
        "applies_to": {"symbol": "X", "timeframe": "4h"},
        "variable": "a",
        "promoted": False,
    }, old_value=1, new_value=2)
    # upsert_skill already calls prune_skills internally
    assert len(store.load_skills()["skills"]) == 0