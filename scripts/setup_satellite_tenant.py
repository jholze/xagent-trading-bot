#!/usr/bin/env python3
"""Reset and align a satellite tenant with operator demo/simulated-live settings."""

from __future__ import annotations

import argparse
import copy
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.operator_mongo import prepare_operator_mongo


def _initial_cash(default_cfg: dict) -> float:
    live = default_cfg.get("live") or {}
    if live.get("simulated_balance_usdt"):
        return float(live["simulated_balance_usdt"])
    paper = (default_cfg.get("paper") or {}).get("initial_capital_usdt")
    if paper:
        return float(paper)
    return float(default_cfg.get("initial_capital_usdt", 100_000))


def setup_satellite_tenant(
    tenant_id: str,
    *,
    scope: str = "demo",
    dry_run: bool = True,
    copy_watchlist_from: str = "default",
) -> dict:
    from core.tenant_context import tenant_context
    from core.trading_profiles import build_operator_like_tenant_config
    from data_manager import _load_default_config_from_disk, load_effective_watchlist
    from storage import tenant_meta_store as tms
    from storage.mongo_ledger import MongoLedgerStore
    from storage.tenant_registry import get_tenant

    tid = tenant_id.strip().lower()
    if not get_tenant(tid):
        raise RuntimeError(f"tenant {tid!r} not found in registry — onboard first")

    default_cfg = _load_default_config_from_disk()
    initial = _initial_cash(default_cfg)
    store = MongoLedgerStore()

    result = {
        "tenant_id": tid,
        "scope": scope,
        "dry_run": dry_run,
        "initial_cash": initial,
    }

    if dry_run:
        result["planned"] = "dry-run only (pass --apply)"
        return result

    # Clean ledger (orders + positions + trade history)
    store.save_orders({"orders": []}, scope, tenant_id=tid)
    store.save_positions({"positions": {}}, scope, tenant_id=tid)
    store.save_trade_history(
        {
            "trades": [],
            "virtual_balance": initial,
            "realized_pnl": 0.0,
            "open_positions": 0,
            "total_pnl": 0.0,
        },
        scope,
        tenant_id=tid,
    )

    tenant_body = build_operator_like_tenant_config(default_cfg)
    tms.save_tenant_config(tid, tenant_body, default_cfg=default_cfg)

    with tenant_context(copy_watchlist_from, scope=scope):
        watchlist = list(load_effective_watchlist())
    if watchlist:
        tms.save_tenant_watchlist(tid, watchlist, default_cfg=default_cfg)

    # Registry: permissive limits matching operator demo trading
    dr = default_cfg.get("dry_run_defaults") or {}
    from storage.mongo_client import get_database

    db = get_database()
    db["tenants"].update_one(
        {"tenant_id": tid},
        {
            "$set": {
                "limits": {
                    "max_open_positions": int(default_cfg.get("max_open_positions", 40)),
                    "max_daily_trades": int(dr.get("max_daily_trades", 60)),
                    "max_daily_usdt": float(dr.get("max_daily_dca_usdt", 24_000)),
                    "allow_live": False,
                },
                "defaults": {
                    "trading_mode": "live",
                    "ledger_scope": scope,
                    "ui_language": "de",
                },
                "status": "active",
            }
        },
    )

    result.update(
        {
            "orders": 0,
            "positions": 0,
            "watchlist_coins": len(watchlist),
            "max_open_positions": tenant_body.get("max_open_positions"),
            "trading_mode": tenant_body.get("trading_mode"),
            "live_dry_run": (tenant_body.get("live") or {}).get("dry_run"),
        }
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tenant_id", help="e.g. henry")
    parser.add_argument("--scope", default="demo", help="Ledger scope (demo on Railway test)")
    parser.add_argument("--apply", action="store_true", help="Persist changes (default: dry-run)")
    parser.add_argument("--mongo-url", default=None, help="Railway MONGO_PUBLIC_URL override")
    args = parser.parse_args()

    os.environ.setdefault("MULTI_TENANT_ENABLED", "1")
    os.environ.setdefault("DEMO_MODE", "1")
    os.environ.setdefault("DEMO_LEDGER_BACKEND", "mongo")
    if args.mongo_url:
        os.environ["MONGO_URL"] = args.mongo_url
        os.environ["DEMO_ALLOW_REMOTE_MONGO"] = "1"
    meta = prepare_operator_mongo()
    if meta.get("pytest_isolated"):
        print("ERROR: pytest-isolated DB — set MONGO_URL for Railway")
        return 1

    out = setup_satellite_tenant(
        args.tenant_id,
        scope=args.scope,
        dry_run=not args.apply,
    )
    for key, val in out.items():
        print(f"{key}: {val}")
    if not args.apply:
        print("\nDry-run — re-run with --apply to persist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())