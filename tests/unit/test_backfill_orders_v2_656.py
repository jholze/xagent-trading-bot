"""Issue #656 Spec Rev 4. Scratch database only: never xagent_test or xagent."""

from __future__ import annotations

import copy
import io
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts.backfill_orders_v2 import (
    ApplyHooks,
    archive_name,
    raw_bson_sha256,
    run_job,
    script_sha256,
    survey,
    verify_parity,
    write_backup_manifest,
)
from storage.order_ledger_v2 import (
    IDEMPOTENCY_KEY_INDEX_NAME,
    IDEMPOTENCY_KEY_PARTIAL_FILTER,
    compound_order_id,
    reset_order_ledger_v2_for_tests,
)

SCOPE = "demo"
AS_OF = "2026-09-25T22:00:00Z"
SUMMARY = {"db": "scratch", "host": "env-test", "pytest_isolated": True}
BERLIN = ZoneInfo("Europe/Berlin")
COLLISIONS = {
    "default": (
        (2259, "5e4b40504c41", "rejected", False),
        (2266, "de8ffe321952", "cancelled", True),
        (2296, "687896bbdcf0", "cancelled", True),
    ),
    "henry": (
        (1483, "0aca9ca4c7c2", "cancelled", True),
        (1576, "36219a872447", "rejected", False),
    ),
}


def _wipe(db) -> None:
    keep_prefixes = ("orders_v2_archive_",)
    named = {
        "orders",
        "orders_v2",
        "order_day_stats",
        "ledger_manual_reverts",
        "positions",
        "trade_history",
    }
    for name in list(db.list_collection_names()):
        if name in named or name.startswith(keep_prefixes):
            db.drop_collection(name)


def _partial_index(db) -> None:
    db.orders_v2.create_index(
        [("idempotency_key", 1)],
        name=IDEMPOTENCY_KEY_INDEX_NAME,
        unique=True,
        partialFilterExpression=IDEMPOTENCY_KEY_PARTIAL_FILTER,
    )
    db.orders_v2.create_index(
        [("tenant_id", 1), ("ledger_scope", 1), ("display_seq", 1)],
        name="tenant_scope_display_seq",
        unique=True,
    )


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("ORDER_LEDGER_V2", "1")
    monkeypatch.setenv("ORDER_LEDGER_V2_BACKEND", "mongo")
    from storage.mongo_client import get_database

    database = get_database()
    assert database.name not in {"xagent_test", "xagent"}
    reset_order_ledger_v2_for_tests()
    _wipe(database)
    _partial_index(database)
    yield database
    reset_order_ledger_v2_for_tests()
    _wipe(database)


def _filled(oid, seq, side, usdt, day, symbol, amount, *, when=None, idem=None):
    stamp = when or f"{day}T12:00:00Z"
    order = {
        "id": oid,
        "display_seq": seq,
        "status": "filled",
        "side": side,
        "symbol": symbol,
        "timeframe": "4h",
        "day_key": day,
        "execution": {"usdt": usdt, "amount": amount, "price": 2.0, "fee": 0.1},
        "timestamps": {"filled": stamp, "created": stamp},
        "tenant_id": "",
        "ledger_scope": SCOPE,
    }
    if idem is not None:
        order["idempotency_key"] = idem
    return order


def _orphan(oid, seq, status, dry):
    return {
        "id": oid,
        "display_seq": seq,
        "status": status,
        "side": "sell",
        "symbol": "SYMOR/USDT",
        "timeframe": "4h",
        "day_key": "2026-07-26",
        "execution": {"dry_run": True, "price": 1, "amount": 1} if dry else {},
        "timestamps": {"created": "2026-07-26T12:00:00Z"},
        "tenant_id": "",
        "ledger_scope": SCOPE,
    }


def _put_v2(db, tenant, order):
    doc = dict(order)
    doc["tenant_id"] = tenant
    doc["ledger_scope"] = SCOPE
    doc["_id"] = compound_order_id(tenant, SCOPE, order["id"])
    db.orders_v2.insert_one(doc)


def _tag(tenant, order):
    order["tenant_id"] = tenant
    order["ledger_scope"] = SCOPE
    return order


def _orphans_for(tenant):
    if tenant == "default":
        seqs = list(range(2103, 2304))
        named = {seq: (oid, status, dry) for seq, oid, status, dry in COLLISIONS["default"]}
        cancel_left, reject_left = 46, 152
    else:
        seqs = list(range(1477, 1648))
        named = {seq: (oid, status, dry) for seq, oid, status, dry in COLLISIONS["henry"]}
        cancel_left, reject_left = 37, 132
    rows = []
    for seq in seqs:
        if seq in named:
            oid, status, dry = named[seq]
        elif cancel_left:
            oid, status, dry = f"orph-{tenant[:1]}-{seq}", "cancelled", True
            cancel_left -= 1
        else:
            oid, status, dry = f"orph-{tenant[:1]}-{seq}", "rejected", False
            reject_left -= 1
        rows.append(_orphan(oid, seq, status, dry))
    assert cancel_left == 0 and reject_left == 0
    return rows


