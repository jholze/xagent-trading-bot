#!/usr/bin/env python3
"""Remove pytest phantom symbols (SENSOR15, XENTRY15, TEST*) from demo ledger."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCOPE = "demo"


def _matches(symbol: str) -> bool:
    from core.test_symbols import is_phantom_test_symbol

    return is_phantom_test_symbol(symbol)


def _save_demo_positions_cache(pos_doc: dict) -> None:
    """Persist demo positions cache directly to Mongo (export-only JSON is not authoritative)."""
    from storage.mongo_ledger import MongoLedgerStore

    MongoLedgerStore().save_positions(pos_doc, SCOPE)


def purge_ledger(*, dry_run: bool = True) -> dict:
    from data_manager import (
        load_orders,
        load_positions_document,
        load_trade_history_document,
        save_orders,
        save_positions_document,
        save_trade_history_document,
    )
    from services.ledger_sync import sync_positions_on_startup
    from strategies.positions import bootstrap_positions, clear_positions_memory, flush_positions

    orders_doc = load_orders(SCOPE)
    orders = list(orders_doc.get("orders") or [])
    pos_doc = load_positions_document(SCOPE)
    positions = dict(pos_doc.get("positions") or {})
    hist = load_trade_history_document(SCOPE)
    trades = list(hist.get("trades") or [])

    def _key_to_symbol(key: str) -> str:
        base, _, _tf = key.rpartition("_")
        return base.replace("_", "/")

    removed_orders = [o for o in orders if _matches(o.get("symbol", ""))]
    kept_orders = [o for o in orders if not _matches(o.get("symbol", ""))]
    removed_pos_keys = [k for k in positions if _matches(_key_to_symbol(k))]

    removed_trades = [t for t in trades if _matches(t.get("symbol", ""))]
    kept_trades = [t for t in trades if not _matches(t.get("symbol", ""))]

    summary = {
        "orders_removed": len(removed_orders),
        "orders_kept": len(kept_orders),
        "positions_removed": len(removed_pos_keys),
        "trades_removed": len(removed_trades),
        "symbols": sorted({
            *(o.get("symbol") for o in removed_orders),
            *(t.get("symbol") for t in removed_trades),
            *(k.rsplit("_", 1)[0].replace("_", "/") + "/USDT" for k in removed_pos_keys),
        }),
    }

    if dry_run:
        return summary

    orders_doc["orders"] = kept_orders
    save_orders(orders_doc, SCOPE)
    for k in removed_pos_keys:
        positions.pop(k, None)
    pos_doc["positions"] = positions
    save_positions_document(pos_doc, SCOPE)
    _save_demo_positions_cache(pos_doc)
    hist["trades"] = kept_trades
    save_trade_history_document(hist, SCOPE)

    from data_manager import reconcile_demo_trade_history_on_startup

    reconcile_demo_trade_history_on_startup()
    clear_positions_memory()
    bootstrap_positions(scope=SCOPE)
    flush_positions(scope=SCOPE, force=True)
    from strategies.positions import _serialize_positions

    _save_demo_positions_cache(_serialize_positions())
    sync_positions_on_startup()
    return summary


def purge_json_file(path: Path, *, dry_run: bool) -> int:
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    removed = 0
    if "orders" in data and isinstance(data["orders"], list):
        before = len(data["orders"])
        data["orders"] = [o for o in data["orders"] if not _matches(o.get("symbol", ""))]
        removed += before - len(data["orders"])
    if "positions" in data and isinstance(data["positions"], dict):
        for k in list(data["positions"].keys()):
            base, _, _ = k.rpartition("_")
            sym = base.replace("_", "/")
            if _matches(sym):
                data["positions"].pop(k, None)
                removed += 1
    if "trades" in data and isinstance(data["trades"], list):
        before = len(data["trades"])
        data["trades"] = [t for t in data["trades"] if not _matches(t.get("symbol", ""))]
        removed += before - len(data["trades"])
    if not dry_run and removed:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return removed


def push_to_railway() -> None:
    import subprocess

    bundle = ROOT / "data" / "_phantom_purge_export.json"
    os.environ.setdefault("DEMO_MODE", "1")
    os.environ.setdefault("DEMO_LEDGER_BACKEND", "mongo")
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "demo_ledger_bundle.py"), "export"],
        check=True,
        stdout=bundle.open("w"),
        env=os.environ.copy(),
    )
    mongo_public = subprocess.check_output(
        ["railway", "variables", "--service", "MongoDB-mPbb", "--json"],
        text=True,
    )
    url = json.loads(mongo_public).get("MONGO_PUBLIC_URL", "")
    if not url:
        raise SystemExit("MONGO_PUBLIC_URL missing")
    env = os.environ.copy()
    env.pop("MONGODB_URI", None)
    env["MONGO_URL"] = url
    env["MONGODB_DB"] = "xagent_test"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "demo_ledger_bundle.py"), "import", "--file", str(bundle)],
        check=True,
        env=env,
    )
    bundle.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    parser.add_argument("--json", action="store_true", help="Also clean local JSON ledger files")
    parser.add_argument("--railway", action="store_true", help="Push cleaned ledger to Railway Mongo")
    args = parser.parse_args()

    os.environ.setdefault("DEMO_MODE", "1")
    os.environ.setdefault("DEMO_LEDGER_BACKEND", "mongo")

    summary = purge_ledger(dry_run=not args.apply)
    print(json.dumps(summary, indent=2))

    if args.json and args.apply:
        for rel in (
            "orders.demo.json",
            "positions.demo.json",
            "live_trade_history.demo.json",
            "positions.paper.json",
            "live_trade_history.json",
            "data/railway_seed/orders.json",
            "data/railway_seed/history.json",
        ):
            n = purge_json_file(ROOT / rel, dry_run=False)
            if n:
                print(f"cleaned {rel}: {n} entries")

    if args.railway and args.apply:
        push_to_railway()
        print("Railway import OK")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())