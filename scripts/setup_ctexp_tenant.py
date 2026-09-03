#!/usr/bin/env python3
"""Create/ensure isolated PAPER tenant ``ctexp`` for a live A/B vs default.

Copies CURRENT ``config.json`` from disk (not hardcoded) into
``tenant_configs.body`` with exactly one override: ``trading_mode: "paper"``.
That is the genuine paper-ledger literal (see ``core/config.py`` and
``core/simulated_trading.py``) — not ``live`` + ``dry_run`` simulated-live.

Default tenant is never written. This script is idempotent (check-then-create
/ update). Does not fabricate a Telegram ``owner_chat_id``.

Price-cycle gate (PR #260): ``iter_price_cycle_tenants`` includes a tenant
without ``owner_chat_id`` when ``telegram.headless=true``. This script sets
that flag. Notifications fall back to the operator chat, tagged ``[ctexp]``.
Optional: still bind a real chat via ``/start ctexp`` if you want a private
inbox. Do not invent a fake chat id.

  MONGO_PUBLIC_URL=... MONGODB_DB=xagent_test \\
    python3 scripts/setup_ctexp_tenant.py --dry-run
  ... --apply
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TENANT_ID = "ctexp"
SOURCE_TENANT = "default"
CONFIG_PATH = ROOT / "config.json"
WATCHLIST_PATH = ROOT / "data" / "watchlist.json"
EXPANSION_PATH = ROOT / "data" / "watchlist.dry_run_expansion.json"
if not WATCHLIST_PATH.exists():
    WATCHLIST_PATH = ROOT / "watchlist.json"
if not EXPANSION_PATH.exists():
    EXPANSION_PATH = ROOT / "watchlist.dry_run_expansion.json"

# Last-resort coins used by notifications/telegram_commands/onboarding_commands.py
_ONBOARD_DEFAULT_WATCHLIST = [
    {"symbol": "BTC/USDT", "active": True},
    {"symbol": "ETH/USDT", "active": True},
    {"symbol": "SOL/USDT", "active": True},
    {"symbol": "PEPE/USDT", "active": True},
]


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_disk_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise ValueError(f"{CONFIG_PATH} is not a JSON object")
    return cfg


def desired_tenant_config_body(disk_cfg: dict | None = None) -> dict:
    """config.json as-is, with only trading_mode flipped to paper."""
    base = disk_cfg if disk_cfg is not None else _load_disk_config()
    return _deep_merge(base, {"trading_mode": "paper"})


def _dedupe_coins(coins: list) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for c in coins:
        if not isinstance(c, dict):
            continue
        sym = str(c.get("symbol") or "").strip()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(dict(c))
    return out


def _coins_from_watchlist_file(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, dict):
        return [c for c in (data.get("coins") or []) if isinstance(c, dict)]
    if isinstance(data, list):
        return [c for c in data if isinstance(c, dict)]
    return []


def load_default_watchlist_coins(db) -> tuple[list[dict], str]:
    """Copy default's persisted universe (same sources onboard / setup_satellite use).

    Order:
      1. Mongo ``tenant_watchlists`` for tenant_id=default (if the running bot
         stored an effective list there).
      2. Repo ``watchlist.json`` + ``watchlist.dry_run_expansion.json`` — the
         files ``data_manager.load_watchlist`` / expansion merge read for the
         default tenant (default is file-backed, not mongo).
      3. Onboard's four-coin fallback.

    Does not call ``load_effective_watchlist`` (that path can hit Gate / WQE).
    Persisting the merged file list into ``tenant_watchlists`` for ctexp is
    what ``setup_satellite_tenant`` / ``_perform_onboard`` do after they load
    the default universe.
    """
    doc = db.tenant_watchlists.find_one({"tenant_id": SOURCE_TENANT})
    if doc and isinstance(doc.get("coins"), list) and doc["coins"]:
        coins = _dedupe_coins(doc["coins"])
        if coins:
            return coins, "mongo:tenant_watchlists:default"

    base = _coins_from_watchlist_file(WATCHLIST_PATH)
    expansion = _coins_from_watchlist_file(EXPANSION_PATH)
    merged = _dedupe_coins(base + expansion)
    if merged:
        src = "disk:watchlist.json"
        if expansion:
            src += "+watchlist.dry_run_expansion.json"
        return merged, src

    return list(_ONBOARD_DEFAULT_WATCHLIST), "onboard:DEFAULT_WATCHLIST"


def _owner_chat_id(tenant_doc: dict | None) -> str:
    if not tenant_doc:
        return ""
    return str((tenant_doc.get("telegram") or {}).get("owner_chat_id") or "").strip()


def _new_tenant_doc(disk_cfg: dict) -> dict:
    """Shape matches ``storage.tenant_registry.create_tenant`` (paper, no secrets)."""
    dr = disk_cfg.get("dry_run_defaults") or {}
    return {
        "tenant_id": TENANT_ID,
        "status": "active",
        "plan": "pro",
        "limits": {
            "max_open_positions": int(disk_cfg.get("max_open_positions", 36)),
            "max_daily_trades": int(dr.get("max_daily_trades", 60)),
            "max_daily_usdt": float(dr.get("max_daily_dca_usdt", 24_000)),
            "allow_live": False,
        },
        "features": ["basic"],
        "telegram": {
            "owner_chat_id": "",
            "headless": True,
            "bot_token_enc": "",
            "bot_token_ref": "env:TELEGRAM_BOT_TOKEN",
            "webhook_secret": secrets.token_urlsafe(32),
        },
        "exchange": {
            "gate": {
                "api_key_enc": "",
                "api_secret_enc": "",
                "testnet": False,
            }
        },
        "defaults": {
            "trading_mode": "paper",
            "ledger_scope": "paper",
            "ui_language": "de",
        },
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }


def _mongo_target() -> tuple[str, str] | tuple[None, None]:
    url = (
        os.environ.get("MONGO_PUBLIC_URL") or os.environ.get("MONGO_URL") or ""
    ).strip()
    db_name = (os.environ.get("MONGODB_DB") or "xagent_test").strip()
    if not url:
        print("ERROR: set MONGO_PUBLIC_URL or MONGO_URL", file=sys.stderr)
        return None, None
    if "railway.internal" in url:
        print(
            "ERROR: internal Railway host — use MONGO_PUBLIC_URL from MongoDB service",
            file=sys.stderr,
        )
        return None, None
    return url, db_name


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--tenant", default=TENANT_ID)
    args = ap.parse_args()
    apply = bool(args.apply)
    tid = str(args.tenant or TENANT_ID).strip().lower()

    url, db_name = _mongo_target()
    if not url:
        return 2

    disk_cfg = _load_disk_config()
    desired_body = desired_tenant_config_body(disk_cfg)

    from pymongo import MongoClient

    client = MongoClient(url, serverSelectionTimeoutMS=20000)
    db = client[db_name]

    existing_tenant = db.tenants.find_one({"tenant_id": tid})
    existing_cfg = db.tenant_configs.find_one({"tenant_id": tid})
    existing_wl = db.tenant_watchlists.find_one({"tenant_id": tid})
    watchlist, watchlist_source = load_default_watchlist_coins(db)

    existing_body = (
        existing_cfg.get("body")
        if existing_cfg and isinstance(existing_cfg.get("body"), dict)
        else None
    )
    existing_coins = (
        existing_wl.get("coins")
        if existing_wl and isinstance(existing_wl.get("coins"), list)
        else []
    )
    owner = _owner_chat_id(existing_tenant)

    before = {
        "registry": None
        if not existing_tenant
        else {
            "status": existing_tenant.get("status"),
            "owner_chat_id": owner or None,
            "defaults": existing_tenant.get("defaults"),
        },
        "trading_mode": None if existing_body is None else existing_body.get("trading_mode"),
        "config_present": existing_body is not None,
        "watchlist_coins": len(_dedupe_coins(existing_coins)),
        "owner_chat_id": owner or None,
    }
    after = {
        "registry": {
            "status": "active",
            "owner_chat_id": owner or None,
            "defaults": {
                "trading_mode": "paper",
                "ledger_scope": "paper",
                "ui_language": (
                    ((existing_tenant or {}).get("defaults") or {}).get("ui_language")
                    or "de"
                ),
            },
        },
        "trading_mode": "paper",
        "config_present": True,
        "watchlist_coins": len(watchlist),
        "watchlist_source": watchlist_source,
        "owner_chat_id": owner or None,
        "telegram_headless": True,
        "owner_chat_required_for_price_cycle": False,
        "manual_followup": (
            "PR #260: telegram.headless=true lets this tenant join the "
            "price cycle without owner_chat_id. Operator chat gets "
            f"[{tid}] tags. Optional: /start {tid} for a private inbox."
        ),
    }
    print(json.dumps({"tenant": tid, "before": before, "after": after}, indent=2))

    if not apply:
        print("dry-run only (pass --apply to write)")
        if not owner:
            print(
                "NOTE: no owner_chat_id — after --apply, telegram.headless=true "
                "is enough for the price cycle (PR #260).",
                file=sys.stderr,
            )
        return 0

    # Backup prior tenant_configs body before any write (Henry-script safety).
    bak_id = f"{tid}:setup:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    db.tenant_configs_backups.insert_one(
        {
            "_id": bak_id,
            "tenant_id": tid,
            "reason": "pre setup_ctexp_tenant (paper copy of config.json)",
            "backed_up_at": _now_iso(),
            "body": existing_body if existing_body is not None else {},
        }
    )

    if existing_tenant:
        set_fields: dict = {
            "status": "active",
            "defaults.trading_mode": "paper",
            "defaults.ledger_scope": "paper",
            "telegram.headless": True,
            "updated_at": _now_iso(),
        }
        if not ((existing_tenant.get("defaults") or {}).get("ui_language")):
            set_fields["defaults.ui_language"] = "de"
        db.tenants.update_one({"tenant_id": tid}, {"$set": set_fields})
    else:
        new_doc = _new_tenant_doc(disk_cfg)
        new_doc["tenant_id"] = tid
        db.tenants.insert_one(new_doc)

    db.tenant_configs.replace_one(
        {"tenant_id": tid},
        {
            "tenant_id": tid,
            "body": desired_body,
            "updated_at": _now_iso(),
            "note": "ctexp paper clone of config.json (trading_mode=paper only)",
        },
        upsert=True,
    )
    db.tenant_watchlists.replace_one(
        {"tenant_id": tid},
        {
            "tenant_id": tid,
            "coins": watchlist,
            "updated_at": _now_iso(),
            "source": watchlist_source,
        },
        upsert=True,
    )
    print(f"applied; backup_id={bak_id}")
    if not owner:
        print(
            f"headless=true — {tid} joins the price cycle without a Telegram "
            f"chat (PR #260). Optional: /start {tid} for a private inbox.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
