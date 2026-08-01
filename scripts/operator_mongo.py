"""Shared env setup for Railway/operator Mongo access (never xagent_pytest)."""

from __future__ import annotations

import os

from storage.mongo_client import (
    DEV_DB_NAME,
    apply_operator_mongo_target,
    is_local_mongo_uri,
    operator_mongo_summary,
)


def prepare_operator_mongo(
    *,
    mongo_url: str | None = None,
    db: str | None = None,
) -> dict:
    """Clear pytest leaks and target operator ledger (default xagent_test).

    Idempotent: if already pinned to the same remote operator target, skip
    apply_operator_mongo_target so hot paths (Exit Radar snapshot) do not
    close the shared MongoClient on every call (#192).
    """
    url = mongo_url or os.environ.get("MONGO_URL")
    target_db = db or os.environ.get("MONGODB_DB") or DEV_DB_NAME
    if (
        url
        and not is_local_mongo_uri(url)
        and os.environ.get("FORCE_OPERATOR_MONGO") == "1"
        and os.environ.get("DEMO_ALLOW_REMOTE_MONGO") == "1"
        and not os.environ.get("PYTEST_RUNNING")
        and not os.environ.get("PYTEST_CURRENT_TEST")
        and os.environ.get("MONGO_URL") == url
        and os.environ.get("MONGODB_DB") == target_db
    ):
        return operator_mongo_summary()

    if url and not is_local_mongo_uri(url):
        apply_operator_mongo_target(
            mongo_url=url,
            db=target_db,
        )
    else:
        os.environ.pop("PYTEST_RUNNING", None)
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        if db:
            os.environ["MONGODB_DB"] = db
        os.environ.setdefault("MONGODB_DB", DEV_DB_NAME)
        os.environ.setdefault("DEMO_MODE", "1")
        os.environ.setdefault("DEMO_LEDGER_BACKEND", "mongo")
    return operator_mongo_summary()