"""RelVol must not be blocked by universe trade cap (discovery path)."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


class TestRelVolUniverseBypass(unittest.TestCase):
    def test_risk_manager_exempts_relvol_from_universe_cap(self):
        path = Path(__file__).resolve().parents[2] / "risk" / "risk_manager.py"
        src = path.read_text(encoding="utf-8")
        self.assertIn("def _is_relvol_buy", src)
        self.assertIn("_is_relvol_buy(source, order)", src)
        self.assertIn('src == "gainer_relvol"', src)
        self.assertIn("GAINER_RELVOL", src)
        # universe block must gate on the helper (not only DCA)
        idx_universe = src.index("universe_trade_cap")
        idx_relvol_check = src.index("not self._is_relvol_buy")
        self.assertLess(idx_relvol_check, idx_universe)
        # static method parses cleanly
        tree = ast.parse(src)
        names = {
            n.name
            for n in tree.body
            if isinstance(n, ast.ClassDef) and n.name == "RiskManager"
            for n in n.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertIn("_is_relvol_buy", names)
        self.assertIn("_is_dca_buy", names)


if __name__ == "__main__":
    unittest.main()
