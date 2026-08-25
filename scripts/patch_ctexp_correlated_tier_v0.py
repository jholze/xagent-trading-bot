#!/usr/bin/env python3
"""Patch ctexp tenant_configs for correlated-tier + stagnant-rotation v0.

Applies the experiment overlay on top of whatever ``ctexp`` already has
(from ``scripts/setup_ctexp_tenant.py``). Deep-merge only — does not replace
``sell_policy.correlated_tier.groups`` (us_stock / crypto_market trail overlay
+ drawdown thresholds stay inherited from base config.json).

Requires MONGO_PUBLIC_URL or MONGO_URL (public proxy from local) and MONGODB_DB=xagent_test.

  MONGO_PUBLIC_URL=... MONGODB_DB=xagent_test \\
    python3 scripts/patch_ctexp_correlated_tier_v0.py --dry-run
  ... --apply
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


_EXPERIMENT_CTEXP_V0 = (
    "Before: max_open_positions from base (config.json typically 36), "
    "sell_policy.correlated_tier.enabled=false, groups inherited as-is, "
    "rotation.stagnant_rotation_enabled=false, stagnant_slack_slots=2, "
    "stagnant_gain_pct=8.0, stagnant_idle_hours=24.0. "
    "After: max_open_positions=18, correlated_tier.enabled=true "
    "(groups us_stock/crypto_market untouched), "
    "stagnant_rotation_enabled=true, stagnant_slack_slots=8, "
    "stagnant_gain_pct=6.0, stagnant_idle_hours=12.0. "
    "Kill: revert via the tenant_configs_backups doc this script writes "
    "before applying, or re-run with an inverse overlay setting "
    "enabled: false / stagnant_rotation_enabled: false."
)

EXPERIMENT = {
    "max_open_positions": 18,
    "sell_policy": {
        "correlated_tier": {
            "enabled": True,
            "_experiment_ctexp_v0": _EXPERIMENT_CTEXP_V0,
        },
        "rotation": {
            "stagnant_rotation_enabled": True,
            "stagnant_slack_slots": 8,
            "stagnant_gain_pct": 6.0,
            "stagnant_idle_hours": 12.0,
            "_experiment_ctexp_v0": _EXPERIMENT_CTEXP_V0,
        },
    },
    "_experiment_ctexp_v0": _EXPERIMENT_CTEXP_V0,
    "_doc": _EXPERIMENT_CTEXP_V0,
}


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _snapshot(body: dict) -> dict:
    sell = body.get("sell_policy") or {}
    ct = sell.get("correlated_tier") or {}
    rot = sell.get("rotation") or {}
    groups = ct.get("groups") if isinstance(ct.get("groups"), dict) else {}
    return {
        "max_open_positions": body.get("max_open_positions"),
        "correlated_tier_enabled": ct.get("enabled"),
        "correlated_tier_group_keys": sorted(groups.keys()),
        "stagnant_rotation_enabled": rot.get("stagnant_rotation_enabled"),
        "stagnant_slack_slots": rot.get("stagnant_slack_slots"),
        "stagnant_gain_pct": rot.get("stagnant_gain_pct"),
        "stagnant_idle_hours": rot.get("stagnant_idle_hours"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--tenant", default="ctexp")
    args = ap.parse_args()
    apply = bool(args.apply)

    url = (
        os.environ.get("MONGO_PUBLIC_URL")
        or os.environ.get("MONGO_URL")
        or ""
    ).strip()
    db_name = (os.environ.get("MONGODB_DB") or "xagent_test").strip()
    if not url:
        print("ERROR: set MONGO_PUBLIC_URL or MONGO_URL", file=sys.stderr)
        return 2
    if "railway.internal" in url:
        print(
            "ERROR: internal Railway host — use MONGO_PUBLIC_URL from MongoDB service",
            file=sys.stderr,
        )
        return 2

    from pymongo import MongoClient

    client = MongoClient(url, serverSelectionTimeoutMS=20000)
    db = client[db_name]
    doc = db.tenant_configs.find_one({"tenant_id": args.tenant})
    if not doc or not isinstance(doc.get("body"), dict):
        print(
            f"ERROR: no tenant_configs body for {args.tenant} "
            "(run scripts/setup_ctexp_tenant.py --apply first)",
            file=sys.stderr,
        )
        return 1

    body = dict(doc["body"])
    before = _snapshot(body)
    new_body = _deep_merge(body, EXPERIMENT)
    after = _snapshot(new_body)
    print(json.dumps({"tenant": args.tenant, "before": before, "after": after}, indent=2))

    if not apply:
        print("dry-run only (pass --apply to write)")
        return 0

    # Backup prior body once per apply
    bak_coll = "tenant_configs_backups"
    bak_id = (
        f"{args.tenant}:correlated_tier_v0:"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    db[bak_coll].insert_one(
        {
            "_id": bak_id,
            "tenant_id": args.tenant,
            "reason": "pre correlated-tier + stagnant-rotation experiment v0",
            "backed_up_at": datetime.now(timezone.utc).isoformat(),
            "body": body,
        }
    )
    db.tenant_configs.replace_one(
        {"tenant_id": args.tenant},
        {
            "tenant_id": args.tenant,
            "body": new_body,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "note": "correlated-tier + stagnant-rotation experiment v0",
        },
        upsert=True,
    )
    print(f"applied; backup_id={bak_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