def _tenant_gap(db, tenant, blob, both):
    collision = {row[0] for row in COLLISIONS[tenant]}
    for orphan in _orphans_for(tenant):
        _tag(tenant, orphan)
        _put_v2(db, tenant, orphan)
        if orphan["display_seq"] not in collision:
            blob.append(_tag(tenant, {
                "id": f"rej-{tenant[:1]}-{orphan['display_seq']}",
                "display_seq": orphan["display_seq"],
                "status": "rejected",
                "side": "buy",
                "symbol": "SYMREJ/USDT",
                "timeframe": "4h",
                "day_key": "2026-07-27",
                "execution": {},
                "timestamps": {"created": "2026-07-27T12:00:00Z"},
            }))
    if tenant == "default":
        inserts = [
            _filled("ins-d-buy-1", 3001, "buy", 1330.18, "2026-08-28", "SYMB0/USDT", 4, idem=None),
            _filled("ins-d-buy-2", 3002, "buy", 1330.18, "2026-09-01", "SYMB1/USDT", 4, idem=None),
            _filled("ins-d-buy-3", 3003, "buy", 1330.19, "2026-09-10", "SYMB2/USDT", 4),
            _filled("ins-d-sell-1", 3004, "sell", 5260.31, "2026-09-15", "SYMD0/USDT", 3),
            _filled("ins-d-sell-2", 3005, "sell", 5260.31, "2026-09-20", "SYMD1/USDT", 3),
            _filled("ins-d-sell-3", 3006, "sell", 5260.31, "2026-09-25", "SYMD2/USDT", 3),
            _filled("ins-d-b1", 2259, "sell", 5260.31, "2026-08-29", "SYMD3/USDT", 3),
            _filled("ins-d-b2", 2266, "sell", 5260.31, "2026-09-02", "SYMD4/USDT", 3),
            _filled("ins-d-b3", 2296, "sell", 5260.31, "2026-09-03", "SYMD5/USDT", 3),
        ]
        sell_symbols = [f"SYMD{i}/USDT" for i in range(6)]
        base_seq = 4000
        keeper = _filled("keep-d", 12931, "buy", 10, "2026-07-24", "SYMKEEP/USDT", 1)
    else:
        inserts = [_filled("ins-h-buy", 1700, "buy", 395.60, "2026-08-28", "SYMBH/USDT", 4)]
        for i, usdt in enumerate([3746.26] * 11):
            day = (datetime(2026, 8, 30) + timedelta(days=i)).strftime("%Y-%m-%d")
            inserts.append(_filled(f"ins-h-sell-{i}", 1701 + i, "sell", usdt, day, f"SYMH{i}/USDT", 3))
        inserts.append(_filled("ins-h-b1", 1483, "sell", 3746.26, "2026-09-21", "SYMH11/USDT", 3))
        inserts.append(_filled("ins-h-b2", 1576, "sell", 3746.32, "2026-09-25", "SYMH12/USDT", 3))
        sell_symbols = [f"SYMH{i}/USDT" for i in range(13)]
        base_seq = 1800
        keeper = _filled("keep-h", 10860, "buy", 10, "2026-07-28", "SYMKEEP/USDT", 1)
    for order in inserts:
        blob.append(_tag(tenant, order))
    for i, symbol in enumerate(sell_symbols):
        base = _filled(f"base-{tenant[:1]}-{i}", base_seq + i, "buy", 10, "2026-07-25", symbol, 10, when="2026-07-25T08:00:00Z")
        _tag(tenant, base)
        blob.append(base)
        both.append(base)
    _tag(tenant, keeper)
    blob.append(keeper)
    both.append(keeper)


def build_gap(db):
    for tenant in ("default", "henry"):
        blob = []
        both = []
        _tenant_gap(db, tenant, blob, both)
        db.orders.insert_one({
            "_id": f"{tenant}:{SCOPE}",
            "tenant_id": tenant,
            "ledger_scope": SCOPE,
            "orders": blob,
        })
        for order in both:
            _put_v2(db, tenant, order)
    db.positions.insert_one({"_id": "default:demo", "tenant_id": "default", "positions": {"marker": 1}})
    db.trade_history.insert_one({
        "_id": "default:demo",
        "tenant_id": "default",
        "trades": [{"id": "dry-run-marker"}],
    })


def _expected(db, tenants=("default", "henry")):
    return survey(db, list(tenants), SCOPE, AS_OF)


def _apply(db, expected, tmp_path, run_id="run656", hooks=None, tenants=("default", "henry"), as_of=AS_OF):
    path = tmp_path / f"backup-{run_id}.json"
    digest = write_backup_manifest(db, path, run_id)
    return run_job(
        db,
        mode="apply",
        tenants=list(tenants),
        scope=SCOPE,
        run_id=run_id,
        as_of=as_of,
        expected=expected,
        backup_path=str(path),
        backup_sha256=digest,
        script_sha256_expected=script_sha256(),
        command=f"backfill --apply --run-id {run_id}",
        summary=SUMMARY,
        hooks=hooks,
    )


