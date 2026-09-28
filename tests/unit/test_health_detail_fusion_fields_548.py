"""#548: /health/detail market_fusion merges fusion_health_fields from the same bias."""

from __future__ import annotations

from unittest.mock import patch

from aria_bot import app


_FUSION_PRIMARY = {
    "active": True,
    "source": "santiment+oracle",
    "regime": "RISK_ON",
    "size_mult": 0.42,
    "sensor_policy": "active",
    "block_buys": False,
    "warmup_active": False,
    "degraded": True,
    "layers": {"santiment": {"fresh": True}, "oracle": {"fresh": False}},
}

_FUSION_SECOND_CALL = {
    "active": True,
    "source": "oracle",
    "regime": "CRASH",
    "size_mult": 0.11,
    "sensor_policy": "halt",
    "block_buys": True,
    "warmup_active": True,
    "degraded": False,
    "layers": {"other": {"fresh": True}},
}


class _NoCache:
    def available(self):
        return False

    def last_refresh(self):
        return None


def test_health_detail_market_fusion_includes_degraded_and_layers_from_same_bias():
    """A second get_global_market_bias() would pick _FUSION_SECOND_CALL for the helper."""
    with patch(
        "services.market_policy_fusion.get_global_market_bias",
        side_effect=[_FUSION_PRIMARY, _FUSION_SECOND_CALL],
    ), patch(
        "services.market_oracle_store.get_latest_snapshot",
        return_value={},
    ), patch(
        "services.market_oracle_store.snapshot_is_fresh",
        return_value=False,
    ), patch(
        "services.market_oracle_store.status_line",
        return_value="",
    ), patch(
        "bus.price_cache.price_cache_from_config",
        return_value=_NoCache(),
    ):
        rv = app.test_client().get("/health/detail")

    assert rv.status_code == 200
    body = rv.get_json()
    fusion = body["market_fusion"]
    assert fusion["market_bias_degraded"] is True
    assert fusion["layers"] == {
        "santiment": {"fresh": True},
        "oracle": {"fresh": False},
    }
    assert fusion["regime"] == "RISK_ON"
    assert fusion["size_mult"] == 0.42
    assert fusion["block_buys"] is False
