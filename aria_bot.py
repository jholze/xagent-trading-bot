import os
import threading
import time
import json
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, jsonify, request

import argparse
import atexit
import signal
from pathlib import Path

from logger import log

_ROOT = Path(__file__).resolve().parent
load_dotenv(_ROOT / ".env")
load_dotenv(_ROOT / ".env.local", override=True)

# Demo mode handling
parser = argparse.ArgumentParser(description="X-Agent Trading Bot")
parser.add_argument('--demo', action='store_true', help='Run in demo mode using separate .demo.json data files')
args, _ = parser.parse_known_args()

if args.demo:
    os.environ['DEMO_MODE'] = '1'
    print("🧪 Demo mode activated - using separate data files (watchlist.demo.json, etc.)")

try:
    from data_manager import get_text
except:
    def get_text(key, default=""):
        return default or key

print(get_text("bot_started") + "\n")

try:
    from core.config import get_bot_config
    from core.tenant_context import DEFAULT_TENANT, tenant_context, resolve_tenant_id
    from data_manager import (
        get_config,
        get_text,
        list_coins,
        load_trade_history,
        load_effective_watchlist,
        load_trade_watchlist,
    )
    from notifications.terminal_dashboard import build_cycle_summary, render_cycle_dashboard
    from price_fetcher import get_prices, get_prices_batch
    from intelligence.trend_engine import TrendEngine
    from services.signal_orchestrator import SignalOrchestrator
    from services.social_pipeline import SocialPipeline
    from strategies.paper_sandbox import PaperSandbox
    from telegram_notifier import (
        handle_telegram_callback,
        handle_telegram_command,
        handle_telegram_text,
        send_cmc_cycle_digest,
        send_cycle_summary,
        send_lc_cycle_digest,
        send_merged_social_digest,
        send_signal_message,
        send_x_cycle_digest,
    )
    from x_analyzer import XAnalyzer
except ImportError as e:
    print(f"Fehler beim Laden der Module: {e}")
    exit()

with open("config.json", encoding="utf-8") as f:
    config = json.load(f)
trading_mode = config.get("trading_mode", "paper" if config.get("virtual_trading", True) else "off")
print(f"Trading mode: {trading_mode.upper()}" + (" (demo)" if os.environ.get("DEMO_MODE") == "1" else ""))

try:
    from storage.mongo_client import assert_safe_demo_mongo_db, log_ledger_startup

    assert_safe_demo_mongo_db()
    log_ledger_startup()
    from core.ledger_repair import maybe_repair_tenant_ledgers_once

    maybe_repair_tenant_ledgers_once()
except SystemExit:
    raise
except Exception as e:
    log(f"Ledger startup guard failed: {e}", "WARNING")

try:
    from data.cmc_capabilities import log_cmc_boot_status

    log_cmc_boot_status()
except Exception as e:
    log(f"CMC capability probe on startup failed: {e}", "WARNING")

# R15: soak boot fingerprint (mode, volume, commit) → logs/cycle_summary.jsonl
try:
    from services.watchlist_quality.soak_log import log_boot_fingerprint

    log_boot_fingerprint()
except Exception as e:
    log(f"WQE boot fingerprint failed: {e}", "DEBUG")


def _flush_positions_on_exit(*_args) -> None:
    try:
        from strategies.positions import flush_positions, get_active_scope

        flush_positions(scope=get_active_scope(), force=True)
    except Exception as e:
        log(f"Position flush on exit failed: {e}", "WARNING")


atexit.register(_flush_positions_on_exit)


def _handle_shutdown(_signum, _frame) -> None:
    _flush_positions_on_exit()
    raise SystemExit(0)


for _sig in (signal.SIGTERM, signal.SIGINT):
    try:
        signal.signal(_sig, _handle_shutdown)
    except Exception:
        pass

try:
    from core.tenant_context import DEFAULT_TENANT, multi_tenant_enabled
    from core.tenant_routing import iter_price_cycle_tenants, tenant_cycle_context
    from data_manager import reconcile_demo_trade_history_on_startup, resolve_ledger_scope
    from services.ledger_sync import rebuild_positions_from_orders, sync_positions_on_startup
    from strategies.positions import flush_positions

    _ledger_scope = resolve_ledger_scope()
    rebuild_positions_from_orders(_ledger_scope, tenant_id=DEFAULT_TENANT)
    if multi_tenant_enabled():
        for _startup_tenant in iter_price_cycle_tenants():
            if _startup_tenant == DEFAULT_TENANT:
                continue
            with tenant_cycle_context(_startup_tenant):
                rebuild_positions_from_orders(_ledger_scope)
    sync_positions_on_startup()
    reconcile_demo_trade_history_on_startup()
    flush_positions(scope=resolve_ledger_scope(), force=True)
except Exception as e:
    log(f"Ledger position sync on startup failed: {e}", "WARNING")

# Flask für Webhook
app = Flask(__name__)

try:
    from services.exit_radar_http import register_exit_radar_routes

    register_exit_radar_routes(app)
except Exception as _exit_radar_exc:
    log(f"exit_radar routes not registered: {_exit_radar_exc}", "WARNING")

