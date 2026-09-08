"""#323 — stale oracle snapshot is discarded even under fail_closed_guards=log."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from core.actions import SELL_PARTIAL_30
from services.market_oracle.store import reset_for_tests as reset_ora
from services.market_oracle.store import store_snapshot as store_ora
from services.santiment.store import reset_for_tests as reset_san
from services.santiment.store import store_snapshot as store_san
from strategies.oracle_climax import (
    MODE_GRIND,
    MODE_IDLE,
    filter_grind_candidates,
    reset_cycle,
    reset_stale_episode_for_tests,
    resolve_climax_decision,
)


def _raw(mode: str = "log") -> dict:
    return {
        "risk": {"fail_closed_guards": mode},
        "sell_policy": {"oracle_climax": {"enabled": True}},
        "architecture": {
            "santiment_risk_enabled": False,
            "market_oracle_risk_enabled": True,
            "market_oracle_warmup_sec": 0,
        },
    }


def _not_armed_feats() -> dict:
    return {
        "btc_ret_24h_pct": 3.5,
        "eth_ret_24h_pct": 4.0,
        "breadth_pct_green": 0.55,
        "btc_ret_4h_pct": 1.2,
        "btc_trend_4h": 1.0,
        "btc_ret_1h_pct": 0.8,
    }


def _ora_snap(*, as_of: str, state: str = "RISK_ON") -> dict:
    return {
        "source": "market_oracle",
        "state": state,
        "regime": state,
        "size_mult": 1.0,
        "sensor_policy": "active",
        "ttl_sec": 900,
        "as_of": as_of,
        "features": _not_armed_feats(),
    }


def _san_snap(*, as_of: str) -> dict:
    return {
        "source": "santiment",
        "regime": "NEUTRAL",
        "size_mult": 1.0,
        "sensor_policy": "active",
        "ttl_sec": 1800,
        "as_of": as_of,
        "features": {},
    }


def _warning_messages(mock_log) -> list[str]:
    out = []
    for args, kwargs in mock_log.call_args_list:
        level = kwargs.get("level")
        if level is None and len(args) >= 2:
            level = args[1]
        if str(level).upper() == "WARNING":
            out.append(str(args[0] if args else ""))
    return out


@pytest.fixture(autouse=True)
def _reset_climax_stores():
    reset_cycle()
    reset_stale_episode_for_tests()
    reset_ora()
    reset_san()
    yield
    reset_cycle()
    reset_stale_episode_for_tests()
    reset_ora()
    reset_san()


def test_stale_risk_on_log_is_idle_warns_once():
    now = datetime.now(timezone.utc)
    stale = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store_ora(_ora_snap(as_of=stale))
    raw = _raw("log")
    fusion = {"regime": "NEUTRAL"}
    with patch(
        "services.market_policy_fusion.get_global_market_bias", return_value=fusion
    ), patch("logger.log") as mock_log:
        first = resolve_climax_decision(raw)
        second = resolve_climax_decision(raw)
    assert first.mode == MODE_IDLE
    assert "oracle_stale" in first.reasons
    assert second.mode == MODE_IDLE
    assert "oracle_stale" in second.reasons
    warns = [m for m in _warning_messages(mock_log) if "stale" in m.lower()]
    assert len(warns) == 1
    kept, blocked = filter_grind_candidates(
        [(SELL_PARTIAL_30, 5, "bb_upper")], first
    )
    assert blocked == []
    assert kept


def test_fresh_risk_on_not_armed_is_grind():
    as_of = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    store_ora(_ora_snap(as_of=as_of))
    raw = _raw("log")
    with patch(
        "services.market_policy_fusion.get_global_market_bias",
        return_value={"regime": "NEUTRAL"},
    ):
        grind = resolve_climax_decision(raw)
    assert grind.mode == MODE_GRIND
    kept, blocked = filter_grind_candidates(
        [(SELL_PARTIAL_30, 5, "bb_upper")], grind
    )
    assert "bb_upper" in blocked
    assert kept == []


def test_store_writes_pytest_prefix_never_aria():
    from bus.redis_client import get_redis

    client = get_redis()
    if client is None:
        pytest.skip("Redis unreachable")
    prefix = os.environ.get("OHLCV_CACHE_KEY_PREFIX") or ""
    assert prefix.startswith("pytest:")
    before_ora = set(client.scan_iter(match="aria:market_oracle:*", count=200))
    before_san = set(client.scan_iter(match="aria:santiment:*", count=200))
    as_of = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    store_ora(_ora_snap(as_of=as_of))
    store_san(_san_snap(as_of=as_of))
    pytest_ora = set(client.scan_iter(match=f"{prefix}market_oracle:*", count=200))
    pytest_san = set(client.scan_iter(match=f"{prefix}santiment:*", count=200))
    assert f"{prefix}market_oracle:latest" in pytest_ora
    assert f"{prefix}santiment:latest" in pytest_san
    after_ora = set(client.scan_iter(match="aria:market_oracle:*", count=200))
    after_san = set(client.scan_iter(match="aria:santiment:*", count=200))
    assert after_ora == before_ora
    assert after_san == before_san
