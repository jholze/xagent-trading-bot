#!/usr/bin/env python3
"""Backup, verify, reset and restore one or more tenant ledgers (epoch tooling).

Replaces the reset half of ``snapshot_and_fresh_start.py`` (see
``/workspace/umbau/reset_vorschlag.md`` gaps A–F):

* ``--tenant`` is mandatory. ``all``/``*`` are refused (gap B).
* The backup covers the ``demo`` **and** ``live`` ledger scopes. Live dry-run
  tenants keep ``peak_equity`` (high-water mark) in ``<tenant>:live`` (gap A).
* Raw ``orders_archive`` parts are saved (a reset deletes them in Mongo).
* Extra per-tenant collections are exported before any delete: single orders
  (``orders_v2``, ``order_day_stats``), the NAV curve (``portfolio_nav_daily``),
  every ``obs_*`` stream (120-day TTL, gap E) and memory (gap D).
* ``verify`` re-checks sha256, line counts, the ledger summary and, against
  Mongo, the document counts plus a canonical content sha. An in-place edit
  that keeps the count the same still fails (#796, residual of #759).
* ``reset`` and ``restore`` are dry runs unless ``--apply``. ``--apply`` also
  needs ``--confirm-writers-stopped`` (gap F) and a verified, fresh backup.
* ``--apply`` takes the bot's writer lease (#306) for every tenant/scope it
  touches, re-verifies the backup under the lease and refuses while any bot or
  side service still holds one, or when a ledger doc carries a newer fence than
  ours (wrong Redis or key prefix). Point ``REDIS_URL``/``REDIS_KEY_PREFIX`` at
  the bot's Redis.
* Every reset/restore writes an epoch marker to ``ledger_epochs`` and into the
  fresh trade_history documents.

Usage::

    python scripts/ledger_epoch.py backup  --tenant henry --tenant default
    python scripts/ledger_epoch.py verify  --dir auswertungen/epoch_backup_<stamp> --against-mongo
    python scripts/ledger_epoch.py reset   --dir <dir> --tenant henry --tenant default            # dry run
    python scripts/ledger_epoch.py reset   --dir <dir> --tenant henry --tenant default \
        --apply --confirm-writers-stopped
    python scripts/ledger_epoch.py restore --dir <dir> --tenant henry                              # dry run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOOL_VERSION = 1
LEDGER_SCOPES = ("demo", "live")
ORDERS = "orders"
POSITIONS = "positions"
TRADE_HISTORY = "trade_history"
ORDERS_ARCHIVE = "orders_archive"
LEDGER_COLLECTIONS = (ORDERS, POSITIONS, TRADE_HISTORY)
LEGACY_DEFAULT_TENANT = "default"
EPOCH_COLLECTION = "ledger_epochs"
OBS_PREFIX = "obs_"
EXTRA_COLLECTIONS = (
    "orders_v2",
    "order_day_stats",
    "portfolio_nav_daily",
    "grid_plans",
    "ledger_manual_reverts",
    "memory_trades",
    "memory_coin_profiles",
    "memory_lessons",
    "memory_path_stats",
    "gis_daily_monitor",
    "tenants",
    "tenant_configs",
    "tenant_configs_backups",
    "tenant_watchlists",
    EPOCH_COLLECTION,
)
REFUSED_TENANT_TOKENS = frozenset({"all", "*", "any", "every", "alle"})
_TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DEFAULT_MAX_BACKUP_AGE_HOURS = 6.0
LEASE_TTL_SEC = 300


class EpochError(RuntimeError):
    """Refusal or failed precondition. Nothing was written."""


# ---------------------------------------------------------------- helpers


def parse_tenants(raw: Iterable[str] | None) -> list[str]:
    """Explicit tenant list. Empty, ``all``/``*`` and odd ids are refused."""
    out: list[str] = []
    for item in raw or []:
        for part in str(item).split(","):
            tid = part.strip().lower()
            if not tid:
                continue
            if tid in REFUSED_TENANT_TOKENS:
                raise EpochError(
                    f"tenant '{part.strip()}' refused: name every tenant explicitly"
                )
            if not _TENANT_RE.match(tid):
                raise EpochError(f"invalid tenant id '{part.strip()}'")
            if tid not in out:
                out.append(tid)
    if not out:
        raise EpochError("--tenant is mandatory (repeat it per tenant)")
    return sorted(out)


def _json_util():
    from bson import json_util

    return json_util, json_util.JSONOptions(
        json_mode=json_util.JSONMode.CANONICAL, tz_aware=True
    )


def _dump_line(doc: dict) -> str:
    json_util, opts = _json_util()
    return json_util.dumps(doc, json_options=opts, sort_keys=True)


def _load_line(line: str) -> dict:
    json_util, opts = _json_util()
    return json_util.loads(line, json_options=opts)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_docs(docs: list[dict]) -> str:
    """Same bytes ``_write_jsonl`` would hash. Key order does not matter."""
    h = hashlib.sha256()
    for doc in docs:
        h.update((_dump_line(doc) + "\n").encode("utf-8"))
    return h.hexdigest()


def _write_jsonl(path: Path, docs: list[dict]) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for doc in docs:
            handle.write(_dump_line(doc) + "\n")
    return {"count": len(docs), "sha256": _sha256_file(path), "bytes": path.stat().st_size}


def _read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [_load_line(line) for line in handle if line.strip()]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(now: datetime) -> str:
    return now.strftime("%Y%m%dT%H%M%SZ")


def _ledger_id(tenant_id: str, scope: str) -> str:
    return f"{tenant_id}:{scope}"


def _archive_filter(tenant_id: str, scope: str) -> dict:
    prefix = re.escape(_ledger_id(tenant_id, scope) + ":part:")
    return {"_id": {"$regex": f"^{prefix}"}}


def _legacy_ids() -> list[str]:
    return list(LEDGER_SCOPES)


def _is_open_lot(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    try:
        return float(row.get("amount") or 0) > 0
    except (TypeError, ValueError):
        return False


def _float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def ledger_summary(docs: dict[str, dict | None], parts: list[dict]) -> dict:
    """Counts from raw ledger docs of one tenant/scope (same rule on backup and verify)."""
    orders_doc = docs.get(ORDERS) or {}
    hot = list(orders_doc.get("orders") or [])
    archived = sum(len(p.get("orders") or []) for p in parts)
    positions = (docs.get(POSITIONS) or {}).get("positions") or {}
    history = docs.get(TRADE_HISTORY) or {}
    return {
        "orders_hot": len(hot),
        "orders_archived": archived,
        "orders_total_rows": len(hot) + archived,
        "archive_parts": len(parts),
        "position_keys": len(positions) if isinstance(positions, dict) else 0,
        "open_lots": sum(1 for row in (positions.values() if isinstance(positions, dict) else []) if _is_open_lot(row)),
        "trades": len(history.get("trades") or []),
        "virtual_balance": _float(history.get("virtual_balance")),
        "peak_equity": _float(history.get("peak_equity")),
    }


# ---------------------------------------------------------------- plan of files


def _file_specs(db, tenants: list[str]) -> list[dict]:
    """Every backup file with its collection and Mongo filter (verify recounts these)."""
    specs: list[dict] = []
    for tid in tenants:
        for scope in LEDGER_SCOPES:
            for coll in LEDGER_COLLECTIONS:
                specs.append(
                    {
                        "path": f"ledger/{tid}/{scope}/{coll}.jsonl",
                        "collection": coll,
                        "filter": {"_id": _ledger_id(tid, scope)},
                        "kind": "ledger",
                        "tenant_id": tid,
                        "scope": scope,
                    }
                )
            specs.append(
                {
                    "path": f"ledger/{tid}/{scope}/{ORDERS_ARCHIVE}.jsonl",
                    "collection": ORDERS_ARCHIVE,
                    "filter": _archive_filter(tid, scope),
                    "kind": "archive",
                    "tenant_id": tid,
                    "scope": scope,
                }
            )
    if LEGACY_DEFAULT_TENANT in tenants:
        for coll in LEDGER_COLLECTIONS:
            specs.append(
                {
                    "path": f"ledger/_legacy/{coll}.jsonl",
                    "collection": coll,
                    "filter": {"_id": {"$in": _legacy_ids()}},
                    "kind": "legacy",
                }
            )
    names = set(db.list_collection_names())
    tenant_filter = {"tenant_id": {"$in": list(tenants)}}
    extras = list(EXTRA_COLLECTIONS) + sorted(n for n in names if n.startswith(OBS_PREFIX))
    for coll in extras:
        specs.append(
            {
                "path": f"extra/{coll}.jsonl",
                "collection": coll,
                "filter": tenant_filter,
                "kind": "obs" if coll.startswith(OBS_PREFIX) else "extra",
            }
        )
    return specs


def _filter_json(filt: dict) -> str:
    return json.dumps(filt, sort_keys=True)


def _filter_from_json(text: str) -> dict:
    return json.loads(text)


# ---------------------------------------------------------------- backup


def backup(db, tenants: list[str], out_dir: Path, *, now: datetime | None = None) -> dict:
    """Read-only export. Never writes to Mongo."""
    now = now or _now()
    tenants = parse_tenants(tenants)
    out_dir = Path(out_dir)
    if (out_dir / "manifest.json").exists():
        raise EpochError(f"{out_dir} already holds a backup; pick a new --out-dir")
    out_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict] = {}
    raw: dict[tuple, list[dict]] = {}
    for spec in _file_specs(db, tenants):
        docs = list(db[spec["collection"]].find(spec["filter"]).sort("_id", 1))
        info = _write_jsonl(out_dir / spec["path"], docs)
        info.update(
            {
                "collection": spec["collection"],
                "filter": _filter_json(spec["filter"]),
                "kind": spec["kind"],
            }
        )
        files[spec["path"]] = info
        if spec["kind"] in ("ledger", "archive"):
            raw[(spec["tenant_id"], spec["scope"], spec["collection"])] = docs
    summary = _summaries_from_raw(tenants, raw)
    manifest = {
        "tool": "scripts/ledger_epoch.py",
        "tool_version": TOOL_VERSION,
        "created_at": now.isoformat(),
        "database": db.name,
        "tenants": tenants,
        "scopes": list(LEDGER_SCOPES),
        "files": files,
        "summary": summary,
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    (out_dir / "manifest.sha256").write_text(
        f"{_sha256_file(manifest_path)}  manifest.json\n", encoding="utf-8"
    )
    return manifest


def _summaries_from_raw(tenants: list[str], raw: dict[tuple, list[dict]]) -> dict:
    out: dict[str, dict] = {}
    for tid in tenants:
        out[tid] = {}
        for scope in LEDGER_SCOPES:
            docs = {
                coll: (raw.get((tid, scope, coll)) or [None])[0] for coll in LEDGER_COLLECTIONS
            }
            parts = raw.get((tid, scope, ORDERS_ARCHIVE)) or []
            out[tid][scope] = ledger_summary(docs, parts)
    return out


def load_manifest(out_dir: Path) -> dict:
    path = Path(out_dir) / "manifest.json"
    if not path.exists():
        raise EpochError(f"no manifest.json in {out_dir}")
    return json.loads(path.read_text(encoding="utf-8"))


def manifest_digest(out_dir: Path) -> str:
    return _sha256_file(Path(out_dir) / "manifest.json")


# ---------------------------------------------------------------- verify


def verify(out_dir: Path, db=None, *, allow_growth: bool = False) -> dict:
    """Check files (sha256, lines, parse, summary) and optionally Mongo.

    Against Mongo, the document count and a canonical content sha are both
    checked. ``allow_growth`` only softens a higher count. An in-place edit
    (same count, different sha) is always a failure. ``reset`` calls this
    again under the writer lease.
    """
    out_dir = Path(out_dir)
    problems: list[str] = []
    warnings: list[str] = []
    try:
        manifest = load_manifest(out_dir)
    except EpochError as exc:
        return {"ok": False, "problems": [str(exc)], "warnings": [], "checked_files": 0}
    sha_file = out_dir / "manifest.sha256"
    if not sha_file.exists():
        problems.append("manifest.sha256 missing")
    else:
        expected = sha_file.read_text(encoding="utf-8").split()[0]
        if expected != manifest_digest(out_dir):
            problems.append("manifest.json checksum mismatch")
    raw: dict[tuple, list[dict]] = {}
    for rel, info in sorted((manifest.get("files") or {}).items()):
        path = out_dir / rel
        if not path.exists():
            problems.append(f"{rel}: missing")
            continue
        if _sha256_file(path) != info.get("sha256"):
            problems.append(f"{rel}: sha256 mismatch")
        try:
            docs = _read_jsonl(path)
        except Exception as exc:  # parse error is a broken backup
            problems.append(f"{rel}: unreadable ({exc})")
            continue
        if len(docs) != int(info.get("count", -1)):
            problems.append(f"{rel}: {len(docs)} lines, manifest says {info.get('count')}")
        parts = rel.split("/")
        if info.get("kind") in ("ledger", "archive") and len(parts) == 4:
            raw[(parts[1], parts[2], info["collection"])] = docs
        if db is not None:
            live_docs = list(
                db[info["collection"]].find(_filter_from_json(info["filter"])).sort("_id", 1)
            )
            live = len(live_docs)
            backed = int(info.get("count", 0))
            if live < backed:
                problems.append(f"{rel}: Mongo has {live} docs, backup {backed} (data lost since backup?)")
            elif live > backed:
                msg = f"{rel}: Mongo has {live} docs, backup {backed} (writers still running?)"
                (warnings if allow_growth else problems).append(msg)
            elif _sha256_docs(live_docs) != info.get("sha256"):
                problems.append(
                    f"{rel}: content sha256 mismatch (in-place edit; document count still {live})"
                )
    recomputed = _summaries_from_raw(list(manifest.get("tenants") or []), raw)
    if recomputed != (manifest.get("summary") or {}):
        problems.append("ledger summary recomputed from files differs from manifest")
    return {
        "ok": not problems,
        "problems": problems,
        "warnings": warnings,
        "checked_files": len(manifest.get("files") or {}),
        "summary": manifest.get("summary"),
        "tenants": manifest.get("tenants"),
        "created_at": manifest.get("created_at"),
    }


# ---------------------------------------------------------------- reset


def _fresh_docs(balance: float, marker: dict, now: datetime) -> dict:
    return {
        ORDERS: {"orders": [], "migrated_from_trades": False, "ledger_epoch": marker},
        POSITIONS: {"positions": {}, "ledger_epoch": marker},
        TRADE_HISTORY: {
            "trades": [],
            "virtual_balance": float(balance),
            "realized_pnl": 0.0,
            "open_positions": 0,
            "total_pnl": 0.0,
            "peak_equity": float(balance),
            "peak_equity_at": now.isoformat(),
            "ledger_epoch": marker,
        },
    }


class _OperatorLeases:
    """Writer leases for every (tenant, scope) a reset/restore touches.

    Acquiring them is the hard proof that no bot or side service is still
    writing: while a writer holds a lease, ``acquire`` fails and we refuse
    before anything is written. ``use`` installs the matching lease so the
    store stamps the right fence on each document.
    """

    def __init__(self, tenants: list[str], *, redis=None, ttl_sec: int = LEASE_TTL_SEC):
        from bus import writer_lease

        self._wl = writer_lease
        self.enabled = writer_lease.lease_enabled()
        self.tenants = tenants
        self.redis = redis
        self.ttl_sec = int(ttl_sec)
        self.leases: dict[tuple[str, str], Any] = {}

    def __enter__(self) -> "_OperatorLeases":
        if not self.enabled:
            return self
        try:
            for tid in self.tenants:
                for scope in LEDGER_SCOPES:
                    lease = self._wl.WriterLease(
                        tid, scope, redis=self.redis, ttl_sec=self.ttl_sec,
                        renew_sec=max(1, self.ttl_sec // 3),
                    )
                    self.leases[(tid, scope)] = lease
                    if not lease.acquire(start_renewer=True):
                        holder = lease.holder_label()
                        if holder.startswith("unavailable"):
                            raise EpochError(
                                f"writer lease {tid}:{scope}: Redis unreachable. Set REDIS_URL to the "
                                "bot's Redis; without the lease the store refuses every ledger write."
                            )
                        raise EpochError(
                            f"writer lease {tid}:{scope} is held by {holder}. Stop the bot and every "
                            "side service that writes the ledger, then retry. Nothing was written."
                        )
        except BaseException:
            self._release()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self._release()

    def _release(self) -> None:
        for lease in self.leases.values():
            try:
                lease.release()
            finally:
                lease.stop()
        self.leases.clear()

    def check_fences(self, db) -> None:
        """Refuse before any write when a ledger doc carries a newer fence than ours."""
        if not self.enabled:
            return
        for (tid, scope), lease in self.leases.items():
            ours = int(lease.fence or 0)
            for coll in LEDGER_COLLECTIONS:
                doc = db[coll].find_one({"_id": _ledger_id(tid, scope)}, {"fence": 1})
                theirs = (doc or {}).get("fence")
                if theirs is not None and int(theirs) > ours:
                    raise EpochError(
                        f"{coll} {tid}:{scope} has fence {theirs} > our lease fence {ours} "
                        "(Redis fence counter behind Mongo). Nothing was written."
                    )

    def stamp(self, doc: dict, tenant_id: str, scope: str) -> dict:
        """Carry our lease fence on a raw restore write, like the store does."""
        lease = self.leases.get((tenant_id, scope))
        if self.enabled and lease is not None and lease.fence is not None:
            doc["fence"] = int(lease.fence)
        return doc

    @contextmanager
    def use(self, tenant_id: str, scope: str):
        if not self.enabled:
            yield
            return
        previous = self._wl.swap_process_lease(self.leases[(tenant_id, scope)])
        try:
            yield
        finally:
            self._wl.swap_process_lease(previous)


def _check_backup_for(out_dir: Path, db, tenants: list[str], now: datetime, max_age_hours: float) -> dict:
    report = verify(out_dir, db)
    if not report["ok"]:
        raise EpochError("backup verify failed: " + "; ".join(report["problems"][:10]))
    missing = [t for t in tenants if t not in (report.get("tenants") or [])]
    if missing:
        raise EpochError(f"backup does not contain tenants {missing}")
    created = datetime.fromisoformat(str(report["created_at"]))
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if now - created > timedelta(hours=float(max_age_hours)):
        raise EpochError(
            f"backup is older than {max_age_hours} h ({report['created_at']}); take a new one"
        )
    return report


def plan_reset(tenants: list[str], balance: float) -> list[str]:
    steps = []
    for tid in tenants:
        steps.append(f"{tid}:demo orders -> [] (orders_archive parts for {tid}:demo deleted)")
        steps.append(f"{tid}:demo positions -> {{}}")
        steps.append(f"{tid}:demo trade_history -> trades [], cash {balance:,.2f}")
        steps.append(f"{tid}:live trade_history -> trades [], cash and peak_equity {balance:,.2f}")
        steps.append(f"{tid}:live orders/positions -> emptied if present")
        steps.append(f"{EPOCH_COLLECTION} += {tid}:<epoch> (kind reset)")
    if LEGACY_DEFAULT_TENANT in tenants:
        steps.append("legacy _id demo/live docs in orders/positions/trade_history -> deleted (backed up)")
    steps.append("untouched: orders_v2, order_day_stats, portfolio_nav_daily, obs_*, memory_*, other tenants")
    return steps


def reset(
    db,
    store,
    out_dir: Path,
    tenants: list[str],
    *,
    balance: float,
    expected_capital: dict[str, float] | None = None,
    apply: bool = False,
    confirm_writers_stopped: bool = False,
    allow_balance_mismatch: bool = False,
    max_age_hours: float = DEFAULT_MAX_BACKUP_AGE_HOURS,
    now: datetime | None = None,
    redis=None,
) -> dict:
    now = now or _now()
    tenants = parse_tenants(tenants)
    if not balance or float(balance) <= 0:
        raise EpochError("balance must be > 0")
    balance = float(balance)
    for tid, cap in (expected_capital or {}).items():
        if tid in tenants and cap is not None and abs(float(cap) - balance) > 0.005:
            if not allow_balance_mismatch:
                raise EpochError(
                    f"{tid}: config start capital is {cap:,.2f} but --balance is {balance:,.2f}. "
                    "Cash is replayed from config capital plus orders, so change the config first "
                    "(or pass --allow-balance-mismatch)."
                )
    report = _check_backup_for(out_dir, db, tenants, now, max_age_hours)
    result = {
        "dry_run": not apply,
        "tenants": tenants,
        "balance": balance,
        "plan": plan_reset(tenants, balance),
        "backup": str(out_dir),
        "backup_summary": {t: report["summary"][t] for t in tenants},
    }
    if not apply:
        return result
    if not confirm_writers_stopped:
        raise EpochError("--apply needs --confirm-writers-stopped (stop bot and side services first)")
    from storage.mongo_client import assert_safe_dev_db_mutation

    assert_safe_dev_db_mutation(db.name, action="ledger epoch reset")
    with _OperatorLeases(tenants, redis=redis) as leases:
        # Re-verify while we hold every lease: nothing can have written since.
        report = _check_backup_for(out_dir, db, tenants, now, max_age_hours)
        leases.check_fences(db)
        try:
            written, epoch_id, legacy_removed = _apply_reset(
                db, store, out_dir, tenants, balance, report, leases, now
            )
        except EpochError:
            raise
        except Exception as exc:
            raise EpochError(
                f"reset failed midway ({type(exc).__name__}: {exc}). The ledger may be partial; "
                f"put it back with: ledger_epoch.py restore --dir {out_dir} "
                + " ".join(f"--tenant {t}" for t in tenants)
                + " --apply --confirm-writers-stopped"
            ) from exc
    result.update({"epoch_id": epoch_id, "epochs": written, "legacy_removed": legacy_removed})
    return result


def _apply_reset(db, store, out_dir, tenants, balance, report, leases, now):
    epoch_id = f"e{_stamp(now)}"
    digest = manifest_digest(out_dir)
    written = {}
    for tid in tenants:
        marker = {
            "epoch_id": epoch_id,
            "kind": "reset",
            "started_at": now.isoformat(),
            "initial_capital": balance,
            "backup_dir": str(out_dir),
            "backup_manifest_sha256": digest,
        }
        fresh = _fresh_docs(balance, marker, now)
        with leases.use(tid, "demo"):
            store.save_orders(dict(fresh[ORDERS]), "demo", tenant_id=tid)
            store.save_positions(dict(fresh[POSITIONS]), "demo", tenant_id=tid)
            store.save_trade_history(dict(fresh[TRADE_HISTORY]), "demo", tenant_id=tid)
        with leases.use(tid, "live"):
            store.save_trade_history(dict(fresh[TRADE_HISTORY]), "live", tenant_id=tid)
            live_parts = db[ORDERS_ARCHIVE].count_documents(_archive_filter(tid, "live"))
            if db[ORDERS].find_one({"_id": _ledger_id(tid, "live")}) or live_parts:
                store.save_orders(dict(fresh[ORDERS]), "live", tenant_id=tid)
            if db[POSITIONS].find_one({"_id": _ledger_id(tid, "live")}):
                store.save_positions(dict(fresh[POSITIONS]), "live", tenant_id=tid)
        for scope in LEDGER_SCOPES:
            leftover = db[ORDERS_ARCHIVE].count_documents(_archive_filter(tid, scope))
            if leftover:
                raise EpochError(f"{tid}:{scope}: {leftover} orders_archive parts survived the reset")
        db[EPOCH_COLLECTION].replace_one(
            {"_id": f"{tid}:{epoch_id}"},
            {
                "_id": f"{tid}:{epoch_id}",
                "tenant_id": tid,
                "previous": report["summary"][tid],
                **marker,
            },
            upsert=True,
        )
        written[tid] = epoch_id
    legacy_removed = []
    if LEGACY_DEFAULT_TENANT in tenants:
        for coll in LEDGER_COLLECTIONS:
            res = db[coll].delete_many({"_id": {"$in": _legacy_ids()}})
            if res.deleted_count:
                legacy_removed.append(f"{coll}:{res.deleted_count}")
    return written, epoch_id, legacy_removed


# ---------------------------------------------------------------- restore


def _strip_fence(doc: dict) -> dict:
    out = dict(doc)
    out.pop("fence", None)
    return out


def restore(
    db,
    out_dir: Path,
    tenants: list[str],
    *,
    apply: bool = False,
    confirm_writers_stopped: bool = False,
    include_extra: bool = False,
    now: datetime | None = None,
    redis=None,
) -> dict:
    """Put the backed-up ledger docs back. Extras only fill missing ids."""
    now = now or _now()
    tenants = parse_tenants(tenants)
    out_dir = Path(out_dir)
    report = verify(out_dir)
    if not report["ok"]:
        raise EpochError("backup verify failed: " + "; ".join(report["problems"][:10]))
    missing = [t for t in tenants if t not in (report.get("tenants") or [])]
    if missing:
        raise EpochError(f"backup does not contain tenants {missing}")
    manifest = load_manifest(out_dir)
    plan: list[str] = []
    for tid in tenants:
        for scope in LEDGER_SCOPES:
            s = manifest["summary"][tid][scope]
            plan.append(
                f"{tid}:{scope} <- backup (orders rows {s['orders_total_rows']}, "
                f"open lots {s['open_lots']}, archive parts {s['archive_parts']}, "
                f"peak {s['peak_equity']})"
            )
    if LEGACY_DEFAULT_TENANT in tenants:
        plan.append("legacy _id demo/live docs <- backup")
    if include_extra:
        plan.append("extra collections: insert ids missing in Mongo (no overwrite)")
    result = {"dry_run": not apply, "tenants": tenants, "plan": plan, "backup": str(out_dir)}
    if not apply:
        return result
    if not confirm_writers_stopped:
        raise EpochError("--apply needs --confirm-writers-stopped (stop bot and side services first)")
    from storage.mongo_client import assert_safe_dev_db_mutation

    assert_safe_dev_db_mutation(db.name, action="ledger epoch restore")
    with _OperatorLeases(tenants, redis=redis) as leases:
        leases.check_fences(db)
        restored = {}
        for tid in tenants:
            for scope in LEDGER_SCOPES:
                for coll in LEDGER_COLLECTIONS:
                    rel = f"ledger/{tid}/{scope}/{coll}.jsonl"
                    docs = _read_jsonl(out_dir / rel)
                    if docs:
                        doc = leases.stamp(_strip_fence(docs[0]), tid, scope)
                        db[coll].replace_one({"_id": doc["_id"]}, doc, upsert=True)
                    else:
                        db[coll].delete_one({"_id": _ledger_id(tid, scope)})
                parts = [_strip_fence(d) for d in _read_jsonl(out_dir / f"ledger/{tid}/{scope}/{ORDERS_ARCHIVE}.jsonl")]
                keep = {d["_id"] for d in parts}
                for doc in parts:
                    db[ORDERS_ARCHIVE].replace_one({"_id": doc["_id"]}, doc, upsert=True)
                for doc in db[ORDERS_ARCHIVE].find(_archive_filter(tid, scope), {"_id": 1}):
                    if doc["_id"] not in keep:
                        db[ORDERS_ARCHIVE].delete_one({"_id": doc["_id"]})
            epoch_id = f"r{_stamp(now)}"
            db[EPOCH_COLLECTION].replace_one(
                {"_id": f"{tid}:{epoch_id}"},
                {
                    "_id": f"{tid}:{epoch_id}",
                    "tenant_id": tid,
                    "epoch_id": epoch_id,
                    "kind": "restore",
                    "started_at": now.isoformat(),
                    "backup_dir": str(out_dir),
                    "backup_manifest_sha256": manifest_digest(out_dir),
                },
                upsert=True,
            )
            restored[tid] = epoch_id
        if LEGACY_DEFAULT_TENANT in tenants:
            for coll in LEDGER_COLLECTIONS:
                for doc in _read_jsonl(out_dir / f"ledger/_legacy/{coll}.jsonl"):
                    doc = _strip_fence(doc)
                    db[coll].replace_one({"_id": doc["_id"]}, doc, upsert=True)
        inserted = 0
        if include_extra:
            for rel, info in manifest["files"].items():
                if info.get("kind") not in ("extra", "obs") or info["collection"] == EPOCH_COLLECTION:
                    continue
                for doc in _read_jsonl(out_dir / rel):
                    if doc.get("tenant_id") not in tenants:
                        continue
                    res = db[info["collection"]].update_one(
                        {"_id": doc["_id"]}, {"$setOnInsert": doc}, upsert=True
                    )
                    if res.upserted_id is not None:
                        inserted += 1
    result.update({"epochs": restored, "extra_inserted": inserted})
    return result


# ---------------------------------------------------------------- CLI


def _expected_capital(tenants: list[str]) -> dict[str, float]:
    from core.portfolio_baseline import initial_capital
    from data_manager import load_config

    out = {}
    for tid in tenants:
        cfg = load_config(tenant_id=tid)
        out[tid] = float(initial_capital(scope="demo", config=cfg))
    return out


def _connect(args) -> tuple[Any, Any]:
    os.environ.setdefault("MULTI_TENANT_ENABLED", "1")
    os.environ.setdefault("DEMO_MODE", "1")
    os.environ.setdefault("DEMO_LEDGER_BACKEND", "mongo")
    if args.mongo_url:
        os.environ["MONGO_URL"] = args.mongo_url
        os.environ["DEMO_ALLOW_REMOTE_MONGO"] = "1"
    from data_manager import _load_default_config_from_disk
    from scripts.operator_mongo import prepare_operator_mongo
    from storage.mongo_client import get_database
    from storage.mongo_ledger import MongoLedgerStore

    meta = prepare_operator_mongo(mongo_url=args.mongo_url, db=args.db)
    if meta.get("pytest_isolated"):
        raise EpochError("pytest-isolated DB; set MONGO_URL")
    cfg = _load_default_config_from_disk()
    db = get_database(config=cfg)
    print(f"Mongo: db={db.name} host={meta.get('host')}")
    if args.cmd in ("reset", "restore") and getattr(args, "apply", False):
        from urllib.parse import urlparse

        from bus.redis_client import resolve_redis_url
        from bus.redis_keys import redis_key_prefix

        url = resolve_redis_url(cfg.get("architecture", {}).get("redis_url"))
        print(f"Writer lease: redis host={urlparse(url).hostname} key prefix={redis_key_prefix()}")
    return db, MongoLedgerStore()


def _print_report(rep: dict) -> None:
    for line in rep.get("plan") or []:
        print(f"  - {line}")
    for key in ("problems", "warnings"):
        for line in rep.get(key) or []:
            print(f"  {key[:-1]}: {line}")


def _confirm(tenants: list[str], action: str, yes: bool) -> bool:
    if yes:
        return True
    token = f"{action.upper()} {' '.join(tenants)}"
    return input(f"Type '{token}' to continue: ").strip() == token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mongo-url", default=None)
    parser.add_argument("--db", default=None, help="Database (default MONGODB_DB or xagent_test)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backup", help="read-only export")
    b.add_argument("--tenant", action="append", default=[])
    b.add_argument("--out-dir", default=None)

    v = sub.add_parser("verify", help="check a backup")
    v.add_argument("--dir", required=True)
    v.add_argument("--against-mongo", action="store_true")
    v.add_argument("--allow-growth", action="store_true")

    r = sub.add_parser("reset", help="fresh start from a verified backup (dry run by default)")
    r.add_argument("--dir", required=True)
    r.add_argument("--tenant", action="append", default=[])
    r.add_argument(
        "--balance",
        type=float,
        default=None,
        help="start capital; must equal every named tenant's config capital (run tenants with different capital separately)",
    )
    r.add_argument("--apply", action="store_true")
    r.add_argument("--confirm-writers-stopped", action="store_true")
    r.add_argument("--allow-balance-mismatch", action="store_true")
    r.add_argument("--max-backup-age-hours", type=float, default=DEFAULT_MAX_BACKUP_AGE_HOURS)
    r.add_argument("--yes", action="store_true")

    s = sub.add_parser("restore", help="put a backup back (dry run by default)")
    s.add_argument("--dir", required=True)
    s.add_argument("--tenant", action="append", default=[])
    s.add_argument("--include-extra", action="store_true")
    s.add_argument("--apply", action="store_true")
    s.add_argument("--confirm-writers-stopped", action="store_true")
    s.add_argument("--yes", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "verify" and not args.against_mongo:
            rep = verify(Path(args.dir))
        else:
            if args.cmd in ("backup", "reset", "restore"):
                tenants = parse_tenants(args.tenant)  # refuse before connecting
            db, store = _connect(args)
            if args.cmd == "backup":
                now = _now()
                out = Path(args.out_dir) if args.out_dir else ROOT / "auswertungen" / f"epoch_backup_{_stamp(now)}"
                manifest = backup(db, tenants, out, now=now)
                for tid, scopes in manifest["summary"].items():
                    for scope, row in scopes.items():
                        print(f"  {tid}:{scope} {row}")
                print(f"backup -> {out} ({len(manifest['files'])} files). Next: verify --against-mongo")
                return 0
            if args.cmd == "verify":
                rep = verify(Path(args.dir), db, allow_growth=args.allow_growth)
            elif args.cmd == "reset":
                expected = _expected_capital(tenants)
                balance = args.balance if args.balance is not None else expected[tenants[0]]
                rep = reset(
                    db, store, Path(args.dir), tenants, balance=balance, expected_capital=expected,
                    apply=False, max_age_hours=args.max_backup_age_hours,
                )
                _print_report(rep)
                if not args.apply:
                    print("DRY RUN. Nothing written. Add --apply --confirm-writers-stopped to execute.")
                    return 0
                if not _confirm(tenants, "reset", args.yes):
                    print("Aborted. Nothing written.")
                    return 1
                rep = reset(
                    db, store, Path(args.dir), tenants, balance=balance, expected_capital=expected,
                    apply=True, confirm_writers_stopped=args.confirm_writers_stopped,
                    allow_balance_mismatch=args.allow_balance_mismatch,
                    max_age_hours=args.max_backup_age_hours,
                )
                print(f"reset done, epoch {rep['epoch_id']}. Restart the bot and side services.")
                return 0
            else:
                rep = restore(db, Path(args.dir), tenants, apply=False, include_extra=args.include_extra)
                _print_report(rep)
                if not args.apply:
                    print("DRY RUN. Nothing written. Add --apply --confirm-writers-stopped to execute.")
                    return 0
                if not _confirm(tenants, "restore", args.yes):
                    print("Aborted. Nothing written.")
                    return 1
                rep = restore(
                    db, Path(args.dir), tenants, apply=True,
                    confirm_writers_stopped=args.confirm_writers_stopped,
                    include_extra=args.include_extra,
                )
                print(f"restore done: {rep['epochs']}. Restart the bot and side services.")
                return 0
        _print_report(rep)
        print("verify OK" if rep["ok"] else "verify FAILED")
        return 0 if rep["ok"] else 1
    except EpochError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
