"""#326: central Redis key prefix — precedence, pytest fallback, worker suffix."""

from __future__ import annotations

import logging

import pytest

from bus.redis_keys import pytest_redis_key_prefix, redis_key_prefix


def _clear_prefix_env(monkeypatch) -> None:
    monkeypatch.delenv("REDIS_KEY_PREFIX", raising=False)
    monkeypatch.delenv("OHLCV_CACHE_KEY_PREFIX", raising=False)


def _not_under_pytest(monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("PYTEST_RUNNING", raising=False)
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)


def test_redis_key_prefix_wins_over_ohlcv_alias_and_pytest(monkeypatch):
    monkeypatch.setenv("REDIS_KEY_PREFIX", "custom:")
    monkeypatch.setenv("OHLCV_CACHE_KEY_PREFIX", "old:")
    monkeypatch.setenv("PYTEST_RUNNING", "1")
    monkeypatch.setenv("PYTEST_DB_SUFFIX", "rk326")
    assert redis_key_prefix() == "custom:"


def test_ohlcv_alias_used_when_redis_key_prefix_unset(monkeypatch):
    monkeypatch.delenv("REDIS_KEY_PREFIX", raising=False)
    monkeypatch.setenv("OHLCV_CACHE_KEY_PREFIX", "legacy:")
    monkeypatch.setenv("PYTEST_RUNNING", "1")
    assert redis_key_prefix() == "legacy:"


def test_ohlcv_alias_warns_once_when_only_old_name_set(monkeypatch, caplog):
    import bus.redis_keys as mod

    _clear_prefix_env(monkeypatch)
    monkeypatch.setenv("OHLCV_CACHE_KEY_PREFIX", "legacy:")
    mod._ohlcv_alias_warned = False
    with caplog.at_level(logging.WARNING, logger="bus.redis_keys"):
        assert redis_key_prefix() == "legacy:"
        assert redis_key_prefix() == "legacy:"
    records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(records) == 1
    assert "OHLCV_CACHE_KEY_PREFIX" in records[0].getMessage()
    assert "REDIS_KEY_PREFIX" in records[0].getMessage()


def test_no_deprecation_when_redis_key_prefix_set(monkeypatch, caplog):
    import bus.redis_keys as mod

    monkeypatch.setenv("REDIS_KEY_PREFIX", "custom:")
    monkeypatch.setenv("OHLCV_CACHE_KEY_PREFIX", "legacy:")
    mod._ohlcv_alias_warned = False
    with caplog.at_level(logging.WARNING, logger="bus.redis_keys"):
        assert redis_key_prefix() == "custom:"
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_pytest_fallback_uses_db_suffix(monkeypatch):
    _clear_prefix_env(monkeypatch)
    monkeypatch.setenv("PYTEST_RUNNING", "1")
    monkeypatch.setenv("PYTEST_DB_SUFFIX", "rk326")
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    assert redis_key_prefix() == "pytest:rk326:"


def test_pytest_fallback_via_pytest_current_test(monkeypatch):
    _clear_prefix_env(monkeypatch)
    monkeypatch.delenv("PYTEST_RUNNING", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/unit/test_redis_keys.py::x")
    monkeypatch.setenv("PYTEST_DB_SUFFIX", "rk326")
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    assert redis_key_prefix() == "pytest:rk326:"


def test_pytest_fallback_default_suffix_when_unset(monkeypatch):
    _clear_prefix_env(monkeypatch)
    monkeypatch.setenv("PYTEST_RUNNING", "1")
    monkeypatch.delenv("PYTEST_DB_SUFFIX", raising=False)
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    assert redis_key_prefix() == "pytest:default:"


def test_pytest_fallback_sanitizes_suffix(monkeypatch):
    _clear_prefix_env(monkeypatch)
    monkeypatch.setenv("PYTEST_RUNNING", "1")
    monkeypatch.setenv("PYTEST_DB_SUFFIX", "rk-326!")
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    assert redis_key_prefix() == "pytest:rk326:"


def test_xdist_worker_suffix_appended(monkeypatch):
    _clear_prefix_env(monkeypatch)
    monkeypatch.setenv("PYTEST_RUNNING", "1")
    monkeypatch.setenv("PYTEST_DB_SUFFIX", "rk326")
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
    assert redis_key_prefix() == "pytest:rk326_gw3:"
    assert pytest_redis_key_prefix() == "pytest:rk326_gw3:"


def test_xdist_worker_suffix_idempotent(monkeypatch):
    _clear_prefix_env(monkeypatch)
    monkeypatch.setenv("PYTEST_RUNNING", "1")
    monkeypatch.setenv("PYTEST_DB_SUFFIX", "rk326_gw3")
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
    assert redis_key_prefix() == "pytest:rk326_gw3:"


def test_xdist_worker_without_suffix_uses_default(monkeypatch):
    _clear_prefix_env(monkeypatch)
    monkeypatch.setenv("PYTEST_RUNNING", "1")
    monkeypatch.delenv("PYTEST_DB_SUFFIX", raising=False)
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    assert redis_key_prefix() == "pytest:default_gw0:"


def test_outside_pytest_defaults_to_aria(monkeypatch):
    _clear_prefix_env(monkeypatch)
    _not_under_pytest(monkeypatch)
    monkeypatch.delenv("PYTEST_DB_SUFFIX", raising=False)
    assert redis_key_prefix() == "aria:"


def test_blank_env_values_are_unset(monkeypatch):
    monkeypatch.setenv("REDIS_KEY_PREFIX", "  ")
    monkeypatch.setenv("OHLCV_CACHE_KEY_PREFIX", "")
    monkeypatch.setenv("PYTEST_RUNNING", "1")
    monkeypatch.setenv("PYTEST_DB_SUFFIX", "rk326")
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    assert redis_key_prefix() == "pytest:rk326:"


def test_ohlcv_cache_and_stores_use_central_prefix(monkeypatch):
    monkeypatch.setenv("REDIS_KEY_PREFIX", "central:")
    monkeypatch.delenv("OHLCV_CACHE_KEY_PREFIX", raising=False)
    from bus.ohlcv_cache import OhlcvCache
    from services.market_oracle.store import _redis_key as ora_key
    from services.santiment.store import _redis_key as san_key

    cache = OhlcvCache(config_raw={"architecture": {}})
    assert cache.key_prefix == "central:"
    assert ora_key() == "central:market_oracle:latest"
    assert san_key() == "central:santiment:latest"


def test_stores_keep_ohlcv_alias(monkeypatch):
    monkeypatch.delenv("REDIS_KEY_PREFIX", raising=False)
    monkeypatch.setenv("OHLCV_CACHE_KEY_PREFIX", "legacy:")
    from services.market_oracle.store import _key_prefix as ora_prefix
    from services.santiment.store import _key_prefix as san_prefix

    assert ora_prefix() == "legacy:"
    assert san_prefix() == "legacy:"
