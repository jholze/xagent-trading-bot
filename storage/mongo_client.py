"""MongoDB connection helpers for ledger storage."""

from __future__ import annotations

import os
from typing import Optional

from pymongo import MongoClient
from pymongo.database import Database

DEFAULT_URI = "mongodb://127.0.0.1:27017"
PROD_DB_NAME = "xagent"
TEST_DB_NAME = "xagent_test"

_client: Optional[MongoClient] = None


def mongo_config(config: dict | None = None) -> dict:
    if config is None:
        from data_manager import get_config

        config = get_config()
    arch = config.get("architecture", {}) or {}
    return dict(arch.get("mongodb", {}) or {})


def resolve_mongo_uri(config: dict | None = None) -> str:
    cfg = mongo_config(config)
    return (
        os.environ.get("MONGODB_URI")
        or cfg.get("uri")
        or DEFAULT_URI
    )


def resolve_database_name(*, test: bool = False, config: dict | None = None) -> str:
    if test:
        return os.environ.get("MONGODB_TEST_DB", TEST_DB_NAME)
    env_db = os.environ.get("MONGODB_DB")
    if env_db:
        return env_db
    cfg = mongo_config(config)
    return cfg.get("db_name") or PROD_DB_NAME


def get_client(config: dict | None = None) -> MongoClient:
    global _client
    uri = resolve_mongo_uri(config)
    if _client is None:
        _client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    return _client


def get_database(*, test: bool = False, config: dict | None = None) -> Database:
    client = get_client(config)
    return client[resolve_database_name(test=test, config=config)]


def ping_database(*, test: bool = False, config: dict | None = None) -> bool:
    db = get_database(test=test, config=config)
    db.command("ping")
    return True


def drop_database(*, test: bool = False, config: dict | None = None) -> None:
    name = resolve_database_name(test=test, config=config)
    get_client(config).drop_database(name)


def close_client() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None