#!/usr/bin/env python3
"""Patch henry tenant_configs for grid-share experiment v1 (staging).

Requires MONGO_PUBLIC_URL or MONGO_URL (public proxy from local) and MONGODB_DB=xagent_test.

  MONGO_PUBLIC_URL=... MONGODB_DB=xagent_test \\
    python3 scripts/patch_henry_grid_experiment_v1.py --dry-run
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


EXPERIMENT = {
    "strategy_allocator": {
        "default_grid_weight": 0.4,
        "default_momentum_weight": 0.6,
        "_experiment_grid_share_v1": (
            "2026-08-10: weight 0.6→0.4 so RANGING stable tends HYBRID not pure GRID. "
            "Kill: 0.6/0.4."
        ),
    },
    "sell_policy": {
        "rotation": {
            "prefer_full_close": False,
            "grid_profit_full_close": False,
            "_experiment_grid_share_v1": (
                "2026-08-10: disable grid full-close upgrades; trail/profit non-grid "
                "full-close stays. Kill: both true."
            ),
        }
    },
    "grid": {
        "sell_policy": {
            "min_sell_gain_pct": 1.0,
            "_min_sell_gain_pct_doc": (
                "Align henry with operator grid floor (was 0.0). Kill: 0.0."
            ),
        }
    },
}


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--tenant", default="henry")
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
        print(f"ERROR: no tenant_configs body for {args.tenant}", file=sys.stderr)
        return 1

    body = dict(doc["body"])
    before = {
        "grid_w": (body.get("strategy_allocator") or {}).get("default_grid_weight"),
        "mom_w": (body.get("strategy_allocator") or {}).get("default_momentum_weight"),
        "prefer_full": ((body.get("sell_policy") or {}).get("rotation") or {}).get(
            "prefer_full_close"
        ),
        "grid_full": ((body.get("sell_policy") or {}).get("rotation") or {}).get(
            "grid_profit_full_close"
        ),
        "min_sell": ((body.get("grid") or {}).get("sell_policy") or {}).get(
            "min_sell_gain_pct"
        ),
    }
    new_body = _deep_merge(body, EXPERIMENT)
    after = {
        "grid_w": (new_body.get("strategy_allocator") or {}).get("default_grid_weight"),
        "mom_w": (new_body.get("strategy_allocator") or {}).get("default_momentum_weight"),
        "prefer_full": ((new_body.get("sell_policy") or {}).get("rotation") or {}).get(
            "prefer_full_close"
        ),
        "grid_full": ((new_body.get("sell_policy") or {}).get("rotation") or {}).get(
            "grid_profit_full_close"
        ),
        "min_sell": ((new_body.get("grid") or {}).get("sell_policy") or {}).get(
            "min_sell_gain_pct"
        ),
    }
    print(json.dumps({"tenant": args.tenant, "before": before, "after": after}, indent=2))

    if not apply:
        print("dry-run only (pass --apply to write)")
        return 0

    # Backup prior body once per apply
    bak_coll = "tenant_configs_backups"
    bak_id = f"{args.tenant}:grid_share_v1:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    db[bak_coll].insert_one(
        {
            "_id": bak_id,
            "tenant_id": args.tenant,
            "reason": "pre grid-share experiment v1",
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
            "note": "grid-share experiment v1",
        },
        upsert=True,
    )
    print(f"applied; backup_id={bak_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