try:
    from services.desk_http import register_desk_routes

    register_desk_routes(app)
except Exception as _desk_exc:
    log(f"desk routes not registered: {_desk_exc}", "WARNING")

try:
    from services.mcp_bot_http import register_mcp_bot_routes

    register_mcp_bot_routes(app)
except Exception as _mcp_exc:
    log(f"mcp execute route not registered: {_mcp_exc}", "WARNING")

try:
    from services.exit_realtime.fire_http import register_exit_ws_fire_routes
    from services.exit_realtime.watch_http import register_exit_ws_watch_routes

    register_exit_ws_fire_routes(app)
    register_exit_ws_watch_routes(app)
except Exception as _exit_fire_exc:
    log(f"exit_ws fire route not registered: {_exit_fire_exc}", "WARNING")

try:
    from services.gainer_signal.bot_http import register_gainer_signal_routes

    register_gainer_signal_routes(app)
except Exception as _gainer_sig_exc:
    log(f"gainer_signal route not registered: {_gainer_sig_exc}", "WARNING")

try:
    from services.dca_sniper.bot_http import register_dca_sniper_routes

    register_dca_sniper_routes(app)
except Exception as _dca_sniper_exc:
    log(f"dca_sniper routes not registered: {_dca_sniper_exc}", "WARNING")


@app.route("/health", methods=["GET"])
def health():
    return "OK", 200


@app.route("/health/detail", methods=["GET"])
def health_detail():
    from bus.price_cache import price_cache_from_config

    cache = price_cache_from_config()
    meta = cache.last_refresh() if cache.available() else None
    try:
        from services.market_service import ohlcv_cache_stats

        ohlcv_stats = ohlcv_cache_stats()
    except Exception:
        ohlcv_stats = {}
    try:
        from webhooks.store import recent_events

        signal_events = recent_events(10)
    except Exception:
        signal_events = []
    try:
        from core.runtime_identity import get_runtime_identity

        identity = get_runtime_identity()
    except Exception:
        identity = {}
    try:
        from bus.eval_queue import eval_queue_enabled, peek_eval_queue, queue_depth
        from services.eval_queue_runtime import worker_stats

        eval_queue = {
            "enabled": eval_queue_enabled(),
            "depth": queue_depth(),
            "worker": worker_stats(),
            "peek": peek_eval_queue(5) if eval_queue_enabled() else [],
        }
    except Exception:
        eval_queue = {}
    try:
        from services.santiment_store import get_latest_snapshot, snapshot_is_fresh, status_line

        san = get_latest_snapshot()
        san_meta = (san or {}).get("meta") if isinstance((san or {}).get("meta"), dict) else {}
        santiment = {
            "line": status_line(),
            "fresh": snapshot_is_fresh(san),
            "regime": (san or {}).get("regime"),
            "size_mult": (san or {}).get("size_mult"),
            "as_of": (san or {}).get("as_of"),
            "data_lag_days_max": san_meta.get("data_lag_days_max"),
            "metrics_ok": san_meta.get("metrics_ok") or [],
            "metrics_failed": san_meta.get("metrics_failed") or [],
            "policy_inputs": san_meta.get("policy_inputs") or [],
            "social_fresh": san_meta.get("social_fresh"),
            "scores": (san or {}).get("scores") or {},
        }
    except Exception:
        santiment = {}
    try:
        from services.market_oracle_store import (
            get_latest_snapshot as get_oracle_snap,
            snapshot_is_fresh as oracle_fresh,
            status_line as oracle_line,
        )
        from services.market_policy_fusion import get_global_market_bias

        ora = get_oracle_snap()
        market_oracle = {
            "line": oracle_line(),
            "fresh": oracle_fresh(ora),
            "state": (ora or {}).get("state") or (ora or {}).get("regime"),
            "size_mult": (ora or {}).get("size_mult"),
            "as_of": (ora or {}).get("as_of"),
        }
        fusion = get_global_market_bias()
        market_fusion = {
            "active": fusion.get("active"),
            "source": fusion.get("source"),
            "regime": fusion.get("regime"),
            "size_mult": fusion.get("size_mult"),
            "sensor_policy": fusion.get("sensor_policy"),
            "block_buys": fusion.get("block_buys"),
            "warmup_active": fusion.get("warmup_active"),
            "line": None,
            "cycle_blocks": None,
            "cycle_cuts": None,
            "memory_enabled": None,
            "hermes_external": None,
        }
        try:
            from services.market_context_observability import (
                cycle_counters,
                format_fusion_line,
            )

            market_fusion["line"] = format_fusion_line(fusion)
            ctr = cycle_counters()
            market_fusion["cycle_blocks"] = ctr.get("buy_blocks")
            market_fusion["cycle_cuts"] = ctr.get("size_cuts")
        except Exception:
            pass
        try:
            from intelligence.memory.store import memory_enabled
            from services.architecture_runtime import hermes_runs_in_process

            market_fusion["memory_enabled"] = memory_enabled()
            market_fusion["hermes_external"] = not hermes_runs_in_process()
        except Exception:
            pass
    except Exception:
        market_oracle = {}
        market_fusion = {}
    return jsonify({
        "status": "OK",
        "redis": cache.available(),
        "price_cache_last_refresh": meta,
        "ohlcv_cache": ohlcv_stats,
        "signal_webhook_recent": signal_events,
        "eval_queue": eval_queue,
        "santiment": santiment,
        "market_oracle": market_oracle,
        "market_fusion": market_fusion,
        "build": {
            "commit": identity.get("commit"),
            "branch": identity.get("branch"),
            "stack": identity.get("stack"),
            "service": identity.get("service"),
        },
    }), 200


