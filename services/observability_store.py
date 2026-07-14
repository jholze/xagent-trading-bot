"""Persist decisions and position snapshots for stack comparison."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from logger import LOG_DIR, log

DECISIONS_LOG_FILE = os.path.join(LOG_DIR, "decisions.jsonl")
SNAPSHOTS_LOG_FILE = os.path.join(LOG_DIR, "position_snapshots.jsonl")

DECISIONS_COLLECTION = "bot_decisions"
SNAPSHOTS_COLLECTION = "position_snapshots"


def _obs_cfg() -> dict:
    try:
        from core.config import get_bot_config

        return get_bot_config().raw.get("observability") or {}
    except Exception:
        return {}


def mongo_sync_enabled() -> bool:
    return bool(_obs_cfg().get("decisions_mongo_sync", True))


def snapshots_enabled() -> bool:
    return bool(_obs_cfg().get("position_snapshots_enabled", True))


def _mongo_insert(collection: str, document: dict) -> None:
    if not mongo_sync_enabled() and collection == DECISIONS_COLLECTION:
        return
    if collection == SNAPSHOTS_COLLECTION and not snapshots_enabled():
        return
    try:
        from storage.mongo_client import assert_safe_dev_db_mutation, get_database

        db_name = get_database().name
        assert_safe_dev_db_mutation(db_name, action="write")
        get_database()[collection].insert_one(document)
    except Exception as exc:
        log(f"Observability mongo insert failed ({collection}): {exc}", "WARNING")


def append_jsonl(path: str, record: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def tail_jsonl(path: str | os.PathLike, limit: int = 50, *, chunk_size: int = 262_144) -> list[dict]:
    """Read the last *limit* JSONL records without scanning the whole file."""
    if limit <= 0:
        return []
    path = os.fspath(path)
    if not os.path.isfile(path):
        return []

    entries: list[dict] = []
    incomplete = b""
    with open(path, "rb") as f:
        pos = f.seek(0, os.SEEK_END)
        while pos > 0 and len(entries) < limit:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size) + incomplete
            lines = chunk.split(b"\n")
            incomplete = lines[0]
            for raw in reversed(lines[1:]):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entries.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
                if len(entries) >= limit:
                    break

    if incomplete.strip() and len(entries) < limit:
        try:
            entries.append(json.loads(incomplete.strip()))
        except json.JSONDecodeError:
            pass

    entries.reverse()
    return entries[-limit:]


def persist_decision(record: dict) -> None:
    """Mirror decision to Mongo when enabled (file write is via log_decision)."""
    if not mongo_sync_enabled():
        return
    doc = dict(record)
    doc.setdefault("recorded_at", datetime.now().isoformat())
    _mongo_insert(DECISIONS_COLLECTION, doc)


def persist_position_snapshot(snapshot: dict) -> None:
    if not snapshots_enabled():
        return
    snap = dict(snapshot)
    snap.setdefault("recorded_at", datetime.now().isoformat())
    append_jsonl(SNAPSHOTS_LOG_FILE, snap)
    if _obs_cfg().get("position_snapshots_mongo_sync", True):
        _mongo_insert(SNAPSHOTS_COLLECTION, snap)


def load_decisions(
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    bot_stack: str | None = None,
    paths: list[Path] | None = None,
) -> list[dict]:
    rows: list[dict] = []
    files = paths or [Path(DECISIONS_LOG_FILE)]
    for path in files:
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if bot_stack and rec.get("bot_stack") != bot_stack:
                    continue
                ts_raw = rec.get("timestamp") or rec.get("recorded_at")
                if ts_raw and since:
                    ts = datetime.fromisoformat(str(ts_raw).replace("Z", "")[:26])
                    if ts < since:
                        continue
                if ts_raw and until:
                    ts = datetime.fromisoformat(str(ts_raw).replace("Z", "")[:26])
                    if ts >= until:
                        continue
                rows.append(rec)
    return rows


def load_snapshots(
    *,
    since: datetime | None = None,
    bot_stack: str | None = None,
    paths: list[Path] | None = None,
) -> list[dict]:
    rows: list[dict] = []
    files = paths or [Path(SNAPSHOTS_LOG_FILE)]
    for path in files:
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if bot_stack and rec.get("bot_stack") != bot_stack:
                    continue
                ts_raw = rec.get("ts") or rec.get("recorded_at")
                if ts_raw and since:
                    ts = datetime.fromisoformat(str(ts_raw).replace("Z", "")[:26])
                    if ts < since:
                        continue
                rows.append(rec)
    return rows


def runtime_context(config_raw: dict | None = None) -> dict[str, Any]:
    from core.runtime_identity import get_runtime_identity
    from services.config_fingerprint import config_fingerprint

    identity = get_runtime_identity(config_raw)
    build = identity.get("build") or {}
    return {
        "bot_stack": identity.get("stack", "unknown"),
        "build_commit": build.get("commit", ""),
        "build_branch": build.get("branch", ""),
        "config_fingerprint": config_fingerprint(config_raw),
    }