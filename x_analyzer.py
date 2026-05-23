import json
from datetime import datetime
from typing import List, Dict

from data_manager import get_text, load_config, load_x_accounts, save_x_accounts
from grok_agent import ask_grok
import json


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
        self.config = load_config()
        self.accounts = load_x_accounts()
        self.min_confidence = self.config.get("min_x_confidence", 65)

    def parse_tweet(self, tweet_text: str, account: str) -> XSignal:
        """Use Grok to parse a tweet into a structured trading signal."""
        prompt = f"""You are a crypto trading analyst. Extract trading signal from this tweet.
Return ONLY valid JSON with these fields:
{{"coin": "SYMBOL", "action": "BUY|SELL|HOLD", "confidence": 0-100, "price_target": number or null, "stop_loss": number or null, "rationale": "short summary"}}

Tweet by @{account}: "{tweet_text}"

JSON:"""

        response = ask_grok(prompt)
        try:
            data = json.loads(response.strip("```json").strip("```").strip())
            return XSignal(
                account=account,
                coin=data.get("coin", "UNKNOWN"),
                action=data.get("action", "HOLD"),
                confidence=int(data.get("confidence", 60)),
                price_target=data.get("price_target"),
                stop_loss=data.get("stop_loss"),
                rationale=data.get("rationale", "")
            )
        except:
            return XSignal(account=account, coin="UNKNOWN", action="HOLD", confidence=30, rationale="Parse failed")

    def fetch_latest_signals(self, limit_per_account: int = 5) -> List[XSignal]:
        """Fetch tweets (mock for now) and parse them with LLM."""
        signals = []
        enabled_accounts = [a for a in self.accounts if a.get("enabled", True)]

        # Mock recent tweets - replace with real Twitter API in Phase 1.2
        mock_tweets = {
            "CryptoCapo_": "ARIA looking very strong here. Breaking resistance with volume. I am buying more.",
            "Pentosh1": "RAVE has been accumulating. Expecting big move soon. Long bias.",
            "SmartContracter": "HIGH is forming a nice bottom. Risk reward looks excellent for long."
        }

        for acc in enabled_accounts[:limit_per_account]:
            handle = acc.get("handle", str(acc))
            tweet = mock_tweets.get(handle, "General bullish sentiment on major alts.")
            signal = self.parse_tweet(tweet, handle)
            if signal.confidence >= self.min_confidence:
                signals.append(signal)

        return signals

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


if __name__ == "__main__":
    analyzer = XAnalyzer()
    signals = analyzer.get_top_signals()
    for s in signals:
        print(f"{s.account}: {s.action} {s.coin} | Confidence: {s.confidence} | Score: {s.score:.2f} | {s.rationale}")
