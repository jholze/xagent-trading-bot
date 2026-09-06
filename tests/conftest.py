import atexit
import json
import os
import shutil
import socket as _socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# data/*.py are importable packages; do not leave .pyc next to them (#327).
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
sys.dont_write_bytecode = True


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
    from bus.redis_keys import pytest_redis_key_prefix

    prefix = pytest_redis_key_prefix()
    os.environ["REDIS_KEY_PREFIX"] = prefix
    os.environ["OHLCV_CACHE_KEY_PREFIX"] = prefix
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
            keys = list(redis_client.scan_iter(match=f"pytest:{suffix}_gw*", count=200))
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
    """One Redis prefix under pytest (#319/#323/#326), worker-aware (#321)."""
    from bus.redis_keys import pytest_redis_key_prefix

    return pytest_redis_key_prefix()


def _pin_pytest_redis_key_prefix() -> str:
    """Pin REDIS_KEY_PREFIX (OHLCV_CACHE_KEY_PREFIX kept as alias)."""
    prefix = _pytest_redis_key_prefix()
    os.environ["REDIS_KEY_PREFIX"] = prefix
    os.environ["OHLCV_CACHE_KEY_PREFIX"] = prefix
    return prefix


_pin_pytest_redis_key_prefix()


def pytest_configure(config):
    os.environ["PYTEST_RUNNING"] = "1"
    _apply_xdist_worker_db_suffix()
    force_local_test_mongo(dev=False)
    os.environ["MONGODB_DB"] = resolve_test_db_name()
    close_client()
    # Pin before collection so import-time / post-teardown store writes never
    # fall back to the production aria: prefix (#323).
    _pin_pytest_redis_key_prefix()
    # xdist workers may skip pytest_collection_finish; honor UNIT_TEST_PROGRESS here.
    _PROGRESS["enabled"] = _progress_enabled(config)
    _install_process_data_isolation()

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
    # #328 review: purge only after tests that actually touched Mongo. The
    # session start drops the worker DB and every touching test purges on
    # teardown, so the DB is already empty when a test begins. A test that
    # never constructs a client (the vast majority) costs nothing here.
    from storage import mongo_client as _mc

    gen0 = _mc._client_generation
    yield
    if _mc._client is not None or _mc._client_generation != gen0:
        _purge_pytest_mongo()
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


def _purge_pytest_mongo() -> None:
    """Drop collections in this worker's xagent_pytest* DB (not operator DBs)."""
    try:
        from storage.mongo_client import get_client, resolve_test_db_name

        name = (
            os.environ.get("MONGODB_TEST_DB")
            or os.environ.get("MONGODB_DB")
            or resolve_test_db_name()
        )
        if not str(name).startswith("xagent_pytest"):
            return
        db = get_client()[name]
        for coll in db.list_collection_names():
            db.drop_collection(coll)
    except Exception:
        return


