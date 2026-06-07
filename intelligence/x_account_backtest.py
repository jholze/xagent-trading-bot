from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

from core.actions import is_buy, is_sell
from core.config import get_bot_config
from core.models import MarketContext
from data_manager import get_config, load_x_accounts
from historical_prices import (
    check_target_hit,
    get_indicators_at_time,
    get_path_extremes,
    get_price_at_time,
    get_return_pct,
    prefetch_for_posts,
)
from logger import log
from strategies.decision_engine import DecisionEngine
from x_analyzer import XAnalyzer, XSignal
from x_data_provider import RawPost, get_x_provider


@dataclass
class BacktestSignal:
    post_id: str
    timestamp: datetime
    coin: str
    action: str
    confidence: int
    rationale: str
    raw_tweet: str
    signal_price: float
    exit_price: float
    return_24h: float
    was_correct: bool
    pnl_if_followed: float = 0.0
    price_target: Optional[float] = None
    stop_loss: Optional[float] = None
    return_7d: float = 0.0
    was_correct_7d: bool = False
    target_hit: Optional[bool] = None
    has_price_target: bool = False
    bot_would_trade: bool = False
    bot_action: str = "HOLD"
    bot_was_correct_7d: Optional[bool] = None
    rsi_at_signal: Optional[float] = None
    exit_price_7d: float = 0.0


