import json
import os
import shutil
import socket as _socket
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _sanitize_pytest_db_suffix(raw: str | None = None) -> str:
    if raw is None:
        raw = os.environ.get("PYTEST_DB_SUFFIX") or ""
    return "".join(
        ch for ch in raw.strip() if (ch.isalnum() and ord(ch) < 128) or ch == "_"
    )


def _effective_pytest_db_suffix() -> str:
    """Sanitized PYTEST_DB_SUFFIX, with ``_<worker>`` appended under xdist.

    Sequential (no ``PYTEST_XDIST_WORKER``): unchanged sanitized value, possibly
    empty so Mongo stays ``xagent_pytest``. xdist worker: ``{sanitized or
    'default'}_{gwN}`` so Mongo is ``xagent_pytest_par_gw3`` and Redis is
    ``pytest:par_gw3:``. Idempotent if the suffix already ends with ``_<worker>``.
    """
    sanitized = _sanitize_pytest_db_suffix()
    worker = (os.environ.get("PYTEST_XDIST_WORKER") or "").strip()
    if not worker:
        return sanitized
    if sanitized.endswith(f"_{worker}"):
        return sanitized
    return f"{sanitized or 'default'}_{worker}"


def _apply_xdist_worker_db_suffix() -> None:
    """Mutate PYTEST_DB_SUFFIX so resolve_test_db_name() sees the worker.

    Must run before ``storage.mongo_client`` is imported: TEST_DB_NAME is
    computed at import time. Sequential runs leave the env var alone.
    """
    worker = (os.environ.get("PYTEST_XDIST_WORKER") or "").strip()
    if not worker:
        return
    os.environ["PYTEST_DB_SUFFIX"] = _effective_pytest_db_suffix()


def _cleanup_xdist_worker_stores() -> None:
    """Drop this xdist worker's Mongo DB and OHLCV Redis keys.

    Sequential runs (no PYTEST_XDIST_WORKER) keep today's behaviour: tests
    drop their own DB in fixtures; no session-end drop of xagent_pytest.
    """
    if not (os.environ.get("PYTEST_XDIST_WORKER") or "").strip():
        return
    from storage.mongo_client import close_client, drop_database, resolve_test_db_name

    # Re-pin in case a test left MONGODB_TEST_DB pointing at a different name.
    test_db = resolve_test_db_name()
    os.environ["MONGODB_TEST_DB"] = test_db
    os.environ["MONGODB_DB"] = test_db
    close_client()
    try:
        drop_database(test=True)
    finally:
        close_client()
    suffix = _sanitize_pytest_db_suffix() or "default"
    os.environ["OHLCV_CACHE_KEY_PREFIX"] = f"pytest:{suffix}:"
    from bus.ohlcv_cache import reset_ohlcv_cache_for_tests

    reset_ohlcv_cache_for_tests()


def _is_xdist_controller(session) -> bool:
    if (os.environ.get("PYTEST_XDIST_WORKER") or "").strip():
        return False
    n = getattr(getattr(session, "config", None), "option", None)
    n = getattr(n, "numprocesses", None) if n is not None else None
    return bool(n)


def _cleanup_xdist_controller_leftovers() -> None:
    """Drop any xagent_pytest_<suffix>_gw* DBs a worker failed to remove."""
    suffix = _sanitize_pytest_db_suffix() or "default"
    db_prefix = f"xagent_pytest_{suffix}_gw"
    from storage.mongo_client import close_client, get_client

    close_client()
    client = get_client()
    try:
        for name in client.list_database_names():
            if name.startswith(db_prefix):
                client.drop_database(name)
    finally:
        close_client()
    try:
        from bus.redis_client import get_redis, reset_redis_client

        reset_redis_client()
        redis_client = get_redis()
        if redis_client:
            keys = list(redis_client.scan_iter(match=f"pytest:{suffix}_gw*:ohlcv:*", count=200))
            if keys:
                redis_client.delete(*keys)
    except Exception:
        pass


