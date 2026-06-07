#!/usr/bin/env python3
"""
Full bot stress test — demo mode, no Telegram spam, CMC top-50 altcoin trials.
Run: DEMO_MODE=1 python3 tests/integration/full_bot_stress_test.py
"""
import os
import sys
import json
import time
import traceback
import unittest
from datetime import datetime
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
os.environ.setdefault("DEMO_MODE", "1")

from dotenv import load_dotenv

load_dotenv()

STABLECOINS = {
    "USDT", "USDC", "USDE", "DAI", "FDUSD", "PYUSD", "TUSD", "USDD",
    "USD1", "USDS", "EURC", "USDG", "USDF", "USDT0",
}
SKIP_COINS = STABLECOINS | {"BTC", "ETH", "WBTC", "WETH", "STETH", "WBETH"}


class TestReport:
    def __init__(self):
        self.passed = []
        self.failed = []
        self.warnings = []
        self.stats = {}

    def ok(self, name, detail=""):
        self.passed.append((name, detail))

    def fail(self, name, detail=""):
        self.failed.append((name, detail))

    def warn(self, name, detail=""):
        self.warnings.append((name, detail))

    def print_summary(self):
        print("\n" + "=" * 72)
        print("FULL BOT STRESS TEST REPORT")
        print("=" * 72)
        print(f"Time: {datetime.now().isoformat()}")
        print(f"Demo mode: {os.environ.get('DEMO_MODE', '0')}")
        for k, v in self.stats.items():
            print(f"  {k}: {v}")
        print(f"\n✅ PASSED: {len(self.passed)}")
        for name, detail in self.passed:
            line = f"   • {name}"
            if detail:
                line += f" — {detail}"
            print(line)
        if self.warnings:
            print(f"\n⚠️  WARNINGS: {len(self.warnings)}")
            for name, detail in self.warnings:
                print(f"   • {name}: {detail}")
        if self.failed:
            print(f"\n❌ FAILED: {len(self.failed)}")
            for name, detail in self.failed:
                print(f"   • {name}: {detail}")
        print("=" * 72)
        return len(self.failed) == 0


report = TestReport()
telegram_log = []


def mock_telegram(text):
    telegram_log.append(text)
    return True


def fetch_cmc_top_altcoins(limit=50):
    """Top altcoins by market cap from CoinMarketCap."""
    import requests

    key = os.getenv("CMC_API_KEY", "")
    if not key:
        return _fetch_coingecko_top(limit)

    try:
        url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"
        headers = {"X-CMC_PRO_API_KEY": key, "Accept": "application/json"}
        params = {"start": 1, "limit": 80, "convert": "USD", "sort": "market_cap"}
        r = requests.get(url, headers=headers, params=params, timeout=20)
        if r.status_code != 200:
            report.warn("CMC API", f"HTTP {r.status_code}, fallback CoinGecko")
            return _fetch_coingecko_top(limit)

        coins = []
        for item in r.json().get("data", []):
            sym = item.get("symbol", "").upper()
            if sym in SKIP_COINS:
                continue
            coins.append({
                "symbol": sym,
                "name": item.get("name", sym),
                "price": item.get("quote", {}).get("USD", {}).get("price", 0),
                "rank": item.get("cmc_rank", 0),
            })
            if len(coins) >= limit:
                break
        return coins
    except Exception as e:
        report.warn("CMC fetch", str(e))
        return _fetch_coingecko_top(limit)


def _fetch_coingecko_top(limit=50):
    import requests

    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {"vs_currency": "usd", "order": "market_cap_desc", "per_page": 80, "page": 1}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    coins = []
    for item in r.json():
        sym = item.get("symbol", "").upper()
        if sym in SKIP_COINS:
            continue
        coins.append({
            "symbol": sym,
            "name": item.get("name", sym),
            "price": item.get("current_price", 0),
            "rank": item.get("market_cap_rank", 0),
        })
        if len(coins) >= limit:
            break
    return coins


def run_unit_tests():
    print("\n▶ Phase 1: Unit tests...")
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName("tests.unit.test_virtual_trading")
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)
    report.stats["unit_tests_total"] = result.testsRun
    report.stats["unit_tests_failures"] = len(result.failures)
    report.stats["unit_tests_errors"] = len(result.errors)
    if result.wasSuccessful():
        report.ok("Unit tests", f"{result.testsRun} passed")
    else:
        for test, err in result.failures + result.errors:
            name = str(test).split()[-1].strip(")>")
            report.fail("Unit test", f"{name}: {err.splitlines()[-1][:120]}")


