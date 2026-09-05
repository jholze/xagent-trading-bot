import json
import locale
import os
import shutil
import tempfile
from datetime import datetime

from logger import log
from storage.errors import LedgerUnavailable, LedgerWriteFailed
from storage.mongo_client import get_database

_ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_ROOT_DIR, "data")


def is_demo_mode() -> bool:
    """Returns True if the bot is running in demo mode (--demo flag or pytest)."""
    return os.environ.get("DEMO_MODE", "0") == "1"


def project_root() -> str:
    return _ROOT_DIR


def data_dir() -> str:
    return _DATA_DIR


def _is_explicit_path(base_name: str) -> bool:
    return os.path.isabs(base_name) or bool(os.path.dirname(base_name))


def resolve_data_path(base_name: str) -> str:
    """Map a JSON basename to ``data/<name>``, with repo-root fallback.

    Absolute paths and paths that already include a directory (tests, tmp)
    are returned unchanged. Missing files still resolve to ``data/`` so new
    writes land there.
    """
    if _is_explicit_path(base_name):
        return base_name
    name = os.path.basename(base_name)
    preferred = os.path.join(_DATA_DIR, name)
    if os.path.exists(preferred):
        return preferred
    legacy = os.path.join(_ROOT_DIR, name)
    if os.path.exists(legacy):
        return legacy
    return preferred


def iter_data_paths(base_name: str):
    """Yield canonical then legacy locations for a basename."""
    if _is_explicit_path(base_name):
        yield base_name
        return
    name = os.path.basename(base_name)
    yield os.path.join(_DATA_DIR, name)
    yield os.path.join(_ROOT_DIR, name)


def find_existing_data_file(*names: str) -> str | None:
    for name in names:
        for path in iter_data_paths(name):
            if os.path.exists(path):
                return path
    return None


def _demo_variant(path: str) -> str:
    if path.endswith(".demo.json"):
        return path
    if path.endswith(".json"):
        return path[:-5] + ".demo.json"
    return path + ".demo.json"


def get_data_file(base_name: str) -> str:
    """Return the JSON path for this process (demo suffix + ``data/`` folder).

    If in demo mode and the ``.demo.json`` does not exist yet, copy the real
    file as a starting point.
    """
    if not is_demo_mode():
        return resolve_data_path(base_name)

    if _is_explicit_path(base_name):
        demo_path = _demo_variant(base_name)
        if not os.path.exists(demo_path) and os.path.exists(base_name):
            try:
                shutil.copy2(base_name, demo_path)
                log(f"Created demo file from existing data: {demo_path}", "INFO")
            except Exception as e:
                log(f"Could not copy {base_name} to {demo_path}: {e}", "WARNING")
        return demo_path

    raw_name = os.path.basename(base_name)
    demo_name = os.path.basename(_demo_variant(raw_name))
    real_name = (
        demo_name.replace(".demo.json", ".json")
        if demo_name.endswith(".demo.json")
        else raw_name
    )
    demo_path = resolve_data_path(demo_name)
    if not os.path.exists(demo_path):
        src = resolve_data_path(real_name)
        if os.path.exists(src) and os.path.abspath(src) != os.path.abspath(demo_path):
            try:
                os.makedirs(os.path.dirname(demo_path) or ".", exist_ok=True)
                shutil.copy2(src, demo_path)
                log(f"Created demo file from existing data: {demo_path}", "INFO")
            except Exception as e:
                log(f"Could not copy {src} to {demo_path}: {e}", "WARNING")
    return demo_path if os.path.exists(demo_path) else os.path.join(_DATA_DIR, demo_name)


def _mongo_test_mode(config: dict | None = None) -> bool:
    from storage.mongo_client import use_isolated_pytest_database
    return use_isolated_pytest_database(config)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _raise_ledger_unavailable(
    op: str,
    cause: BaseException | None = None,
    *,
    scope: str | None = None,
    tenant_id: str | None = None,
    message: str = "",
) -> None:
    if isinstance(cause, LedgerUnavailable):
        raise cause
    detail = message or (str(cause) if cause is not None else "unavailable")
    log(f"{op} failed (scope={scope}, tenant={tenant_id}): {detail}", "ERROR")
    raise LedgerUnavailable(
        detail, scope=scope, tenant_id=tenant_id, op=op, cause=cause
    ) from cause


def _raise_ledger_write_failed(
    op: str,
    cause: BaseException | None = None,
    *,
    scope: str | None = None,
    tenant_id: str | None = None,
    message: str = "",
) -> None:
    if isinstance(cause, LedgerWriteFailed):
        raise cause
    detail = message or (str(cause) if cause is not None else "write failed")
    log(f"{op} failed (scope={scope}, tenant={tenant_id}): {detail}", "ERROR")
    raise LedgerWriteFailed(
        detail, scope=scope, tenant_id=tenant_id, op=op, cause=cause
    ) from cause


def _should_use_mongo_for_tenant_config(config: dict = None) -> bool:
    """Mirror ledger logic: use mongo for tenant meta when backend is mongo."""
    try:
        from storage.ledger_router import resolve_ledger_backend, ledger_dual_write_enabled
        cfg = config or {}
        if ledger_dual_write_enabled(cfg):
            return True
        if is_demo_mode():
            return resolve_ledger_backend("demo", cfg) == "mongo"
        be = resolve_ledger_backend("paper", cfg)
        return be == "mongo"
    except Exception:
        return False  # safe fallback


def atomic_write_json(path: str, data: dict):
    """Write JSON atomically (temp file in the same dir, then replace).

    Unique tmp names so concurrent writers (WQE + tenants at boot) do not
    steal each other's ``path.tmp`` (Railway: ENOENT on os.replace).
    """
    dir_name = os.path.dirname(path) or "."
    os.makedirs(dir_name, exist_ok=True)

    fd = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=os.path.basename(path) + ".",
            suffix=".tmp",
            dir=dir_name,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    except Exception as e:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        log(f"Atomic write failed for {path}: {e}", "ERROR")
        raise

WATCHLIST_FILE = "watchlist.json"
DRY_RUN_OVERLAY_FILE = "watchlist.dry_run_overlay.json"
CMC_TRENDING_OVERLAY_FILE = "watchlist.cmc_trending_overlay.json"
DRY_RUN_EXPANSION_FILE = "watchlist.dry_run_expansion.json"


def is_live_dry_run(config: dict = None) -> bool:
    """True when live mode with dry_run ON — orders stay in the local ledger."""
    cfg = config or get_config()
    if cfg.get("trading_mode") != "live":
        return False
    return bool(cfg.get("live", {}).get("dry_run", True))


def is_dry_run_enhanced(config: dict = None) -> bool:
    """True when dry_run_enhanced is on and (live dry-run or demo mode).

    Never true when live.dry_run is false (real live trading).
    """
    cfg = config or get_config()
    live = cfg.get("live", {})
    if not live.get("dry_run", True):
        return False
    if not live.get("dry_run_enhanced", False):
        return False
    return is_live_dry_run(cfg) or is_demo_mode()


def uses_simulated_live_portfolio(config: dict = None) -> bool:
    """Portfolio cash/positions come from the local ledger, not Gate balances."""
    from core.simulated_trading import uses_simulated_portfolio

    return uses_simulated_portfolio(config)


def uses_watchlist_expansion(config: dict = None) -> bool:
    """Extra watchlist coins apply in demo mode and live dry-run only."""
    if is_demo_mode():
        return True
    return is_live_dry_run(config)


def simulated_balance_usdt(config: dict = None) -> float:
    cfg = config or get_config()
    return float(cfg.get("live", {}).get("simulated_balance_usdt", 5000))


