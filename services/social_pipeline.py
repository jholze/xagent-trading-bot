import threading
from datetime import datetime, timezone
from queue import Queue

from data_manager import add_coin, get_config, load_cmc_posts, load_effective_watchlist, load_lc_signals, log_cmc_post, log_lc_signal
from data.lunarcrush_provider import get_lc_provider
from data.lunarcrush_scorer import score_lc_metrics
from data.cmc_community_provider import CMCCommunityParser, get_cmc_provider
from intelligence.accuracy_tracker import AccuracyTracker
from intelligence.strategy_discovery import StrategyDiscovery
from logger import log
from price_fetcher import get_prices
from telegram_notifier import send_x_recommendation_message
from x_data_provider import RawPost, get_x_provider


class SocialPipeline:
    """Fetches X posts, tracks recommendations, and updates accuracy/trust."""

    def __init__(self, analyzer, orchestrator=None, notify_callback=None):
        self.analyzer = analyzer
        self.orchestrator = orchestrator
        self.notify_callback = notify_callback
        self.tracker = AccuracyTracker()
        self.discovery = StrategyDiscovery()
        self.provider = get_x_provider()
        self.cmc_provider = get_cmc_provider()
        self.cmc_parser = CMCCommunityParser()
        self.lc_provider = get_lc_provider()
        self._cycle_signals = []
        self._cycle_cmc_signals = []
        self._cycle_lc_signals = []
        self._last_lc_digest_sig = ""
        self._notified_post_ids = set()
        self._last_cmc_digest_sig = ""
        self._perf = get_config().get("x_performance", {})
        self._strategy_async = self._perf.get("strategy_discovery_async", True)
        self._strategy_queue: Queue = Queue()
        self._strategy_worker_started = False
        self._strategy_lock = threading.Lock()

    def _ensure_strategy_worker(self):
        if not self._strategy_async or self._strategy_worker_started:
            return
        with self._strategy_lock:
            if self._strategy_worker_started:
                return
            thread = threading.Thread(target=self._strategy_worker, daemon=True)
            thread.start()
            self._strategy_worker_started = True

    def _strategy_worker(self):
        while True:
            item = self._strategy_queue.get()
            if item is None:
                break
            text, account, post_id, symbol = item
            try:
                hypothesis = self.discovery.discover_from_tweet(text, account, post_id)
                if hypothesis:
                    if not hypothesis.symbol and symbol:
                        hypothesis.symbol = symbol
                    self.discovery.save_hypothesis(hypothesis)
            except Exception as e:
                log(f"Strategy discovery failed for @{account}: {e}", "WARNING")
            finally:
                self._strategy_queue.task_done()

    def _enqueue_strategy_discovery(self, post: RawPost, symbol: str | None):
        if not self.discovery._is_strategy_tweet(post.text):
            return
        if self._strategy_async:
            self._ensure_strategy_worker()
            self._strategy_queue.put((post.text, post.account, post.post_id, symbol))
            return
        hypothesis = self.discovery.discover_from_tweet(post.text, post.account, post.post_id)
        if hypothesis:
            if not hypothesis.symbol and symbol:
                hypothesis.symbol = symbol
            self.discovery.save_hypothesis(hypothesis)

    def _already_logged(self, post_id: str) -> bool:
        from data_manager import load_x_posts
        return any(
            p.get("post_id") == post_id
            for p in load_x_posts().get("posts", [])
        )

    def process_new_posts(self) -> list:
        accounts = self.analyzer.accounts
        raw_posts = self.provider.fetch_new_posts(accounts)
        recommendations = []
        self._cycle_signals = []

        new_posts = [post for post in raw_posts if not self._already_logged(post.post_id)]
        parsed_by_id = self.analyzer.parse_tweets_batch(new_posts) if new_posts else {}

        for post in new_posts:
            signal = parsed_by_id.get(post.post_id)
            if not signal:
                continue

            symbol = None
            signal.trust_score = self.analyzer.get_trust_score(post.account)
            signal.effective_confidence = signal.confidence * (signal.trust_score / 100)
            self._cycle_signals.append(signal)
            if signal.coin and signal.coin != "UNKNOWN":
                symbol = f"{signal.coin}/USDT"

            price = 0.0
            if symbol:
                price, _, _ = get_prices(symbol)
                price = price or 0.0

            rec = self.analyzer.track_and_recommend(
                post.text,
                post.account,
                current_price=price,
                orchestrator=self.orchestrator,
                signal=signal,
            )
            rec["post_id"] = post.post_id
            rec["raw_tweet"] = post.text[:200]
            rec["parsed_action"] = signal.action
            rec["signal_price"] = price
            rec["trust_at_signal"] = self.analyzer.get_trust_score(post.account)

            self.analyzer.log_tracked_post(rec)
            recommendations.append(rec)

            self._enqueue_strategy_discovery(post, symbol)

            if rec.get("recommended"):
                log(
                    f"X recommendation: @{post.account} {rec['action']} {rec['coin']} "
                    f"(trust={rec['trust_at_signal']})",
                    "INFO",
                )
                send_x_recommendation_message(rec)
                if rec.get("post_id"):
                    self._notified_post_ids.add(rec["post_id"])
                if rec["action"] == "ADD_TO_WATCHLIST" and rec.get("coin"):
                    add_coin(rec["coin"])

        return recommendations

    def process_cmc_posts(self, watchlist: list = None) -> list:
        from core.config import get_bot_config
        cfg = get_bot_config()
        if not cfg.cmc_config.get("enabled", True):
            return []

        watchlist = watchlist or load_effective_watchlist()
        raw_posts = self.cmc_provider.fetch_posts(watchlist)
        self._cycle_cmc_signals = []
        quotes_as_signal = bool(cfg.cmc_config.get("quotes_fallback_as_signal", False))
        logged_ids = {
            p.get("post_id")
            for p in load_cmc_posts().get("posts", [])
            if p.get("post_id")
        }

        for post in raw_posts:
            signal = self.cmc_parser.parse(post)
            signal.quotes_fallback = post.author == "CMC Market"
            if signal.quotes_fallback and not quotes_as_signal:
                continue
            signal.effective_confidence = signal.confidence * (signal.trust_score / 100)
            self._cycle_cmc_signals.append(signal)
            if post.post_id not in logged_ids:
                log_cmc_post(signal, post.post_id)
                logged_ids.add(post.post_id)
                log(
                    f"CMC signal: {signal.coin} {signal.action} ({signal.confidence}%) "
                    f"votes {signal.votes_bullish}↑/{signal.votes_bearish}↓",
                    "INFO",
                )
        return self._cycle_cmc_signals

    def refresh_signals(self) -> list:
        """Score signals from the current cycle for watchlist integration."""
        signals = self._cycle_signals or self.analyzer.fetch_latest_signals()
        for signal in signals:
            self.analyzer.score_signal(signal, 50.0, all_signals=signals)
        return sorted(signals, key=lambda s: s.score, reverse=True)

    def refresh_cmc_signals(self) -> list:
        from core.config import get_bot_config
        from hermes.cmc_replay import active_signals_for_symbols

        cfg = get_bot_config()
        cmc_cfg = cfg.cmc_config
        ttl_hours = float(
            cmc_cfg.get("signal_ttl_hours")
            or cfg.hermes_config.get("cmc_replay_ttl_hours", 4)
        )
        quotes_as_signal = bool(cmc_cfg.get("quotes_fallback_as_signal", False))
        watchlist = load_effective_watchlist()
        symbols = [c.get("symbol") for c in watchlist if c.get("symbol")]
        trust = float(cmc_cfg.get("trust_score", 65))
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        active = active_signals_for_symbols(symbols, trust_score=trust, ttl_hours=ttl_hours, now_ms=now_ms)
        if not quotes_as_signal:
            active = [s for s in active if not getattr(s, "quotes_fallback", False)]

        by_coin: dict[str, object] = {getattr(s, "coin", ""): s for s in active}
        for signal in self._cycle_cmc_signals:
            if getattr(signal, "quotes_fallback", False) and not quotes_as_signal:
                continue
            coin = getattr(signal, "coin", "")
            existing = by_coin.get(coin)
            if not existing or signal.confidence > getattr(existing, "confidence", 0):
                by_coin[coin] = signal

        merged = sorted(by_coin.values(), key=lambda s: getattr(s, "confidence", 0), reverse=True)
        return merged

    def process_lc_signals(self, watchlist: list = None) -> list:
        from core.config import get_bot_config

        cfg = get_bot_config()
        lc_cfg = cfg.lunarcrush_config
        if not lc_cfg.get("enabled", True):
            return []

        watchlist = watchlist or load_effective_watchlist()
        raw_metrics = self.lc_provider.fetch_for_watchlist(watchlist)
        self._cycle_lc_signals = []
        thresholds = dict(lc_cfg.get("thresholds", {}))
        thresholds["trust_score"] = float(lc_cfg.get("trust_score", 72))
        logged_ids = {
            s.get("signal_id")
            for s in load_lc_signals().get("signals", [])
            if s.get("signal_id")
        }

        for metrics in raw_metrics:
            signal = score_lc_metrics(metrics, thresholds=thresholds, trust_score=thresholds["trust_score"])
            if not signal:
                continue
            signal.effective_confidence = signal.confidence * (signal.trust_score / 100.0)
            self._cycle_lc_signals.append(signal)
            sid = signal.post_id
            if sid and sid not in logged_ids:
                log_lc_signal(signal, sid)
                logged_ids.add(sid)
                log(
                    f"LC signal: {signal.coin} {signal.action} ({signal.confidence}%) "
                    f"galaxy={signal.galaxy_score:.0f} alt_rank={signal.alt_rank}",
                    "INFO",
                )
        return self._cycle_lc_signals

    def refresh_lc_signals(self) -> list:
        from core.config import get_bot_config
        from datetime import datetime, timezone

        cfg = get_bot_config()
        lc_cfg = cfg.lunarcrush_config
        if not lc_cfg.get("enabled", True):
            return []

        ttl_hours = float(lc_cfg.get("signal_ttl_hours", 4))
        trust = float(lc_cfg.get("trust_score", 72))
        cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - int(ttl_hours * 3600 * 1000)

        by_coin: dict = {}
        for entry in load_lc_signals().get("signals", []):
            try:
                ts = datetime.fromisoformat(str(entry.get("timestamp", "")).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if int(ts.timestamp() * 1000) < cutoff_ms:
                    continue
            except (ValueError, TypeError):
                continue
            coin = str(entry.get("coin", "")).upper()
            if not coin:
                continue
            from data.lunarcrush_scorer import LunarCrushSignal

            sig = LunarCrushSignal(
                coin=coin,
                action=str(entry.get("action", "HOLD")),
                confidence=int(entry.get("confidence", 0) or 0),
                rationale=str(entry.get("rationale", "")),
                post_id=entry.get("signal_id"),
                galaxy_score=float(entry.get("galaxy_score", 0) or 0),
                alt_rank=int(entry.get("alt_rank", 0) or 0),
                sentiment=float(entry.get("sentiment", 0) or 0),
            )
            sig.trust_score = trust
            sig.effective_confidence = sig.confidence * (trust / 100.0)
            existing = by_coin.get(coin)
            if not existing or sig.confidence > getattr(existing, "confidence", 0):
                by_coin[coin] = sig

        for signal in self._cycle_lc_signals:
            coin = getattr(signal, "coin", "")
            existing = by_coin.get(coin)
            if not existing or signal.confidence > getattr(existing, "confidence", 0):
                by_coin[coin] = signal

        return sorted(by_coin.values(), key=lambda s: getattr(s, "confidence", 0), reverse=True)

    def should_send_lc_digest(self, signals: list) -> bool:
        if not signals:
            return False
        sig = "|".join(
            f"{getattr(s, 'coin', '')}:{getattr(s, 'action', '')}:{getattr(s, 'confidence', 0)}"
            for s in sorted(signals, key=lambda x: getattr(x, "coin", ""))
        )
        if sig == self._last_lc_digest_sig:
            return False
        self._last_lc_digest_sig = sig
        return True

    def get_notified_post_ids(self) -> set:
        return set(self._notified_post_ids)

    def should_send_cmc_digest(self, signals: list) -> bool:
        if not signals:
            return False
        sig = "|".join(
            f"{getattr(s, 'coin', '')}:{getattr(s, 'action', '')}:{getattr(s, 'confidence', 0)}"
            for s in sorted(signals, key=lambda x: getattr(x, "coin", ""))
        )
        if sig == self._last_cmc_digest_sig:
            return False
        self._last_cmc_digest_sig = sig
        return True

    def update_accuracy_loop(self) -> dict:
        outcomes = self.tracker.update_outcomes()
        trust_updates = self.tracker.update_trust_scores()
        if outcomes or trust_updates:
            self.analyzer.accounts = self.analyzer._reload_accounts()
        return {"outcomes_updated": outcomes, "trust_updates": trust_updates}