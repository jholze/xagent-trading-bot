#!/usr/bin/env python3
"""Safely repair exit_ladder_step on open positions (backup + dry-run by default).

Only mutates exit_ladder_step — never amount, peak_amount, sold_percent, or orders.

Usage:
  python3 scripts/repair_exit_ladder_steps.py
  python3 scripts/repair_exit_ladder_steps.py --apply --yes
  python3 scripts/repair_exit_ladder_steps.py --scope demo --tenant default
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BACKUP_DIR = ROOT / "auswertungen" / "ledger_repair_backups"
IMMUTABLE_POSITION_FIELDS = (
    "amount",
    "peak_amount",
    "sold_percent",
    "average_entry",
    "realized_pnl",
)


def _ensure_mongo() -> None:
    os.environ.setdefault("DEMO_MODE", "1")
    os.environ.setdefault("DEMO_LEDGER_BACKEND", "mongo")


def _position_fingerprint(pos: dict) -> dict:
    return {k: pos.get(k) for k in IMMUTABLE_POSITION_FIELDS}


def _load_positions(scope: str, tenant_id: str) -> tuple[dict, dict]:
    from data_manager import load_positions_document

    doc = load_positions_document(scope, tenant_id=tenant_id)
    return doc, dict(doc.get("positions") or {})


def _backup_path(scope: str, tenant_id: str) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    name = f"positions_{tenant_id}_{scope}_{ts}.json"
    return BACKUP_DIR / name


def plan_repairs(
    positions: dict,
    *,
    tiers: list[float] | None = None,
) -> list[dict]:
    from strategies.exit_ladder import reconcile_exit_ladder_step
    from strategies.positions import is_open_position

    rows = []
    for key, raw in sorted(positions.items()):
        if not is_open_position(raw):
            continue
        before = int(raw.get("exit_ladder_step") or 0)
        sold = float(raw.get("sold_percent") or 0)
        if sold <= 0 and before > 0:
            continue
        trial = copy.deepcopy(raw)
        after = reconcile_exit_ladder_step(trial, tiers)
        if after == before:
            continue
        rows.append({
            "key": key,
            "sold_percent": sold,
            "step_before": before,
            "step_after": after,
            "fingerprint_before": _position_fingerprint(raw),
            "fingerprint_after": _position_fingerprint(trial),
        })
    return rows


def apply_repairs(
    scope: str,
    tenant_id: str,
    rows: list[dict],
    *,
    dry_run: bool,
) -> dict:
    from storage.mongo_ledger import MongoLedgerStore
    from storage.tenant_keys import compound_ledger_id

    if not rows:
        return {"changed": 0, "dry_run": dry_run}

    store = MongoLedgerStore(test=False)
    doc_id = compound_ledger_id(tenant_id, scope)
    coll = store._collection("positions")
    doc = coll.find_one({"_id": doc_id}) or {}
    positions = dict(doc.get("positions") or {})

    for row in rows:
        key = row["key"]
        if key not in positions:
            raise RuntimeError(f"Position {key} vanished during repair — aborting")
        pos = positions[key]
        if _position_fingerprint(pos) != row["fingerprint_before"]:
            raise RuntimeError(
                f"Position {key} changed since plan (immutable fields differ) — aborting"
            )
        pos["exit_ladder_step"] = row["step_after"]
        if _position_fingerprint(pos) != row["fingerprint_after"]:
            raise RuntimeError(f"Repair corrupted immutable fields on {key} — aborting")

    if dry_run:
        return {"changed": len(rows), "dry_run": True, "doc_id": doc_id}

    coll.update_one(
        {"_id": doc_id},
        {"$set": {"positions": positions}},
    )
    return {"changed": len(rows), "dry_run": False, "doc_id": doc_id}


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair exit_ladder_step safely")
    parser.add_argument("--scope", default="demo")
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--apply", action="store_true", help="Persist changes to Mongo")
    parser.add_argument("--yes", action="store_true", help="Required with --apply")
    parser.add_argument("--no-backup", action="store_true", help="Skip backup (not recommended)")
    args = parser.parse_args()

    if args.apply and not args.yes:
        print("ERROR: --apply requires --yes")
        return 1

    _ensure_mongo()
    from strategies.exit_ladder import default_ladder_tiers

    doc, positions = _load_positions(args.scope, args.tenant)
    tiers = default_ladder_tiers()
    rows = plan_repairs(positions, tiers=tiers)

    print(f"Scope: {args.scope} | tenant: {args.tenant} | tiers: {tiers}")
    print(f"Open positions scanned: {sum(1 for p in positions.values() if float(p.get('amount') or 0) > 0)}")
    print(f"Repairs planned: {len(rows)}")
    for row in rows:
        print(
            f"  {row['key']:22s} sold={row['sold_percent']*100:5.1f}% "
            f"step {row['step_before']} → {row['step_after']}"
        )

    if not rows:
        print("Nothing to repair.")
        return 0

    backup_file = None
    if not args.no_backup:
        backup_file = _backup_path(args.scope, args.tenant)
        payload = {
            "scope": args.scope,
            "tenant_id": args.tenant,
            "backup_at": datetime.now(timezone.utc).isoformat(),
            "tiers": tiers,
            "positions_doc": doc,
        }
        backup_file.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"\nBackup written: {backup_file}")

    result = apply_repairs(args.scope, args.tenant, rows, dry_run=not args.apply)
    if result.get("dry_run"):
        print("\nDRY-RUN — no Mongo writes. Use --apply --yes to persist.")
    else:
        print(f"\nApplied {result['changed']} repair(s) to {result.get('doc_id')}")
        print("Restart xagent-test (or flush positions) to reload in-memory state.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())