@dataclass
class BacktestResult:
    handle: str
    days: int
    tweets_fetched: int
    signals: List[BacktestSignal] = field(default_factory=list)
    skipped_no_price: int = 0
    skipped_non_trade: int = 0
    error: str = ""

    @property
    def trade_signals(self) -> List[BacktestSignal]:
        return self.signals

    def _hit_stats(self, signals: list, attr: str) -> dict:
        if not signals:
            return {"count": 0, "hits": 0, "hit_rate": 0.0}
        hits = sum(1 for s in signals if getattr(s, attr))
        return {
            "count": len(signals),
            "hits": hits,
            "hit_rate": round(hits / len(signals), 3),
        }

    def _action_stats(self, action: str, attr: str = "was_correct") -> dict:
        subset = [s for s in self.signals if s.action == action]
        if not subset:
            return {"count": 0, "hits": 0, "hit_rate": 0.0}
        hits = sum(1 for s in subset if getattr(s, attr))
        return {
            "count": len(subset),
            "hits": hits,
            "hit_rate": round(hits / len(subset), 3),
        }

    def summary_stats(self) -> dict:
        if not self.signals:
            empty = {"count": 0, "hits": 0, "hit_rate": 0.0}
            return {
                "samples": 0,
                "hits_24h": 0,
                "hit_rate_24h": 0.0,
                "hits_7d": 0,
                "hit_rate_7d": 0.0,
                "avg_return_24h": 0.0,
                "avg_return_7d": 0.0,
                "hypothetical_pnl": 0.0,
                "buy": self._action_stats("BUY"),
                "sell": self._action_stats("SELL"),
                "target": empty,
                "bot": empty,
            }

        with_target = [s for s in self.signals if s.has_price_target]
        bot_trades = [s for s in self.signals if s.bot_would_trade and s.bot_was_correct_7d is not None]

        return {
            "samples": len(self.signals),
            "hits_24h": sum(1 for s in self.signals if s.was_correct),
            "hit_rate_24h": round(sum(1 for s in self.signals if s.was_correct) / len(self.signals), 3),
            "hits_7d": sum(1 for s in self.signals if s.was_correct_7d),
            "hit_rate_7d": round(sum(1 for s in self.signals if s.was_correct_7d) / len(self.signals), 3),
            "avg_return_24h": round(sum(s.return_24h for s in self.signals) / len(self.signals), 2),
            "avg_return_7d": round(sum(s.return_7d for s in self.signals) / len(self.signals), 2),
            "hypothetical_pnl": round(sum(s.pnl_if_followed for s in self.signals), 2),
            "buy": self._action_stats("BUY", "was_correct_7d"),
            "sell": self._action_stats("SELL", "was_correct_7d"),
            "target": self._hit_stats(with_target, "target_hit"),
            "bot": self._hit_stats(bot_trades, "bot_was_correct_7d"),
            "bot_trade_count": sum(1 for s in self.signals if s.bot_would_trade),
        }

    def to_telegram_summary(self) -> str:
        if self.error:
            return f"❌ Backtest @{self.handle} fehlgeschlagen: {self.error}"

        stats = self.summary_stats()
        if self.tweets_fetched == 0:
            return (
                f"📊 <b>Backtest @{self.handle}</b> ({self.days} Tage)\n\n"
                "Keine Tweets im Zeitraum gefunden."
            )

        lines = [
            f"📊 <b>Backtest @{self.handle}</b> ({self.days} Tage)",
            "",
            f"Tweets geladen: {self.tweets_fetched}",
            f"BUY/SELL-Signale: {stats['samples']}",
            f"Übersprungen (kein Preis): {self.skipped_no_price}",
            f"Übersprungen (HOLD/IGNORE): {self.skipped_non_trade}",
        ]

        if stats["samples"] == 0:
            lines.append("")
            lines.append("Keine handelbaren BUY/SELL-Signale im Zeitraum.")
            return "\n".join(lines)

        lines.extend([
            "",
            "<b>Bewertung — Richtung</b>",
            f"24h: <b>{stats['hit_rate_24h']*100:.0f}%</b> ({stats['hits_24h']}/{stats['samples']}) | Ø {stats['avg_return_24h']:+.1f}%",
            f"7d (Swing): <b>{stats['hit_rate_7d']*100:.0f}%</b> ({stats['hits_7d']}/{stats['samples']}) | Ø {stats['avg_return_7d']:+.1f}%",
        ])

        target = stats["target"]
        if target["count"] > 0:
            lines.append("")
            lines.append("<b>Bewertung — Tweet-Ziel (TP)</b>")
            lines.append(
                f"Ziel erreicht: <b>{target['hit_rate']*100:.0f}%</b> ({target['hits']}/{target['count']} mit TP im Tweet)"
            )
        else:
            lines.append("")
            lines.append("<b>Tweet-Ziel (TP):</b> keine Signale mit extrahiertem Kursziel")

        bot_count = stats.get("bot_trade_count", 0)
        bot = stats["bot"]
        lines.append("")
        lines.append("<b>Bewertung — Bot-Strategie (X + RSI/BB)</b>")
        lines.append(f"Würde handeln: {bot_count}/{stats['samples']} Signale")
        if bot["count"] > 0:
            lines.append(
                f"Treffer 7d (nur Bot-Trades): <b>{bot['hit_rate']*100:.0f}%</b> ({bot['hits']}/{bot['count']})"
            )
        else:
            lines.append("Kein Signal hätte X + Technik gemeinsam ausgelöst")

        lines.extend([
            "",
            f"Hypothetischer PnL (24h): <b>${stats['hypothetical_pnl']:+.2f}</b>",
            "",
            f"🟢 BUY (7d): {stats['buy']['count']} Signale, {stats['buy']['hit_rate']*100:.0f}% Treffer",
            f"🔴 SELL (7d): {stats['sell']['count']} Signale, {stats['sell']['hit_rate']*100:.0f}% Treffer",
        ])

        examples = sorted(self.signals, key=lambda s: s.timestamp, reverse=True)[:3]
        if examples:
            lines.append("")
            lines.append("<b>Letzte Signale:</b>")
            for sig in examples:
                mark = "✅" if sig.was_correct_7d else "❌"
                tp = " 🎯" if sig.target_hit else (" ⭕" if sig.has_price_target else "")
                bot = " 🤖" if sig.bot_would_trade else ""
                ts = sig.timestamp.strftime("%Y-%m-%d %H:%M")
                lines.append(
                    f"{mark}{tp}{bot} {ts} | {sig.action} {sig.coin} | 7d {sig.return_7d:+.1f}%"
                )

        return "\n".join(lines)


