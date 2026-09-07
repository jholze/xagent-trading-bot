"""Stale-price TTL, expiry logging, and entry-price fallback gate (#303)."""

from contextlib import ExitStack
from unittest.mock import patch

import pytest

import price_fetcher as pf


SYM = "SOL/USDT"
MAX_AGE = 300.0


class _Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def monotonic(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def _reset_price_state():
    pf._price_cache.clear()
    pf._last_good_cache.clear()
    pf._stale_warned.clear()
    yield
    pf._price_cache.clear()
    pf._last_good_cache.clear()
    pf._stale_warned.clear()


@pytest.fixture
def clock(monkeypatch):
    clk = _Clock()
    monkeypatch.setattr(pf.time, "monotonic", clk.monotonic)
    monkeypatch.setattr(pf, "_stale_price_max_age_sec", lambda: MAX_AGE)
    return clk


def _enter_no_network(stack: ExitStack, *, gate_hits: dict | None = None):
    stack.enter_context(
        patch("price_fetcher._fetch_gate_bulk", return_value=gate_hits or {})
    )
    stack.enter_context(patch("price_fetcher._fetch_coingecko_bulk", return_value={}))
    stack.enter_context(patch("price_fetcher._fetch_coingecko_single", return_value=None))
    stack.enter_context(patch("price_fetcher._fetch_gate_single", return_value=None))
    stack.enter_context(
        patch("price_fetcher._fetch_single_symbol", side_effect=lambda s: (s, 0.0))
    )
    stack.enter_context(patch("bus.price_cache.price_cache_enabled", return_value=False))
    return stack


def _warnings(logged: list[tuple[str, str]]) -> list[str]:
    return [msg for msg, level in logged if str(level).upper() == "WARNING"]


def test_fresh_cache_entry_served_as_stale(clock):
    pf._last_good_cache[SYM] = (142.5, clock.t)
    clock.t += 10.0
    logged = []
    with ExitStack() as stack:
        _enter_no_network(stack)
        stack.enter_context(
            patch("price_fetcher.log", side_effect=lambda m, level="INFO": logged.append((str(m), str(level))))
        )
        prices, sources = pf.get_prices_batch([SYM], return_sources=True)
    assert prices[SYM] == pytest.approx(142.5)
    assert sources[SYM] == "stale"
    assert _warnings(logged) == []
    assert SYM not in pf.stale_expired_symbols()


def test_expired_cache_returns_zero_and_warns_once(clock):
    pf._last_good_cache[SYM] = (142.5, clock.t)
    clock.t += MAX_AGE + 1.0
    logged = []

    def _log(message, level="INFO"):
        logged.append((str(message), str(level)))

    with ExitStack() as stack:
        _enter_no_network(stack)
        stack.enter_context(patch("price_fetcher.log", side_effect=_log))
        prices, sources = pf.get_prices_batch([SYM], return_sources=True)
        assert prices[SYM] == 0.0
        assert sources[SYM] == "stale_expired"
        first = _warnings(logged)
        assert len(first) == 1
        assert SYM in first[0]
        assert "stale_expired" in first[0]

        prices2, sources2 = pf.get_prices_batch([SYM], return_sources=True)
        assert prices2[SYM] == 0.0
        assert sources2[SYM] == "stale_expired"
        assert len(_warnings(logged)) == 1


def test_fresh_fetch_clears_warned_state_and_serves_live(clock):
    pf._last_good_cache[SYM] = (142.5, clock.t)
    clock.t += MAX_AGE + 5.0
    logged = []

    def _log(message, level="INFO"):
        logged.append((str(message), str(level)))

    with ExitStack() as stack:
        _enter_no_network(stack)
        stack.enter_context(patch("price_fetcher.log", side_effect=_log))
        prices, sources = pf.get_prices_batch([SYM], return_sources=True)
    assert sources[SYM] == "stale_expired"
    assert SYM in pf._stale_warned

    pf._price_cache.clear()
    with ExitStack() as stack:
        _enter_no_network(stack, gate_hits={SYM: 151.0})
        stack.enter_context(patch("price_fetcher.log", side_effect=_log))
        prices, sources = pf.get_prices_batch([SYM], return_sources=True)
    assert prices[SYM] == pytest.approx(151.0)
    assert sources[SYM] == "live"
    assert SYM not in pf._stale_warned
    assert SYM not in pf.stale_expired_symbols()


def test_entry_price_fallback_default_off_and_opt_in(clock):
    fallbacks = {SYM: 99.0}
    with ExitStack() as stack:
        _enter_no_network(stack)
        prices, sources = pf.get_prices_batch(
            [SYM], fallbacks=fallbacks, return_sources=True
        )
    assert prices[SYM] == 0.0
    assert sources[SYM] == "missing"

    with ExitStack() as stack:
        _enter_no_network(stack)
        prices, sources = pf.get_prices_batch(
            [SYM],
            fallbacks=fallbacks,
            return_sources=True,
            allow_entry_price_fallback=True,
        )
    assert prices[SYM] == pytest.approx(99.0)
    assert sources[SYM] == "entry"


def test_stale_expired_symbols_reflects_current_set(clock):
    other = "BNB/USDT"
    pf._last_good_cache[SYM] = (142.5, clock.t)
    pf._last_good_cache[other] = (500.0, clock.t)
    assert pf.stale_expired_symbols() == set()

    clock.t += MAX_AGE + 1.0
    assert pf.stale_expired_symbols() == {SYM, other}

    with ExitStack() as stack:
        _enter_no_network(stack)
        pf.get_prices_batch([SYM, other], return_sources=True)
    assert pf.stale_expired_symbols() == {SYM, other}

    pf._price_cache.clear()
    with ExitStack() as stack:
        _enter_no_network(stack, gate_hits={SYM: 151.0})
        pf.get_prices_batch([SYM], return_sources=True)
    assert pf.stale_expired_symbols() == {other}