def load_watchlist(tenant_id: str | None = None):
    """Thin dispatcher for watchlist. Uses pure default + tenant_meta_store for tenants."""
    from core.tenant_context import resolve_tenant_id, DEFAULT_TENANT
    tid = resolve_tenant_id(tenant_id)
    if tid != DEFAULT_TENANT:
        default_cfg = _load_default_config_from_disk()
        try:
            use_mongo = _should_use_mongo_for_tenant_config(default_cfg)
        except Exception:
            use_mongo = False
        if use_mongo:
            try:
                from storage import tenant_meta_store as _tms
                coins = _tms.load_tenant_watchlist(tid, default_cfg=default_cfg, test=_mongo_test_mode(default_cfg))
                if coins:
                    return coins
            except Exception as e:
                log(f"Failed tenant_meta_store load_tenant_watchlist for {tid}: {e}", "WARNING")
    # default or fallback
    path = get_data_file(WATCHLIST_FILE)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            coins = data.get("coins", [])
            seen = set()
            unique = []
            for c in coins:
                s = c.get("symbol", "")
                if s and s not in seen:
                    seen.add(s)
                    unique.append(c)
            return unique
    except Exception as e:
        log(f"Failed to load watchlist from {path}: {e}", "WARNING")
        return []


def save_watchlist(coins, tenant_id: str | None = None):
    """Thin dispatcher. Delegates tenant watchlist to meta_store when mongo backend."""
    from core.tenant_context import resolve_tenant_id, DEFAULT_TENANT
    tid = resolve_tenant_id(tenant_id)
    if tid != DEFAULT_TENANT:
        default_cfg = _load_default_config_from_disk()
        try:
            use_mongo = _should_use_mongo_for_tenant_config(default_cfg)
        except Exception:
            use_mongo = False
        if not use_mongo:
            return True
        try:
            from storage import tenant_meta_store as _tms
            return _tms.save_tenant_watchlist(tid, coins, default_cfg=default_cfg, test=_mongo_test_mode(default_cfg))
        except Exception as e:
            log(f"Failed tenant_meta_store save_tenant_watchlist for {tid}: {e}", "WARNING")
            return False
    # default
    path = get_data_file(WATCHLIST_FILE)
    try:
        atomic_write_json(path, {"coins": coins})
        return True
    except Exception as e:
        log(f"Failed to save watchlist: {e}", "ERROR")
        return False


def _normalize_watchlist_symbol(symbol: str) -> str:
    sym = (symbol or "").upper().strip()
    if sym and "/" not in sym:
        return f"{sym}/USDT"
    return sym


def _watchlist_symbol(coin: dict) -> str:
    return _normalize_watchlist_symbol(coin.get("symbol", ""))


def add_coin(symbol):
    """Einfaches Hinzufügen eines Coins"""
    symbol = _normalize_watchlist_symbol(symbol)

    if any(_watchlist_symbol(c) == symbol for c in load_effective_watchlist()):
        return False, f"{symbol} ist bereits in der Watchlist."

    coins = load_watchlist()
    new_coin = {
        "symbol": symbol,
        "ticker": symbol.split("/")[0],
        "name": symbol.split("/")[0],
        "timeframe": "4h",
        "active": True,
    }
    coins.append(new_coin)
    save_watchlist(coins)
    return True, f"✅ {symbol} wurde zur Watchlist hinzugefügt."


def save_dry_run_expansion(data: dict) -> bool:
    path = get_data_file(DRY_RUN_EXPANSION_FILE)
    try:
        atomic_write_json(path, data)
        return True
    except Exception:
        return False


def remove_coin(symbol):
    """Remove a coin from whichever watchlist JSON currently provides it."""
    symbol = _normalize_watchlist_symbol(symbol)

    coins = load_watchlist()
    new_coins = [c for c in coins if _watchlist_symbol(c) != symbol]
    if len(new_coins) < len(coins):
        save_watchlist(new_coins)
        return True, f"✅ {symbol} wurde entfernt."

    if uses_watchlist_expansion():
        expansion = load_dry_run_expansion()
        exp_coins = expansion.get("coins", [])
        new_exp = [c for c in exp_coins if _watchlist_symbol(c) != symbol]
        if len(new_exp) < len(exp_coins):
            expansion["coins"] = new_exp
            save_dry_run_expansion(expansion)
            return True, f"✅ {symbol} wurde entfernt."

    if is_dry_run_enhanced():
        overlay = load_dry_run_overlay()
        overlay_coins = overlay.get("coins", [])
        new_overlay = [c for c in overlay_coins if _watchlist_symbol(c) != symbol]
        if len(new_overlay) < len(overlay_coins):
            overlay["coins"] = new_overlay
            save_dry_run_overlay(overlay)
            return True, f"✅ {symbol} wurde entfernt."

    if trending_watchlist_live_enabled():
        overlay = load_cmc_trending_overlay()
        overlay_coins = overlay.get("coins", [])
        new_overlay = [c for c in overlay_coins if _watchlist_symbol(c) != symbol]
        if len(new_overlay) < len(overlay_coins):
            overlay["coins"] = new_overlay
            save_cmc_trending_overlay(overlay)
            return True, f"✅ {symbol} wurde entfernt."

    return False, f"{symbol} nicht in der Watchlist gefunden."


def list_coins():
    """Gibt alle Coins zurück"""
    return load_effective_watchlist()


def load_dry_run_expansion():
    path = get_data_file(DRY_RUN_EXPANSION_FILE)
    if not os.path.exists(path):
        return {"coins": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"Failed to load {path}: {e}", "WARNING")
        return {"coins": []}


def load_dry_run_overlay():
    path = get_data_file(DRY_RUN_OVERLAY_FILE)
    if not os.path.exists(path):
        return {"refreshed_at": "", "source": "", "coins": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"Failed to load {path}: {e}", "WARNING")
        return {"refreshed_at": "", "source": "", "coins": []}


def save_dry_run_overlay(data: dict) -> bool:
    path = get_data_file(DRY_RUN_OVERLAY_FILE)
    try:
        atomic_write_json(path, data)
        return True
    except Exception:
        return False


def load_cmc_trending_overlay():
    path = get_data_file(CMC_TRENDING_OVERLAY_FILE)
    if not os.path.exists(path):
        return {"refreshed_at": "", "source": "", "coins": [], "added": [], "removed": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"Failed to load {path}: {e}", "WARNING")
        return {"refreshed_at": "", "source": "", "coins": [], "added": [], "removed": []}


def save_cmc_trending_overlay(data: dict) -> bool:
    path = get_data_file(CMC_TRENDING_OVERLAY_FILE)
    try:
        atomic_write_json(path, data)
        return True
    except Exception:
        return False


def trending_watchlist_live_enabled(config: dict = None) -> bool:
    cfg = config or get_config()
    tw = cfg.get("cmc", {}).get("trending_watchlist") or cfg.get("live", {}).get("trending_watchlist") or {}
    return bool(tw.get("enabled", True) and tw.get("live_enabled", False))


def _dedupe_watchlist_coins(coins: list) -> list:
    seen = set()
    unique = []
    for c in coins:
        sym = c.get("symbol", "")
        if sym and sym not in seen:
            seen.add(sym)
            unique.append(c)
    return unique


def prune_watchlist_coins_gate_only(
    coins: list,
    *,
    gate_prices: dict | None = None,
    gate_only: bool = True,
) -> tuple[list, list[str]]:
    """Drop coins not listed on the configured exchange (legacy name kept for compat)."""
    if not gate_only or not coins:
        return list(coins), []
    from price_fetcher import get_gate_prices_batch, passes_exchange_filter
    from core.config import get_bot_config

    symbols = [c.get("symbol") for c in coins if c.get("symbol")]
    prices = gate_prices if gate_prices is not None else get_gate_prices_batch(symbols)
    ex = get_bot_config().exchange
    cfg = {"gate_only": True, "exchange_only": True}
    kept, removed = [], []
    for coin in coins:
        sym = coin.get("symbol", "")
        if not sym:
            continue
        ok, _ = passes_exchange_filter(sym, cfg, exchange=ex, price=prices.get(sym, 0))
        if ok:
            kept.append(coin)
        else:
            removed.append(sym)
    return kept, removed


