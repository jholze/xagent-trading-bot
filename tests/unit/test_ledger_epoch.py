"""scripts/ledger_epoch.py: backup -> verify -> reset -> restore on the pytest Mongo."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts import ledger_epoch as le
from storage.mongo_client import drop_database, get_database
from storage.mongo_ledger import MongoLedgerStore

NOW = datetime(2026, 11, 5, 19, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("MULTI_TENANT_ENABLED", "1")
    drop_database(test=True)
    db = get_database(test=True)
    store = MongoLedgerStore(test=True)
    _seed(db)
    yield db, store
    drop_database(test=True)


def _order(i: int, sym: str = "ABC/USDT") -> dict:
    return {"id": f"o{i}", "symbol": sym, "side": "buy", "status": "filled", "amount": 1.0}


def _seed(db) -> None:
    for tid, lots in (("henry", 3), ("default", 2), ("ctexp", 4)):
        positions = {f"C{i}/USDT": {"amount": 1.0 + i, "average_entry": 1.0} for i in range(lots)}
        positions["FLAT/USDT"] = {"amount": 0.0}
        db.orders.insert_one(
            {"_id": f"{tid}:demo", "tenant_id": tid, "ledger_scope": "demo",
             "orders": [_order(i) for i in range(5)], "fence": 7}
        )
        db.orders_archive.insert_one(
            {"_id": f"{tid}:demo:part:0000", "tenant_id": tid, "ledger_scope": "demo",
             "kind": "orders_archive", "part_index": 0, "orders": [_order(100 + i) for i in range(4)]}
        )
        db.positions.insert_one(
            {"_id": f"{tid}:demo", "tenant_id": tid, "ledger_scope": "demo", "positions": positions}
        )
        db.trade_history.insert_one(
            {"_id": f"{tid}:demo", "tenant_id": tid, "ledger_scope": "demo",
             "trades": [{"t": 1}], "virtual_balance": 8352.5}
        )
        db.trade_history.insert_one(
            {"_id": f"{tid}:live", "tenant_id": tid, "ledger_scope": "live",
             "trades": [{"t": 1}, {"t": 2}], "virtual_balance": 9000.0, "peak_equity": 108500.0}
        )
        db.orders_v2.insert_many(
            [{"_id": f"{tid}:demo:o{i}", "tenant_id": tid, "ledger_scope": "demo"} for i in range(6)]
        )
        db.portfolio_nav_daily.insert_one(
            {"tenant_id": tid, "ledger_scope": "demo", "date": "2026-11-04", "nav": 87144.0,
             "as_of": datetime(2026, 11, 4, tzinfo=timezone.utc)}
        )
        db.obs_buy_decision_tape.insert_one({"tenant_id": tid, "symbol": "X", "timestamp": NOW})
        db.memory_lessons.insert_one({"_id": f"les_{tid}", "tenant_id": tid, "text": "x"})
    db.positions.insert_one({"_id": "demo", "positions": {}})
    db.trade_history.insert_one({"_id": "demo", "trades": [{"legacy": 1}], "virtual_balance": 1.0})


def _backup(db, tmp_path, tenants=("henry", "default")):
    out = tmp_path / "bk"
    manifest = le.backup(db, list(tenants), out, now=NOW)
    return out, manifest


# ---------------------------------------------------------------- tenants


@pytest.mark.parametrize("raw", [[], [""], ["all"], ["ALL"], ["*"], ["henry", "all"], ["he nry"]])
def test_parse_tenants_refuses_implicit_or_bad(raw):
    with pytest.raises(le.EpochError):
        le.parse_tenants(raw)


def test_parse_tenants_dedup_and_comma():
    assert le.parse_tenants(["Henry", "default,henry"]) == ["default", "henry"]


# ---------------------------------------------------------------- backup / verify


def test_backup_covers_live_peak_archive_extras_and_skips_other_tenants(env, tmp_path):
    db, _ = env
    out, manifest = _backup(db, tmp_path)
    s = manifest["summary"]
    assert s["henry"]["demo"]["open_lots"] == 3
    assert s["default"]["demo"]["open_lots"] == 2
    assert s["henry"]["demo"]["orders_total_rows"] == 9
    assert s["henry"]["demo"]["archive_parts"] == 1
    assert s["henry"]["live"]["peak_equity"] == 108500.0
    assert s["henry"]["demo"]["virtual_balance"] == 8352.5
    files = manifest["files"]
    assert files["extra/orders_v2.jsonl"]["count"] == 12
    assert files["extra/portfolio_nav_daily.jsonl"]["count"] == 2
    assert files["extra/obs_buy_decision_tape.jsonl"]["count"] == 2
    assert files["extra/memory_lessons.jsonl"]["count"] == 2
    assert files["ledger/_legacy/trade_history.jsonl"]["count"] == 1
    assert "ctexp" not in json.dumps(manifest["summary"])
    v2 = (out / "extra/orders_v2.jsonl").read_text()
    assert "ctexp" not in v2
    # datetime survives the round trip
    nav = le._read_jsonl(out / "extra/portfolio_nav_daily.jsonl")
    assert isinstance(nav[0]["as_of"], datetime)


def test_backup_is_read_only(env, tmp_path):
    db, _ = env
    before = {c: db[c].count_documents({}) for c in db.list_collection_names()}
    _backup(db, tmp_path)
    after = {c: db[c].count_documents({}) for c in db.list_collection_names()}
    assert before == after


def test_backup_refuses_existing_dir(env, tmp_path):
    db, _ = env
    out, _ = _backup(db, tmp_path)
    with pytest.raises(le.EpochError):
        le.backup(db, ["henry"], out, now=NOW)


def test_verify_ok_then_detects_tamper_and_drift(env, tmp_path):
    db, _ = env
    out, _ = _backup(db, tmp_path)
    assert le.verify(out, db)["ok"]
    db.orders_v2.insert_one({"_id": "henry:demo:new", "tenant_id": "henry"})
    rep = le.verify(out, db)
    assert not rep["ok"] and any("orders_v2" in p for p in rep["problems"])
    assert le.verify(out, db, allow_growth=True)["ok"]
    path = out / "ledger/henry/demo/positions.jsonl"
    path.write_text(path.read_text().replace('"amount"', '"amountX"'))
    rep = le.verify(out)
    assert not rep["ok"] and any("sha256" in p for p in rep["problems"])


def test_verify_detects_data_lost_after_backup(env, tmp_path):
    db, _ = env
    out, _ = _backup(db, tmp_path)
    db.obs_buy_decision_tape.delete_many({"tenant_id": "henry"})
    rep = le.verify(out, db, allow_growth=True)
    assert not rep["ok"] and any("lost" in p for p in rep["problems"])


# ---------------------------------------------------------------- reset


def test_reset_dry_run_writes_nothing(env, tmp_path):
    db, store = env
    out, _ = _backup(db, tmp_path)
    before = list(db.orders.find({}, sort=[("_id", 1)]))
    rep = le.reset(db, store, out, ["henry", "default"], balance=100000, now=NOW)
    assert rep["dry_run"] and rep["plan"]
    assert list(db.orders.find({}, sort=[("_id", 1)])) == before
    assert db.ledger_epochs.count_documents({}) == 0


def test_reset_apply_needs_confirm(env, tmp_path):
    db, store = env
    out, _ = _backup(db, tmp_path)
    with pytest.raises(le.EpochError, match="confirm-writers-stopped"):
        le.reset(db, store, out, ["henry"], balance=100000, apply=True, now=NOW)
    assert db.orders.find_one({"_id": "henry:demo"})["orders"]


def test_reset_refuses_tenant_missing_in_backup(env, tmp_path):
    db, store = env
    out, _ = _backup(db, tmp_path, tenants=("henry",))
    with pytest.raises(le.EpochError, match="does not contain"):
        le.reset(db, store, out, ["henry", "default"], balance=100000, now=NOW)


def test_reset_refuses_stale_backup_and_drift(env, tmp_path):
    db, store = env
    out, _ = _backup(db, tmp_path)
    with pytest.raises(le.EpochError, match="older"):
        le.reset(db, store, out, ["henry"], balance=100000, now=NOW + timedelta(hours=7))
    db.orders_v2.insert_one({"_id": "henry:demo:late", "tenant_id": "henry"})
    with pytest.raises(le.EpochError, match="verify failed"):
        le.reset(db, store, out, ["henry"], balance=100000, now=NOW)


def test_reset_refuses_balance_not_matching_config(env, tmp_path):
    db, store = env
    out, _ = _backup(db, tmp_path)
    with pytest.raises(le.EpochError, match="config start capital"):
        le.reset(db, store, out, ["henry"], balance=50000,
                 expected_capital={"henry": 100000.0}, now=NOW)


def test_reset_apply_resets_demo_and_live_keeps_history(env, tmp_path):
    db, store = env
    out, _ = _backup(db, tmp_path)
    rep = le.reset(db, store, out, ["henry", "default"], balance=100000,
                   expected_capital={"henry": 100000.0, "default": 100000.0},
                   apply=True, confirm_writers_stopped=True, now=NOW)
    for tid in ("henry", "default"):
        assert store.load_orders("demo", tenant_id=tid)["orders"] == []
        assert store.load_positions("demo", tenant_id=tid)["positions"] == {}
        th = store.load_trade_history("demo", tenant_id=tid)
        assert th["virtual_balance"] == 100000 and th["trades"] == []
        live = db.trade_history.find_one({"_id": f"{tid}:live"})
        assert live["peak_equity"] == 100000 and live["trades"] == []
        assert live["ledger_epoch"]["epoch_id"] == rep["epoch_id"]
        assert db.orders_archive.count_documents({"_id": {"$regex": f"^{tid}:demo:part:"}}) == 0
        marker = db.ledger_epochs.find_one({"_id": f"{tid}:{rep['epoch_id']}"})
        assert marker["kind"] == "reset" and marker["initial_capital"] == 100000
        assert marker["previous"]["demo"]["open_lots"] in (2, 3)
    # history collections and other tenants untouched
    assert db.orders_v2.count_documents({}) == 18
    assert db.portfolio_nav_daily.count_documents({}) == 3
    assert db.obs_buy_decision_tape.count_documents({}) == 3
    assert len(db.orders.find_one({"_id": "ctexp:demo"})["orders"]) == 5
    assert db.orders_archive.find_one({"_id": "ctexp:demo:part:0000"})
    assert db.trade_history.find_one({"_id": "ctexp:live"})["peak_equity"] == 108500.0
    # legacy default docs removed (they were backed up)
    assert db.trade_history.find_one({"_id": "demo"}) is None
    assert rep["legacy_removed"]


def test_reset_without_default_keeps_legacy_docs(env, tmp_path):
    db, store = env
    out, _ = _backup(db, tmp_path, tenants=("henry",))
    le.reset(db, store, out, ["henry"], balance=100000, apply=True,
             confirm_writers_stopped=True, now=NOW)
    assert db.trade_history.find_one({"_id": "demo"})


# ---------------------------------------------------------------- restore


def test_restore_round_trip(env, tmp_path):
    db, store = env
    out, manifest = _backup(db, tmp_path)
    le.reset(db, store, out, ["henry", "default"], balance=100000, apply=True,
             confirm_writers_stopped=True, now=NOW)
    dry = le.restore(db, out, ["henry", "default"], now=NOW)
    assert dry["dry_run"]
    assert store.load_orders("demo", tenant_id="henry")["orders"] == []
    db.orders_archive.insert_one(
        {"_id": "henry:demo:part:0005", "tenant_id": "henry", "ledger_scope": "demo",
         "kind": "orders_archive", "part_index": 5, "orders": [_order(999)]}
    )
    le.restore(db, out, ["henry", "default"], apply=True, confirm_writers_stopped=True, now=NOW)
    assert db.orders_archive.find_one({"_id": "henry:demo:part:0005"}) is None
    for tid in ("henry", "default"):
        assert len(store.load_orders("demo", tenant_id=tid)["orders"]) == 9
        lots = store.load_positions("demo", tenant_id=tid)["positions"]
        assert sum(1 for r in lots.values() if r["amount"] > 0) == manifest["summary"][tid]["demo"]["open_lots"]
        assert db.trade_history.find_one({"_id": f"{tid}:live"})["peak_equity"] == 108500.0
        assert "fence" not in db.orders.find_one({"_id": f"{tid}:demo"})
    assert db.trade_history.find_one({"_id": "demo"})["trades"] == [{"legacy": 1}]
    assert db.ledger_epochs.count_documents({"kind": "restore"}) == 2


def test_restore_include_extra_only_fills_missing(env, tmp_path):
    db, store = env
    out, _ = _backup(db, tmp_path)
    db.memory_lessons.delete_one({"_id": "les_henry"})
    db.memory_lessons.update_one({"_id": "les_default"}, {"$set": {"text": "newer"}})
    rep = le.restore(db, out, ["henry", "default"], apply=True, include_extra=True,
                     confirm_writers_stopped=True, now=NOW)
    assert rep["extra_inserted"] == 1
    assert db.memory_lessons.find_one({"_id": "les_henry"})
    assert db.memory_lessons.find_one({"_id": "les_default"})["text"] == "newer"


def test_restore_refuses_broken_backup(env, tmp_path):
    db, _ = env
    out, _ = _backup(db, tmp_path)
    (out / "ledger/henry/live/trade_history.jsonl").unlink()
    with pytest.raises(le.EpochError, match="verify failed"):
        le.restore(db, out, ["henry"], apply=True, confirm_writers_stopped=True, now=NOW)


# ---------------------------------------------------------------- CLI and old scripts


def test_cli_refuses_missing_tenant_before_connecting(capsys):
    assert le.main(["backup"]) == 2
    assert le.main(["reset", "--dir", "x", "--tenant", "all"]) == 2
    assert "REFUSED" in capsys.readouterr().err


def test_snapshot_script_refuses_all_and_apply_reset(monkeypatch, capsys):
    from scripts import snapshot_and_fresh_start as snap

    monkeypatch.setattr("sys.argv", ["x"])
    assert snap.main() == 2
    monkeypatch.setattr("sys.argv", ["x", "--tenant", "all"])
    assert snap.main() == 2
    monkeypatch.setattr("sys.argv", ["x", "--tenant", "henry", "--apply-reset", "--yes"])
    assert snap.main() == 2
    assert "ledger_epoch.py" in capsys.readouterr().err


def test_reset_demo_ledger_refuses_without_unsafe_flag(capsys):
    from scripts import reset_demo_ledger

    assert reset_demo_ledger.main(["--yes"]) == 2
    assert "ledger_epoch.py" in capsys.readouterr().err


# ---------------------------------------------------------------- writer lease (#306)


class _MiniRedis:
    """GET / SET NX / DEL / INCR / EXPIRE, enough for WriterLease without EVAL."""

    def __init__(self):
        self.kv: dict[str, str] = {}

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value, nx=False, ex=None, **_k):
        if nx and key in self.kv:
            return False
        self.kv[key] = str(value)
        return True

    def delete(self, key):
        return 1 if self.kv.pop(key, None) is not None else 0

    def incr(self, key):
        self.kv[key] = str(int(self.kv.get(key, "0")) + 1)
        return int(self.kv[key])

    def expire(self, key, ttl):
        return key in self.kv


@pytest.fixture
def lease_on():
    from bus import writer_lease as wl
    from data_manager import get_config

    wl.reset_writer_lease_for_tests()
    arch = get_config().setdefault("architecture", {})
    old = arch.get("single_writer_lease_enabled")
    arch["single_writer_lease_enabled"] = True
    redis = _MiniRedis()
    yield wl, redis
    arch["single_writer_lease_enabled"] = old
    wl.reset_writer_lease_for_tests()


def test_reset_refuses_while_a_writer_holds_the_lease(env, lease_on, tmp_path):
    db, store = env
    wl, redis = lease_on
    out, _ = _backup(db, tmp_path)
    bot = wl.WriterLease("henry", "live", redis=redis, token="bot", host="xagent-test")
    assert bot.acquire(start_renewer=False)
    with pytest.raises(le.EpochError, match="held by bot/xagent-test"):
        le.reset(db, store, out, ["henry", "default"], balance=100000, apply=True,
                 confirm_writers_stopped=True, now=NOW, redis=redis)
    assert len(db.orders.find_one({"_id": "henry:demo"})["orders"]) == 5
    assert len(db.orders.find_one({"_id": "default:demo"})["orders"]) == 5
    # our partial leases were released; the bot's stays
    assert redis.get(wl.lease_redis_key("henry", "demo")) is None
    assert redis.get(wl.lease_redis_key("henry", "live"))


def test_reset_refuses_without_redis(env, lease_on, tmp_path):
    db, store = env
    wl, _ = lease_on
    out, _ = _backup(db, tmp_path)
    wl.set_lease_redis_for_tests(None)
    with pytest.raises(le.EpochError, match="Redis unreachable"):
        le.reset(db, store, out, ["henry"], balance=100000, apply=True,
                 confirm_writers_stopped=True, now=NOW)
    assert db.orders.find_one({"_id": "henry:demo"})["orders"]


def test_reset_refuses_when_redis_fence_is_behind_mongo(env, lease_on, tmp_path):
    db, store = env
    wl, redis = lease_on
    out, _ = _backup(db, tmp_path)
    with pytest.raises(le.EpochError, match="fence 7 > our lease fence"):
        le.reset(db, store, out, ["henry"], balance=100000, apply=True,
                 confirm_writers_stopped=True, now=NOW, redis=redis)
    assert len(db.orders.find_one({"_id": "henry:demo"})["orders"]) == 5


def test_reset_with_lease_stamps_fence_and_releases(env, lease_on, tmp_path):
    db, store = env
    wl, redis = lease_on
    out, _ = _backup(db, tmp_path)
    # production: the Redis INCR counter is ahead of every fence stamped in Mongo
    redis.kv[wl.fence_redis_key("henry", "demo")] = "7"
    le.reset(db, store, out, ["henry"], balance=100000, apply=True,
             confirm_writers_stopped=True, now=NOW, redis=redis)
    assert db.orders.find_one({"_id": "henry:demo"})["fence"] == 8
    assert db.trade_history.find_one({"_id": "henry:live"})["peak_equity"] == 100000
    for scope in ("demo", "live"):
        assert redis.get(wl.lease_redis_key("henry", scope)) is None
    assert wl.process_lease() is None
    # the bot can take the lease right after
    assert wl.WriterLease("henry", "demo", redis=redis, token="bot").acquire(start_renewer=False)


def test_restore_refuses_while_a_writer_holds_the_lease(env, lease_on, tmp_path):
    db, _ = env
    wl, redis = lease_on
    out, _ = _backup(db, tmp_path)
    assert wl.WriterLease("default", "demo", redis=redis, token="bot").acquire(start_renewer=False)
    db.orders.update_one({"_id": "henry:demo"}, {"$set": {"orders": []}})
    with pytest.raises(le.EpochError, match="held by"):
        le.restore(db, out, ["henry", "default"], apply=True,
                   confirm_writers_stopped=True, now=NOW, redis=redis)
    assert db.orders.find_one({"_id": "henry:demo"})["orders"] == []


def test_reset_failure_midway_points_to_restore(env, tmp_path, monkeypatch):
    db, store = env
    out, _ = _backup(db, tmp_path)

    def boom(*_a, **_k):
        raise RuntimeError("mongo went away")

    monkeypatch.setattr(store, "save_positions", boom)
    with pytest.raises(le.EpochError, match="restore --dir"):
        le.reset(db, store, out, ["henry"], balance=100000, apply=True,
                 confirm_writers_stopped=True, now=NOW)
    le.restore(db, out, ["henry"], apply=True, confirm_writers_stopped=True, now=NOW)
    assert len(store.load_orders("demo", tenant_id="henry")["orders"]) == 9