@app.route("/api/santiment/ingest", methods=["POST"])
def santiment_ingest():
    """Receive market-context snapshots from the Santiment sidecar service."""
    from core.config import get_bot_config
    from services.santiment_ingest import (
        process_santiment_ingest,
        santiment_ingest_enabled,
        santiment_token_ok,
    )

    cfg = get_bot_config()
    if not santiment_ingest_enabled(cfg.raw):
        return jsonify({"error": "santiment ingest disabled"}), 404

    token = request.headers.get("X-Santiment-Token") or request.args.get("token")
    if not santiment_token_ok(token, cfg.raw):
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True)
    result = process_santiment_ingest(body, config_raw=cfg.raw)
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


@app.route("/api/market-oracle/ingest", methods=["POST"])
def market_oracle_ingest():
    """Receive MarketSnapshot from the market-oracle service."""
    from core.config import get_bot_config
    from services.market_oracle_ingest import (
        market_oracle_ingest_enabled,
        market_oracle_token_ok,
        process_market_oracle_ingest,
    )

    cfg = get_bot_config()
    if not market_oracle_ingest_enabled(cfg.raw):
        return jsonify({"error": "market oracle ingest disabled"}), 404

    token = (
        request.headers.get("X-Market-Oracle-Token")
        or request.headers.get("X-Oracle-Token")
        or request.args.get("token")
    )
    if not market_oracle_token_ok(token, cfg.raw):
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True)
    result = process_market_oracle_ingest(body, config_raw=cfg.raw)
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


@app.route("/api/signals/webhook", methods=["POST"])
def signal_webhook():
    from core.config import get_bot_config
    from services.signal_webhook_service import process_signal_webhook, signal_webhook_enabled
    from webhooks.auth import signal_webhook_token_ok

    cfg = get_bot_config()
    if not signal_webhook_enabled(cfg.raw):
        return jsonify({"error": "signal webhook disabled"}), 404

    token = request.headers.get("X-Signal-Token") or request.args.get("token")
    if not signal_webhook_token_ok(token, cfg.raw):
        return jsonify({"error": "unauthorized"}), 401

    source = request.args.get("source") or request.headers.get("X-Signal-Source") or "generic"
    body = request.get_json(silent=True)
    if body is None and request.data:
        try:
            body = request.get_data(as_text=True)
        except Exception:
            body = None

    result = process_signal_webhook(body, source=source, config_raw=cfg.raw)
    status = 200 if result.ok else 400
    if result.message == "rate_limit":
        status = 429
    payload = result.as_dict()
    payload["redis_published"] = result.redis_published
    return jsonify(payload), status


@app.route("/api/coins/prices", methods=["GET", "POST"])
def coin_prices_webhook():
    from core.config import get_bot_config
    from services.coin_query_service import (
        normalize_symbols,
        query_coin_prices,
        response_to_dict,
        webhook_token_ok,
    )

    cfg = get_bot_config()
    arch = cfg.architecture_config
    if not arch.get("coin_query_webhook_enabled", True):
        return jsonify({"error": "coin query webhook disabled"}), 404

    token = request.headers.get("X-Coin-Token") or request.args.get("token")
    if not webhook_token_ok(token, cfg.raw):
        return jsonify({"error": "unauthorized"}), 401

    symbols: list[str] = []
    fallbacks: dict[str, float] = {}
    force_refresh = False
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        symbols = normalize_symbols(body.get("symbols") or body.get("symbol"))
        raw_fb = body.get("fallbacks") or {}
        if isinstance(raw_fb, dict):
            for key, val in raw_fb.items():
                normed = normalize_symbols(key)
                if normed:
                    fallbacks[normed[0]] = float(val)
        force_refresh = bool(body.get("force_refresh"))
    else:
        symbols = normalize_symbols(request.args.get("symbols") or request.args.get("symbol"))
        force_refresh = request.args.get("refresh", "").lower() in ("1", "true", "yes")

    if not symbols:
        return jsonify({"error": "symbols required (e.g. BTC,ETH or BTC/USDT)"}), 400

    result = query_coin_prices(
        symbols,
        fallbacks=fallbacks or None,
        config_raw=cfg.raw,
        force_refresh=force_refresh,
    )
    payload = response_to_dict(result)
    payload["ok"] = True
    payload["count"] = len(result.prices)
    return jsonify(payload), 200


@app.route("/", methods=["GET"])
def webhook_get():
    return "OK", 200