def _reset_leaky_module_globals() -> None:
    """Process-wide caches that isolation fixtures did not already cover (#328)."""
    try:
        from storage.order_ledger_v2 import reset_order_ledger_v2_for_tests

        reset_order_ledger_v2_for_tests()
    except Exception:
        pass
    try:
        from services.entry_sensor_loop import reset_poll_state_for_tests

        reset_poll_state_for_tests()
    except Exception:
        pass
    try:
        from strategies.entry_sensor_15m import clear_pending_for_tests

        clear_pending_for_tests()
    except Exception:
        pass
    try:
        from services.market_policy_fusion import reset_degraded_episode_for_tests

        reset_degraded_episode_for_tests()
    except Exception:
        pass
    try:
        from risk.slot_eviction_runtime import reset_rate_limits_for_tests

        reset_rate_limits_for_tests()
    except Exception:
        pass
    try:
        from risk.risk_manager import reset_risk_manager_globals_for_tests

        reset_risk_manager_globals_for_tests()
    except Exception:
        pass
    try:
        from intelligence.memory.cache import invalidate_cache

        invalidate_cache()
    except Exception:
        pass
    try:
        from notifications.daily_portfolio import reset_nav_start_cache_for_tests

        reset_nav_start_cache_for_tests()
    except Exception:
        pass
    try:
        from services.gate_balance import reset_balance_cache_for_tests

        reset_balance_cache_for_tests()
    except Exception:
        pass
    try:
        from data.cmc_market_cap import reset_market_cap_cache_for_tests

        reset_market_cap_cache_for_tests()
    except Exception:
        pass
    try:
        from price_fetcher import clear_price_cache

        clear_price_cache()
    except Exception:
        pass
    try:
        from strategies.watch_15m_state import reset_cache_for_tests

        reset_cache_for_tests()
    except Exception:
        pass
    try:
        from services.architecture_runtime import reset_architecture_runtime_for_tests

        reset_architecture_runtime_for_tests()
    except Exception:
        pass
    try:
        from services.eval_queue_runtime import reset_eval_runtime_for_tests

        reset_eval_runtime_for_tests()
    except Exception:
        pass
    try:
        from notifications.telegram_commands.portfolio_commands import (
            reset_portfolio_commands_for_tests,
        )

        reset_portfolio_commands_for_tests()
    except Exception:
        pass
    try:
        from notifications.telegram_commands.morning_commands import (
            reset_morning_commands_for_tests,
        )

        reset_morning_commands_for_tests()
    except Exception:
        pass
    try:
        from services.telegram_ask_bridge import reset_ask_bridge_for_tests

        reset_ask_bridge_for_tests()
    except Exception:
        pass
    try:
        from core.interactive_priority import reset_interactive_priority_for_tests

        reset_interactive_priority_for_tests()
    except Exception:
        pass
    try:
        from core.cycle_health import reset_cycle_health_for_tests

        reset_cycle_health_for_tests()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def isolate_ohlcv_cache_key_prefix(monkeypatch):
    """Keep pytest Redis keys off the production aria: prefix (#319/#328).

    SCAN+DEL the whole worker prefix (OHLCV, oracle, santiment, and anything
    else tests wrote under ``pytest:<suffix>[_gwN]:``).
    """
    prefix = _pytest_redis_key_prefix()
    monkeypatch.setenv("REDIS_KEY_PREFIX", prefix)
    monkeypatch.setenv("OHLCV_CACHE_KEY_PREFIX", prefix)
    from bus.ohlcv_cache import reset_ohlcv_cache_for_tests
    from services.market_oracle.store import reset_for_tests as reset_ora_store
    from services.santiment.store import reset_for_tests as reset_san_store

    def _purge():
        reset_ohlcv_cache_for_tests()
        reset_ora_store()
        reset_san_store()
        # Whole worker prefix: SCAN of a few hundred keys is cheap (#328).
        _scan_del_redis_keys(f"{prefix}*")

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


# ---------------------------------------------------------------------------
# #327: checkout data/ must stay read-only during the unit suite.
# Process-wide: pytest_configure points _DATA_DIR at a session tmp so
# collection / setUpClass / between-test teardowns cannot hit the checkout.
# Per test: isolate_data_dir overlays tmp_path/data on top of that.
# Tracked seed files (git ls-files data, minus backup dumps and .py) only.
# ---------------------------------------------------------------------------
_DATA_SEED_BYTES: dict[str, bytes] | None = None
_CHECKOUT_ROOT_DIR: str | None = None
_CHECKOUT_DATA_DIR: str | None = None
_SIDECAR_FUNCS_WRAPPED = False


def _is_excluded_data_seed(rel: str) -> bool:
    name = Path(rel).name
    if name.endswith((".py", ".pyc")):
        return True
    if "demo_ledger" in name and "backup" in name:
        return True
    return False


