from data_manager import add_coin
from intelligence.accuracy_tracker import AccuracyTracker
from logger import log
from price_fetcher import get_prices
from telegram_notifier import send_x_recommendation_message
from x_data_provider import get_x_provider


class SocialPipeline:
    """Fetches X posts, tracks recommendations, and updates accuracy/trust."""

    def __init__(self, analyzer, orchestrator=None, notify_callback=None):
        self.analyzer = analyzer
        self.orchestrator = orchestrator
        self.notify_callback = notify_callback
        self.tracker = AccuracyTracker()
        self.provider = get_x_provider()
        self._cycle_signals = []

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

        for post in raw_posts:
            if self._already_logged(post.post_id):
                continue

            symbol = None
            signal = self.analyzer.parse_tweet(post.text, post.account, post_id=post.post_id)
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
            )
            rec["post_id"] = post.post_id
            rec["raw_tweet"] = post.text[:200]
            rec["parsed_action"] = signal.action
            rec["signal_price"] = price
            rec["trust_at_signal"] = self.analyzer.get_trust_score(post.account)

            self.analyzer.log_tracked_post(rec)
            recommendations.append(rec)

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

    def refresh_signals(self) -> list:
        """Score signals from the current cycle for watchlist integration."""
        signals = self._cycle_signals or self.analyzer.fetch_latest_signals()
        for signal in signals:
            self.analyzer.score_signal(signal, 50.0, all_signals=signals)
        return sorted(signals, key=lambda s: s.score, reverse=True)

    def update_accuracy_loop(self) -> dict:
        outcomes = self.tracker.update_outcomes()
        trust_updates = self.tracker.update_trust_scores()
        if outcomes or trust_updates:
            self.analyzer.accounts = self.analyzer._reload_accounts()
        return {"outcomes_updated": outcomes, "trust_updates": trust_updates}