#!/usr/bin/env python3
"""Fast local smoke: multi-tenant + demo scope (mirrors Railway staging)."""

from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

# Local pytest DB only — never Railway.
os.environ.pop("MONGO_URL", None)
os.environ.setdefault("MONGODB_URI", "mongodb://127.0.0.1:27017")
os.environ["MONGODB_DB"] = "xagent_pytest"
os.environ["MONGODB_TEST_DB"] = "xagent_pytest"
os.environ["MULTI_TENANT_ENABLED"] = "1"
os.environ["DEMO_MODE"] = "1"
os.environ["TELEGRAM_CHAT_ID"] = "operator-chat-111"


def _run_pytest() -> int:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/unit/test_tenant_routing.py",
        "tests/unit/test_tenant_demo_scope.py",
        "tests/unit/test_mongo_ledger_mt_split.py",
        "tests/unit/test_portfolio_tenant_isolation.py",
        "tests/unit/test_trending_watchlist_sync.py",
        "tests/unit/test_background_runtime.py",
        "tests/unit/test_cycle_summary.py",
        "tests/unit/test_trade_history_mt_read.py",
        "tests/unit/test_simulated_trading.py",
        "tests/unit/test_portfolio_cycle_cash_parity.py",
        "tests/unit/test_trading_service_simulated.py",
        "tests/unit/test_henry_tenant_orders.py",
        "tests/unit/test_repair_tenant_ledgers.py",
        "-q",
        "--tb=line",
    ]
    print("=== pytest (MT demo subset) ===")
    return subprocess.call(cmd, cwd=ROOT)


def _ledger_smoke() -> int:
    from core.tenant_context import DEFAULT_TENANT, tenant_context
    from core.tenant_routing import resolve_incoming_tenant
    from data_manager import load_orders, resolve_ledger_scope
    from storage.mongo_client import TEST_DB_NAME, close_client, drop_database
    from storage.mongo_ledger import MongoLedgerStore
    from storage.tenant_keys import compound_ledger_id
    from strategies.positions import load_positions, list_active_positions

    print("\n=== ledger smoke (xagent_pytest) ===")
    drop_database(test=True)
    store = MongoLedgerStore(test=True)
    store.save_orders(
        {
            "orders": [
                {
                    "id": "smoke-1",
                    "status": "filled",
                    "side": "buy",
                    "symbol": "ETH/USDT",
                    "timeframe": "4h",
                    "execution": {"price": 50.0, "amount": 2.0},
                    "timestamps": {"filled": "2026-01-02T00:00:00"},
                }
            ],
            "migrated_from_trades": False,
        },
        "demo",
        tenant_id=DEFAULT_TENANT,
    )
    # Legacy demo doc (operator read path under MT)
    store._collection("orders").replace_one(
        {"_id": "demo"},
        {
            "_id": "demo",
            "ledger_scope": "demo",
            "orders": [
                {
                    "id": "legacy-1",
                    "status": "filled",
                    "side": "buy",
                    "symbol": "ETH/USDT",
                    "timeframe": "4h",
                    "execution": {"price": 50.0, "amount": 2.0},
                    "timestamps": {"filled": "2026-01-02T00:00:00"},
                }
            ],
        },
        upsert=True,
    )
    store.save_positions(
        {
            "positions": {
                "ETH_USDT_4h": {
                    "amount": 2.0,
                    "average_entry": 50.0,
                    "peak_amount": 2.0,
                    "sold_percent": 0.0,
                }
            }
        },
        "demo",
        tenant_id=DEFAULT_TENANT,
    )
    store._collection("positions").replace_one(
        {"_id": compound_ledger_id("henry", "demo")},
        {
            "_id": compound_ledger_id("henry", "demo"),
            "tenant_id": "henry",
            "ledger_scope": "demo",
            "positions": {},
        },
        upsert=True,
    )
    store._db["tenants"].replace_one(
        {"tenant_id": "henry"},
        {
            "tenant_id": "henry",
            "status": "active",
            "telegram": {"owner_chat_id": "henry-chat-222"},
            "defaults": {"ledger_scope": "paper"},
        },
        upsert=True,
    )

    scope = resolve_ledger_scope()
    op_route = resolve_incoming_tenant(chat_id="operator-chat-111")
    henry_route = resolve_incoming_tenant(chat_id="henry-chat-222")

    checks: list[tuple[str, bool, str]] = []
    checks.append(("runtime_scope_demo", scope == "demo", f"scope={scope}"))
    checks.append(
        ("operator_route",
         op_route.tenant_id == DEFAULT_TENANT and op_route.scope == "demo",
         f"tenant={op_route.tenant_id} scope={op_route.scope}"),
    )
    checks.append(
        ("henry_route_demo_scope",
         not henry_route.rejected
         and henry_route.tenant_id == "henry"
         and henry_route.scope == "demo",
         f"tenant={henry_route.tenant_id} scope={henry_route.scope} rejected={henry_route.rejected}"),
    )

    with tenant_context(DEFAULT_TENANT, scope="demo"):
        orders = load_orders("demo", tenant_id=DEFAULT_TENANT).get("orders", [])
        load_positions(scope="demo", tenant_id=DEFAULT_TENANT)
        active = list_active_positions()
        checks.append(("operator_orders", len(orders) >= 1, f"count={len(orders)}"))
        checks.append(("operator_positions", len(active) == 1, f"open={len(active)}"))

    with tenant_context("henry", scope="demo"):
        h_orders = load_orders("demo", tenant_id="henry").get("orders", [])
        load_positions(scope="demo", tenant_id="henry")
        h_active = list_active_positions()
        checks.append(("henry_empty", len(h_orders) == 0 and len(h_active) == 0,
                       f"orders={len(h_orders)} open={len(h_active)}"))

    failed = False
    for name, ok, detail in checks:
        status = "OK" if ok else "FAIL"
        print(f"  [{status}] {name}: {detail}")
        failed = failed or not ok

    close_client()
    return 1 if failed else 0


def main() -> int:
    rc = _run_pytest()
    if rc != 0:
        return rc
    return _ledger_smoke()


if __name__ == "__main__":
    raise SystemExit(main())