def _tracked_data_relpaths(orig_root: str) -> list[str]:
    try:
        out = subprocess.check_output(
            ["git", "ls-files", "data"],
            cwd=orig_root,
            text=True,
        )
        return [ln.strip() for ln in out.splitlines() if ln.strip()]
    except Exception:
        root = Path(orig_root) / "data"
        if not root.is_dir():
            return []
        return [
            str(p.relative_to(orig_root))
            for p in root.rglob("*")
            if p.is_file()
        ]


def _data_seed_bytes(orig_root: str) -> dict[str, bytes]:
    global _DATA_SEED_BYTES
    if _DATA_SEED_BYTES is not None:
        return _DATA_SEED_BYTES
    payload: dict[str, bytes] = {}
    for rel in _tracked_data_relpaths(orig_root):
        if _is_excluded_data_seed(rel):
            continue
        src = Path(orig_root) / rel
        if src.is_file():
            payload[rel] = src.read_bytes()
    _DATA_SEED_BYTES = payload
    return payload


def _seed_tmp_data(orig_root: str, test_root: Path) -> None:
    payload = _data_seed_bytes(orig_root)
    for rel, blob in payload.items():
        dest = test_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
        # Pre-materialize the demo sibling so get_data_file() does not
        # copy-on-first-use (that path logs via get_bot_config).
        if rel.endswith(".json") and not rel.endswith(".demo.json"):
            demo_rel = rel[:-5] + ".demo.json"
            if demo_rel not in payload:
                demo_dest = test_root / demo_rel
                demo_dest.write_bytes(blob)


def _redirect_rel_data_path(path, data_dir: Path) -> Path:
    """Map CWD-relative ``data/<name>`` onto ``data_dir``."""
    p = Path(path)
    if p.is_absolute():
        return p
    parts = p.parts
    if parts and parts[0] == "data":
        return data_dir.joinpath(*parts[1:])
    return p


def _live_data_dir() -> Path:
    import data_manager

    return Path(data_manager._DATA_DIR)


def _wrap_sidecar_path_functions() -> None:
    """Once per process: CWD-relative data/ builders follow data_manager._DATA_DIR."""
    global _SIDECAR_FUNCS_WRAPPED
    if _SIDECAR_FUNCS_WRAPPED:
        return
    import services.dca_sniper.state as dca_sniper_state
    import services.gainer_universe.store as gainer_store
    import services.telegram_ask_bridge as ask_bridge

    def _sniper_state_path():
        env = (os.environ.get("DCA_SNIPER_STATE_PATH") or "").strip()
        if env:
            return Path(env)
        return _live_data_dir() / "dca_sniper_state.json"

    dca_sniper_state.state_path = _sniper_state_path

    def _gainer_state_path():
        env = (os.environ.get("GAINER_UNIVERSE_STATE_PATH") or "").strip()
        if env:
            return Path(env)
        return _live_data_dir() / "gainer_universe_state.json"

    gainer_store._state_path = _gainer_state_path

    for name in ("queue_path", "pending_notify_path", "notify_log_path", "agent_inbox_path"):
        orig = getattr(ask_bridge, name)

        def _make(fn):
            def _wrapped(*args, **kwargs):
                return _redirect_rel_data_path(fn(*args, **kwargs), _live_data_dir())

            _wrapped.__name__ = getattr(fn, "__name__", "wrapped")
            return _wrapped

        setattr(ask_bridge, name, _make(orig))

    _SIDECAR_FUNCS_WRAPPED = True