def _get_tenant_id_from_request() -> str:
    # Support future per-tenant routing + header for tests/compat
    # 1. Path param if using /webhook/<tid> (added below)
    # 2. Header X-Tenant-Id
    # 3. Query ?tenant= or ?tenant_id=
    tid = request.view_args.get("tenant_id") if request.view_args else None
    if not tid:
        tid = request.headers.get("X-Tenant-Id") or request.headers.get("X-Tenant-ID")
    if not tid:
        tid = request.args.get("tenant") or request.args.get("tenant_id")
    return tid or DEFAULT_TENANT


def _message_text_from_update(update: dict | None) -> str:
    if not update or "message" not in update:
        return ""
    return str(update["message"].get("text") or "")


def _dispatch_telegram_webhook(explicit_tenant_id: str | None = None):
    """Route update to tenant by chat_id (shared bot) or explicit /webhook/<id>."""
    from core.tenant_routing import extract_chat_id_from_update, resolve_incoming_tenant
    from notifications.telegram_commands.tenant_link_commands import try_link_tenant_from_start
    from telegram_notifier import _send_telegram_direct

    update = request.get_json(silent=True)
    chat_id = extract_chat_id_from_update(update)
    text = _message_text_from_update(update)

    if text.strip().lower().startswith("/start"):
        handled, link_msg = try_link_tenant_from_start(text, chat_id)
        if handled and link_msg:
            _send_telegram_direct(link_msg, chat_id=chat_id, parse_mode="HTML")
            return "OK", 200

    route = resolve_incoming_tenant(
        chat_id=chat_id,
        explicit_tenant_id=explicit_tenant_id or _get_tenant_id_from_request(),
    )
    if route.rejected:
        if route.reject_message and chat_id:
            _send_telegram_direct(route.reject_message, chat_id=chat_id, parse_mode="HTML")
        return "OK", 200

    with tenant_context(route.tenant_id, scope=route.scope, owner_chat_id=route.owner_chat_id):
        return _process_telegram_update(update)


@app.route("/webhook/<tenant_id>", methods=["POST"])
def webhook_with_tenant(tenant_id: str):
    """Per-tenant webhook route for SaaS / BYOB."""
    from storage.tenant_registry import get_webhook_secret

    expected_secret = get_webhook_secret(tenant_id)
    if expected_secret:
        received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if received != expected_secret:
            log(f"Webhook secret mismatch for {tenant_id}", "WARNING")
            return "Forbidden", 403

    try:
        return _dispatch_telegram_webhook(tenant_id)
    except Exception as e:
        log(f"Webhook error ({tenant_id}): {e}", "ERROR")
    return "OK", 200


@app.route("/", methods=["POST"])
def webhook():
    try:
        return _dispatch_telegram_webhook(None)
    except Exception as e:
        log(f"Webhook error: {e}", "ERROR")
    return "OK", 200


def _process_telegram_update(update=None):
    """Inner dispatch. Assumes tenant_context is active."""
    tid = resolve_tenant_id()
    from storage.tenant_registry import get_tenant
    _ = get_tenant(tid)

    if update is None:
        update = request.get_json(silent=True)
    if update:
        from notifications.telegram_commands.menu_i18n import resolve_ui_language

        resolve_ui_language(update, tid)
    if update and "callback_query" in update:
        log(f"[{tid}] Telegram callback", "DEBUG")
        handle_telegram_callback(update["callback_query"])
    elif update and "message" in update:
        text = update["message"].get("text", "")
        chat_id = update["message"].get("chat", {}).get("id")
        log(f"[{tid}] Telegram message: {text[:100]}", "DEBUG")
        if text.startswith("/"):
            handle_telegram_command(text, chat_id=chat_id)
        elif text.strip():
            handle_telegram_text(text, chat_id=chat_id)
    return "OK", 200