def _text(result) -> str:
    return "\n".join(result.lines)


def test_t1_dry_run_counts_split_and_index(db, monkeypatch):
    build_gap(db)

    def boom(*_a, **_k):
        raise AssertionError("dry-run opened the v2 store")

    monkeypatch.setattr("storage.order_ledger_v2.get_order_ledger_v2", boom)
    result = run_job(
        db,
        mode="dry-run",
        tenants=["default", "henry"],
        scope=SCOPE,
        as_of=AS_OF,
        command="backfill --dry-run",
        summary=SUMMARY,
    )
    assert result.exit_code == 0
    found = result.found["tenants"]
    assert found["default"]["archive_count"] == 201
    assert found["henry"]["archive_count"] == 171
    assert found["default"]["insert_count"] == 9
    assert found["henry"]["insert_count"] == 14
    assert found["default"]["buys"] == 3
    assert found["default"]["buy_usdt"] == "3990.55"
    assert found["default"]["sells"] == 6
    assert found["default"]["sell_usdt"] == "31561.86"
    assert found["henry"]["buys"] == 1
    assert found["henry"]["buy_usdt"] == "395.60"
    assert found["henry"]["sells"] == 13
    assert found["henry"]["sell_usdt"] == "48701.44"
    assert found["default"]["insertable_now"] + found["henry"]["insertable_now"] == 18
    assert found["default"]["blocked"] + found["henry"]["blocked"] == 5
    assert found["default"]["blob_max_seq"] == 12931
    assert found["default"]["v2_max_seq"] == 12931
    assert found["henry"]["blob_max_seq"] == 10860
    assert found["henry"]["v2_max_seq"] == 10860
    text = _text(result)
    assert "partial_nonempty=True" in text
    for oid in ("5e4b40504c41", "de8ffe321952", "687896bbdcf0", "0aca9ca4c7c2", "36219a872447"):
        assert oid in text
    assert "DONE" in text and "NOT DONE" in text


def test_t2_second_apply_writes_zero(db, tmp_path):
    build_gap(db)
    expected = _expected(db)
    first = _apply(db, expected, tmp_path, "run656")
    assert first.exit_code == 0, _text(first)
    second = _apply(db, expected, tmp_path, "run656")
    assert second.exit_code == 0, _text(second)
    assert "writes=0" in _text(second)
    assert "archives=0" in _text(second)
    assert db.orders_v2.count_documents({"id": "ins-d-buy-1"}) == 1


def test_t3_expected_counts_mismatch_stops(db, tmp_path):
    build_gap(db)
    expected = _expected(db)
    expected["tenants"]["default"]["insert_count"] = 1
    before = db.orders_v2.count_documents({})
    result = _apply(db, expected, tmp_path, "run656t3")
    assert result.exit_code != 0
    text = _text(result)
    assert "DIFF" in text
    assert "nothing archived, nothing written" in text
    assert db.orders_v2.count_documents({}) == before
    assert archive_name("run656t3") not in set(db.list_collection_names())


def test_t4_lots_match_after_apply(db, tmp_path):
    build_gap(db)
    before = verify_parity(db, ["default", "henry"], SCOPE)
    assert before["tenants"]["default"]["lots_differing"] > 0
    result = _apply(db, _expected(db), tmp_path)
    assert result.exit_code == 0, _text(result)
    report = verify_parity(db, ["default", "henry"], SCOPE)
    assert report["green"] is True
    for tenant in ("default", "henry"):
        row = report["tenants"][tenant]
        assert row["missing"] == 0
        assert row["mismatched"] == 0
        assert row["filled_only_v2"] == 0
        assert row["status_differs"] == 0
        assert row["lots_differing"] == 0
        assert row["orphans_holding_blob_number"] == 0
    assert report["key"] == "tenant_id,ledger_scope,order_id"


def test_t5_day_stats_match_blob(db, tmp_path):
    build_gap(db)
    assert _apply(db, _expected(db), tmp_path).exit_code == 0
    from core.tenant_context import tenant_context
    from services.order_service import OrderService

    def blob_filled(tenant, day):
        doc = db.orders.find_one({"_id": f"{tenant}:{SCOPE}"})
        return sum(
            1
            for order in doc["orders"]
            if order.get("status") == "filled" and order.get("day_key") == day
        )

    def check(tenant, start, end):
        day = start
        while day <= end:
            key = day.strftime("%Y-%m-%d")
            with tenant_context(tenant):
                stats = OrderService(scope=SCOPE).stats_day_filled_fast(
                    datetime(day.year, day.month, day.day, 15, 0, tzinfo=BERLIN)
                )
            assert stats["filled"] == blob_filled(tenant, key), (tenant, key, stats["filled"])
            day += timedelta(days=1)

    for tenant in ("default", "henry"):
        check(tenant, datetime(2026, 8, 28), datetime(2026, 9, 25))
        check(tenant, datetime(2026, 7, 24), datetime(2026, 7, 28))


