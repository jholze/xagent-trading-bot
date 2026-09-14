"""#410: accounting helpers follow live.execution, not live.dry_run.

``execution: testnet, dry_run: false`` used to hit Gate testnet but book fills
as live PnL / real Gate-spot NAV because ``is_live_dry_run``,
``is_simulated_trading`` and ``is_real_live_trading`` keyed off ``live.dry_run``.
Contract: ``is_real_live_trading`` ≡ ``places_real_orders`` (True only for a
resolved ``real``); ``is_live_dry_run`` / ``is_simulated_trading`` ≡ not
real-money (shadow **and** testnet); ``dry_run: true`` still forces shadow.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from core.config import BotConfig
from core.execution_mode import places_real_orders, resolve_execution_mode
from core.simulated_trading import (
    is_real_live_trading,
    is_simulated_trading,
    uses_order_ledger_cash,
    uses_simulated_portfolio,
)
from data_manager import is_dry_run_enhanced, is_live_dry_run, uses_watchlist_expansion

_CREDS = {"GATE_API_KEY": "test-key", "GATE_API_SECRET": "test-secret"}
_NO_DEMO = {"DEMO_MODE": "0"}
_ENV = {**_CREDS, **_NO_DEMO}


def _live_cfg(execution, *, dry_run=False, confirmed=True, **live_extra) -> dict:
    live = {
        "dry_run": dry_run,
        "api_key_env": "GATE_API_KEY",
        "api_secret_env": "GATE_API_SECRET",
        "simulated_balance_usdt": 5000,
        **live_extra,
    }
    if execution is not None:
        live["execution"] = execution
    return {"trading_mode": "live", "live_confirmed": confirmed, "live": live}


class TestPlacesRealOrdersHelper(unittest.TestCase):
    def test_matches_resolve_execution_mode_when_resolvable(self):
        with patch.dict(os.environ, _ENV, clear=False):
            for execution in ("shadow", "testnet", "real", None):
                for dry_run in (True, False):
                    cfg = _live_cfg(execution, dry_run=dry_run)
                    expected = resolve_execution_mode(cfg).places_real_orders
                    self.assertEqual(
                        places_real_orders(cfg), expected, f"{execution=} {dry_run=}"
                    )

    def test_only_real_places_real_orders(self):
        with patch.dict(os.environ, _ENV, clear=False):
            self.assertFalse(places_real_orders(_live_cfg("shadow")))
            self.assertFalse(places_real_orders(_live_cfg("testnet")))
            self.assertTrue(places_real_orders(_live_cfg("real")))

    def test_non_live_trading_modes_never_real(self):
        with patch.dict(os.environ, _ENV, clear=False):
            for mode in ("paper", "demo", "off", ""):
                cfg = {**_live_cfg("real"), "trading_mode": mode}
                self.assertFalse(places_real_orders(cfg), mode)

    def test_fail_closed_requested_real_without_guards_stays_real(self):
        """Unresolvable real (no creds / unconfirmed / DEMO_MODE) is still real-money."""
        empty = {"GATE_API_KEY": "", "GATE_API_SECRET": "", "DEMO_MODE": "0"}
        with patch.dict(os.environ, empty, clear=False):
            with self.assertRaises(RuntimeError):
                resolve_execution_mode(_live_cfg("real"))
            self.assertTrue(places_real_orders(_live_cfg("real")))
        with patch.dict(os.environ, {**_CREDS, "DEMO_MODE": "1"}, clear=False):
            self.assertTrue(places_real_orders(_live_cfg("real")))
        with patch.dict(os.environ, _ENV, clear=False):
            self.assertTrue(places_real_orders(_live_cfg("real", confirmed=False)))

    def test_fail_closed_testnet_without_creds_treated_as_real(self):
        """Unresolvable testnet cannot start an adapter; keep every live gate in place."""
        empty = {"GATE_API_KEY": "", "GATE_API_SECRET": "", "DEMO_MODE": "0"}
        with patch.dict(os.environ, empty, clear=False):
            with self.assertRaises(RuntimeError):
                resolve_execution_mode(_live_cfg("testnet"))
            self.assertTrue(places_real_orders(_live_cfg("testnet")))

    def test_fail_closed_unknown_execution_treated_as_real(self):
        with patch.dict(os.environ, _ENV, clear=False):
            with self.assertRaises(RuntimeError):
                resolve_execution_mode(_live_cfg("bogus"))
            self.assertTrue(places_real_orders(_live_cfg("bogus")))

    def test_fail_closed_path_logs_warning(self):
        """N1: the swallowed RuntimeError must still surface to the operator."""
        with patch.dict(os.environ, _ENV, clear=False), patch(
            "core.execution_mode.log"
        ) as mock_log:
            self.assertTrue(places_real_orders(_live_cfg("typo_shadow", dry_run=False)))
        warnings = [c for c in mock_log.call_args_list if c.args[1:] == ("WARNING",)]
        self.assertEqual(len(warnings), 1, mock_log.call_args_list)
        self.assertIn("typo_shadow", warnings[0].args[0])
        self.assertIn("fail-closed", warnings[0].args[0])

    def test_dry_run_true_short_circuit_does_not_log_warning(self):
        with patch.dict(os.environ, _ENV, clear=False), patch(
            "core.execution_mode.log"
        ) as mock_log:
            self.assertFalse(places_real_orders(_live_cfg("typo_shadow", dry_run=True)))
        mock_log.assert_not_called()

    def test_dry_run_true_never_reaches_fail_closed(self):
        empty = {"GATE_API_KEY": "", "GATE_API_SECRET": "", "DEMO_MODE": "1"}
        with patch.dict(os.environ, empty, clear=False):
            for execution in ("testnet", "real", "typo_shadow"):
                self.assertFalse(places_real_orders(_live_cfg(execution, dry_run=True)), execution)

    def test_unknown_execution_with_dry_run_true_stays_simulated(self):
        """B1: a typo in live.execution with dry_run on must not flip accounting to real."""
        cfg = _live_cfg("typo_shadow", dry_run=True)
        with patch.dict(os.environ, _ENV, clear=False):
            with self.assertRaises(RuntimeError):
                resolve_execution_mode(cfg)
            self.assertFalse(places_real_orders(cfg))
            self.assertTrue(is_live_dry_run(cfg))
            self.assertTrue(is_simulated_trading(cfg))
            self.assertFalse(is_real_live_trading(cfg))

    def test_unknown_execution_with_dry_run_false_stays_fail_closed_real(self):
        cfg = _live_cfg("typo_shadow", dry_run=False)
        with patch.dict(os.environ, _ENV, clear=False):
            with self.assertRaises(RuntimeError):
                resolve_execution_mode(cfg)
            self.assertTrue(places_real_orders(cfg))
            self.assertFalse(is_live_dry_run(cfg))
            self.assertFalse(is_simulated_trading(cfg))
            self.assertTrue(is_real_live_trading(cfg))

    def test_dry_run_must_be_literal_true_to_short_circuit(self):
        """Truthy-but-not-True dry_run (e.g. "yes", 1) does not bypass fail-closed."""
        with patch.dict(os.environ, _ENV, clear=False):
            for dry_run in ("yes", 1, "true"):
                self.assertTrue(
                    places_real_orders(_live_cfg("typo_shadow", dry_run=dry_run)), repr(dry_run)
                )


class TestTestnetDryRunFalse(unittest.TestCase):
    """The #410 bug shape: hits Gate testnet, must not be booked as real."""

    def setUp(self):
        self.cfg = _live_cfg("testnet", dry_run=False)

    def test_not_real_live_trading(self):
        with patch.dict(os.environ, _ENV, clear=False):
            self.assertFalse(is_real_live_trading(self.cfg))

    def test_is_live_dry_run(self):
        with patch.dict(os.environ, _ENV, clear=False):
            self.assertTrue(is_live_dry_run(self.cfg))

    def test_is_simulated_trading_and_ledger_paths(self):
        with patch.dict(os.environ, _ENV, clear=False):
            self.assertTrue(is_simulated_trading(self.cfg))
            self.assertTrue(uses_simulated_portfolio(self.cfg))
            self.assertTrue(uses_order_ledger_cash(self.cfg))
            self.assertTrue(uses_watchlist_expansion(self.cfg))

    def test_dry_run_enhanced_allowed_on_testnet(self):
        cfg = _live_cfg("testnet", dry_run=False, dry_run_enhanced=True)
        with patch.dict(os.environ, _ENV, clear=False):
            self.assertTrue(is_dry_run_enhanced(cfg))
            self.assertFalse(uses_order_ledger_cash(cfg))

    def test_execution_unset_defaults_to_shadow_not_real(self):
        cfg = _live_cfg(None, dry_run=False)
        with patch.dict(os.environ, _ENV, clear=False):
            self.assertFalse(is_real_live_trading(cfg))
            self.assertTrue(is_live_dry_run(cfg))
            self.assertTrue(is_simulated_trading(cfg))


