import json
import re
import uuid
from datetime import datetime

from core.config import get_bot_config
from core.models import StrategyHypothesis
from data_manager import load_paper_strategies, save_paper_strategies
from grok_agent import ask_grok
from logger import log


class StrategyDiscovery:
    """Extract reusable strategy hypotheses from X posts via Grok."""

    STRATEGY_KEYWORDS = (
        "rsi", "divergence", "volume", "breakout", "support", "resistance",
        "macd", "ema", "fibonacci", "momentum", "scalp", "swing", "timeframe",
    )

    def __init__(self, config=None):
        self.config = config or get_bot_config()

    def _is_strategy_tweet(self, text: str) -> bool:
        lower = text.lower()
        return any(kw in lower for kw in self.STRATEGY_KEYWORDS)

    def _heuristic_extract(self, tweet_text: str, account: str, post_id: str = None) -> StrategyHypothesis | None:
        if not self._is_strategy_tweet(tweet_text):
            return None

        lower = tweet_text.lower()
        timeframe = "1h" if "1h" in lower or "1 hour" in lower else "4h"
        symbol = ""
        coin_match = re.search(r"\b([A-Z]{2,10})/USDT\b", tweet_text)
        if coin_match:
            symbol = coin_match.group(0)
        else:
            for token in re.findall(r"\b[A-Z]{2,6}\b", tweet_text):
                if token not in ("RSI", "MACD", "EMA", "USDT", "BUY", "SELL"):
                    symbol = f"{token}/USDT"
                    break

        params = {
            "rsi_buy_low": 30 if "divergence" in lower else 28,
            "rsi_buy_high": 45 if timeframe == "1h" else 48,
            "volume_multiplier": 1.5 if "volume" in lower else 1.3,
            "rsi_sell_30": 68 if timeframe == "1h" else 70,
            "rsi_sell_20": 78 if timeframe == "1h" else 85,
        }
        name = f"{account} {timeframe} strategy"
        if "divergence" in lower:
            name = f"RSI divergence {timeframe}"

        return StrategyHypothesis(
            id=f"hyp_{uuid.uuid4().hex[:8]}",
            name=name,
            source_account=account,
            timeframe=timeframe,
            symbol=symbol,
            params=params,
            rationale="Heuristic extraction from strategy keywords",
            source_tweet=tweet_text[:300],
            source_post_id=post_id or "",
        )

    def _parse_grok_response(self, response: str, tweet_text: str, account: str, post_id: str = None) -> StrategyHypothesis | None:
        if not response or response.startswith("API-Fehler"):
            return None
        try:
            cleaned = response.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            data = json.loads(cleaned)
        except Exception:
            return self._heuristic_extract(tweet_text, account, post_id)

        params = data.get("params") or {}
        symbol = data.get("symbol", "")
        if symbol and "/" not in symbol:
            symbol = f"{symbol.upper()}/USDT"

        if not params and not data.get("conditions"):
            return self._heuristic_extract(tweet_text, account, post_id)

        return StrategyHypothesis(
            id=f"hyp_{uuid.uuid4().hex[:8]}",
            name=data.get("name", f"{account} strategy"),
            source_account=account,
            timeframe=data.get("timeframe", "4h"),
            symbol=symbol,
            params={
                "rsi_buy_low": params.get("rsi_buy_low", 28),
                "rsi_buy_high": params.get("rsi_buy_high", 48),
                "volume_multiplier": params.get("volume_multiplier", 1.3),
                "rsi_sell_30": params.get("rsi_sell_30", 70),
                "rsi_sell_20": params.get("rsi_sell_20", 85),
                "stop_loss_pct": params.get("stop_loss_pct", self.config.stop_loss_pct),
            },
            rationale=data.get("rationale", data.get("conditions", "")),
            source_tweet=tweet_text[:300],
            source_post_id=post_id or "",
        )

    def _guess_symbol_from_tweet(self, tweet_text: str) -> str:
        coin_match = re.search(r"\b([A-Z]{2,10})/USDT\b", tweet_text or "")
        if coin_match:
            return coin_match.group(0)
        for token in re.findall(r"\b[A-Z]{2,6}\b", tweet_text or ""):
            if token not in ("RSI", "MACD", "EMA", "USDT", "BUY", "SELL", "JSON"):
                return f"{token}/USDT"
        return ""

    def build_discovery_prompt(
        self,
        tweet_text: str,
        account: str,
        *,
        retriever=None,
        retrieved_section: str | None = None,
    ) -> str:
        """Build Grok prompt with optional RETRIEVED_MEMORY (epic #72 C4).

        Pure enough for unit tests: inject `retrieved_section` or a RagRetriever.
        Fail-open: retrieval errors omit the memory block.
        """
        memory_block = (retrieved_section or "").strip()
        if not memory_block:
            memory_block = self._retrieve_memory_for_tweet(
                tweet_text, account, retriever=retriever
            )

        base = f"""Analyze this crypto tweet for a reusable TRADING STRATEGY (rules/conditions), not just a single coin tip.
If no clear strategy concept exists, return {{"skip": true}}.

Return ONLY valid JSON:
{{
  "name": "short strategy name",
  "timeframe": "1h|4h|1d",
  "symbol": "COIN or COIN/USDT or empty",
  "conditions": "brief description",
  "rationale": "why this could work",
  "params": {{
    "rsi_buy_low": number,
    "rsi_buy_high": number,
    "volume_multiplier": number,
    "rsi_sell_30": number,
    "rsi_sell_20": number,
    "stop_loss_pct": number
  }}
}}

Tweet by @{account}: "{tweet_text}"
"""
        if memory_block:
            base += f"\n{memory_block}\nUse RETRIEVED_MEMORY only as soft evidence; do not invent trades.\n"
        base += "JSON:"
        return base

    def _retrieve_memory_for_tweet(
        self, tweet_text: str, account: str, *, retriever=None
    ) -> str:
        try:
            from intelligence.memory.rag_config import rag_enabled

            cfg_raw = getattr(self.config, "raw", None) or {}
            if not rag_enabled(cfg_raw):
                return ""
            from hermes.memory.rag_retriever import RagRetriever

            rag = retriever or RagRetriever(config=cfg_raw)
            symbol = self._guess_symbol_from_tweet(tweet_text)
            query = f"trading strategy {account} {tweet_text[:200]}"
            hits = rag.retrieve(
                query,
                top_k=5,
                filters={"symbol": symbol} if symbol else None,
            )
            if not hits and symbol:
                hits = rag.retrieve(query, top_k=5, filters=None)
            if not hits:
                return ""
            lines = ["RETRIEVED_MEMORY:"]
            for h in hits:
                lines.append(f"- ({h.score:.3f}) {(h.text or '')[:280]}")
            return "\n".join(lines)
        except Exception as e:
            log(f"strategy_discovery RAG retrieve skipped: {e}", "DEBUG")
            return ""

    def discover_from_tweet(self, tweet_text: str, account: str, post_id: str = None) -> StrategyHypothesis | None:
        if not self._is_strategy_tweet(tweet_text):
            return None

        if self.config.raw.get("use_mock_x_data", True):
            return self._heuristic_extract(tweet_text, account, post_id)

        prompt = self.build_discovery_prompt(tweet_text, account)
        response = ask_grok(prompt)
        try:
            cleaned = response.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            data = json.loads(cleaned)
            if data.get("skip"):
                return None
        except Exception:
            pass

        return self._parse_grok_response(response, tweet_text, account, post_id)

    def _duplicate(self, hypothesis: StrategyHypothesis) -> bool:
        data = load_paper_strategies()
        for existing in data.get("hypotheses", []):
            if existing.get("source_post_id") and existing.get("source_post_id") == hypothesis.source_post_id:
                return True
            if (
                existing.get("name") == hypothesis.name
                and existing.get("source_account") == hypothesis.source_account
                and existing.get("status") == "testing"
            ):
                return True
        return False

    def save_hypothesis(self, hypothesis: StrategyHypothesis) -> bool:
        if self._duplicate(hypothesis):
            return False
        data = load_paper_strategies()
        entry = {
            "id": hypothesis.id,
            "name": hypothesis.name,
            "source_account": hypothesis.source_account,
            "status": hypothesis.status,
            "timeframe": hypothesis.timeframe,
            "symbol": hypothesis.symbol,
            "params": hypothesis.params,
            "rationale": hypothesis.rationale,
            "source_tweet": hypothesis.source_tweet,
            "source_post_id": hypothesis.source_post_id,
            "created_at": hypothesis.created_at,
            "metrics": hypothesis.metrics or {},
        }
        data.setdefault("hypotheses", []).append(entry)
        saved = save_paper_strategies(data)
        if saved:
            log(f"Sandbox hypothesis saved: {hypothesis.id} ({hypothesis.name})", "INFO")
        return saved

    def list_hypotheses(self, status: str = None) -> list:
        data = load_paper_strategies()
        items = data.get("hypotheses", [])
        if status:
            items = [h for h in items if h.get("status") == status]
        return items

    def get_hypothesis(self, hypothesis_id: str) -> dict | None:
        for hyp in self.list_hypotheses():
            if hyp.get("id") == hypothesis_id:
                return hyp
        return None

    def update_hypothesis(self, hypothesis_id: str, updates: dict) -> bool:
        data = load_paper_strategies()
        for hyp in data.get("hypotheses", []):
            if hyp.get("id") == hypothesis_id:
                hyp.update(updates)
                return save_paper_strategies(data)
        return False