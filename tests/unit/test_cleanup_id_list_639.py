"""Id-list cleanup for #639. Fixtures are synthetic; the real id list stays outside the repo."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.cleanup_id_list import (
    UNTOUCHED,
    CleanupAborted,
    InMemoryCleanupStore,
    _parse_dt,
    apply_plan,
    build_plan,
    commit_nav_result,
    execute,
    open_mongo_store,
    sha256_file,
)

ROOT = Path(__file__).resolve().parents[2]
IDS = ROOT / "tests" / "fixtures" / "cleanup_639" / "synthetic_scope_ids.json"
FIGURE = 23.63710924
STORED_LIVE_REALIZED = 17.38710924
DEMO_CASH = 9439.73285138
TENANTS = ["tenant_a", "tenant_h"]


def _order(oid, *, side, symbol, usdt, amount, price, ts, tenant, scope, pnl=None, fee=0.0):
    row = {
        "id": oid,
        "order_id": oid,
        "side": side,
        "symbol": symbol,
        "timeframe": "1h",
        "status": "filled",
        "fee": fee,
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


def _trade(
    oid,
    *,
    symbol,
    ts,
    trade_id=None,
    side=None,
    price=None,
    qty=None,
    fee=None,
    pnl=None,
    usdt_amount=None,
    usdt_received=None,
):
    row = {
        "id": trade_id or f"trade-{oid}",
        "order_id": oid,
        "symbol": symbol,
        "timestamp": ts,
        "timestamps": {"filled": ts},
    }
    if side is not None:
        row["type"] = side
        row["side"] = side
    if price is not None:
        row["price"] = price
    if qty is not None:
        row["amount"] = qty
    if fee is not None:
        row["fee"] = fee
    if pnl is not None:
        row["pnl"] = pnl
    if usdt_amount is not None:
        row["usdt_amount"] = usdt_amount
    if usdt_received is not None:
        row["usdt_received"] = usdt_received
    return row


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
            _order("ord_h_entry", side="buy", symbol="AAA/USDT", usdt=20.5, amount=10, price=2, fee=0.5, ts="2026-09-27T21:40:00", tenant="tenant_h", scope="demo"),
            _order("ord_h_dca_demo", side="buy", symbol="AAA/USDT", usdt=1.1, amount=1, price=1, fee=0.1, ts="2026-09-30T12:00:00", tenant="tenant_h", scope="demo"),
        ],
        "tenant_h:live": [
            _order("ord_keep_buy", side="buy", symbol="AAA/USDT", usdt=10, amount=10, price=1, ts="2026-08-12T10:00:00", tenant="tenant_h", scope="live"),
            _order("ord_keep_sell", side="sell", symbol="AAA/USDT", usdt=33.637, amount=10, price=3.3637, pnl=FIGURE, ts="2026-08-12T11:00:00", tenant="tenant_h", scope="live"),
            _order("ord_h_extra_buy", side="buy", symbol="BBB/USDT", usdt=7.5, amount=1, price=7.5, ts="2026-08-19T00:00:00", tenant="tenant_h", scope="live"),
            _order("ord_h_extra_kept", side="sell", symbol="BBB/USDT", usdt=7.5, amount=1, price=7.5, pnl=7.5, ts="2026-08-20T00:00:00", tenant="tenant_h", scope="live"),
            _order("ord_h_dca_live", side="buy", symbol="AAA/USDT", usdt=3.3, amount=3, price=1, fee=0.3, ts="2026-10-02T12:00:00", tenant="tenant_h", scope="live"),
        ],
        "tenant_a:live": [
            _order("ord_a_live_quiet", side="buy", symbol="BBB/USDT", usdt=12, amount=1, price=1, ts="2026-10-01T00:00:00", tenant="tenant_a", scope="live"),
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
    store.orders["tenant_a:live"]["seal"] = "untouched-orders"

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
        virtual_balance=1,
        realized_pnl=0,
        trades=[
            _trade("ord_a_before", symbol="AAA/USDT", ts="2026-08-01T00:00:00"),
            _trade(
                "ord_a_entry",
                symbol="AAA/USDT",
                ts="2026-09-27T21:36:00",
                side="BUY",
                price=1,
                qty=2,
                fee=0,
                usdt_amount=2,
            ),
            _trade(
                "ord_a_dca",
                symbol="AAA/USDT",
                ts="2026-09-30T12:00:00",
                side="BUY",
                price=1,
                qty=1,
                fee=0,
                usdt_amount=1,
            ),
        ],
    )
    store.trades["tenant_h:demo"] = _ledger(
        "tenant_h:demo",
        "tenant_h",
        "demo",
        virtual_balance=DEMO_CASH,
        realized_pnl=11.5,
        trades=[
            _trade("ord_h_other", symbol="BBB/USDT", ts="2026-09-01T00:00:00"),
            _trade(
                "ord_h_entry",
                symbol="AAA/USDT",
                ts="2026-09-27T21:40:00",
                trade_id="fill_h_entry",
                side="BUY",
                price=2,
                qty=10,
                fee=0.5,
                usdt_amount=20.5,
            ),
            _trade(
                "ord_h_dca_demo",
                symbol="AAA/USDT",
                ts="2026-09-30T12:00:00",
                trade_id="fill_h_dca_demo",
                side="BUY",
                price=1,
                qty=1,
                fee=0.1,
                usdt_amount=1.1,
            ),
        ],
    )
    store.trades["tenant_h:live"] = _ledger(
        "tenant_h:live",
        "tenant_h",
        "live",
        virtual_balance=5000,
        realized_pnl=STORED_LIVE_REALIZED,
        figure_note="kept-round-trip",
        trades=[
            _trade("ord_keep_buy", symbol="AAA/USDT", ts="2026-08-12T10:00:00", trade_id="tenant_h:live#663"),
            _trade(
                "ord_keep_sell",
                symbol="AAA/USDT",
                ts="2026-08-12T11:00:00",
                trade_id="tenant_h:live#671",
                side="SELL",
                price=3.3637,
                qty=10,
                fee=0,
                pnl=FIGURE,
                usdt_received=33.637,
            ),
            _trade(
                "ord_h_dca_live",
                symbol="AAA/USDT",
                ts="2026-10-02T12:00:00",
                trade_id="fill_partial_a",
                side="BUY",
                price=1,
                qty=1,
                fee=0.1,
                usdt_amount=1.1,
            ),
            _trade(
                "ord_h_dca_live",
                symbol="AAA/USDT",
                ts="2026-10-02T12:00:01",
                trade_id="fill_partial_b",
                side="BUY",
                price=1,
                qty=2,
                fee=0.2,
                usdt_amount=2.2,
            ),
            _trade(
                "ord_orphan_sell",
                symbol="AAA/USDT",
                ts="2026-10-02T15:00:00",
                trade_id="fill_orphan_sell",
                side="SELL",
                price=4,
                qty=2,
                fee=0.4,
                pnl=-6.25,
                usdt_received=7.6,
            ),
        ],
    )
    store.trades["tenant_a:live"] = _ledger(
        "tenant_a:live",
        "tenant_a",
        "live",
        virtual_balance=4242,
        realized_pnl=FIGURE,
        seal="untouched-trades",
        trades=[_trade("ord_a_live_quiet", symbol="BBB/USDT", ts="2026-10-01T00:00:00")],
    )
    store.trades["tenant_c:demo"] = _ledger(
        "tenant_c:demo",
        "tenant_c",
        "demo",
        virtual_balance=5000,
        realized_pnl=0,
        trades=[_trade("ord_ctexp_1", symbol="AAA/USDT", ts="2026-09-29T00:00:00")],
    )
    store.memory["trades"]["mem_trade_1"] = {
        "_id": "mem_trade_1",
        "tenant_id": "not_a_real_tenant",
        "symbol": "AAA/USDT",
        "order_id": "ord_a_entry",
    }
    store.memory["trades"]["mem_trade_2"] = {
        "_id": "mem_trade_2",
        "tenant_id": "tenant_h",
        "symbol": "AAA/USDT",
        "order_id": "ord_h_entry",
    }
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
        {
            "date": "2026-09-28",
            "as_of": "2026-09-28T16:00:00+00:00",
            "nav": 5,
            "cash": 1,
            "positions_mtm": 4,
            "mark": "market",
            "tenant_id": "tenant_h",
            "ledger_scope": "live",
        },
        {
            "date": "2026-10-02",
            "as_of": "2026-10-02T11:00:00+00:00",
            "nav": 250.5,
            "cash": 80.25,
            "positions_mtm": 170.25,
            "realized_pnl": STORED_LIVE_REALIZED,
            "btc_close": 99999,
            "mark": "market",
            "tenant_id": "tenant_h",
            "ledger_scope": "live",
        },
    ]
    store.nav[("tenant_a", "live")] = [
        {"date": "2026-10-03", "nav": 77, "seal": "untouched-nav", "tenant_id": "tenant_a", "ledger_scope": "live"},
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
    assert "replay_simulated_ledger" not in source
    assert "missing initial_capital" not in source
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
    assert live["realized_pnl_before"] == STORED_LIVE_REALIZED
    assert live["realized_pnl_delta"] == 6.25
    assert live["realized_pnl_after"] == FIGURE
    assert live["virtual_balance_before"] == 5000
    assert live["virtual_balance_delta"] == -4.3
    assert live["virtual_balance_after"] == 4995.7
    assert live["writes_metrics"] is True
    assert live["writes_nav"] is True
    demo = next(row for row in report["metrics"] if row["tenant"] == "tenant_h" and row["scope"] == "demo")
    assert demo["virtual_balance_before"] == DEMO_CASH
    assert demo["virtual_balance_delta"] == 21.6
    assert demo["virtual_balance_after"] == 9461.33285138
    assert demo["realized_pnl_before"] == 11.5
    assert demo["realized_pnl_delta"] == 0
    assert demo["realized_pnl_after"] == 11.5
    henry_fills = [row for row in report["fill_groups"] if row["tenant"] == "tenant_h"]
    assert len(henry_fills) == 5
    assert len({row["trade_id"] for row in henry_fills}) == 5
    entry_fill = next(row for row in henry_fills if row["trade_id"] == "fill_h_entry")
    assert entry_fill["cash_effect"] == 20.5
    assert entry_fill["orders_doc_id"] == "tenant_h:demo"
    assert entry_fill["orders_v2_id"] == "tenant_h:demo:ord_h_entry"
    assert entry_fill["memory_trade_ids"] == "mem_trade_2"
    assert entry_fill["position_keys"] == "AAA_USDT_1h"
    assert sum(1 for row in henry_fills if row["order_id"] == "ord_h_entry") == 1
    partials = [row for row in henry_fills if row["order_id"] == "ord_h_dca_live"]
    assert [row["qty"] for row in partials] == [1, 2]
    assert round(sum(row["cash_effect"] for row in partials), 8) == 3.3
    orphan = next(row for row in henry_fills if row["trade_id"] == "fill_orphan_sell")
    assert orphan["orders_doc_id"] == "-"
    assert orphan["orders_v2_id"] == "-"
    assert orphan["cash_effect"] == -7.6
    assert orphan["realized_effect"] == 6.25
    assert round(sum(row["cash_effect"] for row in henry_fills if row["scope"] == "demo"), 8) == demo["virtual_balance_delta"]
    assert round(sum(row["cash_effect"] for row in henry_fills if row["scope"] == "live"), 8) == live["virtual_balance_delta"]
    assert report["fill_problems"] == []
    assert report["fill_source"].endswith("trade_history (embedded trades)")
    assert "missing initial_capital" not in out
    assert "replay_cash" not in out
    assert "nav_day " in out
    held_day = next(row for row in report["nav_days"] if row["tenant"] == "tenant_h" and row["day"] == "2026-10-02")
    assert held_day["held_qty"] == "AAA/USDT:3"
    assert held_day["close"] == 5
    assert held_day["mtm_before"] == 170.25
    assert held_day["mtm_after"] == 155.25
    assert held_day["nav_before"] == 250.5
    assert held_day["nav_after"] == 231.2
    assert held_day["invariant_before"] == "ok"
    assert held_day["invariant_after"] == "ok"
    flat_day = next(row for row in report["nav_days"] if row["tenant"] == "tenant_h" and row["day"] == "2026-09-28")
    assert flat_day["held_qty"] == "0"
    assert flat_day["mtm_before"] == 4
    assert flat_day["mtm_after"] == 4
    assert flat_day["nav_before"] == 5
    assert flat_day["nav_after"] == 5
    assert flat_day["invariant_before"] == "ok"
    assert flat_day["invariant_after"] == "ok"
    quiet = next(row for row in report["metrics"] if row["tenant"] == "tenant_a" and row["scope"] == "live")
    assert quiet["realized_pnl_before"] == FIGURE
    assert quiet["writes_metrics"] is False
    assert quiet["writes_nav"] is False
    assert quiet["realized_pnl_after"] == FIGURE
    assert quiet["virtual_balance_delta"] == 0
    preserved = [row for row in report["preserved_lot_realized_pnl"] if row["tenant"] == "tenant_h"]
    assert preserved[0]["realized_pnl"] == pytest.approx(FIGURE)
    assert any(note["status"] in {"mismatch", "index_out_of_range"} for note in report["index_cross_check"])
    assert "AAA/USDT" in report["dca_policy_symbols"]
    assert "--- expected_counts ---" in out
    assert report["expected_count_problems"] == []
    assert report["expected_counts_actual"]["mongo.orders_v2"]["tenant_h"] == 3
    assert report["expected_counts_actual"]["mongo.trade_history (embedded trades)"]["tenant_h"] == 5
    assert _row(report, "mongo.orders (embedded entries)", "tenant_h")["to_delete"] == 3


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
    quiet_orders = json.dumps(store.orders["tenant_a:live"], sort_keys=True)
    quiet_trades = json.dumps(store.trades["tenant_a:live"], sort_keys=True)
    quiet_nav = json.dumps(store.nav[("tenant_a", "live")], sort_keys=True)
    other_trades = json.dumps(store.trades["tenant_c:demo"], sort_keys=True)
    code, report = _apply(store, tmp_path, capsys)
    assert code == 0, report.get("fill_problems")
    assert report["mode"] == "applied"

    # Index 0 was a different order than the id list claimed. It stays.
    assert _order_ids(store, "tenant_a:demo")[0] == "ord_a_before"
    assert "ord_a_entry" not in _order_ids(store, "tenant_a:demo")
    assert "ord_a_dca" not in _order_ids(store, "tenant_a:demo")
    assert "ord_a_other" in _order_ids(store, "tenant_a:demo")
    assert store.orders["tenant_a:demo"]["_id"] == "tenant_a:demo"

    assert "ord_h_entry" not in _order_ids(store, "tenant_h:demo")
    assert "ord_h_other" in _order_ids(store, "tenant_h:demo")
    assert _order_ids(store, "tenant_h:live") == [
        "ord_keep_buy",
        "ord_keep_sell",
        "ord_h_extra_buy",
        "ord_h_extra_kept",
    ]
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
    assert store.trades["tenant_h:live"]["realized_pnl"] == FIGURE
    assert store.trades["tenant_h:live"]["virtual_balance"] == 4995.7
    assert store.trades["tenant_h:demo"]["virtual_balance"] == 9461.33285138
    assert store.trades["tenant_h:demo"]["realized_pnl"] == 11.5
    assert store.trades["tenant_h:live"]["figure_note"] == "kept-round-trip"
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
    assert nav[0] == {
        "date": "2026-08-01",
        "nav": 111,
        "cash": 111,
        "tenant_id": "tenant_h",
        "ledger_scope": "live",
    }
    kept_day = next(point for point in nav if point["date"] == "2026-09-28")
    assert kept_day["nav"] == 5
    assert kept_day["cash"] == 1
    assert kept_day["positions_mtm"] == 4
    assert kept_day["mark"] == "market"
    marked = next(point for point in nav if point["date"] == "2026-10-02")
    assert marked["mark"] == "market"
    assert marked["realized_pnl"] == FIGURE
    assert marked["cash"] == 75.95
    assert marked["positions_mtm"] == 155.25
    assert marked["nav"] == 231.2
    assert marked["nav"] == marked["cash"] + marked["positions_mtm"]
    assert json.dumps(store.orders["tenant_a:live"], sort_keys=True) == quiet_orders
    assert json.dumps(store.trades["tenant_a:live"], sort_keys=True) == quiet_trades
    assert json.dumps(store.nav[("tenant_a", "live")], sort_keys=True) == quiet_nav
    assert store.trades["tenant_a:live"]["realized_pnl"] == FIGURE
    assert json.dumps(store.trades["tenant_c:demo"], sort_keys=True) == other_trades
    assert store.sentinels["logs"] == ["audit-trail"]
    assert store.sentinels["redis"]["cache"] == "warm"

    live = next(row for row in report["metrics"] if row["tenant"] == "tenant_h" and row["scope"] == "live")
    assert live["realized_pnl_before"] == STORED_LIVE_REALIZED
    assert live["realized_pnl_after"] == FIGURE
    assert any(row["realized_pnl"] == pytest.approx(FIGURE) for row in report["preserved_lot_realized_pnl"])


def test_second_apply_is_idempotent(tmp_path, capsys):
    store = build_store()
    code, _report = _apply(store, tmp_path, capsys)
    assert code == 0
    after_first = _snap(store)
    code, report = _apply(store, tmp_path, capsys)
    assert code == 2
    assert report["expected_count_problems"]
    assert _snap(store) == after_first
    assert all(row["to_delete"] == 0 for row in report["stores"])
    assert store.trades["tenant_h:live"]["realized_pnl"] == FIGURE


def test_one_tenant_does_not_touch_other_ledger_or_memory(tmp_path, capsys):
    store = build_store()
    before = _snap(store)
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
    out = capsys.readouterr().out
    report = _report_from(out)
    assert code == 2
    assert "refusing real run" in out
    assert "no reliable tenant" in out
    assert _snap(store) == before
    assert "ord_a_entry" in _order_ids(store, "tenant_a:demo")
    assert "ord_h_entry" in _order_ids(store, "tenant_h:demo")
    assert "mem_trade_1" in store.memory["trades"]
    assert report["memory_applied"] is False


def test_apply_refuses_without_transactions(tmp_path, capsys):
    store = build_store()
    before = _snap(store)
    store.transactions_enabled = False
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
    assert code == 2
    assert "transactions are not available" in out
    assert _snap(store) == before
    assert "ord_a_entry" in _order_ids(store, "tenant_a:demo")


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


def _write_ids(tmp_path, mutate) -> Path:
    payload = json.loads(IDS.read_text(encoding="utf-8"))
    mutate(payload)
    path = tmp_path / "ids.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _apply_ids(store, path, tmp_path):
    backup = tmp_path / "backup.bin"
    backup.write_bytes(b"fresh-backup")
    return execute(
        [
            "--ids",
            str(path),
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
            sha256_file(path),
        ],
        store=store,
    )


def test_change_between_plan_and_apply_aborts_with_no_write():
    store = build_store()
    spec = json.loads(IDS.read_text(encoding="utf-8"))
    plan = build_plan(store, spec, TENANTS, ids_sha256="x", ids_file=str(IDS))
    store.orders["tenant_a:demo"]["orders"].append({"id": "concurrent", "order_id": "concurrent"})
    before = _snap(store)
    with pytest.raises(CleanupAborted, match="abort"):
        apply_plan(store, plan)
    assert _snap(store) == before
    assert "ord_a_entry" in _order_ids(store, "tenant_a:demo")
    assert any(row.get("id") == "concurrent" for row in store.orders["tenant_a:demo"]["orders"])


def test_missing_count_refuses_apply(tmp_path, capsys):
    store = build_store()
    before = _snap(store)
    path = _write_ids(tmp_path, lambda payload: payload["expected_counts"].pop("mongo.memory_lessons"))
    code = _apply_ids(store, path, tmp_path)
    out = capsys.readouterr().out
    assert code == 2
    assert "expected_counts" in out
    assert "mongo.memory_lessons" in out
    assert _snap(store) == before


def test_count_mismatch_one_tenant_refuses_apply(tmp_path, capsys):
    store = build_store()
    before = _snap(store)
    path = _write_ids(
        tmp_path,
        lambda payload: payload["expected_counts"]["mongo.orders_v2"].__setitem__("tenant_h", 9),
    )
    code = _apply_ids(store, path, tmp_path)
    out = capsys.readouterr().out
    assert code == 2
    assert "tenant_h" in out
    assert "expected 9" in out
    assert _snap(store) == before
    assert "ord_h_entry" in _order_ids(store, "tenant_h:demo")


def test_unknown_event_counts_print_on_dry_run_and_block_apply(tmp_path, capsys):
    store = build_store()
    before = _snap(store)

    def blank_events(payload):
        payload["expected_counts"]["mongo.memory_market_events"] = None
        payload["expected_counts"]["mongo.memory_rag_chunks"] = None

    path = _write_ids(tmp_path, blank_events)
    code = execute(
        ["--ids", str(path), "--tenant", "tenant_a", "--tenant", "tenant_h"],
        store=store,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert _snap(store) == before
    assert "--- expected_counts ---" in out
    assert "--- end expected_counts ---" in out
    block = out.split("--- expected_counts ---", 1)[1].split("--- end expected_counts ---", 1)[0]
    pasted = json.loads(block)["expected_counts"]
    assert pasted["mongo.memory_market_events"]["tenant_h"] == 1
    assert pasted["mongo.memory_rag_chunks"]["tenant_a"] == 2
    assert pasted["mongo.orders_v2"]["tenant_a"] == 2
    code = _apply_ids(store, path, tmp_path)
    out = capsys.readouterr().out
    assert code == 2
    assert "missing or unknown" in out
    assert _snap(store) == before


def test_nav_failure_raises_and_rolls_back_that_tenant(tmp_path, capsys):
    with pytest.raises(RuntimeError, match="nav history replace failed"):
        commit_nav_result(False)
    store = build_store()
    store.nav_ok = False
    before_nav = json.dumps(store.nav[("tenant_h", "live")], sort_keys=True)
    before_h_orders = json.dumps(store.orders["tenant_h:live"], sort_keys=True)
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
    assert "nav history replace failed" in out
    assert json.dumps(store.nav[("tenant_h", "live")], sort_keys=True) == before_nav
    assert json.dumps(store.orders["tenant_h:live"], sort_keys=True) == before_h_orders
    assert "ord_h_entry" in _order_ids(store, "tenant_h:demo")
    assert "ord_a_entry" not in _order_ids(store, "tenant_a:demo")


def test_help_documents_expected_counts(capsys):
    with pytest.raises(SystemExit) as caught:
        execute(["--help"])
    assert caught.value.code == 0
    out = capsys.readouterr().out
    assert "expected_counts" in out
    assert "per tenant" in out
    assert "mongo.memory_market_events" in out
    assert "mongo.memory_rag_chunks" in out
    assert "fill source" in out
    assert "trade_history" in out
    assert "does not read initial_capital" in out
    assert "nav_prices" in out
    assert "Europe/Berlin" in out
    assert "counts once" in out
    assert "100000" in out
    assert "2Z" not in out


def test_expected_counts_example_is_synthetic():
    path = ROOT / "tests" / "fixtures" / "cleanup_639" / "expected_counts.example.json"
    text = path.read_text(encoding="utf-8")
    payload = json.loads(text)
    counts = payload["expected_counts"]
    assert counts["mongo.memory_market_events"] is None
    assert counts["mongo.memory_rag_chunks"] is None
    assert counts["mongo.orders_v2"]["tenant_a"] == 2
    assert counts["mongo.orders_v2"]["tenant_h"] == 1
    assert "c1278fdd8690" not in text
    assert "2Z" not in text


def test_parse_dt_normalizes_non_utc_offset():
    cutoff = _parse_dt("2026-09-27T19:36:00")
    assert _parse_dt("2026-09-27T21:36:00+02:00") == cutoff
    assert _parse_dt("2026-09-27T19:36:00Z") == cutoff
    assert cutoff.tzinfo is None


def test_qty_mismatch_aborts_before_write(tmp_path, capsys):
    store = build_store()
    partial = next(row for row in store.trades["tenant_h:live"]["trades"] if row["id"] == "fill_partial_b")
    partial["amount"] = 9
    partial["usdt_amount"] = 9.2
    before = _snap(store)
    code = _run(store)
    out = capsys.readouterr().out
    assert code == 2
    assert "qty sum" in out
    assert _snap(store) == before
    code = _apply_ids(store, IDS, tmp_path)
    assert code == 2
    assert _snap(store) == before


def test_missing_fee_aborts_before_write(capsys):
    store = build_store()
    entry = next(row for row in store.trades["tenant_h:demo"]["trades"] if row["id"] == "fill_h_entry")
    entry.pop("fee")
    before = _snap(store)
    code = _run(store)
    out = capsys.readouterr().out
    assert code == 2
    assert "missing fee" in out
    assert "missing initial_capital" not in out
    assert _snap(store) == before


def test_missing_nav_cash_aborts_before_write(capsys):
    store = build_store()
    point = next(row for row in store.nav[("tenant_h", "live")] if row["date"] == "2026-10-02")
    point.pop("cash")
    before = _snap(store)
    code = _run(store)
    out = capsys.readouterr().out
    assert code == 2
    assert "missing nav or cash" in out
    assert _snap(store) == before


def _plan(store, spec=None):
    payload = spec if spec is not None else json.loads(IDS.read_text(encoding="utf-8"))
    return build_plan(store, payload, TENANTS, ids_sha256="test", ids_file=str(IDS))


def _ids_copy():
    return json.loads(IDS.read_text(encoding="utf-8"))


def test_mirrored_fill_counts_once_and_books_order_scope():
    store = build_store()
    demo = next(row for row in store.trades["tenant_h:demo"]["trades"] if row["id"] == "fill_h_entry")
    demo["id"] = ""
    mirror = json.loads(json.dumps(demo))
    mirror["timestamp"] = "2026-09-27T21:40:01"
    mirror["timestamps"] = {"filled": "2026-09-27T21:40:01"}
    store.trades["tenant_h:live"]["trades"].append(mirror)
    plan = _plan(store)
    assert plan.report["fill_problems"] == []
    rows = [row for row in plan.report["fill_groups"] if row["order_id"] == "ord_h_entry"]
    assert len(rows) == 1
    assert rows[0]["trade_id"] == "ord_h_entry@2026-09-27T19:40:00"
    assert rows[0]["scope"] == "demo"
    assert rows[0]["trade_scope"] == "demo"
    assert rows[0]["mirror_scope"] == "live"
    assert rows[0]["mirror_doc"] == "tenant_h:live"
    assert rows[0]["cash_effect"] == 20.5
    assert rows[0]["memory_trade_ids"] == "mem_trade_2"
    demo_metrics = next(row for row in plan.report["metrics"] if row["tenant"] == "tenant_h" and row["scope"] == "demo")
    live_metrics = next(row for row in plan.report["metrics"] if row["tenant"] == "tenant_h" and row["scope"] == "live")
    assert demo_metrics["virtual_balance_delta"] == 21.6
    assert live_metrics["virtual_balance_delta"] == -4.3
    apply_plan(store, plan)
    assert store.trades["tenant_h:demo"]["virtual_balance"] == 9461.33285138
    assert store.trades["tenant_h:demo"]["realized_pnl"] == 11.5
    assert store.trades["tenant_h:live"]["virtual_balance"] == 4995.7
    assert store.trades["tenant_h:live"]["realized_pnl"] == FIGURE
    assert "ord_h_entry" not in [row.get("order_id") for row in store.trades["tenant_h:live"]["trades"]]


def test_ambiguous_mirror_names_rows_and_aborts(capsys):
    store = build_store()
    demo = next(row for row in store.trades["tenant_h:demo"]["trades"] if row["id"] == "fill_h_entry")
    mirror = json.loads(json.dumps(demo))
    mirror["id"] = ""
    mirror["timestamp"] = "2026-09-27T23:40:00"
    mirror["timestamps"] = {"filled": mirror["timestamp"]}
    store.trades["tenant_h:live"]["trades"].append(mirror)
    before = _snap(store)
    code = _run(store)
    out = capsys.readouterr().out
    assert code == 2
    assert "ambiguous mirror" in out
    assert "tenant_h/demo" in out
    assert "tenant_h/live" in out
    assert "ord_h_entry" in out
    assert _snap(store) == before


def test_unmaintained_live_is_not_written_and_demo_nav_is_corrected():
    store = build_store()
    live = store.trades["tenant_a:live"]
    live["virtual_balance"] = 100000
    live["realized_pnl"] = 0
    demo_trades = store.trades["tenant_a:demo"]["trades"]
    moved = next(row for row in demo_trades if row["order_id"] == "ord_a_entry")
    store.trades["tenant_a:demo"]["trades"] = [row for row in demo_trades if row["order_id"] != "ord_a_entry"]
    live["trades"].append(moved)
    store.nav[("tenant_a", "demo")] = [
        {
            "date": "2026-09-27",
            "as_of": "2026-09-27T00:00:00",
            "nav": 10,
            "cash": 4,
            "positions_mtm": 6,
            "closes": {"AAA/USDT": 3},
            "tenant_id": "tenant_a",
            "ledger_scope": "demo",
        },
        {
            "date": "2026-10-03",
            "as_of": "2026-10-03T12:00:00",
            "nav": 30,
            "cash": 10,
            "positions_mtm": 20,
            "closes": {"AAA/USDT": 4},
            "tenant_id": "tenant_a",
            "ledger_scope": "demo",
        },
    ]
    before_live = json.dumps(live, sort_keys=True)
    before_live_nav = json.dumps(store.nav[("tenant_a", "live")], sort_keys=True)
    spec = _ids_copy()
    spec["nav_prices"] = list(spec["nav_prices"]) + [
        {
            "tenant": "tenant_a",
            "scope": "demo",
            "date": "2026-09-27",
            "as_of": "2026-09-27T00:00:00+00:00",
            "candle_start": "2026-09-27T00:00:00+00:00",
            "close": 3,
            "source": "https://api.gateio.ws/api/v4/spot/candlesticks",
            "fetched_at": "2026-10-06T08:00:00+00:00",
        },
        {
            "tenant": "tenant_a",
            "scope": "demo",
            "date": "2026-10-03",
            "as_of": "2026-10-03T12:00:00+00:00",
            "candle_start": "2026-10-03T12:00:00+00:00",
            "close": 4,
            "source": "https://api.gateio.ws/api/v4/spot/candlesticks",
            "fetched_at": "2026-10-06T08:00:00+00:00",
        },
    ]
    plan = _plan(store, spec)
    assert plan.report["fill_problems"] == [], plan.report["fill_problems"]
    assert any("skipped unmaintained live scope tenant_a/live" in note for note in plan.report["warnings"])
    demo = next(row for row in plan.report["metrics"] if row["tenant"] == "tenant_a" and row["scope"] == "demo")
    live_metrics = next(row for row in plan.report["metrics"] if row["tenant"] == "tenant_a" and row["scope"] == "live")
    assert demo["virtual_balance_before"] == 1
    assert demo["virtual_balance_delta"] == 3
    assert demo["virtual_balance_after"] == 4
    assert demo["realized_pnl_delta"] == 0
    assert live_metrics["virtual_balance_delta"] == 0
    assert live_metrics["writes_metrics"] is False
    assert live_metrics["writes_nav"] is False
    assert live_metrics["skipped_unmaintained"] is True
    early = next(row for row in plan.report["nav_days"] if row["scope"] == "demo" and row["day"] == "2026-09-27")
    assert early["held_qty"] == "0"
    assert early["mtm_before"] == 6
    assert early["mtm_after"] == 6
    assert early["nav_after"] == 12
    assert early["cash_after"] == 6
    assert early["invariant_before"] == "ok"
    assert early["invariant_after"] == "ok"
    later = next(row for row in plan.report["nav_days"] if row["scope"] == "demo" and row["day"] == "2026-10-03")
    assert later["held_qty"] == "AAA/USDT:3"
    assert later["close"] == 4
    assert later["mtm_before"] == 20
    assert later["mtm_after"] == 8
    assert later["nav_before"] == 30
    assert later["nav_after"] == 21
    assert later["cash_after"] == 13
    assert later["invariant_before"] == "ok"
    assert later["invariant_after"] == "ok"
    assert later["nav_after"] == later["cash_after"] + later["mtm_after"]
    apply_plan(store, plan)
    assert json.dumps(store.trades["tenant_a:live"], sort_keys=True) == before_live
    assert json.dumps(store.nav[("tenant_a", "live")], sort_keys=True) == before_live_nav
    assert store.trades["tenant_a:demo"]["virtual_balance"] == 4
    assert store.trades["tenant_a:demo"]["realized_pnl"] == 0
    written = next(point for point in store.nav[("tenant_a", "demo")] if point["date"] == "2026-10-03")
    assert written["positions_mtm"] == 8
    assert written["nav"] == 21
    assert written["cash"] == 13
    assert written["nav"] == written["cash"] + written["positions_mtm"]
    untouched = next(point for point in store.nav[("tenant_a", "demo")] if point["date"] == "2026-09-27")
    assert untouched["positions_mtm"] == 6


def test_missing_nav_price_entry_aborts_before_write(tmp_path, capsys):
    spec = _ids_copy()
    spec["nav_prices"] = [row for row in spec["nav_prices"] if row["date"] != "2026-10-02"]
    path = tmp_path / "ids.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    store = build_store()
    before = _snap(store)
    code = execute(
        ["--ids", str(path), "--tenant", "tenant_a", "--tenant", "tenant_h"],
        store=store,
    )
    out = capsys.readouterr().out
    assert code == 2
    assert "no price entry" in out
    assert "tenant_h/live" in out
    assert "2026-10-02" in out
    assert "btc_close" not in out.split("fill:", 1)[-1] or "99999" not in out
    assert _snap(store) == before
    assert _apply_ids(store, path, tmp_path) == 2
    assert _snap(store) == before


def test_nav_price_as_of_must_match_the_row(tmp_path, capsys):
    spec = _ids_copy()
    row = next(item for item in spec["nav_prices"] if item["date"] == "2026-10-02")
    row["as_of"] = "2026-10-02T12:00:00+00:00"
    path = tmp_path / "ids.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    store = build_store()
    before = _snap(store)
    code = execute(
        ["--ids", str(path), "--tenant", "tenant_a", "--tenant", "tenant_h"],
        store=store,
    )
    out = capsys.readouterr().out
    assert code == 2
    assert "as_of does not match" in out
    assert "tenant_h/live" in out
    assert "2026-10-02" in out
    assert _snap(store) == before


def test_candle_start_must_contain_as_of(tmp_path, capsys):
    spec = _ids_copy()
    row = next(item for item in spec["nav_prices"] if item["date"] == "2026-10-02")
    # 11:00 is the next candle, not inside [10:00, 11:00).
    row["candle_start"] = "2026-10-02T10:00:00+00:00"
    path = tmp_path / "ids.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    store = build_store()
    before = _snap(store)
    code = execute(
        ["--ids", str(path), "--tenant", "tenant_a", "--tenant", "tenant_h"],
        store=store,
    )
    out = capsys.readouterr().out
    assert code == 2
    assert "candle_start does not contain as_of" in out
    assert "tenant_h/live" in out
    assert "2026-10-02" in out
    assert _snap(store) == before


def test_nav_price_qty_field_is_rejected(tmp_path, capsys):
    spec = _ids_copy()
    spec["nav_prices"][0]["qty"] = 99
    path = tmp_path / "ids.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    store = build_store()
    before = _snap(store)
    code = execute(
        ["--ids", str(path), "--tenant", "tenant_a", "--tenant", "tenant_h"],
        store=store,
    )
    out = capsys.readouterr().out
    assert code == 2
    assert "must not set a quantity" in out
    assert "99" not in out.split("held_qty=", 1)[-1] if "held_qty=" in out else True
    assert _snap(store) == before


def test_naive_fill_time_is_berlin_against_utc_snapshot():
    store = build_store()
    store.nav[("tenant_h", "demo")] = [
        {
            "date": "2026-09-27",
            "as_of": "2026-09-27T19:50:00+00:00",
            "nav": 100,
            "cash": 40,
            "positions_mtm": 60,
            "tenant_id": "tenant_h",
            "ledger_scope": "demo",
        }
    ]
    spec = _ids_copy()
    spec["nav_prices"].append(
        {
            "tenant": "tenant_h",
            "scope": "demo",
            "date": "2026-09-27",
            "as_of": "2026-09-27T19:50:00+00:00",
            "candle_start": "2026-09-27T19:00:00+00:00",
            "close": 2,
            "source": "https://api.gateio.ws/api/v4/spot/candlesticks",
            "fetched_at": "2026-10-06T08:00:00+00:00",
        }
    )
    plan = _plan(store, spec)
    assert plan.report["fill_problems"] == [], plan.report["fill_problems"]
    point = next(row for row in plan.report["nav_days"] if row["scope"] == "demo" and row["day"] == "2026-09-27")
    # 21:40 Berlin is 19:40 UTC, ten minutes before 19:50Z. Read as UTC it would be after the snapshot.
    assert point["held_qty"] == "AAA/USDT:10"
    assert point["close"] == 2
    assert point["mtm_before"] == 60
    assert point["mtm_after"] == 40
    assert point["nav_after"] == 100.5
    assert point["cash_after"] == 60.5
    assert point["invariant_before"] == "ok"
    assert point["invariant_after"] == "ok"
    apply_plan(store, plan)
    written = store.nav[("tenant_h", "demo")][0]
    assert written["positions_mtm"] == 40
    assert written["nav"] == written["cash"] + written["positions_mtm"]


def test_live_only_fill_books_on_demo_order_scope():
    """A fill that exists only on the live trade document is booked on the order's demo scope."""
    store = build_store()
    demo_trades = store.trades["tenant_h:demo"]["trades"]
    moved = next(row for row in demo_trades if row["id"] == "fill_h_entry")
    store.trades["tenant_h:demo"]["trades"] = [row for row in demo_trades if row["id"] != "fill_h_entry"]
    store.trades["tenant_h:live"]["trades"].append(moved)
    plan = _plan(store)
    assert plan.report["fill_problems"] == [], plan.report["fill_problems"]
    row = next(item for item in plan.report["fill_groups"] if item["order_id"] == "ord_h_entry")
    assert row["scope"] == "demo"
    assert row["trade_scope"] == "live"
    assert row["cash_effect"] == 20.5
    demo = next(item for item in plan.report["metrics"] if item["tenant"] == "tenant_h" and item["scope"] == "demo")
    live = next(item for item in plan.report["metrics"] if item["tenant"] == "tenant_h" and item["scope"] == "live")
    assert demo["virtual_balance_delta"] == 21.6
    assert live["virtual_balance_delta"] == -4.3
    apply_plan(store, plan)
    assert store.trades["tenant_h:demo"]["virtual_balance"] == 9461.33285138
    assert store.trades["tenant_h:live"]["virtual_balance"] == 4995.7
    assert "ord_h_entry" not in [item.get("order_id") for item in store.trades["tenant_h:live"]["trades"]]


