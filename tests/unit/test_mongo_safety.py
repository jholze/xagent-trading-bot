"""Guards against accidental remote/dev Mongo wipes from local pytest/scripts."""

import os

import pytest

from storage.mongo_client import (
    DEV_DB_NAME,
    TEST_DB_NAME,
    assert_safe_demo_mongo_db,
    assert_safe_mongo_drop,
    apply_operator_mongo_target,
    drop_database,
    force_local_test_mongo,
    is_local_mongo_uri,
    resolve_database_name,
    resolve_test_db_name,
    use_isolated_pytest_database,
)
from storage.mongo_ledger import MongoLedgerStore


def test_resolve_test_db_name_unset_is_base(monkeypatch):
    monkeypatch.delenv("PYTEST_DB_SUFFIX", raising=False)
    assert resolve_test_db_name() == "xagent_pytest"


def test_resolve_test_db_name_suffix_sanitized(monkeypatch):
    monkeypatch.setenv("PYTEST_DB_SUFFIX", "ci-run.1")
    assert resolve_test_db_name() == "xagent_pytest_cirun1"
    monkeypatch.setenv("PYTEST_DB_SUFFIX", "grok")
    assert resolve_test_db_name() == "xagent_pytest_grok"
    monkeypatch.setenv("PYTEST_DB_SUFFIX", "  ")
    assert resolve_test_db_name() == "xagent_pytest"


def test_force_local_test_mongo_clears_railway_url(monkeypatch):
    monkeypatch.delenv("PYTEST_RUNNING", raising=False)
    monkeypatch.delenv("MONGODB_DB", raising=False)
    monkeypatch.setenv("MONGO_URL", "mongodb://mongo:secret@interchange.proxy.rlwy.net:52996")
    force_local_test_mongo(dev=True)
    assert "MONGO_URL" not in os.environ
    assert is_local_mongo_uri()
    assert os.environ["MONGODB_DB"] == DEV_DB_NAME
    assert os.environ["MONGODB_TEST_DB"] == TEST_DB_NAME


def _clear_railway_runtime(monkeypatch) -> None:
    for key in (
        "RAILWAY_DEPLOY",
        "RAILWAY_ENVIRONMENT",
        "RAILWAY_PUBLIC_DOMAIN",
        "RAILWAY_SERVICE_NAME",
    ):
        monkeypatch.delenv(key, raising=False)


def test_drop_database_refuses_remote_host(monkeypatch):
    _clear_railway_runtime(monkeypatch)
    monkeypatch.setenv("MONGO_URL", "mongodb://mongo:secret@interchange.proxy.rlwy.net:52996")
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("DEMO_ALLOW_REMOTE_MONGO", raising=False)
    monkeypatch.delenv("FORCE_OPERATOR_MONGO", raising=False)
    with pytest.raises(RuntimeError, match="Refusing drop_database"):
        assert_safe_mongo_drop(test=True)


def test_drop_database_refuses_dev_db(monkeypatch):
    monkeypatch.delenv("PYTEST_RUNNING", raising=False)
    monkeypatch.delenv("MONGODB_TEST_DB", raising=False)
    force_local_test_mongo(dev=True)
    monkeypatch.setenv("MONGODB_DB", DEV_DB_NAME)
    with pytest.raises(RuntimeError, match="Refusing drop_database on dev ledger"):
        drop_database(test=False)


def test_drop_database_allows_pytest_db(monkeypatch):
    force_local_test_mongo(dev=False)
    monkeypatch.setenv("MONGODB_DB", TEST_DB_NAME)
    assert_safe_mongo_drop(test=True)
    drop_database(test=True)


def test_pytest_cannot_write_dev_ledger(monkeypatch):
    monkeypatch.setenv("PYTEST_RUNNING", "1")
    monkeypatch.setenv("MONGODB_DB", DEV_DB_NAME)
    store = MongoLedgerStore(test=False)
    with pytest.raises(RuntimeError, match="Refusing write on dev ledger"):
        store.save_orders({"ledger_scope": "demo", "orders": []}, "demo")


def test_pytest_with_railway_url_targets_operator_db(monkeypatch):
    monkeypatch.setenv("PYTEST_RUNNING", "1")
    monkeypatch.setenv(
        "MONGO_URL",
        "mongodb://mongo:secret@hayabusa.proxy.rlwy.net:10592",
    )
    monkeypatch.setenv("MONGODB_DB", DEV_DB_NAME)
    monkeypatch.setenv("DEMO_ALLOW_REMOTE_MONGO", "1")
    monkeypatch.setenv("FORCE_OPERATOR_MONGO", "1")
    assert use_isolated_pytest_database() is False
    assert resolve_database_name() == DEV_DB_NAME