def run_command_tests():
    print("\n▶ Phase 2: Telegram command routing...")
    from notifications.telegram_commands.router import dispatch_command

    commands = [
        "/help", "/mode", "/risk", "/gate", "/list", "/positions",
        "/xsignals", "/cmc", "/sandbox", "/watchlist",
        "/mode paper", "/add SOL", "/list",
    ]
    with patch("telegram_notifier.send_telegram_message", side_effect=mock_telegram):
        for cmd in commands:
            try:
                handled = dispatch_command(cmd)
                if handled:
                    report.ok(f"Command {cmd}")
                else:
                    report.fail(f"Command {cmd}", "not handled")
            except Exception as e:
                report.fail(f"Command {cmd}", str(e)[:100])


def run_price_fetch_top50(coins):
    print("\n▶ Phase 3: Price fetch for top altcoins...")
    from price_fetcher import get_prices, _price_cache

    gate_ok = cg_ok = fail = 0
    sample_fails = []
    for c in coins:
        sym = f"{c['symbol']}/USDT"
        _price_cache.pop(sym, None)
        price, _, _ = get_prices(sym)
        if price and price > 0:
            gate_ok += 1
        else:
            fail += 1
            if len(sample_fails) < 8:
                sample_fails.append(sym)
        time.sleep(0.15)

    report.stats["price_fetch_ok"] = gate_ok
    report.stats["price_fetch_fail"] = fail
    report.stats["price_fetch_total"] = len(coins)
    pct = gate_ok / len(coins) * 100 if coins else 0
    if pct >= 50:
        report.ok("Price fetch top-50", f"{gate_ok}/{len(coins)} ({pct:.0f}%)")
    else:
        report.fail("Price fetch top-50", f"only {gate_ok}/{len(coins)}")
    if sample_fails:
        report.warn("Price misses", ", ".join(sample_fails))


def run_trading_trials(coins):
    print("\n▶ Phase 4: Paper buy/sell trials...")
    from price_fetcher import get_prices, _price_cache
    from services.trading_service import TradingService
    from strategies.positions import list_active_positions, positions, save_positions
    from data_manager import load_trade_history, save_trade_history

    # Clean slate for fair buy trials (demo mode only)
    positions.clear()
    save_positions()
    save_trade_history({
        "virtual_balance": 5000.0,
        "realized_pnl": 0.0,
        "open_positions": 0,
        "trades": [],
    })

    svc = TradingService()
    svc.config.refresh()

    tradeable = []
    for c in coins[:30]:
        sym = f"{c['symbol']}/USDT"
        _price_cache.pop(sym, None)
        price, _, _ = get_prices(sym)
        if price and price > 0:
            tradeable.append((sym, price))
        if len(tradeable) >= 12:
            break
        time.sleep(0.1)

    if not tradeable:
        report.fail("Trading trials", "no tradeable coins found")
        return

    buys = sells = 0
    buy_errors = []

    for sym, price in tradeable[:8]:
        result = svc.execute_buy(sym, "4h", price, usdt=25)
        if result.executed:
            buys += 1
        else:
            buy_errors.append(f"{sym}: {result.message}")

    active = list_active_positions()
    for i, pos in enumerate(active[:5]):
        sym = pos["symbol"] if "/" in pos["symbol"] else pos["symbol"] + "/USDT"
        price, _, _ = get_prices(sym)
        if price <= 0:
            continue
        pct = 0.25 if i % 2 == 0 else 0.5
        amount = float(pos.get("amount", 0)) * pct
        if amount > 0:
            r = svc.execute_sell(sym, "4h", price, "SELL", amount)
            if r.executed:
                sells += 1

    history = load_trade_history()
    active = list_active_positions()
    report.stats["trial_buys"] = buys
    report.stats["trial_sells"] = sells
    report.stats["trial_balance"] = history.get("virtual_balance", 0)
    report.stats["open_positions"] = len(active)

    # Portfolio invariants after multi buy/sell
    portfolio_ok = True
    portfolio_detail = []
    if any(float(p.get("amount", 0)) < 0 for p in active):
        portfolio_ok = False
        portfolio_detail.append("negative amount")
    sell_pnls = [t.get("pnl", 0) for t in history.get("trades", []) if t.get("type") == "SELL"]
    if sell_pnls and abs(sum(sell_pnls) - history.get("realized_pnl", 0)) > 0.05:
        portfolio_ok = False
        portfolio_detail.append("realized_pnl mismatch")
    if history.get("open_positions", -1) != len(active):
        portfolio_ok = False
        portfolio_detail.append("open_positions desync")

    unreal = 0.0
    for p in active:
        sym = p["symbol"] if "/" in p["symbol"] else p["symbol"] + "/USDT"
        price, _, _ = get_prices(sym)
        entry = p.get("average_entry", p.get("entry_price", 0))
        if price and entry:
            unreal += (price - entry) * float(p["amount"])
    total_value = history.get("virtual_balance", 0) + unreal
    report.stats["trial_total_value"] = round(total_value, 2)
    if total_value <= 0:
        portfolio_ok = False
        portfolio_detail.append("total_value <= 0")

    if portfolio_ok:
        report.ok("Portfolio invariants", f"value=${total_value:.0f}, realized=${history.get('realized_pnl', 0):.1f}")
    else:
        report.fail("Portfolio invariants", "; ".join(portfolio_detail) or "check failed")

    if buys >= 3:
        report.ok("Paper BUY trials", f"{buys} executed")
    else:
        report.fail("Paper BUY trials", f"only {buys}; errors: {buy_errors[:3]}")

    if sells >= 1:
        report.ok("Paper SELL trials", f"{sells} executed")
    else:
        report.warn("Paper SELL trials", "no sells (maybe risk limits)")


