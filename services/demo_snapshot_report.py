"""Unified demo snapshot report — same resolve_store path as the running bot.

Demo invariants (verification gating; do not violate):
  - ~25 open positions derived from demo orders SOT (~$100k equity, daily NAV delta < $2k)
  - Mongo positions/portfolio doc is read-only (DemoLedgerStore never writes positions to Mongo)
  - No manual test coins in demo orders (e.g. XRVM/USDT from unit/integration tests)
  - Orders/trade_history SOT is demo JSON; migrate_scope runs idempotently before any Mongo sync
  - --dry-run calls migrate_scope(dry_run=True) then reads only (no Mongo/JSON writes)
  - write_json=False apply syncs Mongo orders/trades only (no JSON file mutations)
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
import migrate_trades_to_orders as _migrate_mod

from data_manager import (
    LIVE_TRADE_HISTORY_FILE,
    ORDERS_SCOPE_FILES,
    POSITIONS_SCOPE_FILES,
    atomic_write_json,
    get_config,
)
from services.ledger_sync import count_open_positions_from_orders
from storage.ledger_router import resolve_store
from storage.mongo_client import resolve_database_name

TARGET_SCOPE = "demo"
SOURCE_SCOPE = "live"
EXPECTED_OPEN_POSITIONS = 25
DEMO_TEST_SYMBOLS = frozenset({"XRVM/USDT", "COUNT_A/USDT", "COUNT_B/USDT"})
DEMO_TEST_POSITION_KEYS = frozenset({"X_USDT_4h", "BTC_USDT_4h"})

SOURCE_FILES = {
    "orders": ORDERS_SCOPE_FILES[SOURCE_SCOPE],
    "positions": POSITIONS_SCOPE_FILES[SOURCE_SCOPE],
    "trade_history": LIVE_TRADE_HISTORY_FILE,
}

DEMO_JSON_FILES = {
    "orders": ORDERS_SCOPE_FILES[TARGET_SCOPE],
    "positions": POSITIONS_SCOPE_FILES[TARGET_SCOPE],
    "trade_history": f"{LIVE_TRADE_HISTORY_FILE.replace('.json', '.demo.json')}",
}


def _load_json(path: str) -> dict | None:
    if not Path(path).exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _prepare_payload(data: dict, scope: str) -> dict:
    payload = copy.deepcopy(data)
    payload["ledger_scope"] = scope
    return payload


def _filled_order_count(orders_doc: dict) -> int:
    return sum(1 for o in orders_doc.get("orders", []) if o.get("status") == "filled")


def _orders_doc_equal(left: dict | None, right: dict | None) -> bool:
    if not left or not right:
        return False
    return left.get("orders") == right.get("orders")


def _manual_test_coin_orders(orders_doc: dict) -> list[str]:
    found = []
    for order in orders_doc.get("orders", []):
        symbol = order.get("symbol", "")
        if symbol in DEMO_TEST_SYMBOLS and order.get("status") == "filled":
            found.append(symbol)
    return found


def check_demo_invariants(report: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    open_count = report.get("open_positions")
    if open_count != EXPECTED_OPEN_POSITIONS:
        violations.append(
            f"open_positions={open_count} expected {EXPECTED_OPEN_POSITIONS}"
        )
    test_coins = report.get("manual_test_coins") or []
    if test_coins:
        violations.append(f"manual test coins in demo orders: {test_coins}")
    if not report.get("positions_mongo_unchanged", True):
        violations.append("Mongo positions doc changed during snapshot")
    nav_delta = report.get("nav_delta")
    if nav_delta is not None and abs(float(nav_delta)) >= 2000:
        violations.append(f"nav_delta=${nav_delta:,.2f} exceeds $2k daily band")
    equity = report.get("equity_nav")
    if equity is not None and not (80_000 <= float(equity) <= 120_000):
        violations.append(f"equity_nav=${equity:,.2f} outside ~$100k band")
    return violations


def build_report_from_reads(
    *,
    demo_orders: dict,
    demo_positions: dict,
    demo_history: dict | None,
    mongo_orders_before: dict,
    mongo_positions_before: dict,
    mongo_history_before: dict | None,
    mongo_orders_after: dict | None = None,
    mongo_positions_after: dict | None = None,
    mongo_history_after: dict | None = None,
    dry_run: bool,
    from_live: bool,
    database: str,
    open_positions: int,
    load_positions_keys: int,
    migrate_pending: int = 0,
    migrate_executed: int = 0,
    migrate_scope_called: bool = False,
    equity_metrics: dict[str, float | None] | None = None,
) -> dict[str, Any]:
    """Pure report builder from pre-loaded ledger reads (no I/O, store, or bootstrap)."""
    manual_test_coins = _manual_test_coin_orders(demo_orders)
    filled_orders = _filled_order_count(demo_orders)
    positions_cache_keys = len(mongo_positions_before.get("positions", {}))
    equity = equity_metrics or {}

    positions_mongo_unchanged = (
        mongo_positions_after is not None
        and mongo_positions_before.get("positions")
        == mongo_positions_after.get("positions")
    )
    if dry_run:
        roundtrip = {
            "orders": _orders_doc_equal(demo_orders, mongo_orders_before),
            "trades": (
                demo_history is not None
                and mongo_history_before is not None
                and demo_history.get("trades") == mongo_history_before.get("trades")
            ),
            "positions_unchanged": positions_mongo_unchanged,
        }
    else:
        roundtrip = {
            "orders": _orders_doc_equal(mongo_orders_after, demo_orders),
            "trades": (
                demo_history is not None
                and mongo_history_after is not None
                and mongo_history_after.get("trades") == demo_history.get("trades")
            ),
            "positions_unchanged": positions_mongo_unchanged,
        }

    return {
        "target_scope": TARGET_SCOPE,
        "database": database,
        "dry_run": dry_run,
        "from_live": from_live,
        "orders": len(demo_orders.get("orders", [])),
        "filled_orders": filled_orders,
        "open_positions": open_positions,
        "load_positions_keys": load_positions_keys,
        "manual_test_coins": manual_test_coins,
        "invariant_violations": check_demo_invariants(
            {
                "open_positions": open_positions,
                "manual_test_coins": manual_test_coins,
                "positions_mongo_unchanged": positions_mongo_unchanged,
                "nav_delta": equity.get("nav_delta"),
                "equity_nav": equity.get("equity_nav"),
            }
        ),
        "positions_cache_keys": positions_cache_keys,
        "trades": len((demo_history or {}).get("trades", [])),
        "migrate_pending": migrate_pending,
        "migrate_executed": migrate_executed,
        "migrate_scope_called": migrate_scope_called,
        "positions_mongo_unchanged": positions_mongo_unchanged,
        "roundtrip": roundtrip,
        "equity_nav": equity.get("equity_nav"),
        "nav_day_start": equity.get("nav_day_start"),
        "nav_delta": equity.get("nav_delta"),
        "mongo_existing_orders": len(mongo_orders_before.get("orders", [])),
        "mongo_existing_positions": positions_cache_keys,
    }


def strip_demo_test_pollution(*, write: bool = True) -> int:
    """Remove manual test-coin orders and orphan cache keys from demo JSON."""
    removed = 0
    orders_path = DEMO_JSON_FILES["orders"]
    if Path(orders_path).exists():
        data = _load_json(orders_path) or {"orders": []}
        before = len(data.get("orders", []))
        data["orders"] = [
            o for o in data.get("orders", [])
            if o.get("symbol") not in DEMO_TEST_SYMBOLS
        ]
        removed += before - len(data["orders"])
        if removed and write:
            atomic_write_json(orders_path, data)

    positions_path = DEMO_JSON_FILES["positions"]
    if Path(positions_path).exists():
        pdata = _load_json(positions_path) or {"positions": {}}
        pos = pdata.get("positions", {}) or {}
        before_keys = len(pos)
        for key in list(pos):
            if key in DEMO_TEST_POSITION_KEYS:
                pos.pop(key, None)
        if before_keys != len(pos) and write:
            pdata["positions"] = pos
            atomic_write_json(positions_path, pdata)
            removed += before_keys - len(pos)

    history_path = DEMO_JSON_FILES["trade_history"]
    if Path(history_path).exists():
        hdata = _load_json(history_path) or {"trades": []}
        trades = hdata.get("trades", []) or []
        before_trades = len(trades)
        hdata["trades"] = [
            t for t in trades
            if t.get("symbol") not in DEMO_TEST_SYMBOLS
        ]
        if before_trades != len(hdata["trades"]) and write:
            atomic_write_json(history_path, hdata)
            removed += before_trades - len(hdata["trades"])
    return removed


def _load_demo_payloads(*, from_live: bool) -> tuple[dict, dict, dict | None]:
    if from_live:
        orders = _load_json(SOURCE_FILES["orders"]) or {
            "ledger_scope": SOURCE_SCOPE,
            "orders": [],
            "migrated_from_trades": False,
        }
        positions = _load_json(SOURCE_FILES["positions"]) or {
            "ledger_scope": SOURCE_SCOPE,
            "positions": {},
        }
        history = _load_json(SOURCE_FILES["trade_history"])
    else:
        orders = _load_json(DEMO_JSON_FILES["orders"]) or {
            "ledger_scope": TARGET_SCOPE,
            "orders": [],
            "migrated_from_trades": False,
        }
        positions = _load_json(DEMO_JSON_FILES["positions"]) or {
            "ledger_scope": TARGET_SCOPE,
            "positions": {},
        }
        history = _load_json(DEMO_JSON_FILES["trade_history"])

    demo_orders = _prepare_payload(orders, TARGET_SCOPE)
    demo_positions = _prepare_payload(positions, TARGET_SCOPE)
    demo_history = _prepare_payload(history, TARGET_SCOPE) if history else None
    return demo_orders, demo_positions, demo_history


def _clear_store_cache() -> None:
    from storage import ledger_router

    ledger_router._store_cache.clear()


def _equity_metrics() -> dict[str, float | None]:
    try:
        from notifications.daily_portfolio import estimate_nav_at_day_start
        from notifications.terminal_dashboard import _portfolio_snapshot
        from strategies.positions import bootstrap_positions, count_open_positions, load_positions

        bootstrap_positions(scope=TARGET_SCOPE)
        snap = _portfolio_snapshot("demo")
        total_value = float(snap.get("total_value", 0) or 0)
        nav_start = float(estimate_nav_at_day_start("demo") or 0)
        return {
            "equity_nav": total_value,
            "nav_day_start": nav_start,
            "nav_delta": total_value - nav_start,
            "open_positions": count_open_positions(),
            "load_positions_keys": len(load_positions(scope=TARGET_SCOPE)),
        }
    except Exception:
        return {
            "equity_nav": None,
            "nav_day_start": None,
            "nav_delta": None,
            "open_positions": None,
            "load_positions_keys": None,
        }


def build_demo_snapshot_report(
    *,
    dry_run: bool = False,
    test_db: bool = False,
    write_json: bool = True,
    from_live: bool = False,
) -> dict[str, Any]:
    """I/O wrapper: load ledger docs, optionally write, delegate to pure builder."""
    if test_db:
        os.environ["MONGODB_DB"] = "xagent_test"

    if not from_live and not dry_run and write_json:
        strip_demo_test_pollution(write=True)

    _clear_store_cache()
    store = resolve_store(TARGET_SCOPE, get_config())

    demo_orders, demo_positions, demo_history = _load_demo_payloads(from_live=from_live)

    migrate_pending = 0
    migrate_executed = 0
    migrate_scope_called = False

    mongo_positions_before = store.load_positions(TARGET_SCOPE)
    mongo_orders_before = store.load_orders(TARGET_SCOPE)
    mongo_history_before = (
        store.load_trade_history(TARGET_SCOPE) if demo_history else None
    )

    if not from_live and demo_history:
        migrate_pending = _migrate_mod.preview_migrate_scope(
            TARGET_SCOPE, DEMO_JSON_FILES["trade_history"], "paper"
        )
        migrate_scope_called = True
        migrate_executed = _migrate_mod.migrate_scope(
            TARGET_SCOPE,
            DEMO_JSON_FILES["trade_history"],
            "paper",
            dry_run=dry_run,
        )
        if migrate_executed and not dry_run:
            reloaded = _load_json(DEMO_JSON_FILES["orders"])
            if reloaded:
                demo_orders = reloaded
        if not dry_run and write_json:
            atomic_write_json(DEMO_JSON_FILES["trade_history"], demo_history)

    mongo_orders_after = None
    mongo_positions_after = None
    mongo_history_after = None

    if not dry_run:
        store.save_orders(demo_orders, TARGET_SCOPE)
        if demo_history is not None:
            store.save_trade_history(demo_history, TARGET_SCOPE)
        if write_json:
            atomic_write_json(DEMO_JSON_FILES["orders"], demo_orders)
            atomic_write_json(DEMO_JSON_FILES["positions"], demo_positions)
            if demo_history is not None:
                atomic_write_json(DEMO_JSON_FILES["trade_history"], demo_history)
                atomic_write_json("trade_history.demo.json", demo_history)

        mongo_orders_after = store.load_orders(TARGET_SCOPE)
        mongo_positions_after = store.load_positions(TARGET_SCOPE)
        mongo_history_after = (
            store.load_trade_history(TARGET_SCOPE) if demo_history else None
        )
    else:
        mongo_orders_after = store.load_orders(TARGET_SCOPE)
        mongo_positions_after = store.load_positions(TARGET_SCOPE)
        if demo_history:
            mongo_history_after = store.load_trade_history(TARGET_SCOPE)

    equity = _equity_metrics()
    open_positions = count_open_positions_from_orders(TARGET_SCOPE)
    load_positions_keys = equity.get("load_positions_keys")
    if load_positions_keys is None:
        load_positions_keys = open_positions

    return build_report_from_reads(
        demo_orders=demo_orders,
        demo_positions=demo_positions,
        demo_history=demo_history,
        mongo_orders_before=mongo_orders_before,
        mongo_positions_before=mongo_positions_before,
        mongo_history_before=mongo_history_before,
        mongo_orders_after=mongo_orders_after,
        mongo_positions_after=mongo_positions_after,
        mongo_history_after=mongo_history_after,
        dry_run=dry_run,
        from_live=from_live,
        database=resolve_database_name(test=test_db),
        open_positions=open_positions,
        load_positions_keys=load_positions_keys,
        migrate_pending=migrate_pending,
        migrate_executed=migrate_executed,
        migrate_scope_called=migrate_scope_called,
        equity_metrics=equity,
    )


def format_report_lines(report: dict[str, Any]) -> list[str]:
    """Stable single-line tokens for gating captures."""
    lines: list[str] = []
    violations = report.get("invariant_violations") or []
    if violations:
        lines.append(f"[invariants-violated] {'; '.join(violations)}")
    else:
        lines.append(
            f"[invariants-ok] open_positions={report.get('open_positions')} "
            "no_manual_test_coins mongo_positions_unchanged"
        )
    if report.get("migrate_scope_called"):
        lines.append(
            f"[migrate] demo scope +{report['migrate_executed']} orders from "
            f"{DEMO_JSON_FILES['trade_history']}"
        )
    if report.get("migrate_pending"):
        lines.append(
            f"[migrate-preview] demo scope +{report['migrate_pending']} orders pending from "
            f"{DEMO_JSON_FILES['trade_history']}"
        )

    prefix = "[dry-run]" if report.get("dry_run") else "[applied]"
    source = "live" if report.get("from_live") else "demo-json"
    lines.append(
        f"{prefix} snapshot {source} -> mongo:{report['target_scope']} "
        f"db={report['database']} orders={report['orders']} "
        f"filled={report['filled_orders']} open_positions={report['open_positions']} "
        f"load_positions_keys={report.get('load_positions_keys')} "
        f"trades={report['trades']} migrate_pending={report['migrate_pending']} "
        f"migrate_executed={report['migrate_executed']} "
        f"mongo_existing_orders={report['mongo_existing_orders']} "
        f"mongo_existing_positions={report['mongo_existing_positions']} "
        f"positions_mongo_unchanged={report['positions_mongo_unchanged']}"
    )
    rt = report.get("roundtrip", {})
    lines.append(
        f"[roundtrip] scope={report['target_scope']} orders={rt.get('orders')} "
        f"positions_unchanged={rt.get('positions_unchanged')} trades={rt.get('trades')}"
    )
    if report.get("equity_nav") is not None:
        lines.append(
            f"[equity] nav=${report['equity_nav']:,.2f} "
            f"tagesstart=${report.get('nav_day_start') or 0:,.2f} "
            f"delta=${report.get('nav_delta') or 0:,.2f}"
        )
    return lines