def _run_tenant_price_cycle(
    cycle_started: float,
    use_dashboard: bool,
    *,
    analyzer,
    orchestrator,
    social_pipeline,
    sandbox,
    trend_engine,
    shared_signals=None,
):
    bot_config = get_bot_config()
    bot_config.refresh()
    try:
        from services.architecture_runtime import ensure_started
        ensure_started()
    except Exception as e:
        log(f"Architecture runtime tick failed: {e}", "WARNING")
    use_dashboard = bot_config.terminal_dashboard_enabled and os.isatty(1)

    if not use_dashboard and os.isatty(1):
        os.system("clear" if os.name == "posix" else "cls")

    now = datetime.now()
    mode = get_config().get("trading_mode", "paper")
    cycle_signal_lines = []
    coin_results = []

    if not use_dashboard:
        print(f"🕒 {now.strftime('%H:%M:%S')}                  X-Agent Trading Bot                  Mode: {mode.upper()}")
        print("=" * 90)

    if orchestrator:
        try:
            orchestrator.begin_tenant_cycle()
        except Exception as e:
            log(f"Tenant cycle init failed: {e}", "WARNING")

    # Gate top-movers refresh (feature-flagged; fail-open). Does not place orders.
    try:
        from services.gainer_universe.runtime import maybe_refresh_gainer_universe

        maybe_refresh_gainer_universe(get_config())
    except Exception as e:
        log(f"gainer_universe refresh skip: {e}", "DEBUG")

    # Observe = broad (memory/WQE); trade = positions + top discovery for process_coin
    observe_watchlist = load_effective_watchlist()
    watchlist = observe_watchlist
    active_observe = [coin for coin in observe_watchlist if coin.get("active", True)]
    active_symbols = [coin["symbol"] for coin in active_observe]
    if not use_dashboard:
        print(
            f"Aktive Coins observe ({len(active_symbols)}): "
            + " • ".join(active_symbols)
        )
        print("-" * 90)
        print("Prüfe Coins + X-Signale:\n")

    if shared_signals is not None:
        x_signals = list(shared_signals.x_signals or [])
        cmc_signals = list(shared_signals.cmc_signals or [])
        lc_signals = list(shared_signals.lc_signals or [])
    elif social_pipeline:
        from services.cycle_shared import prepare_shared_cycle_signals

        bundle = prepare_shared_cycle_signals(
            bot_config=bot_config,
            social_pipeline=social_pipeline,
            analyzer=analyzer,
        )
        x_signals = bundle.x_signals
        cmc_signals = bundle.cmc_signals
        lc_signals = bundle.lc_signals
    else:
        x_signals = analyzer.get_top_signals() if analyzer else []
        cmc_signals = []
        lc_signals = []

    if trend_engine and x_signals:
        candidates = trend_engine.cross_validate(x_signals, run_scan=False)
        for c in candidates[:3]:
            line = f"→ Trend+X: {c['symbol']} ({c['regime']}) 5m:{c['change_5m']:+.1f}%"
            cycle_signal_lines.append(line)
            if not use_dashboard:
                print(f"   {line}")

    if sandbox and get_config().get("sandbox", {}).get("enabled", True):
        sandbox_results = sandbox.run_cycle(watchlist, get_prices)
        for sr in sandbox_results[:3]:
            m = sr["metrics"]
            line = f"→ Sandbox {sr['hypothesis_id']}: {sr['action']} {sr['symbol']} | WR={m.win_rate}%"
            cycle_signal_lines.append(line)
            if not use_dashboard:
                print(f"   {line}")

    for signal in x_signals:
        eff = getattr(signal, "effective_confidence", signal.confidence)
        if eff >= 70:
            line = (
                f"🟢 @{signal.account} {signal.action} {signal.coin} | "
                f"Conf: {signal.confidence}% | Eff: {eff:.0f}%"
            )
            cycle_signal_lines.append(line)
            if not use_dashboard:
                print(f"   → X-Signal @{signal.account}: {signal.action} {signal.coin} | "
                      f"Conf: {signal.confidence}% | Effective: {eff:.0f}% | "
                      f"Trust: {getattr(signal, 'trust_score', '?')}")

    for signal in cmc_signals:
        if signal.confidence >= 60:
            from notifications.user_explain import cmc_score_line_de, cmc_signal_kind, cmc_source_label_de

            kind = cmc_signal_kind(signal)
            src = cmc_source_label_de(kind)
            line = f"📊 CMC [{src}] {signal.action} {signal.coin} | {signal.confidence}%"
            cycle_signal_lines.append(line)
            if not use_dashboard:
                print(
                    f"   → CMC [{src}]: {signal.action} {signal.coin} | "
                    f"Conf: {signal.confidence}% | "
                    f"{cmc_score_line_de(kind, signal.votes_bullish, signal.votes_bearish)}"
                )

    for signal in lc_signals:
        if signal.confidence >= 55:
            line = (
                f"🌙 LC {signal.action} {signal.coin} | {signal.confidence}% | "
                f"Galaxy {signal.galaxy_score:.0f}"
            )
            cycle_signal_lines.append(line)
            if not use_dashboard:
                print(
                    f"   → LunarCrush: {signal.action} {signal.coin} | "
                    f"Conf: {signal.confidence}% | Galaxy: {signal.galaxy_score:.0f} | "
                    f"AltRank: {signal.alt_rank} | Sentiment: {signal.sentiment:.0f}%"
                )

    from core.cycle_order import order_watchlist_positions_first
    from core.tenant_context import multi_tenant_enabled
    from strategies.positions import list_active_positions, list_active_positions_from_ledger

    # Multi-tenant: orders ledger is SOT (RAM can lag after external fills).
    if multi_tenant_enabled():
        open_positions = list_active_positions_from_ledger()
    else:
        open_positions = list_active_positions()

    trade_watchlist = load_trade_watchlist(
        observe_coins=observe_watchlist,
        open_positions=open_positions,
    )
    active_coins = [coin for coin in trade_watchlist if coin.get("active", True)]
    if not use_dashboard and len(active_coins) != len(active_observe):
        print(
            f"Trade universe ({len(active_coins)} of {len(active_observe)} observe): "
            + " • ".join(c["symbol"] for c in active_coins[:40])
            + (" …" if len(active_coins) > 40 else "")
        )
    prefer_gainer = False
    try:
        from services.gainer_universe.config import gainer_universe_config

        prefer_gainer = bool(
            gainer_universe_config(bot_config.raw).get("scan_prefer_gainer", True)
        )
    except Exception:
        prefer_gainer = False
    scan_coins = order_watchlist_positions_first(
        active_coins, open_positions, prefer_gainer=prefer_gainer
    )
    price_map = get_prices_batch([coin["symbol"] for coin in scan_coins])

    try:
        from data_manager import resolve_ledger_scope
        from services.ledger_sync import reconcile_recent_highs

        reconcile_recent_highs(resolve_ledger_scope(), price_map=price_map)
    except Exception as e:
        log(f"Peak reconcile failed: {e}", "WARNING")

    from bus.eval_queue import eval_queue_enabled

    use_eval_queue = eval_queue_enabled(bot_config.raw)

    if orchestrator:
        try:
            orchestrator.run_portfolio_dca_pass(scan_coins, price_map, quiet=use_dashboard)
        except Exception as e:
            log(f"Portfolio DCA pass failed: {e}", "WARNING")

    if use_eval_queue and orchestrator:
        from services.eval_queue_runtime import get_recent_coin_results, seed_meta_producers

        seed_meta_producers(
            # Observe set: more symbols for memory/sensor queue coverage
            watchlist=active_observe,
            open_positions=open_positions,
            x_signals=x_signals,
            cmc_signals=cmc_signals,
            lc_signals=lc_signals,
            config_raw=bot_config.raw,
        )
        from core.tenant_context import resolve_tenant_id

        coin_results = get_recent_coin_results(
            cycle_started, tenant_id=resolve_tenant_id(),
        )
        if not use_dashboard:
            print(f"Redis eval queue aktiv — {len(coin_results)} Ergebnisse im Zyklus")
    else:
        for coin in scan_coins:
            symbol = coin["symbol"]
            if not use_dashboard:
                print(f"→ {symbol}")

            price = float(price_map.get(symbol, 0) or 0)
            if orchestrator:
                result = orchestrator.process_coin(
                    coin, price, x_signals, cmc_signals, lc_signals, quiet=use_dashboard
                )
                coin_results.append(result)
            else:
                from strategies.core_strategy import check_signal
                check_signal(coin, price, x_signals, notify_callback=send_signal_message)
            if not use_dashboard:
                print()

    interval = get_config().get("update_interval", 600)
    cycle_elapsed = int(time.time() - cycle_started)
    if cycle_elapsed > 30:
        mode_label = "eval_queue" if use_eval_queue else f"{len(scan_coins)} coins"
        log(
            f"Cycle completed in {cycle_elapsed}s "
            f"({mode_label}, {len(open_positions)} positions first)",
            "INFO",
        )
        try:
            from services.market_service import ohlcv_cache_stats

            stats = ohlcv_cache_stats()
            if stats:
                log(
                    "ohlcv_cache: hits={hits} misses={misses} "
                    "hit_rate={hit_rate_pct}% ram={ram_entries}".format(**stats),
                    "INFO",
                )
        except Exception:
            pass

    try:
        from services.position_tracking import maybe_snapshot_after_cycle

        maybe_snapshot_after_cycle(price_map, config_raw=bot_config.raw)
    except Exception as e:
        log(f"Position snapshot failed: {e}", "WARNING")

    try:
        from services.portfolio_plan import portfolio_plan_config
        from services.portfolio_nav_history import capture_current_nav_snapshot

        if portfolio_plan_config(bot_config.raw).get("enabled", True):
            capture_current_nav_snapshot()
    except Exception as e:
        log(f"Portfolio NAV daily snapshot failed: {e}", "DEBUG")

    if use_dashboard:
        render_cycle_dashboard(
            cycle_signals=cycle_signal_lines,
            coin_results=coin_results,
            trading_mode=mode,
            next_update=interval,
        )
    else:
        print("-" * 90)
        print(f"Update abgeschlossen um {now.strftime('%H:%M:%S')}")

    top_x = ""
    top_cmc = ""
    top_lc = ""
    if x_signals:
        best_x = max(
            x_signals,
            key=lambda s: getattr(s, "effective_confidence", getattr(s, "confidence", 0)),
        )
        eff = getattr(best_x, "effective_confidence", best_x.confidence)
        top_x = f"@{best_x.account} {best_x.action} {best_x.coin} ({eff:.0f}%)"
    if cmc_signals:
        from notifications.user_explain import cmc_score_line_de, cmc_signal_kind, cmc_source_label_de

        best_cmc = max(cmc_signals, key=lambda s: s.confidence)
        kind = cmc_signal_kind(best_cmc)
        top_cmc = (
            f"[{cmc_source_label_de(kind)}] {best_cmc.coin} {best_cmc.action} "
            f"({best_cmc.confidence}%) "
            f"{cmc_score_line_de(kind, best_cmc.votes_bullish, best_cmc.votes_bearish)}"
        )

    if lc_signals:
        best_lc = max(lc_signals, key=lambda s: s.confidence)
        top_lc = (
            f"{best_lc.coin} {best_lc.action} ({best_lc.confidence}%) "
            f"Galaxy {best_lc.galaxy_score:.0f}"
        )

    from services.cycle_notification_policy import cycle_notification_policy

    cycle_notification_policy.flush_hold_explanations()

    cycle_notif_cfg = bot_config.observability_config.get("cycle_notifications", {})
    digest_merge = cycle_notif_cfg.get("digest_merge", True)
    x_skip = social_pipeline.get_notified_post_ids() if social_pipeline else set()

    if social_pipeline and cycle_notification_policy.should_send_social_digest():
        if digest_merge:
            if social_pipeline.should_send_merged_digest(
                cmc_signals, lc_signals, x_signals, skip_post_ids=x_skip
            ):
                send_merged_social_digest(
                    cmc_signals, lc_signals, x_signals, skip_post_ids=x_skip
                )
        else:
            if social_pipeline.should_send_cmc_digest(cmc_signals):
                send_cmc_cycle_digest(cmc_signals)
            if social_pipeline.should_send_lc_digest(lc_signals):
                send_lc_cycle_digest(lc_signals)
            if social_pipeline.should_send_x_digest(x_signals, skip_post_ids=x_skip):
                send_x_cycle_digest(x_signals, skip_post_ids=x_skip)

    from notifications.terminal_dashboard import _portfolio_snapshot

    portfolio_snap = _portfolio_snapshot(mode)
    summary = build_cycle_summary(
        coin_results=coin_results,
        trading_mode=mode,
        x_signal_count=len(x_signals),
        cmc_signal_count=len(cmc_signals),
        lc_signal_count=len(lc_signals),
        top_x=top_x,
        top_cmc=top_cmc,
        top_lc=top_lc,
    )
    send_cycle_summary(
        summary,
        cycle_ctx={
            "coin_results": coin_results,
            "total_value": float(portfolio_snap.get("total_value", 0) or 0),
        },
    )

    # R15: durable cycle_summary.jsonl on volume (fail-open, no trading impact)
    try:
        from services.watchlist_quality.soak_log import log_cycle_summary
        from core.tenant_context import resolve_tenant_id

        log_cycle_summary(
            config=bot_config.raw,
            duration_sec=float(time.time() - cycle_started),
            n_watchlist=len(active_observe),
            n_open_positions=len(open_positions),
            coin_results=coin_results,
            eval_processed_delta=len(coin_results) if coin_results is not None else None,
            tenant_id=resolve_tenant_id() or "default",
        )
    except Exception as e:
        log(f"cycle_summary soak log failed: {e}", "DEBUG")