class XAccountBacktester:
    def __init__(self, config: dict = None, progress_callback: Callable[[str], None] = None):
        self.config = config or get_config()
        self.backtest_cfg = self.config.get("x_backtest", {})
        self.accuracy_cfg = self.config.get("accuracy", {})
        self.buy_success_pct = self.accuracy_cfg.get("buy_success_pct", 3.0)
        self.sell_success_pct = self.accuracy_cfg.get("sell_success_pct", -2.0)
        self.max_usdt = self.config.get("max_usdt_per_trade", 150)
        self.max_hold_days = int(self.backtest_cfg.get("max_hold_days", 7))
        self.target_tolerance_pct = float(self.backtest_cfg.get("target_tolerance_pct", 0.5))
        self.min_signal_age_hours = int(self.backtest_cfg.get("min_signal_age_hours", 24))
        self.analyzer = XAnalyzer()
        self.provider = get_x_provider(self.config)
        self.progress_callback = progress_callback
        self._decision_engine = DecisionEngine()

    def _notify(self, message: str):
        if self.progress_callback:
            self.progress_callback(message)

    def _parse_ts(self, ts: str) -> datetime:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return datetime.now(timezone.utc) - timedelta(days=30)

    def _direction_correct(self, action: str, return_pct: float) -> bool:
        if action == "BUY":
            return return_pct >= self.buy_success_pct
        if action == "SELL":
            return return_pct <= self.sell_success_pct
        return False

    def _display_return_pct(self, action: str, signal_price: float, exit_price: float) -> float:
        """Positive = profitable if you followed the signal."""
        raw = get_return_pct(signal_price, exit_price)
        return -raw if action == "SELL" else raw

    def _pnl(self, action: str, signal_price: float, exit_price: float) -> float:
        amount = self.max_usdt / signal_price if signal_price else 0
        if action == "BUY":
            return (exit_price - signal_price) * amount
        if action == "SELL":
            return (signal_price - exit_price) * amount
        return 0.0

    def _evaluate_bot_strategy(self, parsed: XSignal, signal_price: float, signal_time: datetime) -> tuple[bool, str, Optional[float]]:
        symbol = f"{parsed.coin.upper()}/USDT"
        indicators = get_indicators_at_time(symbol, signal_time, timeframe="4h")
        if not indicators:
            return False, "HOLD", None

        bot_config = get_bot_config()
        coin = {"symbol": symbol, "timeframe": "4h"}
        market = MarketContext(
            symbol=symbol,
            timeframe="4h",
            current_price=signal_price,
            rsi=indicators["rsi"],
            lower_bb=indicators["lower_bb"],
            vol_multiplier=indicators["vol_multiplier"],
            has_position=False,
            average_entry=0.0,
            open_positions=0,
            strategy_params=bot_config.strategy_params(symbol, "4h"),
        )

        parsed.trust_score = self.analyzer.get_trust_score(parsed.account)
        parsed.effective_confidence = parsed.confidence * (parsed.trust_score / 100)

        try:
            analysis = self._decision_engine.evaluate_with_market(
                coin, market, x_signals=[parsed],
            )
        except Exception as e:
            log(f"Backtest bot strategy eval failed for {symbol}: {e}", "WARNING")
            return False, "HOLD", indicators["rsi"]

        would_trade = analysis.recommended and (
            (parsed.action == "BUY" and is_buy(analysis.normalized_action))
            or (parsed.action == "SELL" and is_sell(analysis.normalized_action))
        )
        return would_trade, analysis.normalized_action, indicators["rsi"]

    def run(self, handle: str, days: int = None) -> BacktestResult:
        default_days = self.backtest_cfg.get("default_days", 60)
        max_days = self.backtest_cfg.get("max_days", 365)
        max_posts = self.backtest_cfg.get("max_posts", 50)
        days = max(1, min(days or default_days, max_days))

        result = BacktestResult(handle=handle, days=days, tweets_fetched=0)

        source = "Grok X Search" if self.config.get("use_grok_x_search") else "X API"
        self._notify(f"📥 Lade Tweets für @{handle} ({days} Tage) via {source}…")
        posts = self.provider.fetch_historical_posts(handle, days=days, max_posts=max_posts, config=self.config)
        result.tweets_fetched = len(posts)

        if not posts:
            return result

        self._notify(f"🔍 Analysiere {len(posts)} Tweets mit Grok (Batch)…")

        parsed_by_id = self.analyzer.parse_tweets_batch(posts)
        trade_candidates = []
        min_age = timedelta(hours=self.min_signal_age_hours)
        now = datetime.now(timezone.utc)

        for post in posts:
            parsed = parsed_by_id.get(post.post_id)
            if not parsed:
                continue
            action = parsed.action.upper()
            coin = parsed.coin.upper()
            if action not in ("BUY", "SELL") or coin in ("", "UNKNOWN"):
                result.skipped_non_trade += 1
                continue
            signal_time = self._parse_ts(post.created_at)
            if signal_time > now - min_age:
                result.skipped_no_price += 1
                continue
            if signal_time > now - timedelta(days=self.max_hold_days):
                result.skipped_no_price += 1
                continue
            trade_candidates.append((post, parsed, signal_time))

        if trade_candidates:
            prefetch_for_posts(
                [(f"{p.coin.upper()}/USDT", t) for _, p, t in trade_candidates],
                hold_days=self.max_hold_days,
            )

        self._notify(f"📈 Bewerte {len(trade_candidates)} Signale (24h / 7d / Ziel / Bot-Strategie)…")

        for i, (post, parsed, signal_time) in enumerate(trade_candidates, 1):
            if i == 1 or i == len(trade_candidates) or i % 5 == 0:
                self._notify(f"🔍 Fortschritt: {i}/{len(trade_candidates)} Signale…")

            signal = self._evaluate_parsed(post, parsed, signal_time, result)
            if signal:
                result.signals.append(signal)

        return result

    def _evaluate_parsed(
        self,
        post: RawPost,
        parsed: XSignal,
        signal_time: datetime,
        result: BacktestResult,
    ) -> Optional[BacktestSignal]:
        action = parsed.action.upper()
        coin = parsed.coin.upper()
        symbol = f"{coin}/USDT"
        signal_price = get_price_at_time(symbol, signal_time)
        exit_24h = get_price_at_time(symbol, signal_time + timedelta(hours=24))
        exit_7d = get_price_at_time(symbol, signal_time + timedelta(days=self.max_hold_days))

        if not signal_price or not exit_24h or not exit_7d:
            result.skipped_no_price += 1
            return None

        raw_24h = get_return_pct(signal_price, exit_24h)
        raw_7d = get_return_pct(signal_price, exit_7d)
        return_24h = self._display_return_pct(action, signal_price, exit_24h)
        return_7d = self._display_return_pct(action, signal_price, exit_7d)
        was_correct_24h = self._direction_correct(action, raw_24h)
        was_correct_7d = self._direction_correct(action, raw_7d)

        price_target = parsed.price_target
        has_target = price_target is not None and price_target > 0
        target_hit = None
        if has_target:
            hold_end = signal_time + timedelta(days=self.max_hold_days)
            max_high, min_low = get_path_extremes(symbol, signal_time, hold_end)
            target_hit = check_target_hit(
                action, signal_price, price_target, max_high, min_low, self.target_tolerance_pct,
            )

        bot_would_trade, bot_action, rsi = self._evaluate_bot_strategy(parsed, signal_price, signal_time)
        bot_was_correct_7d = None
        if bot_would_trade:
            bot_was_correct_7d = was_correct_7d

        return BacktestSignal(
            post_id=post.post_id,
            timestamp=signal_time,
            coin=coin,
            action=action,
            confidence=parsed.confidence,
            rationale=parsed.rationale,
            raw_tweet=post.text[:120],
            signal_price=signal_price,
            exit_price=exit_24h,
            exit_price_7d=exit_7d,
            return_24h=round(return_24h, 2),
            return_7d=round(return_7d, 2),
            was_correct=was_correct_24h,
            was_correct_7d=was_correct_7d,
            pnl_if_followed=round(self._pnl(action, signal_price, exit_24h), 2),
            price_target=price_target,
            stop_loss=parsed.stop_loss,
            has_price_target=has_target,
            target_hit=target_hit,
            bot_would_trade=bot_would_trade,
            bot_action=bot_action,
            bot_was_correct_7d=bot_was_correct_7d,
            rsi_at_signal=rsi,
        )