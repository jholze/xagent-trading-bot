"""Replay historical CMC community signals for pipeline backtests."""

from __future__ import annotations

from datetime import datetime, timezone

from data.cmc_community_provider import CMCCommunitySignal
from data_manager import load_cmc_posts


def _parse_ts(value: str) -> int:
    if not value:
        return 0
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


def coin_base(symbol: str) -> str:
    return symbol.split("/")[0].upper()


def load_posts_for_coin(symbol: str, since_ms: int = 0, until_ms: int | None = None) -> list[dict]:
    base = coin_base(symbol)
    until_ms = until_ms or 9_999_999_999_999
    posts = []
    for post in load_cmc_posts().get("posts", []):
        if str(post.get("coin", "")).upper() != base:
            continue
        ts = _parse_ts(post.get("timestamp", ""))
        if ts < since_ms or ts > until_ms:
            continue
        posts.append({**post, "_ts_ms": ts})
    posts.sort(key=lambda p: p["_ts_ms"])
    return posts


def make_cmc_signal(post: dict, trust_score: float = 65.0) -> CMCCommunitySignal:
    confidence = int(post.get("confidence", 50) or 50)
    signal = CMCCommunitySignal(
        coin=str(post.get("coin", "")).upper(),
        action=str(post.get("action", "HOLD")).upper(),
        confidence=confidence,
        rationale=str(post.get("rationale", ""))[:120],
        post_id=post.get("post_id"),
        votes_bullish=int(post.get("votes_bullish", 0) or 0),
        votes_bearish=int(post.get("votes_bearish", 0) or 0),
    )
    signal.trust_score = float(trust_score)
    signal.effective_confidence = confidence * (trust_score / 100.0)
    signal.quotes_fallback = str(post.get("post_id", "")).startswith("cmc_quote_")
    return signal


def signals_at_timestamp(
    posts: list[dict],
    bar_ts_ms: int,
    trust_score: float = 65.0,
    ttl_ms: int = 4 * 3600 * 1000,
) -> list[CMCCommunitySignal]:
    """Return CMC signals active at bar timestamp (within TTL window)."""
    active = []
    for post in posts:
        p_ts = post.get("_ts_ms", 0)
        if p_ts <= bar_ts_ms <= p_ts + ttl_ms:
            active.append(make_cmc_signal(post, trust_score))
    if not active:
        return []
    active.sort(key=lambda s: s.confidence, reverse=True)
    return active


def active_signals_for_symbols(
    symbols: list[str],
    trust_score: float = 65.0,
    ttl_hours: float = 4.0,
    now_ms: int | None = None,
) -> list[CMCCommunitySignal]:
    """Strongest active CMC signal per symbol within TTL window."""
    now_ms = now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
    ttl_ms = int(ttl_hours * 3600 * 1000)
    since_ms = now_ms - ttl_ms
    active: list[CMCCommunitySignal] = []
    for symbol in symbols:
        posts = load_posts_for_coin(symbol, since_ms=since_ms, until_ms=now_ms)
        signals = signals_at_timestamp(posts, now_ms, trust_score=trust_score, ttl_ms=ttl_ms)
        if signals:
            active.append(signals[0])
    return active


def recent_signal_activity(symbols: list[str], hours: int = 24) -> dict[str, int]:
    """Count CMC BUY posts per symbol in the last N hours — for rotation."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    since_ms = now_ms - hours * 3600 * 1000
    counts: dict[str, int] = {s: 0 for s in symbols}
    for post in load_cmc_posts().get("posts", []):
        ts = _parse_ts(post.get("timestamp", ""))
        if ts < since_ms:
            continue
        if str(post.get("action", "")).upper() != "BUY":
            continue
        for symbol in symbols:
            if coin_base(symbol) == str(post.get("coin", "")).upper():
                counts[symbol] = counts.get(symbol, 0) + 1
    return counts