"""MongoDB connection helpers for ledger storage."""

from __future__ import annotations

import os
import threading
from typing import Optional

from pymongo import MongoClient
from pymongo.database import Database
from pymongo.errors import InvalidOperation

DEFAULT_URI = "mongodb://127.0.0.1:27017"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
PROD_DB_NAME = "xagent"
DEV_DB_NAME = "xagent_test"
TEST_DB_NAME = "xagent_pytest"

_client: Optional[MongoClient] = None
_client_uri: Optional[str] = None
_client_lock = threading.RLock()


def mongo_uri_host(uri: str) -> str:
    """Best-effort host extraction for logging and safety checks."""
    from urllib.parse import urlparse

    parsed = urlparse(uri)
    return (parsed.hostname or "unknown").strip("[]")


def is_local_mongo_uri(uri: str | None = None, *, config: dict | None = None) -> bool:
    uri = uri or resolve_mongo_uri(config)
    return mongo_uri_host(uri) in LOCAL_HOSTS


def is_railway_runtime() -> bool:
    return bool(
        os.environ.get("RAILWAY_DEPLOY")
        or os.environ.get("RAILWAY_ENVIRONMENT")
        or os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    )


def is_pytest_running() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or os.environ.get("PYTEST_RUNNING") == "1"


def use_isolated_pytest_database(config: dict | None = None) -> bool:
    """Use xagent_pytest only for isolated local pytest — never Railway/operator targets."""
    if not is_pytest_running():
        return False
    if os.environ.get("DEMO_ALLOW_REMOTE_MONGO") == "1":
        return False
    if os.environ.get("FORCE_OPERATOR_MONGO") == "1":
        return False
    mongo_url = os.environ.get("MONGO_URL")
    if mongo_url and not is_local_mongo_uri(mongo_url, config=config):
        return False
    explicit_db = os.environ.get("MONGODB_DB")
    if explicit_db and explicit_db not in (TEST_DB_NAME, ""):
        return False
    return True


def apply_operator_mongo_target(
    *,
    db: str | None = None,
    mongo_url: str | None = None,
    allow_remote: bool = True,
) -> str:
    """Pin process to operator/Railway ledger DB (xagent_test), not xagent_pytest."""
    os.environ.pop("PYTEST_RUNNING", None)
    os.environ.pop("PYTEST_CURRENT_TEST", None)
    os.environ["FORCE_OPERATOR_MONGO"] = "1"
    if allow_remote:
        os.environ["DEMO_ALLOW_REMOTE_MONGO"] = "1"
        os.environ.setdefault("ALLOW_DEV_DB_MUTATION", "1")
    if db:
        os.environ["MONGODB_DB"] = db
    elif not os.environ.get("MONGODB_DB"):
        os.environ["MONGODB_DB"] = DEV_DB_NAME
    if mongo_url:
        os.environ["MONGO_URL"] = mongo_url
        os.environ.pop("MONGODB_URI", None)
    os.environ.setdefault("DEMO_MODE", "1")
    os.environ.setdefault("DEMO_LEDGER_BACKEND", "mongo")
    close_client()
    return resolve_database_name(config=None)


def operator_mongo_summary(*, config: dict | None = None) -> dict:
    uri = resolve_mongo_uri(config)
    return {
        "db": resolve_database_name(config=config),
        "host": mongo_uri_host(uri),
        "pytest_isolated": use_isolated_pytest_database(config=config),
    }


def force_local_test_mongo(*, dev: bool = True) -> None:
    """Force localhost Mongo — dev bot uses xagent_test; pytest uses xagent_pytest."""
    os.environ["MONGODB_URI"] = DEFAULT_URI
    os.environ.pop("MONGO_URL", None)
    os.environ["MONGODB_TEST_DB"] = TEST_DB_NAME
    if dev and os.environ.get("PYTEST_RUNNING") != "1":
        os.environ.setdefault("MONGODB_DB", DEV_DB_NAME)
    close_client()


def assert_safe_mongo_drop(*, test: bool = False, config: dict | None = None) -> None:
    """Block drop_database against remote Mongo (e.g. Railway) from local machines."""
    if is_railway_runtime() or os.environ.get("ALLOW_REMOTE_MONGO_DROP") == "1":
        return
    uri = resolve_mongo_uri(config)
    if not is_local_mongo_uri(uri, config=config):
        host = mongo_uri_host(uri)
        raise RuntimeError(
            f"Refusing drop_database on remote MongoDB host '{host}'. "
            f"Unset MONGO_URL and use MONGODB_URI={DEFAULT_URI} for local tests. "
            f"See scripts/dev_local_mongo.sh"
        )


def mongo_config(config: dict | None = None) -> dict:
    if config is None:
        from data_manager import get_config

        config = get_config()
    arch = config.get("architecture", {}) or {}
    return dict(arch.get("mongodb", {}) or {})


def resolve_mongo_uri(config: dict | None = None) -> str:
    cfg = mongo_config(config)
    # Remote operator/Railway: MONGO_URL wins over leaked local MONGODB_URI.
    if os.environ.get("DEMO_ALLOW_REMOTE_MONGO") == "1" and os.environ.get("MONGO_URL"):
        return os.environ["MONGO_URL"]
    # Prefer explicit local URI over inherited shell MONGO_URL (local dev safety).
    if os.environ.get("MONGODB_URI"):
        return os.environ["MONGODB_URI"]
    if os.environ.get("MONGO_URL"):
        return os.environ["MONGO_URL"]
    if cfg.get("uri"):
        return str(cfg["uri"])
    return DEFAULT_URI


