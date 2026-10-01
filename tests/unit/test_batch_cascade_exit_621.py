"""#621 batch cascade exit. Paper fixtures only — no Gate HTTP.

B3 selection before any batch payload. B4 evidence rows. B5 partial ACK
remainder and transport abort. B6 no flatten without a per-lot ACK. B7
fire_enabled false does not flatten. B9 named cases. B8 prints a wall-clock
delta and does not assert it.
"""

from __future__ import annotations

import json
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import ccxt
import pytest

from core.actions import COVER_FULL, SELL_FULL
from core.config import BotConfig
from execution.gate_adapter import (
    GATE_SPOT_BATCH_MAX_ORDERS_PER_PAIR,
    GATE_SPOT_BATCH_MAX_PAIRS,
    BatchMarketSellNoAck,
    GateExecutionAdapter,
)
from services.exit_realtime.cascade_state import CascadeState
from services.exit_realtime.config import CASCADE_DEFAULTS, cascade_config
from services.exit_realtime.execute import execute_cascade_exit, execute_cascade_exit_batch
from strategies.position_lock import build_lock
from strategies.sell_sources import LIQ_CASCADE_SOURCE


def _clear(symbols: list[str]) -> None:
    import services.exit_realtime.execute as ex

    with ex._inflight_lock:
        for sym in symbols:
            ex._inflight.discard(sym)
            ex._last_exit_at.pop(sym, None)


def _lot(symbol: str, **over) -> dict:
    lot = {
        "symbol": symbol,
        "timeframe": "1h",
        "amount": 10.0,
        "average_entry": 1.0,
        "side": "long",
        "current_price": 1.10,
    }
    lot.update(over)
    return lot


def _locked(lot: dict) -> dict:
    out = dict(lot)
    out["lock"] = build_lock(reason="hold")
    return out


@contextmanager
def _book(lots: list[dict], attach=None):
    by = {(x["symbol"], str(x.get("timeframe") or "1h")): dict(x) for x in lots}

    def _get(symbol, tf):
        return dict(by[(symbol, str(tf))])

    if attach is None:
        def attach(pos, *args, **kwargs):
            return pos

    with ExitStack() as stack:
        stack.enter_context(patch("strategies.positions.get_position", side_effect=_get))
        stack.enter_context(
            patch("strategies.positions.is_open_position", return_value=True)
        )
        stack.enter_context(
            patch(
                "strategies.position_lock.attach_lock_from_ledger",
                side_effect=attach,
            )
        )
        stack.enter_context(patch("core.costs.CostModel.round_trip_pct", return_value=0.5))
        yield


def _trading(seller):
    return SimpleNamespace(
        execute_order=MagicMock(
            return_value=SimpleNamespace(executed=True, message="ok filled")
        ),
        portfolio=SimpleNamespace(
            execute_sell=MagicMock(
                return_value=SimpleNamespace(executed=True, message="paper sell")
            )
        ),
        adapter=SimpleNamespace(batch_market_sell=seller),
    )


def _full_fill(row, order_id):
    """Succeeded ACK that is a full fill of the requested amount at the caller price.

    ``succeeded`` true alone is not a fill. Tests that expect a flatten have
    to report filled_amount and a fill price, the same fields a Gate row carries.
    """
    price = row.get("price")
    if price in (None, ""):
        price = "1.10"
    return {
        "succeeded": True,
        "label": "",
        "message": "",
        "text": row["text"],
        "id": order_id,
        "status": "closed",
        "finish_as": "filled",
        "filled_amount": row["amount"],
        "left": "0",
        "avg_deal_price": price,
        "fill_price": price,
    }


def _acks_all_ok(payload):
    return [_full_fill(row, f"ack-{i}") for i, row in enumerate(payload)]


def _run_batch(lots, seller, **kwargs):
    symbols = [lot["symbol"] for lot in lots]
    _clear(symbols)
    trading = kwargs.pop("trading", None) or _trading(seller)
    attach = kwargs.pop("attach", None)
    try:
        with _book(lots, attach=attach):
            out = execute_cascade_exit_batch(
                side=kwargs.pop("side", "long"),
                lots=lots,
                prices={lot["symbol"]: float(lot["current_price"]) for lot in lots},
                trading=trading,
                fire_enabled=kwargs.pop("fire_enabled", True),
                **kwargs,
            )
        return out, trading
    finally:
        _clear(symbols)


def _symbols(payload) -> list[str]:
    return [row["symbol"] for row in payload]


def _real_adapter() -> tuple[GateExecutionAdapter, MagicMock]:
    adapter = GateExecutionAdapter(BotConfig({}), MagicMock(), mode="real")
    exchange = MagicMock()
    adapter._exchange = exchange
    return adapter, exchange


