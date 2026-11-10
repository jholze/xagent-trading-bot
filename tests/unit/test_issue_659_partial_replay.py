"""#659: replay keeps the executed quantity of partial and remainder-canceled rows.

Spot buy/sell only. Short and cover rows stay on status ``filled`` (#659 out of
scope). Numbers are fixture values. The daily-loss reader is not this test:
``is_executed_status`` still means filled, and that reader is the follow-up hook.
"""

from __future__ import annotations

import pytest

from core.models import is_executed_status
from core.net_cashflow import row_counts_for_replay
from core.sim_ledger_replay import replay_simulated_ledger
from strategies.positions import get_key

TF = "4h"
INITIAL = 5000.0


def _order(
    oid: str,
    side: str,
    symbol: str,
    price: float,
    amount: float | None,
    *,
    status: str = "filled",
    usdt: float | None = None,
    ts: str,
    request: dict | None = None,
    pnl: float | None = None,
    filled_qty_gross: float | None = None,
    leverage: float | None = None,
    omit_execution_amount: bool = False,
) -> dict:
    execution: dict = {"price": price}
    if not omit_execution_amount and amount is not None:
        execution["amount"] = amount
    if usdt is not None:
        execution["usdt"] = usdt
    if filled_qty_gross is not None:
        execution["filled_qty_gross"] = filled_qty_gross
    row = {
        "id": oid,
        "status": status,
        "side": side,
        "symbol": symbol,
        "timeframe": TF,
        "execution": execution,
        "request": request or {},
        "timestamps": {"created": ts, "filled": ts},
    }
    if pnl is not None:
        row["pnl"] = pnl
    if leverage is not None:
        row["leverage"] = leverage
    return row


def _lot(replay: dict, symbol: str) -> dict:
    return replay["positions"][get_key(symbol, TF)]


def test_t1_remainder_canceled_buy_books_execution_not_request():
    symbol = "T1/USDT"
    order = _order(
        "t1",
        "buy",
        symbol,
        1.0,
        40.0,
        status="canceled",
        usdt=40.08,
        ts="2026-10-07T12:00:00",
        request={"amount": 100.0, "usdt": 100.0, "price": 1.0},
    )
    replay = replay_simulated_ledger([order], INITIAL)
    lot = _lot(replay, symbol)
    assert lot["amount"] == pytest.approx(40.0)
    assert lot["average_entry"] == pytest.approx(40.08 / 40.0)
    assert replay["cash"] == pytest.approx(INITIAL - 40.08)


def test_t2_same_order_counts_once_at_current_cumulative_fill():
    symbol = "T2/USDT"
    partial = _order(
        "t2",
        "buy",
        symbol,
        1.0,
        30.0,
        status="partially_filled",
        usdt=30.0,
        ts="2026-10-07T12:00:00",
        request={"amount": 100.0, "usdt": 100.0},
    )
    filled = _order(
        "t2",
        "buy",
        symbol,
        1.0,
        100.0,
        status="filled",
        usdt=100.0,
        ts="2026-10-07T12:05:00",
        request={"amount": 100.0, "usdt": 100.0},
    )
    replay = replay_simulated_ledger([partial, filled, dict(filled)], INITIAL)
    lot = _lot(replay, symbol)
    assert lot["amount"] == pytest.approx(100.0)
    assert lot["average_entry"] == pytest.approx(1.0)
    assert replay["cash"] == pytest.approx(INITIAL - 100.0)


def test_restart_duplicate_of_canceled_buy_books_once():
    symbol = "RST/USDT"
    row = _order(
        "rst",
        "buy",
        symbol,
        1.0,
        40.0,
        status="cancelled",
        usdt=40.08,
        ts="2026-10-07T12:00:00",
        request={"amount": 90.0, "usdt": 90.0},
    )
    replay = replay_simulated_ledger([row, dict(row)], INITIAL)
    assert _lot(replay, symbol)["amount"] == pytest.approx(40.0)
    assert replay["cash"] == pytest.approx(INITIAL - 40.08)


def test_t3_remainder_canceled_sell_keeps_basis_and_filled_pnl():
    symbol = "T3/USDT"
    buy = _order(
        "t3-buy",
        "buy",
        symbol,
        2.0,
        100.0,
        usdt=200.0,
        ts="2026-10-07T12:00:00",
    )
    sell = _order(
        "t3-sell",
        "sell",
        symbol,
        3.0,
        20.0,
        status="canceled",
        usdt=60.0,
        ts="2026-10-07T13:00:00",
        request={"amount": 50.0, "usdt": 150.0, "price": 3.0},
        pnl=20.0,
    )
    replay = replay_simulated_ledger([buy, sell], INITIAL)
    lot = _lot(replay, symbol)
    assert lot["amount"] == pytest.approx(80.0)
    assert lot["average_entry"] == pytest.approx(2.0)
    assert replay["realized_pnl"] == pytest.approx(20.0)
    assert row_counts_for_replay(sell) is True
    assert is_executed_status(sell["status"]) is False