class TestRealDryRunFalse(unittest.TestCase):
    def test_real_with_guards_is_real(self):
        cfg = _live_cfg("real", dry_run=False)
        with patch.dict(os.environ, _ENV, clear=False):
            self.assertTrue(is_real_live_trading(cfg))
            self.assertFalse(is_live_dry_run(cfg))
            self.assertFalse(is_simulated_trading(cfg))
            self.assertFalse(uses_simulated_portfolio(cfg))
            self.assertFalse(uses_order_ledger_cash(cfg))
            self.assertFalse(uses_watchlist_expansion(cfg))

    def test_real_never_dry_run_enhanced(self):
        cfg = _live_cfg("real", dry_run=False, dry_run_enhanced=True)
        with patch.dict(os.environ, _ENV, clear=False):
            self.assertFalse(is_dry_run_enhanced(cfg))

    def test_real_unconfirmed_is_not_real_live_trading(self):
        """live_confirmed stays a hard requirement (unchanged pre-#410 behaviour)."""
        cfg = _live_cfg("real", dry_run=False, confirmed=False)
        with patch.dict(os.environ, _ENV, clear=False):
            self.assertFalse(is_real_live_trading(cfg))
            # fail-closed: requested real is not booked to the simulated ledger either
            self.assertFalse(is_live_dry_run(cfg))

    def test_real_confirmed_without_creds_fail_closed_real(self):
        """Mirrors risk_manager's short gate: unresolved real must stay real."""
        cfg = _live_cfg("real", dry_run=False)
        empty = {"GATE_API_KEY": "", "GATE_API_SECRET": "", "DEMO_MODE": "0"}
        with patch.dict(os.environ, empty, clear=False):
            self.assertTrue(is_real_live_trading(cfg))
            self.assertFalse(is_live_dry_run(cfg))
            self.assertFalse(is_simulated_trading(cfg))