def test_batch_enabled_defaults_false_and_detector_numbers_unchanged():
    assert CASCADE_DEFAULTS["fire_enabled"] is False
    assert CASCADE_DEFAULTS["batch_enabled"] is False
    assert CASCADE_DEFAULTS["multiplier"] == 3.0
    assert CASCADE_DEFAULTS["min_notional_usd"] == 100000
    assert CASCADE_DEFAULTS["window_sec"] == 300
    cfg = cascade_config({})
    assert cfg["batch_enabled"] is False
    assert cfg["fire_enabled"] is False
    raw = json.loads(
        (Path(__file__).resolve().parents[2] / "config.json").read_text(encoding="utf-8")
    )
    cascade = raw["exit_realtime"]["cascade"]
    assert cascade["fire_enabled"] is False
    assert cascade.get("batch_enabled") is not True
    assert cascade["multiplier"] == 3.0
    assert cascade["min_notional_usd"] == 100000


def test_b3_adapter_submits_one_spot_chunk_and_normalizes_acks():
    adapter, exchange = _real_adapter()
    exchange.privateSpotPostBatchOrders.return_value = [
        {
            "succeeded": False,
            "label": "BALANCE_NOT_ENOUGH",
            "message": "Not enough balance",
            "text": "t-aaa",
        },
        {
            "succeeded": True,
            "label": "",
            "message": "",
            "text": "t-bbb",
            "id": "99",
            "status": "closed",
        },
    ]
    rows = adapter.batch_market_sell(
        [
            {"symbol": "AAA/USDT", "amount": 10.5, "text": "t-aaa"},
            {"symbol": "BBB/USDT", "amount": 2, "text": "t-bbb"},
        ]
    )
    body = exchange.privateSpotPostBatchOrders.call_args[0][0]
    assert exchange.privateSpotPostBatchOrders.call_count == 1
    assert [row["currency_pair"] for row in body] == ["AAA_USDT", "BBB_USDT"]
    assert {row["account"] for row in body} == {"spot"}
    assert {row["side"] for row in body} == {"sell"}
    assert {row["type"] for row in body} == {"market"}
    assert {row["time_in_force"] for row in body} == {"ioc"}
    assert body[0]["text"] == "t-aaa"
    assert body[0]["amount"] == "10.5"
    assert "margin" not in json.dumps(body)
    assert rows[0]["succeeded"] is False
    assert rows[0]["label"] == "BALANCE_NOT_ENOUGH"
    assert rows[0]["message"] == "Not enough balance"
    assert rows[1]["succeeded"] is True
    assert rows[1]["id"] == "99"


def test_b3_adapter_rejects_oversize_chunk_before_http():
    adapter, exchange = _real_adapter()
    five = [
        {"symbol": f"C{i}/USDT", "amount": 1, "text": f"t-c{i}"}
        for i in range(GATE_SPOT_BATCH_MAX_PAIRS + 1)
    ]
    with pytest.raises(ValueError, match="currency pairs"):
        adapter.batch_market_sell(five)
    eleven = [
        {"symbol": "AAA/USDT", "amount": 1, "text": f"t-n{i:02d}"}
        for i in range(GATE_SPOT_BATCH_MAX_ORDERS_PER_PAIR + 1)
    ]
    with pytest.raises(ValueError, match="orders"):
        adapter.batch_market_sell(eleven)
    exchange.privateSpotPostBatchOrders.assert_not_called()


def test_b3_adapter_rejects_margin_and_non_sell_before_http():
    adapter, exchange = _real_adapter()
    with pytest.raises(ValueError, match="margin"):
        adapter.batch_market_sell(
            [
                {
                    "symbol": "AAA/USDT",
                    "amount": 1,
                    "text": "t-aaa",
                    "account": "margin",
                }
            ]
        )
    with pytest.raises(ValueError, match="market sells"):
        adapter.batch_market_sell(
            [{"symbol": "AAA/USDT", "amount": 1, "text": "t-aaa", "side": "buy"}]
        )
    exchange.privateSpotPostBatchOrders.assert_not_called()


@pytest.mark.parametrize("exc_type", [ccxt.NetworkError, ccxt.AuthenticationError])
def test_b6_adapter_transport_or_auth_raises_without_ack_list(exc_type):
    adapter, exchange = _real_adapter()
    exchange.privateSpotPostBatchOrders.side_effect = exc_type("down")
    with pytest.raises(BatchMarketSellNoAck) as raised:
        adapter.batch_market_sell(
            [{"symbol": "AAA/USDT", "amount": 1, "text": "t-aaa"}]
        )
    assert isinstance(raised.value.__cause__, exc_type)


