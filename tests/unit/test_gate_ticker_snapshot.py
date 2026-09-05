"""Process-wide Gate /spot/tickers snapshot (#304)."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

import price_fetcher as pf


TTL = 25.0


class _Clock:
    def __init__(self, t: float = 1_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _resp(payload: list[dict], *, status: int = 200, body: bytes | None = None):
    resp = MagicMock()
    resp.status_code = status
    raw = body if body is not None else b"x" * 128
    resp.content = raw
    resp.json.return_value = payload
    return resp


@pytest.fixture(autouse=True)
def _ttl_and_log(monkeypatch):
    monkeypatch.setattr(pf, "_gate_ticker_snapshot_ttl_sec", lambda: TTL)
    logged: list[tuple[str, str]] = []
    monkeypatch.setattr(
        pf, "log", lambda m, level="INFO": logged.append((str(m), str(level)))
    )
    return logged


def test_ttl_with_fake_clock_single_download(monkeypatch, _ttl_and_log):
    clock = _Clock()
    monkeypatch.setattr(pf, "_now", clock)
    payload = [
        {"currency_pair": "BTC_USDT", "last": "100.5"},
        {"currency_pair": "ETH_USDT", "last": "3.25"},
        {"currency_pair": "SOL_USDT", "last": "0"},
    ]
    calls = {"n": 0}

    def fake_get(*_a, **_k):
        calls["n"] += 1
        return _resp(payload, body=b"y" * 541)

    monkeypatch.setattr(pf.requests, "get", fake_get)

    first = pf._fetch_gate_bulk(["BTC/USDT", "ETH/USDT", "SOL/USDT"])
    assert first == {"BTC/USDT": 100.5, "ETH/USDT": 3.25}
    assert calls["n"] == 1
    infos = [m for m, level in _ttl_and_log if level == "INFO"]
    assert len(infos) == 1
    assert "541 bytes" in infos[0]
    assert "ms" in infos[0]

    clock.t += TTL - 0.01
    second = pf._fetch_gate_bulk(["BTC/USDT"])
    assert second == {"BTC/USDT": 100.5}
    assert calls["n"] == 1
    assert len([m for m, level in _ttl_and_log if level == "INFO"]) == 1

    clock.t += 0.02
    third = pf._gate_ticker_snapshot()
    assert third["ETH/USDT"] == pytest.approx(3.25)
    assert calls["n"] == 2
    assert len([m for m, level in _ttl_and_log if level == "INFO"]) == 2


def test_concurrent_callers_share_one_download(monkeypatch, _ttl_and_log):
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []
    errors: list[BaseException] = []

    def fake_get(*_a, **_k):
        calls.append(1)
        started.set()
        if not release.wait(timeout=3):
            raise TimeoutError("download was not released")
        return _resp([{"currency_pair": "BTC_USDT", "last": "42"}])

    monkeypatch.setattr(pf.requests, "get", fake_get)

    results: list[dict] = []

    def worker():
        try:
            results.append(pf._fetch_gate_bulk(["BTC/USDT"]))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    assert started.wait(timeout=3)
    threading.Event().wait(0.05)
    assert len(calls) == 1
    release.set()
    for t in threads:
        t.join(timeout=3)
    assert errors == []
    assert len(calls) == 1
    assert results and all(r == {"BTC/USDT": 42.0} for r in results)


def test_stale_fallback_within_5x_ttl_then_empty(monkeypatch, _ttl_and_log):
    clock = _Clock()
    monkeypatch.setattr(pf, "_now", clock)

    def ok(*_a, **_k):
        return _resp(
            [{"currency_pair": "PEPE_USDT", "last": "0.00001"}],
            body=b"ok",
        )

    monkeypatch.setattr(pf.requests, "get", ok)
    assert pf._fetch_gate_bulk(["PEPE/USDT"]) == {"PEPE/USDT": 0.00001}

    def boom(*_a, **_k):
        raise ConnectionError("gate down")

    monkeypatch.setattr(pf.requests, "get", boom)

    clock.t += TTL + 1.0
    stale = pf._fetch_gate_bulk(["PEPE/USDT"])
    assert stale == {"PEPE/USDT": 0.00001}

    clock.t += (TTL * 5) + 1.0
    assert pf._fetch_gate_bulk(["PEPE/USDT"]) == {}


def test_http_error_uses_stale_then_previous_empty(monkeypatch, _ttl_and_log):
    clock = _Clock()
    monkeypatch.setattr(pf, "_now", clock)
    monkeypatch.setattr(
        pf.requests,
        "get",
        lambda *_a, **_k: _resp([{"currency_pair": "SOL_USDT", "last": "150"}]),
    )
    assert pf._fetch_gate_bulk(["SOL/USDT"])["SOL/USDT"] == pytest.approx(150)

    monkeypatch.setattr(
        pf.requests,
        "get",
        lambda *_a, **_k: _resp([], status=500, body=b"err"),
    )
    clock.t += TTL + 0.5
    assert pf._fetch_gate_bulk(["SOL/USDT"]) == {"SOL/USDT": 150.0}

    clock.t += TTL * 5
    assert pf._fetch_gate_bulk(["SOL/USDT"]) == {}


def test_empty_symbols_does_not_download(monkeypatch, _ttl_and_log):
    def fail(*_a, **_k):
        raise AssertionError("download should not run")

    monkeypatch.setattr(pf.requests, "get", fail)
    assert pf._fetch_gate_bulk([]) == {}
