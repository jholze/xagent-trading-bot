#!/usr/bin/env python3
"""Drop isolated pytest Mongo database (xagent_pytest only)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage.mongo_client import (
    DEV_DB_NAME,
    PROD_DB_NAME,
    TEST_DB_NAME,
    close_client,
    get_client,
    mongo_uri_host,
    ping_database,
    resolve_mongo_uri,
)


def _list_collections(db_name: str) -> list[str]:
    client = get_client()
    return sorted(client[db_name].list_collection_names())


def main() -> int:
    parser = argparse.ArgumentParser(
        description=f"Drop Mongo database '{TEST_DB_NAME}' (pytest isolation only)."
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm drop (required)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show target DB and collections without deleting",
    )
    args = parser.parse_args()

    target = TEST_DB_NAME
    if target in (DEV_DB_NAME, PROD_DB_NAME):
        print(f"ERROR: refusing unsafe target '{target}'", file=sys.stderr)
        return 1

    uri = resolve_mongo_uri()
    host = mongo_uri_host(uri)
    print(f"Mongo host: {host}")
    print(f"Target DB:  {target}")

    try:
        ping_database(test=True)
    except Exception as exc:
        print(f"ERROR: Mongo ping failed: {exc}", file=sys.stderr)
        return 1

    try:
        collections = _list_collections(target)
    except Exception as exc:
        print(f"NOTE: database '{target}' not found or empty ({exc})")
        collections = []

    if collections:
        print(f"Collections ({len(collections)}): {', '.join(collections)}")
    else:
        print("Collections: (none — already absent or empty)")

    if args.dry_run:
        print("Dry run — no changes made.")
        return 0

    if not args.yes:
        print("Refusing drop without --yes", file=sys.stderr)
        return 1

    client = get_client()
    client.drop_database(target)
    close_client()
    print(f"Dropped database '{target}' on {host}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())