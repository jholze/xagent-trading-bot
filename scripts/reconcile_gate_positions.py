#!/usr/bin/env python3
"""Compare local positions.json amounts vs Gate spot holdings."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv

load_dotenv()

from core.config import get_bot_config
from data_manager import get_config
from data_manager import uses_exchange_ledger
from services.gate_balance import fetch_spot_holdings
from strategies.positions import list_active_positions


def main() -> int:
    cfg = get_bot_config()
    if not uses_exchange_ledger(cfg.trading_mode):
        print(f"trading_mode={cfg.trading_mode} — switch to gate_testnet or live first")
        return 1

    holdings = {h["currency"]: h["amount"] for h in fetch_spot_holdings(cfg)}
    local = list_active_positions()

    print(f"Mode: {cfg.trading_mode}")
    print(f"{'Symbol':<14} {'Local':>12} {'Gate':>12} {'Delta':>12}")
    print("-" * 54)

    seen = set()
    mismatches = 0
    for pos in local:
        sym = pos["symbol"].split("/")[0]
        seen.add(sym)
        local_amt = float(pos.get("amount", 0))
        gate_amt = float(holdings.get(sym, 0))
        delta = local_amt - gate_amt
        flag = " ⚠️" if abs(delta) > max(local_amt, gate_amt, 1e-8) * 0.05 else ""
        if flag:
            mismatches += 1
        print(f"{sym:<14} {local_amt:12.6f} {gate_amt:12.6f} {delta:+12.6f}{flag}")

    for currency, amount in sorted(holdings.items()):
        if currency in seen or amount <= 0:
            continue
        print(f"{currency:<14} {0:12.6f} {amount:12.6f} {-amount:+12.6f} ⚠️ (only on Gate)")
        mismatches += 1

    if mismatches:
        print(f"\n{mismatches} mismatch(es) — run manual review before live auto-trades")
        return 2
    print("\nOK — local positions align with Gate holdings")
    return 0


if __name__ == "__main__":
    sys.exit(main())