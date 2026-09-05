"""Fail-open Tier 1b — market-bias `degraded` / `measured` (#299).

Existing fusion keys are frozen: extra fields (`degraded`, `layers`) are additive.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.config import BotConfig
from core.models import TradeOrder
from data_manager import get_config
from risk.moderate_deploy import size_boost_for_regime
from risk.risk_manager import RiskManager
from services.market_oracle.store import reset_for_tests as reset_ora
from services.market_oracle.store import store_snapshot as store_ora
from services.market_policy_fusion import get_global_market_bias
from services.santiment.sidecar.regime import decide_regime
from services.santiment.sidecar.snapshot import build_snapshot
from services.santiment.store import reset_for_tests as reset_san
from services.santiment.store import store_snapshot as store_san
from strategies.dca_policy import DcaContext, dca_policy_config, evaluate_dca_policy
from strategies.oracle_climax import MODE_GRIND, MODE_IDLE, resolve_climax_decision


# Keys `get_global_market_bias()` returned before Tier 1b. Values and semantics
# must not change; `degraded` / `layers` are additive.
_FUSION_STABLE_KEYS = (
    "active",
    "source",
    "sources",
    "regime",
    "sentiment",
    "size_mult",
    "sensor_policy",
    "block_buys",
    "apply_size_mult",
    "apply_sensor_policy",
    "apply_mode_bias",
    "apply_grid_spacing",
    "grid_spacing_mult",
    "rationale",
    "as_of",
    "fresh",
    "warmup_active",
)

def _now_as_of() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _arch_cfg(**extra):
    arch = {
        "santiment_risk_enabled": True,
        "market_oracle_risk_enabled": True,
        "market_oracle_warmup_sec": 0,
        "santiment_apply_size_mult": True,
        "market_oracle_apply_size_mult": True,
        "santiment_apply_sensor_policy": True,
        "market_oracle_apply_sensor_policy": True,
    }
    arch.update(extra)
    return {"architecture": arch}


def _fresh_layer_snaps(*, san_measured=None, ora_measured=None):
    as_of = _now_as_of()
    ora = {
        "source": "market_oracle",
        "state": "NEUTRAL",
        "regime": "NEUTRAL",
        "size_mult": 0.85,
        "sensor_policy": "active",
        "ttl_sec": 900,
        "as_of": as_of,
        "rationale": "test-ora",
    }
    san = {
        "source": "santiment",
        "regime": "NEUTRAL",
        "size_mult": 0.85,
        "sensor_policy": "active",
        "ttl_sec": 1800,
        "as_of": as_of,
        "rationale": "test-san",
    }
    if san_measured is not None:
        san["measured"] = san_measured
    if ora_measured is not None:
        ora["measured"] = ora_measured
    return san, ora


def _store_both(*, san_measured=None, ora_measured=None):
    reset_san()
    reset_ora()
    san, ora = _fresh_layer_snaps(san_measured=san_measured, ora_measured=ora_measured)
    store_san(san)
    store_ora(ora)


def _reset_fusion_episode():
    import services.market_policy_fusion as fusion

    if hasattr(fusion, "reset_degraded_episode_for_tests"):
        fusion.reset_degraded_episode_for_tests()
    elif hasattr(fusion, "_DEGRADED_EPISODE"):
        fusion._DEGRADED_EPISODE = False


def _reset_obs_degraded():
    import services.market_context_observability as obs

    for name in (
        "_last_degraded",
        "_last_degraded_notified",
        "_degraded_last",
    ):
        if hasattr(obs, name):
            setattr(obs, name, None)
    for name in ("_last_degraded_notify_ts", "_degraded_notify_ts"):
        if hasattr(obs, name):
            setattr(obs, name, 0.0)


def _stable(bias: dict) -> dict:
    return {k: bias[k] for k in _FUSION_STABLE_KEYS}


def _warning_messages(mock_log) -> list[str]:
    out = []
    for args, kwargs in mock_log.call_args_list:
        level = kwargs.get("level")
        if level is None and len(args) >= 2:
            level = args[1]
        if str(level).upper() == "WARNING":
            out.append(str(args[0] if args else ""))
    return out


def _risk_cfg(mode="log", **risk_over) -> BotConfig:
    raw = dict(get_config())
    raw["trading_mode"] = "paper"
    raw["max_open_positions"] = 50
    raw["max_position_percent"] = 100
    raw["max_usdt_per_trade"] = 500
    risk = dict(raw.get("risk") or {})
    risk["fail_closed_guards"] = mode
    risk["venue_quality"] = {"enabled": False}
    risk["cash_floor_pct"] = 0
    risk["cash_policy"] = {"enabled": False}
    risk["position_capacity"] = {"enabled": False}
    risk["slot_eviction"] = {"enabled": False}
    risk["min_trade_usdt"] = 1
    risk.update(risk_over)
    raw["risk"] = risk
    return BotConfig(raw)


def _buy_order(**kw) -> TradeOrder:
    fields = dict(
        type="BUY",
        symbol="NEAR/USDT",
        price=1.0,
        amount=0,
        usdt_amount=200.0,
        signal="BUY",
        source="grid",
        timestamp="2026-01-01T00:00:00",
    )
    fields.update(kw)
    return TradeOrder(**fields)


@contextmanager
def _buy_eval_env(rm: RiskManager, extra=()):
    cap = SimpleNamespace(
        max_open_eff=100,
        enabled=False,
        rationale="",
        factors={},
        free_slots=100,
        regime=None,
    )
    with ExitStack() as stack:
        stack.enter_context(patch("risk.risk_manager.get_position", return_value={"amount": 0}))
        stack.enter_context(
            patch("risk.risk_manager.find_open_position_for_symbol", return_value=None)
        )
        stack.enter_context(patch("risk.risk_manager.count_open_full_slots", return_value=0))
        stack.enter_context(patch("risk.risk_manager.count_open_positions", return_value=0))
        stack.enter_context(patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")))
        stack.enter_context(patch.object(rm, "_cash_floor_blocked", return_value=None))
        stack.enter_context(patch.object(rm, "_daily_buy_limit_blocked", return_value=None))
        stack.enter_context(
            patch.object(rm, "_dynamic_size", return_value=(200.0, {"total_multiplier": 1.0}))
        )
        stack.enter_context(patch.object(rm, "_portfolio_equity", return_value=100_000.0))
        stack.enter_context(patch.object(rm, "_spendable_usdt", return_value=50_000.0))
        stack.enter_context(patch.object(rm, "_available_usdt", return_value=50_000.0))
        stack.enter_context(patch.object(rm, "_resolve_position_capacity", return_value=cap))
        stack.enter_context(
            patch(
                "services.correlated_tier.api.correlated_tier_selloff_active",
                return_value=False,
            )
        )
        stack.enter_context(
            patch(
                "services.gainer_universe.chase_guard.check_gainer_chase_guard",
                return_value=(False, ""),
            )
        )
        stack.enter_context(patch("intelligence.memory.cache.get_entry_bias", return_value="neutral"))
        stack.enter_context(patch.object(rm, "_sensor_reentry_cooloff_blocked", return_value=None))
        stack.enter_context(patch("intelligence.macro.snapshot.get_risk_multipliers", return_value={}))
        stack.enter_context(patch("services.universe.split.universe_split_enabled", return_value=False))
        stack.enter_context(patch("services.universe.split.is_trade_eligible", return_value=True))
        stack.enter_context(patch("core.stablecoins.is_stablecoin_symbol", return_value=False))
        stack.enter_context(patch("core.stablecoins.stablecoin_buys_blocked", return_value=True))
        stack.enter_context(patch("services.watchlist_quality.config.wqe_mode", return_value="off"))
        stack.enter_context(
            patch("services.venue_quality.venue_quality_config", return_value={"enabled": False})
        )
        for p in extra:
            stack.enter_context(p)
        yield


@pytest.fixture(autouse=True)
def _clean_stores():
    reset_san()
    reset_ora()
    _reset_fusion_episode()
    _reset_obs_degraded()
    yield
    reset_san()
    reset_ora()
    _reset_fusion_episode()
    _reset_obs_degraded()


class TestFusionSnapshotExistingKeys:
    """§5 #3 — both layers fresh+measured: existing keys keep value and semantics."""

    def test_fresh_measured_existing_keys_unchanged(self):
        san, ora = _fresh_layer_snaps()  # legacy snaps: no `measured` field → measured=True
        reset_san()
        reset_ora()
        store_san(san)
        store_ora(ora)
        bias = get_global_market_bias(_arch_cfg())
        got = _stable(bias)
        assert got["active"] is True
        assert got["source"] == "santiment+oracle"
        assert got["sources"] == ["santiment", "oracle"]
        assert got["regime"] == "NEUTRAL"
        assert got["sentiment"] == 0.0
        assert got["size_mult"] == pytest.approx(0.85)
        assert got["sensor_policy"] == "active"
        assert got["block_buys"] is False
        assert got["apply_size_mult"] is True
        assert got["apply_sensor_policy"] is True
        assert got["apply_mode_bias"] is True
        assert got["apply_grid_spacing"] is True
        assert got["grid_spacing_mult"] == pytest.approx(1.0)
        assert got["as_of"] == san["as_of"]
        assert got["fresh"] is True
        assert got["warmup_active"] is False
        assert "test-san" in got["rationale"]
        assert "test-ora" in got["rationale"]
        # Additive fields must not flip the healthy path.
        if "degraded" in bias:
            assert bias["degraded"] is False
        if "layers" in bias:
            assert bias["layers"]["santiment"]["active"] is True
            assert bias["layers"]["santiment"]["fresh"] is True
            assert bias["layers"]["santiment"]["measured"] is True
            assert bias["layers"]["oracle"]["active"] is True
            assert bias["layers"]["oracle"]["fresh"] is True
            assert bias["layers"]["oracle"]["measured"] is True