def _set_sidecar_path_constants(tmp_root: Path, tmp_data: Path, monkeypatch=None) -> None:
    """Rebind import-time Path constants that do not go through _DATA_DIR."""
    import notifications.coin_links as coin_links
    import notifications.morning_briefing as morning_briefing
    import notifications.telegram_commands.command_context as command_context
    import services.exit_radar.sniper_status as sniper_status
    import services.portfolio_nav_history as portfolio_nav
    import services.telegram_ask_bridge as ask_bridge

    pairs = (
        (command_context, "_CONTEXT_FILE", tmp_data / "telegram_command_context.json"),
        (morning_briefing, "_STATE_FILE", tmp_data / "morning_briefing.json"),
        (coin_links, "_CACHE_PATH", tmp_data / "cmc_slug_cache.json"),
        (portfolio_nav, "_BOT_ROOT", tmp_root),
        (sniper_status, "_REPO_ROOT", tmp_root),
        (ask_bridge, "_DEFAULT_QUEUE", tmp_data / "telegram_ask_queue.json"),
    )
    for mod, attr, value in pairs:
        if monkeypatch is not None:
            monkeypatch.setattr(mod, attr, value)
        else:
            setattr(mod, attr, value)


def _install_process_data_isolation() -> None:
    """Point data_manager at a session tmp before collection (#327)."""
    global _CHECKOUT_ROOT_DIR, _CHECKOUT_DATA_DIR
    import data_manager

    if _CHECKOUT_ROOT_DIR is None:
        _CHECKOUT_ROOT_DIR = data_manager._ROOT_DIR
        _CHECKOUT_DATA_DIR = data_manager._DATA_DIR
    session_root = Path(tempfile.mkdtemp(prefix="xagent_pytest_data_"))
    session_data = session_root / "data"
    session_data.mkdir()
    _seed_tmp_data(_CHECKOUT_ROOT_DIR, session_root)
    data_manager._ROOT_DIR = str(session_root)
    data_manager._DATA_DIR = str(session_data)
    _wrap_sidecar_path_functions()
    _set_sidecar_path_constants(session_root, session_data)
    atexit.register(shutil.rmtree, str(session_root), True)


def _patch_module_data_constants(monkeypatch, tmp_root: Path, tmp_data: Path, request) -> None:
    _wrap_sidecar_path_functions()
    _set_sidecar_path_constants(tmp_root, tmp_data, monkeypatch=monkeypatch)
    # Tests that `from module import _STATE_FILE` keep the original Path object.
    mod = getattr(request, "module", None)
    imported = getattr(mod, "_STATE_FILE", None) if mod is not None else None
    if isinstance(imported, Path) and imported.name == "morning_briefing.json":
        monkeypatch.setattr(mod, "_STATE_FILE", tmp_data / "morning_briefing.json", raising=False)


@pytest.fixture(autouse=True)
def isolate_data_dir(tmp_path, monkeypatch, request):
    """Redirect data_manager._DATA_DIR (and _ROOT_DIR fallback) to tmp_path/data.

    Production copy-on-first-use of resolve_positions_file's demo branch is
    unchanged: get_data_file("positions.json") still copies into _DATA_DIR,
    which is now the per-test tmp dir. Sidecar modules that hard-code data/
    are rebound in the same fixture (#327).
    """
    import data_manager

    checkout_root = _CHECKOUT_ROOT_DIR or data_manager._ROOT_DIR
    checkout_data = _CHECKOUT_DATA_DIR or data_manager._DATA_DIR
    test_root = tmp_path
    test_data = tmp_path / "data"
    test_data.mkdir(exist_ok=True)
    _seed_tmp_data(checkout_root, test_root)
    monkeypatch.setattr(data_manager, "_ROOT_DIR", str(test_root))
    monkeypatch.setattr(data_manager, "_DATA_DIR", str(test_data))
    _patch_module_data_constants(monkeypatch, test_root, test_data, request)
    try:
        yield {
            "orig_root": checkout_root,
            "orig_data": checkout_data,
            "test_root": test_root,
            "test_data": test_data,
        }
    finally:
        try:
            from strategies.positions import _cancel_flush_timer

            _cancel_flush_timer()
        except Exception:
            pass
        try:
            from strategies.watch_15m_state import reset_cache_for_tests

            reset_cache_for_tests()
        except Exception:
            pass