# Never let pytest touch Railway/remote Mongo — must run before any test imports mongo_client.
_apply_xdist_worker_db_suffix()
from storage.mongo_client import (
    DEV_DB_NAME,
    close_client,
    force_local_test_mongo,
    resolve_test_db_name,
)

os.environ["PYTEST_RUNNING"] = "1"
force_local_test_mongo(dev=False)
os.environ["MONGODB_DB"] = resolve_test_db_name()


def _pytest_redis_key_prefix() -> str:
    """One Redis prefix for OHLCV, oracle and santiment under pytest (#319/#323),
    worker-aware under xdist (#321): pytest:<suffix>[_gwN]:"""
    return f"pytest:{_effective_pytest_db_suffix() or 'default'}:"


def pytest_configure(config):
    os.environ["PYTEST_RUNNING"] = "1"
    _apply_xdist_worker_db_suffix()
    force_local_test_mongo(dev=False)
    os.environ["MONGODB_DB"] = resolve_test_db_name()
    close_client()
    # Pin before collection so import-time / post-teardown store writes never
    # fall back to the production aria: prefix (#323).
    os.environ["OHLCV_CACHE_KEY_PREFIX"] = _pytest_redis_key_prefix()
    # xdist workers may skip pytest_collection_finish; honor UNIT_TEST_PROGRESS here.
    _PROGRESS["enabled"] = _progress_enabled(config)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hermes"

# ---------------------------------------------------------------------------
# Unit tests must not reach the public internet (#324).
# Local Mongo / Redis / in-process HTTP (127.0.0.1, localhost, ::1) stay open.
# ---------------------------------------------------------------------------
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})


def _is_local_host(host) -> bool:
    if host is None or host == "":
        return True  # AF_UNIX / missing host
    if isinstance(host, bytes):
        try:
            host = host.decode("utf-8", "replace")
        except Exception:
            return False
    h = str(host).strip().lower().strip("[]")
    if "%" in h:  # IPv6 zone index, e.g. ::1%lo0
        h = h.split("%", 1)[0]
    if h in _LOCAL_HOSTS:
        return True
    if h.startswith("127."):
        return True
    return False


def _host_from_connect_address(address):
    if isinstance(address, (bytes, bytearray, str)):
        return None  # AF_UNIX
    if isinstance(address, tuple) and address:
        return address[0]
    return None


def _host_from_url(url) -> str:
    if url is None:
        return ""
    if hasattr(url, "get_full_url"):
        url = url.get_full_url()
    raw = str(url)
    parsed = urlparse(raw)
    return parsed.hostname or raw


def _raise_network_blocked(target) -> None:
    raise RuntimeError(f"network blocked in unit tests: {target}")


