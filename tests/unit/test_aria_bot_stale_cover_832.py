"""#832: the aria_bot queue-off loop carries the stale-price flag (#826 / #809).

Drives the production caller ``aria_bot._run_tenant_price_cycle`` with
``architecture.eval_queue_enabled`` off and a real
``SignalOrchestrator.process_coin`` (analysis stubbed, order sink faked).
``audit.record`` runs right after the short-cover step and stops the cycle,
so nothing after the cover decision runs. Synthetic symbol, no network, no
ledger writes.

Contract (frozen #826 behaviour table, #809): a sandbox-tagged short at a
``stale`` cycle quote gets the #809 unpriced cover, the same decision as the
queue-on path (``process_eval_job``). The "deferred" wording of the #832
issue text is superseded by that contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from strategies.short_cover import evaluate_short_cover

SYM = "AAA/USDT"
_BERLIN = ZoneInfo("Europe/Berlin")
OPENED = datetime(2026, 11, 24, 17, 0, tzinfo=_BERLIN)
INSIDE = datetime(2026, 11, 24, 18, 0, tzinfo=_BERLIN)
MARK = 100.2  # inside the hold: no stop, no trail, no RSI cover
STOP_PX = 110.0  # above the short stop: an untagged lot covers on price


class _StopAfterCover(Exception):
    pass


class _Config(dict):
    def get(self, key, default=None):
        if key == "update_interval":
            raise AssertionError("cycle must stop at the short-cover step")
        return super().get(key, default)


class _BotConfig:
    terminal_dashboard_enabled = False

    def __init__(self, raw):
        self.raw = raw

    def refresh(self):
        return None


class _Trading:
    def __init__(self):
        self.orders = []

    def execute_order(self, order, tf, source="auto"):
        self.orders.append(order)
        return {"executed": True}


class _Audit:
    def record(self, *a, **k):
        raise _StopAfterCover


def _raw(*, flag=True):
    return {
        "trading_mode": "paper",
        "architecture": {"eval_queue_enabled": False},
        "shorts": {
            "enabled": True,
            "allow_live": False,
            "sandbox_expiry_v220": {
                "enabled": flag,
                "execution_budget_seconds": 0,
                "reconcile_budget_seconds": 0,
            },
            "testnet_futures": {"paper_start": {}},
        },
    }


def _lot(*, tagged=True, **extra):
    lot = {
        "side": "short",
        "amount": 1.0,
        "average_entry": 100.0,
        "leverage": 2,
        "strategy_tier": "volatile",
        "entry_at": OPENED.isoformat(),
        "symbol": SYM,
        "timeframe": "1h",
        "recent_low": MARK,
    }
    if tagged:
        lot["sandbox_path"] = "v220_paper"
    lot.update(extra)
    return lot


@pytest.fixture
def clock(monkeypatch):
    class _Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = INSIDE.astimezone(timezone.utc)
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    monkeypatch.setattr("strategies.short_cover.datetime", _Fixed)


def _orchestrator(raw):
    from services.signal_orchestrator import SignalOrchestrator

    orch = SignalOrchestrator.__new__(SignalOrchestrator)
    orch.config = type("Cfg", (), {"raw": raw})()
    orch.trading = _Trading()
    orch.audit = _Audit()
    orch.analyze = lambda *a, **k: type(
        "Analysis", (), {"symbol": SYM, "timeframe": "1h", "rsi": 50.0}
    )()
    return orch


def _cycle(monkeypatch, *, lot, source, price=MARK, raw=None):
    """Run one queue-off cycle; return (orders, process_coin kwargs)."""
    import aria_bot

    raw = raw or _raw()
    coins = [{"symbol": SYM, "timeframe": "1h", "active": True}]
    orch = _orchestrator(raw)
    seen: list[dict] = []
    real_process_coin = orch.process_coin

    def _spy(*args, **kwargs):
        seen.append(dict(kwargs))
        return real_process_coin(*args, **kwargs)

    orch.process_coin = _spy
    monkeypatch.setattr("services.architecture_runtime.ensure_started", lambda *a, **k: None)
    monkeypatch.setattr("strategies.positions.is_positions_state_unknown", lambda *a, **k: False)
    monkeypatch.setattr("strategies.positions.list_active_positions", lambda *a, **k: [])
    monkeypatch.setattr("strategies.positions.set_position_field", lambda *a, **k: None)
    monkeypatch.setattr("core.tenant_context.multi_tenant_enabled", lambda: False)
    monkeypatch.setattr("services.universe.source_tags.publish_cycle_tags", lambda *a, **k: None)
    monkeypatch.setattr("services.ledger_sync.reconcile_recent_highs", lambda *a, **k: None)
    monkeypatch.setattr(
        "services.gainer_universe.runtime.maybe_refresh_gainer_universe", lambda *a, **k: None
    )
    monkeypatch.setattr("bus.eval_queue.eval_queue_enabled", lambda raw: False)
    monkeypatch.setattr(
        "services.signal_orchestrator.find_open_position_for_symbol",
        lambda symbol, preferred_timeframe=None: ("1h", dict(lot)),
    )
    monkeypatch.setattr("strategies.climax_fade.last_closed_4h_bar", lambda *a, **k: None)
    monkeypatch.setattr(aria_bot, "get_bot_config", lambda: _BotConfig(raw))
    monkeypatch.setattr(aria_bot, "get_config", lambda: _Config(raw))
    monkeypatch.setattr(aria_bot, "load_effective_watchlist", lambda *a, **k: list(coins))
    monkeypatch.setattr(aria_bot, "load_trade_watchlist", lambda *a, **k: list(coins))
    monkeypatch.setattr(aria_bot, "log", lambda *a, **k: None)

    def _batch(symbols, fallbacks=None, *, return_sources=False, **k):
        prices = {SYM: price}
        if return_sources:
            return prices, ({SYM: source} if source is not None else {})
        return prices

    monkeypatch.setattr(aria_bot, "get_prices_batch", _batch)
    with pytest.raises(_StopAfterCover):
        aria_bot._run_tenant_price_cycle(
            cycle_started=0.0, use_dashboard=False,
            analyzer=None, orchestrator=orch, social_pipeline=None, sandbox=None, trend_engine=None,
        )
    return orch.trading.orders, seen


def test_tagged_short_at_stale_quote_gets_the_809_unpriced_cover(clock, monkeypatch):
    orders, seen = _cycle(monkeypatch, lot=_lot(), source="stale")
    assert seen == [{"quiet": False, "price_source": "stale"}]
    assert len(orders) == 1
    assert orders[0].type == "COVER"
    assert orders[0].exit_source == "time_cap"
    assert orders[0].exit_rationale.startswith("price stale")


def test_queue_off_decision_equals_the_queue_on_decision(clock, monkeypatch):
    """Same decision as process_eval_job's path: evaluate_short_cover(price_stale=True)."""
    from strategies.short_cover_wiring import sandbox_cover_inputs

    raw = _raw()
    expected = evaluate_short_cover(
        _lot(), MARK, symbol=SYM, config_raw=raw,
        **sandbox_cover_inputs(_lot(), raw, price_stale=True),
    )
    orders, _ = _cycle(monkeypatch, lot=_lot(), source="stale", raw=raw)
    assert expected is not None
    assert [(o.exit_source, o.exit_rationale) for o in orders] == [
        (str(expected["source"]), str(expected["rationale"])[:240])
    ]