def test_b6_adapter_non_list_response_is_no_ack():
    adapter, exchange = _real_adapter()
    exchange.privateSpotPostBatchOrders.return_value = {
        "label": "INVALID_KEY",
        "message": "Invalid key",
    }
    with pytest.raises(BatchMarketSellNoAck):
        adapter.batch_market_sell(
            [{"symbol": "AAA/USDT", "amount": 1, "text": "t-aaa"}]
        )


def test_b3_shadow_ack_matches_batch_shape_and_does_not_call_exchange():
    portfolio = MagicMock()
    adapter = GateExecutionAdapter(BotConfig({}), portfolio, mode="shadow")
    adapter._get_exchange = MagicMock(side_effect=AssertionError("network"))
    rows = adapter.batch_market_sell(
        [{"symbol": "AAA/USDT", "amount": 1.25, "text": "t-shadow1"}]
    )
    adapter._get_exchange.assert_not_called()
    portfolio.execute_sell.assert_not_called()
    assert rows[0]["succeeded"] is True
    assert rows[0]["label"] == ""
    assert rows[0]["message"] == ""
    assert rows[0]["text"] == "t-shadow1"
    assert rows[0]["currency_pair"] == "AAA_USDT"
    assert rows[0]["account"] == "spot"
    assert rows[0]["side"] == "sell"


def test_b3_selection_excludes_lock_loss_and_does_not_use_trail():
    calls = []

    def _sell(payload):
        calls.append(payload)
        return _acks_all_ok(payload)

    lots = [
        _lot("AAA/USDT"),
        _locked(_lot("BBB/USDT")),
        _lot("CCC/USDT", current_price=0.90),
        _lot("DDD/USDT", current_price=0),
    ]
    with patch("services.exit_realtime.execute.try_execute_trail_exit") as trail:
        out, trading = _run_batch(lots, _sell)
    trail.assert_not_called()
    assert len(calls) == 1
    assert _symbols(calls[0]) == ["AAA/USDT"]
    assert calls[0][0]["account"] == "spot"
    assert calls[0][0]["side"] == "sell"
    assert calls[0][0]["type"] == "market"
    assert str(calls[0][0]["text"]).startswith("t-")
    by_sym = {row["symbol"]: row for row in out["results"]}
    assert by_sym["BBB/USDT"]["code"] == "position_locked"
    assert by_sym["BBB/USDT"]["status"] == "failed"
    assert by_sym["BBB/USDT"]["message"].startswith("failed BBB/USDT:")
    assert by_sym["CCC/USDT"]["code"] == "not_in_profit"
    assert by_sym["DDD/USDT"]["code"] == "no_price"
    assert by_sym["AAA/USDT"]["executed"] is True
    assert by_sym["AAA/USDT"]["status"] == "closed"
    trading.execute_order.assert_not_called()
    assert trading.portfolio.execute_sell.call_count == 1


def test_b3_lock_check_error_is_not_submitted():
    def _sell(payload):
        raise AssertionError(f"payload built before lock check: {payload}")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    out, trading = _run_batch([_lot("AAA/USDT")], _sell, attach=_boom)
    assert out["executed"] is False
    assert out["results"][0]["code"] == "position_lock_check_error"
    trading.portfolio.execute_sell.assert_not_called()
    trading.execute_order.assert_not_called()


def test_b9_chunk_at_4_pairs_and_ten_orders_per_pair():
    calls = []

    def _sell(payload):
        calls.append(payload)
        return _acks_all_ok(payload)

    five = [_lot(f"P{i}/USDT") for i in range(5)]
    out, trading = _run_batch(five, _sell)
    assert [len(chunk) for chunk in calls] == [4, 1]
    assert all(len({row["symbol"] for row in chunk}) <= 4 for chunk in calls)
    assert out["filled"] == 5
    assert trading.portfolio.execute_sell.call_count == 5
    trading.execute_order.assert_not_called()

    calls.clear()
    many = [_lot("AAA/USDT", timeframe=f"t{i}") for i in range(11)]
    out2, trading2 = _run_batch(many, _sell)
    assert [len(chunk) for chunk in calls] == [10, 1]
    assert {row["symbol"] for chunk in calls for row in chunk} == {"AAA/USDT"}
    assert out2["filled"] == 11
    trading2.execute_order.assert_not_called()