def prune_non_gate_watchlist_sources(config: dict = None) -> dict:
    """
    Remove non-Gate coins from overlay/expansion JSON (and optionally base watchlist).
    Idempotent; safe to call each bot cycle when gate_only is enabled.
    """
    from core.config import BotConfig, get_bot_config

    bot_cfg = BotConfig(raw=config) if config is not None else get_bot_config()
    cfg = bot_cfg.raw
    tw = bot_cfg.trending_watchlist_config
    if not tw.get("gate_only", True) or not tw.get("prune_non_gate", True):
        return {"removed": [], "by_source": {}}

    all_symbols: list[str] = []
    sources: list[tuple[str, list]] = []

    if tw.get("prune_base_watchlist", False):
        base = load_watchlist()
        sources.append(("base", base))
        all_symbols.extend(c.get("symbol") for c in base if c.get("symbol"))

    if uses_watchlist_expansion(cfg):
        expansion = load_dry_run_expansion()
        exp_coins = expansion.get("coins", [])
        sources.append(("expansion", exp_coins))
        all_symbols.extend(c.get("symbol") for c in exp_coins if c.get("symbol"))

    if is_dry_run_enhanced(cfg):
        overlay = load_dry_run_overlay()
        ov_coins = overlay.get("coins", [])
        sources.append(("dry_run_overlay", ov_coins))
        all_symbols.extend(c.get("symbol") for c in ov_coins if c.get("symbol"))

    if trending_watchlist_live_enabled(cfg):
        overlay = load_cmc_trending_overlay()
        ov_coins = overlay.get("coins", [])
        sources.append(("cmc_trending_overlay", ov_coins))
        all_symbols.extend(c.get("symbol") for c in ov_coins if c.get("symbol"))

    if not all_symbols:
        return {"removed": [], "by_source": {}}

    from price_fetcher import get_gate_prices_batch

    gate_prices = get_gate_prices_batch(list(dict.fromkeys(all_symbols)))
    removed_all: list[str] = []
    by_source: dict[str, list[str]] = {}

    for label, coins in sources:
        kept, removed = prune_watchlist_coins_gate_only(
            coins,
            gate_prices=gate_prices,
            gate_only=True,
        )
        if not removed:
            continue
        by_source[label] = removed
        removed_all.extend(removed)
        if label == "base":
            save_watchlist(kept)
        elif label == "expansion":
            expansion = load_dry_run_expansion()
            expansion["coins"] = kept
            save_dry_run_expansion(expansion)
        elif label == "dry_run_overlay":
            overlay = load_dry_run_overlay()
            overlay["coins"] = kept
            overlay.setdefault("gate_pruned", []).extend(removed)
            save_dry_run_overlay(overlay)
        elif label == "cmc_trending_overlay":
            overlay = load_cmc_trending_overlay()
            overlay["coins"] = kept
            overlay.setdefault("gate_pruned", []).extend(removed)
            save_cmc_trending_overlay(overlay)

    if removed_all:
        log(
            f"Gate watchlist prune: -{len(removed_all)} coins "
            f"({', '.join(f'{k}:{len(v)}' for k, v in by_source.items())})",
            "INFO",
        )
    return {"removed": removed_all, "by_source": by_source}


def build_merged_watchlist_coins(
    tenant_id: str | None = None,
    *,
    config: dict | None = None,
    apply_wqe: bool = True,
) -> list:
    """Merge base + expansion + overlays + Gate/profile filters (+ optional WQE soft/enforce).

    This is the raw observe candidate set *before* universe observe_max cap.
    """
    cfg = config if config is not None else load_config(tenant_id=tenant_id)
    coins = list(load_watchlist(tenant_id=tenant_id))
    base_syms = {c.get("symbol") for c in coins if c.get("symbol")}
    if uses_watchlist_expansion(cfg):
        coins = _dedupe_watchlist_coins(coins + load_dry_run_expansion().get("coins", []))
    if is_dry_run_enhanced(cfg):
        coins = _dedupe_watchlist_coins(coins + load_dry_run_overlay().get("coins", []))

    from core.coin_eligibility import filter_watchlist_coins, should_include_trending_overlay
    from core.config import get_bot_config

    include_trending = (
        trending_watchlist_live_enabled(cfg)
        and should_include_trending_overlay(cfg)
    )
    if include_trending:
        coins = _dedupe_watchlist_coins(coins + load_cmc_trending_overlay().get("coins", []))

    tw = get_bot_config(tenant_id=tenant_id).trending_watchlist_config
    if tw.get("exchange_only", tw.get("gate_only", True)):
        kept, _ = prune_watchlist_coins_gate_only(coins)
        coins = kept

    coins = filter_watchlist_coins(coins, cfg)

    if apply_wqe:
        try:
            from services.watchlist_quality.config import wqe_mode
            from services.watchlist_quality.runtime import apply_wqe_to_watchlist

            mode = wqe_mode(cfg)
            if mode in ("soft", "enforce"):
                coins = apply_wqe_to_watchlist(
                    coins,
                    config=cfg,
                    base_symbols=base_syms,
                    tenant_id=tenant_id or "default",
                )
        except Exception as e:
            log(f"WQE watchlist transform skipped: {e}", "DEBUG")

    return coins


def load_effective_watchlist(tenant_id: str | None = None):
    """Observe universe (broad when universe.split_enabled).

    Used for memory, WQE scoring, listings, Telegram /list.
    Trade scan uses ``load_trade_watchlist`` when split is on.
    """
    try:
        from services.universe.split import load_observe_universe, universe_split_enabled

        cfg = load_config(tenant_id=tenant_id)
        if universe_split_enabled(cfg):
            return load_observe_universe(tenant_id=tenant_id, config=cfg)
    except Exception as e:
        log(f"observe universe failed, fallback merge: {e}", "DEBUG")
    return build_merged_watchlist_coins(tenant_id=tenant_id)


def load_trade_watchlist(
    tenant_id: str | None = None,
    *,
    observe_coins: list | None = None,
    open_positions: list | None = None,
):
    """Trade-eligible coins for process_coin / BUY path (positions always kept)."""
    try:
        from services.universe.split import load_trade_universe, universe_split_enabled

        cfg = load_config(tenant_id=tenant_id)
        if not universe_split_enabled(cfg):
            return list(observe_coins) if observe_coins is not None else load_effective_watchlist(
                tenant_id=tenant_id
            )
        open_syms = None
        if open_positions is not None:
            open_syms = set()
            for p in open_positions:
                if isinstance(p, dict):
                    s = p.get("symbol")
                else:
                    s = getattr(p, "symbol", None)
                if s:
                    open_syms.add(str(s).strip())
        return load_trade_universe(
            tenant_id=tenant_id,
            observe_coins=observe_coins,
            open_symbols=open_syms,
            config=cfg,
        )
    except Exception as e:
        log(f"trade universe failed, fallback effective: {e}", "DEBUG")
        return list(observe_coins) if observe_coins is not None else load_effective_watchlist(
            tenant_id=tenant_id
        )


def save_full_coin(coin_data):
    coins = load_watchlist()
    coins = [c for c in coins if c.get("symbol") != coin_data.get("symbol")]
    coins.append(coin_data)
    save_watchlist(coins)
    return (
        True,
        f"✅ {coin_data.get('name', coin_data.get('symbol'))} wurde hinzugefügt.",
    )


def _load_default_config_from_disk():
    """Recursion-free loader for the base/default config only.
    Must NEVER call get_config, load_config, resolve_*, or anything that can re-enter tenant paths.
    Used for default cache and for feeding tenant_meta decisions.
    """
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"Failed to load config.json for default, using hardcoded defaults: {e}", "WARNING")
        return {
            "virtual_trading": True,
            "initial_capital_usdt": 5000,
            "max_usdt_per_trade": 150,
            "stop_loss_pct": 12.0,
            "max_open_positions": 5,
            "debug": False,
            "x_accounts": ["CryptoCapo_", "Pentosh1"],
            "min_x_confidence": 65,
            "x_weight": 0.45,
            "technical_weight": 0.35,
            "onchain_weight": 0.2,
            "max_daily_trades": 5,
            "strategies": []
        }


def _apply_trading_profile_merge(default_cfg: dict, tenant_body: dict | None) -> dict:
    from core.trading_profiles import apply_effective_config
    return apply_effective_config(default_cfg, tenant_body)


def _load_tenant_config_body(tid: str, default_cfg: dict) -> dict | None:
    from storage import tenant_meta_store as _tms

    try:
        return _tms.load_tenant_config_body(
            tid, default_cfg=default_cfg, test=_mongo_test_mode(default_cfg)
        )
    except LedgerUnavailable:
        raise
    except Exception as e:
        _raise_ledger_unavailable("load_tenant_config_body", e, tenant_id=tid)


