#!/usr/bin/env python3
"""Full snapshot (Mongo tenants + JSON) then optional fresh ledger reset.

Backs up:
  - Per-tenant demo ledgers (orders, positions, trade_history)
  - tenants registry, tenant_configs, tenant_watchlists
  - All *.demo.json under repo root + data/*.json

Reset (--apply):
  - Empty ledgers for each active tenant (default + henry by default)
  - Remove legacy scope-only Mongo docs (demo)
  - Rewrite ledger *.demo.json to match fresh cash balance
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCOPE = "demo"
DEFAULT_TENANTS = ["default", "henry"]
LEDGER_DEMO_JSON = (
    "orders.demo.json",
    "positions.demo.json",
    "trade_history.demo.json",
    "live_trade_history.demo.json",
)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _initial_cash(cfg: dict) -> float:
    live = cfg.get("live") or {}
    if live.get("simulated_balance_usdt"):
        return float(live["simulated_balance_usdt"])
    paper = (cfg.get("paper") or {}).get("initial_capital_usdt")
    if paper:
        return float(paper)
    return float(cfg.get("initial_capital_usdt", 100_000))


def _fresh_ledger_bundle(balance: float, tenant_id: str) -> dict:
    return {
        "orders": {
            "tenant_id": tenant_id,
            "ledger_scope": SCOPE,
            "orders": [],
            "migrated_from_trades": False,
        },
        "positions": {
            "tenant_id": tenant_id,
            "ledger_scope": SCOPE,
            "positions": {},
        },
        "trade_history": {
            "tenant_id": tenant_id,
            "ledger_scope": SCOPE,
            "trades": [],
            "virtual_balance": float(balance),
            "realized_pnl": 0.0,
            "open_positions": 0,
            "total_pnl": 0.0,
        },
    }


def _active_tenants(explicit: list[str]) -> list[str]:
    if explicit:
        return [t.strip().lower() for t in explicit if t.strip()]
    from storage.tenant_registry import list_active_tenants

    active = [t["tenant_id"] for t in list_active_tenants()]
    for tid in DEFAULT_TENANTS:
        if tid not in active:
            active.append(tid)
    return sorted(set(active))


def _export_tenant_ledger(store, tenant_id: str) -> dict:
    return {
        "tenant_id": tenant_id,
        "scope": SCOPE,
        "orders": store.load_orders(SCOPE, tenant_id=tenant_id),
        "positions": store.load_positions(SCOPE, tenant_id=tenant_id),
        "trade_history": store.load_trade_history(SCOPE, tenant_id=tenant_id),
    }


def _export_mongo_meta(default_cfg: dict) -> dict:
    from storage.mongo_client import get_database
    from storage.tenant_meta_store import TENANT_CONFIGS_COLL, TENANT_WATCHLISTS_COLL
    from storage.tenant_registry import TENANTS_COLLECTION

    db = get_database(config=default_cfg)
    tenants = list(db[TENANTS_COLLECTION].find({}, {"_id": 0}))
    configs = list(db[TENANT_CONFIGS_COLL].find({}, {"_id": 0}))
    watchlists = list(db[TENANT_WATCHLISTS_COLL].find({}, {"_id": 0}))
    return {
        "tenants": tenants,
        "tenant_configs": configs,
        "tenant_watchlists": watchlists,
    }


def _export_legacy_docs(default_cfg: dict) -> dict:
    from storage.mongo_ledger import (
        ORDERS_COLLECTION,
        POSITIONS_COLLECTION,
        TRADE_HISTORY_COLLECTION,
    )
    from storage.mongo_client import get_database

    db = get_database(config=default_cfg)
    out = {}
    for coll in (ORDERS_COLLECTION, POSITIONS_COLLECTION, TRADE_HISTORY_COLLECTION):
        doc = db[coll].find_one({"_id": SCOPE})
        if doc:
            payload = dict(doc)
            payload.pop("_id", None)
            out[coll] = payload
    return out


def _collect_json_paths() -> list[Path]:
    paths: list[Path] = []
    for pattern in ("*.demo.json", "data/*.json"):
        paths.extend(ROOT.glob(pattern))
    return sorted({p.resolve() for p in paths if p.is_file()})


def snapshot(
    *,
    tenants: list[str],
    out_dir: Path,
    default_cfg: dict,
) -> dict:
    from scripts.operator_mongo import prepare_operator_mongo
    from storage.mongo_ledger import MongoLedgerStore

    prepare_operator_mongo()
    store = MongoLedgerStore()
    out_dir.mkdir(parents=True, exist_ok=True)
    json_dir = out_dir / "json_files"
    json_dir.mkdir(exist_ok=True)
    mongo_dir = out_dir / "mongo"
    mongo_dir.mkdir(exist_ok=True)

    stats = {"tenants": {}, "json_files": 0, "out_dir": str(out_dir)}

    for tid in tenants:
        bundle = _export_tenant_ledger(store, tid)
        path = mongo_dir / f"{tid}_{SCOPE}_ledger.json"
        path.write_text(json.dumps(bundle, indent=2, default=str), encoding="utf-8")
        orders = len(bundle["orders"].get("orders") or [])
        pos = bundle["positions"].get("positions") or {}
        cash = float(bundle["trade_history"].get("virtual_balance") or 0)
        stats["tenants"][tid] = {
            "orders": orders,
            "positions": len(pos),
            "cash": cash,
        }

    legacy = _export_legacy_docs(default_cfg)
    if legacy:
        (mongo_dir / f"legacy_{SCOPE}_docs.json").write_text(
            json.dumps(legacy, indent=2, default=str),
            encoding="utf-8",
        )
        stats["legacy_docs"] = list(legacy.keys())

    meta = _export_mongo_meta(default_cfg)
    (mongo_dir / "tenant_meta.json").write_text(
        json.dumps(meta, indent=2, default=str),
        encoding="utf-8",
    )

    for src in _collect_json_paths():
        rel = src.relative_to(ROOT)
        dest = json_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        stats["json_files"] += 1

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": SCOPE,
        "tenants": tenants,
        "stats": stats,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    return stats


def _delete_legacy_scope_docs(default_cfg: dict) -> list[str]:
    from storage.mongo_ledger import (
        ORDERS_COLLECTION,
        POSITIONS_COLLECTION,
        TRADE_HISTORY_COLLECTION,
    )
    from storage.mongo_client import get_database

    db = get_database(config=default_cfg)
    removed = []
    for coll in (ORDERS_COLLECTION, POSITIONS_COLLECTION, TRADE_HISTORY_COLLECTION):
        res = db[coll].delete_one({"_id": SCOPE})
        if res.deleted_count:
            removed.append(coll)
    return removed


def reset_tenant_ledger(store, tenant_id: str, balance: float) -> dict:
    bundle = _fresh_ledger_bundle(balance, tenant_id)
    store.save_orders(bundle["orders"], SCOPE, tenant_id=tenant_id)
    store.save_positions(bundle["positions"], SCOPE, tenant_id=tenant_id)
    store.save_trade_history(bundle["trade_history"], SCOPE, tenant_id=tenant_id)
    return {"tenant_id": tenant_id, "orders": 0, "positions": 0, "cash": balance}


def _write_fresh_demo_json(balance: float) -> list[str]:
    updated = []
    fresh_history = {
        "trades": [],
        "ledger_scope": SCOPE,
        "open_positions": 0,
        "virtual_balance": float(balance),
        "realized_pnl": 0.0,
        "total_pnl": 0.0,
    }
    fresh_orders = {"ledger_scope": SCOPE, "orders": [], "migrated_from_trades": False}
    fresh_positions = {"ledger_scope": SCOPE, "positions": {}}

    targets = {
        "orders.demo.json": fresh_orders,
        "positions.demo.json": fresh_positions,
        "trade_history.demo.json": fresh_history,
        "live_trade_history.demo.json": dict(fresh_history),
    }
    for name, payload in targets.items():
        path = ROOT / name
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        updated.append(name)
    return updated


def fresh_start(
    *,
    tenants: list[str],
    balance: float,
    default_cfg: dict,
) -> dict:
    from scripts.operator_mongo import prepare_operator_mongo
    from storage.mongo_ledger import MongoLedgerStore
    from services.ledger_sync import sync_positions_on_startup
    from strategies.positions import bootstrap_positions, flush_positions
    from data_manager import reconcile_demo_trade_history_on_startup

    prepare_operator_mongo()
    store = MongoLedgerStore()
    results = {"tenants": {}, "legacy_removed": _delete_legacy_scope_docs(default_cfg)}

    for tid in tenants:
        results["tenants"][tid] = reset_tenant_ledger(store, tid, balance)

    bootstrap_positions(scope=SCOPE)
    flush_positions(scope=SCOPE, force=True)
    sync_positions_on_startup()
    reconcile_demo_trade_history_on_startup()

    results["json_reset"] = _write_fresh_demo_json(balance)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tenant",
        action="append",
        default=[],
        help="Tenant id (repeatable; default: active registry + default/henry)",
    )
    parser.add_argument(
        "--balance",
        type=float,
        default=None,
        help="Fresh virtual_balance USDT (default: from config, usually 100000)",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Snapshot directory (default: auswertungen/snapshot_<stamp>)",
    )
    parser.add_argument(
        "--apply-reset",
        action="store_true",
        help="After snapshot, reset Mongo + ledger JSON to empty start",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation for --apply-reset",
    )
    parser.add_argument("--mongo-url", default=None)
    args = parser.parse_args()

    os.environ.setdefault("MULTI_TENANT_ENABLED", "1")
    os.environ.setdefault("DEMO_MODE", "1")
    os.environ.setdefault("DEMO_LEDGER_BACKEND", "mongo")
    if args.mongo_url:
        os.environ["MONGO_URL"] = args.mongo_url
        os.environ["DEMO_ALLOW_REMOTE_MONGO"] = "1"

    from data_manager import _load_default_config_from_disk
    from scripts.operator_mongo import prepare_operator_mongo
    from storage.mongo_client import ping_database

    default_cfg = _load_default_config_from_disk()
    balance = float(args.balance if args.balance is not None else _initial_cash(default_cfg))

    meta = prepare_operator_mongo(mongo_url=args.mongo_url)
    if meta.get("pytest_isolated"):
        print("ERROR: pytest-isolated DB — set MONGO_URL", file=sys.stderr)
        return 1
    if not ping_database():
        print("ERROR: Mongo ping failed", file=sys.stderr)
        return 1

    tenants = _active_tenants(args.tenant)
    out_dir = Path(args.out_dir) if args.out_dir else ROOT / "auswertungen" / f"snapshot_{_stamp()}"

    print(f"Mongo: db={meta.get('database')} host={meta.get('host')}")
    print(f"Tenants: {', '.join(tenants)}")
    print(f"Snapshot → {out_dir}")

    stats = snapshot(tenants=tenants, out_dir=out_dir, default_cfg=default_cfg)
    for tid, row in stats["tenants"].items():
        print(
            f"  backed up {tid}: orders={row['orders']} "
            f"positions={row['positions']} cash=${row['cash']:,.2f}"
        )
    print(f"  json_files: {stats['json_files']}")
    print(f"manifest: {out_dir / 'manifest.json'}")

    if not args.apply_reset:
        print("\nSnapshot only. Re-run with --apply-reset to fresh-start ledgers.")
        return 0

    if not args.yes:
        print(f"\nWill reset tenants {tenants} → orders=0 positions=0 cash=${balance:,.0f}")
        if input("Type FRESH to continue: ").strip() != "FRESH":
            print("Aborted (snapshot kept).")
            return 1

    results = fresh_start(tenants=tenants, balance=balance, default_cfg=default_cfg)
    print("\n[reset] done")
    if results.get("legacy_removed"):
        print(f"  legacy docs removed: {results['legacy_removed']}")
    for tid, row in results["tenants"].items():
        print(
            f"  {tid}: orders={row['orders']} positions={row['positions']} "
            f"cash=${row['cash']:,.2f}"
        )
    print(f"  json: {', '.join(results.get('json_reset') or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())