def test_b5_partial_false_ack_maps_label_and_sequential_remainder():
    order_log = []

    def _sell(payload):
        order_log.append("batch")
        rows = []
        for i, row in enumerate(payload):
            if i == 1:
                rows.append(
                    {
                        "succeeded": False,
                        "label": "BALANCE_NOT_ENOUGH",
                        "message": "Not enough balance",
                        "text": row["text"],
                    }
                )
            else:
                rows.append(_full_fill(row, "ok-1"))
        return rows

    trading = _trading(_sell)

    def _remainder(order, *args, **kwargs):
        order_log.append(("seq", order.symbol, order.type, order.signal, order.source))
        return SimpleNamespace(executed=True, message="ok filled")

    trading.execute_order.side_effect = _remainder
    lots = [_lot("AAA/USDT"), _lot("BBB/USDT")]
    out, trading = _run_batch(lots, _sell, trading=trading)
    assert order_log[0] == "batch"
    assert order_log[1][0] == "seq"
    assert order_log[1][1:] == ("BBB/USDT", "SELL", SELL_FULL, LIQ_CASCADE_SOURCE)
    assert trading.portfolio.execute_sell.call_count == 1
    sold_symbol = trading.portfolio.execute_sell.call_args[0][0]
    assert sold_symbol == "AAA/USDT"
    by_sym = {row["symbol"]: row for row in out["results"]}
    assert by_sym["AAA/USDT"]["status"] == "closed"
    assert by_sym["AAA/USDT"]["code"] == "batch_ack"
    assert by_sym["BBB/USDT"]["executed"] is True
    assert by_sym["BBB/USDT"]["code"] == "sequential_remainder"
    assert by_sym["BBB/USDT"]["gate_label"] == "BALANCE_NOT_ENOUGH"
    assert "BALANCE_NOT_ENOUGH" in by_sym["BBB/USDT"]["message"]
    assert "STOP" not in (order_log[1][3] or "")


def test_b5_explicit_failure_does_not_block_the_next_chunk():
    calls = []

    def _sell(payload):
        calls.append([row["symbol"] for row in payload])
        rows = []
        for row in payload:
            failed = len(calls) == 1 and row["symbol"] == "P3/USDT"
            if failed:
                rows.append(
                    {
                        "succeeded": False,
                        "label": "ORDER_BOOK_NOT_FOUND",
                        "message": "empty book",
                        "text": row["text"],
                        "id": "",
                    }
                )
            else:
                rows.append(_full_fill(row, "ok"))
        return rows

    lots = [_lot(f"P{i}/USDT") for i in range(5)]
    out, trading = _run_batch(lots, _sell)
    assert calls == [["P0/USDT", "P1/USDT", "P2/USDT", "P3/USDT"], ["P4/USDT"]]
    assert trading.execute_order.call_count == 1
    assert trading.execute_order.call_args[0][0].symbol == "P3/USDT"
    assert trading.portfolio.execute_sell.call_count == 4
    failed = next(row for row in out["results"] if row["symbol"] == "P3/USDT")
    assert failed["gate_label"] == "ORDER_BOOK_NOT_FOUND"
    assert failed["code"] == "sequential_remainder"


def test_b5_transport_abort_stops_later_chunks_and_does_not_invent_fills():
    calls = []

    def _sell(payload):
        calls.append([row["symbol"] for row in payload])
        if len(calls) == 1:
            return _acks_all_ok(payload)
        raise BatchMarketSellNoAck("connection reset")

    lots = [_lot(f"P{i}/USDT") for i in range(9)]
    out, trading = _run_batch(lots, _sell)
    assert calls == [
        ["P0/USDT", "P1/USDT", "P2/USDT", "P3/USDT"],
        ["P4/USDT", "P5/USDT", "P6/USDT", "P7/USDT"],
    ]
    trading.execute_order.assert_not_called()
    assert trading.portfolio.execute_sell.call_count == 4
    by_sym = {row["symbol"]: row for row in out["results"]}
    for sym in ("P0/USDT", "P1/USDT", "P2/USDT", "P3/USDT"):
        assert by_sym[sym]["status"] == "closed"
    for sym in ("P4/USDT", "P5/USDT", "P6/USDT", "P7/USDT"):
        assert by_sym[sym]["code"] == "transport_error"
        assert by_sym[sym]["executed"] is False
    assert by_sym["P8/USDT"]["code"] == "chunk_failure"
    assert by_sym["P8/USDT"]["executed"] is False
    assert out["filled"] == 4