def resolve_database_name(*, test: bool = False, config: dict | None = None) -> str:
    if test:
        return os.environ.get("MONGODB_TEST_DB", TEST_DB_NAME)
    env_db = os.environ.get("MONGODB_DB")
    if env_db:
        return env_db
    cfg = mongo_config(config)
    return cfg.get("db_name") or PROD_DB_NAME


def _client_is_closed(client: MongoClient | None) -> bool:
    """Best-effort: PyMongo sets ``_closed`` after MongoClient.close()."""
    if client is None:
        return True
    return bool(getattr(client, "_closed", False))


def get_client(config: dict | None = None) -> MongoClient:
    """Return a process-wide MongoClient, reopening if closed or URI changed.

    Thread-safe. Concurrent callers never receive a client that is already closed
    at return time. Do **not** call close_client() from request hot-paths — that
    races Telegram portfolio threads with ``Cannot use MongoClient after close``.
    """
    global _client, _client_uri
    uri = resolve_mongo_uri(config)
    with _client_lock:
        if _client is not None and (not _client_is_closed(_client)) and _client_uri == uri:
            return _client
        if _client is not None:
            try:
                if not _client_is_closed(_client):
                    _client.close()
            except Exception:
                pass
            _client = None
            _client_uri = None
        _client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        _client_uri = uri
        return _client


def get_database(*, test: bool = False, config: dict | None = None) -> Database:
    """Return a live database handle; reopen once if the client was closed.

    Transient network errors no longer call ``close_client()`` — that raced
    concurrent portfolio/ledger threads with
    ``Cannot use MongoClient after close``.
    """
    name = resolve_database_name(test=test, config=config)
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            client = get_client(config)
            db = client[name]
            db.command("ping")
            return db
        except InvalidOperation as e:
            last_err = e
            if attempt == 0 and (
                _client_is_closed(_client) or "after close" in str(e).lower()
            ):
                close_client()
                continue
            raise
        except Exception as e:
            last_err = e
            if attempt == 0:
                # Retry once. Only drop the singleton when it is already closed;
                # never close a healthy shared client on timeouts (thread race).
                if _client_is_closed(_client):
                    close_client()
                continue
            raise
    assert last_err is not None
    raise last_err


def ping_database(*, test: bool = False, config: dict | None = None) -> bool:
    db = get_database(test=test, config=config)
    db.command("ping")
    return True


def assert_safe_dev_db_mutation(db_name: str, *, action: str = "write") -> None:
    """Block pytest (and scripts) from mutating the operator dev ledger database."""
    if db_name != DEV_DB_NAME:
        return
    if os.environ.get("ALLOW_DEV_DB_MUTATION") == "1":
        return
    if is_pytest_running():
        raise RuntimeError(
            f"Refusing {action} on dev ledger database '{DEV_DB_NAME}' during pytest. "
            f"Tests must use '{TEST_DB_NAME}' (set MONGODB_TEST_DB / PYTEST_RUNNING)."
        )


def drop_database(*, test: bool = False, config: dict | None = None) -> None:
    assert_safe_mongo_drop(test=test, config=config)
    name = resolve_database_name(test=test, config=config)
    if name == DEV_DB_NAME and os.environ.get("ALLOW_DROP_DEV_DB") != "1":
        raise RuntimeError(
            f"Refusing drop_database on dev ledger '{DEV_DB_NAME}'. "
            f"Pytest must target '{TEST_DB_NAME}' via drop_database(test=True)."
        )
    get_client(config).drop_database(name)


def close_client() -> None:
    """Close and forget the shared client (URI switch, tests, process teardown)."""
    global _client, _client_uri
    with _client_lock:
        if _client is not None:
            try:
                if not _client_is_closed(_client):
                    _client.close()
            except Exception:
                pass
            _client = None
            _client_uri = None


def assert_safe_demo_mongo_db() -> str:
    """Abort when local demo mode would touch production or remote Mongo."""
    from data_manager import is_demo_mode, resolve_ledger_scope

    db = resolve_database_name()
    scope = resolve_ledger_scope()
    if is_demo_mode() and db == PROD_DB_NAME:
        raise SystemExit(
            f"Demo mode refuses production MongoDB database '{db}' "
            f"(scope={scope}). Set MONGODB_DB={DEV_DB_NAME}."
        )
    if (
        is_demo_mode()
        and not is_railway_runtime()
        and not os.environ.get("DEMO_ALLOW_REMOTE_MONGO") == "1"
        and not is_local_mongo_uri()
    ):
        host = mongo_uri_host(resolve_mongo_uri())
        raise SystemExit(
            f"Local demo mode refuses remote MongoDB host '{host}' (scope={scope}). "
            f"Unset MONGO_URL or run: source scripts/dev_local_mongo.sh"
        )
    return db


def log_ledger_startup() -> None:
    """Log resolved Mongo DB and ledger scope once at bot startup."""
    from data_manager import is_demo_mode, resolve_ledger_scope

    from logger import log

    scope = resolve_ledger_scope()
    db = resolve_database_name()
    demo = "demo" if is_demo_mode() else "off"
    log(f"Ledger startup: scope={scope} db={db} demo={demo}", "INFO")