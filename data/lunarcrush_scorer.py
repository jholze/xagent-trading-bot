from datetime import datetime, timezone

from data.lunarcrush_provider import RawLCMetrics


class LunarCrushSignal:
    """Normalized LC signal — compatible with CMCCommunitySignal / XSignal."""

    def __init__(
        self,
        coin: str,
        action: str,
        confidence: int,
        rationale: str = "",
        post_id: str = None,
        galaxy_score: float = 0.0,
        alt_rank: int = 0,
        sentiment: float = 0.0,
        alt_improve_pct: float = 0.0,
    ):
        self.account = "LunarCrush"
        self.coin = coin.upper()
        self.action = action.upper()
        self.confidence = int(confidence)
        self.rationale = rationale
        self.post_id = post_id
        self.source = "lc"
        self.timestamp = datetime.now()
        self.trust_score = 72.0
        self.effective_confidence = float(confidence)
        self.galaxy_score = galaxy_score
        self.alt_rank = alt_rank
        self.sentiment = sentiment
        self.alt_improve_pct = alt_improve_pct
        self.votes_bullish = 0
        self.votes_bearish = 0
        self.score = 0.0
        self.quotes_fallback = False


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _alt_improve_pct(metrics: RawLCMetrics) -> float:
    prev = max(metrics.alt_rank_previous, 1)
    if metrics.alt_rank <= 0:
        return 0.0
    return max(0.0, (prev - metrics.alt_rank) / prev * 100.0)


def _galaxy_delta(metrics: RawLCMetrics) -> float:
    return metrics.galaxy_score - metrics.galaxy_score_previous


def score_lc_metrics(metrics: RawLCMetrics, thresholds: dict = None, trust_score: float = 72.0) -> LunarCrushSignal | None:
    """Turn LC snapshot into BUY/SELL/HOLD signal."""
    th = thresholds or {}
    buy_galaxy_min = float(th.get("buy_galaxy_min", 58))
    buy_galaxy_delta_min = float(th.get("buy_galaxy_delta_min", 4))
    buy_sentiment_min = float(th.get("buy_sentiment_min", 68))
    buy_alt_improve_min_pct = float(th.get("buy_alt_improve_min_pct", 8))
    buy_raw_min = float(th.get("buy_raw_min", 55))
    sell_galaxy_max = float(th.get("sell_galaxy_max", 42))
    sell_sentiment_max = float(th.get("sell_sentiment_max", 40))
    sell_alt_worsen_min_pct = float(th.get("sell_alt_worsen_min_pct", 12))

    alt_imp = _alt_improve_pct(metrics)
    gal_delta = _galaxy_delta(metrics)
    alt_worsen = 0.0
    if metrics.alt_rank_previous > 0 and metrics.alt_rank > metrics.alt_rank_previous:
        alt_worsen = (metrics.alt_rank - metrics.alt_rank_previous) / metrics.alt_rank_previous * 100.0

    raw_confidence = (
        0.50 * _clamp(metrics.galaxy_score, 0, 100)
        + 0.35 * _clamp(alt_imp * 2.0, 0, 100)
        + 0.15 * _clamp(metrics.sentiment, 0, 100)
    )

    action = "HOLD"
    confidence = int(_clamp(raw_confidence, 40, 95))

    if (
        metrics.galaxy_score >= buy_galaxy_min
        and (gal_delta >= buy_galaxy_delta_min or alt_imp >= buy_alt_improve_min_pct)
        and metrics.sentiment >= buy_sentiment_min
        and raw_confidence >= buy_raw_min
    ):
        action = "BUY"
    elif (
        metrics.galaxy_score <= sell_galaxy_max
        and metrics.sentiment <= sell_sentiment_max
        and alt_worsen >= sell_alt_worsen_min_pct
    ):
        action = "SELL"
        confidence = int(_clamp(55 + alt_worsen * 0.5, 50, 90))
    else:
        action = "HOLD"
        confidence = max(40, confidence - 5)

    if action == "HOLD" and confidence < 50:
        return None

    bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    post_id = f"lc_{metrics.symbol}_{bucket}"
    rationale = (
        f"Galaxy {metrics.galaxy_score:.0f} ({gal_delta:+.0f}), "
        f"AltRank {metrics.alt_rank} ({alt_imp:.0f}%↑), "
        f"Sentiment {metrics.sentiment:.0f}%"
    )

    signal = LunarCrushSignal(
        coin=metrics.symbol,
        action=action,
        confidence=confidence,
        rationale=rationale,
        post_id=post_id,
        galaxy_score=metrics.galaxy_score,
        alt_rank=metrics.alt_rank,
        sentiment=metrics.sentiment,
        alt_improve_pct=alt_imp,
    )
    trust = float(trust_score)
    signal.trust_score = trust
    signal.effective_confidence = confidence * (trust / 100.0)
    return signal