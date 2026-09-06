"""#305 slice 3: /panic preview, 60s token, sequential SELL_STOP_FULL."""

from __future__ import annotations

from unittest.mock import patch

from core.models import TradeOrder, TradeResult
from notifications.telegram_commands.pause_commands import (
    PANIC_SIGNAL,
    PANIC_TTL_SEC,
    _create_panic_token,
    _execute_panic,
    collect_panic_lots,
    consume_panic_token,
    handle,
    handle_callback,
    reset_panic_for_tests,
)
from strategies.position_lock import build_lock


class _Clock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _pos(symbol, amount, entry, *, side="long", timeframe="4h", locked=False):
    lot = {
        "symbol": symbol,
        "amount": amount,
        "average_entry": entry,
        "entry_price": entry,
        "timeframe": timeframe,
        "side": side,
    }
    if locked:
        lot["lock"] = build_lock(reason="hold", locked_by="test")
    return lot


def test_preview_lists_notional_desc_marks_locked_excludes_shorts():
    positions = [
        _pos("DOGE/USDT", 1000, 0.10),           # notional 100
        _pos("ETH/USDT", 10, 2000, locked=True),  # notional 30_000 locked
        _pos("BTC/USDT", 1, 40000),               # notional 50_000
        _pos("SOL/USDT", 5, 100, side="short"),   # excluded
    ]
    prices = {
        "DOGE/USDT": 0.10,
        "ETH/USDT": 3000.0,
        "BTC/USDT": 50000.0,
        "SOL/USDT": 150.0,
    }
    lots = collect_panic_lots(positions, prices)
    symbols = [row["symbol"] for row in lots]
    assert symbols == ["BTC/USDT", "ETH/USDT", "DOGE/USDT"]
    assert "SOL/USDT" not in symbols
    by_sym = {row["symbol"]: row for row in lots}
    assert by_sym["ETH/USDT"]["locked"] is True
    assert by_sym["BTC/USDT"]["locked"] is False
    assert by_sym["BTC/USDT"]["notional"] > by_sym["DOGE/USDT"]["notional"]

    captured = {}

    def _buttons(msg, keyboard, **_k):
        captured["msg"] = msg
        captured["keyboard"] = keyboard
        return True

    with patch("strategies.positions.list_active_positions", return_value=positions), \
         patch("price_fetcher.get_prices_batch", return_value=prices), \
         patch("notifications.telegram_commands.pause_commands.send_telegram_buttons", side_effect=_buttons), \
         patch("notifications.telegram_commands.pause_commands.send_telegram_message"):
        assert handle("/panic") is True
    msg = captured["msg"]
    assert msg.index("BTC/USDT") < msg.index("ETH/USDT") < msg.index("DOGE/USDT")
    assert "SOL/USDT" not in msg
    assert "gesperrt, wird übersprungen" in msg
    assert "ETH/USDT" in msg
    row0 = captured["keyboard"][0]
    labels = [b["text"] for b in row0]
    assert "Alles schließen" in labels
    assert "Abbrechen" in labels
    assert any(b["callback_data"].startswith("panic_ok:") for b in row0)


def test_confirmation_token_expires_after_60s_pinned_clock():
    clock = _Clock(1000.0)
    reset_panic_for_tests(clock=clock)
    token = _create_panic_token([{"symbol": "BTC/USDT", "locked": False, "amount": 1}])
    assert consume_panic_token(token) is not None

    token2 = _create_panic_token([{"symbol": "ETH/USDT"}])
    clock.t += PANIC_TTL_SEC - 0.1
    assert consume_panic_token(token2) is not None

    token3 = _create_panic_token([{"symbol": "DOGE/USDT"}])
    clock.t += PANIC_TTL_SEC + 0.1
    assert consume_panic_token(token3) is None

    with patch("notifications.telegram_commands.pause_commands.send_telegram_message") as send, \
         patch("notifications.telegram_commands.pause_commands.answer_callback_query"):
        assert handle_callback({"id": "cb", "data": f"panic_ok:{token3}"}) is True
        assert "abgelaufen" in send.call_args[0][0].lower() or "expired" in send.call_args[0][0].lower()


