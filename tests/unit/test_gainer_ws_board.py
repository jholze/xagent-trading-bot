"""Unit tests for gainer WS board identify (no live Gate)."""

from __future__ import annotations

import unittest

from services.gainer_universe.ws_board import (
    WsBoardState,
    reset_ws_board,
    watch_symbols_from_gainer_state,
    ws_board_config,
    ws_board_enabled,
)


class TestWsBoardConfig(unittest.TestCase):
    def test_default_off(self):
        self.assertFalse(ws_board_enabled({}))
        self.assertFalse(ws_board_enabled({"gainer_universe": {}}))

    def test_enabled_from_root(self):
        raw = {
            "gainer_universe": {
                "ws_board": {"enabled": True, "mode": "shadow", "max_watch": 20}
            }
        }
        self.assertTrue(ws_board_enabled(raw))
        cfg = ws_board_config(raw)
        self.assertEqual(cfg["max_watch"], 20)
        self.assertEqual(cfg["mode"], "shadow")

    def test_enabled_from_section(self):
        raw = {"ws_board": {"enabled": True, "mode": "shadow"}}
        self.assertTrue(ws_board_enabled(raw))

    def test_mode_off(self):
        raw = {"gainer_universe": {"ws_board": {"enabled": True, "mode": "off"}}}
        self.assertFalse(ws_board_enabled(raw))


class TestWatchSymbols(unittest.TestCase):
    def test_prefer_live_top_cap(self):
        state = {
            "live_top": [
                {"symbol": f"C{i}/USDT", "pct_24h": 10 + i} for i in range(10)
            ],
            "eligible": [{"symbol": "EXTRA/USDT", "rank": 1}],
        }
        cfg = {
            "gainer_universe": {"ws_board": {"enabled": True, "max_watch": 5}}
        }
        syms = watch_symbols_from_gainer_state(state, cfg)
        self.assertEqual(len(syms), 5)
        self.assertEqual(syms[0], "C0/USDT")
        self.assertNotIn("EXTRA/USDT", syms)


class TestWsBoardState(unittest.TestCase):
    def setUp(self):
        reset_ws_board()

    def test_rank_by_pct(self):
        b = WsBoardState()
        b.on_tick("AAA/USDT", last=1.0, pct_24h=12.0)
        b.on_tick("BBB/USDT", last=2.0, pct_24h=40.0)
        b.on_tick("CCC/USDT", last=3.0, pct_24h=3.0)  # below min
        board = b.ranked_board(top_n=10, min_pct=5.0, max_age_sec=60)
        self.assertEqual(len(board), 2)
        self.assertEqual(board[0]["symbol"], "BBB/USDT")
        self.assertEqual(board[0]["rank"], 1)
        self.assertEqual(board[1]["symbol"], "AAA/USDT")

    def test_stale_dropped(self):
        b = WsBoardState()
        b.on_tick("OLD/USDT", last=1.0, pct_24h=20.0)
        with b._lock:
            b._ticks["OLD/USDT"]["ts"] = 0.0
        board = b.ranked_board(top_n=5, min_pct=5.0, max_age_sec=1.0)
        self.assertEqual(board, [])


if __name__ == "__main__":
    unittest.main()
