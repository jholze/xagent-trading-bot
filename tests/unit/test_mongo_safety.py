"""Guards against accidental remote/dev Mongo wipes from local pytest/scripts."""

import os

import pytest

from storage.mongo_client import (
    DEV_DB_NAME,
    TEST_DB_NAME,
    assert_safe_demo_mongo_db,
    assert_safe_mongo_drop,
    drop_database,
    force_local_test_mongo,
    is_local_mongo_uri,
)
from storage.mongo_ledger import MongoLedgerStore


def test_force_local_test_mongo_clears_railway_url(monkeypatch):
    monkeypatch.delenv("PYTEST_RUNNING", raising=False)
    monkeypatch.delenv("MONGODB_DB", raising=False)
    monkeypatch.setenv("MONGO_URL", "mongodb://mongo:secret@interchange.proxy.rlwy.net:52996")
    force_local_test_mongo(dev=True)
    assert "MONGO_URL" not in os.environ
    assert is_local_mongo_uri()
    assert os.environ["MONGODB_DB"] == DEV_DB_NAME
    assert os.environ["MONGODB_TEST_DB"] == TEST_DB_NAME


def test_drop_database_refuses_remote_host(monkeypatch):
    monkeypatch.setenv("MONGO_URL", "mongodb://mongo:secret@interchange.proxy.rlwy.net:52996")
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("RAILWAY_DEPLOY", raising=False)
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


def test_local_demo_refuses_remote_mongo(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("MONGO_URL", "mongodb://mongo:secret@interchange.proxy.rlwy.net:52996")
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("RAILWAY_DEPLOY", raising=False)
    monkeypatch.delenv("DEMO_ALLOW_REMOTE_MONGO", raising=False)
    with pytest.raises(SystemExit, match="refuses remote MongoDB"):
        assert_safe_demo_mongo_db()