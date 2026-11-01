"""risk.fail_closed_guards missing or malformed must fail closed ("deny").

Before this change a config without the key (e.g. a live config that never got
it) resolved to "log": a crashing risk guard was logged and the buy continued.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from risk.risk_manager import _fail_closed_guards_mode, guard_failed
from tests.unit.test_fail_closed_guards import (
    A_RISK_GUARDS,
    _buy_eval_env,
    _buy_order,
    _raise_patches,
    _risk_cfg,
    _rm,
)


@pytest.mark.parametrize(
    "config",
    [
        None,
        {},
        {"risk": {}},
        {"risk": None},
        {"risk": {"fail_closed_guards": None}},
        {"risk": {"fail_closed_guards": ""}},
        {"risk": {"fail_closed_guards": "   "}},
        {"risk": {"fail_closed_guards": "warn"}},
        {"risk": {"fail_closed_guards": 0}},
        {"risk": {"fail_closed_guards": ["log"]}},
        SimpleNamespace(),
        SimpleNamespace(risk_config=None, raw=None),
        SimpleNamespace(risk_config={}),
    ],
)
def test_missing_or_malformed_key_is_deny(config):
    assert _fail_closed_guards_mode(config) == "deny"


@pytest.mark.parametrize("value", ["log", "LOG", " Log "])
def test_explicit_log_still_opts_out(value):
    assert _fail_closed_guards_mode({"risk": {"fail_closed_guards": value}}) == "log"
    assert _fail_closed_guards_mode(_risk_cfg(value)) == "log"


def test_explicit_deny_stays_deny():
    assert _fail_closed_guards_mode({"risk": {"fail_closed_guards": "deny"}}) == "deny"
    assert _fail_closed_guards_mode(_risk_cfg("deny")) == "deny"


def test_error_while_reading_config_is_deny():
    class Exploding:
        @property
        def risk_config(self):
            raise RuntimeError("config read failed")

    assert _fail_closed_guards_mode(Exploding()) == "deny"


def test_guard_failed_without_key_returns_deny_decision():
    with patch("logger.log"):
        dec = guard_failed("stablecoin_blocked", RuntimeError("x"), _buy_order(), config=_risk_cfg(None))
    assert dec is not None
    assert dec.approved is False
    assert dec.code == "stablecoin_blocked_error"
    assert dec.size_multiplier == 0.0


@pytest.mark.parametrize("guard", A_RISK_GUARDS)
def test_risk_evaluate_without_key_rejects_on_guard_error(guard):
    rm = _rm(None)
    order = _buy_order()
    with patch("logger.log"), _buy_eval_env(rm, extra=_raise_patches(guard, rm)):
        dec = rm.evaluate(order, timeframe="4h", source="grid")
    assert dec.approved is False
    assert dec.code == f"{guard}_error"
    assert dec.size_multiplier == 0.0
