"""Table-driven resolve_execution_mode matrix (#312)."""

from __future__ import annotations

import itertools
import warnings

import pytest

from core.config import BotConfig
from core.execution_mode import ExecutionMode, resolve_execution_mode

_TRADING = ("demo", "paper", "live")
_EXECUTION = ("shadow", "testnet", "real", None, "bogus")
_DRY_RUN = (True, False, None)
_DEMO = ("1", None)
_CONFIRMED = (True, False)
_CREDS = (True, False)

_CASES = list(itertools.product(_TRADING, _EXECUTION, _DRY_RUN, _DEMO, _CONFIRMED, _CREDS))


def _case_id(trading_mode, execution, dry_run, demo_mode, live_confirmed, creds) -> str:
    ex = "missing" if execution is None else execution
    if dry_run is True:
        dr = "true"
    elif dry_run is False:
        dr = "false"
    else:
        dr = "missing"
    dm = "1" if demo_mode == "1" else "unset"
    return (
        f"{trading_mode}|ex={ex}|dry={dr}|DEMO={dm}"
        f"|conf={int(live_confirmed)}|creds={int(creds)}"
    )


def _expected(trading_mode, execution, dry_run, demo_mode, live_confirmed, creds):
    """Independent encoding of the #312 resolution spec (not imported from production)."""
    if trading_mode in ("demo", "paper"):
        return "shadow"
    requested = "shadow" if execution is None else execution
    if requested not in ("shadow", "testnet", "real"):
        return "error"
    if dry_run is True:
        return "shadow"
    if requested == "shadow":
        return "shadow"
    if requested == "testnet":
        return "testnet" if creds else "error"
    # real — never silently downgrade
    if (not live_confirmed) or (not creds) or demo_mode == "1":
        return "error"
    return "real"


def _config(trading_mode, execution, dry_run, live_confirmed) -> dict:
    live: dict = {
        "api_key_env": "GATE_API_KEY",
        "api_secret_env": "GATE_API_SECRET",
    }
    if execution is not None:
        live["execution"] = execution
    if dry_run is not None:
        live["dry_run"] = dry_run
    return {
        "trading_mode": trading_mode,
        "live_confirmed": live_confirmed,
        "live": live,
    }


def _env(demo_mode, creds) -> dict:
    env: dict[str, str] = {}
    if demo_mode is not None:
        env["DEMO_MODE"] = demo_mode
    if creds:
        env["GATE_API_KEY"] = "test-key"
        env["GATE_API_SECRET"] = "test-secret"
    return env


@pytest.mark.parametrize(
    "trading_mode,execution,dry_run,demo_mode,live_confirmed,creds",
    _CASES,
    ids=[_case_id(*c) for c in _CASES],
)
def test_resolve_execution_mode_matrix(
    trading_mode, execution, dry_run, demo_mode, live_confirmed, creds
):
    cfg = _config(trading_mode, execution, dry_run, live_confirmed)
    env = _env(demo_mode, creds)
    want = _expected(trading_mode, execution, dry_run, demo_mode, live_confirmed, creds)
    if want == "error":
        with pytest.raises(RuntimeError) as exc:
            resolve_execution_mode(cfg, env)
        assert str(exc.value)
        return
    got = resolve_execution_mode(cfg, env)
    assert isinstance(got, ExecutionMode)
    assert got.adapter_mode == want
    assert got.places_real_orders is (want == "real")
    assert got.reason


def test_real_error_names_each_failed_guard():
    cfg = _config("live", "real", False, live_confirmed=False)
    with pytest.raises(RuntimeError) as exc:
        resolve_execution_mode(cfg, {"DEMO_MODE": "1"})
    msg = str(exc.value)
    assert "live_confirmed" in msg
    assert "GATE_API_KEY" in msg or "credential" in msg.lower() or "secret" in msg.lower()
    assert "DEMO_MODE" in msg


def test_unknown_execution_raises_even_when_dry_run_true():
    cfg = _config("live", "bogus", True, live_confirmed=True)
    with pytest.raises(RuntimeError, match="bogus"):
        resolve_execution_mode(cfg, {"GATE_API_KEY": "k", "GATE_API_SECRET": "s"})


def test_dry_run_true_forces_shadow_and_warns_once(monkeypatch):
    import core.execution_mode as em

    monkeypatch.setattr(em, "_DRY_RUN_WARNED", False)
    cfg = _config("live", "real", True, live_confirmed=True)
    env = {"GATE_API_KEY": "k", "GATE_API_SECRET": "s"}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        first = resolve_execution_mode(cfg, env)
        second = resolve_execution_mode(cfg, env)
    assert first.adapter_mode == "shadow"
    assert second.adapter_mode == "shadow"
    assert first.places_real_orders is False
    assert "dry_run" in first.reason
    dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(dep) == 1


def test_demo_mode_live_dry_run_false_real_raises():
    """Review finding: DEMO_MODE=1 + live + dry_run=false must not reach real."""
    cfg = _config("live", "real", False, live_confirmed=True)
    env = {"DEMO_MODE": "1", "GATE_API_KEY": "k", "GATE_API_SECRET": "s"}
    with pytest.raises(RuntimeError, match="DEMO_MODE"):
        resolve_execution_mode(cfg, env)


def test_bot_config_live_execution_defaults_shadow():
    cfg = BotConfig({"live": {}})
    assert cfg.live_execution == "shadow"


def test_bot_config_live_execution_reads_key():
    cfg = BotConfig({"live": {"execution": "TestNet"}})
    assert cfg.live_execution == "testnet"