def price_loop(analyzer=None, orchestrator=None, social_pipeline=None, sandbox=None, trend_engine=None):
    bot_config = get_bot_config()
    use_dashboard = bot_config.terminal_dashboard_enabled and os.isatty(1)

    while True:
        try:
            cycle_started = time.time()
            from services.cycle_notification_policy import cycle_notification_policy

            cycle_notification_policy.reset_cycle()
            try:
                from services.market_context_observability import observe_cycle_start

                observe_cycle_start(bot_config.raw)
            except Exception:
                pass
            try:
                from services.market_service import reset_ohlcv_cache_cycle_stats

                reset_ohlcv_cache_cycle_stats()
            except Exception:
                pass
            from core.tenant_routing import iter_price_cycle_tenants, tenant_cycle_context
            from services.cycle_shared import prepare_shared_cycle_signals, sync_global_watchlist_once

            sync_global_watchlist_once(bot_config)
            shared_signals = prepare_shared_cycle_signals(
                bot_config=bot_config,
                social_pipeline=social_pipeline,
                analyzer=analyzer,
            )

            for _cycle_tenant in iter_price_cycle_tenants():
                with tenant_cycle_context(_cycle_tenant):
                    _run_tenant_price_cycle(
                        cycle_started,
                        use_dashboard,
                        analyzer=analyzer,
                        orchestrator=orchestrator,
                        social_pipeline=social_pipeline,
                        sandbox=sandbox,
                        trend_engine=trend_engine,
                        shared_signals=shared_signals,
                    )

            interval = get_config().get("update_interval", 600)
            cycle_elapsed = int(time.time() - cycle_started)

            if not bot_config.architecture_config.get("background_backtest_enabled", True):
                try:
                    from services.strategy_backtest_worker import tick_strategy_backtest

                    tick_strategy_backtest()
                except Exception as e:
                    log(f"Strategy backtest tick failed: {e}", "WARNING")

            sleep_seconds = max(0, interval - cycle_elapsed)
            if sleep_seconds == 0 and cycle_elapsed >= interval:
                log(f"Cycle took {cycle_elapsed}s (>= interval {interval}s) — starting next immediately", "WARNING")

            for remaining in range(sleep_seconds, 0, -1):
                if not use_dashboard:
                    print(f"\r   Nächste Aktualisierung in {remaining:3d} Sekunden...", end="", flush=True)
                time.sleep(1)
            if not use_dashboard:
                print("\n")

        except Exception as e:
            log(f"Error in price loop: {e}", "ERROR")
            time.sleep(get_config().get("update_interval", 600))


