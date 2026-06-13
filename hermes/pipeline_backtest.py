"""Full DecisionEngine + CMC replay backtest for Hermes 2.0."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from core.config import get_bot_config
from core.models import MarketContext, SandboxMetrics
from hermes.cmc_replay import load_posts_for_coin, signals_at_timestamp
from hermes.metrics import enrich_sandbox_metrics, sharpe_from_trades
from strategies.decision_engine import DecisionEngine
from strategies.positions import sell_fraction_for_signal


@dataclass
class PipelineBacktestResult:
    symbol: str
    timeframe: str
    params: dict
    metrics: SandboxMetrics
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)
    bars_tested: int = 0
    decision_buys: int = 0


class PipelineBacktester:
    """Bar-by-bar simulation through DecisionEngine with optional CMC replay."""

    def __init__(self, config=None):
        self.config = config or get_bot_config()
        self.hermes = self.config.hermes_config
        self.decision_engine = DecisionEngine()

    def run(
        self,
        symbol: str,
        timeframe: str,
        params: dict,
        ohlcv_df: pd.DataFrame,
        cmc_ttl_hours: float = 4.0,
    ) -> PipelineBacktestResult:
        df = ohlcv_df.copy()
        if df is None or df.empty or len(df) < 30:
            return PipelineBacktestResult(
                symbol=symbol,
                timeframe=timeframe,
                params=params,
                metrics=SandboxMetrics(),
                bars_tested=len(df) if df is not None else 0,
            )

        df = self._add_indicators(df)
        since_ms = int(df["ts"].iloc[0])
        until_ms = int(df["ts"].iloc[-1])
        cmc_posts = load_posts_for_coin(symbol, since_ms=since_ms, until_ms=until_ms + int(cmc_ttl_hours * 3600 * 1000))
        ttl_ms = int(cmc_ttl_hours * 3600 * 1000)
        trust = float(params.get("cmc_trust_score", 65.0))

        capital = float(self.hermes.get("initial_capital_usdt", 1000))
        usdt_per_trade = float(self.hermes.get("usdt_per_trade", 50))
        slippage = self.config.slippage_percent / 100

        balance = capital
        position = {"amount": 0.0, "average_entry": 0.0}
        sim_state = {
            "last_rsi": 45.0,
            "last_ampel": "🟡",
            "rsi_sell_tiers_done": {},
        }
        trades = []
        equity_curve = []
        peak_equity = capital
        max_drawdown_pct = 0.0
        decision_buys = 0

        coin = {"symbol": symbol, "timeframe": timeframe, "strategy_params": params}

        for i in range(20, len(df)):
            row = df.iloc[i]
            price = float(row["close"])
            bar_ts = int(row["ts"])
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
                sim_state=sim_state,
            )

            cmc_signals = signals_at_timestamp(cmc_posts, bar_ts, trust_score=trust, ttl_ms=ttl_ms)
            analysis = self.decision_engine.evaluate_with_market(coin, market, cmc_signals=cmc_signals)
            action = analysis.action if analysis else "HOLD"

            if action == "BUY" and position["amount"] <= 0 and balance >= usdt_per_trade:
                cost = usdt_per_trade * (1 + slippage)
                if balance >= cost:
                    amount = usdt_per_trade / price
                    position["amount"] = amount
                    position["average_entry"] = price
                    balance -= cost
                    sim_state["rsi_sell_tiers_done"] = {}
                    decision_buys += 1
                    trades.append({
                        "type": "BUY",
                        "price": price,
                        "amount": amount,
                        "usdt": usdt_per_trade,
                        "bar": i,
                        "sources": list(analysis.sources or []),
                    })

            elif position["amount"] > 0 and action != "HOLD" and "SELL" in action:
                sell_fraction = sell_fraction_for_signal(action)
                if sell_fraction > 0:
                    entry = position["average_entry"]
                    sell_amount = position["amount"] * sell_fraction
                    received = price * sell_amount * (1 - slippage)
                    pnl = (price - entry) * sell_amount
                    balance += received
                    position["amount"] -= sell_amount
                    if position["amount"] < 1e-10:
                        position["amount"] = 0.0
                        position["average_entry"] = 0.0
                    tiers = dict(sim_state.get("rsi_sell_tiers_done") or {})
                    if "TP" in action.upper():
                        tiers["tp"] = True
                    elif "30" in action:
                        tiers["30"] = True
                    elif "20" in action:
                        tiers["20"] = True
                    sim_state["rsi_sell_tiers_done"] = tiers
                    trades.append({
                        "type": "SELL",
                        "price": price,
                        "amount": sell_amount,
                        "pnl": pnl,
                        "usdt_received": received,
                        "bar": i,
                        "action": action,
                    })

            sim_state["last_rsi"] = float(row["rsi"])
            equity = balance + position["amount"] * price
            peak_equity = max(peak_equity, equity)
            if peak_equity > 0:
                max_drawdown_pct = max(max_drawdown_pct, (peak_equity - equity) / peak_equity * 100)
            equity_curve.append(equity)

        bars_tested = len(df) - 20
        sells = [t for t in trades if t.get("type") == "SELL"]
        wins = [t for t in sells if t.get("pnl", 0) > 0]
        win_rate = (len(wins) / len(sells) * 100) if sells else 0.0
        equity = balance + position.get("amount", 0) * float(df.iloc[-1]["close"])
        realized_pnl = sum(t.get("pnl", 0) for t in sells)

        metrics = SandboxMetrics(
            win_rate=round(win_rate, 1),
            sharpe=sharpe_from_trades(trades),
            max_drawdown_pct=round(max_drawdown_pct, 1),
            trades=len(sells),
            realized_pnl=round(realized_pnl, 2),
            equity=round(equity, 2),
        )
        metrics = enrich_sandbox_metrics(metrics, trades, bars_tested)

        return PipelineBacktestResult(
            symbol=symbol,
            timeframe=timeframe,
            params=params,
            metrics=metrics,
            trades=trades,
            equity_curve=equity_curve,
            bars_tested=bars_tested,
            decision_buys=decision_buys,
        )

    def _add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        import talib

        out = df.copy()
        out["rsi"] = talib.RSI(out["close"], timeperiod=14)
        _, _, out["lower"] = talib.BBANDS(out["close"], timeperiod=20)
        out["vol_avg"] = out["volume"].rolling(window=20).mean()
        return out.dropna()