def test_execution_sell_stop_full_skips_locked_stops_on_ledger_unavailable():
    reset_panic_for_tests()
    lots = [
        {
            "symbol": "BTC/USDT",
            "timeframe": "4h",
            "amount": 1.0,
            "price": 50000.0,
            "notional": 50000.0,
            "locked": False,
        },
        {
            "symbol": "ETH/USDT",
            "timeframe": "4h",
            "amount": 10.0,
            "price": 3000.0,
            "notional": 30000.0,
            "locked": False,
        },
        {
            "symbol": "PEPE/USDT",
            "timeframe": "4h",
            "amount": 100.0,
            "price": 1.0,
            "notional": 100.0,
            "locked": True,
        },
        {
            "symbol": "DOGE/USDT",
            "timeframe": "4h",
            "amount": 1000.0,
            "price": 0.1,
            "notional": 100.0,
            "locked": False,
        },
    ]
    calls: list[TradeOrder] = []

    def _exec(order, timeframe="4h", source="manual", **_k):
        calls.append(order)
        if order.symbol == "ETH/USDT":
            result = TradeResult(False, "SELL", order.symbol, message="Ledger unavailable: down")
            result.code = "ledger_unavailable"
            return result
        return TradeResult(True, "SELL", order.symbol, amount=order.amount, price=order.price)

    saved = {}

    def _save(*, entries_enabled=None, exits_enabled=None):
        saved["entries_enabled"] = entries_enabled
        saved["exits_enabled"] = exits_enabled
        return True

    with patch("services.trading_service.TradingService.execute_order", side_effect=_exec), \
         patch("strategies.positions.get_position", return_value={"amount": 0}), \
         patch("price_fetcher.get_prices", return_value=(None, None)), \
         patch("notifications.telegram_commands.pause_commands.save_trading_flags", side_effect=_save), \
         patch("notifications.telegram_commands.pause_commands.send_telegram_message") as send, \
         patch("notifications.telegram_commands.pause_commands.get_bot_config") as gbc:
        gbc.return_value.exits_enabled = True
        _execute_panic(lots)

    assert [o.symbol for o in calls] == ["BTC/USDT", "ETH/USDT"]
    assert all(o.signal == PANIC_SIGNAL == "SELL_STOP_FULL" for o in calls)
    assert all(o.type == "SELL" for o in calls)
    assert all(o.amount > 0 for o in calls)
    assert saved["entries_enabled"] is False
    summary = "\n".join(c[0][0] for c in send.call_args_list)
    assert "BTC/USDT" in summary
    assert "ETH/USDT" in summary
    assert "PEPE/USDT" in summary
    assert "gesperrt" in summary.lower() or "locked" in summary.lower() or "Übersprungen" in summary or "Skipped" in summary
    assert "DOGE/USDT" in summary  # remaining after ledger stop


def test_panic_leaves_slot_and_rebuy_cooldown_state_untouched():
    import risk.slot_eviction_runtime as sev

    reset_panic_for_tests()
    sev._SYMBOL_COOLDOWN["BTC/USDT"] = 99.0
    sev._EVICT_TS.append(1.5)
    before_cd = dict(sev._SYMBOL_COOLDOWN)
    before_ts = list(sev._EVICT_TS)
    lots = [
        {
            "symbol": "BTC/USDT",
            "timeframe": "4h",
            "amount": 1.0,
            "price": 10.0,
            "notional": 10.0,
            "locked": False,
        }
    ]
    with patch(
        "services.trading_service.TradingService.execute_order",
        return_value=TradeResult(True, "SELL", "BTC/USDT", amount=1, price=10),
    ), \
         patch("strategies.positions.get_position", return_value={"amount": 1}), \
         patch("price_fetcher.get_prices", return_value=(10.0, None)), \
         patch("notifications.telegram_commands.pause_commands.save_trading_flags", return_value=True), \
         patch("notifications.telegram_commands.pause_commands.send_telegram_message"), \
         patch("notifications.telegram_commands.pause_commands.get_bot_config") as gbc:
        gbc.return_value.exits_enabled = True
        _execute_panic(lots)
    assert dict(sev._SYMBOL_COOLDOWN) == before_cd
    assert list(sev._EVICT_TS) == before_ts


def test_panic_progress_every_five_positions():
    reset_panic_for_tests()
    lots = [
        {
            "symbol": f"C{i}/USDT",
            "timeframe": "4h",
            "amount": 1.0,
            "price": 10.0,
            "notional": 100 - i,
            "locked": False,
        }
        for i in range(6)
    ]
    messages: list[str] = []

    def _send(msg, **_k):
        messages.append(msg)
        return True

    with patch(
        "services.trading_service.TradingService.execute_order",
        side_effect=lambda order, *a, **k: TradeResult(
            True, "SELL", order.symbol, amount=1, price=10
        ),
    ), \
         patch("strategies.positions.get_position", return_value={"amount": 1}), \
         patch("price_fetcher.get_prices", return_value=(10.0, None)), \
         patch("notifications.telegram_commands.pause_commands.save_trading_flags", return_value=True), \
         patch("notifications.telegram_commands.pause_commands.send_telegram_message", side_effect=_send), \
         patch("notifications.telegram_commands.pause_commands.get_bot_config") as gbc:
        gbc.return_value.exits_enabled = True
        _execute_panic(lots)
    progress = [m for m in messages if "5/6" in m or "5/6" in m.replace(" ", "")]
    assert any("5" in m and "6" in m and ("bearbeitet" in m or "processed" in m) for m in messages)
    assert progress or any("panic_progress" in m.lower() or "⏳" in m for m in messages)