if __name__ == "__main__":
    analyzer = XAnalyzer()
    try:
        from notifications.coin_links import prefetch_watchlist_slugs

        prefetch_watchlist_slugs()
    except Exception as e:
        log(f"Coin link slug prefetch skipped: {e}", "WARNING")

    try:
        from notifications.telegram_commands.command_menu import register_bot_commands

        register_bot_commands()
    except Exception as e:
        log(f"Telegram command menu registration skipped: {e}", "WARNING")

    orchestrator = SignalOrchestrator(notify_callback=send_signal_message)
    try:
        from services.architecture_runtime import register_eval_orchestrator

        register_eval_orchestrator(orchestrator)
    except Exception as e:
        log(f"Eval queue worker register skipped: {e}", "WARNING")
    social_pipeline = SocialPipeline(analyzer, orchestrator=orchestrator)
    try:
        from services.background_runtime import register_pipeline

        register_pipeline(social_pipeline)
    except Exception as e:
        log(f"Background runtime pipeline register skipped: {e}", "WARNING")
    sandbox = PaperSandbox()
    trend_engine = TrendEngine()
    price_thread = threading.Thread(
        target=price_loop,
        args=(analyzer, orchestrator, social_pipeline, sandbox, trend_engine),
        daemon=True,
    )
    price_thread.start()

    try:
        from services.entry_sensor_loop import start_entry_sensor_loop

        start_entry_sensor_loop(orchestrator)
    except Exception as e:
        log(f"15m entry sensor loop not started: {e}", "WARNING")

    try:
        from services.webhook_watchdog import start_webhook_watchdog

        if os.environ.get("DEMO_MODE") == "1" and not os.environ.get("WEBHOOK_BASE_URL"):
            log("Webhook watchdog skipped in local demo (ngrok managed by start script)", "INFO")
        else:
            start_webhook_watchdog()
    except Exception as e:
        log(f"Webhook watchdog not started: {e}", "WARNING")

    try:
        from services.telegram_ask_bridge import start_ask_bridge_poller

        start_ask_bridge_poller()
    except Exception as e:
        log(f"Ask bridge poller not started: {e}", "WARNING")

    try:
        from services.architecture_runtime import ensure_started
        ensure_started()
    except Exception as e:
        log(f"Architecture runtime start failed: {e}", "WARNING")

    bot_config = get_bot_config()
    from services.architecture_runtime import hermes_runs_in_process
    if hermes_runs_in_process(bot_config):
        from hermes.agent import HermesAgent

        hermes_interval = int(bot_config.hermes_config.get("cycle_interval_sec", 3600))

        def hermes_loop():
            agent = HermesAgent(bot_config)
            while True:
                try:
                    bot_config.refresh()
                    result = agent.run_cycle()
                    log(result.summary, "INFO")
                except Exception as e:
                    log(f"Hermes loop error: {e}", "ERROR")
                time.sleep(hermes_interval)

        threading.Thread(target=hermes_loop, daemon=True, name="hermes-agent").start()
        print(f"Hermes self-improvement loop started (interval={hermes_interval}s)")

    try:
        from bus.price_cache import price_cache_from_config

        cache = price_cache_from_config(bot_config.raw)
        if cache.available():
            print(f"Redis price cache OK (ttl={int(cache.ttl_sec)}s)")
        else:
            log(
                "Redis not reachable — price cache disabled; run: bash scripts/ensure_redis.sh",
                "WARNING",
            )
    except Exception as e:
        log(f"Redis price cache check failed: {e}", "WARNING")

    # Soft hot-reload after deploy/start when needed (Redis caches survive restarts)
    _startup_reload_report = None
    try:
        from services.reload_registry import auto_reload_on_startup

        _startup_reload_report = auto_reload_on_startup(actor="bot_start")
        if _startup_reload_report is not None:
            print(
                f"Startup soft-reload: scopes={_startup_reload_report.scopes} "
                f"ok={_startup_reload_report.ok}"
            )
    except Exception as e:
        log(f"Startup auto-reload skipped: {e}", "WARNING")

    print(get_text("webhook_started"))
    print("Coin price webhook: GET/POST /api/coins/prices?symbols=BTC,ETH")
    print("Signal webhook: POST /api/signals/webhook?source=tradingview")

    try:
        from core.runtime_identity import format_startup_message, should_notify_startup
        from services.reload_registry import format_auto_reload_startup_line
        from telegram_notifier import send_telegram_message

        if should_notify_startup():
            _reload_line = format_auto_reload_startup_line(_startup_reload_report)

            def _startup_ping():
                time.sleep(2)
                msg = format_startup_message()
                if _reload_line:
                    msg = msg.rstrip() + "\n" + _reload_line
                send_telegram_message(msg)

            threading.Thread(target=_startup_ping, daemon=True, name="startup-notify").start()
    except Exception as e:
        log(f"Startup Telegram notify skipped: {e}", "WARNING")

    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"Listening on {host}:{port}")
    app.run(host=host, port=port, threaded=True)