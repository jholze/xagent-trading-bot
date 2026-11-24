"""#832: aria_bot queue-off loop defers a tagged short at a stale quote.

Drives the production caller ``aria_bot._run_tenant_price_cycle`` with
``architecture.eval_queue_enabled`` off, a fake orchestrator and a fake
quote batch. The cycle is stopped right after the scan loop (the first
``get_config().get("update_interval")`` read) so no post-cycle digest runs.
Synthetic symbol, no network, no ledger writes.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

SYM = "AAA/USDT"
OTHER = "BBB/USDT"
_BERLIN = ZoneInfo("Europe/Berlin")
OPENED = datetime(2026, 11, 24, 17, 0, tzinfo=_BERLIN)


class _StopAfterScan(Exception):
    pass


class _Config(dict):
    def get(self, key, default=None):
        if key == "update_interval":
            raise _StopAfterScan()
        return super().get(key, default)


class _BotConfig:
    terminal_dashboard_enabled = False

    def __init__(self, raw):
        self.raw = raw

    def refresh(self):
        return None


class _Orchestrator:
    def __init__(self):
        self.calls: list[tuple] = []

    def begin_tenant_cycle(self):
        return None

    def run_portfolio_dca_pass(self, *a, **k):
        return None

    def process_coin(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"action": "HOLD", "symbol": args[0]["symbol"]}


def _raw(*, flag=True):
    return {
        "trading_mode": "paper",
        "architecture": {"eval_queue_enabled": False},
        "shorts": {
            "enabled": True,
            "allow_live": False,
            "sandbox_expiry_v220": {"enabled": flag},
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
    }
    if tagged:
        lot["sandbox_path"] = "v220_paper"
    lot.update(extra)
    return lot


def _reset():
    # New in #832; imported lazily so the file collects (and fails) on base.
    try:
        from strategies.short_cover_wiring import reset_cycle_stale_stats_for_tests
    except ImportError:
        return
    reset_cycle_stale_stats_for_tests()


@pytest.fixture(autouse=True)
def _reset_stats():
    _reset()
    yield
    _reset()


def _cycle(monkeypatch, *, lots_by_symbol, sources, raw=None, prices=None):
    import aria_bot

    raw = raw or _raw()
    prices = prices or {SYM: 90.0, OTHER: 10.0}
    coins = [{"symbol": s, "active": True} for s in prices]
    orch = _Orchestrator()
    monkeypatch.setattr("services.architecture_runtime.ensure_started", lambda *a, **k: None)
    monkeypatch.setattr("strategies.positions.is_positions_state_unknown", lambda *a, **k: False)
    monkeypatch.setattr("strategies.positions.list_active_positions", lambda *a, **k: [])
    monkeypatch.setattr("core.tenant_context.multi_tenant_enabled", lambda: False)
    monkeypatch.setattr("services.universe.source_tags.publish_cycle_tags", lambda *a, **k: None)
    monkeypatch.setattr("services.ledger_sync.reconcile_recent_highs", lambda *a, **k: None)
    monkeypatch.setattr(
        "services.gainer_universe.runtime.maybe_refresh_gainer_universe", lambda *a, **k: None
    )
    monkeypatch.setattr("bus.eval_queue.eval_queue_enabled", lambda raw: False)
    monkeypatch.setattr(aria_bot, "get_bot_config", lambda: _BotConfig(raw))
    monkeypatch.setattr(aria_bot, "get_config", lambda: _Config(raw))
    monkeypatch.setattr(aria_bot, "load_effective_watchlist", lambda *a, **k: list(coins))
    monkeypatch.setattr(aria_bot, "load_trade_watchlist", lambda *a, **k: list(coins))
    monkeypatch.setattr(aria_bot, "log", lambda *a, **k: None)

    def _batch(symbols, fallbacks=None, *, return_sources=False, **k):
        got = {s: prices[s] for s in symbols if s in prices}
        if return_sources:
            return got, {s: sources[s] for s in symbols if s in sources}
        return got

    monkeypatch.setattr(aria_bot, "get_prices_batch", _batch)

    def _lots(symbol, **k):
        value = lots_by_symbol.get(symbol, [])
        if isinstance(value, Exception):
            raise value
        return [(lot.get("timeframe", "1h"), lot) for lot in value]

    monkeypatch.setattr("strategies.positions.find_open_positions_for_symbol", _lots)
    with pytest.raises(_StopAfterScan):
        aria_bot._run_tenant_price_cycle(
            cycle_started=0.0, use_dashboard=False,
            analyzer=None, orchestrator=orch, social_pipeline=None, sandbox=None, trend_engine=None,
        )
    return orch


def _symbols(orch):
    return [args[0]["symbol"] for args, _ in orch.calls]


def _deferred():
    try:
        from strategies.short_cover_wiring import cycle_stale_stats
    except ImportError:
        return None
    return cycle_stale_stats().get("stale_tagged_short_deferred")


def test_tagged_short_at_stale_quote_is_deferred_and_counted(monkeypatch):
    orch = _cycle(
        monkeypatch,
        lots_by_symbol={SYM: [_lot()]},
        sources={SYM: "stale", OTHER: "live"},
    )
    assert _symbols(orch) == [OTHER]  # no cover evaluation for SYM at the stale price
    assert _deferred() == 1


def test_counter_accumulates_per_stale_cycle(monkeypatch):
    for _ in range(2):
        _cycle(monkeypatch, lots_by_symbol={SYM: [_lot()]}, sources={SYM: "stale"})
    assert _deferred() == 2


@pytest.mark.parametrize(
    "lots, source",
    [
        ([_lot(tagged=False)], "stale"),
        ([_lot(short_recipe="climax_fade")], "stale"),
        ([{"side": "long", "amount": 1.0, "average_entry": 100.0, "symbol": SYM, "timeframe": "1h"}], "stale"),
        ([], "stale"),
        ([_lot()], "live"),
        ([_lot()], "redis"),
        ([_lot()], None),
    ],
    ids=["untagged", "climax", "long", "no_lot", "tagged_live", "tagged_redis", "tagged_no_label"],
)
def test_other_lots_and_fresh_quotes_keep_the_base_call(monkeypatch, lots, source):
    orch = _cycle(
        monkeypatch,
        lots_by_symbol={SYM: lots},
        sources={SYM: source} if source else {},
    )
    assert _symbols(orch) == [SYM, OTHER]
    args, kwargs = orch.calls[0]
    # Identical to the base call: positional (coin, price, x, cmc, lc), quiet only.
    assert args[0]["symbol"] == SYM and args[1] == 90.0
    assert kwargs == {"quiet": False}
    assert _deferred() == 0


def test_flag_off_keeps_the_base_call(monkeypatch):
    orch = _cycle(
        monkeypatch,
        lots_by_symbol={SYM: [_lot()]},
        sources={SYM: "stale"},
        raw=_raw(flag=False),
    )
    assert _symbols(orch) == [SYM, OTHER]
    assert _deferred() == 0


def test_lookup_error_never_crashes_or_suppresses_the_call(monkeypatch):
    orch = _cycle(
        monkeypatch,
        lots_by_symbol={SYM: RuntimeError("ledger down")},
        sources={SYM: "stale"},
    )
    assert _symbols(orch) == [SYM, OTHER]
    assert _deferred() == 0


def test_hub_and_cycle_share_the_tagged_short_predicate():
    from strategies.short_cover_wiring import stale_defers_tagged_short

    raw = _raw()
    assert stale_defers_tagged_short(_lot(), raw) is True
    assert stale_defers_tagged_short(_lot(tagged=False), raw) is False
    assert stale_defers_tagged_short(_lot(short_recipe="climax_fade"), raw) is False
    assert stale_defers_tagged_short(_lot(amount=0), raw) is False
    assert stale_defers_tagged_short(None, raw) is False


def test_mixed_lots_on_one_symbol_keep_the_base_call(monkeypatch):
    """A tagged short plus an untagged lot on another timeframe: no deferral."""
    orch = _cycle(
        monkeypatch,
        lots_by_symbol={SYM: [_lot(timeframe="4h"), _lot(tagged=False)]},
        sources={SYM: "stale"},
    )
    assert _symbols(orch) == [SYM, OTHER]
    assert orch.calls[0][1] == {"quiet": False}
    assert _deferred() == 0