class TestSidecarMeasured:
    """§5 #1 — no Santiment features → measured=False → fusion degraded, still active."""

    def test_no_features_measured_false_fusion_degraded_still_active(self):
        d = decide_regime({})
        assert d.measured is False
        snap = build_snapshot({}, decision=d)
        assert snap.get("measured") is False
        store_san(snap)
        bias = get_global_market_bias(_arch_cfg())
        assert bias["active"] is True
        assert bias["degraded"] is True
        assert bias["layers"]["santiment"]["active"] is True
        assert bias["layers"]["santiment"]["measured"] is False


class TestFusionDegradedInactive:
    """§5 #2 — both layers inactive → degraded=True."""

    def test_both_layers_inactive_degraded(self):
        with patch("services.santiment.policy.get_latest_snapshot", return_value=None), patch(
            "services.market_oracle.policy.get_latest_snapshot", return_value=None
        ):
            bias = get_global_market_bias(_arch_cfg())
        assert bias["active"] is False
        assert bias["degraded"] is True
        assert bias["size_mult"] == pytest.approx(1.0)
        assert bias["fresh"] is False
        assert bias["layers"]["santiment"]["active"] is False
        assert bias["layers"]["oracle"]["active"] is False


class TestDynamicSizeDenyDegraded:
    """§5 #4 — deny + degraded: size ≤ no-bias, no 35% moderate-deploy boost."""

    def _size(self, rm, bias):
        order = _buy_order()
        with patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value=bias,
        ), patch("intelligence.memory.cache.get_size_bias", return_value=1.0), patch(
            "intelligence.memory.cache.get_coin_profile", return_value=None
        ), patch(
            "intelligence.macro.snapshot.get_risk_multipliers", return_value={}
        ), patch.object(rm, "_equity_drawdown_pct", return_value=0.0), patch.object(
            rm, "_available_usdt", return_value=10_000.0
        ), patch.object(rm, "_portfolio_equity", return_value=100_000.0):
            return rm._dynamic_size(
                1000.0, order, "4h", "grid", 70.0, 50.0, {"atr_pct": 3.0}
            )

    def test_deny_degraded_no_size_boost(self):
        md = {
            "enabled": True,
            "size_boost_default": 1.35,
            "size_boost_neutral": 1.5,
            "size_boost_risk_off": 1.0,
            "max_total_multiplier": 2.0,
            "max_boost": 1.75,
            "cash_rich_pct": 90,
        }
        assert size_boost_for_regime({"risk": {"moderate_deploy": md}}, "UNKNOWN") == pytest.approx(
            1.0
        )
        rm_deny = RiskManager(_risk_cfg("deny", moderate_deploy=md, min_size_multiplier=0.25))
        degraded = {
            "active": False,
            "degraded": True,
            "apply_size_mult": False,
            "size_mult": 1.0,
            "regime": None,
            "block_buys": False,
            "source": None,
        }
        sized_deg, fac_deg = self._size(rm_deny, degraded)
        rm_none = RiskManager(
            _risk_cfg("deny", moderate_deploy={"enabled": False}, min_size_multiplier=0.25)
        )
        sized_plain, _ = self._size(
            rm_none,
            {
                "active": False,
                "degraded": False,
                "apply_size_mult": False,
                "size_mult": 1.0,
                "regime": None,
            },
        )
        assert fac_deg["moderate_deploy_mult"] == pytest.approx(1.0)
        assert fac_deg["global_regime"] == "UNKNOWN"
        assert sized_deg <= sized_plain + 1e-9

    def test_log_degraded_keeps_default_boost_and_warns_once(self):
        md = {
            "enabled": True,
            "size_boost_default": 1.35,
            "size_boost_neutral": 1.5,
            "max_total_multiplier": 2.0,
            "max_boost": 1.75,
            "cash_rich_pct": 90,
        }
        rm = RiskManager(_risk_cfg("log", moderate_deploy=md, min_size_multiplier=0.25))
        bias = {
            "active": False,
            "degraded": True,
            "apply_size_mult": False,
            "size_mult": 1.0,
            "regime": None,
        }
        sized, fac = self._size(rm, bias)
        assert fac["moderate_deploy_mult"] == pytest.approx(1.35)
        assert sized > 1000.0

        _reset_fusion_episode()
        with patch("services.santiment.policy.get_latest_snapshot", return_value=None), patch(
            "services.market_oracle.policy.get_latest_snapshot", return_value=None
        ), patch("logger.log") as mock_log:
            get_global_market_bias(_arch_cfg())
            get_global_market_bias(_arch_cfg())
        warns = [m for m in _warning_messages(mock_log) if "degraded" in m.lower()]
        assert len(warns) == 1