def test_t6_blob_byte_identical_and_late_order_skipped(db, tmp_path):
    late = _tag("default", _filled(
        "late-d", 4, "sell", 10, "2026-09-26", "SYMLATE/USDT", 1,
        when="2026-09-26T01:00:00Z",
    ))
    keeper = _tag("default", _filled("keep", 20, "buy", 1, "2026-07-24", "SYMKEEP/USDT", 1))
    in_range = _tag("default", _filled("in-range", 3, "buy", 5, "2026-08-28", "SYM/USDT", 1))
    db.orders.insert_one({
        "_id": "default:demo",
        "tenant_id": "default",
        "ledger_scope": SCOPE,
        "orders": [late, keeper, in_range],
    })
    _put_v2(db, "default", keeper)
    before = raw_bson_sha256(db, "orders")
    result = _apply(db, _expected(db, ("default",)), tmp_path, "run656t6", tenants=("default",))
    assert result.exit_code == 0, _text(result)
    assert raw_bson_sha256(db, "orders") == before
    assert db.orders_v2.find_one({"id": "late-d"}) is None
    assert db.orders_v2.find_one({"id": "in-range"}) is not None


def test_t7_archive_identity_audit_and_trade_history(db, tmp_path):
    build_gap(db)
    original = {
        doc["id"]: copy.deepcopy(doc)
        for doc in db.orders_v2.find({"id": {"$in": ["5e4b40504c41", "de8ffe321952", "0aca9ca4c7c2"]}})
    }
    trade_before = raw_bson_sha256(db, "trade_history")
    result = _apply(db, _expected(db), tmp_path, "run656")
    assert result.exit_code == 0, _text(result)
    archived = db[archive_name("run656")].find_one({"id": "de8ffe321952"})
    assert archived is not None
    body = {k: v for k, v in archived.items() if k not in {"archived_at", "reason", "blob_order_id"}}
    assert body == original["de8ffe321952"]
    assert archived["reason"] == "cause-b-orphan"
    assert archived["blob_order_id"] == "ins-d-b2"
    assert db.orders_v2.find_one({"id": "ins-d-b2", "display_seq": 2266}) is not None
    assert db.orders_v2.find_one({"id": "de8ffe321952"}) is None
    assert raw_bson_sha256(db, "trade_history") == trade_before
    henry = db.ledger_manual_reverts.find_one({"_id": "repair|henry|v2|run656"})
    default = db.ledger_manual_reverts.find_one({"_id": "repair|default|v2|run656"})
    assert henry and default
    assert henry["run_id"] == "run656"
    assert henry["archive_collection"] == archive_name("run656")
    assert len(henry["dry_run_fill_order_ids"]) == 38
    assert "0aca9ca4c7c2" in henry["dry_run_fill_order_ids"]
    assert len(default["dry_run_fill_order_ids"]) == 48
    assert db.ledger_manual_reverts.count_documents({"run_id": "run656"}) == 2
    stored = db.orders_v2.find_one({"id": "ins-d-buy-1"})
    assert "idempotency_key" not in stored


def test_t8_dual_write_logs_identity_and_keeps_blob(db):
    from services.order_service import OrderService
    from core.tenant_context import tenant_context

    record = {
        "id": "dw656",
        "display_seq": 4,
        "tenant_id": "default",
        "ledger_scope": SCOPE,
        "status": "filled",
        "side": "buy",
        "symbol": "SYM/USDT",
        "execution": {"usdt": 1, "amount": 1, "price": 1},
        "timestamps": {"created": "2026-09-01T00:00:00Z", "filled": "2026-09-01T00:00:00Z"},
    }
    with tenant_context("default"):
        svc = OrderService(scope=SCOPE)
        assert svc._save({"tenant_id": "default", "ledger_scope": SCOPE, "orders": [record]})

        class Boom:
            def upsert_order(self, _order):
                raise RuntimeError("forced-v2-failure")

        buf = io.StringIO()
        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr("storage.order_ledger_v2.get_order_ledger_v2", lambda: Boom())
            with redirect_stdout(buf):
                svc._dual_write_v2(record)
        loaded = svc._load()
    text = buf.getvalue()
    assert "tenant=default" in text
    assert "scope=demo" in text
    assert "order_id=dw656" in text
    assert "display_seq=4" in text
    assert "forced-v2-failure" in text
    assert any(order.get("id") == "dw656" for order in loaded["orders"])


