#!/usr/bin/env python3
"""Gate.io live readiness smoke test — validates keys, dry_run, and order ledger wiring.

Usage:
  python3 scripts/gate_live_smoke_test.py
  python3 scripts/gate_live_smoke_test.py --dry-run-only

Does NOT place orders unless --execute is passed AND dry_run is false in config.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv

load_dotenv()

from core.config import BotConfig, get_bot_config
from data_manager import get_config, load_orders, uses_exchange_ledger
from execution.gate_adapter import GateExecutionAdapter
from services.gate_balance import fetch_spot_holdings, fetch_usdt_balance


def _check_keys(cfg: dict) -> tuple[bool, str]:
    section = cfg.get("live", {})
    key_env = section.get("api_key_env", "GATE_API_KEY")
    secret_env = section.get("api_secret_env", "GATE_API_SECRET")
    if not os.getenv(key_env) or not os.getenv(secret_env):
        return False, f"Missing {key_env} / {secret_env}"
    return True, "OK"


def main() -> int:
    parser = argparse.ArgumentParser(description="Gate.io live readiness smoke test")
    parser.add_argument("--dry-run-only", action="store_true", help="Refuse if dry_run is false")
    parser.add_argument("--execute", action="store_true", help="Place a tiny test order (dangerous)")
    args = parser.parse_args()

    raw = dict(get_config())
    raw["trading_mode"] = "live"
    cfg = BotConfig()
    cfg._raw = raw
    section = cfg.live_config

    ok, msg = _check_keys(raw)
    print(f"[keys] live: {msg}")
    if not ok:
        return 1

    dry = section.get("dry_run", True)
    print(f"[config] dry_run={dry} max_usdt={section.get('max_usdt_per_trade', 25)}")

    if args.dry_run_only and not dry:
        print("[abort] dry_run is false — use without --dry-run-only to acknowledge")
        return 1

    if not uses_exchange_ledger(cfg.trading_mode):
        print("[abort] trading_mode must be live")
        return 1

    usdt = fetch_usdt_balance(cfg)
    holdings = fetch_spot_holdings(cfg)
    print(f"[balance] USDT free: ${usdt:,.2f}")
    print(f"[holdings] {len(holdings)} spot assets")

    adapter = GateExecutionAdapter(cfg)
    exchange = adapter._get_exchange()
    if not exchange:
        print("[abort] ccxt exchange init failed")
        return 1
    print("[ccxt] Gate exchange connected")

    orders_before = len(load_orders("live").get("orders", []))

    if args.execute:
        if dry:
            print("[execute] skipped — dry_run is ON")
        else:
            print("[execute] NOT IMPLEMENTED — place manual /buy via Telegram after review")
            return 1

    orders_after = len(load_orders("live").get("orders", []))
    print(f"[ledger] orders.live.json entries: {orders_after} (delta {orders_after - orders_before})")
    print("[ok] smoke checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())