def test_t4_zero_or_missing_fill_is_ignored_and_not_logged(monkeypatch):
    symbol = "T4/USDT"
    messages: list[tuple] = []

    def _log(message, level="INFO"):
        messages.append((level, str(message)))

    monkeypatch.setattr("core.sim_ledger_replay.log", _log)
    rows = [
        _order(
            "z1", "buy", symbol, 1.0, 0.0, status="canceled", usdt=10.0,
            ts="2026-10-07T12:00:00", request={"amount": 8.0, "usdt": 8.0},
        ),
        _order(
            "z2", "buy", symbol, 1.0, None, status="partially_filled",
            ts="2026-10-07T12:01:00", omit_execution_amount=True,
            request={"amount": 8.0, "usdt": 8.0},
        ),
        _order(
            "z3", "sell", symbol, 1.0, None, status="canceled",
            ts="2026-10-07T12:02:00", omit_execution_amount=True,
            request={"amount": 8.0},
        ),
    ]
    replay = replay_simulated_ledger(rows, INITIAL)
    assert replay["positions"] == {}
    assert replay["cash"] == pytest.approx(INITIAL)
    assert not any("replay counted partial fill" in text for _, text in messages)


def test_t5_cost_stamped_partial_buy_uses_stamped_usdt():
    """Same shape as the cost-stamped partial named on #659. No coin name in code."""
    symbol = "T5/USDT"
    order = _order(
        "t5",
        "buy",
        symbol,
        1.5,
        10.0,
        status="partially_filled",
        usdt=15.2,
        ts="2026-09-07T20:36:20",
        filled_qty_gross=10.0,
        request={"amount": 80.0, "usdt": 120.0, "price": 1.5},
    )
    replay = replay_simulated_ledger([order], INITIAL)
    lot = _lot(replay, symbol)
    assert lot["amount"] == pytest.approx(10.0)
    assert lot["average_entry"] == pytest.approx(15.2 / 10.0)


def test_filled_buy_still_falls_back_to_request_amount():
    symbol = "RQ/USDT"
    order = _order(
        "rq",
        "buy",
        symbol,
        2.0,
        None,
        usdt=20.0,
        ts="2026-10-07T12:00:00",
        omit_execution_amount=True,
        request={"amount": 10.0, "price": 2.0, "usdt": 20.0},
    )
    replay = replay_simulated_ledger([order], INITIAL)
    assert _lot(replay, symbol)["amount"] == pytest.approx(10.0)


def test_t6_short_and_cover_partials_do_not_change_the_short_branch():
    symbol = "SH/USDT"
    filled = _order(
        "sh-filled",
        "short",
        symbol,
        100.0,
        1.0,
        usdt=100.0,
        ts="2026-10-07T12:00:00",
        leverage=1.0,
    )
    partial = _order(
        "sh-partial",
        "short",
        symbol,
        100.0,
        5.0,
        status="partially_filled",
        usdt=500.0,
        ts="2026-10-07T12:05:00",
        leverage=1.0,
    )
    cover = _order(
        "sh-cover",
        "cover",
        symbol,
        90.0,
        1.0,
        status="canceled",
        usdt=90.0,
        ts="2026-10-07T12:10:00",
    )
    only_partial = replay_simulated_ledger([partial, cover], INITIAL)
    assert only_partial["positions"] == {}
    kept = replay_simulated_ledger([filled, partial, cover], INITIAL)
    lot = _lot(kept, symbol)
    assert lot["amount"] == pytest.approx(1.0)
    assert lot["side"] == "short"


def test_partial_rows_log_once_per_taken_order(monkeypatch):
    symbol = "LG/USDT"
    messages: list[str] = []

    def _log(message, level="INFO"):
        if level == "INFO":
            messages.append(str(message))

    monkeypatch.setattr("core.sim_ledger_replay.log", _log)
    first = _order(
        "lg1", "buy", symbol, 1.0, 4.0, status="partially_filled", usdt=4.0,
        ts="2026-10-07T12:00:00",
    )
    second = _order(
        "lg2", "buy", symbol, 1.0, 1.0, status="canceled", usdt=1.0,
        ts="2026-10-07T12:01:00",
    )
    replay_simulated_ledger([first, dict(first), second], INITIAL, tenant_id="tenant-a")
    counted = [text for text in messages if "replay counted partial fill" in text]
    assert len(counted) == 2
    assert any("symbol=LG/USDT" in text and "filled_qty=4.0" in text and "tenant=tenant-a" in text for text in counted)
    assert any("status=canceled" in text and "filled_qty=1.0" in text for text in counted)