def test_t9_bad_index_aborts_without_calling_store(db, monkeypatch, tmp_path):
    db.orders_v2.drop()
    db.orders_v2.create_index(
        [("idempotency_key", 1)],
        name=IDEMPOTENCY_KEY_INDEX_NAME,
        unique=True,
        sparse=True,
    )
    before = db.orders_v2.index_information()[IDEMPOTENCY_KEY_INDEX_NAME]

    def boom(*_a, **_k):
        raise AssertionError("get_order_ledger_v2 called")

    monkeypatch.setattr("storage.order_ledger_v2.get_order_ledger_v2", boom)
    dry = run_job(
        db, mode="dry-run", tenants=["default"], scope=SCOPE, as_of=AS_OF,
        command="dry", summary=SUMMARY,
    )
    assert dry.exit_code != 0
    assert "nothing archived, nothing written" in _text(dry)
    applied = _apply(db, {"as_of": AS_OF, "tenants": {}}, tmp_path, "run656t9", tenants=("default",))
    assert applied.exit_code != 0
    after = db.orders_v2.index_information()[IDEMPOTENCY_KEY_INDEX_NAME]
    assert after == before
    assert after.get("sparse") is True


def test_t10_filled_orphan_stops_with_nothing_written(db, tmp_path):
    _put_v2(db, "default", _tag("default", _filled("ghost-fill", 8, "buy", 5, "2026-08-28", "SYMG/USDT", 1)))
    db.orders.insert_one({
        "_id": "default:demo",
        "tenant_id": "default",
        "ledger_scope": SCOPE,
        "orders": [_tag("default", _filled("real-1", 9, "buy", 5, "2026-08-28", "SYMR/USDT", 1))],
    })
    before = raw_bson_sha256(db, "orders_v2")
    result = _apply(db, _expected(db, ("default",)), tmp_path, "run656t10", tenants=("default",))
    assert result.exit_code != 0
    text = _text(result)
    assert "nothing archived, nothing written" in text
    assert "ghost-fill" in text
    assert raw_bson_sha256(db, "orders_v2") == before
    assert archive_name("run656t10") not in set(db.list_collection_names())


def test_t11_upsert_readback_and_missing_store(db, tmp_path):
    order = _tag("default", _filled("only-1", 3, "buy", 5, "2026-08-28", "SYM/USDT", 1))
    _put_v2(db, "default", _tag("default", _filled("keep", 20, "buy", 1, "2026-07-24", "SYMKEEP/USDT", 1)))
    db.orders.insert_one({"_id": "default:demo", "tenant_id": "default", "ledger_scope": SCOPE, "orders": [
        order,
        _tag("default", _filled("keep", 20, "buy", 1, "2026-07-24", "SYMKEEP/USDT", 1)),
    ]})
    expected = _expected(db, ("default",))
    raised = _apply(
        db, expected, tmp_path, "run656t11a", tenants=("default",),
        hooks=ApplyHooks(fail_upsert_ids={"only-1"}),
    )
    assert raised.exit_code != 0
    assert "DONE" in _text(raised) and "NOT DONE" in _text(raised)
    assert "forced upsert failure" in _text(raised)
    assert db.orders_v2.find_one({"id": "only-1"}) is None

    unread = _apply(
        db, expected, tmp_path, "run656t11b", tenants=("default",),
        hooks=ApplyHooks(fail_readback_ids={"only-1"}),
    )
    assert unread.exit_code != 0
    assert "read-back failed" in _text(unread)

    db.orders_v2.delete_many({"id": "only-1"})
    missing = _apply(
        db, expected, tmp_path, "run656t11c", tenants=("default",),
        hooks=ApplyHooks(store_none=True),
    )
    assert missing.exit_code != 0
    assert "nothing archived, nothing written" in _text(missing)
    degraded = _apply(
        db, expected, tmp_path, "run656t11d", tenants=("default",),
        hooks=ApplyHooks(degraded=True),
    )
    assert degraded.exit_code != 0
    assert "degraded" in _text(degraded)


def test_t12_seq_held_by_blob_order_stops(db, tmp_path):
    holder = _tag("default", {
        "id": "holder-1",
        "display_seq": 7,
        "status": "rejected",
        "side": "buy",
        "symbol": "SYMHOLD/USDT",
        "timeframe": "4h",
        "day_key": "2026-08-01",
        "execution": {},
        "timestamps": {"created": "2026-08-01T00:00:00Z"},
    })
    missing = _tag("default", _filled("want-7", 7, "buy", 5, "2026-08-28", "SYMW/USDT", 1))
    keeper = _tag("default", _filled("keep", 30, "buy", 1, "2026-07-24", "SYMKEEP/USDT", 1))
    _put_v2(db, "default", holder)
    _put_v2(db, "default", keeper)
    db.orders.insert_one({
        "_id": "default:demo",
        "tenant_id": "default",
        "ledger_scope": SCOPE,
        "orders": [holder, missing, keeper],
    })
    before = db.orders_v2.count_documents({})
    result = _apply(db, _expected(db, ("default",)), tmp_path, "run656t12", tenants=("default",))
    assert result.exit_code != 0
    assert "non-orphan" in _text(result)
    assert "nothing archived, nothing written" in _text(result)
    assert db.orders_v2.count_documents({}) == before