def run_orchestrator_cycle(coins):
    print("\n▶ Phase 5: Signal orchestrator + decision engine...")
    from services.signal_orchestrator import SignalOrchestrator
    from price_fetcher import get_prices

    orch = SignalOrchestrator(notify_callback=lambda *a, **k: None)
    analyzed = executed = errors = 0

    watchlist_coins = [
        {"symbol": f"{c['symbol']}/USDT", "timeframe": "4h", "active": True}
        for c in coins[:15]
    ]

    for coin in watchlist_coins:
        try:
            price, _, _ = get_prices(coin["symbol"])
            if not price or price <= 0:
                continue
            result = orch.process_coin(coin, price, x_signals=[], cmc_signals=[], quiet=True)
            analyzed += 1
            if result and result.get("executed"):
                executed += 1
        except Exception as e:
            errors += 1
            if errors <= 3:
                report.warn(f"Orchestrator {coin['symbol']}", str(e)[:80])

    report.stats["orchestrator_analyzed"] = analyzed
    report.stats["orchestrator_executed"] = executed
    if analyzed >= 5:
        report.ok("Orchestrator cycle", f"{analyzed} coins analyzed, {executed} trades")
    else:
        report.fail("Orchestrator cycle", f"only {analyzed} analyzed")


def run_social_pipeline():
    print("\n▶ Phase 6: Social pipeline (X + CMC)...")
    from x_analyzer import XAnalyzer
    from services.social_pipeline import SocialPipeline
    from services.signal_orchestrator import SignalOrchestrator
    from data_manager import load_watchlist

    grok_response = (
        '{"coin": "SOL", "action": "BUY", "confidence": 80, '
        '"price_target": 200, "stop_loss": 150, "rationale": "Stress test signal"}'
    )

    try:
        analyzer = XAnalyzer()
        orch = SignalOrchestrator(notify_callback=lambda *a, **k: None)
        pipeline = SocialPipeline(analyzer, orchestrator=orch)

        with patch("x_analyzer.ask_grok_json", return_value=grok_response):
            x_posts = pipeline.process_new_posts()
        report.stats["x_posts_processed"] = len(x_posts) if x_posts else 0

        watchlist = load_watchlist()
        cmc = pipeline.process_cmc_posts(watchlist)
        report.stats["cmc_posts_processed"] = len(cmc) if cmc else 0

        x_sig = pipeline.refresh_signals()
        cmc_sig = pipeline.refresh_cmc_signals()
        report.stats["x_signals"] = len(x_sig) if x_sig else 0
        report.stats["cmc_signals"] = len(cmc_sig) if cmc_sig else 0

        acc = pipeline.update_accuracy_loop()
        report.ok(
            "Social pipeline",
            f"X posts={report.stats['x_posts_processed']}, "
            f"signals X={report.stats['x_signals']} CMC={report.stats['cmc_signals']}",
        )
    except Exception as e:
        report.fail("Social pipeline", str(e)[:120])


