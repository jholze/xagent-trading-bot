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
    return jsonify({
        "status": "OK",
        "redis": cache.available(),
        "price_cache_last_refresh": meta,
        "ohlcv_cache": ohlcv_stats,
        "signal_webhook_recent": signal_events,
        "eval_queue": eval_queue,
        "build": {
            "commit": identity.get("commit"),
            "branch": identity.get("branch"),
            "stack": identity.get("stack"),
            "service": identity.get("service"),
        },
    }), 200


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

    watchlist = load_effective_watchlist()
    active_symbols = [
        coin["symbol"] for coin in watchlist if coin.get("active", True)
    ]
    if not use_dashboard:
        print(f"Aktive Coins ({len(active_symbols)}): " + " • ".join(active_symbols))
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
            line = f"📊 CMC {signal.action} {signal.coin} | {signal.confidence}%"
            cycle_signal_lines.append(line)
            if not use_dashboard:
                print(
                    f"   → CMC Community: {signal.action} {signal.coin} | "
                    f"Conf: {signal.confidence}% | Votes: {signal.votes_bullish}↑/{signal.votes_bearish}↓"
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

    active_coins = [coin for coin in watchlist if coin.get("active", True)]
    from core.cycle_order import order_watchlist_positions_first
    from core.tenant_context import multi_tenant_enabled
    from strategies.positions import list_active_positions, list_active_positions_from_ledger

    # Multi-tenant: orders ledger is SOT (RAM can lag after external fills).
    if multi_tenant_enabled():
        open_positions = list_active_positions_from_ledger()
    else:
        open_positions = list_active_positions()
    scan_coins = order_watchlist_positions_first(active_coins, open_positions)
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
            watchlist=active_coins,
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
        best_cmc = max(cmc_signals, key=lambda s: s.confidence)
        top_cmc = (
            f"{best_cmc.coin} {best_cmc.action} ({best_cmc.confidence}%) "
            f"Votes {best_cmc.votes_bullish}↑/{best_cmc.votes_bearish}↓"
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

    if social_pipeline:
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


def price_loop(analyzer=None, orchestrator=None, social_pipeline=None, sandbox=None, trend_engine=None):
    bot_config = get_bot_config()
    use_dashboard = bot_config.terminal_dashboard_enabled and os.isatty(1)

    while True:
        try:
            cycle_started = time.time()
            from services.cycle_notification_policy import cycle_notification_policy

            cycle_notification_policy.reset_cycle()
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

    print(get_text("webhook_started"))
    print("Coin price webhook: GET/POST /api/coins/prices?symbols=BTC,ETH")
    print("Signal webhook: POST /api/signals/webhook?source=tradingview")

    try:
        from core.runtime_identity import format_startup_message, should_notify_startup
        from telegram_notifier import send_telegram_message

        if should_notify_startup():

            def _startup_ping():
                time.sleep(2)
                send_telegram_message(format_startup_message())

            threading.Thread(target=_startup_ping, daemon=True, name="startup-notify").start()
    except Exception as e:
        log(f"Startup Telegram notify skipped: {e}", "WARNING")

    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"Listening on {host}:{port}")
    app.run(host=host, port=port, threaded=True)