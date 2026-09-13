"""#391: promotion tick must not apply while hermes.enabled is false."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch

import pytest

T0 = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
SKIP_MSG = "Hermes promotion tick skipped because hermes.enabled=false"


@pytest.fixture(autouse=True)
def _reset_disabled_warning():
    from hermes.promotion import _reset_disabled_warning as reset

    reset()
    yield
    reset()


@pytest.fixture
def promo_env(hermes_memory_tmp, monkeypatch):
    from core.config import BotConfig
    from hermes.memory import store

    raw = copy.deepcopy(BotConfig().raw)
    raw["hermes"]["enabled"] = False
    raw["hermes"]["live_evidence"]["enabled"] = False
    raw["hermes"]["sync_to_config"] = True
    raw["hermes"].setdefault("promotion", {})
    raw["hermes"]["promotion"]["veto_window_min"] = 10
    raw["hermes"]["promotion"]["max_promotions_per_day"] = 1
    raw["hermes"]["promotion"]["post_apply_validation_hours"] = 24
    raw["hermes"]["promotion"]["post_apply_min_trades"] = 5
    cfg = BotConfig(raw)
    monkeypatch.setattr("core.config.get_bot_config", lambda *a, **k: cfg)
    monkeypatch.setattr("data_manager.get_config", lambda *a, **k: raw)
    monkeypatch.setattr("data_manager.reload_config", lambda *a, **k: raw)
    store.init_baseline_from_config(cfg, "ARIA/USDT", "4h")
    return cfg, raw, hermes_memory_tmp


def _pending_record(**kwargs):
    rec = {
        "id": kwargs.get("id", "exp_enabled_gate"),
        "variable": "rsi_buy_low",
        "old_value": 30,
        "new_value": 28,
        "symbol": "ARIA/USDT",
        "timeframe": "4h",
        "params": {"rsi_buy_low": 28, "rsi_sell_30": 70},
        "baseline_params": {"rsi_buy_low": 30, "rsi_sell_30": 70},
        "variant_metrics": {
            "sharpe": 1.1,
            "win_rate": 60,
            "trades": 40,
            "trade_quality": 0.8,
        },
        "win_probability": 0.97,
        "total_trades": 41,
        "threshold_used": 0.95,
        "verdict": "pending",
    }
    rec.update(kwargs)
    return rec


def _agent(cfg):
    agent = MagicMock()
    agent.config = cfg
    agent.hermes = cfg.hermes_config
    agent._is_observe_mode.return_value = False
    return agent


def _queue_due_pending(agent):
    from hermes import promotion
    from hermes.memory import store

    rec = store.append_experiment(_pending_record())
    queued = promotion.queue_or_suppress(agent, rec, observe=False, now=T0)
    assert queued["status"] == "pending"
    return queued


def test_tick_does_not_apply_pending_when_hermes_disabled(promo_env):
    """Would have applied under the old unconditional tick() path."""
    from hermes import promotion
    from hermes.memory import store

    cfg, _raw, _tmp = promo_env
    agent = _agent(cfg)
    _queue_due_pending(agent)
    before = copy.deepcopy(store.load_promotion_state())
    pending_before = [p for p in before.get("pending") or [] if p.get("experiment_id") == "exp_enabled_gate"]
    assert len(pending_before) == 1
    assert pending_before[0]["status"] == "pending"

    with (
        patch.object(store, "save_baseline") as save_bl,
        patch.object(store, "save_profile") as save_pr,
        patch.object(store, "save_promotion_state") as save_state,
        patch("strategies.registry.sync_hermes_baseline_to_config") as sync,
    ):
        agent._sync_to_config.side_effect = lambda baseline, exp_id: sync(baseline, exp_id)
        out = promotion.tick(agent, now=T0 + timedelta(minutes=11))

    assert out == {"applied": [], "reverted": []}
    save_bl.assert_not_called()
    save_pr.assert_not_called()
    sync.assert_not_called()
    save_state.assert_not_called()

    after = store.load_promotion_state()
    pending_after = [p for p in after.get("pending") or [] if p.get("experiment_id") == "exp_enabled_gate"]
    assert len(pending_after) == 1
    assert pending_after[0]["status"] == "pending"
    assert pending_after[0] == pending_before[0]
    assert after.get("applied") == before.get("applied")


def test_tick_applies_after_enabled_flipped_true(promo_env):
    from hermes import promotion
    from hermes.memory import store

    cfg, raw, tmp = promo_env
    agent = _agent(cfg)
    _queue_due_pending(agent)

    skipped = promotion.tick(agent, now=T0 + timedelta(minutes=11))
    assert skipped == {"applied": [], "reverted": []}
    pending = [p for p in store.load_promotion_state().get("pending") or [] if p.get("status") == "pending"]
    assert [p["experiment_id"] for p in pending] == ["exp_enabled_gate"]

    raw["hermes"]["enabled"] = True
    assert cfg.hermes_enabled is True

    out = promotion.tick(agent, now=T0 + timedelta(minutes=11))
    assert len(out["applied"]) == 1
    assert out["applied"][0]["experiment_id"] == "exp_enabled_gate"
    assert store.load_baseline()["params"]["rsi_buy_low"] == 28
    agent._sync_to_config.assert_called()
    snap = tmp / "snapshots" / "exp_enabled_gate.json"
    assert snap.exists()


def test_disabled_skip_logs_once_across_two_ticks(promo_env):
    from hermes import promotion

    cfg, _raw, _tmp = promo_env
    agent = _agent(cfg)
    _queue_due_pending(agent)
    due = T0 + timedelta(minutes=11)

    with patch("hermes.promotion.log") as log_fn:
        first = promotion.tick(agent, now=due)
        second = promotion.tick(agent, now=due)

    assert first == {"applied": [], "reverted": []}
    assert second == {"applied": [], "reverted": []}
    skip_calls = [c for c in log_fn.call_args_list if c == call(SKIP_MSG, "INFO")]
    assert skip_calls == [call(SKIP_MSG, "INFO")]


def test_tick_fails_closed_when_enabled_lookup_raises(promo_env, monkeypatch):
    """If the enabled-flag lookup itself blows up, treat Hermes as disabled."""
    from hermes import promotion
    from hermes.memory import store

    cfg, raw, _tmp = promo_env
    agent = _agent(cfg)
    _queue_due_pending(agent)
    raw["hermes"]["enabled"] = True  # would apply if the lookup succeeded

    def _boom(*a, **k):
        raise RuntimeError("config unavailable")

    # Non-BotConfig agent.config forces the get_bot_config() fallback path.
    agent.config = object()
    monkeypatch.setattr("core.config.get_bot_config", _boom)

    with (
        patch.object(store, "save_baseline") as save_bl,
        patch("strategies.registry.sync_hermes_baseline_to_config") as sync,
    ):
        out = promotion.tick(agent, now=T0 + timedelta(minutes=11))

    assert out == {"applied": [], "reverted": []}
    save_bl.assert_not_called()
    sync.assert_not_called()
    pending = [p for p in store.load_promotion_state().get("pending") or [] if p.get("status") == "pending"]
    assert [p["experiment_id"] for p in pending] == ["exp_enabled_gate"]


def test_tick_hermes_promotions_does_not_construct_agent_when_disabled(promo_env):
    from hermes import promotion
    from hermes.memory import store

    cfg, _raw, _tmp = promo_env
    agent = _agent(cfg)
    _queue_due_pending(agent)

    with patch("hermes.agent.HermesAgent") as agent_cls:
        out = promotion.tick_hermes_promotions(now=T0 + timedelta(minutes=11))

    assert out == {"applied": [], "reverted": []}
    agent_cls.assert_not_called()
    pending = [
        p
        for p in store.load_promotion_state().get("pending") or []
        if p.get("experiment_id") == "exp_enabled_gate"
    ]
    assert len(pending) == 1
    assert pending[0]["status"] == "pending"