def test_b6_no_flatten_without_per_lot_ack():
    def _raise(_payload):
        raise RuntimeError("timed out")

    out, trading = _run_batch([_lot("AAA/USDT"), _lot("BBB/USDT")], _raise)
    assert out["executed"] is False
    assert {row["code"] for row in out["results"]} == {"transport_error"}
    trading.portfolio.execute_sell.assert_not_called()
    trading.execute_order.assert_not_called()

    def _none(_payload):
        return None

    out2, trading2 = _run_batch([_lot("AAA/USDT")], _none)
    assert out2["results"][0]["code"] == "transport_error"
    trading2.portfolio.execute_sell.assert_not_called()
    trading2.execute_order.assert_not_called()

    def _dict(_payload):
        return {"label": "INVALID_KEY", "message": "Invalid key"}

    out3, trading3 = _run_batch([_lot("AAA/USDT")], _dict)
    assert out3["results"][0]["code"] == "transport_error"
    trading3.portfolio.execute_sell.assert_not_called()

    def _missing(payload):
        return [
            {
                "succeeded": True,
                "label": "",
                "message": "",
                "text": "t-someone-else",
                "id": "1",
            }
        ]

    out4, trading4 = _run_batch([_lot("AAA/USDT"), _lot("BBB/USDT")], _missing)
    assert {row["code"] for row in out4["results"]} == {"no_ack"}
    assert all(row["executed"] is False for row in out4["results"])
    trading4.portfolio.execute_sell.assert_not_called()
    trading4.execute_order.assert_not_called()

    def _uncertain(payload):
        return [{"text": row["text"], "label": "", "message": ""} for row in payload]

    out5, trading5 = _run_batch([_lot("AAA/USDT")], _uncertain)
    assert out5["results"][0]["code"] == "no_ack"
    trading5.portfolio.execute_sell.assert_not_called()
    trading5.execute_order.assert_not_called()


def test_b6_transport_brakes_a_resend_and_does_not_record_a_fill():
    calls = []

    def _sell(payload):
        calls.append(payload)
        raise RuntimeError("timed out")

    lot = _lot("AAA/USDT")
    _clear(["AAA/USDT"])
    trading = _trading(_sell)
    try:
        with _book([lot]):
            first = execute_cascade_exit_batch(
                side="long",
                lots=[lot],
                prices={"AAA/USDT": 1.10},
                trading=trading,
                fire_enabled=True,
            )
            second = execute_cascade_exit_batch(
                side="long",
                lots=[lot],
                prices={"AAA/USDT": 1.10},
                trading=trading,
                fire_enabled=True,
            )
        assert len(calls) == 1
        assert first["executed"] is False
        assert first["results"][0]["code"] == "transport_error"
        assert second["executed"] is False
        assert second["results"][0]["code"] == "recent_exit"
        trading.portfolio.execute_sell.assert_not_called()
        trading.execute_order.assert_not_called()
    finally:
        _clear(["AAA/USDT"])


def test_b2_shorts_stay_on_sequential_cover_and_skip_the_spot_batch():
    def _sell(_payload):
        raise AssertionError("COVER must not be sent to the spot batch endpoint")

    lot = _lot("BBB/USDT", side="short", current_price=0.90)
    out, trading = _run_batch([lot], _sell, side="short")
    assert out["action"] == COVER_FULL
    assert out["executed"] is True
    trading.portfolio.execute_sell.assert_not_called()
    order = trading.execute_order.call_args[0][0]
    assert order.type == "COVER"
    assert order.signal == COVER_FULL
    assert order.source == LIQ_CASCADE_SOURCE
    assert "STOP" not in order.signal
    assert "STOP" not in (order.exit_rationale or "")


def test_b7_fire_enabled_false_does_not_flatten_even_if_batch_enabled():
    def _sell(_payload):
        raise AssertionError("batch submit while fire_enabled is false")

    raw = {"exit_realtime": {"cascade": {"fire_enabled": False, "batch_enabled": True}}}
    trading = _trading(_sell)
    trading.execute_order.side_effect = AssertionError("sequential flatten")
    _clear(["AAA/USDT"])
    with _book([_lot("AAA/USDT")]):
        out = execute_cascade_exit_batch(
            side="long",
            lots=[_lot("AAA/USDT")],
            prices={"AAA/USDT": 1.10},
            trading=trading,
            fire_enabled=False,
            raw_config=raw,
        )
        out_default = execute_cascade_exit_batch(
            side="long",
            lots=[_lot("AAA/USDT")],
            prices={"AAA/USDT": 1.10},
            trading=trading,
            raw_config=raw,
        )
    assert out["executed"] is False
    assert out["message"] == "fire_disabled"
    assert out["results"] == []
    assert out_default["message"] == "fire_disabled"
    assert out_default["executed"] is False
    trading.portfolio.execute_sell.assert_not_called()
    trading.execute_order.assert_not_called()


def _hub(cascade: dict):
    from services.exit_realtime.hub import ExitRealtimeHub

    hub = ExitRealtimeHub({"exit_realtime": {"enabled": True, "cascade": cascade}})
    hub._cascade_detector = MagicMock()
    hub._cascade_state = CascadeState(600)
    hub._cascade_detector.evaluate.return_value = {
        "long": SimpleNamespace(fire=True, window_usd="1000000", ratio=4.0)
    }
    return hub


