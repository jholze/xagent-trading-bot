import json
import re
from datetime import datetime
from typing import Dict, List, Optional, Union

from data_manager import get_config, load_effective_watchlist, load_x_accounts, load_x_posts, save_x_posts
from grok_agent import ask_grok, ask_grok_json
from x_data_provider import RawPost, get_x_provider


class XSignal:
    def __init__(
        self,
        account: str,
        coin: str,
        action: str,
        confidence: int,
        price_target: float = None,
        stop_loss: float = None,
        rationale: str = "",
        post_id: str = None,
    ):
        self.account = account
        self.coin = coin.upper()
        self.action = action.upper()
        self.confidence = confidence
        self.price_target = price_target
        self.stop_loss = stop_loss
        self.rationale = rationale
        self.post_id = post_id
        self.timestamp = datetime.now()
        self.score = 0.0
        self.trust_score = 70.0
        self.effective_confidence = float(confidence)


def _clean_grok_json(response: str) -> str:
    cleaned = response.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _extract_json_array(text: str) -> list:
    cleaned = _clean_grok_json(text)
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        data = json.loads(cleaned[start : end + 1])
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


class XAnalyzer:
    def __init__(self):
        self.config = get_config()
        self.accounts = load_x_accounts()
        self.min_confidence = self.config.get("min_x_confidence", 65)
        self.provider = get_x_provider(self.config)
        self._perf = self.config.get("x_performance", {})
        self._parse_batch_size = int(self._perf.get("parse_batch_size", 10))
        self._parse_cache: Dict[str, XSignal] = {}
        self._hydrate_parse_cache()

    def _reload_accounts(self):
        self.accounts = load_x_accounts()
        return self.accounts

    def _hydrate_parse_cache(self):
        for entry in load_x_posts().get("posts", []):
            post_id = entry.get("post_id")
            if not post_id or post_id in self._parse_cache:
                continue
            action = entry.get("parsed_action") or entry.get("action")
            coin = entry.get("coin")
            if not action or not coin:
                continue
            self._parse_cache[post_id] = XSignal(
                account=entry.get("account", ""),
                coin=coin,
                action=action,
                confidence=int(entry.get("confidence", 70)),
                price_target=entry.get("price_target"),
                stop_loss=entry.get("stop_loss"),
                rationale=entry.get("rationale", ""),
                post_id=post_id,
            )

    def _cache_signal(self, post_id: str, signal: XSignal):
        if post_id:
            self._parse_cache[post_id] = signal

    def _cached_signal(self, post: RawPost) -> Optional[XSignal]:
        if not post.post_id:
            return None
        cached = self._parse_cache.get(post.post_id)
        if not cached:
            return None
        return XSignal(
            account=post.account,
            coin=cached.coin,
            action=cached.action,
            confidence=cached.confidence,
            price_target=cached.price_target,
            stop_loss=cached.stop_loss,
            rationale=cached.rationale,
            post_id=post.post_id,
        )

    def get_trust_score(self, account: str) -> float:
        for acc in self.accounts:
            if acc.get("handle", acc) == account:
                return float(acc.get("trust_score", 70))
        return 70.0

    def effective_confidence_threshold(self, account: str) -> float:
        trust = self.get_trust_score(account)
        base = self.config.get("aggression", {}).get("base_confidence_threshold", 75)
        adjustment = (70 - trust) * 0.2
        return max(65.0, min(90.0, base + adjustment))

    def _signal_from_data(self, account: str, data: dict, post_id: str = None) -> XSignal:
        return XSignal(
            account=account,
            coin=data.get("coin", "UNKNOWN"),
            action=data.get("action", "HOLD"),
            confidence=int(data.get("confidence", 70)),
            price_target=data.get("price_target"),
            stop_loss=data.get("stop_loss"),
            rationale=data.get("rationale", "Positive momentum detected"),
            post_id=post_id,
        )

    def _levels_from_signal(self, signal: XSignal) -> Dict:
        return {
            "price_target": signal.price_target,
            "stop_loss": signal.stop_loss,
        }

    def _hold_signal(self, account: str, post_id: str = None, rationale: str = "") -> XSignal:
        return XSignal(
            account=account,
            coin="UNKNOWN",
            action="HOLD",
            confidence=40,
            rationale=rationale or "Parse error",
            post_id=post_id,
        )

    def parse_tweet(self, tweet_text: str, account: str, post_id: str = None) -> XSignal:
        if post_id and post_id in self._parse_cache:
            cached = self._parse_cache[post_id]
            return XSignal(
                account=account,
                coin=cached.coin,
                action=cached.action,
                confidence=cached.confidence,
                price_target=cached.price_target,
                stop_loss=cached.stop_loss,
                rationale=cached.rationale,
                post_id=post_id,
            )

        prompt = f"""You are a professional crypto trader. Analyze this tweet and give a clear, decisive trading recommendation.
Be confident and specific. Do not default to HOLD unless the tweet is neutral.

Return ONLY valid JSON with these fields:
{{"coin": "SYMBOL", "action": "BUY|SELL|HOLD", "confidence": 0-100, "price_target": number or null, "stop_loss": number or null, "rationale": "short 1-sentence summary why"}}

Tweet by @{account}: "{tweet_text}"

JSON:"""

        response = ask_grok_json(prompt)
        try:
            data = json.loads(_clean_grok_json(response))
            signal = self._signal_from_data(account, data, post_id)
            self._cache_signal(post_id, signal)
            return signal
        except Exception as e:
            signal = self._hold_signal(account, post_id, f"Parse error: {str(e)[:50]}")
            self._cache_signal(post_id, signal)
            return signal

    def _batch_prompt(self, posts: List[RawPost]) -> str:
        lines = [
            "You are a professional crypto trader. Analyze each tweet below and return trading recommendations.",
            "Be confident and specific. Do not default to HOLD unless the tweet is neutral.",
            "",
            "Return ONLY a valid JSON array. Each item must include post_id and:",
            '{"post_id": "...", "coin": "SYMBOL", "action": "BUY|SELL|HOLD", "confidence": 0-100, "price_target": number or null, "stop_loss": number or null, "rationale": "short summary"}',
            "",
            "Tweets:",
        ]
        for i, post in enumerate(posts, 1):
            pid = post.post_id or f"idx_{i}"
            lines.append(f'{i}. post_id="{pid}" account=@{post.account}: "{post.text}"')
        lines.append("")
        lines.append("JSON array:")
        return "\n".join(lines)

    def _parse_batch_response(self, response: str, posts: List[RawPost]) -> Dict[str, XSignal]:
        by_id: Dict[str, XSignal] = {}
        for item in _extract_json_array(response):
            if not isinstance(item, dict):
                continue
            post_id = str(item.get("post_id", "")).strip()
            account = item.get("account", "")
            if not post_id:
                continue
            if not account:
                for post in posts:
                    if post.post_id == post_id:
                        account = post.account
                        break
            signal = self._signal_from_data(account or "unknown", item, post_id)
            by_id[post_id] = signal
            self._cache_signal(post_id, signal)
        return by_id

    def _parse_posts_chunk(self, posts: List[RawPost]) -> Dict[str, XSignal]:
        if not posts:
            return {}
        if len(posts) == 1:
            post = posts[0]
            return {post.post_id: self.parse_tweet(post.text, post.account, post_id=post.post_id)}

        response = ask_grok_json(self._batch_prompt(posts))
        parsed = self._parse_batch_response(response, posts)
        if len(parsed) >= len(posts):
            return parsed

        results: Dict[str, XSignal] = {}
        for post in posts:
            pid = post.post_id
            if pid in parsed:
                results[pid] = parsed[pid]
            else:
                results[pid] = self.parse_tweet(post.text, post.account, post_id=pid)
        return results

    def parse_tweets_batch(self, posts: List[Union[RawPost, dict]]) -> Dict[str, XSignal]:
        normalized: List[RawPost] = []
        results: Dict[str, XSignal] = {}

        for item in posts:
            if isinstance(item, RawPost):
                post = item
            else:
                post = RawPost(
                    post_id=item.get("post_id", ""),
                    account=item.get("account", ""),
                    text=item.get("text", ""),
                    created_at=item.get("created_at", ""),
                )
            if not post.post_id:
                continue
            cached = self._cached_signal(post)
            if cached:
                results[post.post_id] = cached
            else:
                normalized.append(post)

        for i in range(0, len(normalized), self._parse_batch_size):
            chunk = normalized[i : i + self._parse_batch_size]
            results.update(self._parse_posts_chunk(chunk))

        return results

    def fetch_latest_signals(self, limit_per_account: int = 5) -> List[XSignal]:
        raw_posts = self.provider.fetch_new_posts(self.accounts, limit_per_account)
        parsed = self.parse_tweets_batch(raw_posts)
        signals = []

        for post in raw_posts:
            signal = parsed.get(post.post_id)
            if not signal:
                continue
            threshold = self.effective_confidence_threshold(post.account)
            signal.trust_score = self.get_trust_score(post.account)
            signal.effective_confidence = signal.confidence * (signal.trust_score / 100)
            if signal.effective_confidence >= min(self.min_confidence, threshold):
                signals.append(signal)

        return signals

    def _consensus_multiplier(self, signals: List[XSignal], coin: str) -> float:
        count = sum(1 for s in signals if s.coin == coin and s.action in ("BUY", "SELL"))
        if count >= 3:
            return 1.25
        if count >= 2:
            return 1.1
        return 1.0

    def score_signal(self, signal: XSignal, technical_score: float = 50.0, all_signals: List[XSignal] = None) -> float:
        trust = signal.trust_score or self.get_trust_score(signal.account)
        effective = signal.confidence * (trust / 100)
        consensus = self._consensus_multiplier(all_signals or [], signal.coin)
        effective *= consensus
        signal.effective_confidence = effective

        x_score = effective * self.config.get("x_weight", 0.45)
        tech_score = technical_score * self.config.get("technical_weight", 0.35)
        onchain_score = 0.0
        if getattr(signal, "source", "x") == "cmc":
            onchain_score = effective * self.config.get("onchain_weight", 0.2)
            x_score = 0.0
        signal.score = (x_score + tech_score + onchain_score) / 100
        return signal.score

    def get_top_signals(self, technical_scores: Dict[str, float] = None) -> List[XSignal]:
        signals = self.fetch_latest_signals()
        for signal in signals:
            tech = technical_scores.get(signal.coin, 50.0) if technical_scores else 50.0
            self.score_signal(signal, tech, all_signals=signals)
        return sorted(signals, key=lambda s: s.score, reverse=True)

    def log_tracked_post(self, recommendation: Dict):
        data = load_x_posts()
        entry = {
            "timestamp": datetime.now().isoformat(),
            "post_id": recommendation.get("post_id"),
            "account": recommendation["account"],
            "coin": recommendation["coin"],
            "action": recommendation["action"],
            "parsed_action": recommendation.get("parsed_action"),
            "confidence": recommendation["confidence"],
            "trust_at_signal": recommendation.get("trust_at_signal"),
            "signal_price": recommendation.get("signal_price"),
            "price_target": recommendation.get("price_target"),
            "stop_loss": recommendation.get("stop_loss"),
            "rationale": recommendation["rationale"],
            "recommended": recommendation["recommended"],
            "raw_tweet": recommendation.get("raw_tweet", ""),
        }
        if recommendation.get("post_id"):
            existing_ids = {p.get("post_id") for p in data.get("posts", [])}
            if recommendation["post_id"] in existing_ids:
                return
        data["posts"].append(entry)
        save_x_posts(data)
        post_id = recommendation.get("post_id")
        if post_id and recommendation.get("parsed_action"):
            self._cache_signal(
                post_id,
                XSignal(
                    account=recommendation["account"],
                    coin=recommendation.get("coin", "UNKNOWN"),
                    action=recommendation["parsed_action"],
                    confidence=int(recommendation.get("confidence", 70)),
                    price_target=recommendation.get("price_target"),
                    stop_loss=recommendation.get("stop_loss"),
                    rationale=recommendation.get("rationale", ""),
                    post_id=post_id,
                ),
            )

    def track_and_recommend(
        self,
        tweet_text: str,
        account: str,
        current_price: float = 0.0,
        orchestrator=None,
        signal: XSignal = None,
    ) -> Dict:
        if signal is None:
            signal = self.parse_tweet(tweet_text, account)
        trust = self.get_trust_score(account)
        signal.trust_score = trust
        signal.effective_confidence = signal.confidence * (trust / 100)
        recommendation = {
            "account": account,
            "action": "IGNORE",
            "confidence": signal.confidence,
            "rationale": signal.rationale,
            "coin": signal.coin,
            "recommended": False,
            "raw_tweet": tweet_text[:200],
            "trust_at_signal": trust,
            "parsed_action": signal.action,
            "signal_price": current_price,
            **self._levels_from_signal(signal),
        }

        if signal.coin == "UNKNOWN" or signal.confidence < self.min_confidence:
            return recommendation

        if signal.effective_confidence < self.effective_confidence_threshold(account):
            recommendation["rationale"] += f" (below trust-adjusted threshold for @{account})"
            return recommendation

        from strategies.decision_engine import DecisionEngine

        coin_data = {"symbol": signal.coin + "/USDT", "timeframe": "4h"}
        engine = getattr(orchestrator, "decision_engine", None) if orchestrator else None
        if engine is None:
            engine = DecisionEngine()

        if current_price:
            analysis = engine.evaluate(coin_data, current_price, x_signals=[signal])
            if analysis:
                return engine.to_recommendation(signal, analysis, account, tweet_text, current_price)

        if signal.coin not in [c["symbol"].split("/")[0] for c in load_effective_watchlist()]:
            recommendation["action"] = "ADD_TO_WATCHLIST"
            recommendation["recommended"] = True

        return recommendation


if __name__ == "__main__":
    analyzer = XAnalyzer()
    signals = analyzer.get_top_signals()
    for s in signals:
        print(
            f"{s.account}: {s.action} {s.coin} | Conf: {s.confidence} | "
            f"Effective: {s.effective_confidence:.0f} | Trust: {s.trust_score} | "
            f"Score: {s.score:.2f} | {s.rationale}"
        )