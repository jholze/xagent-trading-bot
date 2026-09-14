"""#410: risk_manager._execution_places_real_orders delegates to the shared helper.

Contract: the risk manager must not parse ``live.execution`` / ``live.dry_run``
itself. It follows ``core.execution_mode.places_real_orders`` — only a resolved
``real`` is real money; shadow and testnet are not, even with ``dry_run: false``;
an unresolved mode is treated as real (fail-closed, short gate stays on).
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from core.execution_mode import places_real_orders
from risk import risk_manager as rm_mod
from risk.risk_manager import _execution_places_real_orders

_GATE_CREDS = {"GATE_API_KEY": "test-key", "GATE_API_SECRET": "test-secret"}


def _live_cfg(execution: str, *, dry_run: bool = False, confirmed: bool = True) -> dict:
    return {
        "trading_mode": "live",
        "live_confirmed": confirmed,
        "live": {
            "execution": execution,
            "dry_run": dry_run,
            "api_key_env": "GATE_API_KEY",
            "api_secret_env": "GATE_API_SECRET",
        },
    }


class TestRiskExecutionPlacesRealOrders(unittest.TestCase):
    def test_delegates_to_shared_helper(self):
        cfg = _live_cfg("testnet")
        with patch("core.execution_mode.places_real_orders", return_value="sentinel") as helper:
            self.assertEqual(_execution_places_real_orders(cfg), "sentinel")
        helper.assert_called_once_with(cfg)

    def test_matches_shared_helper_across_modes(self):
        with patch.dict(os.environ, _GATE_CREDS, clear=False):
            for execution in ("shadow", "testnet", "real", "bogus"):
                for dry_run in (False, True):
                    cfg = _live_cfg(execution, dry_run=dry_run)
                    self.assertIs(
                        _execution_places_real_orders(cfg),
                        places_real_orders(cfg),
                        f"{execution=} {dry_run=}",
                    )

    def test_testnet_dry_run_false_is_not_real(self):
        with patch.dict(os.environ, _GATE_CREDS, clear=False):
            self.assertFalse(_execution_places_real_orders(_live_cfg("testnet")))
            self.assertFalse(_execution_places_real_orders(_live_cfg("shadow")))

    def test_real_with_guards_is_real(self):
        with patch.dict(os.environ, _GATE_CREDS, clear=False):
            os.environ.pop("DEMO_MODE", None)
            self.assertTrue(_execution_places_real_orders(_live_cfg("real")))

    def test_unresolved_mode_fails_closed_as_real(self):
        with patch.dict(os.environ, _GATE_CREDS, clear=False):
            self.assertTrue(_execution_places_real_orders(_live_cfg("bogus")))
            self.assertTrue(_execution_places_real_orders(_live_cfg("real", confirmed=False)))
        env = {k: v for k, v in os.environ.items() if k not in _GATE_CREDS}
        with patch.dict(os.environ, env, clear=True):
            self.assertTrue(_execution_places_real_orders(_live_cfg("testnet")))

    def test_non_live_modes_are_not_real(self):
        for mode in ("paper", "demo", "gate_testnet"):
            self.assertFalse(_execution_places_real_orders({"trading_mode": mode}))

    def test_no_second_parser_in_risk_manager(self):
        import inspect

        src = inspect.getsource(rm_mod._execution_places_real_orders)
        self.assertNotIn("resolve_execution_mode", src)
        self.assertNotIn("dry_run", src.split('"""')[-1])
        self.assertNotIn('"execution"', src)
        self.assertIn("places_real_orders", src)


if __name__ == "__main__":
    unittest.main()