@pytest.fixture(scope="session", autouse=True)
def block_outbound_network():
    """Fail immediately on non-loopback sockets / HTTP. No opt-out marker.

    Also wraps ssl.SSLSocket.connect and requests.Session.send: ccxt calls
    session.send() (not Session.request) and TLS sockets skip the plain
    socket.socket.connect wrapper.
    """
    orig_create_connection = _socket.create_connection
    orig_connect = _socket.socket.connect
    orig_connect_ex = _socket.socket.connect_ex

    import ssl as _ssl
    import urllib.request as _ureq

    orig_ssl_connect = _ssl.SSLSocket.connect
    orig_ssl_connect_ex = getattr(_ssl.SSLSocket, "connect_ex", None)
    orig_urlopen = _ureq.urlopen

    import requests as _requests

    orig_session_request = _requests.Session.request
    orig_session_send = _requests.Session.send
    orig_requests_get = _requests.get
    orig_requests_post = _requests.post

    def _guarded_create_connection(address, *args, **kwargs):
        host = _host_from_connect_address(address)
        if not _is_local_host(host):
            _raise_network_blocked(host)
        return orig_create_connection(address, *args, **kwargs)

    def _guarded_connect(self, address):
        host = _host_from_connect_address(address)
        if not _is_local_host(host):
            _raise_network_blocked(host)
        return orig_connect(self, address)

    def _guarded_connect_ex(self, address):
        host = _host_from_connect_address(address)
        if not _is_local_host(host):
            _raise_network_blocked(host)
        return orig_connect_ex(self, address)

    def _guarded_ssl_connect(self, address):
        host = _host_from_connect_address(address)
        if not _is_local_host(host):
            _raise_network_blocked(host)
        return orig_ssl_connect(self, address)

    def _guarded_ssl_connect_ex(self, address):
        host = _host_from_connect_address(address)
        if not _is_local_host(host):
            _raise_network_blocked(host)
        return orig_ssl_connect_ex(self, address)

    def _guarded_urlopen(url, *args, **kwargs):
        host = _host_from_url(url)
        if not _is_local_host(host):
            _raise_network_blocked(host)
        return orig_urlopen(url, *args, **kwargs)

    def _guarded_session_request(self, method, url, *args, **kwargs):
        host = _host_from_url(url)
        if not _is_local_host(host):
            _raise_network_blocked(host)
        return orig_session_request(self, method, url, *args, **kwargs)

    def _guarded_session_send(self, request, **kwargs):
        host = _host_from_url(getattr(request, "url", None))
        if not _is_local_host(host):
            _raise_network_blocked(host)
        return orig_session_send(self, request, **kwargs)

    def _guarded_requests_get(url, *args, **kwargs):
        host = _host_from_url(url)
        if not _is_local_host(host):
            _raise_network_blocked(host)
        return orig_requests_get(url, *args, **kwargs)

    def _guarded_requests_post(url, *args, **kwargs):
        host = _host_from_url(url)
        if not _is_local_host(host):
            _raise_network_blocked(host)
        return orig_requests_post(url, *args, **kwargs)

    _socket.create_connection = _guarded_create_connection
    _socket.socket.connect = _guarded_connect
    _socket.socket.connect_ex = _guarded_connect_ex
    _ssl.SSLSocket.connect = _guarded_ssl_connect
    if orig_ssl_connect_ex is not None:
        _ssl.SSLSocket.connect_ex = _guarded_ssl_connect_ex
    _ureq.urlopen = _guarded_urlopen
    _requests.Session.request = _guarded_session_request
    _requests.Session.send = _guarded_session_send
    _requests.get = _guarded_requests_get
    _requests.post = _guarded_requests_post
    try:
        yield
    finally:
        _socket.create_connection = orig_create_connection
        _socket.socket.connect = orig_connect
        _socket.socket.connect_ex = orig_connect_ex
        _ssl.SSLSocket.connect = orig_ssl_connect
        if orig_ssl_connect_ex is not None:
            _ssl.SSLSocket.connect_ex = orig_ssl_connect_ex
        _ureq.urlopen = orig_urlopen
        _requests.Session.request = orig_session_request
        _requests.Session.send = orig_session_send
        _requests.get = orig_requests_get
        _requests.post = orig_requests_post


@pytest.fixture(autouse=True)
def isolate_test_mongo(monkeypatch):
    """Pytest uses isolated xagent_pytest — never the operator dev ledger xagent_test."""
    monkeypatch.delenv("MONGO_URL", raising=False)
    monkeypatch.delenv("DEMO_LEDGER_BACKEND", raising=False)
    monkeypatch.setenv("PYTEST_RUNNING", "1")
    monkeypatch.setenv("MONGODB_URI", "mongodb://127.0.0.1:27017")
    test_db = resolve_test_db_name()
    monkeypatch.setenv("MONGODB_DB", test_db)
    monkeypatch.setenv("MONGODB_TEST_DB", test_db)
    close_client()
    yield
    close_client()


@pytest.fixture(autouse=True)
def reset_oracle_climax_cycle():
    """oracle_climax._cycle is a process-wide cache; do not leak grind across tests (#323)."""
    from strategies.oracle_climax import reset_cycle, reset_stale_episode_for_tests

    reset_cycle()
    reset_stale_episode_for_tests()
    yield
    reset_cycle()
    reset_stale_episode_for_tests()


