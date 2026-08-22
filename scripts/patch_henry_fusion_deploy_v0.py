#!/usr/bin/env python3
"""Patch henry Mongo overlay so cash_policy.size_mult_deploy follows disk 0.80.

default (operator) reads disk only — size_mult_deploy 0.80 lives in config.json.
henry: tenant body deep-merges onto disk and may pin size_mult_deploy=1.0.
ctexp: leave 1.0 (not in the always-on pair).

Fusion keep-size is disk architecture + tenant list default,henry (no Mongo needed).

  MONGO_PUBLIC_URL=... MONGODB_DB=xagent_test \\
    python3 scripts/patch_henry_fusion_deploy_v0.py
    python3 scripts/patch_henry_fusion_deploy_v0.py --apply

Kill: --disable --apply  (and config.json size_mult_deploy=1.0,
      architecture.fusion_oracle_risk_on_keep_size=false).
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

DEPLOY_ON = {"risk": {"cash_policy": {"size_mult_deploy": 0.80}}}
DEPLOY_OFF = {"risk": {"cash_policy": {"size_mult_deploy": 1.0}}}


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _snapshot(body: dict) -> dict:
    cp = ((body.get("risk") or {}).get("cash_policy") or {})
    arch = body.get("architecture") or {}
    return {
        "size_mult_deploy": cp.get("size_mult_deploy"),
        "fusion_oracle_risk_on_keep_size": arch.get("fusion_oracle_risk_on_keep_size"),
    }


def _patch_one(db, tenant: str, *, apply: bool, disable: bool) -> int:
    if tenant == "default":
        print(json.dumps({"tenant": "default", "note": "disk only — no Mongo overlay"}, indent=2))
        return 0
    if tenant != "henry":
        print(json.dumps({"tenant": tenant, "note": "skip — not henry"}, indent=2))
        return 0
    doc = db.tenant_configs.find_one({"tenant_id": tenant})
    if not doc or not isinstance(doc.get("body"), dict):
        print(f"ERROR: no tenant_configs body for {tenant}", file=sys.stderr)
        return 1
    body = dict(doc["body"])
    overlay = DEPLOY_OFF if disable else DEPLOY_ON
    new_body = _deep_merge(body, overlay)
    print(
        json.dumps(
            {
                "tenant": tenant,
                "disable": bool(disable),
                "before": _snapshot(body),
                "after": _snapshot(new_body),
            },
            indent=2,
        )
    )
    if not apply:
        print(f"dry-run only ({tenant}; pass --apply)")
        return 0
    bak_id = (
        f"{tenant}:fusion_deploy_v0:"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    db.tenant_configs_backups.insert_one(
        {
            "_id": bak_id,
            "tenant_id": tenant,
            "reason": "pre fusion-oracle-keep-size + size_mult_deploy 0.80",
            "backed_up_at": datetime.now(timezone.utc).isoformat(),
            "body": body,
        }
    )
    db.tenant_configs.replace_one(
        {"tenant_id": tenant},
        {
            "tenant_id": tenant,
            "body": new_body,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "note": "fusion-oracle-keep-size + size_mult_deploy 0.80",
        },
        upsert=True,
    )
    print(f"applied {tenant}; backup_id={bak_id}")
    return 0


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
    return _patch_one(db, args.tenant.strip() or "henry", apply=bool(args.apply), disable=bool(args.disable))


if __name__ == "__main__":
    raise SystemExit(main())