def test_b7_hub_fire_disabled_does_not_call_batch_or_sequential():
    hub = _hub({"enabled": True, "fire_enabled": False, "batch_enabled": True})
    with patch(
        "services.exit_realtime.execute.execute_cascade_exit_batch"
    ) as batch, patch(
        "services.exit_realtime.execute.execute_cascade_exit"
    ) as sequential:
        out = hub.on_liq_batch([])
    batch.assert_not_called()
    sequential.assert_not_called()
    assert out.get("exits") in (None, [])


def test_hub_uses_batch_only_when_both_flags_are_true():
    both = _hub({"enabled": True, "fire_enabled": True, "batch_enabled": True})
    with patch(
        "services.exit_realtime.execute.execute_cascade_exit_batch",
        return_value={"executed": False},
    ) as batch, patch(
        "services.exit_realtime.execute.execute_cascade_exit",
        return_value={"executed": False},
    ) as sequential:
        both.on_liq_batch([])
    batch.assert_called_once()
    sequential.assert_not_called()
    assert batch.call_args.kwargs["fire_enabled"] is True

    sequential_only = _hub(
        {"enabled": True, "fire_enabled": True, "batch_enabled": False}
    )
    with patch(
        "services.exit_realtime.execute.execute_cascade_exit_batch"
    ) as batch2, patch(
        "services.exit_realtime.execute.execute_cascade_exit",
        return_value={"executed": False},
    ) as sequential2:
        sequential_only.on_liq_batch([])
    sequential2.assert_called_once()
    batch2.assert_not_called()
    assert sequential2.call_args.kwargs["fire_enabled"] is True


def test_shadow_adapter_ack_is_what_allows_the_local_flatten():
    portfolio = SimpleNamespace(
        execute_sell=MagicMock(return_value=SimpleNamespace(executed=True, message="paper"))
    )
    adapter = GateExecutionAdapter(BotConfig({}), MagicMock(), mode="shadow")
    adapter._get_exchange = MagicMock(side_effect=AssertionError("network"))
    trading = SimpleNamespace(
        adapter=adapter,
        portfolio=portfolio,
        execute_order=MagicMock(side_effect=AssertionError("sequential")),
    )
    out, _ = _run_batch([_lot("AAA/USDT")], adapter.batch_market_sell, trading=trading)
    adapter._get_exchange.assert_not_called()
    assert out["executed"] is True
    assert out["results"][0]["code"] == "batch_ack"
    portfolio.execute_sell.assert_called_once()
    trading.execute_order.assert_not_called()


def test_b8_prints_outcome_table_without_a_timing_claim(capsys):
    lots = [_lot(f"P{i}/USDT") for i in range(6)]
    lots.append(_locked(_lot("LOCKED/USDT")))
    lots.append(_lot("LOSS/USDT", current_price=0.90))
    symbols = [lot["symbol"] for lot in lots]

    seq_trading = MagicMock()
    seq_trading.execute_order.return_value = SimpleNamespace(executed=True, message="ok filled")
    _clear(symbols)
    t0 = time.perf_counter()
    with _book(lots):
        sequential = execute_cascade_exit(
            side="long",
            lots=lots,
            prices={lot["symbol"]: float(lot["current_price"]) for lot in lots},
            trading=seq_trading,
            fire_enabled=True,
        )
    sequential_sec = time.perf_counter() - t0

    def _sell(payload):
        return _acks_all_ok(payload)

    t1 = time.perf_counter()
    batched, _ = _run_batch(lots, _sell)
    batch_sec = time.perf_counter() - t1

    seq_by = {row["symbol"]: row for row in sequential["results"]}
    batch_by = {row["symbol"]: row for row in batched["results"]}
    print("b8 per-lot outcome (one synthetic run, not a spec claim)")
    print(f"{'symbol':<14} {'sequential':<12} {'batch':<12} batch_code")
    for lot in lots:
        sym = lot["symbol"]
        seq_row = seq_by[sym]
        batch_row = batch_by[sym]
        seq_state = "closed" if seq_row.get("executed") else "failed"
        batch_state = batch_row.get("status") or ("closed" if batch_row.get("executed") else "failed")
        print(
            f"{sym:<14} {seq_state:<12} {batch_state:<12} {batch_row.get('code') or ''}"
        )
    print(
        f"wall_clock_sec sequential={sequential_sec:.6f} batch={batch_sec:.6f}"
    )
    captured = capsys.readouterr().out
    assert "b8 per-lot outcome" in captured
    assert "wall_clock_sec" in captured

    def _closed(rows):
        return sorted(row["symbol"] for row in rows if row.get("executed"))

    assert _closed(sequential["results"]) == _closed(batched["results"])
    assert batch_by["LOCKED/USDT"]["code"] == "position_locked"
    assert seq_by["LOCKED/USDT"]["code"] == "position_locked"
    assert batch_by["LOSS/USDT"]["code"] == "not_in_profit"
    assert seq_by["LOSS/USDT"]["message"] == "not_in_profit"
    _clear(symbols)