class TestDryRunTrueForcesShadow(unittest.TestCase):
    def test_dry_run_true_is_shadow_regardless_of_execution(self):
        with patch.dict(os.environ, _ENV, clear=False):
            for execution in ("shadow", "testnet", "real", None, "typo_shadow"):
                cfg = _live_cfg(execution, dry_run=True)
                if execution == "typo_shadow":
                    # resolve refuses the unknown value; accounting still stays simulated
                    with self.assertRaises(RuntimeError):
                        resolve_execution_mode(cfg)
                else:
                    self.assertEqual(resolve_execution_mode(cfg).adapter_mode, "shadow", execution)
                self.assertFalse(places_real_orders(cfg), execution)
                self.assertFalse(is_real_live_trading(cfg), execution)
                self.assertTrue(is_live_dry_run(cfg), execution)
                self.assertTrue(is_simulated_trading(cfg), execution)

    def test_demo_mode_is_always_simulated(self):
        cfg = _live_cfg("testnet", dry_run=False)
        with patch.dict(os.environ, {**_CREDS, "DEMO_MODE": "1"}, clear=False):
            self.assertTrue(is_simulated_trading(cfg))
            self.assertTrue(is_live_dry_run(cfg))


class TestBotConfigCallers(unittest.TestCase):
    """BotConfig-wrapped callers (gate_balance / nav history) follow the helpers."""

    def test_gate_balance_testnet_never_reads_gate_spot_wallet(self):
        import services.gate_balance as gb

        gb.reset_balance_cache_for_tests()
        cfg = BotConfig(_live_cfg("testnet", dry_run=False))
        adapter = MagicMock()
        history = {"virtual_balance": 4321.0, "trades": []}
        with patch.dict(os.environ, _ENV, clear=False), patch.object(
            gb, "get_gate_adapter", return_value=adapter
        ), patch.object(gb, "resolve_sim_cash_balance", return_value=4321.0), patch.object(
            gb, "load_live_trade_history", return_value=history
        ), patch.object(gb, "get_prices_batch", return_value={}):
            bundle = gb.fetch_balance_bundle(cfg)
            holdings = gb.fetch_spot_holdings(cfg)
            with patch.object(gb, "_equity_cash_plus_positions", side_effect=lambda cash, *_: cash):
                equity = gb.fetch_portfolio_equity(cfg)
            cache_key = gb._balance_cache_key(cfg)
        adapter._get_exchange.assert_not_called()
        self.assertEqual(bundle["usdt"], 4321.0)
        self.assertEqual(bundle["holdings"], [])
        self.assertEqual(holdings, [])
        self.assertEqual(equity, 4321.0)
        self.assertTrue(cache_key.endswith(":live:True"), cache_key)

    def test_gate_balance_real_reads_gate_spot_wallet(self):
        import services.gate_balance as gb

        gb.reset_balance_cache_for_tests()
        cfg = BotConfig(_live_cfg("real", dry_run=False))
        adapter = MagicMock()
        exchange = MagicMock()
        exchange.fetch_balance.return_value = {
            "USDT": {"free": 123.0},
            "free": {"USDT": 123.0, "BTC": 0.1},
        }
        adapter._get_exchange.return_value = exchange
        with patch.dict(os.environ, _ENV, clear=False), patch.object(
            gb, "get_gate_adapter", return_value=adapter
        ):
            bundle = gb.fetch_balance_bundle(cfg)
            cache_key = gb._balance_cache_key(cfg)
        exchange.fetch_balance.assert_called_once()
        self.assertEqual(bundle["usdt"], 123.0)
        self.assertEqual([h["currency"] for h in bundle["holdings"]], ["BTC"])
        self.assertTrue(cache_key.endswith(":live:False"), cache_key)
        gb.reset_balance_cache_for_tests()

    def test_nav_history_testnet_uses_live_dry_run_history_store(self):
        import services.portfolio_nav_history as nav
        from data_manager import (
            load_live_trade_history,
            load_trade_history,
            save_live_trade_history,
            save_trade_history,
        )

        with patch.dict(os.environ, _ENV, clear=False), patch(
            "data_manager.get_config", return_value=_live_cfg("testnet", dry_run=False)
        ):
            load_fn, save_fn = nav._trade_history_io()
        self.assertIs(load_fn, load_live_trade_history)
        self.assertIs(save_fn, save_live_trade_history)

        with patch.dict(os.environ, _ENV, clear=False), patch(
            "data_manager.get_config", return_value=_live_cfg("real", dry_run=False)
        ):
            load_fn, save_fn = nav._trade_history_io()
        self.assertIs(load_fn, load_trade_history)
        self.assertIs(save_fn, save_trade_history)


if __name__ == "__main__":
    unittest.main()