class TestMarketBlockDenyDegraded:
    """§5 #5 — deny + degraded: new BUY rejected; DCA and SELL approved."""

    _DEG = {
        "active": False,
        "degraded": True,
        "block_buys": False,
        "apply_size_mult": False,
        "size_mult": 1.0,
        "regime": None,
        "rationale": "no global bias active",
        "source": None,
    }

    def _fusion(self):
        return patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value=self._DEG,
        )

    def test_deny_new_buy_rejected_dca_and_sell_ok(self):
        rm = RiskManager(_risk_cfg("deny"))
        with _buy_eval_env(rm, extra=(self._fusion(),)):
            dec = rm.evaluate(_buy_order(), timeframe="4h", source="grid")
        assert dec.approved is False
        assert dec.code == "market_bias_degraded"

        rm_dca = RiskManager(_risk_cfg("deny"))
        with _buy_eval_env(rm_dca, extra=(self._fusion(),)):
            dca = rm_dca.evaluate(
                _buy_order(signal="BUY_DCA", source="dca"),
                timeframe="4h",
                source="dca",
            )
        assert dca.approved is True

        rm_sell = RiskManager(_risk_cfg("deny"))
        sell = TradeOrder(
            type="SELL",
            symbol="NEAR/USDT",
            price=1.0,
            amount=10.0,
            signal="SELL_FULL",
            source="auto",
        )
        with patch("risk.risk_manager.get_position", return_value={"amount": 10.0}), patch(
            "strategies.position_lock.auto_sell_blocked", return_value=(False, "")
        ), patch.object(rm_sell, "_partial_sell_blocked", return_value=(False, "")), patch.object(
            rm_sell, "_daily_sells_count", return_value=0
        ), patch.object(rm_sell, "_effective_max_daily_sells", return_value=0), self._fusion():
            sdec = rm_sell.evaluate(sell, timeframe="4h", source="auto")
        assert sdec.approved is True

    def test_log_new_buy_still_approved(self):
        rm = RiskManager(_risk_cfg("log"))
        fusion = patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value=self._DEG,
        )
        with _buy_eval_env(rm, extra=(fusion,)):
            dec = rm.evaluate(_buy_order(), timeframe="4h", source="grid")
        assert dec.approved is True