def test_pytest_local_still_uses_isolated_db(monkeypatch):
    monkeypatch.setenv("PYTEST_RUNNING", "1")
    monkeypatch.delenv("FORCE_OPERATOR_MONGO", raising=False)
    monkeypatch.delenv("DEMO_ALLOW_REMOTE_MONGO", raising=False)
    monkeypatch.delenv("MONGO_URL", raising=False)
    force_local_test_mongo(dev=False)
    monkeypatch.setenv("MONGODB_DB", TEST_DB_NAME)
    assert use_isolated_pytest_database() is True
    assert resolve_database_name(test=True) == TEST_DB_NAME


def test_explicit_xagent_test_db_blocks_pytest_isolation(monkeypatch):
    monkeypatch.setenv("PYTEST_RUNNING", "1")
    monkeypatch.delenv("FORCE_OPERATOR_MONGO", raising=False)
    monkeypatch.delenv("DEMO_ALLOW_REMOTE_MONGO", raising=False)
    monkeypatch.delenv("MONGO_URL", raising=False)
    force_local_test_mongo(dev=True)
    monkeypatch.setenv("MONGODB_DB", DEV_DB_NAME)
    assert use_isolated_pytest_database() is False
    assert resolve_database_name() == DEV_DB_NAME


def test_local_demo_refuses_remote_mongo(monkeypatch):
    _clear_railway_runtime(monkeypatch)
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("MONGO_URL", "mongodb://mongo:secret@interchange.proxy.rlwy.net:52996")
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("DEMO_ALLOW_REMOTE_MONGO", raising=False)
    monkeypatch.delenv("FORCE_OPERATOR_MONGO", raising=False)
    with pytest.raises(SystemExit, match="refuses remote MongoDB"):
        assert_safe_demo_mongo_db()


def test_get_client_reopens_after_close(monkeypatch):
    """Portfolio threads must not keep a closed singleton (MongoClient after close)."""
    from storage import mongo_client as mc

    force_local_test_mongo(dev=False)
    monkeypatch.setenv("MONGODB_DB", TEST_DB_NAME)
    mc.close_client()

    first = mc.get_client()
    assert first is not None
    assert not getattr(first, "_closed", False)

    mc.close_client()
    assert getattr(first, "_closed", False) is True

    second = mc.get_client()
    assert second is not first
    assert not getattr(second, "_closed", False)

    # get_database recovers after an explicit close of a held client path
    mc.close_client()
    db = mc.get_database(test=True)
    assert db is not None
    db.command("ping")
    mc.close_client()


def test_apply_operator_mongo_target_idempotent_skips_close(monkeypatch):
    """Repeated prepare must not close a healthy shared client (#192)."""
    from storage import mongo_client as mc
    from unittest.mock import patch

    remote = "mongodb://mongo:secret@hayabusa.proxy.rlwy.net:10592"
    monkeypatch.delenv("PYTEST_RUNNING", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("MONGO_URL", remote)
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.setenv("MONGODB_DB", DEV_DB_NAME)
    monkeypatch.setenv("DEMO_ALLOW_REMOTE_MONGO", "1")
    monkeypatch.setenv("FORCE_OPERATOR_MONGO", "1")

    mc.close_client()
    # Seed a fake open client so we can detect close_client calls.
    sentinel = object()
    mc._client = sentinel  # type: ignore[assignment]
    mc._client_uri = remote

    with patch.object(mc, "close_client") as mock_close:
        # Same target → no close
        apply_operator_mongo_target(mongo_url=remote, db=DEV_DB_NAME)
        mock_close.assert_not_called()

        # DB change → close once
        apply_operator_mongo_target(mongo_url=remote, db="other_db")
        mock_close.assert_called_once()

    mc._client = None
    mc._client_uri = None


def test_prepare_operator_mongo_idempotent(monkeypatch):
    from scripts.operator_mongo import prepare_operator_mongo
    from storage import mongo_client as mc
    from unittest.mock import patch

    remote = "mongodb://mongo:secret@hayabusa.proxy.rlwy.net:10592"
    monkeypatch.delenv("PYTEST_RUNNING", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("MONGO_URL", remote)
    monkeypatch.setenv("MONGODB_DB", DEV_DB_NAME)
    monkeypatch.setenv("FORCE_OPERATOR_MONGO", "1")
    monkeypatch.setenv("DEMO_ALLOW_REMOTE_MONGO", "1")

    with patch.object(mc, "apply_operator_mongo_target") as mock_apply:
        prepare_operator_mongo(mongo_url=remote, db=DEV_DB_NAME)
        mock_apply.assert_not_called()