@pytest.fixture(autouse=True)
def isolate_demo_ledger_files(tmp_path, monkeypatch, isolate_data_dir):
    """Keep unit tests from mutating operator orders.demo.json (XRVM etc.)."""
    import data_manager
    import storage.ledger_router as ledger_router

    # #325: every scope of every ledger table gets its own per-test copy. A
    # single suite run used to write orders.live.json, orders.paper.json,
    # positions.paper.json, trade_history.json and live_trade_history(.demo).json
    # into data/ — and xdist workers clobbered each other in those files.
    # get_data_file()/resolve_data_path() return explicit paths unchanged, so
    # the tmp copies are used as-is by both data_manager and the JSON store.
    orig_data = isolate_data_dir["orig_data"]
    orig_root = isolate_data_dir["orig_root"]
    test_data = isolate_data_dir["test_data"]

    def _src_for(name: str) -> str | None:
        for base in (orig_data, orig_root):
            cand = os.path.join(base, name)
            if os.path.exists(cand):
                return cand
        return None

    def _tmp_scope_file(name: str, default: dict) -> str:
        dst = str(tmp_path / name)
        src = _src_for(name)
        if src:
            shutil.copy2(src, dst)
        else:
            Path(dst).write_text(json.dumps(default), encoding="utf-8")
        return dst

    # resolve_positions_file("demo") ignores the scope table and calls
    # get_data_file("positions.json") (copy-on-first-use). Seed the tmp data
    # dir so that copy still happens, just not in the checkout.
    for name in ("positions.json", "positions.demo.json"):
        src = _src_for(name)
        if src:
            shutil.copy2(src, test_data / name)

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
    from strategies.positions import (
        _cancel_flush_timer,
        clear_positions_memory,
        load_positions,
        reset_all_position_stores_for_tests,
    )

    reset_all_position_stores_for_tests()
    try:
        load_positions(resolve_ledger_scope())
    except Exception:
        pass
    yield
    # A debounced flush_positions() timer left by the test would fire after the
    # monkeypatches are gone and write the real data/positions.*.json (#325).
    _cancel_flush_timer()
    reset_all_position_stores_for_tests()


@pytest.fixture(autouse=True)
def reset_cross_test_process_state(isolate_demo_ledger_files):
    """Reset per-worker Redis/v2/module leftovers that survive JSON isolation (#328)."""
    _reset_leaky_module_globals()
    yield
    _reset_leaky_module_globals()


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

    # Reload from disk so a previous test's nested mutation of _config_cache
    # cannot leak max_daily_dca_usdt (and friends) into this test (#328).
    data_manager._config_cache = None
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


def _sessionfinish_thread_report() -> None:
    """Warn about leftover app threads after the last test (#329).

    Skips the main thread, pytest/xdist/execnet internals, and pymongo
    topology workers (those die after close_client(); they are not app leaks).
    """
    leftover = []
    main = threading.main_thread()
    for thread in threading.enumerate():
        if thread is main or not thread.is_alive():
            continue
        name = thread.name or ""
        lname = name.lower()
        if name.startswith("Dummy-") or name.startswith("execnet"):
            continue
        if "pytest" in lname or "xdist" in lname:
            continue
        if name.startswith("pymongo_") or lname.startswith("pymongo"):
            continue
        leftover.append(thread)
    if leftover:
        print(
            "WARNING: non-main threads still alive at pytest session end:",
            flush=True,
        )
        for thread in leftover:
            print(
                f"  name={thread.name!r} daemon={thread.daemon}",
                flush=True,
            )
    else:
        print(
            "pytest_sessionfinish: no leftover non-main threads",
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
        from services.architecture_runtime import reset_architecture_runtime_for_tests

        reset_architecture_runtime_for_tests()
    except Exception:
        pass
    try:
        close_client()
    except Exception:
        pass
    try:
        _sessionfinish_thread_report()
    except Exception:
        pass
    try:
        prefix = _pytest_redis_key_prefix()
        _scan_del_redis_keys(f"{prefix}*")
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