@pytest.mark.parametrize("source", ["live", "redis", None])
def test_tagged_short_at_fresh_quote_holds_inside_the_window(clock, monkeypatch, source):
    orders, seen = _cycle(monkeypatch, lot=_lot(), source=source)
    assert orders == []
    expected_kw = {"quiet": False} if source is None else {"quiet": False, "price_source": source}
    assert seen == [expected_kw]


@pytest.mark.parametrize("price", [MARK, STOP_PX], ids=["hold_price", "stop_price"])
@pytest.mark.parametrize("source", ["stale", "live", None])
@pytest.mark.parametrize(
    "lot",
    [
        _lot(tagged=False),
        _lot(tagged=False, short_recipe="climax_fade"),
        _lot(short_recipe="climax_fade"),
    ],
    ids=["untagged", "climax_untagged", "climax_tagged"],
)
def test_untagged_and_climax_lot_decision_identical_to_base(clock, monkeypatch, lot, source, price):
    """Base: the queue-off loop passed no price_source. These lots ignore it."""
    from services.signal_orchestrator import SignalOrchestrator

    monkeypatch.setattr("strategies.climax_fade.last_closed_4h_bar", lambda *a, **k: None)
    base_orch = _orchestrator(_raw())
    analysis = type("Analysis", (), {"symbol": SYM, "timeframe": "1h"})()
    SignalOrchestrator._cycle_short_cover(base_orch, dict(lot), analysis, price)
    orders, _ = _cycle(monkeypatch, lot=lot, source=source, price=price)
    assert [(o.type, o.exit_source, o.exit_rationale) for o in orders] == [
        (o.type, o.exit_source, o.exit_rationale) for o in base_orch.trading.orders
    ]


def test_flag_off_tagged_lot_identical_to_base(clock, monkeypatch):
    raw = _raw(flag=False)
    expected = evaluate_short_cover(_lot(), MARK, symbol=SYM, config_raw=raw)
    orders, _ = _cycle(monkeypatch, lot=_lot(), source="stale", raw=raw)
    assert expected is None
    assert orders == []


def test_hub_stale_predicate_unchanged():
    """The hub's stale_tagged_short_deferred predicate (#826) stays as is."""
    from strategies.short_cover_wiring import stale_defers_tagged_short

    raw = _raw()
    assert stale_defers_tagged_short(_lot(), raw) is True
    assert stale_defers_tagged_short(_lot(tagged=False), raw) is False
    assert stale_defers_tagged_short(_lot(short_recipe="climax_fade"), raw) is False
    assert stale_defers_tagged_short(_lot(amount=0), raw) is False
