"""#655 Rev 6d: spot-long cost basis, dust keep, fee-unknown estimate.

Symbols and prices here are fixtures. Production code does not name them.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.costs import CostModel, Fill, vip0_default
from core.models import RiskDecision, TradeOrder, TradeResult
from core.portfolio_baseline import reconcile_display_nav
from core.sim_ledger_replay import replay_simulated_ledger
from strategies.positions import (
    DUST_AMOUNT_EPSILON,
    clear_positions_memory,
    derive_positions_from_orders_and_cache,
    get_key,
    get_position,
    is_open_position,
    update_position,
)


TF = "4h"
INITIAL = 100_000.0


def _paper_cfg(**extra) -> dict:
    cfg = {
        "trading_mode": "paper",
        "live": {"dry_run": True},
        "paper": {"initial_capital_usdt": INITIAL},
        "costs": {
            "fee_source": "config",
            "gate": {
                "spot": {
                    "fee_maker_pct": 0.10,
                    "fee_taker_pct": 0.20,
                    "slippage_pct": 0.15,
                    "fee_side_buy": "base",
                    "fee_side_sell": "quote",
                }
            },
        },
    }
    cfg.update(extra)
    return cfg


def _fee_cfg(*, side: str = "base", maker: float = 0.10, taker: float = 0.20) -> dict:
    cfg = _paper_cfg()
    spot = cfg["costs"]["gate"]["spot"]
    spot["fee_side_buy"] = side
    spot["fee_maker_pct"] = maker
    spot["fee_taker_pct"] = taker
    return cfg


def _order(
    oid: str,
    side: str,
    symbol: str,
    price: float,
    amount: float,
    *,
    usdt: float | None = None,
    ts: str,
    order_type: str = "market",
    fee_unknown: bool = False,
    filled_qty_gross: float | None = None,
    omit_usdt: bool = False,
    leverage: float | None = None,
    signal: str | None = None,
) -> dict:
    execution: dict = {"price": price, "amount": amount}
    if not omit_usdt:
        execution["usdt"] = price * amount if usdt is None else usdt
    if fee_unknown:
        execution["fee_unknown"] = True
    if filled_qty_gross is not None:
        execution["filled_qty_gross"] = filled_qty_gross
    row = {
        "id": oid,
        "status": "filled",
        "side": side,
        "symbol": symbol,
        "timeframe": TF,
        "order_type": order_type,
        "execution": execution,
        "timestamps": {"created": ts, "filled": ts},
    }
    if leverage is not None:
        row["leverage"] = leverage
    if signal:
        row["signal"] = signal
    return row


def _gross_fill(price: float, amount: float) -> Fill:
    quote = float(price) * float(amount)
    return Fill(
        side="buy",
        order_type="market",
        request_price=price,
        fill_price=price,
        qty_gross=amount,
        qty_net=amount,
        quote_gross=quote,
        quote_net=quote,
        fee_base=0.0,
        fee_quote=0.0,
        fee_usdt=0.0,
        slippage_usdt=0.0,
    )


def _sell_fill(price: float, amount: float) -> Fill:
    quote = float(price) * float(amount)
    return Fill(
        side="sell",
        order_type="market",
        request_price=price,
        fill_price=price,
        qty_gross=amount,
        qty_net=amount,
        quote_gross=quote,
        quote_net=quote,
        fee_base=0.0,
        fee_quote=0.0,
        fee_usdt=0.0,
        slippage_usdt=0.0,
    )


def _reload(orders: list, cache: dict | None = None, *, config: dict | None = None):
    from strategies.positions import load_positions

    cache_doc = cache if cache is not None else {"positions": {}}
    cfg = config or _paper_cfg()
    with patch("data_manager.load_orders", return_value={"orders": orders}), patch(
        "services.ledger_sync.load_orders", create=True, return_value={"orders": orders}
    ), patch(
        "strategies.positions.load_positions_document", return_value=cache_doc
    ), patch("data_manager.load_positions_document", return_value=cache_doc), patch(
        "data_manager.get_config", return_value=cfg
    ), patch("core.portfolio_baseline.initial_capital", return_value=INITIAL):
        return load_positions()


def _lot(snapshot: dict, symbol: str) -> dict:
    return snapshot[get_key(symbol, TF)]


@pytest.fixture(autouse=True)
def _clean_books(monkeypatch):
    docs: dict[tuple, dict] = {}

    def _load(scope="paper", tenant_id=None, **_k):
        key = (str(tenant_id or "default"), str(scope or "paper"))
        return {"positions": dict((docs.get(key) or {}).get("positions") or {})}

    def _save(data, scope="paper", tenant_id=None, **_k):
        key = (str(tenant_id or "default"), str(scope or "paper"))
        docs[key] = {"positions": dict((data or {}).get("positions") or {})}
        return True

    monkeypatch.setattr("strategies.positions.load_positions_document", _load)
    monkeypatch.setattr("strategies.positions.save_positions_document", _save)
    monkeypatch.setattr("data_manager.load_positions_document", _load)
    monkeypatch.setattr("data_manager.save_positions_document", _save)
    clear_positions_memory()
    yield
    clear_positions_memory()


def test_t1_closed_cycle_pnl_matches_cash_identity():
    from services.portfolio_service import PortfolioService

    symbol = "PX/USDT"
    buy = _order(
        "b1", "buy", symbol, 1.0, 99.8, usdt=100.0, ts="2026-10-01T00:00:00",
        filled_qty_gross=100.0,
    )
    snap = _reload([buy])
    assert snap[get_key(symbol, TF)]["average_entry"] == pytest.approx(100.0 / 99.8)

    recorded = []
    with patch("services.portfolio_service.record_trade", side_effect=lambda row: recorded.append(row)):
        result = PortfolioService().execute_sell(
            symbol, TF, 1.0, "SELL_FULL", 99.8, fill=_sell_fill(1.0, 99.8),
        )
    assert result.executed
    assert result.pnl_basis == "net"
    assert result.pnl == pytest.approx(99.8 - 100.0)
    assert recorded[0]["pnl"] == pytest.approx(result.pnl)
    assert recorded[0]["pnl_basis"] == "net"
    assert recorded[0]["usdt_received"] - buy["execution"]["usdt"] == pytest.approx(result.pnl)
    assert float(get_position(symbol, TF)["amount"]) == pytest.approx(0.0)


def test_t1b_unstamped_row_keeps_stored_ratio_and_does_not_warn():
    from services.portfolio_service import PortfolioService

    symbol = "PX/USDT"
    buy = _order("b1", "buy", symbol, 2.0, 40.0, usdt=80.0, ts="2026-10-01T00:00:00")
    assert "filled_qty_gross" not in buy["execution"]
    warns: list[str] = []

    def _log(msg, level="INFO"):
        if level == "WARNING" and "basis fee estimated" in str(msg):
            warns.append(str(msg))

    with patch("core.sim_ledger_replay.log", side_effect=_log):
        snap = _reload([buy])
    pos = _lot(snap, symbol)
    assert pos["average_entry"] == pytest.approx(2.0)
    assert warns == []

    clear_positions_memory()
    with patch("services.portfolio_service.log", side_effect=_log):
        PortfolioService().execute_buy(
            symbol, TF, 2.0, fill=_gross_fill(2.0, 40.0), sync_virtual_ledger=False,
        )
    mem = get_position(symbol, TF)
    assert float(mem["average_entry"]) == pytest.approx(pos["average_entry"])
    assert not mem.get("basis_fee_estimated")
    assert warns == []

    # fee_unknown without a gross stamp is still N1: do not invent a fee.
    stamped_missing = _order(
        "b2", "buy", "LOT/USDT", 3.0, 10.0, usdt=30.0, ts="2026-10-01T00:01:00",
        fee_unknown=True,
    )
    with patch("core.sim_ledger_replay.log", side_effect=_log):
        other = _reload([stamped_missing])
    assert _lot(other, "LOT/USDT")["average_entry"] == pytest.approx(3.0)
    assert not _lot(other, "LOT/USDT").get("basis_fee_estimated")
    assert warns == []


def test_t2_two_price_dca_memory_matches_reload():
    from services.portfolio_service import PortfolioService

    symbol = "PX/USDT"
    orders = [
        _order("b1", "buy", symbol, 1.0, 100.0, usdt=100.2, ts="2026-10-01T00:00:00", filled_qty_gross=100.2),
        _order("b2", "buy", symbol, 2.0, 50.0, usdt=100.4, ts="2026-10-01T01:00:00", filled_qty_gross=50.2),
    ]
    snap = _reload(orders)
    expected = (100.2 + 100.4) / 150.0
    assert _lot(snap, symbol)["average_entry"] == pytest.approx(expected)

    clear_positions_memory()
    book = PortfolioService()
    book.execute_buy(symbol, TF, 1.0, fill=_fill_net(1.0, 100.0, 100.2), sync_virtual_ledger=False)
    book.execute_buy(symbol, TF, 2.0, fill=_fill_net(2.0, 50.0, 100.4), sync_virtual_ledger=False, source="dca")
    assert float(get_position(symbol, TF)["average_entry"]) == pytest.approx(expected)
    assert float(get_position(symbol, TF)["amount"]) == pytest.approx(150.0)


def _fill_net(price: float, amount: float, quote: float) -> Fill:
    return Fill(
        side="buy",
        order_type="market",
        request_price=price,
        fill_price=price,
        qty_gross=amount,
        qty_net=amount,
        quote_gross=quote,
        quote_net=quote,
        fee_base=0.0,
        fee_quote=0.0,
        fee_usdt=quote - price * amount,
        slippage_usdt=0.0,
    )


def test_t3_replay_receives_tenant_config():
    from core.tenant_context import tenant_context
    from services.ledger_sync import sync_positions_on_startup
    from strategies.positions import save_positions

    symbol = "PX/USDT"
    tenant_cfg = _paper_cfg(tenant_marker="basisb")
    orders = [_order("b1", "buy", symbol, 1.0, 10.0, usdt=10.0, ts="2026-10-01T00:00:00")]
    seen: list[tuple] = []
    real = replay_simulated_ledger

    def spy(rows, initial=5000.0, config=None, tenant_id=None):
        seen.append((config, tenant_id))
        return real(rows, initial, config=config, tenant_id=tenant_id)

    def fake_get_config(tenant_id=None):
        if tenant_id == "basisb":
            return tenant_cfg
        return _paper_cfg()

    with tenant_context("basisb", scope="paper"), patch(
        "core.sim_ledger_replay.replay_simulated_ledger", side_effect=spy
    ), patch("data_manager.get_config", side_effect=fake_get_config), patch(
        "data_manager.load_orders", return_value={"orders": orders}
    ), patch("price_fetcher.get_prices_batch", return_value={}):
        update_position(symbol, TF, "BUY", 1.0, 10.0)
        save_positions()
        from strategies.positions import load_positions

        loaded = load_positions()
        sync_positions_on_startup()

    assert get_key(symbol, TF) in loaded
    assert any(cfg is tenant_cfg and tid == "basisb" for cfg, tid in seen)


def test_t4_fee_unknown_basis_marker_and_no_fee_fetch():
    from services.portfolio_service import PortfolioService
    from strategies.technical_rsi_bb import TechnicalRSIStrategy
    from core.models import MarketContext

    symbol = "FEE/USDT"
    price = 10.0
    amount = 5.0
    warns: list[str] = []

    def _log(msg, level="INFO"):
        if level == "WARNING" and "basis fee estimated" in str(msg):
            warns.append(str(msg))

    cases = [
        ("base", "market", "fee_unknown"),
        ("base", "limit", "fee_unknown"),
        ("quote", "market", "usdt_missing"),
        ("quote", "limit", "fee_unknown"),
    ]
    for side, order_type, trigger in cases:
        clear_positions_memory()
        warns.clear()
        cfg = _fee_cfg(side=side)
        cfg["costs"]["fee_source"] = "auto"
        cfg["trading_mode"] = "live"
        cfg["live_confirmed"] = True
        cfg["live"] = {"dry_run": False, "api_key_env": "GATE_API_KEY", "api_secret_env": "GATE_API_SECRET"}
        model = CostModel.from_config(_fee_cfg(side=side), symbol=symbol)
        expected = model.estimated_buy_entry(price, order_type)
        assert expected > price
        row = _order(
            f"b-{side}-{order_type}",
            "buy",
            symbol,
            price,
            amount,
            ts="2026-10-01T00:00:00",
            order_type=order_type,
            fee_unknown=trigger == "fee_unknown",
            filled_qty_gross=amount,
            omit_usdt=trigger == "usdt_missing",
            usdt=price * amount,
        )
        fetch = MagicMock(return_value=None)
        with patch("core.costs._looks_live", return_value=True), patch(
            "core.costs._try_fetch_exchange_fees", fetch
        ), patch("core.sim_ledger_replay.log", side_effect=_log):
            snap = _reload([row], config=cfg)
        assert fetch.call_count == 0
        assert _lot(snap, symbol)["average_entry"] == pytest.approx(expected)
        assert _lot(snap, symbol).get("basis_fee_estimated") is True
        assert any(trigger in msg and symbol in msg for msg in warns)

        clear_positions_memory()
        book = PortfolioService()
        book.config = SimpleNamespace(raw=cfg, max_usdt_per_trade=50.0)
        fetch.reset_mock()
        with patch("core.costs._looks_live", return_value=True), patch(
            "core.costs._try_fetch_exchange_fees", fetch
        ), patch("services.portfolio_service.log", side_effect=_log):
            bought = book.execute_buy(
                symbol,
                TF,
                price,
                fill=_gross_fill(price, amount),
                fee_unknown=True,
                order_type=order_type,
                sync_virtual_ledger=False,
            )
        assert bought.executed
        assert fetch.call_count == 0
        mem = get_position(symbol, TF)
        assert float(mem["average_entry"]) == pytest.approx(expected)
        assert float(mem["average_entry"]) > 0
        assert mem.get("basis_fee_estimated") is True

    # Sell on an estimated lot, then a stop, then marker lifetime.
    clear_positions_memory()
    cfg = _fee_cfg(side="base")
    book = PortfolioService()
    book.config = SimpleNamespace(raw=cfg, max_usdt_per_trade=50.0)
    book.execute_buy(
        symbol, TF, price, fill=_gross_fill(price, amount),
        fee_unknown=True, order_type="market", sync_virtual_ledger=False,
    )
    recorded = []
    with patch("services.portfolio_service.record_trade", side_effect=lambda row: recorded.append(row)):
        sold = book.execute_sell(
            symbol, TF, price * 0.5, "SELL", 1.0, fill=_sell_fill(price * 0.5, 1.0),
        )
    assert sold.executed
    assert sold.pnl_basis == "fee_estimated"
    assert sold.pnl_fee_source
    assert recorded[0]["pnl_basis"] == "fee_estimated"
    assert recorded[0]["pnl_fee_source"] == sold.pnl_fee_source
    assert float(get_position(symbol, TF)["average_entry"]) > 0

    market = MarketContext(
        symbol=symbol,
        timeframe=TF,
        current_price=price * 0.5,
        rsi=40.0,
        has_position=True,
        average_entry=float(get_position(symbol, TF)["average_entry"]),
        strategy_params={"stop_loss_pct": 5.0, "buy_regime": "dip"},
    )
    analysis = TechnicalRSIStrategy().analyze({"symbol": symbol, "timeframe": TF}, market)
    assert analysis.action == "SELL_STOP_FULL"

    # Full flat clears the marker.
    left = float(get_position(symbol, TF)["amount"])
    book.execute_sell(symbol, TF, price, "SELL_FULL", left, fill=_sell_fill(price, left))
    flat = get_position(symbol, TF)
    assert float(flat["amount"]) == pytest.approx(0.0)
    assert not flat.get("basis_fee_estimated")

    # F7 dust re-entry restores the marker because the dust share is not zero.
    clear_positions_memory()
    book.execute_buy(
        symbol, TF, 1.0, fill=_gross_fill(1.0, 100.0),
        fee_unknown=True, order_type="market", sync_virtual_ledger=False,
    )
    book.execute_sell(symbol, TF, 1.0, "SELL_FULL", 99.995, fill=_sell_fill(1.0, 99.995))
    dust = get_position(symbol, TF)
    assert float(dust["amount"]) == pytest.approx(0.005)
    assert dust.get("basis_fee_estimated") is True
    update_position(symbol, TF, "BUY", 1.2, 50.0, entry_source="sensor_probe")
    again = get_position(symbol, TF)
    assert again.get("basis_fee_estimated") is True
    assert again.get("entry_source") == "sensor_probe"

    # A non-F7 dust re-entry does not keep a marker.
    clear_positions_memory()
    update_position(symbol, TF, "BUY", 1.0, 100.0)
    update_position(symbol, TF, "SELL_FULL", 1.0, 99.995)
    update_position(symbol, TF, "BUY", 1.2, 50.0)
    assert not get_position(symbol, TF).get("basis_fee_estimated")

    empty = CostModel.from_config({})
    assert empty.params is vip0_default
    present = CostModel.from_config(_fee_cfg())
    assert present.params is not vip0_default


def test_t5_daily_loss_uses_net_basis_pnl():
    from core.config import BotConfig
    from risk.risk_manager import RiskManager

    symbol = "PX/USDT"
    # Net basis 100/99.8. Sell quote 99.7 → pnl -0.3. Gross basis would be -0.1.
    net_pnl = 99.7 - 100.0
    gross_pnl = 99.7 - 99.8
    assert net_pnl == pytest.approx(-0.3)
    assert gross_pnl == pytest.approx(-0.1)
    now = datetime.now().isoformat()

    def _sell(pnl: float) -> dict:
        return {
            "id": "s1",
            "status": "filled",
            "side": "sell",
            "symbol": symbol,
            "timeframe": TF,
            "pnl": pnl,
            "execution": {"price": 1.0, "amount": 99.8, "usdt": 99.7},
            "ledger_scope": "demo",
            "timestamps": {"filled": now, "created": now},
        }

    raw = _paper_cfg()
    raw["risk"] = {"max_daily_loss_pct": 0.2, "fail_closed_guards": "log"}
    cfg = BotConfig()
    cfg._raw = raw
    risk = RiskManager(cfg)

    def _blocked(pnl: float):
        from services import order_service as order_svc

        order_svc._ORDERS_READ_CACHE.clear()
        with patch("services.order_service.load_orders", return_value={"orders": [_sell(pnl)]}), patch.object(
            risk, "_portfolio_equity", return_value=100.0
        ), patch.object(risk, "_risk_history_load", return_value={}), patch.object(
            risk, "_risk_history_save"
        ):
            return risk._daily_loss_limit_blocked(
                TradeOrder(type="BUY", symbol=symbol, price=1.0, qty=0, usdt_amount=10)
            )

    assert _blocked(gross_pnl) is None
    net_block = _blocked(net_pnl)
    assert net_block is not None
    assert net_block.code == "daily_loss_limit"
    with patch("services.order_service.load_orders", return_value={"orders": [_sell(net_pnl)]}), patch.object(
        risk, "_portfolio_equity", return_value=100.0
    ), patch.object(risk, "_risk_history_load", return_value={}), patch.object(
        risk, "_risk_history_save"
    ):
        seen = risk._trailing_24h_realized_pnl()
        decision = risk.evaluate(TradeOrder(type="BUY", symbol=symbol, price=1.0, qty=0), TF)
    assert seen == pytest.approx(net_pnl)
    assert decision.approved is False
    assert decision.code == "daily_loss_limit"


def test_t6_partial_sell_then_dca_then_new_cycle():
    symbol = "PX/USDT"
    update_position(symbol, TF, "BUY", 1.0, 100.0)
    assert float(get_position(symbol, TF)["average_entry"]) == pytest.approx(1.0)
    _assert_reload_matches(
        symbol,
        [_order("b1", "buy", symbol, 1.0, 100.0, usdt=100.0, ts="2026-10-01T00:00:00")],
    )
    update_position(symbol, TF, "SELL", 1.0, 50.0)
    assert float(get_position(symbol, TF)["average_entry"]) == pytest.approx(1.0)
    assert float(get_position(symbol, TF)["amount"]) == pytest.approx(50.0)
    _assert_reload_matches(
        symbol,
        [
            _order("b1", "buy", symbol, 1.0, 100.0, usdt=100.0, ts="2026-10-01T00:00:00"),
            _order("s1", "sell", symbol, 1.0, 50.0, usdt=50.0, ts="2026-10-01T01:00:00"),
        ],
    )
    update_position(symbol, TF, "BUY_DCA", 0.8, 50.0, source="dca")
    assert float(get_position(symbol, TF)["average_entry"]) == pytest.approx(0.90)
    assert float(get_position(symbol, TF)["amount"]) == pytest.approx(100.0)
    _assert_reload_matches(
        symbol,
        [
            _order("b1", "buy", symbol, 1.0, 100.0, usdt=100.0, ts="2026-10-01T00:00:00"),
            _order("s1", "sell", symbol, 1.0, 50.0, usdt=50.0, ts="2026-10-01T01:00:00"),
            _order("b2", "buy", symbol, 0.8, 50.0, usdt=40.0, ts="2026-10-01T02:00:00", signal="BUY_DCA"),
        ],
    )
    update_position(symbol, TF, "SELL_FULL", 0.9, 100.0)
    assert float(get_position(symbol, TF)["amount"]) == pytest.approx(0.0)
    update_position(symbol, TF, "BUY", 3.0, 10.0)
    assert float(get_position(symbol, TF)["average_entry"]) == pytest.approx(3.0)
    assert float(get_position(symbol, TF)["amount"]) == pytest.approx(10.0)


def _assert_reload_matches(symbol: str, orders: list) -> None:
    mem_entry = float(get_position(symbol, TF)["average_entry"])
    mem_amt = float(get_position(symbol, TF)["amount"])
    snap = _reload(orders)
    if mem_amt <= DUST_AMOUNT_EPSILON:
        assert get_key(symbol, TF) not in snap
        return
    got = _lot(snap, symbol)
    assert float(got["amount"]) == pytest.approx(mem_amt)
    assert float(got["average_entry"]) == pytest.approx(mem_entry)


def test_t7_mtm_uses_average_entry_nav_cash_unchanged():
    from notifications.telegram_commands.position_display import _position_metrics

    symbol = "NAV/USDT"
    buy = _order(
        "b1", "buy", symbol, 1.0, 99.8, usdt=100.0, ts="2026-10-01T00:00:00",
        filled_qty_gross=100.0,
    )
    snap = _reload([buy])
    pos = _lot(snap, symbol)
    price = 1.5
    metrics = _position_metrics(pos, price)
    assert metrics["unreal"] == pytest.approx(float(pos["amount"]) * (price - float(pos["average_entry"])))
    cash = INITIAL - 100.0
    market = price * float(pos["amount"])
    nav = reconcile_display_nav(
        cash, INITIAL, market, trade_realized=0.0, open_lots_mtm=metrics["unreal"],
        positions_cost_basis=float(pos["amount"]) * float(pos["average_entry"]),
    )
    assert nav["reconciled"] is False
    assert nav["total_value"] == pytest.approx(cash + market)


def test_t8a_sell_full_dust_reentry_resets_cycle_fields():
    symbol = "PX/USDT"
    update_position(
        symbol, TF, "BUY", 1.0, 100.0,
        entry_source="old_source", entry_15m_vol_ratio=1.5,
    )
    update_position(symbol, TF, "SELL_FULL", 1.0, 99.995)
    dust = get_position(symbol, TF)
    assert float(dust["amount"]) == pytest.approx(0.005)
    assert float(dust["average_entry"]) == pytest.approx(1.0)
    assert float(dust["sold_percent"]) == pytest.approx(1.0)
    orders = [
        _order("b1", "buy", symbol, 1.0, 100.0, usdt=100.0, ts="2026-10-01T00:00:00"),
        _order("s1", "sell", symbol, 1.0, 99.995, usdt=99.995, ts="2026-10-01T01:00:00"),
    ]
    cache = {
        "positions": {
            get_key(symbol, TF): {
                "amount": 0.005,
                "average_entry": 1.0,
                "sold_percent": 1.0,
                "first_buy_at": "2026-10-01T00:00:00",
                "entry_source": "old_source",
                "entry_at": "2026-10-01T00:00:00",
                "entry_15m_vol_ratio": 1.5,
                "last_rsi": 44.0,
            }
        }
    }
    reloaded = _reload(orders, cache)
    kept = _lot(reloaded, symbol)
    assert float(kept["amount"]) == pytest.approx(0.005)
    assert float(kept["average_entry"]) == pytest.approx(1.0)
    assert float(kept["sold_percent"]) == pytest.approx(1.0)
    assert kept["last_rsi"] == pytest.approx(44.0)

    update_position(
        symbol, TF, "BUY", 1.2, 50.0,
        entry_source="new_source", entry_15m_vol_ratio=2.5,
    )
    mem = get_position(symbol, TF)
    assert float(mem["amount"]) == pytest.approx(50.005)
    assert float(mem["average_entry"]) == pytest.approx(60.005 / 50.005)
    assert mem.get("entry_source") == "new_source"
    assert mem.get("entry_15m_vol_ratio") == pytest.approx(2.5)
    orders.append(
        _order("b2", "buy", symbol, 1.2, 50.0, usdt=60.0, ts="2026-10-01T02:00:00")
    )
    again = _reload(orders)
    assert float(_lot(again, symbol)["amount"]) == pytest.approx(float(mem["amount"]))
    assert float(_lot(again, symbol)["average_entry"]) == pytest.approx(float(mem["average_entry"]))


def test_t8b_non_full_sell_hard_clear_keeps_dust():
    from services.order_service import OrderService
    from services.trading_service import TradingService

    symbol = "PX/USDT"
    update_position(symbol, TF, "BUY", 1.0, 100.0)
    update_position(symbol, TF, "SELL", 1.0, 50.0)
    svc = TradingService()
    order = TradeOrder(
        type="SELL", symbol=symbol, price=1.0, qty=49.996, usdt_amount=49.996, signal="SELL",
    )
    decision = RiskDecision(approved=True, message="ok", order=order)

    def _exec(approved, _tf):
        return svc.portfolio.execute_sell(
            approved.symbol, _tf, approved.price, approved.signal or "SELL", approved.qty,
            fill=_sell_fill(approved.price, approved.qty),
            sync_virtual_ledger=False,
        )

    fake_adapter = SimpleNamespace(mode="paper", execute=_exec)
    with patch("bus.writer_lease.require_lease_for_order"), patch.object(
        svc, "can_execute", return_value=(True, "")
    ), patch.object(svc.risk, "evaluate", return_value=decision), patch.object(
        type(svc), "adapter", new=property(lambda _self: fake_adapter)
    ), patch.object(OrderService, "create_from_request", return_value={"id": "oid-b"}), patch.object(
        OrderService, "link_execution_result"
    ), patch.object(OrderService, "update_status"), patch.object(
        svc, "_maybe_auto_short_after_sell", return_value=None
    ), patch.object(svc, "_record_positions_snapshot"):
        result = svc._execute_order_locked(order, TF, source="auto")
    assert result.executed
    pos = get_position(symbol, TF)
    assert float(pos["amount"]) == pytest.approx(0.004)
    assert float(pos["average_entry"]) == pytest.approx(1.0)
    assert float(pos["sold_percent"]) == pytest.approx(1.0)
    orders = [
        _order("b1", "buy", symbol, 1.0, 100.0, usdt=100.0, ts="2026-10-01T00:00:00"),
        _order("s1", "sell", symbol, 1.0, 50.0, usdt=50.0, ts="2026-10-01T01:00:00"),
        _order("s2", "sell", symbol, 1.0, 49.996, usdt=49.996, ts="2026-10-01T02:00:00"),
    ]
    snap = _reload(orders)
    assert float(_lot(snap, symbol)["amount"]) == pytest.approx(0.004)
    assert float(_lot(snap, symbol)["average_entry"]) == pytest.approx(1.0)
    update_position(symbol, TF, "BUY", 1.5, 20.0)
    assert float(get_position(symbol, TF)["average_entry"]) == pytest.approx(30.004 / 20.004)


def test_t8c_short_on_dust_replaces_lot():
    from services.portfolio_service import PortfolioService

    symbol = "SH/USDT"
    update_position(symbol, TF, "BUY", 1.0, 10.0)
    update_position(symbol, TF, "SELL_FULL", 1.0, 9.999)
    dust = get_position(symbol, TF)
    assert not is_open_position(dust)
    assert float(dust["amount"]) > DUST_AMOUNT_EPSILON

    opened = PortfolioService().execute_short(
        symbol, TF, 2.0, usdt_amount=20.0, leverage=2.0, sync_virtual_ledger=False,
    )
    assert opened.executed
    assert "one-way" not in (opened.message or "")
    mem = get_position(symbol, TF)
    assert str(mem.get("side")) == "short"
    assert float(mem["amount"]) == pytest.approx(10.0)
    assert float(mem["average_entry"]) == pytest.approx(2.0)
    assert float(mem.get("leverage") or 0) == pytest.approx(2.0)

    from execution.gate_adapter import GateExecutionAdapter

    clear_positions_memory()
    update_position(symbol, TF, "BUY", 1.0, 10.0)
    update_position(symbol, TF, "SELL_FULL", 1.0, 9.999)
    adapter = GateExecutionAdapter.__new__(GateExecutionAdapter)
    adapter._clamp_short_leverage = lambda _order: (2.0, None)
    adapter._fetch_usdt_balance = lambda: 1_000_000.0
    adapter._shadow_adjust_amount = lambda _ex, _sym, qty: (qty, True)
    adapter._synthesize_shadow_raw = lambda *_a, **_k: {"id": "shadow"}
    adapter._finalize_exchange_order = lambda *_a, **_k: TradeResult(True, "SHORT", symbol)
    adapter._precision_unverified = False
    shadow = adapter._execute_short_shadow(
        None,
        TradeOrder(type="SHORT", symbol=symbol, price=2.0, qty=0, usdt_amount=20.0, leverage=2.0),
        TF,
    )
    assert shadow.executed
    assert "one-way" not in (shadow.message or "")

    clear_positions_memory()
    update_position(symbol, TF, "BUY", 1.0, 10.0)
    update_position(symbol, TF, "SELL_FULL", 1.0, 9.999)
    update_position(symbol, TF, "SHORT", 2.0, 10.0, leverage=2.0)
    mem = get_position(symbol, TF)
    orders = [
        _order("b1", "buy", symbol, 1.0, 10.0, usdt=10.0, ts="2026-10-01T00:00:00"),
        _order("s1", "sell", symbol, 1.0, 9.999, usdt=9.999, ts="2026-10-01T01:00:00"),
        _order("h1", "short", symbol, 2.0, 10.0, usdt=20.0, ts="2026-10-01T02:00:00", leverage=2.0),
    ]
    snap = _lot(_reload(orders), symbol)
    for field in ("amount", "average_entry", "side", "leverage"):
        assert snap.get(field) == mem.get(field) or float(snap.get(field)) == pytest.approx(float(mem.get(field)))


def test_t8d_batch_attribute_keeps_dust_and_marks_sold():
    from services.exit_realtime.execute import _attribute_acked_batch_sell
    from services.portfolio_service import PortfolioService

    symbol = "PX/USDT"
    update_position(symbol, TF, "BUY", 1.0, 100.0)

    @contextmanager
    def _lock(*_a, **_k):
        yield None

    trading = SimpleNamespace(config=SimpleNamespace(raw=_paper_cfg(), trading_mode="paper"))
    with patch("bus.locks.ledger_lock", _lock), patch(
        "services.exit_realtime.execute._portfolio_for_batch", return_value=PortfolioService()
    ), patch("services.exit_realtime.execute._record_batch_fill"), patch(
        "data_manager.uses_exchange_ledger", return_value=False
    ):
        ok = _attribute_acked_batch_sell(
            trading,
            symbol=symbol,
            timeframe=TF,
            price=1.0,
            amount=99.995,
            requested=100.0,
            ack={"id": "ack-1"},
            text="t8d",
            source="liq_cascade",
            action="SELL",
        )
    assert ok
    pos = get_position(symbol, TF)
    assert float(pos["amount"]) == pytest.approx(0.005)
    assert float(pos["average_entry"]) == pytest.approx(1.0)
    assert float(pos["sold_percent"]) == pytest.approx(1.0)


def test_t8e_dust_key_takes_the_same_buy_as_a_flat_key():
    from data.lunarcrush_scorer import LunarCrushSignal
    from strategies.decision_engine import DecisionEngine

    symbol = "PX/USDT"
    engine = DecisionEngine()
    lc = LunarCrushSignal("PX", "BUY", 82, rationale="fixture", galaxy_score=74, alt_rank=45, sentiment=76)
    lc.trust_score = 72.0
    lc.effective_confidence = 59.0
    indicators = {"rsi": 50.0, "lower_bb": 0.9, "middle_bb": 1.0, "upper_bb": 1.1, "vol_multiplier": 1.0, "atr_pct": 3.0}

    def _eval():
        with patch.object(engine.market, "fetch_ohlcv_and_indicators", return_value=(None, indicators)), patch.object(
            engine.market, "fetch_indicators", return_value=indicators
        ), patch("strategies.decision_engine.count_open_positions", return_value=0), patch(
            "price_fetcher.get_gate_prices_batch", side_effect=lambda syms: {s: 1.0 for s in syms}
        ), patch("price_fetcher.get_ticker_price", return_value=1.0), patch(
            "price_fetcher.get_prices_batch", side_effect=lambda syms: {s: 1.0 for s in syms}
        ):
            ctx = engine.build_market_context({"symbol": symbol, "timeframe": TF}, 1.0)
            analysis = engine.evaluate({"symbol": symbol, "timeframe": TF}, 1.0, lc_signals=[lc])
        return ctx, analysis

    clear_positions_memory()
    flat_ctx, flat = _eval()
    update_position(symbol, TF, "BUY", 1.0, 10.0)
    update_position(symbol, TF, "SELL_FULL", 1.0, 9.999)
    assert not is_open_position(get_position(symbol, TF))
    dust_ctx, dust = _eval()
    assert flat_ctx.has_position is False
    assert dust_ctx.has_position is False
    assert flat.normalized_action in ("BUY", "BUY_STRONG")
    assert dust.normalized_action == flat.normalized_action
    assert dust.action == flat.action


def test_t8f_dust_buy_is_a_new_entry():
    from core.config import BotConfig
    from data_manager import get_config
    from risk.risk_manager import RiskManager

    symbol = "PX/USDT"
    outside = "OUT/USDT"
    update_position(symbol, TF, "BUY", 1.0, 10.0)
    update_position(symbol, TF, "SELL_FULL", 1.0, 9.999)
    update_position(outside, TF, "BUY", 1.0, 10.0)
    update_position(outside, TF, "SELL_FULL", 1.0, 9.999)
    assert not is_open_position(get_position(symbol, TF))

    raw = dict(get_config())
    raw["max_open_positions"] = 1
    risk_cfg = dict(raw.get("risk") or {})
    risk_cfg["position_capacity"] = {"enabled": False}
    risk_cfg["slot_eviction"] = {"enabled": False}
    risk_cfg["fail_closed_guards"] = "log"
    raw["risk"] = risk_cfg
    raw["universe"] = {"split_enabled": True}
    cfg = BotConfig(raw)
    risk = RiskManager(cfg)

    with patch.object(risk.market, "fetch_indicators", return_value={"atr_pct": 3.0}), patch(
        "risk.risk_manager.count_open_full_slots", return_value=1
    ), patch("risk.risk_manager.count_open_positions", return_value=1), patch(
        "services.universe.split.is_trade_eligible", return_value=True
    ), patch.object(risk, "_trade_cooldown_blocked", return_value=(False, "")):
        capped = risk.evaluate(
            TradeOrder(type="BUY", symbol=symbol, price=1.0, qty=0, source="auto"),
            TF,
            source="auto",
            indicators={"atr_pct": 3.0},
        )
    assert capped.approved is False
    assert capped.code == "max_open_positions"

    with patch.object(risk.market, "fetch_indicators", return_value={"atr_pct": 3.0}), patch(
        "risk.risk_manager.count_open_full_slots", return_value=0
    ), patch("services.universe.split.is_trade_eligible", return_value=False), patch(
        "services.universe.split.universe_split_enabled", return_value=True
    ), patch.object(risk, "_trade_cooldown_blocked", return_value=(False, "")):
        universe = risk.evaluate(
            TradeOrder(type="BUY", symbol=outside, price=1.0, qty=0, source="auto"),
            TF,
            source="auto",
            indicators={"atr_pct": 3.0},
        )
    assert universe.approved is False
    assert universe.code == "universe_trade_cap"


def test_t8g_legacy_zero_cache_and_missing_cache():
    symbol = "PX/USDT"
    orders = [
        _order("b1", "buy", symbol, 1.0, 100.0, usdt=100.0, ts="2026-10-01T00:00:00"),
        _order("s1", "sell", symbol, 1.0, 99.995, usdt=99.995, ts="2026-10-01T01:00:00"),
    ]
    replay = replay_simulated_ledger(orders, INITIAL, config=_paper_cfg())
    key = get_key(symbol, TF)
    dust = replay["positions"][key]
    cache = {
        "positions": {
            key: {
                "amount": 0.0,
                "average_entry": 9.0,
                "sold_percent": 1.0,
                "first_buy_at": "2026-10-01T00:00:00",
                "last_rsi": 33.0,
                "entry_source": "legacy",
            }
        }
    }
    merged = derive_positions_from_orders_and_cache(replay["positions"], cache)
    assert float(merged[key]["amount"]) == pytest.approx(float(dust["amount"]))
    assert float(merged[key]["average_entry"]) == pytest.approx(float(dust["average_entry"]))
    assert merged[key]["last_rsi"] == pytest.approx(33.0)
    assert merged[key]["entry_source"] == "legacy"

    loaded = _reload(orders, cache)
    assert float(_lot(loaded, symbol)["amount"]) == pytest.approx(0.005)
    assert float(_lot(loaded, symbol)["average_entry"]) == pytest.approx(1.0)
    assert _lot(loaded, symbol)["last_rsi"] == pytest.approx(33.0)

    update_position(symbol, TF, "BUY", 1.2, 50.0)
    mem_amt = float(get_position(symbol, TF)["amount"])
    mem_entry = float(get_position(symbol, TF)["average_entry"])
    orders.append(_order("b2", "buy", symbol, 1.2, 50.0, usdt=60.0, ts="2026-10-01T02:00:00"))
    restarted = _reload(orders)
    assert float(_lot(restarted, symbol)["amount"]) == pytest.approx(mem_amt)
    assert float(_lot(restarted, symbol)["average_entry"]) == pytest.approx(mem_entry)

    # No cache key: replay dust comes back, and the re-entry matches after restart.
    clear_positions_memory()
    bare = [
        _order("b1", "buy", symbol, 1.0, 100.0, usdt=100.0, ts="2026-10-01T00:00:00"),
        _order("s1", "sell", symbol, 1.0, 99.995, usdt=99.995, ts="2026-10-01T01:00:00"),
    ]
    first = _reload(bare, {"positions": {}})
    assert float(_lot(first, symbol)["amount"]) == pytest.approx(0.005)
    update_position(symbol, TF, "BUY", 1.2, 50.0)
    mem_amt = float(get_position(symbol, TF)["amount"])
    mem_entry = float(get_position(symbol, TF)["average_entry"])
    bare.append(_order("b2", "buy", symbol, 1.2, 50.0, usdt=60.0, ts="2026-10-01T02:00:00"))
    second = _reload(bare, {"positions": {}})
    assert float(_lot(second, symbol)["amount"]) == pytest.approx(mem_amt)
    assert float(_lot(second, symbol)["average_entry"]) == pytest.approx(mem_entry)


def test_t8h_small_ticket_survives_reload_and_sell_credits_cash():
    symbol = "PX/USDT"
    price = 60_000.0
    ticket = 25.0
    amount = ticket / price
    assert amount < 0.01
    buy = _order("b1", "buy", symbol, price, amount, usdt=ticket, ts="2026-10-01T00:00:00")
    replay = replay_simulated_ledger([buy], INITIAL, config=_paper_cfg())
    pos = replay["positions"][get_key(symbol, TF)]
    assert float(pos["amount"]) == pytest.approx(amount)
    assert float(pos["average_entry"]) == pytest.approx(price)
    loaded = _reload([buy], {"positions": {}})
    assert float(_lot(loaded, symbol)["amount"]) == pytest.approx(amount)
    assert float(_lot(loaded, symbol)["average_entry"]) == pytest.approx(price)

    sell_usdt = 24.5
    sold = _order("s1", "sell", symbol, price, amount, usdt=sell_usdt, ts="2026-10-01T01:00:00")
    after = replay_simulated_ledger([buy, sold], INITIAL, config=_paper_cfg())
    assert get_key(symbol, TF) not in after["positions"]
    assert after["cash"] == pytest.approx(INITIAL - ticket + sell_usdt)
    reloaded = _reload([buy, sold], {"positions": {}})
    assert get_key(symbol, TF) not in reloaded

    clear_positions_memory()
    update_position(symbol, TF, "BUY", price, amount)
    update_position(symbol, TF, "SELL_FULL", price, amount)
    assert float(get_position(symbol, TF)["amount"]) == pytest.approx(0.0)


def test_t9_long_profit_counts_buy_fee_once_short_keeps_round_trip():
    from services.exit_realtime.execute import (
        _execute_long_cascade_batch,
        _lot_in_profit,
        execute_cascade_exit,
    )

    entry = 100.0
    amount = 10.0
    taker = 0.002
    slip = 0.0015
    threshold = entry / ((1.0 - slip) * (1.0 - taker))
    above = threshold + 0.05
    between = (entry + threshold) / 2.0
    assert entry < between < threshold < above
    raw = _fee_cfg(side="base", maker=0.10, taker=0.20)
    long_lot = {
        "symbol": "LOT/USDT",
        "timeframe": TF,
        "amount": amount,
        "average_entry": entry,
        "side": "long",
    }
    assert _lot_in_profit({**long_lot, "symbol": "LOT/USDT"}, above, raw) is True
    assert _lot_in_profit({**long_lot, "symbol": "LOT/USDT"}, between, raw) is False

    def _run_long(price: float, fn):
        import services.exit_realtime.execute as ex

        with ex._inflight_lock:
            ex._inflight.discard("LOT/USDT")
            ex._last_exit_at.pop("LOT/USDT", None)
        trading = MagicMock()
        trading.execute_order.return_value = SimpleNamespace(executed=True, message="ok")
        lot = {**long_lot, "current_price": price}
        with patch("strategies.positions.get_position", return_value=dict(lot)), patch(
            "strategies.positions.is_open_position", return_value=True
        ), patch(
            "strategies.position_lock.attach_lock_from_ledger", side_effect=lambda pos, *a, **k: pos
        ):
            if fn == "cascade":
                out = execute_cascade_exit(
                    side="long",
                    lots=[lot],
                    prices={"LOT/USDT": price},
                    trading=trading,
                    fire_enabled=True,
                    raw_config=raw,
                )
            else:
                out = _execute_long_cascade_batch(
                    side_key="long",
                    action="SELL_FULL",
                    lots=[lot],
                    prices={"LOT/USDT": price},
                    trading=trading,
                    raw_config=raw,
                    mono=1.0,
                    state=None,
                    source="liq_cascade",
                )
        return out

    high = _run_long(above, "cascade")
    low = _run_long(between, "cascade")
    assert high["executed"] is True
    assert any(r.get("message") == "not_in_profit" for r in low["results"])
    batch_high = _run_long(above, "batch")
    batch_low = _run_long(between, "batch")
    assert not any(
        r.get("code") == "not_in_profit" or r.get("message") == "not_in_profit"
        for r in batch_high["results"]
    )
    assert any(
        r.get("code") == "not_in_profit" or r.get("message") == "not_in_profit"
        for r in batch_low["results"]
    )

    short_entry = 100.0
    short_px = 99.0
    gain = (short_entry - short_px) / short_entry * 100.0
    rt = CostModel.from_config(raw).round_trip_pct()
    expect = (gain - rt) > 0.0
    short_lot = {
        "symbol": "SH/USDT",
        "timeframe": TF,
        "amount": 10.0,
        "average_entry": short_entry,
        "side": "short",
        "current_price": short_px,
    }
    assert _lot_in_profit(short_lot, short_px, raw) is expect
    import services.exit_realtime.execute as ex

    with ex._inflight_lock:
        ex._inflight.discard("SH/USDT")
        ex._last_exit_at.pop("SH/USDT", None)
    trading = MagicMock()
    trading.execute_order.return_value = SimpleNamespace(executed=True, message="ok")
    with patch("strategies.positions.get_position", return_value=dict(short_lot)), patch(
        "strategies.positions.is_open_position", return_value=True
    ), patch(
        "strategies.position_lock.attach_lock_from_ledger", side_effect=lambda pos, *a, **k: pos
    ):
        out = execute_cascade_exit(
            side="short",
            lots=[short_lot],
            prices={"SH/USDT": short_px},
            trading=trading,
            fire_enabled=True,
            raw_config=raw,
        )
    assert out["executed"] is expect


def test_t10_short_basis_and_cover_stay_gross():
    symbol = "SH/USDT"
    orders = [
        _order("h1", "short", symbol, 2.0, 10.0, usdt=20.0, ts="2026-10-01T00:00:00", leverage=2.0),
        _order("c1", "cover", symbol, 1.5, 4.0, usdt=6.0, ts="2026-10-01T01:00:00"),
    ]
    replay = replay_simulated_ledger(orders, INITIAL, config=_paper_cfg())
    pos = replay["positions"][get_key(symbol, TF)]
    assert float(pos["average_entry"]) == pytest.approx(2.0)
    assert float(pos["amount"]) == pytest.approx(6.0)
    assert replay["realized_pnl"] == pytest.approx(4.0 * (2.0 - 1.5))


def test_t11_fee_unknown_sell_basis():
    from services.portfolio_service import PortfolioService

    book = PortfolioService()
    # Marker set, then a fee-unknown sell wins over fee_estimated.
    update_position("FEE2/USDT", TF, "BUY", 1.0, 10.0, fee_estimated=True)
    recorded = []
    with patch("services.portfolio_service.record_trade", side_effect=lambda row: recorded.append(row)):
        result = book.execute_sell(
            "FEE2/USDT", TF, 1.1, "SELL", 4.0, fee_unknown=True, fill=None,
        )
    assert result.executed
    assert result.pnl_basis == "sell_fee_unknown"
    assert recorded[0]["pnl_basis"] == "sell_fee_unknown"
    assert result.fee == pytest.approx(0.0)


def test_n10_dust_is_not_counted_as_open(tmp_path):
    from notifications.daily_portfolio import _position_value_from_snapshot
    from notifications.daily_stats import open_positions_summary
    from strategies.buy_decision_tape import emit_buy_decision_tape
    from strategies.dca_scheduled import collect_open_position_symbols

    dust = {"amount": 0.005, "average_entry": 1.0}
    material = {"amount": 10.0, "average_entry": 2.0}
    assert _position_value_from_snapshot(
        {"PX_USDT_4h": dust, "LOT_USDT_4h": material},
        {"PX/USDT": 3.0, "LOT/USDT": 4.0},
    ) == pytest.approx(40.0)

    root = tmp_path / "stats_root"
    data = root / "data"
    data.mkdir(parents=True)
    (data / "positions.json").write_text(
        '{"positions": {"PX_USDT_4h": {"amount": 0.005, "average_entry": 1.0},'
        ' "LOT_USDT_4h": {"amount": 10, "average_entry": 2}}}'
    )
    with patch(
        "strategies.positions.list_active_positions_from_ledger",
        side_effect=RuntimeError("ledger down"),
    ):
        count, total = open_positions_summary(root)
    assert count == 1
    assert total == pytest.approx(20.0)

    def _get(symbol, _tf):
        return dust if symbol.startswith("PX") else material

    assert collect_open_position_symbols(
        [{"symbol": "PX/USDT"}, {"symbol": "LOT/USDT"}],
        get_position_fn=_get,
        resolve_coin_config_fn=lambda coin: {"timeframe": TF},
    ) == ["LOT/USDT"]

    rows = []
    with patch(
        "strategies.buy_decision_tape._lookup_position", return_value=dust
    ), patch("strategies.buy_decision_tape._file_write_allowed", return_value=True), patch(
        "services.observability_store.append_jsonl", side_effect=lambda _p, row: rows.append(row)
    ), patch("services.observability_store.maybe_rotate_jsonl"):
        emit_buy_decision_tape(
            TradeOrder(type="BUY", symbol="PX/USDT", price=1.0, qty=0),
            RiskDecision(approved=True, message="ok"),
            config={"buy_decision_tape": {"enabled": True}},
        )
    assert rows[0]["book"]["state"] == "empty"


def test_n10_audit_and_entry_sensor_ignore_dust():
    from services.audit_trail import AuditTrail
    from services.signal_orchestrator import SignalOrchestrator

    symbol = "PX/USDT"
    update_position(symbol, TF, "BUY", 1.0, 10.0)
    update_position(symbol, TF, "SELL_FULL", 1.0, 9.999)
    captured = []
    analysis = SimpleNamespace(
        symbol=symbol,
        timeframe=TF,
        action="HOLD",
        normalized_action="HOLD",
        confidence=0,
        sources=[],
        rationale="",
        rsi=45.0,
        vol_multiplier=1.0,
        atr_pct=3.0,
        volatility_tier="",
        strategy_profile="",
        shadow_action="",
        sell_policy_audit=None,
    )
    with patch("services.observability_store.persist_decision", side_effect=captured.append), patch(
        "services.audit_trail.log_decision"
    ):
        AuditTrail().record({"symbol": symbol}, analysis, price=1.0)
    assert captured[0]["has_position"] is False

    orch = SignalOrchestrator()
    hold = SimpleNamespace(
        action="HOLD",
        normalized_action="HOLD",
        timeframe=TF,
        sources=[],
        rationale="",
        confidence=0,
        rsi=45.0,
        should_notify=False,
    )
    with patch.object(orch, "analyze", return_value=hold), patch.object(orch.audit, "record"):
        out = orch.process_entry_sensor({"symbol": symbol, "timeframe": TF}, 1.0, quiet=True)
    assert out["has_position"] is False


def test_k14_partial_sell_gate_sees_dust_as_closed():
    from risk.risk_manager import RiskManager

    symbol = "PX/USDT"
    update_position(symbol, TF, "BUY", 1.0, 10.0)
    update_position(symbol, TF, "SELL_FULL", 1.0, 9.999)
    seen = []

    def _spy(coin, has_position=False, frozen_tier=None, **kwargs):
        seen.append(has_position)
        return {}

    risk = RiskManager()
    order = TradeOrder(type="SELL", symbol=symbol, price=1.0, qty=1.0, signal="SELL_30")
    with patch("strategies.registry.resolve_strategy_params", side_effect=_spy):
        risk._partial_sell_blocked(order, TF, "auto")
    assert seen == [False]