def test_t13_status_differs_any_status(db):
    blob = _tag("default", {
        "id": "st-1",
        "display_seq": 4,
        "status": "rejected",
        "side": "buy",
        "symbol": "SYM/USDT",
        "timeframe": "4h",
        "day_key": "2026-08-01",
        "execution": {},
        "timestamps": {"created": "2026-08-01T00:00:00Z"},
    })
    other = dict(blob)
    other["status"] = "cancelled"
    db.orders.insert_one({"_id": "default:demo", "tenant_id": "default", "ledger_scope": SCOPE, "orders": [blob]})
    _put_v2(db, "default", other)
    result = run_job(
        db, mode="verify", tenants=["default"], scope=SCOPE, command="verify", summary=SUMMARY,
    )
    assert result.exit_code != 0
    row = result.verify["tenants"]["default"]
    assert row["status_differs"] == 1
    assert row["missing"] == 0
    assert "status_differs=1" in _text(result)


def test_r2_key_is_order_id_not_display_seq(db):
    blob = _tag("default", _filled("same-id", 5, "buy", 8, "2026-08-28", "SYM/USDT", 2))
    moved = dict(blob)
    moved["display_seq"] = 9
    db.orders.insert_one({"_id": "default:demo", "tenant_id": "default", "ledger_scope": SCOPE, "orders": [blob]})
    _put_v2(db, "default", moved)
    report = verify_parity(db, ["default"], SCOPE)
    row = report["tenants"]["default"]
    assert report["key"] == "tenant_id,ledger_scope,order_id"
    assert row["missing"] == 0
    assert row["filled_only_v2"] == 0
    assert row["mismatched"] == 1
    assert "display_seq" in row["mismatched_detail"][0]["fields"]


def test_t14_stops_before_write_on_log_hash_and_baseline(db, tmp_path, monkeypatch):
    order = _tag("default", _filled("only-1", 3, "buy", 5, "2026-08-28", "SYM/USDT", 1))
    keeper = _tag("default", _filled("keep", 20, "buy", 1, "2026-07-24", "SYMKEEP/USDT", 1))
    db.orders.insert_one({"_id": "default:demo", "tenant_id": "default", "ledger_scope": SCOPE, "orders": [order, keeper]})
    _put_v2(db, "default", keeper)
    expected = _expected(db, ("default",))
    before = db.orders_v2.count_documents({})
    path = tmp_path / "b.json"
    digest = write_backup_manifest(db, path, "run656t14cmd")
    result = run_job(
        db, mode="apply", tenants=["default"], scope=SCOPE, run_id="run656t14cmd",
        as_of=AS_OF, expected=expected, backup_path=str(path), backup_sha256=digest,
        script_sha256_expected=script_sha256(), command="", summary=SUMMARY,
    )
    assert result.exit_code != 0
    assert "missing command" in _text(result)
    assert db.orders_v2.count_documents({}) == before

    result = run_job(
        db, mode="apply", tenants=["default"], scope=SCOPE, run_id="run656t14sum",
        as_of=AS_OF, expected=expected, backup_path=str(path), backup_sha256=digest,
        script_sha256_expected=script_sha256(), command="apply", summary={"db": "scratch"},
    )
    assert result.exit_code != 0
    assert "operator_mongo_summary" in _text(result)

    result = run_job(
        db, mode="apply", tenants=["default"], scope=SCOPE, run_id="run656t14sha",
        as_of=AS_OF, expected=expected, backup_path=str(path), backup_sha256=digest,
        script_sha256_expected="0" * 64, command="apply", summary=SUMMARY,
    )
    assert result.exit_code != 0
    assert "script sha256" in _text(result)

    hash_path = tmp_path / "hash-before.json"
    hash_digest = write_backup_manifest(db, hash_path, "run656t14hash")
    db.positions.insert_one({"_id": "extra", "n": 1})
    result = run_job(
        db, mode="apply", tenants=["default"], scope=SCOPE, run_id="run656t14hash",
        as_of=AS_OF, expected=expected, backup_path=str(hash_path), backup_sha256=hash_digest,
        script_sha256_expected=script_sha256(), command="apply", summary=SUMMARY,
    )
    assert result.exit_code != 0
    assert "hash mismatch" in _text(result)
    assert archive_name("run656t14hash") not in set(db.list_collection_names())
    db.positions.delete_one({"_id": "extra"})

    db.orders_v2.update_one({"id": "keep"}, {"$set": {"display_seq": 99}})
    result = _apply(db, expected, tmp_path, "run656t14base", tenants=("default",))
    assert result.exit_code != 0
    assert "v2_baseline" in _text(result)
    assert db.orders_v2.find_one({"id": "only-1"}) is None
    db.orders_v2.update_one({"id": "keep"}, {"$set": {"display_seq": 20}})

    def bump():
        db.orders.update_one(
            {"_id": "default:demo"},
            {"$push": {"orders": _tag("default", _filled("intruder", 50, "buy", 1, "2026-08-28", "SYMI/USDT", 1))}},
        )

    result = _apply(
        db, expected, tmp_path, "run656t14run", tenants=("default",),
        hooks=ApplyHooks(after_archive=bump),
    )
    assert result.exit_code != 0
    assert "during run" in _text(result)
    db.orders.update_one({"_id": "default:demo"}, {"$pull": {"orders": {"id": "intruder"}}})

    def extra():
        _put_v2(db, "default", _tag("default", _filled("extra-end", 77, "buy", 1, "2026-08-28", "SYME/USDT", 1)))

    result = _apply(
        db, expected, tmp_path, "run656t14end", tenants=("default",),
        hooks=ApplyHooks(after_writes=extra),
    )
    assert result.exit_code != 0
    assert "end state" in _text(result)