@pytest.fixture(autouse=True)
def reset_gate_ticker_snapshot():
    """Process-wide Gate /spot/tickers snapshot must not leak across tests (#304)."""
    from price_fetcher import reset_gate_ticker_snapshot_for_tests

    reset_gate_ticker_snapshot_for_tests()
    yield
    reset_gate_ticker_snapshot_for_tests()


def _scan_del_redis_keys(*patterns: str) -> None:
    """SCAN + DEL. Swallows errors so tests without Redis still run."""
    try:
        from bus.redis_client import get_redis

        client = get_redis()
        if not client:
            return
        keys: list = []
        for pattern in patterns:
            keys.extend(client.scan_iter(match=pattern, count=200))
        for i in range(0, len(keys), 200):
            batch = keys[i : i + 200]
            if batch:
                client.delete(*batch)
    except Exception:
        return


@pytest.fixture(autouse=True)
def isolate_ohlcv_cache_key_prefix(monkeypatch):
    """Keep pytest OHLCV Redis keys off the production aria: prefix (#319).

    Also SCAN+DEL `{prefix}market_oracle:*` and `{prefix}santiment:*` (#323).
    """
    prefix = _pytest_redis_key_prefix()
    monkeypatch.setenv("OHLCV_CACHE_KEY_PREFIX", prefix)
    from bus.ohlcv_cache import reset_ohlcv_cache_for_tests
    from services.market_oracle.store import reset_for_tests as reset_ora_store
    from services.santiment.store import reset_for_tests as reset_san_store

    def _purge():
        reset_ohlcv_cache_for_tests()
        reset_ora_store()
        reset_san_store()
        _scan_del_redis_keys(f"{prefix}market_oracle:*", f"{prefix}santiment:*")

    _purge()
    yield
    _purge()


@pytest.fixture(autouse=True)
def demo_mode_env(monkeypatch, request):
    """Unit tests run with isolated demo JSON paths when touching data files."""
    nodeid = getattr(request.node, "nodeid", "") or ""
    if "live_gate_readiness" in nodeid or "mongo_backend" in nodeid:
        monkeypatch.setenv("DEMO_MODE", "0")
        return
    monkeypatch.setenv("DEMO_MODE", "1")


@pytest.fixture(autouse=True)
def disable_universe_split_unless_explicit(monkeypatch, request):
    """Production config.json has universe.split_enabled=true; most unit tests
    were written against an open watchlist. Keep split on only for universe tests.
    """
    nodeid = getattr(request.node, "nodeid", "") or ""
    if "universe" in nodeid or "relvol_risk_universe" in nodeid:
        return
    monkeypatch.setattr(
        "services.universe.split.universe_split_enabled",
        lambda config=None: False,
    )


@pytest.fixture(autouse=True)
def isolate_operator_production_flags(monkeypatch, request):
    """Keep unit tests off operator MT / WQE flags unless the test is about them."""
    nodeid = getattr(request.node, "nodeid", "") or ""
    if "mongo_ledger" not in nodeid and "tenant_" not in nodeid:
        monkeypatch.setenv("MULTI_TENANT_ENABLED", "0")
    if "watchlist_quality" not in nodeid and "wqe" not in nodeid:
        monkeypatch.setenv("WATCHLIST_QUALITY_MODE", "off")


