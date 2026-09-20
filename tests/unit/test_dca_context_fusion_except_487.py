"""#487 — fusion except in build_dca_context must not force fail_closed_guards=log."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from strategies.dca_context import build_dca_context, _fail_closed_guards_from_config
from strategies.dca_policy import dca_policy_config, evaluate_dca_policy


def _deny_cfg(**extra):
    raw = {
        "risk": {"fail_closed_guards": "deny", "cash_policy": {"enabled": False}},
        "memory": {"enabled": False, "coin_facts": {"enabled": False}},
    }
    raw.update(extra)
    return raw


def _fusion_throw_ctx(raw):
    with patch(
        "services.market_policy_fusion.get_global_market_bias",
        side_effect=RuntimeError("fusion sidecar down"),
    ), patch("intelligence.memory.cache.get_coin_profile", return_value=None), patch(
        "intelligence.macro.snapshot.get_risk_multipliers", return_value={}
    ):
        return build_dca_context(
            symbol="NEAR/USDT",
            include_rag=False,
            config_raw=raw,
        )


class TestFailClosedGuardsFromConfig:
    def test_deny_from_risk_block(self):
        assert _fail_closed_guards_from_config({"risk": {"fail_closed_guards": "deny"}}) == "deny"

    def test_explicit_log_honored(self):
        assert _fail_closed_guards_from_config({"risk": {"fail_closed_guards": "log"}}) == "log"

    def test_missing_key_defaults_deny(self):
        assert _fail_closed_guards_from_config({}) == "deny"
        assert _fail_closed_guards_from_config(None) == "deny"


class TestFusionExceptKeepsConfigDeny:
    """Would have failed under the old except that wrote fail_closed_guards='log'."""

    def test_fusion_throw_leaves_deny_and_degraded(self):
        ctx = _fusion_throw_ctx(_deny_cfg())
        assert ctx.fail_closed_guards == "deny"
        assert ctx.fusion_degraded is True
        assert ctx.fusion_measured is False
        assert ctx.fusion_fresh is False
        assert ctx.fusion_missing is True
        assert ctx.fusion_size_mult == pytest.approx(1.0)

    def test_fusion_throw_deny_no_deploy_mult(self):
        """Same policy inputs as test_deny_degraded_no_deploy_mult, via except path."""
        ctx = _fusion_throw_ctx(_deny_cfg())
        cfg = dca_policy_config({"policy": {"enabled": True, "deploy_mult": 1.35}})
        r = evaluate_dca_policy(ctx, cfg)
        assert "deploy_boost" not in r.reason_codes
        assert r.size_mult == pytest.approx(1.0)

    def test_fusion_throw_explicit_log_still_deploys(self):
        raw = _deny_cfg()
        raw["risk"] = {"fail_closed_guards": "log", "cash_policy": {"enabled": False}}
        ctx = _fusion_throw_ctx(raw)
        assert ctx.fail_closed_guards == "log"
        cfg = dca_policy_config({"policy": {"enabled": True, "deploy_mult": 1.35}})
        r = evaluate_dca_policy(ctx, cfg)
        assert "deploy_boost" in r.reason_codes
        assert r.size_mult == pytest.approx(1.35)

    def test_fusion_throw_missing_key_defaults_deny(self):
        ctx = _fusion_throw_ctx({"risk": {"cash_policy": {"enabled": False}}})
        assert ctx.fail_closed_guards == "deny"
        assert ctx.fusion_degraded is True


class TestInnerGuardsImportExcept:
    def test_guards_helper_throw_reads_config_deny(self):
        bias = {
            "size_mult": 0.85,
            "block_buys": False,
            "degraded": False,
            "fresh": True,
            "layers": {},
        }
        with patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value=bias,
        ), patch(
            "risk.risk_manager._fail_closed_guards_mode",
            side_effect=RuntimeError("helper import/call failed"),
        ), patch("intelligence.memory.cache.get_coin_profile", return_value=None), patch(
            "intelligence.macro.snapshot.get_risk_multipliers", return_value={}
        ):
            ctx = build_dca_context(
                symbol="NEAR/USDT",
                include_rag=False,
                config_raw=_deny_cfg(),
            )
        assert ctx.fail_closed_guards == "deny"
        assert ctx.fusion_degraded is False
        assert ctx.fusion_missing is False
