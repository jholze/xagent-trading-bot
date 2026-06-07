import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import ccxt
import pandas as pd
import talib

from core.config import get_bot_config
from core.models import MarketContext, SandboxMetrics
from logger import log
from strategies.technical_rsi_bb import TechnicalRSIStrategy


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    params: dict
    metrics: SandboxMetrics
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)
    bars_tested: int = 0


class Backtester:
    """Historical bar-by-bar backtest using TechnicalRSIStrategy."""

    EXCHANGES = ["gate", "binance", "kucoin", "bybit"]

    def __init__(self, config=None):
        self.config = config or get_bot_config()
        self.strategy = TechnicalRSIStrategy()
        self.hermes = self.config.hermes_config

    def run(
        self,
        symbol: str,
        timeframe: str,
        params: dict,
        days: int | None = None,
        ohlcv_df: pd.DataFrame | None = None,
    ) -> BacktestResult:
        days = days or int(self.hermes.get("backtest_days", 60))
        df = ohlcv_df if ohlcv_df is not None else self._fetch_ohlcv(symbol, timeframe, days)
        if df is None or df.empty or len(df) < 30:
            log(f"Backtest insufficient data for {symbol} {timeframe}", "WARNING")
            return BacktestResult(
                symbol=symbol,
                timeframe=timeframe,
                params=params,
                metrics=SandboxMetrics(),
                bars_tested=len(df) if df is not None else 0,
            )

        df = self._add_indicators(df)
        return self._simulate(symbol, timeframe, params, df)

    def _fetch_ohlcv(self, symbol: str, timeframe: str, days: int) -> pd.DataFrame | None:
        since_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
        for ex_name in self.EXCHANGES:
            try:
                exchange = getattr(ccxt, ex_name)({"enableRateLimit": True, "timeout": 15000})
                bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=1000)
                if bars:
                    df = pd.DataFrame(bars, columns=["ts", "open", "high", "low", "close", "volume"])
                    return df
            except Exception as e:
                log(f"Backtest {ex_name} fetch failed for {symbol}: {e}", "WARNING")
        return None

    def _add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["rsi"] = talib.RSI(out["close"], timeperiod=14)
        _, _, out["lower"] = talib.BBANDS(out["close"], timeperiod=20)
        out["vol_avg"] = out["volume"].rolling(window=20).mean()
        return out.dropna()

    def _simulate(self, symbol: str, timeframe: str, params: dict, df: pd.DataFrame) -> BacktestResult:
        capital = float(self.hermes.get("initial_capital_usdt", 1000))
        usdt_per_trade = float(self.hermes.get("usdt_per_trade", 50))
        slippage = self.config.slippage_percent / 100
        stop_loss_pct = params.get("stop_loss_pct", self.config.stop_loss_pct)

        balance = capital
        position = {"amount": 0.0, "average_entry": 0.0}
        trades = []
        equity_curve = []
        peak_equity = capital

        coin = {"symbol": symbol, "timeframe": timeframe, "strategy_params": params}

        for i in range(20, len(df)):
            row = df.iloc[i]
            price = float(row["close"])
            recent_vol = df["volume"].iloc[max(0, i - 3):i + 1].mean()
            long_vol = float(row["vol_avg"]) if row["vol_avg"] > 0 else 1.0
            vol_mult = recent_vol / long_vol if long_vol > 0 else 1.0

            open_positions = 1 if position["amount"] > 0 else 0
            market = MarketContext(
                symbol=symbol,
                timeframe=timeframe,
                current_price=price,
                rsi=float(row["rsi"]),
                lower_bb=float(row["lower"]),
                vol_multiplier=float(vol_mult),
                has_position=position["amount"] > 0,
                average_entry=position["average_entry"],
                open_positions=open_positions,
                strategy_params=params,
            )

            analysis = self.strategy.analyze(coin, market)
            action = analysis.action

            if action == "BUY" and position["amount"] <= 0 and balance >= usdt_per_trade:
                cost = usdt_per_trade * (1 + slippage)
                if balance >= cost:
                    amount = usdt_per_trade / price
                    position["amount"] = amount
                    position["average_entry"] = price
                    balance -= cost
                    trades.append({
                        "type": "BUY",
                        "price": price,
                        "amount": amount,
                        "usdt": usdt_per_trade,
                        "bar": i,
                    })

            elif position["amount"] > 0:
                sell_fraction = self._sell_fraction(action)
                if sell_fraction > 0:
                    entry = position["average_entry"]
                    loss_pct = (price / entry - 1) * -100 if entry > 0 else 0
                    force_stop = loss_pct > stop_loss_pct and "SELL" not in action
                    if sell_fraction > 0 or force_stop:
                        frac = 1.0 if force_stop else sell_fraction
                        sell_amount = position["amount"] * frac
                        received = price * sell_amount * (1 - slippage)
                        pnl = (price - entry) * sell_amount
                        balance += received
                        position["amount"] -= sell_amount
                        if position["amount"] < 1e-10:
                            position["amount"] = 0.0
                            position["average_entry"] = 0.0
                        trades.append({
                            "type": "SELL",
                            "price": price,
                            "amount": sell_amount,
                            "pnl": pnl,
                            "usdt_received": received,
                            "bar": i,
                            "action": action,
                        })

            equity = balance + position["amount"] * price
            peak_equity = max(peak_equity, equity)
            equity_curve.append(equity)

        metrics = self._compute_metrics(trades, balance, position, df.iloc[-1]["close"], peak_equity, capital)
        return BacktestResult(
            symbol=symbol,
            timeframe=timeframe,
            params=params,
            metrics=metrics,
            trades=trades,
            equity_curve=equity_curve,
            bars_tested=len(df) - 20,
        )

    def _sell_fraction(self, action: str) -> float:
        if "FULL" in action or action == "SELL":
            return 1.0
        if "PARTIAL" in action or "50" in action or "20" in action:
            if "20" in action:
                return 0.2
            if "30" in action:
                return 0.3
            return 0.5
        if "30" in action:
            return 0.3
        if "SELL" in action:
            return 1.0
        return 0.0

    def _compute_metrics(
        self,
        trades: list,
        balance: float,
        position: dict,
        last_price: float,
        peak_equity: float,
        initial_capital: float,
    ) -> SandboxMetrics:
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

        equity = balance + position.get("amount", 0) * float(last_price)
        peak = max(peak_equity, equity, initial_capital)
        max_dd = max(0.0, (peak - equity) / peak * 100) if peak > 0 else 0.0
        realized_pnl = sum(t.get("pnl", 0) for t in sells)

        return SandboxMetrics(
            win_rate=round(win_rate, 1),
            sharpe=round(sharpe, 2),
            max_drawdown_pct=round(max_dd, 1),
            trades=len(trades),
            realized_pnl=round(realized_pnl, 2),
            equity=round(equity, 2),
        )