def test_normalize_batch_ack_keeps_fill_fields():
    adapter, exchange = _real_adapter()
    exchange.privateSpotPostBatchOrders.return_value = [
        {
            "succeeded": True,
            "text": "t-aaa",
            "id": "99",
            "status": "cancelled",
            "finish_as": "depth_not_enough",
            "filled_amount": "0",
            "left": "10.5",
            "avg_deal_price": "0",
            "fill_price": "0",
        }
    ]
    rows = adapter.batch_market_sell(
        [{"symbol": "AAA/USDT", "amount": 10.5, "text": "t-aaa"}]
    )
    assert rows[0]["succeeded"] is True
    assert rows[0]["finish_as"] == "depth_not_enough"
    assert rows[0]["filled_amount"] == 0.0
    assert rows[0]["left"] == 10.5
    assert rows[0]["avg_deal_price"] == 0.0
    assert rows[0]["fill_price"] == 0.0
    assert rows[0]["id"] == "99"


def test_lease_missing_sends_no_batch_and_does_not_flatten():
    from storage.errors import WriterLeaseLost

    def _sell(_payload):
        raise AssertionError("batch HTTP while the writer lease is missing")

    trading = _trading(_sell)
    with patch(
        "bus.writer_lease.require_lease_for_order",
        side_effect=WriterLeaseLost(reason="not_held"),
    ):
        out, trading = _run_batch([_lot("AAA/USDT")], _sell, trading=trading)
    assert out["executed"] is False
    assert out["filled"] == 0
    assert out["message"] == "no_writer_lease"
    assert out["results"][0]["code"] == "no_writer_lease"
    trading.portfolio.execute_sell.assert_not_called()
    trading.execute_order.assert_not_called()


def test_intent_queue_fails_closed_without_a_batch_or_an_intent():
    def _sell(_payload):
        raise AssertionError("batch HTTP while the intent queue would take the order")

    trading = _trading(_sell)
    with patch(
        "services.trading_engine_runtime.should_queue_intent", return_value=True
    ), patch(
        "services.trading_engine_runtime.submit_trade_intent",
        side_effect=AssertionError("intent queue path is out of scope"),
    ):
        out, trading = _run_batch([_lot("AAA/USDT")], _sell, trading=trading)
    assert out["executed"] is False
    assert out["message"] == "intent_queue_required"
    assert out["results"][0]["code"] == "intent_queue_required"
    trading.portfolio.execute_sell.assert_not_called()
    trading.execute_order.assert_not_called()


def test_attribution_runs_under_ledger_lock():
    from core.tenant_context import resolve_tenant_scope

    held = {"value": False}
    seen = {}

    class _Lock:
        def __enter__(self):
            held["value"] = True
            return self

        def __exit__(self, *_args):
            held["value"] = False
            return False

    def _lock(scope, cfg=None, **kwargs):
        seen["scope"] = scope
        seen["cfg"] = cfg
        return _Lock()

    def _sell_pos(*args, **kwargs):
        assert held["value"] is True
        seen["sell_args"] = args
        return SimpleNamespace(executed=True, message="paper", amount=args[4], price=args[2])

    def _clear(*_args, **_kwargs):
        assert held["value"] is True
        seen["cleared"] = True
        return False

    cfg = BotConfig({})
    trading = _trading(_acks_all_ok)
    trading.config = cfg
    trading.portfolio.execute_sell.side_effect = _sell_pos
    with patch("bus.locks.ledger_lock", side_effect=_lock), patch(
        "strategies.positions.hard_clear_closed_lot", side_effect=_clear
    ):
        out, trading = _run_batch([_lot("AAA/USDT")], _acks_all_ok, trading=trading)
    assert out["results"][0]["code"] == "batch_ack"
    assert seen["scope"] == resolve_tenant_scope()
    assert seen["cfg"] is cfg
    assert seen["cleared"] is True
    assert seen["sell_args"][2] == pytest.approx(1.10)
    assert seen["sell_args"][4] == pytest.approx(10.0)


