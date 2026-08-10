#!/usr/bin/env python3
"""Patch henry tenant_configs for gainer-catch v1 (staging).

  MONGO_PUBLIC_URL=... MONGODB_DB=xagent_test \\
    python3 scripts/patch_henry_gainer_catch_v1.py --dry-run
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
    "gainer_universe": {
        "expand_inject_max": 25,
        "trade_max_with_expand": 60,
        "live_heat_max_pct": 45,
        "_experiment_gainer_catch_v1": (
            "2026-08-10: inject 25, expand 60, heat_max 45. Kill: 12/50/40."
        ),
    },
    "gainer_entry": {
        "max_open": 6,
        "max_buys_per_day": 10,
        "_experiment_gainer_catch_v1": (
            "2026-08-10: max_open 6, max_buys_per_day 10. Kill: 3/6."
        ),
    },
    "gainer_relvol_shadow": {
        "enabled": True,
        "mode": "shadow",
        "_experiment": "feature/gainer-relvol-shadow-v0",
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
    ap.add_argument("--tenant", default="henry")
    args = ap.parse_args()

    url = (os.environ.get("MONGO_PUBLIC_URL") or os.environ.get("MONGO_URL") or "").strip()
    db_name = (os.environ.get("MONGODB_DB") or "xagent_test").strip()
    if not url:
        print("ERROR: set MONGO_PUBLIC_URL or MONGO_URL", file=sys.stderr)
        return 2
    if "railway.internal" in url:
        print("ERROR: use MONGO_PUBLIC_URL (not internal host)", file=sys.stderr)
        return 2

    from pymongo import MongoClient

    db = MongoClient(url, serverSelectionTimeoutMS=20000)[db_name]
    doc = db.tenant_configs.find_one({"tenant_id": args.tenant})
    if not doc or not isinstance(doc.get("body"), dict):
        print(f"ERROR: no tenant_configs body for {args.tenant}", file=sys.stderr)
        return 1

    body = dict(doc["body"])
    before = {
        "inject": (body.get("gainer_universe") or {}).get("expand_inject_max"),
        "max_open": (body.get("gainer_entry") or {}).get("max_open"),
        "heat_max": (body.get("gainer_universe") or {}).get("live_heat_max_pct"),
        "buys_day": (body.get("gainer_entry") or {}).get("max_buys_per_day"),
    }
    new_body = _deep_merge(body, EXPERIMENT)
    after = {
        "inject": (new_body.get("gainer_universe") or {}).get("expand_inject_max"),
        "max_open": (new_body.get("gainer_entry") or {}).get("max_open"),
        "heat_max": (new_body.get("gainer_universe") or {}).get("live_heat_max_pct"),
        "buys_day": (new_body.get("gainer_entry") or {}).get("max_buys_per_day"),
        "relvol": (new_body.get("gainer_relvol_shadow") or {}).get("enabled"),
    }
    print(json.dumps({"tenant": args.tenant, "before": before, "after": after}, indent=2))

    if not args.apply:
        print("dry-run only (pass --apply)")
        return 0

    bak_id = (
        f"{args.tenant}:gainer_catch_v1:"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    db.tenant_configs_backups.insert_one(
        {
            "_id": bak_id,
            "tenant_id": args.tenant,
            "reason": "pre gainer-catch v1 + relvol shadow",
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
            "note": "gainer-catch v1 + relvol shadow",
        },
        upsert=True,
    )
    print(f"applied; backup_id={bak_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