def load_config(tenant_id: str | None = None):
    """Thin dispatcher.
    - default path: use pure disk loader + cache
    - tenant: resolve tid, get default_cfg via pure loader, delegate to tenant_meta_store if mongo backend
    Never calls get_config from inside tenant decision paths.
    """
    from core.tenant_context import resolve_tenant_id, DEFAULT_TENANT
    default_cfg = _load_default_config_from_disk()
    tid = resolve_tenant_id(tenant_id)
    if tid != DEFAULT_TENANT:
        tenant_body = None
        try:
            use_mongo = _should_use_mongo_for_tenant_config(default_cfg)
        except Exception:
            use_mongo = False
        if use_mongo:
            tenant_body = _load_tenant_config_body(tid, default_cfg)
        return _apply_trading_profile_merge(default_cfg, tenant_body)
    # default
    global _config_cache
    if _config_cache is None:
        _config_cache = _apply_trading_profile_merge(default_cfg, None)
    return _config_cache


# Simple module-level cache to avoid repeated disk reads
_config_cache = None


def get_config(tenant_id: str | None = None):
    """Thin dispatcher.
    - explicit or ctx non-default: delegate to load_config (which uses pure default + meta store)
    - default: use cached pure disk loader
    """
    global _config_cache
    from core.tenant_context import resolve_tenant_id, DEFAULT_TENANT
    if tenant_id is not None:
        if tenant_id != DEFAULT_TENANT:
            return load_config(tenant_id=tenant_id)
        # explicit default
        if _config_cache is None:
            _config_cache = _apply_trading_profile_merge(_load_default_config_from_disk(), None)
        return _config_cache
    # no explicit arg: respect ctx via load_config (which will handle tenant or default)
    tid = resolve_tenant_id(None)
    if tid != DEFAULT_TENANT:
        return load_config(tenant_id=tid)
    if _config_cache is None:
        _config_cache = _apply_trading_profile_merge(_load_default_config_from_disk(), None)
    return _config_cache


def reload_config(tenant_id: str | None = None):
    """Forces reload. For tenant: always fresh load."""
    global _config_cache
    from core.tenant_context import resolve_tenant_id, DEFAULT_TENANT
    tid = resolve_tenant_id(tenant_id)
    if tid != DEFAULT_TENANT:
        return load_config(tenant_id=tid)
    _config_cache = None
    return load_config()


def save_config(config, tenant_id: str | None = None):
    global _config_cache
    from core.tenant_context import resolve_tenant_id, DEFAULT_TENANT
    tid = resolve_tenant_id(tenant_id)
    if tid != DEFAULT_TENANT:
        default_cfg = _load_default_config_from_disk()
        try:
            use_mongo = _should_use_mongo_for_tenant_config(default_cfg)
        except Exception:
            use_mongo = False
        if not use_mongo:
            return True
        try:
            from storage import tenant_meta_store as _tms
            ok = _tms.save_tenant_config(tid, config, default_cfg=default_cfg, test=_mongo_test_mode(default_cfg))
            if ok:
                # do not touch the default cache
                return True
            return False
        except Exception as e:
            log(f"Failed tenant_meta_store save_tenant_config for {tid}: {e}", "WARNING")
            return False
    # default
    path = "config.json"
    try:
        atomic_write_json(path, config)
        _config_cache = None
        return True
    except Exception:
        return False


