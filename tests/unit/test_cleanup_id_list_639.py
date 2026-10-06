"""Id-list cleanup for #639. Fixtures are synthetic; the real id list stays outside the repo."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.cleanup_id_list import (
    UNTOUCHED,
    InMemoryCleanupStore,
    execute,
    open_mongo_store,
    sha256_file,
)

ROOT = Path(__file__).resolve().parents[2]
IDS = ROOT / "tests" / "fixtures" / "cleanup_639" / "synthetic_scope_ids.json"
FIGURE = 23.637
TENANTS = ["tenant_a", "tenant_h"]


def _order(oid, *, side, symbol, usdt, amount, price, ts, tenant, scope, pnl=None):
    row = {
        "id": oid,
        "order_id": oid,
        "side": side,
        "symbol": symbol,
        "timeframe": "1h",
        "status": "filled",
        "execution": {"usdt": usdt, "amount": amount, "price": price},
        "timestamps": {"filled": ts},
        "tenant_id": tenant,
        "ledger_scope": scope,
    }
    if pnl is not None:
        row["pnl"] = pnl
    return row


def _v2(order):
    doc = dict(order)
    doc["_id"] = f"{order['tenant_id']}:{order['ledger_scope']}:{order['id']}"
    return doc


def _trade(oid, *, symbol, ts, trade_id=None):
    return {
        "id": trade_id or f"trade-{oid}",
        "order_id": oid,
        "symbol": symbol,
        "timestamp": ts,
        "timestamps": {"filled": ts},
    }


def _ledger(doc_id, tenant, scope, **payload):
    body = {
        "_id": doc_id,
        "tenant_id": tenant,
        "ledger_scope": scope,
    }
    body.update(payload)
    return body


def build_store() -> InMemoryCleanupStore:
    store = InMemoryCleanupStore()
    orders = {
        "tenant_a:demo": [
            _order("ord_a_before", side="buy", symbol="AAA/USDT", usdt=10, amount=1, price=1, ts="2026-08-01T00:00:00", tenant="tenant_a", scope="demo"),
            _order("ord_a_entry", side="buy", symbol="AAA/USDT", usdt=100, amount=2, price=1, ts="2026-09-27T21:36:00", tenant="tenant_a", scope="demo"),
            _order("ord_a_dca", side="buy", symbol="AAA/USDT", usdt=40, amount=1, price=1, ts="2026-09-30T12:00:00", tenant="tenant_a", scope="demo"),
            _order("ord_a_other", side="buy", symbol="BBB/USDT", usdt=15, amount=1, price=1, ts="2026-09-28T00:00:00", tenant="tenant_a", scope="demo"),
        ],
        "tenant_h:demo": [
            _order("ord_h_other", side="buy", symbol="BBB/USDT", usdt=20, amount=1, price=1, ts="2026-09-01T00:00:00", tenant="tenant_h", scope="demo"),
            _order("ord_h_entry", side="buy", symbol="AAA/USDT", usdt=80, amount=2, price=1, ts="2026-09-27T21:40:00", tenant="tenant_h", scope="demo"),
            _order("ord_h_dca_demo", side="buy", symbol="AAA/USDT", usdt=30, amount=1, price=1, ts="2026-09-30T12:00:00", tenant="tenant_h", scope="demo"),
        ],
        "tenant_h:live": [
            _order("ord_keep_buy", side="buy", symbol="AAA/USDT", usdt=10, amount=10, price=1, ts="2026-08-12T10:00:00", tenant="tenant_h", scope="live"),
            _order("ord_keep_sell", side="sell", symbol="AAA/USDT", usdt=33.637, amount=10, price=3.3637, pnl=FIGURE, ts="2026-08-12T11:00:00", tenant="tenant_h", scope="live"),
            _order("ord_h_dca_live", side="buy", symbol="AAA/USDT", usdt=50, amount=1, price=1, ts="2026-10-02T12:00:00", tenant="tenant_h", scope="live"),
        ],
        "tenant_c:demo": [
            _order("ord_ctexp_1", side="buy", symbol="AAA/USDT", usdt=5, amount=1, price=1, ts="2026-09-29T00:00:00", tenant="tenant_c", scope="demo"),
            _order("ord_ctexp_2", side="buy", symbol="AAA/USDT", usdt=5, amount=1, price=1, ts="2026-09-29T01:00:00", tenant="tenant_c", scope="demo"),
        ],
    }
    for doc_id, rows in orders.items():
        tenant, scope = doc_id.split(":")
        store.orders[doc_id] = _ledger(doc_id, tenant, scope, orders=rows)
        for row in rows:
            doc = _v2(row)
            store.v2[doc["_id"]] = doc

    store.positions["tenant_a:demo"] = _ledger(
        "tenant_a:demo",
        "tenant_a",
        "demo",
        positions={
            "AAA_USDT_1h": {"amount": 3, "average_entry": 1, "realized_pnl": 0},
            "BBB_USDT_4h": {"amount": 1, "average_entry": 2, "realized_pnl": 4},
        },
    )
    store.positions["tenant_h:demo"] = _ledger(
        "tenant_h:demo",
        "tenant_h",
        "demo",
        positions={
            "AAA_USDT_1h": {"amount": 12, "average_entry": 1, "realized_pnl": FIGURE},
            "BBB_USDT_4h": {"amount": 2, "average_entry": 3, "realized_pnl": 1.5},
        },
    )
    store.trades["tenant_a:demo"] = _ledger(
        "tenant_a:demo",
        "tenant_a",
        "demo",
        initial_capital=10000,
        virtual_balance=1,
        realized_pnl=0,
        trades=[
            _trade("ord_a_before", symbol="AAA/USDT", ts="2026-08-01T00:00:00"),
            _trade("ord_a_entry", symbol="AAA/USDT", ts="2026-09-27T21:36:00"),
            _trade("ord_a_dca", symbol="AAA/USDT", ts="2026-09-30T12:00:00"),
        ],
    )
    store.trades["tenant_h:demo"] = _ledger(
        "tenant_h:demo",
        "tenant_h",
        "demo",
        initial_capital=10000,
        virtual_balance=1,
        realized_pnl=0,
        trades=[
            _trade("ord_h_other", symbol="BBB/USDT", ts="2026-09-01T00:00:00"),
            _trade("ord_h_entry", symbol="AAA/USDT", ts="2026-09-27T21:40:00"),
            _trade("ord_h_dca_demo", symbol="AAA/USDT", ts="2026-09-30T12:00:00"),
        ],
    )
    store.trades["tenant_h:live"] = _ledger(
        "tenant_h:live",
        "tenant_h",
        "live",
        initial_capital=10000,
        virtual_balance=1,
        realized_pnl=999,
        trades=[
            _trade("ord_keep_buy", symbol="AAA/USDT", ts="2026-08-12T10:00:00", trade_id="tenant_h:live#663"),
            _trade("ord_keep_sell", symbol="AAA/USDT", ts="2026-08-12T11:00:00", trade_id="tenant_h:live#671"),
            _trade("ord_h_dca_live", symbol="AAA/USDT", ts="2026-10-02T12:00:00"),
        ],
    )
    store.trades["tenant_c:demo"] = _ledger(
        "tenant_c:demo",
        "tenant_c",
        "demo",
        initial_capital=10000,
        virtual_balance=5000,
        realized_pnl=0,
        trades=[_trade("ord_ctexp_1", symbol="AAA/USDT", ts="2026-09-29T00:00:00")],
    )
    store.memory["trades"]["mem_trade_1"] = {"_id": "mem_trade_1", "tenant_id": "not_a_real_tenant", "symbol": "AAA/USDT"}
    store.memory["trades"]["mem_trade_2"] = {"_id": "mem_trade_2", "tenant_id": "tenant_h", "symbol": "AAA/USDT"}
    store.memory["trades"]["mem_trade_stay"] = {"_id": "mem_trade_stay", "tenant_id": "tenant_a", "symbol": "BBB/USDT"}
    store.memory["events"]["evt_listed"] = {
        "_id": "evt_listed",
        "tenant_id": "not_a_real_tenant",
        "source": "dca_policy",
        "symbols": ["AAA/USDT"],
        "timestamp": "2026-09-28T00:00:00Z",
    }
    store.memory["events"]["evt_keep_0812"] = {
        "_id": "evt_keep_0812",
        "tenant_id": "tenant_a",
        "source": "dca_policy",
        "symbols": ["AAA/USDT"],
        "timestamp": "2026-10-02T00:00:00Z",
    }
    store.memory["events"]["evt_before"] = {
        "_id": "evt_before",
        "tenant_id": "tenant_a",
        "source": "dca_policy",
        "symbols": ["AAA/USDT"],
        "timestamp": "2026-08-12T08:00:00Z",
    }
    store.memory["events"]["evt_new"] = {
        "_id": "evt_new",
        "tenant_id": "tenant_h",
        "source": "dca_policy",
        "symbols": ["AAA/USDT"],
        "timestamp": "2026-10-06T00:00:00Z",
    }
    store.memory["events"]["evt_cmc"] = {
        "_id": "evt_cmc",
        "tenant_id": "tenant_a",
        "source": "cmc",
        "symbols": ["AAA/USDT"],
        "timestamp": "2026-10-06T00:00:00Z",
    }
    store.memory["rag"]["rag_listed"] = {"_id": "rag_listed", "metadata": {"source_id": "demo:ord_a_entry"}}
    store.memory["rag"]["rag_by_source"] = {"_id": "rag_by_source", "metadata": {"source_id": "demo:ord_a_entry"}}
    store.memory["rag"]["rag_keep"] = {"_id": "rag_keep", "metadata": {"source_id": "demo:ord_a_entry"}}
    store.memory["rag"]["rag_keep_fill"] = {"_id": "rag_keep_fill", "metadata": {"source_id": "live:ord_keep_sell"}}
    store.memory["rag"]["rag_other"] = {"_id": "rag_other", "metadata": {"source_id": "demo:unrelated"}}
    store.memory["lessons"]["lesson_1"] = {"_id": "lesson_1", "tenant_id": "someone_else", "text": "drop"}
    store.memory["lessons"]["lesson_stay"] = {"_id": "lesson_stay", "tenant_id": "tenant_a", "text": "stay"}
    store.memory["profiles"]["tenant_a|demo|AAA/USDT"] = {"_id": "tenant_a|demo|AAA/USDT", "tenant_id": "other"}
    store.memory["profiles"]["tenant_h|demo|BBB/USDT"] = {"_id": "tenant_h|demo|BBB/USDT", "tenant_id": "tenant_h"}
    store.nav[("tenant_h", "live")] = [
        {"date": "2026-08-01", "nav": 111, "cash": 111, "tenant_id": "tenant_h", "ledger_scope": "live"},
        {"date": "2026-09-28", "nav": 1, "cash": 1, "tenant_id": "tenant_h", "ledger_scope": "live"},
    ]
    return store


def _snap(store: InMemoryCleanupStore) -> str:
    return json.dumps(store.to_obj(), sort_keys=True, default=str)


def _run(store, *extra):
    code = execute(["--ids", str(IDS), *(arg for tenant in TENANTS for arg in ("--tenant", tenant)), *extra], store=store)
    return code


def _report_from(text: str) -> dict:
    marker = "--- json ---"
    return json.loads(text.split(marker, 1)[1])


def _report(capsys) -> dict:
    return _report_from(capsys.readouterr().out)


def _row(report, store_name, tenant):
    for row in report["stores"]:
        if row["store"] == store_name and row["tenant"] == tenant:
            return row
    raise AssertionError(f"missing row {store_name} {tenant}")


def test_script_has_no_hardcoded_coin_or_default_tenants():
    source = (ROOT / "scripts" / "cleanup_id_list.py").read_text(encoding="utf-8")
    assert "2Z" not in source
    assert "default,henry" not in source
    assert "--tenant" in source
    assert UNTOUCHED.isdisjoint(
        {
            "mongo.orders_v2",
            "mongo.orders (embedded entries)",
            "mongo.positions (lots)",
            "mongo.trade_history (embedded trades)",
        }
    )


def test_pytest_cannot_open_mongo():
    with pytest.raises(RuntimeError, match="Mongo"):
        open_mongo_store()


def test_dry_run_changes_nothing_and_prints_ids_sha256(capsys):
    store = build_store()
    before = _snap(store)
    code = _run(store)
    assert code == 0
    assert _snap(store) == before
    assert store.sentinels == {"logs": ["audit-trail"], "redis": {"cache": "warm"}}
    out = capsys.readouterr().out
    assert f"ids_sha256: {sha256_file(IDS)}" in out
    assert "logs: untouched" in out
    assert "redis: untouched" in out
    report = _report_from(out)
    assert report["mode"] == "dry-run"
    assert report["ids_sha256"] == sha256_file(IDS)
    assert report["logs"] == "untouched"
    assert report["redis"] == "untouched"
    assert _row(report, "mongo.orders (embedded entries)", "tenant_a")["to_delete"] == 2
    assert _row(report, "mongo.orders (embedded entries)", "tenant_h")["to_delete"] == 3
    assert _row(report, "mongo.orders (embedded entries)", "tenant_h")["kept_henry_0812"] == 2
    assert _row(report, "mongo.orders (embedded entries)", "tenant_c")["kept_ctexp"] == 2
    assert _row(report, "mongo.orders (embedded entries)", "tenant_c")["to_delete"] == 0
    assert _row(report, "mongo.positions (lots)", "tenant_h")["to_delete"] == 1
    live = next(row for row in report["metrics"] if row["tenant"] == "tenant_h" and row["scope"] == "live")
    assert live["realized_after"] == pytest.approx(FIGURE)
    assert live["cash_after"] > live["cash_before"]
    preserved = [row for row in report["preserved_lot_realized_pnl"] if row["tenant"] == "tenant_h"]
    assert preserved[0]["realized_pnl"] == pytest.approx(FIGURE)
    assert any(note["status"] in {"mismatch", "index_out_of_range"} for note in report["index_cross_check"])
    assert "AAA/USDT" in report["dca_policy_symbols"]


def test_real_run_without_sha256_refuses(capsys):
    store = build_store()
    before = _snap(store)
    code = _run(store, "--apply")
    assert code == 2
    assert _snap(store) == before
    assert "refusing real run" in capsys.readouterr().out


def test_real_run_wrong_ids_sha256_refuses(tmp_path, capsys):
    store = build_store()
    before = _snap(store)
    backup = tmp_path / "backup.bin"
    backup.write_bytes(b"fresh-backup")
    code = _run(
        store,
        "--apply",
        "--backup",
        str(backup),
        "--backup-sha256",
        sha256_file(backup),
        "--ids-sha256",
        "a" * 64,
    )
    assert code == 2
    assert _snap(store) == before
    assert "ids-sha256" in capsys.readouterr().out


def test_real_run_wrong_backup_sha256_refuses(tmp_path, capsys):
    store = build_store()
    before = _snap(store)
    backup = tmp_path / "backup.bin"
    backup.write_bytes(b"fresh-backup")
    code = _run(
        store,
        "--apply",
        "--backup",
        str(backup),
        "--backup-sha256",
        "b" * 64,
        "--ids-sha256",
        sha256_file(IDS),
    )
    assert code == 2
    assert _snap(store) == before
    assert "backup-sha256" in capsys.readouterr().out


def test_changed_ids_file_hash_refuses(tmp_path, capsys):
    store = build_store()
    before = _snap(store)
    original = sha256_file(IDS)
    changed = tmp_path / "ids.json"
    payload = json.loads(IDS.read_text(encoding="utf-8"))
    payload["stores"]["mongo.memory_trades"]["in_scope_ids"].append({"_id": "mem_trade_stay"})
    changed.write_text(json.dumps(payload), encoding="utf-8")
    backup = tmp_path / "backup.bin"
    backup.write_bytes(b"fresh-backup")
    code = execute(
        [
            "--ids",
            str(changed),
            "--tenant",
            "tenant_a",
            "--tenant",
            "tenant_h",
            "--apply",
            "--backup",
            str(backup),
            "--backup-sha256",
            sha256_file(backup),
            "--ids-sha256",
            original,
        ],
        store=store,
    )
    assert code == 2
    assert _snap(store) == before
    assert sha256_file(changed) != original
    assert "ids-sha256" in capsys.readouterr().out


def _apply(store, tmp_path, capsys):
    backup = tmp_path / "backup.bin"
    backup.write_bytes(b"fresh-backup")
    code = _run(
        store,
        "--apply",
        "--backup",
        str(backup),
        "--backup-sha256",
        sha256_file(backup),
        "--ids-sha256",
        sha256_file(IDS),
    )
    report = _report(capsys)
    return code, report


def _order_ids(store, doc_id):
    return [row["id"] for row in store.orders[doc_id]["orders"]]


def test_apply_removes_by_id_recomputes_and_preserves_figure(tmp_path, capsys):
    store = build_store()
    code, report = _apply(store, tmp_path, capsys)
    assert code == 0
    assert report["mode"] == "applied"

    # Index 0 was a different order than the id list claimed. It stays.
    assert _order_ids(store, "tenant_a:demo")[0] == "ord_a_before"
    assert "ord_a_entry" not in _order_ids(store, "tenant_a:demo")
    assert "ord_a_dca" not in _order_ids(store, "tenant_a:demo")
    assert "ord_a_other" in _order_ids(store, "tenant_a:demo")
    assert store.orders["tenant_a:demo"]["_id"] == "tenant_a:demo"

    assert "ord_h_entry" not in _order_ids(store, "tenant_h:demo")
    assert "ord_h_other" in _order_ids(store, "tenant_h:demo")
    assert _order_ids(store, "tenant_h:live") == ["ord_keep_buy", "ord_keep_sell"]
    assert "ord_ctexp_1" in _order_ids(store, "tenant_c:demo")
    assert "ord_ctexp_2" in _order_ids(store, "tenant_c:demo")
    assert "tenant_h:live:ord_keep_sell" in store.v2
    assert "tenant_a:demo:ord_a_entry" not in store.v2
    assert "tenant_c:demo:ord_ctexp_1" in store.v2

    assert "AAA_USDT_1h" not in store.positions["tenant_h:demo"]["positions"]
    assert store.positions["tenant_h:demo"]["positions"]["BBB_USDT_4h"]["realized_pnl"] == 1.5
    assert "BBB_USDT_4h" in store.positions["tenant_a:demo"]["positions"]
    assert store.positions["tenant_h:demo"]["_id"] == "tenant_h:demo"

    live_ids = [row["order_id"] for row in store.trades["tenant_h:live"]["trades"]]
    assert live_ids == ["ord_keep_buy", "ord_keep_sell"]
    assert store.trades["tenant_h:live"]["realized_pnl"] == pytest.approx(FIGURE)
    assert store.trades["tenant_h:live"]["trades"][0]["id"] == "tenant_h:live#663"
    demo_trade_ids = [row["order_id"] for row in store.trades["tenant_h:demo"]["trades"]]
    assert demo_trade_ids == ["ord_h_other"]
    assert [row["order_id"] for row in store.trades["tenant_c:demo"]["trades"]] == ["ord_ctexp_1"]

    assert "mem_trade_1" not in store.memory["trades"]
    assert "mem_trade_2" not in store.memory["trades"]
    assert "mem_trade_stay" in store.memory["trades"]
    assert "evt_listed" not in store.memory["events"]
    assert "evt_new" not in store.memory["events"]
    assert "evt_keep_0812" in store.memory["events"]
    assert "evt_before" in store.memory["events"]
    assert "evt_cmc" in store.memory["events"]
    assert "rag_listed" not in store.memory["rag"]
    assert "rag_by_source" not in store.memory["rag"]
    assert "rag_keep" in store.memory["rag"]
    assert "rag_keep_fill" in store.memory["rag"]
    assert "rag_other" in store.memory["rag"]
    assert "lesson_1" not in store.memory["lessons"]
    assert "lesson_stay" in store.memory["lessons"]
    assert "tenant_a|demo|AAA/USDT" not in store.memory["profiles"]
    assert "tenant_h|demo|BBB/USDT" in store.memory["profiles"]

    nav = store.nav[("tenant_h", "live")]
    assert nav[0]["date"] == "2026-08-01"
    assert nav[0]["nav"] == 111
    assert any(point["date"] >= "2026-09-27" and point["nav"] != 1 for point in nav)
    assert store.sentinels["logs"] == ["audit-trail"]
    assert store.sentinels["redis"]["cache"] == "warm"

    live = next(row for row in report["metrics"] if row["tenant"] == "tenant_h" and row["scope"] == "live")
    assert live["realized_after"] == pytest.approx(FIGURE)
    assert any(row["realized_pnl"] == pytest.approx(FIGURE) for row in report["preserved_lot_realized_pnl"])


def test_second_apply_is_idempotent(tmp_path, capsys):
    store = build_store()
    code, _report = _apply(store, tmp_path, capsys)
    assert code == 0
    after_first = _snap(store)
    code, report = _apply(store, tmp_path, capsys)
    assert code == 0
    assert _snap(store) == after_first
    assert all(row["to_delete"] == 0 for row in report["stores"])
    assert store.trades["tenant_h:live"]["realized_pnl"] == pytest.approx(FIGURE)


def test_one_tenant_does_not_touch_other_ledger_or_memory(tmp_path, capsys):
    store = build_store()
    backup = tmp_path / "backup.bin"
    backup.write_bytes(b"fresh-backup")
    code = execute(
        [
            "--ids",
            str(IDS),
            "--tenant",
            "tenant_a",
            "--apply",
            "--backup",
            str(backup),
            "--backup-sha256",
            sha256_file(backup),
            "--ids-sha256",
            sha256_file(IDS),
        ],
        store=store,
    )
    report = _report(capsys)
    assert code == 0
    assert "ord_a_entry" not in _order_ids(store, "tenant_a:demo")
    assert "ord_h_entry" in _order_ids(store, "tenant_h:demo")
    assert "AAA_USDT_1h" in store.positions["tenant_h:demo"]["positions"]
    assert store.positions["tenant_h:demo"]["positions"]["AAA_USDT_1h"]["realized_pnl"] == FIGURE
    assert "mem_trade_1" in store.memory["trades"]
    assert "evt_new" in store.memory["events"]
    assert report["memory_applied"] is False
    assert any("no reliable tenant" in note for note in report["warnings"])


def test_stop_on_error_without_transaction_reports_completed_steps(tmp_path, capsys):
    store = build_store()
    store.transactions_enabled = False
    store.fail_on = ("tenant_a", "trade_history")
    backup = tmp_path / "backup.bin"
    backup.write_bytes(b"fresh-backup")
    code = _run(
        store,
        "--apply",
        "--backup",
        str(backup),
        "--backup-sha256",
        sha256_file(backup),
        "--ids-sha256",
        sha256_file(IDS),
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "stopped on first error" in out
    assert "tenant_a:orders" in out
    assert "ord_a_entry" not in _order_ids(store, "tenant_a:demo")
    assert any(row["order_id"] == "ord_a_entry" for row in store.trades["tenant_a:demo"]["trades"])
    assert "AAA_USDT_1h" in store.positions["tenant_a:demo"]["positions"]
    assert "ord_h_entry" in _order_ids(store, "tenant_h:demo")


def test_transaction_rolls_back_failed_tenant_only(tmp_path, capsys):
    store = build_store()
    store.transactions_enabled = True
    store.fail_on = ("tenant_h", "positions")
    backup = tmp_path / "backup.bin"
    backup.write_bytes(b"fresh-backup")
    before_h = json.dumps(store.orders["tenant_h:demo"], sort_keys=True)
    code = _run(
        store,
        "--apply",
        "--backup",
        str(backup),
        "--backup-sha256",
        sha256_file(backup),
        "--ids-sha256",
        sha256_file(IDS),
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "stopped on first error" in out
    assert "tenant_a" in out
    assert "ord_a_entry" not in _order_ids(store, "tenant_a:demo")
    assert json.dumps(store.orders["tenant_h:demo"], sort_keys=True) == before_h
    assert "AAA_USDT_1h" in store.positions["tenant_h:demo"]["positions"]
