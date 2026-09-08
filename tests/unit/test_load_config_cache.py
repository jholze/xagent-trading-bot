"""load_config must not re-read config.json on a default-tenant cache hit (#304)."""

from __future__ import annotations

import pytest

import data_manager as dm


@pytest.fixture(autouse=True)
def _restore_config_cache():
    prev = dm._config_cache
    try:
        yield
    finally:
        dm._config_cache = prev


def test_load_config_skips_disk_on_cache_hit(monkeypatch):
    reads = {"n": 0}

    def counting_load():
        reads["n"] += 1
        return {"trading_mode": "paper", "max_open_positions": 7}

    monkeypatch.setattr(dm, "_load_default_config_from_disk", counting_load)
    monkeypatch.setattr(dm, "_apply_trading_profile_merge", lambda cfg, body: dict(cfg))
    dm._config_cache = None

    first = dm.load_config()
    assert reads["n"] == 1
    assert first["max_open_positions"] == 7

    second = dm.load_config()
    assert reads["n"] == 1
    assert second is first

    third = dm.load_config(tenant_id="default")
    assert reads["n"] == 1
    assert third is first


def test_reload_config_invalidates_and_rereads(monkeypatch):
    reads = {"n": 0}

    def counting_load():
        reads["n"] += 1
        return {"trading_mode": "paper", "token": reads["n"]}

    monkeypatch.setattr(dm, "_load_default_config_from_disk", counting_load)
    monkeypatch.setattr(dm, "_apply_trading_profile_merge", lambda cfg, body: dict(cfg))
    dm._config_cache = None

    first = dm.load_config()
    assert first["token"] == 1
    reloaded = dm.reload_config()
    assert reads["n"] == 2
    assert reloaded["token"] == 2
    assert reloaded is not first
    again = dm.load_config()
    assert reads["n"] == 2
    assert again is reloaded


def test_tenant_load_config_still_reads_disk(monkeypatch):
    reads = {"n": 0}

    def counting_load():
        reads["n"] += 1
        return {"trading_mode": "paper", "max_open_positions": 3}

    monkeypatch.setattr(dm, "_load_default_config_from_disk", counting_load)
    monkeypatch.setattr(dm, "_apply_trading_profile_merge", lambda cfg, body: dict(cfg))
    monkeypatch.setattr(dm, "_should_use_mongo_for_tenant_config", lambda cfg=None: False)
    dm._config_cache = {"trading_mode": "paper", "cached": True}

    cfg = dm.load_config(tenant_id="henry")
    assert reads["n"] == 1
    assert "cached" not in cfg
    dm.load_config(tenant_id="henry")
    assert reads["n"] == 2
