"""#350: daily_auswertung must count fills that exist only in the order ledger.

``generate_report()`` / ``build_telegram_daily_summary()`` used to read
``trade_history.trades`` only; the morning briefing already merged filled
orders via ``trades_in_window()``. Both now share
``merge_trades_with_filled_orders`` — and a fill present in both ledgers
must collapse to one row.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.config import BotConfig
from core.models import OrderStatus, TradeOrder
from core.tenant_context import tenant_context
from data_manager import (
    load_orders,
    load_trade_history_document,
    resolve_ledger_scope,
    save_orders,
    save_trade_history,
    save_trade_history_document,
)
from notifications.daily_stats import (
    filled_order_to_trade,
    merge_trades_with_filled_orders,
    trade_dedup_key,
    trade_order_id,
    trades_in_window,
)
from scripts import daily_auswertung
from scripts.daily_auswertung import (
    LEDGER_UNAVAILABLE_BANNER,
    _day_trades,
    _ledger_bundle,
    build_telegram_daily_summary,
    generate_report,
)
from services.order_service import OrderService
from services.portfolio_service import PortfolioService
from storage.errors import LedgerUnavailable

REPORT_DATE = datetime(2026, 6, 14, 12, 0, 0)
DAY_START = REPORT_DATE.replace(hour=0, minute=0, second=0, microsecond=0)
DAY_END = datetime(2026, 6, 15, 0, 0, 0)
SYMBOL = "SOL/USDT"


@pytest.fixture
def bot_dir(tmp_path: Path) -> Path:
    """Minimal bot root: config.json is required, everything else is optional."""
    (tmp_path / "config.json").write_text(
        json.dumps({"live": {"dry_run": True}, "hermes": {"enabled": False}}),
        encoding="utf-8",
    )
    return tmp_path


def _filled_order(
    order_id: str,
    side: str,
    usdt: float,
    ts: str,
    *,
    source: str = "auto",
    pnl: float | None = None,
    request_usdt: float | None = None,
) -> dict:
    return {
        "id": order_id,
        "ledger_scope": resolve_ledger_scope(),
        "status": "filled",
        "side": side,
        "symbol": SYMBOL,
        "source": source,
        "request": {"usdt": request_usdt if request_usdt is not None else usdt},
        "execution": {"usdt": usdt, "price": 2.0, "amount": usdt / 2.0},
        "pnl": pnl,
        "timestamps": {"created": ts, "updated": ts, "filled": ts},
    }


# ---------------------------------------------------------------------------
# 1) history empty, fills only in orders → report populated from the merge
# ---------------------------------------------------------------------------


def test_generate_report_counts_filled_orders_when_history_empty(bot_dir):
    scope = resolve_ledger_scope()
    save_trade_history_document(
        {"trades": [], "virtual_balance": 4800.0, "realized_pnl": 7.5}, scope
    )
    save_orders(
        {
            "ledger_scope": scope,
            "orders": [
                _filled_order("o-buy", "buy", 150.0, "2026-06-14T09:05:00", source="dca"),
                _filled_order(
                    "o-sell", "sell", 210.0, "2026-06-14T15:40:00", source="grid", pnl=7.5
                ),
                # Out of window: must not leak into the day
                _filled_order("o-old", "buy", 99.0, "2026-06-13T23:59:59", source="dca"),
                # Not filled: must not count as a trade
                {
                    "id": "o-rej",
                    "ledger_scope": scope,
                    "status": "rejected",
                    "side": "buy",
                    "symbol": SYMBOL,
                    "request": {"usdt": 50.0},
                    "timestamps": {"created": "2026-06-14T11:00:00"},
                },
            ],
        },
        scope,
    )

    th, orders_raw, _ = _ledger_bundle()
    assert th["trades"] == []
    merged = _day_trades(th, orders_raw, DAY_START, DAY_END)
    assert [(t["type"], t["usdt_amount"]) for t in merged] == [("BUY", 150.0), ("SELL", 210.0)]
    assert all(t.get("_from_order") for t in merged)
    # Same fills the morning briefing would show for this window.
    assert [trade_dedup_key(t) for t in merged] == [
        trade_dedup_key(t) for t in trades_in_window(bot_dir, DAY_START, DAY_END)
    ]

    report = generate_report(bot_dir, REPORT_DATE)
    assert "Keine Trades heute" not in report
    assert "2 Trades (1 BUY, 1 SELL), realized PnL Verkäufe: **+7.50 USDT**" in report
    assert "| Trades heute | 2 (1 BUY / 1 SELL) |" in report
    assert "| Realized PnL heute (Verkäufe) | +7.50 USDT |" in report
    # Trade-lines table rows come from the order ledger
    assert "| 14.06. 09:05 | BUY | SOL/USDT | $150.00 | dca | +0.00 |" in report
    assert "| 14.06. 15:40 | SELL | SOL/USDT | $210.00 | grid | +7.50 |" in report
    assert "| grid | +7.50 |" in report  # PnL nach Quelle
    assert "| Orders heute | 3 (2 filled / 1 rejected) |" in report

    summary = build_telegram_daily_summary(bot_dir, REPORT_DATE)
    assert "keine Trades" not in summary
    assert "<b>Trades heute</b> 2 (1 BUY / 1 SELL, davon 1 DCA)" in summary
    assert "heute +7.5 USDT" in summary
    assert "• 09:05 BUY SOL/USDT $150 (dca)" in summary
    assert "• 15:40 SELL SOL/USDT $210 (grid) PnL +7.5" in summary


# ---------------------------------------------------------------------------
# 2) same fill in trade_history AND orders → exactly one merged row
# ---------------------------------------------------------------------------

FIXED_NOW = datetime(2026, 6, 14, 10, 15, 0, 123456)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: D401 - drop-in for datetime.now
        return FIXED_NOW if tz is None else FIXED_NOW.astimezone(tz)


def _cost_cfg() -> BotConfig:
    # fee on the quote side so quote_net (100.2) != request.usdt (100.0):
    # the dedup must line up execution.usdt with history usdt_amount, not the request.
    return BotConfig(
        {
            "trading_mode": "paper",
            "max_usdt_per_trade": 1000,
            "costs": {
                "fee_source": "config",
                "gate": {
                    "spot": {
                        "fee_maker_pct": 0.2,
                        "fee_taker_pct": 0.2,
                        "slippage_pct": 0.0,
                        "fee_side_buy": "quote",
                        "fee_side_sell": "quote",
                    }
                },
            },
            "live": {"execution": "shadow", "dry_run": False, "simulated_balance_usdt": 5000},
        }
    )


def test_same_fill_in_history_and_orders_is_not_double_counted(bot_dir, monkeypatch):
    """Both ledgers written by the real code paths, not by equal literals.

    trade_history row: PortfolioService.execute_buy/execute_sell → record_trade
    (``usdt_amount``/``usdt_received`` = ``Fill.quote_net``).
    order row: OrderService.create_from_request + link_execution_result
    (``execution.usdt`` = ``TradeResult.usdt_amount``).
    The clock is frozen only so both writers stamp the same second — the
    dedup key truncates timestamps to seconds.
    """
    monkeypatch.setattr("services.portfolio_service.datetime", _FrozenDatetime)
    monkeypatch.setattr("services.order_service._now", lambda: FIXED_NOW.isoformat())
    cfg = _cost_cfg()

    scope = resolve_ledger_scope()  # record_trade writes into this scope
    with tenant_context("default", scope=scope):
        save_trade_history(
            {"virtual_balance": 5000.0, "realized_pnl": 0.0, "open_positions": 0, "trades": []}
        )
        svc = OrderService(scope)
        ps = PortfolioService(cfg)

        buy_order = TradeOrder("BUY", SYMBOL, 2.0, 0, usdt_amount=100.0, source="dca")
        buy_rec = svc.create_from_request(
            buy_order, status=OrderStatus.QUEUED, telegram_token="t350-buy", timeframe="4h"
        )
        buy_res = ps.execute_buy(SYMBOL, "4h", 2.0, usdt_amount=100.0, source="dca", order_id=buy_rec["id"])
        assert buy_res.executed, buy_res.message
        svc.link_execution_result(buy_rec["id"], buy_res, buy_order)

        sell_order = TradeOrder("SELL", SYMBOL, 2.5, buy_res.amount, signal="SELL", source="grid")
        sell_rec = svc.create_from_request(
            sell_order, status=OrderStatus.QUEUED, telegram_token="t350-sell", timeframe="4h"
        )
        sell_res = ps.execute_sell(
            SYMBOL, "4h", 2.5, "SELL", amount=buy_res.amount, source="grid", order_id=sell_rec["id"]
        )
        assert sell_res.executed, sell_res.message
        svc.link_execution_result(sell_rec["id"], sell_res, sell_order)

        th = load_trade_history_document(scope)
        orders_raw = load_orders(scope)

    # Both ledgers hold the same two fills, in their own representation.
    hist = {t["type"]: t for t in th["trades"]}
    assert set(hist) == {"BUY", "SELL"}
    filled = {o["side"].upper(): o for o in orders_raw["orders"] if o["status"] == "filled"}
    assert set(filled) == {"BUY", "SELL"}

    assert hist["BUY"]["usdt_amount"] == pytest.approx(100.2)  # quote_net incl. 0.2% fee
    assert "usdt_received" not in hist["BUY"]
    assert filled["BUY"]["request"]["usdt"] == pytest.approx(100.0)
    assert filled["BUY"]["execution"]["usdt"] == pytest.approx(hist["BUY"]["usdt_amount"])

    assert "usdt_amount" not in hist["SELL"]
    assert hist["SELL"]["usdt_received"] == pytest.approx(sell_res.usdt_amount)
    assert filled["SELL"]["execution"]["usdt"] == pytest.approx(hist["SELL"]["usdt_received"])
    assert filled["SELL"]["pnl"] == pytest.approx(hist["SELL"]["pnl"])

    # Timestamps: same second, different precision/keys — still one key.
    assert hist["BUY"]["timestamp"][:19] == filled["BUY"]["timestamps"]["filled"][:19]

    for side in ("BUY", "SELL"):
        assert trade_dedup_key(hist[side]) == trade_dedup_key(filled_order_to_trade(filled[side]))

    merged = _day_trades(th, orders_raw, DAY_START, DAY_END)
    assert len(merged) == 2, [trade_dedup_key(t) for t in merged]
    assert sorted(t["type"] for t in merged) == ["BUY", "SELL"]
    # History rows win; nothing was pulled in from the order ledger.
    assert not any(t.get("_from_order") for t in merged)

    report = generate_report(bot_dir, FIXED_NOW)
    assert "2 Trades (1 BUY, 1 SELL)" in report
    assert "| Trades heute | 2 (1 BUY / 1 SELL) |" in report
    assert f"| Realized PnL heute (Verkäufe) | {hist['SELL']['pnl']:+.2f} USDT |" in report
    assert report.count("| SOL/USDT |") == 2
    assert "| Orders heute | 2 (2 filled / 0 rejected) |" in report

    summary = build_telegram_daily_summary(bot_dir, FIXED_NOW)
    assert "<b>Trades heute</b> 2 (1 BUY / 1 SELL, davon 1 DCA)" in summary
    assert summary.count("SOL/USDT") == 2


HISTORY_NOW = datetime(2026, 6, 14, 10, 15, 0, 900000)
ORDER_FILLED_NOW = HISTORY_NOW + timedelta(seconds=1, microseconds=300000)  # crosses :00 → :02


class _HistoryClock(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: D401 - drop-in for datetime.now
        return HISTORY_NOW if tz is None else HISTORY_NOW.astimezone(tz)


def test_same_fill_is_not_double_counted_when_order_filled_stamp_lags_history(bot_dir, monkeypatch):
    """Reviewer BLOCK on #350: the two ledgers stamp with independent clocks.

    ``record_trade`` stamps during ``adapter.execute``; ``link_execution_result``
    stamps ``timestamps.filled`` afterwards, after a Mongo load/save. When that gap
    crosses a second boundary the ``timestamp[:19]`` part of the fuzzy key differs
    and the merge used to append the order row a second time, double-counting the
    SELL in ``sell_pnl_day``. The clocks are *not* frozen to the same instant here:
    history stamps at 10:15:00.9, the order fill at 10:15:02.2 (>= 1 s later).
    Both sides carry the order id, so the merge must match on that.
    """
    monkeypatch.setattr("services.portfolio_service.datetime", _HistoryClock)
    monkeypatch.setattr("services.order_service._now", lambda: ORDER_FILLED_NOW.isoformat())
    cfg = _cost_cfg()

    scope = resolve_ledger_scope()
    with tenant_context("default", scope=scope):
        save_trade_history(
            {"virtual_balance": 5000.0, "realized_pnl": 0.0, "open_positions": 0, "trades": []}
        )
        svc = OrderService(scope)
        ps = PortfolioService(cfg)

        buy_order = TradeOrder("BUY", SYMBOL, 2.0, 0, usdt_amount=100.0, source="dca")
        buy_rec = svc.create_from_request(
            buy_order, status=OrderStatus.QUEUED, telegram_token="t350-lag-buy", timeframe="4h"
        )
        buy_res = ps.execute_buy(SYMBOL, "4h", 2.0, usdt_amount=100.0, source="dca", order_id=buy_rec["id"])
        assert buy_res.executed, buy_res.message
        svc.link_execution_result(buy_rec["id"], buy_res, buy_order)

        sell_order = TradeOrder("SELL", SYMBOL, 2.5, buy_res.amount, signal="SELL", source="grid")
        sell_rec = svc.create_from_request(
            sell_order, status=OrderStatus.QUEUED, telegram_token="t350-lag-sell", timeframe="4h"
        )
        sell_res = ps.execute_sell(
            SYMBOL, "4h", 2.5, "SELL", amount=buy_res.amount, source="grid", order_id=sell_rec["id"]
        )
        assert sell_res.executed, sell_res.message
        svc.link_execution_result(sell_rec["id"], sell_res, sell_order)

        th = load_trade_history_document(scope)
        orders_raw = load_orders(scope)

    hist = {t["type"]: t for t in th["trades"]}
    filled = {o["side"].upper(): o for o in orders_raw["orders"] if o["status"] == "filled"}
    assert set(hist) == set(filled) == {"BUY", "SELL"}

    # quote_net proof still holds: execution.usdt == history amount != request.usdt
    assert hist["BUY"]["usdt_amount"] == pytest.approx(100.2)
    assert filled["BUY"]["request"]["usdt"] == pytest.approx(100.0)
    assert filled["BUY"]["execution"]["usdt"] == pytest.approx(hist["BUY"]["usdt_amount"])
    assert filled["SELL"]["execution"]["usdt"] == pytest.approx(hist["SELL"]["usdt_received"])

    # The stamps really are in different seconds → the fuzzy key does NOT match ...
    for side in ("BUY", "SELL"):
        h_ts = datetime.fromisoformat(hist[side]["timestamp"])
        o_ts = datetime.fromisoformat(filled[side]["timestamps"]["filled"])
        assert o_ts - h_ts >= timedelta(seconds=1)
        assert hist[side]["timestamp"][:19] != filled[side]["timestamps"]["filled"][:19]
        assert trade_dedup_key(hist[side]) != trade_dedup_key(filled_order_to_trade(filled[side]))
        # ... but both sides carry the same order id.
        assert hist[side]["order_id"] == filled[side]["id"]
        assert trade_order_id(hist[side]) == trade_order_id(filled_order_to_trade(filled[side]))

    merged = _day_trades(th, orders_raw, DAY_START, DAY_END)
    assert len(merged) == 2, [(t["type"], t["timestamp"], t.get("_from_order")) for t in merged]
    assert sorted(t["type"] for t in merged) == ["BUY", "SELL"]
    assert not any(t.get("_from_order") for t in merged)

    report = generate_report(bot_dir, HISTORY_NOW)
    assert "2 Trades (1 BUY, 1 SELL)" in report
    assert "| Trades heute | 2 (1 BUY / 1 SELL) |" in report
    # SELL PnL counted exactly once
    assert f"| Realized PnL heute (Verkäufe) | {hist['SELL']['pnl']:+.2f} USDT |" in report
    assert report.count("| SOL/USDT |") == 2
    assert "| Orders heute | 2 (2 filled / 0 rejected) |" in report

    summary = build_telegram_daily_summary(bot_dir, HISTORY_NOW)
    assert "<b>Trades heute</b> 2 (1 BUY / 1 SELL, davon 1 DCA)" in summary
    assert summary.count("SOL/USDT") == 2


def test_merge_id_match_and_fuzzy_fallback_semantics():
    """Pure merge: ids decide when present, fuzzy key only when an id is missing."""
    ts_a, ts_b = "2026-06-14T10:15:00.900000", "2026-06-14T10:15:02.200000"
    history = [
        # has id, stamp differs by 2 s from its order → id match, no dup
        {"type": "SELL", "symbol": SYMBOL, "usdt_received": 50.0, "order_id": "o-1", "timestamp": ts_a},
        # legacy row without id → fuzzy fallback still dedups its order
        {"type": "BUY", "symbol": SYMBOL, "usdt_amount": 20.0, "timestamp": "2026-06-14T11:00:00"},
        # has id; a *different* order with the same fuzzy key is a separate fill
        {"type": "BUY", "symbol": SYMBOL, "usdt_amount": 30.0, "order_id": "o-3", "timestamp": "2026-06-14T12:00:00"},
    ]
    orders = [
        _filled_order("o-1", "sell", 50.0, ts_b, pnl=1.0),
        _filled_order("o-2", "buy", 20.0, "2026-06-14T11:00:00.400000"),
        _filled_order("o-3", "buy", 30.0, "2026-06-14T12:00:00"),
        _filled_order("o-4", "buy", 30.0, "2026-06-14T12:00:00"),
    ]
    merged = merge_trades_with_filled_orders(history, orders, DAY_START, DAY_END)
    assert [(t["type"], trade_order_id(t), bool(t.get("_from_order"))) for t in merged] == [
        ("SELL", "o-1", False),
        ("BUY", None, False),
        ("BUY", "o-3", False),
        ("BUY", "o-4", True),
    ]


def test_merge_pulls_only_missing_fills_when_history_is_partial(bot_dir):
    """History has the BUY, orders have BUY + SELL → BUY once, SELL from orders."""
    scope = resolve_ledger_scope()
    ts_buy = "2026-06-14T09:05:00"
    save_trade_history_document(
        {
            "trades": [
                {
                    "type": "BUY",
                    "symbol": SYMBOL,
                    "usdt_amount": 150.004,  # history float noise vs 150.0 in execution.usdt
                    "source": "dca",
                    "timestamp": ts_buy + ".482913",
                }
            ],
            "virtual_balance": 4850.0,
            "realized_pnl": 0.0,
        },
        scope,
    )
    save_orders(
        {
            "ledger_scope": scope,
            "orders": [
                _filled_order("o-buy", "buy", 150.0, ts_buy, source="dca"),
                _filled_order("o-sell", "sell", 160.0, "2026-06-14T16:00:00", source="grid", pnl=3.25),
            ],
        },
        scope,
    )
    th, orders_raw, _ = _ledger_bundle()
    merged = _day_trades(th, orders_raw, DAY_START, DAY_END)
    assert [(t["type"], bool(t.get("_from_order"))) for t in merged] == [
        ("BUY", False),
        ("SELL", True),
    ]
    report = generate_report(bot_dir, REPORT_DATE)
    assert "2 Trades (1 BUY, 1 SELL), realized PnL Verkäufe: **+3.25 USDT**" in report


# ---------------------------------------------------------------------------
# 3) ledger read failure must not become an empty/zero report
# ---------------------------------------------------------------------------


def test_ledger_bundle_raises_instead_of_empty_report(bot_dir, monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("mongo down")

    monkeypatch.setattr("data_manager.load_trade_history_document", _boom)

    with pytest.raises(LedgerUnavailable) as info:
        _ledger_bundle()
    assert "mongo down" in str(info.value)
    assert isinstance(info.value.cause, RuntimeError)

    with pytest.raises(LedgerUnavailable):
        generate_report(bot_dir, REPORT_DATE)
    with pytest.raises(LedgerUnavailable):
        build_telegram_daily_summary(bot_dir, REPORT_DATE)


def test_ledger_bundle_reraises_ledger_unavailable_unchanged(monkeypatch):
    original = LedgerUnavailable("refused", op="load_orders", scope="paper")

    def _boom(*_a, **_k):
        raise original

    monkeypatch.setattr("data_manager.load_orders", _boom)
    with pytest.raises(LedgerUnavailable) as info:
        _ledger_bundle()
    assert info.value is original


def test_main_prints_banner_and_exits_nonzero_when_ledger_unavailable(bot_dir, monkeypatch, capsys):
    def _boom(*_a, **_k):
        raise RuntimeError("mongo down")

    monkeypatch.setattr("data_manager.load_orders", _boom)
    monkeypatch.setattr(
        sys, "argv",
        ["daily_auswertung.py", "--bot-dir", str(bot_dir), "--date", "2026-06-14", "--no-telegram"],
    )
    with pytest.raises(SystemExit) as exc:
        daily_auswertung.main()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert LEDGER_UNAVAILABLE_BANNER in captured.err
    assert "mongo down" in captured.err
    assert "Tages-Auswertung Trading Bot" not in captured.out
    assert not (bot_dir / "auswertungen").exists()
