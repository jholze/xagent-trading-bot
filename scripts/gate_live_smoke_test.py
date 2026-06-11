#!/usr/bin/env python3
"""Gate testnet/live smoke test — validates keys, dry_run, and order ledger wiring.

Usage:
  python3 scripts/gate_live_smoke_test.py --testnet
  python3 scripts/gate_live_smoke_test.py --live --dry-run-only

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
from data_manager import get_config, load_orders
from execution.gate_adapter import GateExecutionAdapter
from data_manager import uses_exchange_ledger
from services.gate_balance import fetch_spot_holdings, fetch_usdt_balance
from services.order_service import OrderService


def _check_keys(cfg: dict, testnet: bool) -> tuple[bool, str]:
    section = cfg.get("gate_testnet" if testnet else "live", {})
    key_env = section.get("api_key_env", "GATE_TESTNET_API_KEY" if testnet else "GATE_API_KEY")
    secret_env = section.get("api_secret_env", "GATE_TESTNET_API_SECRET" if testnet else "GATE_API_SECRET")
    if not os.getenv(key_env) or not os.getenv(secret_env):
        return False, f"Missing {key_env} / {secret_env}"
    return True, "OK"


def main() -> int:
    parser = argparse.ArgumentParser(description="Gate.io readiness smoke test")
    parser.add_argument("--testnet", action="store_true", help="Check gate_testnet config")
    parser.add_argument("--live", action="store_true", help="Check live mainnet config")
    parser.add_argument("--dry-run-only", action="store_true", help="Refuse if dry_run is false")
    parser.add_argument("--execute", action="store_true", help="Place a tiny test order (dangerous)")
    args = parser.parse_args()

    if not args.testnet and not args.live:
        args.testnet = True

    raw = dict(get_config())
    if args.testnet:
        raw["trading_mode"] = "gate_testnet"
    if args.live:
        raw["trading_mode"] = "live"

    cfg = BotConfig()
    cfg._raw = raw
    section = cfg.gate_testnet_config if args.testnet else cfg.live_config
    label = "testnet" if args.testnet else "live"

    ok, msg = _check_keys(raw, testnet=args.testnet and not args.live)
    print(f"[keys] {label}: {msg}")
    if not ok:
        return 1

    dry = section.get("dry_run", not args.testnet)
    print(f"[config] dry_run={dry} max_usdt={section.get('max_usdt_per_trade', 25)}")

    if args.dry_run_only and not dry:
        print("[abort] dry_run is false — use without --dry-run-only to acknowledge")
        return 1

    if not uses_exchange_ledger(cfg.trading_mode):
        print("[abort] trading_mode must be live or gate_testnet")
        return 1

    usdt = fetch_usdt_balance(cfg)
    holdings = fetch_spot_holdings(cfg)
    print(f"[balance] USDT free: ${usdt:,.2f}")
    print(f"[holdings] {len(holdings)} spot assets")

    adapter = GateExecutionAdapter(cfg, testnet=args.testnet and not args.live)
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