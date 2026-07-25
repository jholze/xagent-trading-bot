"""W1 Memory→WQE adapter unit tests (arena §4.9.7 / #125)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from services.watchlist_quality.memory_bias import (
    MemoryWqeInput,
    get_memory_wqe_input,
    memory_wqe_enabled,
)


def _prof(
    symbol: str = "ARIA/USDT",
    *,
    entry_bias: str = "neutral",
    size_bias: float = 1.0,
    rationale: str = "",
    features: dict | None = None,
):
    return SimpleNamespace(
        symbol=symbol,
        entry_bias=entry_bias,
        size_bias=size_bias,
        rationale=rationale,
        features=features or {},
    )


NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)


class TestMemoryWqeEnabled:
    def test_default_enabled(self):
        assert memory_wqe_enabled({}) is True
        assert memory_wqe_enabled(None) is True

    def test_kill_switch(self):
        cfg = {"watchlist_quality": {"memory": {"enabled": False}}}
        assert memory_wqe_enabled(cfg) is False


class TestGetMemoryWqeInput:
    def test_no_profile_fail_open(self):
        with patch(
            "intelligence.memory.cache.get_coin_profile", return_value=None
        ), patch(
            "intelligence.memory.store.memory_enabled", return_value=True
        ):
            m = get_memory_wqe_input("FOO/USDT", config={}, now=NOW)
        assert m.entry_bias == "neutral"
        assert m.memory_score == pytest.approx(0.5)
        assert m.hard_exclude_new_add is False
        assert m.source == "default"

    def test_memory_disabled_global(self):
        with patch("intelligence.memory.store.memory_enabled", return_value=False):
            m = get_memory_wqe_input("FOO/USDT", config={}, now=NOW)
        assert m.source == "disabled"
        assert m.memory_score == pytest.approx(0.5)
        assert m.hard_exclude_new_add is False

    def test_wqe_memory_disabled(self):
        cfg = {"watchlist_quality": {"memory": {"enabled": False}}}
        m = get_memory_wqe_input("FOO/USDT", config=cfg, now=NOW)
        assert m.source == "disabled"
        assert m.hard_exclude_new_add is False

    def test_prefer(self):
        prof = _prof(entry_bias="prefer", size_bias=1.0, rationale="strong")
        m = get_memory_wqe_input("H/USDT", profile=prof, config={}, now=NOW)
        assert m.entry_bias == "prefer"
        assert m.memory_score >= 0.65
        assert m.hard_exclude_new_add is False
        assert m.source == "profile"

    def test_soft_block_all_new_active(self):
        until = (NOW + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        prof = _prof(
            entry_bias="soft_block",
            size_bias=0.5,
            features={"soft_block_scope": "all_new", "soft_block_until": until},
            rationale="gross_loss",
        )
        cfg = {
            "watchlist_quality": {
                "honor_memory_soft_block": True,
                "memory": {
                    "soft_penalty": 0.40,
                    "exclude_new_adds_on_soft_block": True,
                    "apply_size_bias_to_score": True,
                },
            }
        }
        m = get_memory_wqe_input("BDX/USDT", profile=prof, config=cfg, now=NOW)
        assert m.entry_bias == "soft_block"
        assert m.hard_exclude_new_add is True
        assert m.ttl_active is True
        # 0.5 - 0.40 = 0.10, * size_bias 0.5 = 0.05
        assert m.memory_score < 0.5
        assert m.memory_score == pytest.approx(0.05, abs=0.01)

    def test_soft_block_ttl_expired_treats_neutral(self):
        until = (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        prof = _prof(
            entry_bias="soft_block",
            features={"soft_block_scope": "all_new", "soft_block_until": until},
        )
        m = get_memory_wqe_input("BDX/USDT", profile=prof, config={}, now=NOW)
        assert m.entry_bias == "neutral"
        assert m.hard_exclude_new_add is False
        assert m.ttl_active is False
        assert m.memory_score == pytest.approx(0.5)

    def test_soft_block_sensor_only_no_hard_exclude(self):
        until = (NOW + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        prof = _prof(
            entry_bias="soft_block",
            size_bias=1.0,
            features={"soft_block_scope": "sensor_only", "soft_block_until": until},
        )
        cfg = {
            "watchlist_quality": {
                "memory": {
                    "soft_penalty_sensor_only": 0.15,
                    "exclude_new_adds_on_soft_block": True,
                    "apply_size_bias_to_score": False,
                }
            }
        }
        m = get_memory_wqe_input("X/USDT", profile=prof, config=cfg, now=NOW)
        assert m.entry_bias == "soft_block"
        assert m.scope == "sensor_only"
        assert m.hard_exclude_new_add is False
        assert m.memory_score == pytest.approx(0.35, abs=0.01)

    def test_honor_false_never_hard_exclude(self):
        until = (NOW + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        prof = _prof(
            entry_bias="soft_block",
            features={"soft_block_scope": "all_new", "soft_block_until": until},
        )
        cfg = {
            "watchlist_quality": {
                "honor_memory_soft_block": False,
                "memory": {"exclude_new_adds_on_soft_block": True},
            }
        }
        m = get_memory_wqe_input("Y/USDT", profile=prof, config=cfg, now=NOW)
        assert m.hard_exclude_new_add is False
        assert m.entry_bias == "soft_block"
        assert m.memory_score < 0.5

    def test_profile_load_error_fail_open(self):
        with patch(
            "intelligence.memory.cache.get_coin_profile",
            side_effect=RuntimeError("mongo down"),
        ), patch(
            "intelligence.memory.store.memory_enabled", return_value=True
        ):
            m = get_memory_wqe_input("Z/USDT", config={}, now=NOW)
        assert m.source == "error"
        assert m.memory_score == pytest.approx(0.5)
        assert m.hard_exclude_new_add is False

    def test_soft_block_open_position_symbol_still_demotes_score_only(self):
        """Adapter has no list side effects — only score/exclude flags for WQE."""
        until = (NOW + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        prof = _prof(
            entry_bias="soft_block",
            features={"soft_block_scope": "all_new", "soft_block_until": until},
        )
        m = get_memory_wqe_input("OPEN/USDT", profile=prof, config={}, now=NOW)
        assert isinstance(m, MemoryWqeInput)
        assert m.memory_score < 0.5
        # POS keep is a consumer concern; adapter only exposes flags
        assert m.hard_exclude_new_add is True

    def test_empty_symbol(self):
        m = get_memory_wqe_input("  ", config={}, now=NOW)
        assert m.source == "default"
        assert m.symbol == ""
