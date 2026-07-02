"""Guards against accidental remote Mongo wipes from local pytest/scripts."""

import os

import pytest

from storage.mongo_client import (
    assert_safe_demo_mongo_db,
    assert_safe_mongo_drop,
    drop_database,
    force_local_test_mongo,
    is_local_mongo_uri,
)


def test_force_local_test_mongo_clears_railway_url(monkeypatch):
    monkeypatch.setenv("MONGO_URL", "mongodb://mongo:secret@interchange.proxy.rlwy.net:52996")
    force_local_test_mongo()
    assert "MONGO_URL" not in os.environ
    assert is_local_mongo_uri()
    assert os.environ["MONGODB_DB"] == "xagent_test"


def test_drop_database_refuses_remote_host(monkeypatch):
    monkeypatch.setenv("MONGO_URL", "mongodb://mongo:secret@interchange.proxy.rlwy.net:52996")
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("RAILWAY_DEPLOY", raising=False)
    with pytest.raises(RuntimeError, match="Refusing drop_database"):
        assert_safe_mongo_drop(test=True)


def test_drop_database_allows_localhost(monkeypatch):
    force_local_test_mongo()
    assert_safe_mongo_drop(test=True)
    drop_database(test=True)


def test_local_demo_refuses_remote_mongo(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("MONGO_URL", "mongodb://mongo:secret@interchange.proxy.rlwy.net:52996")
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("RAILWAY_DEPLOY", raising=False)
    monkeypatch.delenv("DEMO_ALLOW_REMOTE_MONGO", raising=False)
    with pytest.raises(SystemExit, match="refuses remote MongoDB"):
        assert_safe_demo_mongo_db()