class TestDcaPolicyDegraded:
    """§5 #6 — deny + degraded: no deploy_mult."""

    def test_deny_degraded_no_deploy_mult(self):
        cfg = dca_policy_config({"policy": {"enabled": True, "deploy_mult": 1.35}})
        r = evaluate_dca_policy(
            DcaContext(
                fusion_size_mult=1.0,
                fusion_measured=False,
                fusion_fresh=True,
                fusion_degraded=True,
                fail_closed_guards="deny",
            ),
            cfg,
        )
        assert "deploy_boost" not in r.reason_codes
        assert r.size_mult == pytest.approx(1.0)

    def test_log_degraded_still_deploys(self):
        cfg = dca_policy_config({"policy": {"enabled": True, "deploy_mult": 1.35}})
        r = evaluate_dca_policy(
            DcaContext(
                fusion_size_mult=1.0,
                fusion_measured=False,
                fusion_fresh=True,
                fusion_degraded=True,
                fail_closed_guards="log",
            ),
            cfg,
        )
        assert "deploy_boost" in r.reason_codes
        assert r.size_mult == pytest.approx(1.35)


class TestOracleClimaxFreshness:
    """§5 #7 — stale snapshot → IDLE; fresh RISK_ON → GRIND."""

    def _raw(self, mode="deny"):
        return {
            "risk": {"fail_closed_guards": mode},
            "sell_policy": {"oracle_climax": {"enabled": True}},
            "architecture": {
                "santiment_risk_enabled": False,
                "market_oracle_risk_enabled": True,
                "market_oracle_warmup_sec": 0,
            },
        }

    def _ora_snap(self, *, as_of: str, state="RISK_ON"):
        return {
            "source": "market_oracle",
            "state": state,
            "regime": state,
            "size_mult": 1.0,
            "sensor_policy": "active",
            "ttl_sec": 900,
            "as_of": as_of,
            "features": {
                "btc_ret_24h_pct": 3.5,
                "eth_ret_24h_pct": 4.0,
                "breadth_pct_green": 0.55,
                "btc_ret_4h_pct": 1.2,
                "btc_trend_4h": 1.0,
                "btc_ret_1h_pct": 0.8,
            },
        }

    def test_stale_idle_fresh_grind(self):
        now = datetime.now(timezone.utc)
        stale = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        fresh = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        store_ora(self._ora_snap(as_of=stale))
        idle = resolve_climax_decision(self._raw("deny"))
        assert idle.mode == MODE_IDLE
        store_ora(self._ora_snap(as_of=fresh))
        grind = resolve_climax_decision(self._raw("deny"))
        assert grind.mode == MODE_GRIND


class TestNotifyOperatorOncePerTransition:
    """§5 #8 — notify_operator once per False→True and True→False transition."""

    def test_notify_once_per_transition(self):
        from services.market_context_observability import maybe_notify_degraded

        healthy = {
            "degraded": False,
            "active": True,
            "regime": "NEUTRAL",
            "layers": {},
        }
        down = {
            "degraded": True,
            "active": False,
            "regime": None,
            "layers": {
                "santiment": {"active": False, "fresh": False, "measured": False, "as_of": None},
                "oracle": {"active": False, "fresh": False, "measured": False, "as_of": None},
            },
        }
        with patch(
            "services.market_context_observability.min_degraded_notify_interval_sec",
            return_value=0,
        ), patch("core.operator_notify.notify_operator", return_value=True) as notify:
            assert maybe_notify_degraded(healthy) is False
            assert notify.call_count == 0
            assert maybe_notify_degraded(down) is True
            assert notify.call_count == 1
            assert maybe_notify_degraded(down) is False
            assert notify.call_count == 1
            assert maybe_notify_degraded(healthy) is True
            assert notify.call_count == 2
            assert maybe_notify_degraded(healthy) is False
            assert notify.call_count == 2
