"""Process-wide Redis key prefix (#326).

Precedence:
  1. REDIS_KEY_PREFIX (if set)
  2. OHLCV_CACHE_KEY_PREFIX (transition alias; one-time WARNING if used alone)
  3. under pytest: pytest:<PYTEST_DB_SUFFIX or default>[_gwN]:
  4. aria:
"""

from __future__ import annotations

import logging
import os

DEFAULT_KEY_PREFIX = "aria:"
_ENV_REDIS_KEY_PREFIX = "REDIS_KEY_PREFIX"
_ENV_OHLCV_ALIAS = "OHLCV_CACHE_KEY_PREFIX"

_log = logging.getLogger(__name__)
_ohlcv_alias_warned = False


def _env_stripped(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _under_pytest() -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return bool(_env_stripped("PYTEST_RUNNING"))


def _sanitize_pytest_db_suffix(raw: str | None = None) -> str:
    """Same rule as tests/conftest.py::_sanitize_pytest_db_suffix."""
    if raw is None:
        raw = os.environ.get("PYTEST_DB_SUFFIX") or ""
    return "".join(
        ch for ch in raw.strip() if (ch.isalnum() and ord(ch) < 128) or ch == "_"
    )


def _effective_pytest_db_suffix() -> str:
    """Same rule as tests/conftest.py::_effective_pytest_db_suffix.

    Sequential (no PYTEST_XDIST_WORKER): sanitized value, possibly empty.
    xdist worker: ``{sanitized or 'default'}_{gwN}``. Idempotent if the
    suffix already ends with ``_{worker}``.
    """
    sanitized = _sanitize_pytest_db_suffix()
    worker = (os.environ.get("PYTEST_XDIST_WORKER") or "").strip()
    if not worker:
        return sanitized
    if sanitized.endswith(f"_{worker}"):
        return sanitized
    return f"{sanitized or 'default'}_{worker}"


def pytest_redis_key_prefix() -> str:
    """Worker-aware ``pytest:<suffix>[_gwN]:`` (#319/#321/#323/#326)."""
    return f"pytest:{_effective_pytest_db_suffix() or 'default'}:"


def _warn_ohlcv_alias_once() -> None:
    global _ohlcv_alias_warned
    if _ohlcv_alias_warned:
        return
    _ohlcv_alias_warned = True
    _log.warning(
        "OHLCV_CACHE_KEY_PREFIX is deprecated; set REDIS_KEY_PREFIX instead"
    )


def redis_key_prefix() -> str:
    """Return the Redis key prefix for this process."""
    redis_prefix = _env_stripped(_ENV_REDIS_KEY_PREFIX)
    if redis_prefix:
        return redis_prefix
    alias = _env_stripped(_ENV_OHLCV_ALIAS)
    if alias:
        _warn_ohlcv_alias_once()
        return alias
    if _under_pytest():
        return pytest_redis_key_prefix()
    return DEFAULT_KEY_PREFIX