def run_sandbox_cycle():
    print("\n▶ Phase 7: Paper sandbox...")
    from strategies.paper_sandbox import PaperSandbox
    from data_manager import load_watchlist
    from price_fetcher import get_prices

    try:
        sandbox = PaperSandbox()
        results = sandbox.run_cycle(load_watchlist(), get_prices)
        report.stats["sandbox_actions"] = len(results)
        report.ok("Sandbox cycle", f"{len(results)} hypothesis evaluations")
    except Exception as e:
        report.fail("Sandbox cycle", str(e)[:120])


def run_bot_startup():
    print("\n▶ Phase 8: Bot module startup + one cycle simulation...")
    try:
        from core.config import get_bot_config
        from data_manager import get_config, load_watchlist
        from notifications.terminal_dashboard import build_cycle_summary
        from services.signal_orchestrator import SignalOrchestrator
        from services.social_pipeline import SocialPipeline
        from strategies.paper_sandbox import PaperSandbox
        from x_analyzer import XAnalyzer
        from price_fetcher import get_prices

        cfg = get_bot_config()
        mode = get_config().get("trading_mode", "paper")
        analyzer = XAnalyzer()
        orch = SignalOrchestrator(notify_callback=lambda *a, **k: None)
        social = SocialPipeline(analyzer, orchestrator=orch)
        sandbox = PaperSandbox()

        social.process_new_posts()
        social.process_cmc_posts(load_watchlist())
        x_signals = social.refresh_signals()
        cmc_signals = social.refresh_cmc_signals()

        coin_results = []
        for coin in load_watchlist():
            if not coin.get("active", True):
                continue
            sym = coin["symbol"]
            price, _, _ = get_prices(sym)
            if price > 0:
                coin_results.append(orch.process_coin(coin, price, x_signals, cmc_signals, quiet=True))

        sandbox.run_cycle(load_watchlist(), get_prices)
        summary = build_cycle_summary(coin_results, mode, len(x_signals), len(cmc_signals))

        report.stats["bot_cycle_coins"] = len(coin_results)
        report.stats["trading_mode"] = mode
        report.ok(
            "Bot startup cycle",
            f"mode={mode}, watchlist={len(coin_results)} coins, summary len={len(summary)}",
        )
    except Exception as e:
        report.fail("Bot startup cycle", traceback.format_exc()[-200:])


def run_risk_and_portfolio():
    print("\n▶ Phase 9: Risk manager + portfolio...")
    from risk.risk_manager import RiskManager
    from strategies.positions import list_active_positions
    from data_manager import load_trade_history

    try:
        rm = RiskManager()
        status = rm.status_summary()
        history = load_trade_history()
        active = list_active_positions()
        report.stats["portfolio_balance"] = status.get("virtual_balance", 0)
        report.stats["portfolio_positions"] = len(active)
        report.ok(
            "Risk/Portfolio",
            f"balance=${status.get('virtual_balance', 0):.0f}, "
            f"positions={len(active)}, trades={len(history.get('trades', []))}",
        )
    except Exception as e:
        report.fail("Risk/Portfolio", str(e)[:120])


def main():
    print("=" * 72)
    print("X-AGENT BOT — FULL STRESS TEST (DEMO MODE)")
    print("=" * 72)

    coins = []
    try:
        print("\n▶ Fetching top 50 altcoins from CoinMarketCap...")
        coins = fetch_cmc_top_altcoins(50)
        report.stats["altcoins_fetched"] = len(coins)
        top5 = ", ".join(c["symbol"] for c in coins[:5])
        report.ok("CMC top altcoins", f"{len(coins)} coins — top: {top5}")
    except Exception as e:
        report.fail("CMC top altcoins", str(e))

    run_unit_tests()

    with patch("telegram_notifier.send_telegram_message", side_effect=mock_telegram):
        run_command_tests()

    if coins:
        run_price_fetch_top50(coins)
        run_trading_trials(coins)
        run_orchestrator_cycle(coins)

    run_social_pipeline()
    run_sandbox_cycle()
    run_bot_startup()
    run_risk_and_portfolio()

    ok = report.print_summary()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()