@pytest.fixture(autouse=True)
def isolate_demo_ledger_files(tmp_path, monkeypatch):
    """Keep unit tests from mutating operator orders.demo.json (XRVM etc.)."""
    import data_manager
    import storage.ledger_router as ledger_router

    # #325: every scope of every ledger table gets its own per-test copy. A
    # single suite run used to write orders.live.json, orders.paper.json,
    # positions.paper.json, trade_history.json and live_trade_history(.demo).json
    # into data/ — and xdist workers clobbered each other in those files.
    # get_data_file()/resolve_data_path() return explicit paths unchanged, so
    # the tmp copies are used as-is by both data_manager and the JSON store.
    def _tmp_scope_file(name: str, default: dict) -> str:
        dst = str(tmp_path / name)
        src = data_manager.resolve_data_path(name)
        if src and os.path.exists(src):
            shutil.copy2(src, dst)
        else:
            Path(dst).write_text(json.dumps(default), encoding="utf-8")
        return dst

    orders_files = {
        scope: _tmp_scope_file(
            name, {"ledger_scope": scope, "orders": [], "migrated_from_trades": False}
        )
        for scope, name in data_manager.ORDERS_SCOPE_FILES.items()
    }
    positions_files = {
        scope: _tmp_scope_file(name, {"ledger_scope": scope, "positions": {}})
        for scope, name in data_manager.POSITIONS_SCOPE_FILES.items()
    }
    # demo and live share one physical file in production; keep that here.
    history_files = {}
    for scope, name in data_manager.TRADE_HISTORY_SCOPE_FILES.items():
        key = data_manager._demo_variant(name) if scope == "demo" else name
        history_files[scope] = history_files.get(key) or _tmp_scope_file(key, {"trades": []})
        history_files[key] = history_files[scope]
    history_files = {k: v for k, v in history_files.items() if k in data_manager.TRADE_HISTORY_SCOPE_FILES}

    monkeypatch.setattr(data_manager, "ORDERS_SCOPE_FILES", orders_files)
    monkeypatch.setattr(data_manager, "POSITIONS_SCOPE_FILES", positions_files)
    monkeypatch.setattr(data_manager, "TRADE_HISTORY_SCOPE_FILES", history_files)
    monkeypatch.setattr(ledger_router, "ORDERS_SCOPE_FILES", orders_files)
    monkeypatch.setattr(ledger_router, "POSITIONS_SCOPE_FILES", positions_files)
    # strategies.positions binds both tables at import time (from storage.ledger_router
    # import ...), so its resolve_positions_file() needs the patched copy as well.
    import strategies.positions as positions_mod

    monkeypatch.setattr(positions_mod, "ORDERS_SCOPE_FILES", orders_files)
    monkeypatch.setattr(positions_mod, "POSITIONS_SCOPE_FILES", positions_files)
    yield


@pytest.fixture(autouse=True)
def reset_positions_memory(isolate_demo_ledger_files):
    """Prevent in-memory positions dict from leaking across unit tests."""
    from data_manager import resolve_ledger_scope
    from strategies.positions import _cancel_flush_timer, clear_positions_memory, load_positions

    clear_positions_memory()
    try:
        load_positions(resolve_ledger_scope())
    except Exception:
        pass
    yield
    # A debounced flush_positions() timer left by the test would fire after the
    # monkeypatches are gone and write the real data/positions.*.json (#325).
    _cancel_flush_timer()
    clear_positions_memory()


@pytest.fixture(autouse=True)
def clear_ledger_caches():
    """Prevent OrderService / resolve_store caches from leaking across tests."""
    from services import order_service
    from storage import ledger_router

    order_service._ORDERS_READ_CACHE.clear()
    ledger_router._store_cache.clear()
    yield
    order_service._ORDERS_READ_CACHE.clear()
    ledger_router._store_cache.clear()


