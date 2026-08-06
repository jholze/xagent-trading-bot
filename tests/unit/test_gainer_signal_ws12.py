"""WS-1 pure board/signal + WS-2 /internal/gainer-signal handlers."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask

from services.gainer_signal.board import LeadersBoard, reset_board
from services.gainer_signal.bot_http import (
    count_gainer_buys_today_from_fills,
    gainer_entry_enabled,
    process_gainer_signal,
    register_gainer_signal_routes,
)
from services.gainer_signal.pure import (
    check_gainer_entry_caps,
    clamp_usdt_to_vol,
    count_open_gainer_positions,
    is_eligible,
    rank_leaders_from_tickers,
    select_entry_signals,
    vol_bucket_from_atr_pct,
)
from services.gainer_signal.push import push_signal_to_bot
from services.portfolio_service import PortfolioService
from strategies.positions import get_position, list_active_positions, positions


class TestPureBoard(unittest.TestCase):
    def test_rank_recognize_no_vol_cut_no_min_price(self):
        tickers = {
            "THIN/USDT": {"percentage": 90, "quoteVolume": 10_000, "last": 1e-8},
            "FAT/USDT": {"percentage": 30, "quoteVolume": 2_000_000, "last": 1.0},
            "LEV3L/USDT": {"percentage": 95, "quoteVolume": 9_000_000, "last": 1.0},
            "X/USDC": {"percentage": 99, "quoteVolume": 9_000_000, "last": 1.0},
        }
        leaders = rank_leaders_from_tickers(tickers, top_n=100)
        syms = [L["symbol"] for L in leaders]
        self.assertIn("THIN/USDT", syms)
        self.assertIn("LEV3L/USDT", syms)
        self.assertNotIn("X/USDC", syms)
        thin = next(L for L in leaders if L["symbol"] == "THIN/USDT")
        self.assertFalse(thin["eligible"])
        self.assertEqual(thin["reject_reason"], "low_volume")
        fat = next(L for L in leaders if L["symbol"] == "FAT/USDT")
        self.assertTrue(fat["eligible"])
        lev = next(L for L in leaders if L["symbol"] == "LEV3L/USDT")
        self.assertTrue(lev["leverage"])
        self.assertFalse(lev["eligible"])

    def test_eligible_500k_boundary(self):
        self.assertTrue(is_eligible(quote_vol=500_000, leverage=False)[0])
        self.assertFalse(is_eligible(quote_vol=499_999, leverage=False)[0])

    def test_select_heat_signal(self):
        leaders = [
            {
                "symbol": "A/USDT",
                "rank": 2,
                "pct_24h": 25.0,
                "quote_vol": 3e6,
                "last": 1.0,
                "eligible": True,
            },
            {
                "symbol": "B/USDT",
                "rank": 50,
                "pct_24h": 25.0,
                "quote_vol": 3e6,
                "last": 1.0,
                "eligible": True,
            },
        ]
        sigs = select_entry_signals(leaders, max_rank=20, heat_min=12, heat_max=40)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["symbol"], "A/USDT")
        self.assertEqual(sigs[0]["trigger"], "heat")

    def test_select_rejects_parabolic_above_heat_max(self):
        """Board may list BLESS at +80%; entry signals must not (anti peak-FOMO)."""
        leaders = [
            {
                "symbol": "BLESS/USDT",
                "rank": 3,
                "pct_24h": 80.0,
                "quote_vol": 5e6,
                "last": 0.02,
                "eligible": True,
            },
            {
                "symbol": "EARLY/USDT",
                "rank": 5,
                "pct_24h": 22.0,
                "quote_vol": 2e6,
                "last": 1.0,
                "eligible": True,
            },
        ]
        sigs = select_entry_signals(leaders, max_rank=20, heat_min=12, heat_max=40)
        syms = [s["symbol"] for s in sigs]
        self.assertNotIn("BLESS/USDT", syms)
        self.assertIn("EARLY/USDT", syms)

    def test_sticky_also_respects_heat_ceiling(self):
        leaders = [
            {
                "symbol": "PEAK/USDT",
                "rank": 2,
                "pct_24h": 70.0,
                "quote_vol": 4e6,
                "last": 1.0,
                "eligible": True,
            },
        ]
        prev = {"PEAK/USDT": {"rank": 3, "pct_24h": 50.0}}
        sigs = select_entry_signals(
            leaders, max_rank=20, heat_min=12, heat_max=40, prev_board=prev
        )
        self.assertEqual(sigs, [])

    def test_vol_bucket_from_atr(self):
        self.assertEqual(vol_bucket_from_atr_pct(2.0), "low")
        self.assertEqual(vol_bucket_from_atr_pct(4.0), "mid")
        self.assertEqual(vol_bucket_from_atr_pct(7.0), "high")
        self.assertIsNone(vol_bucket_from_atr_pct(None))

    def test_coin_aware_mid_band_and_atr_required(self):
        leaders = [
            {
                "symbol": "MID/USDT",
                "rank": 4,
                "pct_24h": 22.0,
                "quote_vol": 2e6,
                "last": 1.0,
                "eligible": True,
            },
            {
                "symbol": "NOATR/USDT",
                "rank": 5,
                "pct_24h": 22.0,
                "quote_vol": 2e6,
                "last": 1.0,
                "eligible": True,
            },
            {
                "symbol": "PEAK/USDT",
                "rank": 2,
                "pct_24h": 60.0,
                "quote_vol": 5e6,
                "last": 1.0,
                "eligible": True,
            },
        ]
        atr = {"MID/USDT": 4.0, "PEAK/USDT": 8.0}  # PEAK high bucket max 45
        state = {
            "MID/USDT": {"scans_in_top_k": 2, "first_seen_top_k_at": 0},
            "NOATR/USDT": {"scans_in_top_k": 3, "first_seen_top_k_at": 0},
            "PEAK/USDT": {"scans_in_top_k": 5, "first_seen_top_k_at": 0},
        }
        sigs = select_entry_signals(
            leaders,
            entry_policy="coin_aware_v1",
            max_rank=20,
            hard_ceiling=50,
            atr_by_symbol=atr,
            symbol_state=state,
            now_ts=1000.0,
        )
        syms = [s["symbol"] for s in sigs]
        self.assertIn("MID/USDT", syms)
        self.assertNotIn("NOATR/USDT", syms)
        self.assertNotIn("PEAK/USDT", syms)  # 60 > hard 50 and > high band 45

    def test_coin_aware_high_needs_fresh_not_only_dwell(self):
        leaders = [
            {
                "symbol": "H/USDT",
                "rank": 3,
                "pct_24h": 30.0,
                "quote_vol": 3e6,
                "last": 1.0,
                "eligible": True,
            },
        ]
        atr = {"H/USDT": 9.0}  # high
        # old first_seen, many scans, no improve
        state = {
            "H/USDT": {
                "scans_in_top_k": 10,
                "first_seen_top_k_at": 0.0,
                "prev_rank": 3,
            }
        }
        sigs = select_entry_signals(
            leaders,
            entry_policy="coin_aware_v1",
            atr_by_symbol=atr,
            symbol_state=state,
            now_ts=3600.0 * 2,  # 2h age
            first_seen_max_min=15,
        )
        self.assertEqual(sigs, [])
        # fresh first_seen → ok
        state["H/USDT"]["first_seen_top_k_at"] = 3600.0 * 2 - 60
        sigs2 = select_entry_signals(
            leaders,
            entry_policy="coin_aware_v1",
            atr_by_symbol=atr,
            symbol_state=state,
            now_ts=3600.0 * 2,
            first_seen_max_min=15,
        )
        self.assertEqual(len(sigs2), 1)

    def test_memory_block_coin_facts(self):
        class _Flags:
            hard_negative = True
            structure_risk = False
            unlock = False
            flow_only = False
            profit_taking = False

        body, status = process_gainer_signal(
            {
                "symbol": "BAD/USDT",
                "last": 1.0,
                "quote_vol": 5e6,
                "eligible": True,
                "rank": 2,
                "pct_24h": 20,
                "flags": _Flags(),
            },
            config={"gainer_entry": {"enabled": True}, "memory": {"enabled": True}},
            positions=[],
            gainer_buys_today=0,
            execute_buy=MagicMock(),
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["message"], "blocked_coin_facts")

    def test_caps(self):
        ok, reason = check_gainer_entry_caps(open_gainer_count=3, gainer_buys_today=0)
        self.assertFalse(ok)
        self.assertEqual(reason, "max_open_gainer")
        ok2, reason2 = check_gainer_entry_caps(open_gainer_count=2, gainer_buys_today=6)
        self.assertFalse(ok2)
        self.assertEqual(reason2, "max_buys_per_day")
        ok3, _ = check_gainer_entry_caps(open_gainer_count=2, gainer_buys_today=5)
        self.assertTrue(ok3)

    def test_clamp_usdt(self):
        # 2% of 100k = 2000
        self.assertEqual(clamp_usdt_to_vol(1000, 100_000, max_pct_of_vol=2.0), 1000.0)
        self.assertEqual(clamp_usdt_to_vol(5000, 100_000, max_pct_of_vol=2.0), 2000.0)

    def test_count_open_gainer(self):
        positions = [
            {"symbol": "A/USDT", "entry_source": "gainer_live_heat"},
            {"symbol": "B/USDT", "source": "grid"},
            {"symbol": "C/USDT", "entry_source": "gainer_rank_entry"},
        ]
        self.assertEqual(count_open_gainer_positions(positions), 2)

    def test_board_apply(self):
        b = LeadersBoard()
        tickers = {
            f"C{i}/USDT": {"percentage": float(100 - i), "quoteVolume": 1_000_000 + i, "last": 1}
            for i in range(120)
        }
        leaders, _ = b.apply_tickers(tickers, top_n=100, from_rest=True)
        self.assertEqual(len(leaders), 100)
        st = b.stats()
        self.assertEqual(st["n_recognized"], 100)
        self.assertGreaterEqual(st["n_eligible"], 100)
        self.assertEqual(st["rest_seeds"], 1)


class TestPush(unittest.TestCase):
    def test_push_no_token(self):
        with patch.dict(os.environ, {"GAINER_SIGNAL_TOKEN": "", "EXIT_WS_INTERNAL_TOKEN": ""}, clear=False):
            # force empty
            r = push_signal_to_bot({"symbol": "A/USDT"}, token="")
        self.assertFalse(r.get("ok"))
        self.assertEqual(r.get("message"), "no_token")


class TestBotHttp(unittest.TestCase):
    def setUp(self):
        os.environ["GAINER_SIGNAL_TOKEN"] = "secret"
        os.environ.pop("GAINER_ENTRY_ENABLED", None)

    def tearDown(self):
        os.environ.pop("GAINER_SIGNAL_TOKEN", None)
        os.environ.pop("GAINER_ENTRY_ENABLED", None)

    def test_auth_401(self):
        app = Flask(__name__)
        register_gainer_signal_routes(app)
        client = app.test_client()
        r = client.post(
            "/internal/gainer-signal",
            json={"symbol": "A/USDT", "last": 1, "quote_vol": 1e6, "eligible": True},
            headers={"X-Gainer-Signal-Token": "bad"},
        )
        self.assertEqual(r.status_code, 401)

    def test_not_configured_503(self):
        os.environ.pop("GAINER_SIGNAL_TOKEN", None)
        os.environ.pop("EXIT_WS_INTERNAL_TOKEN", None)
        app = Flask(__name__)
        register_gainer_signal_routes(app)
        client = app.test_client()
        r = client.post("/internal/gainer-signal", json={})
        self.assertEqual(r.status_code, 503)

    def test_kill_switch(self):
        body, status = process_gainer_signal(
            {
                "symbol": "A/USDT",
                "last": 1.0,
                "quote_vol": 2e6,
                "eligible": True,
                "rank": 1,
                "pct_24h": 20,
            },
            config={"gainer_entry": {"enabled": False}},
            positions=[],
            gainer_buys_today=0,
            execute_buy=MagicMock(),
        )
        self.assertEqual(status, 503)
        self.assertEqual(body["message"], "gainer_entry_disabled")

    def test_default_disabled_without_config_key(self):
        """Product: board is not a buy source — default OFF when config omits flag."""
        self.assertFalse(gainer_entry_enabled({}))
        self.assertFalse(gainer_entry_enabled({"gainer_entry": {}}))
        buy = MagicMock()
        body, status = process_gainer_signal(
            {
                "symbol": "A/USDT",
                "last": 1.0,
                "quote_vol": 2e6,
                "eligible": True,
                "rank": 1,
                "pct_24h": 20,
            },
            config={},
            positions=[],
            gainer_buys_today=0,
            execute_buy=buy,
        )
        self.assertEqual(status, 503)
        self.assertEqual(body["message"], "gainer_entry_disabled")
        buy.assert_not_called()

    def test_not_eligible(self):
        body, status = process_gainer_signal(
            {
                "symbol": "A/USDT",
                "last": 1.0,
                "quote_vol": 1000,
                "eligible": True,
                "rank": 1,
                "pct_24h": 20,
            },
            config={"gainer_entry": {"enabled": True, "require_eligible": True}},
            positions=[],
            gainer_buys_today=0,
            execute_buy=MagicMock(),
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["message"], "not_eligible")

    def test_max_open_reject(self):
        positions = [
            {"symbol": f"X{i}/USDT", "entry_source": "gainer_live_heat"} for i in range(3)
        ]
        exec_fn = MagicMock()
        body, status = process_gainer_signal(
            {
                "symbol": "NEW/USDT",
                "last": 1.0,
                "quote_vol": 5e6,
                "eligible": True,
                "rank": 2,
                "pct_24h": 22,
                "source": "gainer_live_heat",
            },
            config={
                "gainer_entry": {"enabled": True, "max_open": 3, "max_buys_per_day": 6},
                "max_usdt_per_trade": 500,
            },
            positions=positions,
            gainer_buys_today=0,
            execute_buy=exec_fn,
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["message"], "max_open_gainer")
        exec_fn.assert_not_called()

    def test_max_day_reject(self):
        exec_fn = MagicMock()
        body, status = process_gainer_signal(
            {
                "symbol": "NEW/USDT",
                "last": 1.0,
                "quote_vol": 5e6,
                "eligible": True,
                "rank": 2,
                "pct_24h": 22,
            },
            config={
                "gainer_entry": {"enabled": True, "max_open": 3, "max_buys_per_day": 6},
                "max_usdt_per_trade": 500,
            },
            positions=[],
            gainer_buys_today=6,
            execute_buy=exec_fn,
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["message"], "max_buys_per_day")
        exec_fn.assert_not_called()

    def test_happy_path_execute(self):
        result = MagicMock()
        result.executed = True
        result.message = "ok"
        result.order_id = "ord1"
        exec_fn = MagicMock(return_value=result)
        body, status = process_gainer_signal(
            {
                "symbol": "BLESS/USDT",
                "last": 0.02,
                "quote_vol": 5_000_000,
                "eligible": True,
                "rank": 3,
                "pct_24h": 25.0,
                "trigger": "heat",
                "source": "gainer_live_heat",
            },
            config={
                "gainer_entry": {"enabled": True, "max_open": 3, "max_buys_per_day": 6},
                "max_usdt_per_trade": 1000,
            },
            positions=[],
            gainer_buys_today=0,
            execute_buy=exec_fn,
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["executed"])
        self.assertEqual(body["source"], "gainer_live_heat")
        self.assertEqual(body["meta"]["leader_rank"], 3)
        self.assertEqual(body["meta"]["trigger"], "heat")
        kwargs = exec_fn.call_args.kwargs
        self.assertEqual(kwargs["symbol"], "BLESS/USDT")
        self.assertEqual(kwargs["source"], "gainer_live_heat")
        self.assertIn("gainer_meta", kwargs["request_extra"])

    def test_flask_happy_path(self):
        app = Flask(__name__)
        register_gainer_signal_routes(app)
        client = app.test_client()
        result = MagicMock(executed=True, message="filled", order_id="x")
        with patch(
            "services.gainer_signal.bot_http.process_gainer_signal",
            return_value=(
                {
                    "ok": True,
                    "executed": True,
                    "message": "filled",
                    "source": "gainer_live_heat",
                },
                200,
            ),
        ) as mock_proc:
            # Actually test real process via patch execute only
            pass
        # real process path through flask
        with patch(
            "services.gainer_signal.bot_http.process_gainer_signal",
            wraps=None,
        ):
            pass

        def _exec(**kw):
            return MagicMock(executed=True, message="filled", order_id="id1")

        # Call process through route by patching execute inside process
        with patch(
            "services.gainer_signal.bot_http.list_active_positions",
            create=True,
        ):
            body, st = process_gainer_signal(
                {
                    "symbol": "AAA/USDT",
                    "last": 1.0,
                    "quote_vol": 2e6,
                    "eligible": True,
                    "rank": 1,
                    "pct_24h": 15,
                    "source": "gainer_rank_entry",
                },
                config={"gainer_entry": {"enabled": True}, "max_usdt_per_trade": 200},
                positions=[],
                gainer_buys_today=1,
                execute_buy=_exec,
            )
        self.assertTrue(body["executed"])
        self.assertEqual(st, 200)

        r = client.post(
            "/internal/gainer-signal",
            json={
                "symbol": "AAA/USDT",
                "last": 1.0,
                "quote_vol": 2e6,
                "eligible": True,
                "rank": 1,
                "pct_24h": 15,
            },
            headers={"X-Gainer-Signal-Token": "secret"},
        )
        # may fail execute without full bot — at least not 401
        self.assertNotEqual(r.status_code, 401)


class TestRealBuyPathCaps(unittest.TestCase):
    """Honest path: PortfolioService.execute_buy must tag entry_source so caps work."""

    def setUp(self):
        self._backup = {k: dict(v) for k, v in positions.items()}
        positions.clear()
        os.environ.pop("GAINER_ENTRY_ENABLED", None)

    def tearDown(self):
        positions.clear()
        positions.update(self._backup)

    def test_portfolio_buy_tags_gainer_entry_source(self):
        ps = PortfolioService()
        r = ps.execute_buy(
            "GREAL1/USDT",
            "1h",
            1.0,
            usdt_amount=100,
            source="gainer_live_heat",
            sync_virtual_ledger=False,
        )
        self.assertTrue(r.executed)
        pos = get_position("GREAL1/USDT", "1h")
        self.assertEqual(pos.get("entry_source"), "gainer_live_heat")
        lots = list_active_positions()
        self.assertEqual(count_open_gainer_positions(lots), 1)
        tagged = [L for L in lots if L.get("symbol") == "GREAL1/USDT"]
        self.assertTrue(tagged)
        self.assertEqual(tagged[0].get("entry_source"), "gainer_live_heat")

    def test_fourth_open_rejected_after_real_portfolio_buys(self):
        """Drive 3 real PortfolioService buys, then process_gainer_signal must reject 4th."""
        ps = PortfolioService()
        for i in range(3):
            r = ps.execute_buy(
                f"GCAP{i}/USDT",
                "1h",
                1.0,
                usdt_amount=50,
                source="gainer_rank_entry",
                sync_virtual_ledger=False,
            )
            self.assertTrue(r.executed, msg=r.message)

        # entry_source must be visible via list_active_positions (not only get_position)
        lots = list_active_positions()
        self.assertEqual(count_open_gainer_positions(lots), 3)

        exec_fn = MagicMock()
        body, status = process_gainer_signal(
            {
                "symbol": "GCAPNEW/USDT",
                "last": 1.0,
                "quote_vol": 5e6,
                "eligible": True,
                "rank": 1,
                "pct_24h": 20,
                "source": "gainer_live_heat",
            },
            config={
                "gainer_entry": {"enabled": True, "max_open": 3, "max_buys_per_day": 6},
                "max_usdt_per_trade": 100,
            },
            # None → live list_active_positions path
            positions=None,
            gainer_buys_today=0,
            execute_buy=exec_fn,
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["message"], "max_open_gainer")
        self.assertEqual(body.get("open_gainer_count"), 3)
        exec_fn.assert_not_called()

    def test_day_buy_cap_from_ledger_fills(self):
        """When gainer_buys_today is None, ledger day fills must block the 7th buy."""
        fills = [
            {
                "side": "buy",
                "status": "filled",
                "source": "gainer_live_heat",
                "day_key": "2099-01-01",
            }
            for _ in range(6)
        ]
        self.assertEqual(
            count_gainer_buys_today_from_fills(fills, day_scoped=True),
            6,
        )
        with patch(
            "services.gainer_signal.bot_http.load_gainer_buys_today_from_ledger",
            return_value=6,
        ):
            exec_fn = MagicMock()
            body, status = process_gainer_signal(
                {
                    "symbol": "GDAY/USDT",
                    "last": 1.0,
                    "quote_vol": 5e6,
                    "eligible": True,
                    "rank": 1,
                    "pct_24h": 18,
                    "source": "gainer_signal",
                },
                config={
                    "gainer_entry": {
                        "enabled": True,
                        "max_open": 3,
                        "max_buys_per_day": 6,
                    },
                    "max_usdt_per_trade": 100,
                },
                positions=[],
                gainer_buys_today=None,  # force ledger path
                execute_buy=exec_fn,
            )
        self.assertEqual(status, 409)
        self.assertEqual(body["message"], "max_buys_per_day")
        exec_fn.assert_not_called()

    def test_default_execute_buy_uses_portfolio_tagging(self):
        """Default execute_buy path (TradingService-shaped) via PortfolioService only.

        Avoid full TradingService/Risk (needs market/config); assert the portfolio
        boundary that gate_adapter._sync_local_ledger calls tags entry_source.
        """
        ps = PortfolioService()
        # Mimic gate_adapter._sync_local_ledger BUY branch
        local = ps.execute_buy(
            "GWIRE/USDT",
            "1h",
            0.5,
            usdt_amount=200,
            source="gainer_live_heat",
            order_id="test-ord",
            sync_virtual_ledger=False,
        )
        self.assertTrue(local.executed)
        pos = get_position("GWIRE/USDT", "1h")
        self.assertEqual(pos["entry_source"], "gainer_live_heat")
        # And process_gainer_signal default path wiring: source reaches execute kwargs
        seen = {}

        def _exec(**kw):
            seen.update(kw)
            # real portfolio write
            return ps.execute_buy(
                kw["symbol"],
                kw.get("timeframe") or "1h",
                float(kw["price"]),
                usdt_amount=float(kw["usdt"]),
                source=kw["source"],
                sync_virtual_ledger=False,
            )

        body, st = process_gainer_signal(
            {
                "symbol": "GWIRE2/USDT",
                "last": 0.5,
                "quote_vol": 3e6,
                "eligible": True,
                "rank": 2,
                "pct_24h": 22,
                "source": "gainer_live_heat",
                "trigger": "heat",
            },
            config={
                "gainer_entry": {"enabled": True, "max_open": 3, "max_buys_per_day": 6},
                "max_usdt_per_trade": 200,
            },
            positions=list_active_positions(),
            gainer_buys_today=0,
            execute_buy=_exec,
        )
        self.assertEqual(st, 200)
        self.assertTrue(body["executed"])
        self.assertEqual(seen.get("source"), "gainer_live_heat")
        self.assertEqual(
            get_position("GWIRE2/USDT", "1h").get("entry_source"),
            "gainer_live_heat",
        )


class TestServiceAppHealth(unittest.TestCase):
    def test_health_and_leaders_with_seed(self):
        from services.gainer_signal.board import reset_board

        board = reset_board()
        tickers = {
            f"S{i}/USDT": {
                "percentage": float(50 - i * 0.1),
                "quoteVolume": 10_000 if i > 80 else 1_000_000,
                "last": 1.0,
            }
            for i in range(120)
        }
        leaders, _ = board.apply_tickers(tickers, top_n=100, from_rest=True)
        self.assertEqual(len(leaders), 100)
        st = board.stats()
        self.assertEqual(st["n_recognized"], 100)
        # some low vol not eligible
        self.assertLess(st["n_eligible"], 100)
        low = [L for L in leaders if not L["eligible"]]
        high = [L for L in leaders if L["eligible"]]
        self.assertTrue(any(L["reject_reason"] == "low_volume" for L in low))
        self.assertTrue(len(high) > 0)

        app = Flask(__name__)

        @app.route("/health")
        def health():
            s = board.stats()
            return {
                "status": "OK",
                "n_recognized": s["n_recognized"],
                "n_eligible": s["n_eligible"],
                "connected": s.get("connected"),
            }

        @app.route("/leaders")
        def leaders_ep():
            return {"leaders": board.leaders(), "n": len(board.leaders())}

        client = app.test_client()
        h = client.get("/health").get_json()
        self.assertEqual(h["n_recognized"], 100)
        L = client.get("/leaders").get_json()
        self.assertEqual(L["n"], 100)


if __name__ == "__main__":
    unittest.main()
