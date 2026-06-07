import threading
from queue import Queue

from data_manager import add_coin, get_config, load_watchlist, log_cmc_post
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
        self._cycle_signals = []
        self._cycle_cmc_signals = []
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
                if rec["action"] == "ADD_TO_WATCHLIST" and rec.get("coin"):
                    add_coin(rec["coin"])

        return recommendations

    def process_cmc_posts(self, watchlist: list = None) -> list:
        from core.config import get_bot_config
        cfg = get_bot_config()
        if not cfg.cmc_config.get("enabled", True):
            return []

        watchlist = watchlist or load_watchlist()
        raw_posts = self.cmc_provider.fetch_posts(watchlist)
        self._cycle_cmc_signals = []

        for post in raw_posts:
            signal = self.cmc_parser.parse(post)
            signal.effective_confidence = signal.confidence * (signal.trust_score / 100)
            self._cycle_cmc_signals.append(signal)
            log_cmc_post(signal, post.post_id)
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
        return self._cycle_cmc_signals

    def update_accuracy_loop(self) -> dict:
        outcomes = self.tracker.update_outcomes()
        trust_updates = self.tracker.update_trust_scores()
        if outcomes or trust_updates:
            self.analyzer.accounts = self.analyzer._reload_accounts()
        return {"outcomes_updated": outcomes, "trust_updates": trust_updates}