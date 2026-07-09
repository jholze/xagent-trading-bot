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
    """Clear pytest leaks and target operator ledger (default xagent_test)."""
    url = mongo_url or os.environ.get("MONGO_URL")
    if url and not is_local_mongo_uri(url):
        apply_operator_mongo_target(
            mongo_url=url,
            db=db or os.environ.get("MONGODB_DB") or DEV_DB_NAME,
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