@pytest.fixture(autouse=True)
def normalize_unit_test_config(monkeypatch, request):
    """Keep unit tests independent of operator-scaled production config.json."""
    import copy

    import data_manager
    from data_manager import load_config

    cfg = copy.deepcopy(load_config())
    cfg["trading_mode"] = "paper"
    cfg["virtual_trading"] = True
    risk = cfg.setdefault("risk", {})
    risk["min_trade_usdt"] = 5.0
    risk["min_sell_notional_usdt"] = 15
    risk["min_position_usdt_for_partial_sell"] = 25
    risk["dca_reserve_pct"] = 0
    risk["dust_sweep_max_position_usdt"] = 15
    cmc = cfg.setdefault("cmc", {})
    cmc["min_sell_notional_usdt"] = 15
    cmc["min_position_usdt_for_social_sell"] = 50
    cfg["initial_capital_usdt"] = 5000
    cfg["max_usdt_per_trade"] = 200
    cfg.setdefault("live", {})["max_usdt_per_trade"] = 200
    cfg.setdefault("paper", {})["initial_capital_usdt"] = 5000
    cfg["paper"]["backend"] = "local"
    cfg.setdefault("demo", {})["backend"] = "local"
    cfg.setdefault("aggression", {})["max_position_multiplier"] = 2.0
    arch = cfg.setdefault("architecture", {})
    arch["ledger_backend"] = "local"
    arch["ledger_dual_write"] = False
    cfg.setdefault("multi_tenant", {})["enabled"] = False
    cfg.setdefault("watchlist_quality", {})["mode"] = "off"
    risk.setdefault("cash_policy", {})["enabled"] = False
    risk.setdefault("position_capacity", {})["enabled"] = False
    cfg["x_weight"] = 0.40
    cfg["technical_weight"] = 0.27
    cfg["onchain_weight"] = 0.15
    cfg["lc_weight"] = 0.18
    nodeid = getattr(request, "node", None)
    nodeid = getattr(nodeid, "nodeid", "") or ""
    if "indicator_regime" not in nodeid:
        cfg.setdefault("sell_policy", {}).setdefault("indicator_regime", {})["enabled"] = False
    if "regime" not in nodeid and "allocator" not in nodeid:
        cfg.setdefault("regime_detector", {})["enabled"] = False
        cfg.setdefault("strategy_allocator", {})["enabled"] = False

    def _disable_exit_ladders(node):
        if isinstance(node, dict):
            ladder = node.get("exit_ladder")
            if isinstance(ladder, dict):
                ladder["enabled"] = False
            for value in node.values():
                _disable_exit_ladders(value)
        elif isinstance(node, list):
            for item in node:
                _disable_exit_ladders(item)

    _disable_exit_ladders(cfg)
    cfg.setdefault("volatile_altcoin", {})["mode"] = "active"
    cfg["entry_sensor_15m"] = {
        "enabled": True,
        "mode": "active",
        "timeframe": "15m",
        "poll_interval_sec": 20,
        "vol_spike_mult": 2.0,
        "vol_avg_period": 20,
        "ema_period": 9,
        "require_ema_breakout": False,
        "block_buy_if_rsi_4h_above": 75,
        "fakeout_min_body_atr_ratio": 0.3,
        "cooldown_after_reject_hours": 2,
        "max_watched_coins": 15,
        "min_poll_gap_sec_per_coin": 20,
        "setup_modes": ["buy_signal", "setup_zone", "trending"],
        "watch_ttl_hours": 24,
    }
    data_manager._config_cache = cfg
    orig_save_config = data_manager.save_config
    orig_reload_config = data_manager.reload_config

    # Per strategy: seed cache only for default-tenant; do not replace get_config/reload globally.
    # Default-tenant calls will hit real get_config() which returns from _config_cache.
    # For reload on default: just reset cache.

    def _reload_config(tenant_id=None, **kwargs):
        if (tenant_id is None or tenant_id == "default"):
            data_manager._config_cache = None
        # for non-default, real reload will handle via load_config
        return data_manager.reload_config(tenant_id=tenant_id, **kwargs) if hasattr(data_manager, 'reload_config') else cfg

    # Note: we keep orig_reload but do not setattr reload globally; only guard default writes via save.

    def _save_config(updated, tenant_id=None, **kwargs):
        if (tenant_id is None or tenant_id == "default"):
            # guard only default-tenant: update in-memory cache only, no json write
            nonlocal cfg
            cfg = copy.deepcopy(updated)
            data_manager._config_cache = cfg
            return True
        # for non-default tenant: call real (will use tenant_meta_store)
        return orig_save_config(updated, tenant_id=tenant_id, **kwargs)

    monkeypatch.setattr(data_manager, "save_config", _save_config)
    # Do not setattr get_config or reload globally; only cache for default.
    # Remove cross-module propagation for get/reload.

    def _bot_config(tenant_id=None, **_kwargs):
        from core.config import BotConfig
        return BotConfig(raw=copy.deepcopy(cfg))

    monkeypatch.setattr("core.config.get_bot_config", _bot_config)
    monkeypatch.setattr("strategies.decision_engine.get_bot_config", _bot_config)