def test_zero_fill_depth_not_enough_does_not_flatten_and_stays_sequential():
    from services.order_service import OrderService

    recorded = []
    seen = {}

    def _sell(payload):
        seen["text"] = payload[0]["text"]
        return [
            {
                "succeeded": True,
                "label": "",
                "message": "",
                "text": payload[0]["text"],
                "id": "z-1",
                "status": "cancelled",
                "finish_as": "depth_not_enough",
                "filled_amount": 0,
                "left": payload[0]["amount"],
                "avg_deal_price": "0",
                "fill_price": "0",
            }
        ]

    trading = _trading(_sell)

    def _remainder(order, *_args, **_kwargs):
        seen["remainder"] = (order.symbol, order.type, order.amount)
        return SimpleNamespace(executed=False, message="not sent in this test")

    trading.execute_order.side_effect = _remainder
    with patch("data_manager.record_live_trade", side_effect=lambda trade: recorded.append(trade)):
        out, trading = _run_batch([_lot("AAA/USDT")], _sell, trading=trading)
    assert out["results"][0]["code"] == "zero_fill_cancel"
    assert out["results"][0]["executed"] is False
    trading.portfolio.execute_sell.assert_not_called()
    assert seen["remainder"][0] == "AAA/USDT"
    assert seen["remainder"][1] == "SELL"
    assert seen["remainder"][2] == pytest.approx(10.0)
    assert recorded == []
    assert OrderService().find_by_idempotency_key(seen["text"]) is None


def test_partial_fill_attributes_only_the_filled_amount():
    def _sell(payload):
        row = payload[0]
        return [
            {
                "succeeded": True,
                "label": "",
                "message": "",
                "text": row["text"],
                "id": "p-1",
                "status": "closed",
                "finish_as": "stp",
                "filled_amount": "4",
                "left": "6",
                "avg_deal_price": "1.25",
                "fill_price": "1.25",
            }
        ]

    trading = _trading(_sell)
    out, trading = _run_batch(
        [_lot("AAA/USDT", amount=10.0, current_price=1.10)], _sell, trading=trading
    )
    assert out["results"][0]["executed"] is True
    assert out["results"][0]["code"] == "batch_ack"
    assert out["results"][0]["amount"] == pytest.approx(4.0)
    assert out["results"][0]["price"] == pytest.approx(1.25)
    trading.execute_order.assert_not_called()
    args = trading.portfolio.execute_sell.call_args[0]
    assert args[0] == "AAA/USDT"
    assert args[2] == pytest.approx(1.25)
    assert args[4] == pytest.approx(4.0)


def test_acknowledged_fill_records_order_row_and_live_trade_zero_fill_does_not():
    from services.order_service import OrderService

    recorded = []
    seen = {}

    def _spy(trade):
        recorded.append(dict(trade))
        return None

    def _sell(payload):
        seen["payload"] = payload
        rows = []
        for i, row in enumerate(payload):
            if row["symbol"] == "ZZZ/USDT":
                rows.append(
                    {
                        "succeeded": True,
                        "text": row["text"],
                        "id": "zero-1",
                        "status": "cancelled",
                        "finish_as": "depth_not_enough",
                        "filled_amount": 0,
                        "left": row["amount"],
                        "fill_price": "0",
                    }
                )
            else:
                rows.append(_full_fill(row, f"fill-{i}"))
        return rows

    trading = _trading(_sell)
    trading.execute_order.return_value = SimpleNamespace(executed=False, message="remainder skipped")
    lots = [_lot("AAA/USDT", amount=10.0, current_price=1.10), _lot("ZZZ/USDT")]
    with patch("data_manager.record_live_trade", side_effect=_spy):
        out, trading = _run_batch(lots, _sell, trading=trading)
    by_sym = {row["symbol"]: row for row in out["results"]}
    assert by_sym["AAA/USDT"]["code"] == "batch_ack"
    assert by_sym["ZZZ/USDT"]["code"] == "zero_fill_cancel"
    assert len(recorded) == 1
    trade = recorded[0]
    aaa_text = next(row["text"] for row in seen["payload"] if row["symbol"] == "AAA/USDT")
    zzz_text = next(row["text"] for row in seen["payload"] if row["symbol"] == "ZZZ/USDT")
    assert trade["exchange_order_id"] == "fill-0"
    assert trade["text"] == aaa_text
    assert float(trade["amount"]) == pytest.approx(10.0)
    assert float(trade["price"]) == pytest.approx(1.10)
    assert trade["symbol"] == "AAA/USDT"
    order = OrderService().find_by_idempotency_key(aaa_text)
    assert order is not None
    assert order["exchange_order_id"] == "fill-0"
    assert order.get("client_order_id") == aaa_text
    assert float(order["filled_qty"]) == pytest.approx(10.0)
    assert float(order["execution"]["amount"]) == pytest.approx(10.0)
    assert float(order["execution"]["price"]) == pytest.approx(1.10)
    assert OrderService().find_by_idempotency_key(zzz_text) is None
    trading.portfolio.execute_sell.assert_called_once()
