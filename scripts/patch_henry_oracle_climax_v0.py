#!/usr/bin/env python3
"""Patch henry tenant_configs for oracle-climax two-stage overlay (staging).

Disk default is sell_policy.oracle_climax.enabled=false. This overlay turns it
on for one paper tenant. Fusion RISK_OFF/CRASH never sells. Locks/red lots skip.

  MONGO_PUBLIC_URL=... MONGODB_DB=xagent_test \\
    python3 scripts/patch_henry_oracle_climax_v0.py
  ... --apply

Kill: enabled=false (re-run with --disable --apply, or restore backup).
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
    "sell_policy": {
        "oracle_climax": {
            "enabled": True,
            "min_btc_24h_pct": 6.0,
            "min_eth_24h_pct": 10.0,
            "min_breadth_green": 0.70,
            "min_btc_4h_pct": 1.0,
            "min_trend_4h": 0.0,
            "stall_1h_max_pct": 0.40,
            "stall_1h_min_pct": -0.80,
            "harvest_1h_max_pct": -0.30,
            "dump_15m_max_pct": -0.80,
            "harvest_min_gain_pct": 12.0,
            "tighten_trail_pct": 1.5,
            "_experiment": "feature/oracle-climax-two-stage",
            "_doc": (
                "2026-08-21: Oracle RISK_ON grind holds BB/TTP runners; "
                "climax stall tightens trail; 1h/15m dump harvests lots ≥12%. "
                "Fusion RISK_OFF/CRASH never sells. Kill: enabled=false."
            ),
        }
    }
}

DISABLE = {
    "sell_policy": {
        "oracle_climax": {
            "enabled": False,
        }
    }
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
    oc = ((body.get("sell_policy") or {}).get("oracle_climax") or {})
    return {
        "enabled": oc.get("enabled"),
        "harvest_min_gain_pct": oc.get("harvest_min_gain_pct"),
        "tighten_trail_pct": oc.get("tighten_trail_pct"),
        "min_breadth_green": oc.get("min_breadth_green"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--disable", action="store_true")
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
    overlay = DISABLE if args.disable else EXPERIMENT
    new_body = _deep_merge(body, overlay)
    print(
        json.dumps(
            {
                "tenant": args.tenant,
                "disable": bool(args.disable),
                "before": _snapshot(body),
                "after": _snapshot(new_body),
            },
            indent=2,
        )
    )

    if not args.apply:
        print("dry-run only (pass --apply)")
        return 0

    bak_id = (
        f"{args.tenant}:oracle_climax_v0:"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    db.tenant_configs_backups.insert_one(
        {
            "_id": bak_id,
            "tenant_id": args.tenant,
            "reason": "pre oracle-climax two-stage v0",
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
            "note": "oracle-climax two-stage v0",
        },
        upsert=True,
    )
    print(f"applied; backup_id={bak_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
