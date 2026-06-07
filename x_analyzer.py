import json
from datetime import datetime
from typing import List, Dict

from data_manager import get_config, get_text, load_x_accounts, load_x_posts, load_watchlist, save_x_accounts, save_x_posts
from grok_agent import ask_grok
import json
from datetime import datetime


class XSignal:
    def __init__(self, account: str, coin: str, action: str, confidence: int, price_target: float = None, stop_loss: float = None, rationale: str = ""):
        self.account = account
        self.coin = coin.upper()
        self.action = action.upper()  # BUY, SELL, HOLD
        self.confidence = confidence  # 0-100
        self.price_target = price_target
        self.stop_loss = stop_loss
        self.rationale = rationale
        self.timestamp = datetime.now()
        self.score = 0.0  # Will be calculated by analyzer


class XAnalyzer:
    def __init__(self):
        self.config = get_config()
        self.accounts = load_x_accounts()
        self.min_confidence = self.config.get("min_x_confidence", 65)

    def parse_tweet(self, tweet_text: str, account: str) -> XSignal:
        """Use Grok to parse a tweet into a structured trading signal with better prompts for useful output."""
        prompt = f"""You are a professional crypto trader. Analyze this tweet and give a clear, decisive trading recommendation.
Be confident and specific. Do not default to HOLD unless the tweet is neutral.

Return ONLY valid JSON with these fields:
{{"coin": "SYMBOL", "action": "BUY|SELL|HOLD", "confidence": 0-100, "price_target": number or null, "stop_loss": number or null, "rationale": "short 1-sentence summary why"}}

Tweet by @{account}: "{tweet_text}"

JSON:"""

        response = ask_grok(prompt)
        try:
            data = json.loads(response.strip("```json").strip("```").strip())
            return XSignal(
                account=account,
                coin=data.get("coin", "UNKNOWN"),
                action=data.get("action", "HOLD"),
                confidence=int(data.get("confidence", 70)),
                price_target=data.get("price_target"),
                stop_loss=data.get("stop_loss"),
                rationale=data.get("rationale", "Positive momentum detected")
            )
        except Exception as e:
            return XSignal(account=account, coin="UNKNOWN", action="HOLD", confidence=40, rationale=f"Parse error: {str(e)[:50]}")

    def fetch_latest_signals(self, limit_per_account: int = 5) -> List[XSignal]:
        """Fetch tweets (currently mock) and parse them with LLM."""
        signals = []
        enabled_accounts = [a for a in self.accounts if a.get("enabled", True)]

        # Controlled mock data usage (easy to turn off later when real API is integrated)
        if self.config.get("use_mock_x_data", True):
            mock_tweets = self._get_mock_tweets()
        else:
            mock_tweets = {}

        for acc in enabled_accounts[:limit_per_account]:
            handle = acc.get("handle", str(acc))
            tweet = mock_tweets.get(handle, "General bullish sentiment on major alts.")
            signal = self.parse_tweet(tweet, handle)
            if signal.confidence >= self.min_confidence:
                signals.append(signal)

        return signals

    def _get_mock_tweets(self) -> dict:
        """Central place for mock X data — easy to spot and replace later."""
        return {
            "CryptoCapo_": "BTC breaking key resistance with strong volume. Macro looks very bullish. Buying more now.",
            "Pentosh1": "SOL is overextended on the daily. Taking profits here. Short term bearish.",
            "SmartContracter": "ETH forming a nice higher low. Good risk/reward for long position.",
            "TheCryptoDog": "DOGE community is strong but price is consolidating. Watching for breakout, no position yet.",
            "CryptoWizardd": "BNB breaking out of long consolidation. Volume picking up. Bullish bias.",
            "CryptoCapo_": "Highstreet (HIGH) looks weak. Resistance not breaking. Prefer to stay away or short."
        }

    def score_signal(self, signal: XSignal, technical_score: float = 50.0) -> float:
        """Hybrid scoring: X confidence + technical weight."""
        x_score = signal.confidence * (self.config.get("x_weight", 0.45))
        tech_score = technical_score * (self.config.get("technical_weight", 0.35))
        signal.score = (x_score + tech_score) / 100
        return signal.score

    def get_top_signals(self, technical_scores: Dict[str, float] = None) -> List[XSignal]:
        signals = self.fetch_latest_signals()
        for signal in signals:
            tech = technical_scores.get(signal.coin, 50.0) if technical_scores else 50.0
            self.score_signal(signal, tech)
        return sorted([s for s in signals if s.confidence >= self.min_confidence], 
                     key=lambda s: s.score, reverse=True)

    def log_tracked_post(self, recommendation: Dict):
        """Log the tracked post and recommendation to x_posts.json."""
        data = load_x_posts()
        data["posts"].append({
            "timestamp": datetime.now().isoformat(),
            "account": recommendation["account"],
            "coin": recommendation["coin"],
            "action": recommendation["action"],
            "confidence": recommendation["confidence"],
            "rationale": recommendation["rationale"],
            "recommended": recommendation["recommended"]
        })
        save_x_posts(data)

    def track_and_recommend(self, tweet_text: str, account: str, current_price: float = 0.0) -> Dict:
        """Track a post, parse it, compare to current technical strategy, and recommend action."""
        from strategies.core_strategy import check_signal
        signal = self.parse_tweet(tweet_text, account)
        recommendation = {
            "account": account,
            "action": "IGNORE",
            "confidence": signal.confidence,
            "rationale": signal.rationale,
            "coin": signal.coin,
            "recommended": False,
            "raw_tweet": tweet_text[:100] + "..." if len(tweet_text) > 100 else tweet_text
        }

        if signal.coin == "UNKNOWN" or signal.confidence < self.min_confidence:
            return recommendation

        # Compare to current technical strategy
        coin_data = {"symbol": signal.coin + "/USDT"}
        technical_signal = check_signal(coin_data, current_price, x_signals=[signal])

        sell_signals = ("SELL", "SELL_20", "SELL_30", "SELL_STOP_FULL", "SELL_STOP_PARTIAL")
        if signal.action == "BUY" and technical_signal == "BUY":
            recommendation["action"] = "BUY"
            recommendation["recommended"] = True
        elif signal.action == "SELL" and technical_signal in sell_signals:
            recommendation["action"] = "SELL"
            recommendation["recommended"] = True
        elif signal.coin not in [c["symbol"].split("/")[0] for c in load_watchlist()]:
            recommendation["action"] = "ADD_TO_WATCHLIST"
            recommendation["recommended"] = True

        return recommendation


if __name__ == "__main__":
    analyzer = XAnalyzer()
    signals = analyzer.get_top_signals()
    for s in signals:
        print(f"{s.account}: {s.action} {s.coin} | Confidence: {s.confidence} | Score: {s.score:.2f} | {s.rationale}")