def load_x_accounts():
    path = get_data_file("x_accounts.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"Failed to load {path}: {e}", "WARNING")
        return []


def save_x_accounts(accounts):
    path = get_data_file("x_accounts.json")
    try:
        atomic_write_json(path, accounts)
        return True
    except Exception:
        return False


def load_x_posts():
    path = get_data_file("x_posts.json")
    if not os.path.exists(path):
        return {"posts": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"Failed to load {path}: {e}", "WARNING")
        return {"posts": []}


def save_x_posts(data):
    path = get_data_file("x_posts.json")
    try:
        atomic_write_json(path, data)
        return True
    except Exception:
        return False


def load_demo_data():
    """Load realistic demo data for testing.
    Only has an effect when running with --demo.
    Writes into the .demo.json files.
    """
    if not is_demo_mode():
        log("load_demo_data() called without --demo. Doing nothing.", "WARNING")
        return

    log("Loading demo data into .demo.json files...", "INFO")
    # Demo watchlist with top coins
    watchlist = [
        {"symbol": "ARIA/USDT", "ticker": "ARIA", "name": "Aria AI", "active": True},
        {"symbol": "RAVE/USDT", "ticker": "RAVE", "name": "RaveDAO", "active": True},
        {"symbol": "HIGH/USDT", "ticker": "HIGH", "name": "Highstreet", "active": True},
        {"symbol": "SOL/USDT", "ticker": "SOL", "name": "Solana", "active": True},
        {"symbol": "BTC/USDT", "ticker": "BTC", "name": "Bitcoin", "active": True}
    ]
    save_watchlist(watchlist)

    # Demo positions with realistic average entry and PnL
    # We write via the normal positions module so it respects demo mode
    try:
        from decimal import Decimal
        from strategies.positions import positions as pos_dict, save_positions, get_key
        # Clear existing demo positions first
        pos_dict.clear()
        # Add demo positions
        for key, p in {
            "ARIA_USDT_4h": {"amount": 2150.75, "sold_percent": 0.0, "average_entry": 0.0523, "realized_pnl": 45.2, "last_buy_price": 0.0523, "last_ampel": "🟢", "last_rsi": 32.4, "last_action": "BUY"},
            "RAVE_USDT_4h": {"amount": 875.4, "sold_percent": 0.25, "average_entry": 0.481, "realized_pnl": 128.7, "last_buy_price": 0.481, "last_ampel": "🟡", "last_rsi": 52.1, "last_action": "SELL"},
            "HIGH_USDT_4h": {"amount": 1240.0, "sold_percent": 0.0, "average_entry": 0.172, "realized_pnl": -67.3, "last_buy_price": 0.172, "last_ampel": "🟢", "last_rsi": 41.8, "last_action": "BUY"},
        }.items():
            pos_dict[key] = {k: Decimal(str(v)) if k == "amount" else v for k, v in p.items()}
        save_positions()
    except Exception as e:
        log(f"Could not seed demo positions: {e}", "WARNING")

    # Demo trade history - use the normal save function (respects demo mode)
    trades = {
        "virtual_balance": 4250.75,
        "realized_pnl": 320.4,
        "open_positions": 3,
        "trades": [
            {"type": "BUY", "symbol": "ARIA/USDT", "price": 0.0523, "amount": 2150.75, "usdt_amount": 112.5, "timestamp": "2026-05-23T10:15:00"},
            {"type": "BUY", "symbol": "HIGH/USDT", "price": 0.172, "amount": 1240.0, "usdt_amount": 213.28, "timestamp": "2026-05-23T10:20:00"},
            {"type": "SELL", "symbol": "RAVE/USDT", "price": 0.62, "amount": 218.85, "usdt_received": 135.69, "pnl": 32.4, "timestamp": "2026-05-23T10:45:00"},
        ]
    }
    save_trade_history(trades)

    print("Demo data loaded for testing (into .demo.json files).")



TRADE_HISTORY_FILE = "trade_history.json"
LIVE_TRADE_HISTORY_FILE = "live_trade_history.json"

TRADE_HISTORY_SCOPE_FILES = {
    "paper": TRADE_HISTORY_FILE,
    "live": LIVE_TRADE_HISTORY_FILE,
    "demo": LIVE_TRADE_HISTORY_FILE,
}


def _default_trade_history(scope: str = "paper", config: dict = None) -> dict:
    cfg = config or get_config()
    if scope == "live":
        return {"trades": [], "total_pnl": 0.0, "realized_pnl": 0.0}
    from core.portfolio_baseline import initial_capital

    initial = initial_capital(scope=scope, config=cfg)
    return {
        "virtual_balance": initial,
        "realized_pnl": 0.0,
        "open_positions": 0,
        "trades": [],
    }


def _load_trade_history_json(scope: str = "paper", config: dict = None) -> dict:
    path = get_data_file(TRADE_HISTORY_SCOPE_FILES.get(scope, TRADE_HISTORY_FILE))
    if not os.path.exists(path):
        return _default_trade_history(scope, config)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        _raise_ledger_unavailable("load_trade_history_json", e, scope=scope)


def _save_trade_history_json(data: dict, scope: str = "paper") -> bool:
    path = get_data_file(TRADE_HISTORY_SCOPE_FILES.get(scope, TRADE_HISTORY_FILE))
    try:
        atomic_write_json(path, data)
        return True
    except Exception as e:
        _raise_ledger_write_failed("save_trade_history_json", e, scope=scope)


def load_trade_history_document(
    scope: str = "paper", config: dict = None, tenant_id: str | None = None
) -> dict:
    from core.tenant_context import resolve_tenant_id

    cfg = config or get_config()
    tid = resolve_tenant_id(tenant_id)
    from core.tenant_context import multi_tenant_enabled

    if _ledger_reads_mongo_trade_history(scope, cfg):
        try:
            history = _mongo_ledger_store(cfg).load_trade_history(scope, tenant_id=tid)
        except Exception as e:
            if _should_refuse_json_fallback(scope, cfg) or multi_tenant_enabled():
                _raise_ledger_unavailable(
                    "load_trade_history_document", e, scope=scope, tenant_id=tid
                )
            log(f"Mongo trade_history load failed ({scope}), falling back to JSON: {e}", "WARNING")
            history = _load_trade_history_json(scope, cfg)
    elif multi_tenant_enabled():
        history = {
            "tenant_id": tid,
            "ledger_scope": scope,
            "trades": [],
            "virtual_balance": 5000.0,
            "realized_pnl": 0.0,
            "open_positions": 0,
        }
    else:
        history = _load_trade_history_json(scope, cfg)
    history, changed = _reconcile_scoped_trade_history(history, scope, cfg, tenant_id=tid)
    if changed:
        save_trade_history_document(history, scope, cfg, tenant_id=tid)
    return history


def reconcile_demo_trade_history_on_startup(config: dict = None) -> dict:
    """Refresh demo virtual_balance from ledger orders before the first trading cycle."""
    if not is_demo_mode():
        return {}
    from core.tenant_context import DEFAULT_TENANT, multi_tenant_enabled, tenant_context

    cfg = config or get_config()
    results: dict[str, dict] = {}
    results[DEFAULT_TENANT] = load_trade_history_document(
        "demo", cfg, tenant_id=DEFAULT_TENANT
    )
    if multi_tenant_enabled():
        try:
            from storage.tenant_registry import list_active_tenants

            for doc in list_active_tenants():
                tid = str(doc.get("tenant_id") or "").strip()
                if not tid or tid == DEFAULT_TENANT:
                    continue
                if doc.get("status", "active") != "active":
                    continue
                owner = str((doc.get("telegram") or {}).get("owner_chat_id") or "").strip()
                if not owner:
                    continue
                with tenant_context(tid, scope="demo", owner_chat_id=owner):
                    results[tid] = load_trade_history_document("demo", cfg, tenant_id=tid)
        except Exception as e:
            log(f"Multi-tenant demo trade_history reconcile skipped: {e}", "WARNING")
    return results


def save_trade_history_document(
    data: dict, scope: str = "paper", config: dict = None, tenant_id: str | None = None
) -> bool:
    from core.tenant_context import resolve_tenant_id

    cfg = config or get_config()
    tid = resolve_tenant_id(tenant_id)
    if _ledger_writes_json(scope, cfg):
        _save_trade_history_json(data, scope)
    if _ledger_writes_mongo(scope, cfg):
        try:
            _mongo_ledger_store(cfg).save_trade_history(data, scope, tenant_id=tid)
        except Exception as e:
            _raise_ledger_write_failed(
                "save_trade_history_document", e, scope=scope, tenant_id=tid
            )
    return True


def load_trade_history():
    return load_trade_history_document(resolve_ledger_scope())


def save_trade_history(data):
    return save_trade_history_document(data, resolve_ledger_scope())


def resolve_sim_cash_balance(
    scope: str = None,
    config: dict = None,
    tenant_id: str | None = None,
    *,
    history: dict | None = None,
) -> float:
    """Orders-replayed USDT cash for simulated trading (display source of truth)."""
    from core.portfolio_baseline import initial_capital
    from core.simulated_trading import is_simulated_trading, uses_order_ledger_cash
    from core.tenant_context import resolve_tenant_id

    cfg = config or get_config()
    resolved_scope = scope or resolve_ledger_scope()
    tid = resolve_tenant_id(tenant_id)
    if not uses_order_ledger_cash(cfg):
        hist = history or load_trade_history_document(resolved_scope, cfg, tenant_id=tid)
        return float(hist.get("virtual_balance", 0) or 0)

    filled = [
        o
        for o in load_orders(resolved_scope, tenant_id=tid).get("orders", [])
        if o.get("status") == "filled"
    ]
    initial = initial_capital(
        scope=resolved_scope,
        config=cfg,
        history=history,
        trading_mode=cfg.get("trading_mode"),
    )
    return float(compute_sim_cash_from_orders(filled, initial))


def resolve_sim_realized_pnl(
    scope: str = None,
    config: dict = None,
    tenant_id: str | None = None,
) -> float:
    """Realized PnL from filled sell orders (simulated trading)."""
    from core.simulated_trading import uses_order_ledger_cash
    from core.tenant_context import resolve_tenant_id

    cfg = config or get_config()
    resolved_scope = scope or resolve_ledger_scope()
    if not uses_order_ledger_cash(cfg):
        hist = load_trade_history_document(resolved_scope, cfg, tenant_id=resolve_tenant_id(tenant_id))
        return float(hist.get("realized_pnl", hist.get("total_pnl", 0)) or 0)
    from core.portfolio_baseline import initial_capital

    tid = resolve_tenant_id(tenant_id)
    filled = [
        o
        for o in load_orders(resolved_scope, tenant_id=tid).get("orders", [])
        if o.get("status") == "filled"
    ]
    initial = initial_capital(
        scope=resolved_scope,
        config=cfg,
        trading_mode=cfg.get("trading_mode"),
    )
    return float(compute_realized_pnl_from_orders(filled, initial))

def _filled_order_usdt(order: dict) -> float:
    execution = order.get("execution") or {}
    request = order.get("request") or {}
    for section in (execution, request):
        raw = section.get("usdt")
        if raw is not None:
            try:
                val = float(raw)
                if val > 0:
                    return val
            except (TypeError, ValueError):
                pass
    price = float(execution.get("price") or request.get("price") or 0)
    amount = float(execution.get("amount") or request.get("amount") or 0)
    if price > 0 and amount > 0:
        return price * amount
    return 0.0


def compute_sim_cash_from_orders(orders: list, initial: float = 5000.0) -> float:
    """Replay filled orders — cash leg of unified simulated-ledger replay."""
    from core.sim_ledger_replay import replay_simulated_ledger

    return float(replay_simulated_ledger(orders, initial)["cash"])


def compute_realized_pnl_from_orders(orders: list, initial: float = 5000.0) -> float:
    """Realized PnL from acknowledged sells only (matches position replay)."""
    from core.sim_ledger_replay import replay_simulated_ledger

    return float(replay_simulated_ledger(orders, initial)["realized_pnl"])


def _reconcile_scoped_trade_history(
    history: dict,
    scope: str,
    config: dict = None,
    tenant_id: str | None = None,
) -> tuple:
    from core.simulated_trading import is_simulated_trading

    cfg = config or get_config()
    if not is_simulated_trading(cfg):
        return history, False
    if scope not in ("demo", "live", "paper"):
        return history, False
    from core.portfolio_baseline import initial_capital
    from core.tenant_context import resolve_tenant_id
    from services.ledger_sync import count_open_positions_from_orders

    tid = resolve_tenant_id(tenant_id)
    initial = initial_capital(scope=scope, config=cfg)
    filled = [
        o
        for o in load_orders(scope, tenant_id=tid).get("orders", [])
        if o.get("status") == "filled"
    ]
    computed_cash = compute_sim_cash_from_orders(filled, initial)
    computed_pnl = compute_realized_pnl_from_orders(filled, initial)
    changed = False
    stored_cash = history.get("virtual_balance")
    if stored_cash is None or abs(float(stored_cash) - computed_cash) > 0.01:
        history["virtual_balance"] = computed_cash
        changed = True
    stored_pnl = history.get("realized_pnl")
    if stored_pnl is None or abs(float(stored_pnl or 0) - computed_pnl) > 0.01:
        history["realized_pnl"] = computed_pnl
        changed = True
    open_pos = count_open_positions_from_orders(scope, tenant_id=tid)
    if history.get("open_positions") != open_pos:
        history["open_positions"] = open_pos
        changed = True
    if changed:
        log(
            f"Reconciled demo cash: ${float(stored_cash or 0):,.2f} → "
            f"${computed_cash:,.2f} ({open_pos} open positions)",
            "INFO",
        )
    return history, changed


_SIM_CASH_EPS = 0.01


def _trade_margin_usdt(trade: dict) -> float:
    try:
        stored = float(trade.get("margin_usdt") or 0)
    except (TypeError, ValueError):
        stored = 0.0
    if stored > 0:
        return stored
    try:
        notion = float(trade.get("usdt_amount") or 0)
        lev = float(trade.get("leverage") or 2) or 2.0
    except (TypeError, ValueError):
        return 0.0
    if notion <= 0 or lev <= 0:
        return 0.0
    return notion / lev


def compute_sim_cash_from_trades(trades: list, initial: float = 5000.0) -> float:
    """Replay dry-run trades from starting capital to derive sim USDT cash."""
    balance = float(initial)
    for trade in trades or []:
        typ = str(trade.get("type") or "").upper()
        if typ == "BUY":
            usdt = float(trade.get("usdt_amount") or 0)
            if usdt > balance + _SIM_CASH_EPS:
                continue
            balance -= usdt
        elif typ == "SELL":
            balance += float(trade.get("usdt_received") or 0)
        elif typ == "SHORT":
            margin = _trade_margin_usdt(trade)
            if margin > balance + _SIM_CASH_EPS:
                continue
            balance -= margin
        elif typ == "COVER":
            balance += _trade_margin_usdt(trade) + float(trade.get("pnl") or 0)
    return round(max(0.0, balance), 8)


def compute_sim_realized_pnl(trades: list) -> float:
    return round(
        sum(
            float(t.get("pnl") or 0)
            for t in (trades or [])
            if str(t.get("type") or "").upper() in ("SELL", "COVER")
        ),
        8,
    )


def live_sim_initial_capital(config: dict = None) -> float:
    """Starting USDT for replaying live dry-run trades into virtual_balance."""
    cfg = config or get_config()
    if is_dry_run_enhanced(cfg):
        return simulated_balance_usdt(cfg)
    return float(cfg.get("initial_capital_usdt", simulated_balance_usdt(cfg)))


def _ensure_live_virtual_balance(history: dict, config: dict = None) -> dict:
    cfg = config or get_config()
    if not is_live_dry_run(cfg):
        return history
    initial = live_sim_initial_capital(cfg)
    trades = history.get("trades", [])
    history["virtual_balance"] = compute_sim_cash_from_trades(trades, initial)
    history["realized_pnl"] = compute_sim_realized_pnl(trades)
    history["total_pnl"] = history["realized_pnl"]
    return history


def _reconcile_live_trade_sources(history: dict) -> tuple:
    changed = False
    try:
        from services.order_service import OrderService, infer_manual_source

        svc = OrderService("live")
        if svc.reconcile_legacy_sources():
            changed = True
        by_id = {o["id"]: o.get("source") for o in svc._load().get("orders", []) if o.get("id")}
        for trade in history.get("trades", []):
            oid = trade.get("order_id")
            src = by_id.get(oid) if oid else None
            if not src:
                side = "buy" if trade.get("type") == "BUY" else "sell"
                signal = "" if trade.get("type") == "BUY" else "SELL"
                src = infer_manual_source({
                    "side": side,
                    "signal": signal,
                    "source": trade.get("source"),
                })
            if src and trade.get("source") != src:
                trade["source"] = src
                changed = True
    except Exception:
        pass
    return history, changed


def _load_live_trade_history_json() -> dict:
    path = get_data_file(LIVE_TRADE_HISTORY_FILE)
    if not os.path.exists(path):
        return {"trades": [], "total_pnl": 0.0, "realized_pnl": 0.0}
    try:
        with open(path, "r", encoding="utf-8") as f:
            history = json.load(f)
            if history.get("total_pnl") is not None and history.get("realized_pnl") is None:
                history["realized_pnl"] = history["total_pnl"]
            return history
    except Exception as e:
        _raise_ledger_unavailable("load_live_trade_history_json", e, scope="live")


def load_live_trade_history():
    cfg = get_config()
    if is_demo_mode() and is_live_dry_run(cfg):
        return load_trade_history_document("live")
    if is_demo_mode():
        return load_trade_history_document("demo")
    cfg = get_config()
    if _ledger_reads_mongo("live", cfg):
        try:
            from core.tenant_context import resolve_tenant_id

            history = _mongo_ledger_store(cfg).load_trade_history(
                "live", tenant_id=resolve_tenant_id()
            )
        except Exception as e:
            if _should_refuse_json_fallback("live", cfg):
                _raise_ledger_unavailable("load_live_trade_history", e, scope="live")
            log(f"Mongo live trade_history load failed: {e}", "WARNING")
            history = _load_live_trade_history_json()
    else:
        history = _load_live_trade_history_json()
    stored_cash = history.get("virtual_balance")
    history, reconciled = _reconcile_live_trade_sources(history)
    history = _ensure_live_virtual_balance(history)
    cash_drifted = (
        stored_cash is not None
        and abs(float(stored_cash) - float(history.get("virtual_balance", 0))) > 0.01
    )
    if reconciled or cash_drifted:
        save_live_trade_history(history)
    return history


def save_live_trade_history(data):
    return save_trade_history_document(data, "live")


def record_live_trade(trade):
    cfg = get_config()
    history = load_live_trade_history()
    history.setdefault("trades", []).append(trade)
    if is_live_dry_run(cfg):
        history = _ensure_live_virtual_balance(history, cfg)
    elif trade.get("type") == "SELL":
        history["total_pnl"] = history.get("total_pnl", 0) + trade.get("pnl", 0)
        history["realized_pnl"] = history["total_pnl"]
    save_live_trade_history(history)
    return history


def record_trade(trade):
    history = load_trade_history()
    history.setdefault("trades", []).append(trade)
    typ = str(trade.get("type") or "").upper()
    if typ == "BUY":
        history["virtual_balance"] = max(0, history["virtual_balance"] - trade.get("usdt_amount", 0))
    elif typ == "SELL":
        history["virtual_balance"] += trade.get("usdt_received", 0)
        history["realized_pnl"] += trade.get("pnl", 0)
    elif typ == "SHORT":
        history["virtual_balance"] = max(0, float(history.get("virtual_balance") or 0) - _trade_margin_usdt(trade))
    elif typ == "COVER":
        history["virtual_balance"] = float(history.get("virtual_balance") or 0) + _trade_margin_usdt(trade) + float(trade.get("pnl") or 0)
        history["realized_pnl"] = float(history.get("realized_pnl") or 0) + float(trade.get("pnl") or 0)
    try:
        from strategies.positions import count_open_positions
        history["open_positions"] = count_open_positions()
    except Exception:
        if typ in ("BUY", "SHORT"):
            history["open_positions"] = history.get("open_positions", 0) + 1
        elif typ in ("SELL", "COVER"):
            history["open_positions"] = max(0, history.get("open_positions", 0) - 1)
    save_trade_history(history)
    return history


ORDERS_SCOPE_FILES = {
    "demo": "orders.demo.json",
    "paper": "orders.paper.json",
    "live": "orders.live.json",
}

POSITIONS_SCOPE_FILES = {
    "demo": "positions.demo.json",
    "paper": "positions.paper.json",
    "live": "positions.live.json",
}

def resolve_ledger_backend(scope: str = None, config: dict = None) -> str:
    from storage.ledger_router import resolve_ledger_backend as _router_backend

    cfg = config or get_config()
    target = scope or resolve_ledger_scope()
    return _router_backend(target, cfg)


def ledger_dual_write_enabled(config: dict = None) -> bool:
    cfg = config or get_config()
    return bool((cfg.get("architecture", {}) or {}).get("ledger_dual_write", False))


def _demo_ledger_backend_is_mongo(config: dict = None) -> bool:
    return resolve_ledger_backend("demo", config) == "mongo"


def _demo_json_fallback_enabled() -> bool:
    return os.environ.get("DEMO_LEDGER_JSON_FALLBACK", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _should_refuse_demo_json_fallback(scope: str, cfg: dict) -> bool:
    return _should_refuse_json_fallback(scope, cfg)


def _ledger_reads_mongo(scope: str, config: dict = None) -> bool:
    """Whether positions load from Mongo."""
    if ledger_dual_write_enabled(config):
        return False
    if scope == "demo":
        return _demo_ledger_backend_is_mongo(config)
    return resolve_ledger_backend(scope, config) == "mongo"


def _ledger_reads_mongo_orders(scope: str, config: dict = None) -> bool:
    if scope == "demo":
        return _demo_ledger_backend_is_mongo(config)
    return _ledger_reads_mongo(scope, config)


def _ledger_reads_mongo_trade_history(scope: str, config: dict = None) -> bool:
    if scope == "demo":
        return _demo_ledger_backend_is_mongo(config)
    return _ledger_reads_mongo(scope, config)


def _ledger_writes_json(scope: str, config: dict = None) -> bool:
    from core.tenant_context import multi_tenant_enabled

    if multi_tenant_enabled():
        return False
    if scope == "demo":
        return not _demo_ledger_backend_is_mongo(config)
    backend = resolve_ledger_backend(scope, config)
    return backend == "local" or ledger_dual_write_enabled(config)


def _should_refuse_json_fallback(scope: str, cfg: dict) -> bool:
    """Refuse JSON fallback when Mongo is the writer for this scope (JSON is a dead book).

    ``DEMO_LEDGER_JSON_FALLBACK`` remains an explicit emergency exit for demo.
    """
    if scope == "demo" and _demo_json_fallback_enabled():
        return False
    return not _ledger_writes_json(scope, cfg)


def _ledger_writes_mongo(scope: str, config: dict = None) -> bool:
    if scope == "demo":
        return _demo_ledger_backend_is_mongo(config)
    backend = resolve_ledger_backend(scope, config)
    return backend == "mongo" or ledger_dual_write_enabled(config)


def _mongo_ledger_store(config: dict = None):
    from storage.mongo_client import use_isolated_pytest_database
    from storage.mongo_ledger import get_ledger_store

    return get_ledger_store(test=use_isolated_pytest_database(config), config=config)


def resolve_orders_file(scope: str) -> str:
    if scope not in ORDERS_SCOPE_FILES:
        raise ValueError(f"Invalid ledger scope: {scope}")
    # Scope is already in the filename (orders.demo.json / .paper / .live).
    # Do not run get_data_file() here — in DEMO_MODE it would rewrite
    # paper/live paths to *.demo.json.
    return resolve_data_path(ORDERS_SCOPE_FILES[scope])


def resolve_positions_file(scope: str) -> str:
    if scope not in POSITIONS_SCOPE_FILES:
        raise ValueError(f"Invalid ledger scope: {scope}")
    if scope == "demo":
        return get_data_file("positions.json")
    return resolve_data_path(POSITIONS_SCOPE_FILES[scope])


def resolve_ledger_scope(trading_mode: str = None) -> str:
    if is_demo_mode():
        return "demo"
    mode = trading_mode or get_config().get("trading_mode", "paper")
    if mode == "live":
        return "live"
    return "paper"


def uses_exchange_ledger(trading_mode: str = None) -> bool:
    mode = trading_mode or get_config().get("trading_mode", "paper")
    return mode == "live"


def _empty_orders(scope: str) -> dict:
    return {"ledger_scope": scope, "orders": [], "migrated_from_trades": False}


def _load_orders_json(scope: str) -> dict:
    path = resolve_orders_file(scope)
    if not os.path.exists(path):
        return _empty_orders(scope)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("ledger_scope") != scope:
            data["ledger_scope"] = scope
        return data
    except Exception as e:
        _raise_ledger_unavailable("load_orders_json", e, scope=scope)


def _save_orders_json(data: dict, scope: str) -> bool:
    path = resolve_orders_file(scope)
    try:
        payload = dict(data)
        payload["ledger_scope"] = scope
        atomic_write_json(path, payload)
        return True
    except Exception as e:
        _raise_ledger_write_failed("save_orders_json", e, scope=scope)


def load_orders(scope: str, tenant_id: str | None = None):
    from core.tenant_context import multi_tenant_enabled, resolve_tenant_id

    cfg = get_config()
    tid = resolve_tenant_id(tenant_id)
    if _ledger_reads_mongo_orders(scope, cfg):
        try:
            return _mongo_ledger_store(cfg).load_orders(scope, tenant_id=tid)
        except Exception as e:
            if _should_refuse_json_fallback(scope, cfg) or multi_tenant_enabled():
                _raise_ledger_unavailable("load_orders", e, scope=scope, tenant_id=tid)
            log(f"Mongo orders load failed ({scope}), falling back to JSON: {e}", "WARNING")
    if multi_tenant_enabled():
        return {
            "tenant_id": tid,
            "ledger_scope": scope,
            "orders": [],
            "migrated_from_trades": False,
        }
    return _load_orders_json(scope)


def _reject_demo_mongo_orders_downgrade(data: dict, scope: str, cfg: dict) -> bool:
    """Block accidental wipe when Mongo has a full demo book but memory is nearly empty."""
    if scope != "demo" or not _demo_ledger_backend_is_mongo(cfg):
        return False
    try:
        existing = _mongo_ledger_store(cfg).load_orders(scope)
        old_count = len(existing.get("orders", []))
        new_count = len(data.get("orders", []))
        if old_count >= 50 and new_count < max(10, old_count // 2):
            log(
                f"Blocked demo Mongo orders downgrade ({old_count} → {new_count})",
                "ERROR",
            )
            return True
    except Exception as e:
        log(f"Demo orders downgrade guard failed: {e}", "WARNING")
    return False


def save_orders(data: dict, scope: str, tenant_id: str | None = None) -> bool:
    from core.tenant_context import resolve_tenant_id

    cfg = get_config()
    tid = resolve_tenant_id(tenant_id)
    if _reject_demo_mongo_orders_downgrade(data, scope, cfg):
        _raise_ledger_write_failed(
            "save_orders",
            message="blocked demo mongo orders downgrade",
            scope=scope,
            tenant_id=tid,
        )
    if _ledger_writes_json(scope, cfg):
        _save_orders_json(data, scope)
    if _ledger_writes_mongo(scope, cfg):
        try:
            _mongo_ledger_store(cfg).save_orders(data, scope, tenant_id=tid)
        except Exception as e:
            _raise_ledger_write_failed("save_orders", e, scope=scope, tenant_id=tid)
    return True


def _empty_positions(scope: str) -> dict:
    return {"ledger_scope": scope, "positions": {}}


def _load_positions_json(scope: str) -> dict:
    path = resolve_positions_file(scope)
    if not os.path.exists(path):
        return _empty_positions(scope)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("positions", {})
        data["ledger_scope"] = scope
        return data
    except Exception as e:
        _raise_ledger_unavailable("load_positions_json", e, scope=scope)


def _save_positions_json(data: dict, scope: str) -> bool:
    path = resolve_positions_file(scope)
    try:
        payload = dict(data)
        payload["ledger_scope"] = scope
        atomic_write_json(path, payload)
        return True
    except Exception as e:
        _raise_ledger_write_failed("save_positions_json", e, scope=scope)


def load_positions_document(
    scope: str = None, config: dict = None, tenant_id: str | None = None
) -> dict:
    from core.tenant_context import resolve_tenant_id

    target = scope or resolve_ledger_scope()
    cfg = config or get_config()
    tid = resolve_tenant_id(tenant_id)
    from core.tenant_context import multi_tenant_enabled

    if _ledger_reads_mongo(target, cfg):
        try:
            return _mongo_ledger_store(cfg).load_positions(target, tenant_id=tid)
        except Exception as e:
            if _should_refuse_json_fallback(target, cfg) or multi_tenant_enabled():
                _raise_ledger_unavailable(
                    "load_positions_document", e, scope=target, tenant_id=tid
                )
            log(f"Mongo positions load failed ({target}), falling back to JSON: {e}", "WARNING")
    if multi_tenant_enabled():
        return {"tenant_id": tid, "ledger_scope": target, "positions": {}}
    return _load_positions_json(target)


def save_positions_document(
    data: dict, scope: str = None, config: dict = None, tenant_id: str | None = None
) -> bool:
    from core.tenant_context import resolve_tenant_id

    target = scope or resolve_ledger_scope()
    cfg = config or get_config()
    tid = resolve_tenant_id(tenant_id)
    if _ledger_writes_json(target, cfg):
        _save_positions_json(data, target)
    if _ledger_writes_mongo(target, cfg):
        try:
            _mongo_ledger_store(cfg).save_positions(data, target, tenant_id=tid)
        except Exception as e:
            _raise_ledger_write_failed(
                "save_positions_document", e, scope=target, tenant_id=tid
            )
    return True


STRATEGY_BACKTEST_FILE = "strategy_backtest.json"


def load_strategy_backtest_results() -> dict:
    path = get_data_file(STRATEGY_BACKTEST_FILE)
    if not os.path.exists(path):
        return {"coins": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("coins", {})
            return data
    except Exception as e:
        log(f"Failed to load {path}: {e}", "WARNING")
        return {"coins": {}}


def save_strategy_backtest_results(data: dict) -> bool:
    path = get_data_file(STRATEGY_BACKTEST_FILE)
    try:
        data.setdefault("coins", {})
        atomic_write_json(path, data)
        return True
    except Exception:
        return False


def get_strategy_backtest_entry(key: str) -> dict:
    return load_strategy_backtest_results().get("coins", {}).get(key, {})


def save_strategy_backtest_entry(key: str, entry: dict) -> bool:
    data = load_strategy_backtest_results()
    data.setdefault("coins", {})[key] = entry
    return save_strategy_backtest_results(data)


def list_strategy_targets() -> list:
    """Unique strategy entries from config.strategies (no trending-only coins)."""
    cfg = get_config()
    seen = set()
    targets = []
    for entry in cfg.get("strategies", []):
        symbol = entry.get("symbol")
        tf = entry.get("timeframe", "4h")
        if not symbol:
            continue
        if entry.get("live_enabled") is False and cfg.get("trading_mode") == "live":
            continue
        key = f"{symbol}_{tf}"
        if key in seen:
            continue
        seen.add(key)
        targets.append(dict(entry))
    return targets


PAPER_STRATEGIES_FILE = "paper_strategies.json"
PAPER_SANDBOX_HISTORY_FILE = "paper_sandbox_history.json"
CMC_POSTS_FILE = "cmc_posts.json"
LC_SIGNALS_FILE = "lc_signals.json"


def load_cmc_posts():
    path = get_data_file(CMC_POSTS_FILE)
    if not os.path.exists(path):
        return {"posts": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"Failed to load {path}: {e}", "WARNING")
        return {"posts": []}


def save_cmc_posts(data):
    path = get_data_file(CMC_POSTS_FILE)
    try:
        atomic_write_json(path, data)
        return True
    except Exception:
        return False


def log_cmc_post(signal, post_id: str = None):
    data = load_cmc_posts()
    entry = {
        "timestamp": datetime.now().isoformat(),
        "post_id": post_id or getattr(signal, "post_id", None),
        "coin": getattr(signal, "coin", ""),
        "action": getattr(signal, "action", "HOLD"),
        "confidence": getattr(signal, "confidence", 0),
        "rationale": getattr(signal, "rationale", ""),
        "votes_bullish": getattr(signal, "votes_bullish", 0),
        "votes_bearish": getattr(signal, "votes_bearish", 0),
        "source": "cmc",
        "quotes_fallback": bool(getattr(signal, "quotes_fallback", False)),
    }
    pid = entry.get("post_id")
    already = bool(pid and any(p.get("post_id") == pid for p in data.get("posts", [])))
    if not already:
        data.setdefault("posts", []).append(entry)
        save_cmc_posts(data)
    # Dual-write always (even if JSON already had id) so failed first feed write can recover
    try:
        from intelligence.memory.social_ingest import append_social_feed

        append_social_feed(entry)
    except Exception:
        pass
    return data


_lc_signals_cache: dict = {"mtime": 0.0, "data": {"signals": []}}


def load_lc_signals():
    path = get_data_file(LC_SIGNALS_FILE)
    if not os.path.exists(path):
        return {"signals": []}
    try:
        mtime = os.path.getmtime(path)
        if _lc_signals_cache["mtime"] == mtime:
            return _lc_signals_cache["data"]
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _lc_signals_cache["mtime"] = mtime
        _lc_signals_cache["data"] = data
        return data
    except Exception as e:
        log(f"Failed to load {path}: {e}", "WARNING")
        return {"signals": []}


def save_lc_signals(data):
    path = get_data_file(LC_SIGNALS_FILE)
    try:
        atomic_write_json(path, data)
        if os.path.exists(path):
            _lc_signals_cache["mtime"] = os.path.getmtime(path)
            _lc_signals_cache["data"] = data
        return True
    except Exception:
        return False


def log_lc_signal(signal, signal_id: str = None):
    data = load_lc_signals()
    entry = {
        "timestamp": datetime.now().isoformat(),
        "signal_id": signal_id or getattr(signal, "post_id", None),
        "coin": getattr(signal, "coin", ""),
        "action": getattr(signal, "action", "HOLD"),
        "confidence": getattr(signal, "confidence", 0),
        "rationale": getattr(signal, "rationale", ""),
        "galaxy_score": getattr(signal, "galaxy_score", 0),
        "alt_rank": getattr(signal, "alt_rank", 0),
        "sentiment": getattr(signal, "sentiment", 0),
        "source": "lc",
    }
    sid = entry.get("signal_id")
    already = bool(sid and any(s.get("signal_id") == sid for s in data.get("signals", [])))
    if not already:
        data.setdefault("signals", []).append(entry)
        save_lc_signals(data)
    # Dual-write always so memory_social_feed recovers after a failed first write
    try:
        from intelligence.memory.social_ingest import append_social_feed

        append_social_feed(entry)
    except Exception:
        pass
    return data


def load_paper_strategies():
    path = get_data_file(PAPER_STRATEGIES_FILE)
    if not os.path.exists(path):
        return {"hypotheses": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"Failed to load {path}: {e}", "WARNING")
        return {"hypotheses": []}


def save_paper_strategies(data):
    path = get_data_file(PAPER_STRATEGIES_FILE)
    try:
        atomic_write_json(path, data)
        return True
    except Exception:
        return False


def load_paper_sandbox_history():
    path = get_data_file(PAPER_SANDBOX_HISTORY_FILE)
    if not os.path.exists(path):
        return {"portfolios": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"Failed to load {path}: {e}", "WARNING")
        return {"portfolios": {}}


def save_paper_sandbox_history(data):
    path = get_data_file(PAPER_SANDBOX_HISTORY_FILE)
    try:
        atomic_write_json(path, data)
        return True
    except Exception:
        return False


TRANSLATIONS = {}

def load_translations():
    global TRANSLATIONS
    try:
        with open("locales/en.json", "r", encoding="utf-8") as f:
            TRANSLATIONS["en"] = json.load(f)
        with open("locales/de.json", "r", encoding="utf-8") as f:
            TRANSLATIONS["de"] = json.load(f)
    except Exception as e:
        log(f"Failed to load translation files: {e}", "WARNING")
        TRANSLATIONS = {"en": {}, "de": {}}

def get_system_lang():
    try:
        lang = locale.getdefaultlocale()[0] or "en_US"
        if lang.lower().startswith("de"):
            return "de"
        return "en"
    except Exception as e:
        log(f"Failed to detect system language: {e}", "WARNING")
        return "en"

def get_text(key, default=""):
    from core.i18n import get_text as _get_text

    return _get_text(key, default)

load_translations()

