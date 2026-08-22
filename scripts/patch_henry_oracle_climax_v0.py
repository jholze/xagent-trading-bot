#!/usr/bin/env python3
"""Patch paper tenant_configs for climax overlay + RelVol cap (staging).

default (operator) reads disk only — climax/RelVol 8 live in config.json.
henry: Mongo overlay (climax on, RelVol 8) because tenant body overrides disk.
ctexp: Mongo overlay climax OFF (disk is on; ctexp is not in the always-on pair).

  MONGO_PUBLIC_URL=... MONGODB_DB=xagent_test \\
    python3 scripts/patch_henry_oracle_climax_v0.py --tenants henry,ctexp
    python3 scripts/patch_henry_oracle_climax_v0.py --tenants henry,ctexp --apply

Kill: --disable --tenants henry --apply  (and config.json oracle_climax.enabled=false).
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

CLIMAX_ON = {
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
            "harvest_1h_max_pct": -1.0,
            "dump_15m_max_pct": -0.80,
            "harvest_min_gain_pct": 12.0,
            "tighten_trail_pct": 1.5,
            "_experiment": "feature/oracle-climax-two-stage",
            "_doc": (
                "2026-08-21: Oracle RISK_ON grind holds BB/TTP/TS; harvest BTC 1h ≤ −1.0%. "
                "Fusion RISK_OFF/CRASH never sells. Kill: enabled=false."
            ),
        },
        "indicator_regime": {
            "enabled": True,
            "trail_allow_rsi": True,
            "rsi_full_close": True,
            "tenants": ["default", "henry"],
            "_doc": "RSI SELL_FULL through exclusive; grind RSI +8. Kill: enabled=false.",
        },
    }
}

RELVOL_8 = {
    "gainer_relvol_shadow": {
        "max_open": 8,
        "_experiment_climax_v0": "2026-08-21: RelVol cap 4→8 with climax grind. Kill: 4.",
    }
}

CLIMAX_OFF = {"sell_policy": {"oracle_climax": {"enabled": False}}}

# Back-compat alias
EXPERIMENT = {**CLIMAX_ON, **RELVOL_8}
DISABLE = CLIMAX_OFF


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
    rv = body.get("gainer_relvol_shadow") or {}
    return {
        "climax_enabled": oc.get("enabled"),
        "harvest_min_gain_pct": oc.get("harvest_min_gain_pct"),
        "relvol_max_open": rv.get("max_open"),
    }


def _overlay_for(tenant: str, disable: bool) -> dict:
    if disable:
        if tenant == "ctexp":
            return CLIMAX_OFF
        return _deep_merge(CLIMAX_OFF, {"gainer_relvol_shadow": {"max_open": 4}})
    if tenant == "ctexp":
        return CLIMAX_OFF
    if tenant == "henry":
        return _deep_merge(CLIMAX_ON, RELVOL_8)
    return CLIMAX_ON


def _patch_one(db, tenant: str, *, apply: bool, disable: bool) -> int:
    if tenant == "default":
        print(json.dumps({"tenant": "default", "note": "disk only — no Mongo overlay"}, indent=2))
        return 0
    doc = db.tenant_configs.find_one({"tenant_id": tenant})
    if not doc or not isinstance(doc.get("body"), dict):
        print(f"ERROR: no tenant_configs body for {tenant}", file=sys.stderr)
        return 1
    body = dict(doc["body"])
    overlay = _overlay_for(tenant, disable)
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
        f"{tenant}:oracle_climax_v0:"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    db.tenant_configs_backups.insert_one(
        {
            "_id": bak_id,
            "tenant_id": tenant,
            "reason": "pre oracle-climax two-stage v0",
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
            "note": "oracle-climax two-stage v0",
        },
        upsert=True,
    )
    print(f"applied {tenant}; backup_id={bak_id}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--disable", action="store_true")
    ap.add_argument("--tenant", default="")
    ap.add_argument("--tenants", default="henry,ctexp")
    args = ap.parse_args()

    url = (os.environ.get("MONGO_PUBLIC_URL") or os.environ.get("MONGO_URL") or "").strip()
    db_name = (os.environ.get("MONGODB_DB") or "xagent_test").strip()
    if not url:
        print("ERROR: set MONGO_PUBLIC_URL or MONGO_URL", file=sys.stderr)
        return 2
    if "railway.internal" in url:
        print("ERROR: use MONGO_PUBLIC_URL (not internal host)", file=sys.stderr)
        return 2

    raw = args.tenant.strip() or args.tenants
    tenants = [t.strip() for t in raw.split(",") if t.strip()]
    if not tenants:
        print("ERROR: no tenants", file=sys.stderr)
        return 2

    from pymongo import MongoClient

    db = MongoClient(url, serverSelectionTimeoutMS=20000)[db_name]
    rc = 0
    for tid in tenants:
        rc = max(rc, _patch_one(db, tid, apply=bool(args.apply), disable=bool(args.disable)))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