def test_t14_c1_refuses_before_any_read(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "storage.mongo_client.get_database",
        lambda *a, **k: calls.append("db") or (_ for _ in ()).throw(AssertionError("read")),
    )
    monkeypatch.setattr(
        "scripts.operator_mongo.prepare_operator_mongo",
        lambda *a, **k: calls.append("prepare") or {"db": "xagent_test", "host": "env-test", "pytest_isolated": False},
    )

    monkeypatch.delenv("MONGODB_DB", raising=False)
    monkeypatch.delenv("ORDER_LEDGER_V2_BACKEND", raising=False)
    from scripts.backfill_orders_v2 import main

    assert main(["--dry-run"]) != 0
    assert calls == []

    monkeypatch.setenv("MONGODB_DB", "xagent_test")
    monkeypatch.setenv("ORDER_LEDGER_V2_BACKEND", "memory")
    assert main(["--dry-run"]) != 0
    assert "prepare" not in calls

    monkeypatch.setenv("ORDER_LEDGER_V2_BACKEND", "mongo")
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.setattr(
        "scripts.operator_mongo.prepare_operator_mongo",
        lambda *a, **k: {"db": "xagent_test", "host": "not-the-env-test", "pytest_isolated": False},
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(["--verify"])
    assert code != 0
    assert "host is not the env-test Mongo" in buf.getvalue()
    assert "not-the-env-test" not in buf.getvalue()
    assert calls == []


def test_t15_delete_guard_and_bad_archive(db, tmp_path):
    orphan = _tag("default", _orphan("orph-1", 5, "rejected", False))
    blocked = _tag("default", _filled("ins-b", 5, "buy", 4, "2026-08-28", "SYM/USDT", 1))
    keeper = _tag("default", _filled("keep", 40, "buy", 1, "2026-07-24", "SYMKEEP/USDT", 1))
    _put_v2(db, "default", orphan)
    _put_v2(db, "default", keeper)
    db.orders.insert_one({
        "_id": "default:demo", "tenant_id": "default", "ledger_scope": SCOPE,
        "orders": [blocked, keeper],
    })
    expected = _expected(db, ("default",))
    mutated = _apply(
        db, expected, tmp_path, "run656t15a", tenants=("default",),
        hooks=ApplyHooks(mutate_v2_before_delete=True),
    )
    assert mutated.exit_code != 0
    assert "changed before delete" in _text(mutated)
    assert db.orders_v2.find_one({"id": "orph-1"}) is not None

    db.orders_v2.update_one({"id": "orph-1"}, {"$set": {"status": "rejected"}})
    db.drop_collection(archive_name("run656t15a"))
    counted = _apply(
        db, expected, tmp_path, "run656t15b", tenants=("default",),
        hooks=ApplyHooks(deleted_count_override=0),
    )
    assert counted.exit_code != 0
    assert "deleted_count=0" in _text(counted)
    assert db.orders_v2.find_one({"id": "orph-1"}) is not None

    db.drop_collection(archive_name("run656t15b"))
    bad = dict(orphan)
    bad["_id"] = compound_order_id("default", SCOPE, "orph-1")
    bad["status"] = "cancelled"
    bad["archived_at"] = "2026-01-01T00:00:00Z"
    bad["reason"] = "other"
    bad["blob_order_id"] = "nope"
    db[archive_name("run656t15c")].insert_one(bad)
    differed = _apply(db, expected, tmp_path, "run656t15c", tenants=("default",))
    assert differed.exit_code != 0
    assert "archive copy differs" in _text(differed)
    assert db.orders_v2.find_one({"id": "orph-1"})["status"] == "rejected"


def test_t16_existing_target_is_not_replaced(db, tmp_path):
    order = _tag("default", _filled("only-1", 3, "buy", 5, "2026-08-28", "SYM/USDT", 1))
    keeper = _tag("default", _filled("keep", 20, "buy", 1, "2026-07-24", "SYMKEEP/USDT", 1))
    db.orders.insert_one({"_id": "default:demo", "tenant_id": "default", "ledger_scope": SCOPE, "orders": [order, keeper]})
    _put_v2(db, "default", keeper)
    expected = _expected(db, ("default",))

    def plant():
        _put_v2(db, "default", _tag("default", {
            "id": "only-1",
            "display_seq": 88,
            "status": "rejected",
            "side": "buy",
            "symbol": "SYM/USDT",
            "timeframe": "4h",
            "day_key": "2026-08-28",
            "execution": {},
            "timestamps": {"created": "2026-08-28T00:00:00Z"},
        }))

    result = _apply(
        db, expected, tmp_path, "run656t16", tenants=("default",),
        hooks=ApplyHooks(before_insert=plant),
    )
    assert result.exit_code != 0
    assert "nothing replaced" in _text(result)
    left = db.orders_v2.find_one({"id": "only-1"})
    assert left["status"] == "rejected"
    assert left["display_seq"] == 88


def test_t17_resume_completes_without_duplicate_archive(db, tmp_path):
    orphan = _tag("default", _orphan("orph-1", 5, "cancelled", True))
    blocked = _tag("default", _filled("ins-b", 5, "buy", 4, "2026-08-28", "SYM/USDT", 2))
    free = _tag("default", _filled("ins-f", 6, "buy", 4, "2026-09-01", "SYMF/USDT", 2))
    keeper = _tag("default", _filled("keep", 40, "buy", 1, "2026-07-24", "SYMKEEP/USDT", 1))
    _put_v2(db, "default", orphan)
    _put_v2(db, "default", keeper)
    db.orders.insert_one({
        "_id": "default:demo", "tenant_id": "default", "ledger_scope": SCOPE,
        "orders": [blocked, free, keeper],
    })
    expected = _expected(db, ("default",))
    stopped = _apply(
        db, expected, tmp_path, "run656t17", tenants=("default",),
        hooks=ApplyHooks(abort_after_delete=True),
    )
    assert stopped.exit_code != 0
    assert db[archive_name("run656t17")].count_documents({}) == 1
    assert db.orders_v2.find_one({"id": "orph-1"}) is None
    assert db.orders_v2.find_one({"id": "ins-b"}) is None
    resumed = _apply(db, expected, tmp_path, "run656t17", tenants=("default",))
    assert resumed.exit_code == 0, _text(resumed)
    assert db[archive_name("run656t17")].count_documents({}) == 1
    report = verify_parity(db, ["default"], SCOPE)
    assert report["green"] is True


def test_t7b_archive_readback_failure_removes_nothing(db, tmp_path):
    orphan = _tag("default", _orphan("orph-1", 5, "rejected", False))
    blocked = _tag("default", _filled("ins-b", 5, "buy", 4, "2026-08-28", "SYM/USDT", 1))
    keeper = _tag("default", _filled("keep", 40, "buy", 1, "2026-07-24", "SYMKEEP/USDT", 1))
    _put_v2(db, "default", orphan)
    _put_v2(db, "default", keeper)
    db.orders.insert_one({
        "_id": "default:demo", "tenant_id": "default", "ledger_scope": SCOPE,
        "orders": [blocked, keeper],
    })
    before = db.orders_v2.count_documents({})
    result = _apply(
        db, _expected(db, ("default",)), tmp_path, "run656rb", tenants=("default",),
        hooks=ApplyHooks(fail_archive_readback={"orph-1"}),
    )
    assert result.exit_code != 0
    assert "nothing removed" in _text(result)
    assert db.orders_v2.count_documents({}) == before


def test_rollback_restores_orphans_and_removes_inserts(db, tmp_path):
    orphan = _tag("default", _orphan("orph-1", 5, "rejected", False))
    blocked = _tag("default", _filled("ins-b", 5, "buy", 4, "2026-08-28", "SYM/USDT", 1))
    keeper = _tag("default", _filled("keep", 40, "buy", 1, "2026-07-24", "SYMKEEP/USDT", 1))
    _put_v2(db, "default", orphan)
    _put_v2(db, "default", keeper)
    db.orders.insert_one({
        "_id": "default:demo", "tenant_id": "default", "ledger_scope": SCOPE,
        "orders": [blocked, keeper],
    })
    blob_before = raw_bson_sha256(db, "orders")
    expected = _expected(db, ("default",))
    assert _apply(db, expected, tmp_path, "run656rbk", tenants=("default",)).exit_code == 0
    path = tmp_path / "after.json"
    digest = write_backup_manifest(db, path, "run656rbk")
    result = run_job(
        db, mode="rollback", tenants=["default"], scope=SCOPE, run_id="run656rbk",
        backup_path=str(path), backup_sha256=digest, script_sha256_expected=script_sha256(),
        command="backfill --rollback --run-id run656rbk", summary=SUMMARY,
    )
    assert result.exit_code == 0, _text(result)
    assert db.orders_v2.find_one({"id": "orph-1"}) is not None
    assert db.orders_v2.find_one({"id": "ins-b"}) is None
    assert archive_name("run656rbk") not in set(db.list_collection_names())
    assert db.ledger_manual_reverts.count_documents({"run_id": "run656rbk"}) == 0
    assert raw_bson_sha256(db, "orders") == blob_before


def test_script_has_no_secrets_hosts_or_backfill_complete_instruction():
    text = Path("scripts/backfill_orders_v2.py").read_text(encoding="utf-8")
    lowered = text.lower()
    assert "mongodb://" not in lowered
    assert "redis://" not in lowered
    assert "railway.internal" not in lowered
    assert "next: set order_ledger_v2_backfill_complete=1" not in lowered
    assert "does not set that flag" in text