@pytest.fixture(autouse=True)
def telegram_credentials(monkeypatch):
    """Keep Telegram send paths testable after other tests clear env vars."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")


# ---------------------------------------------------------------------------
# Local unit-suite progress (always on for tests/unit unless UNIT_TEST_PROGRESS=0)
# ---------------------------------------------------------------------------
import time as _time

_PROGRESS: dict = {
    "total": 0,
    "done": 0,
    "passed": 0,
    "failed": 0,
    "skipped": 0,
    "errors": 0,
    "t0": 0.0,
    "enabled": True,
    "every": 1,  # print every N completed tests; failures always print
    "last_print": 0,
}


def _progress_enabled(config) -> bool:
    env = (os.environ.get("UNIT_TEST_PROGRESS") or "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    # Default: on for local human runs; off when CI sets CI=true and no override
    if (os.environ.get("CI") or "").strip().lower() in ("1", "true", "yes") and env == "":
        # still show summary every 50 in CI
        return True
    return True


def pytest_collection_finish(session):
    """Announce local unit suite size + isolation."""
    _PROGRESS["total"] = len(session.items)
    _PROGRESS["done"] = 0
    _PROGRESS["passed"] = 0
    _PROGRESS["failed"] = 0
    _PROGRESS["skipped"] = 0
    _PROGRESS["errors"] = 0
    _PROGRESS["t0"] = _time.time()
    _PROGRESS["enabled"] = _progress_enabled(session.config)
    # denser progress when quiet (-q) so the run is not silent
    quiet = session.config.getoption("quiet", default=0) or 0
    if quiet:
        _PROGRESS["every"] = int(os.environ.get("UNIT_TEST_PROGRESS_EVERY") or 10)
    else:
        _PROGRESS["every"] = int(os.environ.get("UNIT_TEST_PROGRESS_EVERY") or 1)
    if not _PROGRESS["enabled"]:
        return
    try:
        unit_n = sum(1 for i in session.items if "unit" in Path(str(i.path)).parts)
    except Exception:
        unit_n = _PROGRESS["total"]
    print(
        f"\n{'='*60}\n"
        f"  LOCAL tests  |  collected={_PROGRESS['total']}  (unit≈{unit_n})\n"
        f"  mongo={resolve_test_db_name()} @ 127.0.0.1  |  never Railway/remote\n"
        f"  progress every {_PROGRESS['every']} test(s)"
        f"  (UNIT_TEST_PROGRESS=0 to silence)\n"
        f"{'='*60}\n",
        flush=True,
    )


def _fmt_nodeid(nodeid: str) -> str:
    # tests/unit/foo.py::Test::test_x → unit/foo.py::test_x
    s = nodeid.replace("tests/", "")
    if len(s) > 72:
        return "…" + s[-71:]
    return s


def pytest_runtest_logreport(report):
    """Print live progress after each call (or setup skip/fail)."""
    if not _PROGRESS.get("enabled"):
        return
    # Count call outcomes; also setup failures and skips
    if report.when == "call":
        pass
    elif report.when == "setup" and (report.failed or report.skipped):
        pass
    else:
        return

    _PROGRESS["done"] += 1
    if report.skipped:
        _PROGRESS["skipped"] += 1
        status = "SKIP"
    elif report.failed:
        if report.when == "call":
            _PROGRESS["failed"] += 1
        else:
            _PROGRESS["errors"] += 1
        status = "FAIL" if report.when == "call" else "ERROR"
    elif report.passed and report.when == "call":
        _PROGRESS["passed"] += 1
        status = "PASS"
    else:
        status = report.outcome.upper()[:4]

    total = max(1, _PROGRESS["total"])
    done = _PROGRESS["done"]
    pct = 100.0 * done / total
    elapsed = _time.time() - (_PROGRESS["t0"] or _time.time())
    rate = done / elapsed if elapsed > 0.5 else 0.0
    eta = (total - done) / rate if rate > 0 else 0.0

    always = status in ("FAIL", "ERROR") or done == total
    every = max(1, int(_PROGRESS.get("every") or 1))
    if not always and (done % every) != 0:
        return

    print(
        f"[{done:4d}/{total} {pct:5.1f}% | "
        f"ok={_PROGRESS['passed']} fail={_PROGRESS['failed']} "
        f"err={_PROGRESS['errors']} skip={_PROGRESS['skipped']} | "
        f"{elapsed:6.1f}s eta={eta:5.0f}s] "
        f"{status} {_fmt_nodeid(report.nodeid)}",
        flush=True,
    )


def pytest_sessionfinish(session, exitstatus):
    # #325: importing aria_bot registers atexit(_flush_positions_on_exit). At
    # interpreter exit no fixture is active any more (DEMO_MODE unset -> scope
    # "paper"), so that hook wrote data/positions.paper.json and positions.json
    # after every run. Unregister it before the process exits.
    try:
        import atexit
        import sys as _sys

        _aria = _sys.modules.get("aria_bot")
        if _aria is not None and hasattr(_aria, "_flush_positions_on_exit"):
            atexit.unregister(_aria._flush_positions_on_exit)
    except Exception:
        pass
    try:
        prefix = _pytest_redis_key_prefix()
        _scan_del_redis_keys(f"{prefix}market_oracle:*", f"{prefix}santiment:*")
        if (os.environ.get("PYTEST_XDIST_WORKER") or "").strip():
            _cleanup_xdist_worker_stores()
        elif _is_xdist_controller(session):
            _cleanup_xdist_controller_leftovers()
    finally:
        if not _PROGRESS.get("enabled") or not _PROGRESS.get("total"):
            return
        elapsed = _time.time() - (_PROGRESS["t0"] or _time.time())
        print(
            f"\n{'='*60}\n"
            f"  LOCAL suite done  exit={exitstatus}  "
            f"{elapsed:.1f}s\n"
            f"  ok={_PROGRESS['passed']}  fail={_PROGRESS['failed']}  "
            f"err={_PROGRESS['errors']}  skip={_PROGRESS['skipped']}  "
            f"total={_PROGRESS['total']}\n"
            f"  db={resolve_test_db_name()} (local only)\n"
            f"{'='*60}\n",
            flush=True,
        )


@pytest.fixture(autouse=True)
def isolate_bot_logs(tmp_path, monkeypatch):
    """Keep test runs from appending to logs/aria_log.txt while the bot is live."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr("logger.LOG_DIR", str(log_dir))
    monkeypatch.setattr("logger.LOG_FILE", str(log_dir / "aria_log.txt"))
    monkeypatch.setattr("logger.JSON_LOG_FILE", str(log_dir / "aria_log.jsonl"))
    monkeypatch.setattr("logger.DECISIONS_LOG_FILE", str(log_dir / "decisions.jsonl"))


