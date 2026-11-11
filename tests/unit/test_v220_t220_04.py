"""T220-04 draft (not a GitHub issue): signed net cash flow, default off.

The 1-unit short numbers are the sprint's synthetic example only.
They are not a production default and not a profitability result.
Tolerance 0.01 USDT is the frozen paper/testnet start value and is not widened.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from core.net_cashflow import account, reconcile_money, reconcile_quantity

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "net_cashflow"
INCLUDES = ["price", "fees", "funding"]


def _load(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _on(**extra: object) -> dict:
    config: dict = {
        "trading_mode": "paper",
        "accounting": {"net_cashflow_v220": {"enabled": True}},
        "shorts": {"allow_live": False},
    }
    config.update(extra)
    return config


def _reconcile(venue: str, *, tolerance: object = "0.01", ledger: str = "9.83") -> dict:
    return {
        "metric": "actual_net",
        "horizon": "trade",
        "ledger": ledger,
        "venue": venue,
        "tolerance": tolerance,
        "ledger_includes": list(INCLUDES),
        "venue_includes": ["funding", "price", "fees"],
    }


def test_ac04_example_receipt_is_9_83_and_payment_is_9_79():
    receipt = account(_on(), _load("short_receipt.json"))
    payment = account(_on(), _load("short_payment.json"))
    assert receipt["status"] == "accounted"
    assert receipt["price_pnl"] == "10"
    assert receipt["fees"] == "0.19"
    assert receipt["funding"] == "0.02"
    assert receipt["actual_net"] == "9.83"
    assert payment["actual_net"] == "9.79"
    assert receipt["live_pnl"] is False
    assert receipt["venue_calls"] == 0
    assert receipt["booked_ids"].count("fee:syn-fee-open") == 1
    assert receipt["booked_ids"].count("funding:syn-fund-receipt") == 1


def test_ac04_restart_does_not_double_book_ids_or_cumulative_qty():
    payload = _load("short_receipt.json")
    payload["fees"] = payload["fees"] + payload["fees"]
    payload["funding"] = payload["funding"] + payload["funding"]
    payload["adl"] = [
        {"record_id": "syn-adl-1", "signed_amount": "-0.50", "quote": "USDT"},
        {"record_id": "syn-adl-1", "signed_amount": "-0.50", "quote": "USDT"},
    ]
    result = account(_on(), payload)
    assert result["actual_net"] == "9.33"
    assert result["booked_ids"].count("fee:syn-fee-open") == 1
    assert result["booked_ids"].count("funding:syn-fund-receipt") == 1
    assert result["booked_ids"].count("adl:syn-adl-1") == 1

    cumulative = account(_on(), _load("cumulative_partial.json"))
    assert cumulative["qty"] == "1"
    assert cumulative["actual_net"] == "9.83"


def test_ac04_incremental_fills_sum_once_and_mixed_semantics_block():
    payload = _load("cumulative_partial.json")
    payload["fills"] = [
        {
            "order_id": "syn-ord-1",
            "record_id": "syn-trade-a",
            "semantics": "incremental",
            "filled_qty": "0.4",
        },
        {
            "order_id": "syn-ord-1",
            "record_id": "syn-trade-a",
            "semantics": "incremental",
            "filled_qty": "0.4",
        },
        {
            "order_id": "syn-ord-1",
            "record_id": "syn-trade-b",
            "semantics": "incremental",
            "filled_qty": "0.6",
        },
    ]
    assert account(_on(), payload)["qty"] == "1"

    payload["fills"].append({
        "order_id": "syn-ord-1",
        "record_id": "syn-snap-x",
        "semantics": "cumulative",
        "filled_qty": "1",
    })
    mixed = account(_on(), payload)
    assert mixed["actual_net"] is None
    assert "mixed_quantity_semantics" in mixed["reasons"]


def test_ac04_unknown_fee_and_missing_settlement_are_not_actual_zero():
    unknown = _load("short_receipt.json")
    unknown["fees"][0]["state"] = "unknown"
    unknown_result = account(_on(), unknown)
    assert unknown_result["actual_net"] is None
    assert unknown_result["status"] == "unknown"
    assert "fee_unknown" in unknown_result["reasons"]

    non_usdt = _load("short_receipt.json")
    non_usdt["fees"][1]["quote"] = "BTC"
    non_usdt_result = account(_on(), non_usdt)
    assert non_usdt_result["actual_net"] is None
    assert "non_usdt" in non_usdt_result["reasons"]

    floated = _load("short_receipt.json")
    floated["fees"][0]["amount"] = 0.10
    assert account(_on(), floated)["actual_net"] is None

    indicative = account(_on(), _load("indicative_funding.json"))
    assert indicative["actual_net"] is None
    assert "indicative_not_settlement" in indicative["reasons"]
    assert indicative["funding"] is None

    absent = account(_on(), _load("absent_settlement.json"))
    assert absent["actual_net"] is None
    assert "no_settlement_observed" in absent["reasons"]
    assert absent["actual_net"] != "0"


def test_ac04_paper_funding_stays_simulation_and_is_not_the_venue_leg():
    payload = _load("short_receipt.json")
    payload["funding"].append({
        "record_id": "syn-paper-fund",
        "signed_amount": "5",
        "quote": "USDT",
        "provenance": "paper_simulation",
    })
    result = account(_on(), payload)
    assert result["actual_net"] == "9.83"
    assert result["funding"] == "0.02"
    assert result["paper_funding"] == "5"
    assert result["paper_label"] == "simulation"

    paper_only = _load("short_receipt.json")
    paper_only["funding"] = [{
        "record_id": "syn-paper-only",
        "signed_amount": "0.02",
        "quote": "USDT",
        "provenance": "paper_simulation",
    }]
    only = account(_on(), paper_only)
    assert only["actual_net"] is None
    assert only["paper_funding"] == "0.02"
    assert only["paper_label"] == "simulation"
    assert "no_settlement_observed" in only["reasons"]


def test_ac04_settlement_window_bounds_and_late_publication():
    late = account(_on(), _load("short_receipt.json"))
    assert late["actual_net"] == "9.83"

    at_start = _load("short_receipt.json")
    at_start["funding"][0]["settlement_time"] = at_start["window"]["start"]
    at_start["funding"][0]["publication_time"] = "2026-11-09T00:00:00+00:00"
    assert account(_on(), at_start)["actual_net"] == "9.83"

    open_start = _load("short_receipt.json")
    open_start["funding"][0]["settlement_time"] = open_start["window"]["start"]
    open_start["window"]["inclusive_start"] = False
    assert "outside_window" in account(_on(), open_start)["reasons"]

    before = _load("short_receipt.json")
    before["funding"][0]["settlement_time"] = "2026-11-10T07:59:59+00:00"
    before["funding"][0]["publication_time"] = "2026-11-10T12:00:00+00:00"
    before_result = account(_on(), before)
    assert before_result["actual_net"] is None
    assert "outside_window" in before_result["reasons"]

    after = _load("short_receipt.json")
    after["funding"][0]["settlement_time"] = "2026-11-10T16:00:01+00:00"
    after["funding"][0]["publication_time"] = "2026-11-10T12:00:00+00:00"
    assert account(_on(), after)["actual_net"] is None

    at_end = _load("short_receipt.json")
    at_end["funding"][0]["settlement_time"] = at_end["window"]["end"]
    assert account(_on(), at_end)["actual_net"] == "9.83"

    open_end = _load("short_receipt.json")
    open_end["funding"][0]["settlement_time"] = open_end["window"]["end"]
    open_end["window"]["inclusive_end"] = False
    assert account(_on(), open_end)["actual_net"] is None

    missing_flags = _load("short_receipt.json")
    del missing_flags["window"]["inclusive_start"]
    del missing_flags["window"]["inclusive_end"]
    assert "window_unspecified" in account(_on(), missing_flags)["reasons"]

    naive = _load("short_receipt.json")
    naive["funding"][0]["settlement_time"] = "2026-11-10T12:00:00"
    assert "naive_or_bad_timestamp" in account(_on(), naive)["reasons"]


def test_ac04_tolerance_exact_t_passes_and_wider_t_is_rejected():
    exact = _load("short_receipt.json")
    exact["reconcile"] = _reconcile("9.84")
    exact_result = account(_on(), exact)
    assert exact_result["reconcile"]["pass"] is True
    assert exact_result["reconcile"]["frozen_t"] == "0.01"
    assert exact_result["reconcile"]["abs_diff"] == "0.01"

    quantum = _load("short_receipt.json")
    quantum["reconcile"] = _reconcile("9.84000001")
    quantum_result = account(_on(), quantum)
    assert quantum_result["reconcile"]["pass"] is False
    assert quantum_result["reconcile"]["status"] == "fail"

    widened = _load("short_receipt.json")
    widened["reconcile"] = _reconcile("9.85", tolerance="0.02")
    widened_result = account(_on(), widened)
    assert widened_result["reconcile"]["pass"] is False
    assert widened_result["reconcile"]["status"] == "blocked_unapproved_tolerance"

    missing = _load("short_receipt.json")
    block = _reconcile("9.83")
    del block["tolerance"]
    missing["reconcile"] = block
    assert account(_on(), missing)["reconcile"]["status"] == "blocked_missing_tolerance"

    direct = reconcile_money(
        ledger="0",
        venue="0.01000001",
        tolerance="0.01",
        metric="actual_net",
        horizon="trade",
        ledger_includes=INCLUDES,
        venue_includes=list(reversed(INCLUDES)),
    )
    assert direct["pass"] is False
    assert reconcile_money(
        ledger="0",
        venue="0.01",
        tolerance=None,
        metric="actual_net",
        horizon="trade",
        ledger_includes=INCLUDES,
        venue_includes=INCLUDES,
    )["status"] == "blocked_missing_tolerance"
    assert reconcile_money(
        ledger="9.83",
        venue="9.83",
        tolerance=0.01,
        metric="actual_net",
        horizon="trade",
        ledger_includes=INCLUDES,
        venue_includes=INCLUDES,
    )["status"] == "blocked_unapproved_tolerance"

    config = _on()
    config["accounting"]["net_cashflow_v220"]["tolerance"] = "1"
    configured = _load("short_receipt.json")
    configured["reconcile"] = _reconcile("9.85")
    assert account(config, configured)["reconcile"]["pass"] is False


def test_ac04_mapping_mismatch_does_not_subtract_fees_again():
    payload = _load("short_receipt.json")
    payload["adl"] = [{
        "record_id": "syn-venue-realised",
        "signed_amount": "9.83",
        "quote": "USDT",
        "includes": ["price", "fees", "funding"],
    }]
    payload["reconcile"] = _reconcile("9.83", ledger="9.83")
    result = account(_on(), payload)
    assert result["actual_net"] == "9.83"
    assert result["venue_figures"][0]["signed_amount"] == "9.83"
    assert result["reconcile"]["pass"] is True

    mismatch = _load("short_receipt.json")
    mismatch["reconcile"] = _reconcile("10")
    mismatch["reconcile"]["ledger_includes"] = ["price"]
    mismatch["reconcile"]["venue_includes"] = ["price", "fees", "funding"]
    mapped = account(_on(), mismatch)
    assert mapped["reconcile"]["status"] == "blocked_mapping"
    assert mapped["reconcile"]["pass"] is False
    assert mapped["actual_net"] == "9.83"


def test_ac04_quantity_precision_does_not_reuse_money_t():
    missing = reconcile_quantity()
    assert missing["pass"] is False
    assert missing["status"] == "blocked_missing_quantity_precision"
    assert missing["uses_money_tolerance"] is False
    reused = reconcile_quantity(tolerance="0.01", ledger_qty="1", venue_qty="1")
    assert reused["pass"] is False
    assert reused["status"] == "blocked_unapproved_quantity_precision"


def test_ac04_flag_missing_or_live_returns_no_actual_and_no_zero():
    payload = _load("short_receipt.json")
    off = account({}, payload)
    assert off["status"] == "feature_off"
    assert off["actual_net"] is None
    assert off["venue_calls"] == 0
    assert account(None, payload)["actual_net"] is None
    assert account({"accounting": {"net_cashflow_v220": {"enabled": False}}}, payload)["status"] == "feature_off"
    assert account({"accounting": {"net_cashflow_v220": {"enabled": "true"}}}, payload)["actual_net"] is None
    climax = {"shorts": {"climax_fade": {"enabled": True}, "allow_live": False}}
    assert account(climax, payload)["status"] == "feature_off"

    live = _on(trading_mode="live")
    assert account(live, payload)["status"] == "live_denied"
    allowed = _on()
    allowed["shorts"]["allow_live"] = True
    assert account(allowed, payload)["status"] == "live_denied"
    real_exec = _on()
    real_exec["live"] = {"execution": "real"}
    assert account(real_exec, payload)["status"] == "live_denied"
    assert account(_on(trading_mode="spot"), payload)["status"] == "mode_not_sandbox"
    assert account(_on(trading_mode="futures_testnet"), payload)["actual_net"] == "9.83"


def test_ac04_module_imports_no_venue_client_and_replay_cover_stays_price_only():
    root = Path(__file__).resolve().parents[2]
    source = (root / "core/net_cashflow.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert set(imported) == {"__future__", "math", "datetime", "decimal"}
    replay = (root / "core/sim_ledger_replay.py").read_text(encoding="utf-8")
    assert "pnl = cover_amt * (entry - price)" in replay
    assert ".account(" not in replay
