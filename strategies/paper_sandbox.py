import math
from datetime import datetime, timedelta

from core.config import get_bot_config
from core.models import MarketContext, SandboxMetrics
from data_manager import (
    load_paper_sandbox_history,
    load_paper_strategies,
    save_paper_sandbox_history,
    save_paper_strategies,
)
from logger import log
from services.market_service import MarketService
from strategies.positions import is_open_position
from strategies.registry import promote_hypothesis_to_config
from strategies.technical_rsi_bb import TechnicalRSIStrategy


class PaperSandbox:
    """Isolated paper portfolios for testing strategy hypotheses."""

    def __init__(self, config=None, market_service: MarketService = None):
        self.config = config or get_bot_config()
        self.market = market_service or MarketService()
        self.strategy = TechnicalRSIStrategy()

    def _sandbox_cfg(self) -> dict:
        return self.config.sandbox_config

    def _ensure_portfolio(self, hypothesis_id: str) -> dict:
        data = load_paper_sandbox_history()
        portfolios = data.setdefault("portfolios", {})
        if hypothesis_id not in portfolios:
            capital = float(self._sandbox_cfg().get("initial_capital_usdt", 1000))
            portfolios[hypothesis_id] = {
                "virtual_balance": capital,
                "realized_pnl": 0.0,
                "equity_peak": capital,
                "trades": [],
                "positions": {},
            }
            save_paper_sandbox_history(data)
        return portfolios[hypothesis_id]

    def _get_position(self, portfolio: dict, symbol: str) -> dict:
        positions = portfolio.setdefault("positions", {})
        if symbol not in positions:
            positions[symbol] = {"amount": 0.0, "average_entry": 0.0}
        return positions[symbol]

    def _trade_size(self) -> float:
        return float(self._sandbox_cfg().get("usdt_per_trade", 50))

    def _execute_buy(self, portfolio: dict, symbol: str, price: float, usdt: float) -> bool:
        if price <= 0 or usdt <= 0 or portfolio["virtual_balance"] < usdt:
            return False
        amount = usdt / price
        pos = self._get_position(portfolio, symbol)
        old_amount = pos["amount"]
        new_amount = old_amount + amount
        if old_amount > 0:
            pos["average_entry"] = (pos["average_entry"] * old_amount + price * amount) / new_amount
        else:
            pos["average_entry"] = price
        pos["amount"] = new_amount
        portfolio["virtual_balance"] -= usdt
        portfolio["trades"].append({
            "type": "BUY",
            "symbol": symbol,
            "price": price,
            "amount": amount,
            "usdt_amount": usdt,
            "timestamp": datetime.now().isoformat(),
        })
        return True

    def _execute_sell(self, portfolio: dict, symbol: str, price: float, fraction: float) -> bool:
        pos = self._get_position(portfolio, symbol)
        if pos["amount"] <= 0 or price <= 0:
            return False
        amount = pos["amount"] * fraction
        entry = pos["average_entry"] or price
        received = price * amount * (1 - self.config.slippage_percent / 100)
        pnl = (price - entry) * amount
        pos["amount"] -= amount
        if pos["amount"] < 1e-8:
            pos["amount"] = 0.0
            pos["average_entry"] = 0.0
        portfolio["virtual_balance"] += received
        portfolio["realized_pnl"] += pnl
        portfolio["trades"].append({
            "type": "SELL",
            "symbol": symbol,
            "price": price,
            "amount": amount,
            "usdt_received": received,
            "pnl": pnl,
            "timestamp": datetime.now().isoformat(),
        })
        return True

    def _sell_fraction(self, action: str) -> float:
        if "FULL" in action or action == "SELL":
            return 1.0
        if "PARTIAL" in action or "50" in action:
            return 0.5
        if "30" in action:
            return 0.3
        return 0.2

    def evaluate_hypothesis(self, hypothesis: dict, symbol: str, price: float) -> str | None:
        if not price or price <= 0:
            return None
        if hypothesis.get("status") != "testing":
            return None

        tf = hypothesis.get("timeframe", "4h")
        params = hypothesis.get("params") or {}
        indicators = self.market.fetch_indicators(symbol, tf, price)
        portfolio = self._ensure_portfolio(hypothesis["id"])
        pos = self._get_position(portfolio, symbol)

        market = MarketContext(
            symbol=symbol,
            timeframe=tf,
            current_price=price,
            rsi=indicators["rsi"],
            lower_bb=indicators["lower_bb"],
            vol_multiplier=indicators["vol_multiplier"],
            has_position=pos["amount"] > 0,
            average_entry=pos.get("average_entry", 0),
            open_positions=sum(
                1 for p in portfolio["positions"].values() if is_open_position(p)
            ),
            strategy_params=params,
        )

        coin = {"symbol": symbol, "timeframe": tf, "strategy_params": params}
        analysis = self.strategy.analyze(coin, market)
        action = analysis.action

        executed = None
        if action == "BUY" and pos["amount"] <= 0:
            if self._execute_buy(portfolio, symbol, price, self._trade_size()):
                executed = "BUY"
        elif "SELL" in action and pos["amount"] > 0:
            if self._execute_sell(portfolio, symbol, price, self._sell_fraction(action)):
                executed = action

        if executed:
            data = load_paper_sandbox_history()
            data.setdefault("portfolios", {})[hypothesis["id"]] = portfolio
            save_paper_sandbox_history(data)
        return executed

    def compute_metrics(self, hypothesis_id: str, mark_prices: dict = None) -> SandboxMetrics:
        portfolio = self._ensure_portfolio(hypothesis_id)
        trades = portfolio.get("trades", [])
        sells = [t for t in trades if t.get("type") == "SELL"]
        wins = [t for t in sells if t.get("pnl", 0) > 0]
        win_rate = (len(wins) / len(sells) * 100) if sells else 0.0

        returns = []
        for trade in sells:
            usdt = trade.get("usdt_received", 0)
            if usdt > 0:
                returns.append(trade.get("pnl", 0) / usdt)

        if len(returns) > 1:
            mean_r = sum(returns) / len(returns)
            var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
            std = math.sqrt(var) if var > 0 else 0
            sharpe = (mean_r / std) * math.sqrt(len(returns)) if std > 0 else 0.0
        elif returns:
            sharpe = 1.0 if returns[0] > 0 else -1.0
        else:
            sharpe = 0.0

        equity = portfolio["virtual_balance"]
        mark_prices = mark_prices or {}
        for symbol, pos in portfolio.get("positions", {}).items():
            if pos.get("amount", 0) > 0:
                px = mark_prices.get(symbol, pos.get("average_entry", 0))
                equity += pos["amount"] * px

        peak = float(portfolio.get("equity_peak", equity))
        peak = max(peak, equity)
        portfolio["equity_peak"] = peak
        max_dd = max(0.0, (peak - equity) / peak * 100) if peak > 0 else 0.0

        return SandboxMetrics(
            win_rate=round(win_rate, 1),
            sharpe=round(sharpe, 2),
            max_drawdown_pct=round(max_dd, 1),
            trades=len(trades),
            realized_pnl=round(portfolio.get("realized_pnl", 0), 2),
            equity=round(equity, 2),
        )

    def _persist_metrics(self, hypothesis_id: str, metrics: SandboxMetrics):
        data = load_paper_strategies()
        for hyp in data.get("hypotheses", []):
            if hyp.get("id") == hypothesis_id:
                hyp["metrics"] = {
                    "win_rate": metrics.win_rate,
                    "sharpe": metrics.sharpe,
                    "max_drawdown_pct": metrics.max_drawdown_pct,
                    "trades": metrics.trades,
                    "realized_pnl": metrics.realized_pnl,
                    "equity": metrics.equity,
                    "updated_at": datetime.now().isoformat(),
                }
                break
        save_paper_strategies(data)

    def run_cycle(self, watchlist: list, price_fn) -> list:
        if not self.config.sandbox_enabled:
            return []

        hypotheses = [h for h in load_paper_strategies().get("hypotheses", []) if h.get("status") == "testing"]
        if not hypotheses:
            return []

        results = []
        mark_prices = {}
        for coin in watchlist:
            symbol = coin.get("symbol", "")
            if symbol:
                price, _, _ = price_fn(symbol)
                if price:
                    mark_prices[symbol] = price

        data = load_paper_sandbox_history()
        for hyp in hypotheses:
            symbol = hyp.get("symbol") or (watchlist[0]["symbol"] if watchlist else "")
            if not symbol:
                continue
            price = mark_prices.get(symbol)
            if not price:
                price, _, _ = price_fn(symbol)
            if not price:
                continue

            action = self.evaluate_hypothesis(hyp, symbol, price)
            metrics = self.compute_metrics(hyp["id"], mark_prices)
            data["portfolios"][hyp["id"]] = self._ensure_portfolio(hyp["id"])
            self._persist_metrics(hyp["id"], metrics)

            if action:
                results.append({
                    "hypothesis_id": hyp["id"],
                    "name": hyp.get("name"),
                    "symbol": symbol,
                    "action": action,
                    "metrics": metrics,
                })
                log(f"Sandbox {hyp['id']}: {action} {symbol} (WR={metrics.win_rate}%)", "INFO")

        save_paper_sandbox_history(data)
        self._expire_stale_hypotheses()
        return results

    def _expire_stale_hypotheses(self):
        max_days = int(self._sandbox_cfg().get("max_test_days", 30))
        cutoff = datetime.now() - timedelta(days=max_days)
        data = load_paper_strategies()
        changed = False
        for hyp in data.get("hypotheses", []):
            if hyp.get("status") != "testing":
                continue
            created = hyp.get("created_at", "")
            try:
                ts = datetime.fromisoformat(created.replace("Z", ""))
            except Exception:
                continue
            if ts < cutoff:
                hyp["status"] = "expired"
                changed = True
        if changed:
            save_paper_strategies(data)

    def list_testing(self) -> list:
        return [h for h in load_paper_strategies().get("hypotheses", []) if h.get("status") == "testing"]

    def promotion_ready(self, hypothesis_id: str) -> tuple:
        hyp = None
        for h in load_paper_strategies().get("hypotheses", []):
            if h.get("id") == hypothesis_id:
                hyp = h
                break
        if not hyp:
            return False, "Hypothesis not found"
        if hyp.get("status") != "testing":
            return False, f"Status is {hyp.get('status')}, not testing"

        min_days = int(self._sandbox_cfg().get("min_test_days", 7))
        try:
            created = datetime.fromisoformat(hyp.get("created_at", "").replace("Z", ""))
            age_days = (datetime.now() - created).days
        except Exception:
            age_days = 0
        if age_days < min_days:
            return False, f"Only {age_days}d tested (min {min_days}d)"

        promo = self._sandbox_cfg().get("promotion", {})
        metrics = hyp.get("metrics") or self.compute_metrics(hypothesis_id).__dict__
        if metrics.get("trades", 0) < promo.get("min_trades", 3):
            return False, f"Need {promo.get('min_trades', 3)} trades (have {metrics.get('trades', 0)})"
        if metrics.get("win_rate", 0) < promo.get("min_win_rate", 55):
            return False, f"Win rate {metrics.get('win_rate', 0):.0f}% below {promo.get('min_win_rate', 55)}%"
        if metrics.get("sharpe", 0) < promo.get("min_sharpe", 0.5):
            return False, f"Sharpe {metrics.get('sharpe', 0):.2f} below {promo.get('min_sharpe', 0.5)}"
        if metrics.get("max_drawdown_pct", 100) > promo.get("max_drawdown_pct", 15):
            return False, f"Drawdown {metrics.get('max_drawdown_pct', 0):.0f}% above limit"

        return True, "Ready for promotion"

    def promote(self, hypothesis_id: str) -> tuple:
        ready, reason = self.promotion_ready(hypothesis_id)
        if not ready:
            return False, reason

        hyp = None
        data = load_paper_strategies()
        for h in data.get("hypotheses", []):
            if h.get("id") == hypothesis_id:
                hyp = h
                break
        if not hyp:
            return False, "Hypothesis not found"

        ok, msg = promote_hypothesis_to_config(hyp)
        if not ok:
            return False, msg

        hyp["status"] = "promoted"
        hyp["promoted_at"] = datetime.now().isoformat()
        save_paper_strategies(data)
        log(f"Promoted sandbox hypothesis {hypothesis_id} to active strategies", "INFO")
        return True, f"Promoted '{hyp.get('name')}' to config.strategies[]"