@pytest.fixture
def zero_cost_model(monkeypatch):
    """Frictionless CostModel for legacy bookkeeping tests (#301).

    These tests assert cash flow, weighted entry and position quantity. Since
    #301 every buy/sell carries fees + slippage; the arithmetic of those costs
    is covered by tests/unit/test_costs.py and test_portfolio_service_costs.py.
    Pinning zero costs here keeps the legacy assertions meaningful without
    coupling them to config.json fee tiers. Opt-in — never autouse.
    """
    from core.costs import CostModel, CostParams

    zero = CostParams(fee_maker_pct=0.0, fee_taker_pct=0.0, slippage_pct=0.0)

    def _zero(cls, config=None, *, exchange="gate", market="spot", symbol=None):
        return cls(zero, exchange=exchange, market=market)

    monkeypatch.setattr(CostModel, "from_config", classmethod(_zero))
    yield zero


@pytest.fixture
def hermes_memory_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    from hermes.memory import store

    monkeypatch.setattr(store, "MEMORY_DIR", tmp_path)
    yield tmp_path


@pytest.fixture
def sample_live_trade_history():
    with open(FIXTURES / "live_trade_history.sample.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def sample_positions_live():
    with open(FIXTURES / "positions.live.sample.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def sample_orders_live():
    with open(FIXTURES / "orders.live.sample.json", encoding="utf-8") as f:
        return json.load(f)