"""AC7 (Rev1, Kay on #737): the allocator CONFIG applies. The code fallback in intelligence/strategy_allocator.py:50-51
(default_grid_weight 0.6 / default_momentum_weight 0.4) is NOT changed, but using it must log a WARNING (once per
tenant and process). Real StrategyAllocator.allocate in the tenant context; only the config source is patched.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from cs_helpers import tenant

TENANTS = ("default", "henry")


def _regime():
    from core.models import RegimeResult

    return RegimeResult(primary_regime="RANGING", confidence=0.8, weighted_score=0.0, volatility_tier="stable",
                        sentiment_score=0.0)


def _warnings(spy):
    return [c.args[0] for c in spy.call_args_list
            if len(c.args) > 1 and c.args[1] == "WARNING" and "code fallback" in str(c.args[0])]


def _reset():
    import intelligence.strategy_allocator as sa

    getattr(sa, "_FALLBACK_WARNED", set()).clear()


@pytest.fixture(autouse=True)
def _no_tenant_body():
    import data_manager

    with patch.object(data_manager, "_load_tenant_config_body", lambda *_a, **_k: None):
        _reset()
        yield
        _reset()


class TestAC7AllocatorFallbackWarning:
    @pytest.mark.parametrize("tid", TENANTS)
    def test_config_applies_no_warning(self, tid):
        import intelligence.strategy_allocator as sa

        with tenant(tid), patch.object(sa, "log") as spy:
            d = sa.StrategyAllocator().allocate(regime_result=_regime(), coin={"symbol": "CS7X/USDT"})
        assert d.strategy_weights == {"grid": 0.4, "momentum": 0.6}
        assert _warnings(spy) == []

    def test_botconfig_defaults_shadow_code_fallback(self):
        """Fact pin: BotConfig.strategy_allocator_config always carries both keys (core/config.py:600-611, 0.4/0.6),
        so the 0.6/0.4 code fallback is only reachable when that property no longer supplies them."""
        from core.config import BotConfig

        c = BotConfig(raw={}).strategy_allocator_config
        assert (c["default_grid_weight"], c["default_momentum_weight"]) == (0.4, 0.6)

    @pytest.mark.parametrize("tid", TENANTS)
    def test_code_fallback_logs_warning_once_per_tenant(self, tid):
        import intelligence.strategy_allocator as sa
        from core.config import BotConfig

        with tenant(tid), patch.object(BotConfig, "strategy_allocator_config", property(lambda self: {"enabled": True})), \
                patch.object(sa, "log") as spy:
            a = sa.StrategyAllocator()
            d1 = a.allocate(regime_result=_regime(), coin={"symbol": "CS7X/USDT"})
            d2 = a.allocate(regime_result=_regime(), coin={"symbol": "CS7Y/USDT"})
        assert d1.strategy_weights == d2.strategy_weights == {"grid": 0.6, "momentum": 0.4}  # fallback unchanged
        w = _warnings(spy)
        assert len(w) == 1, w
        assert f"tenant={tid}" in w[0] and "default_grid_weight=0.6" in w[0] and "default_momentum_weight=0.4" in w[0]

    def test_warning_is_per_tenant(self):
        import intelligence.strategy_allocator as sa
        from core.config import BotConfig

        with patch.object(BotConfig, "strategy_allocator_config", property(lambda self: {"enabled": True})), \
                patch.object(sa, "log") as spy:
            for t in TENANTS:
                with tenant(t):
                    sa.StrategyAllocator().allocate(regime_result=_regime(), coin={"symbol": "CS7X/USDT"})
        w = _warnings(spy)
        assert len(w) == 2 and "tenant=default" in w[0] and "tenant=henry" in w[1], w