def test_broken_nav_invariant_aborts_before_write(capsys):
    store = build_store()
    point = next(row for row in store.nav[("tenant_h", "live")] if row["date"] == "2026-10-02")
    point["nav"] = 999
    before = _snap(store)
    code = _run(store)
    out = capsys.readouterr().out
    assert code == 2
    assert "invariant failed" in out
    assert "tenant_h/live" in out
    assert "2026-10-02" in out
    assert _snap(store) == before


def test_cli_dry_run_imports_without_pythonpath(tmp_path):
    # cwd is not the repo, so the script must put the repo root on sys.path.
    # A minimal config.json avoids the ledger logger retrying a missing file.
    (tmp_path / "config.json").write_text("{}\n", encoding="utf-8")
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "PYTHONPATH",
            "PYTEST_RUNNING",
            "PYTEST_CURRENT_TEST",
            "FORCE_OPERATOR_MONGO",
            "DEMO_ALLOW_REMOTE_MONGO",
            "MONGO_URL",
            "ALLOW_DEV_DB_MUTATION",
        }
    }
    env["MONGODB_URI"] = "mongodb://127.0.0.1:27017"
    env["MONGODB_DB"] = "xagent_pytest_smoke"
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "cleanup_id_list.py"),
            "--ids",
            str(IDS),
            "--tenant",
            "tenant_a",
            "--tenant",
            "tenant_h",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert "ModuleNotFoundError" not in proc.stderr
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "ids_sha256:" in proc.stdout
    assert "--apply" not in proc.args
