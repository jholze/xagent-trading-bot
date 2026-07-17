"""Polymarket curated markets + mispricing score (MC-5/6)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class PmMarket:
    market_id: str
    title: str
    prob: float  # 0..1
    prev_prob: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def delta_pp(self) -> float:
        if self.prev_prob is None:
            return 0.0
        return (self.prob - self.prev_prob) * 100.0


def mispricing_score(
    pm_prob: float,
    *,
    prev_prob: float | None = None,
    btc_ret: float | None = None,
    delta_pp_threshold: float = 10.0,
    btc_ret_threshold: float = 0.01,
    fusion_regime: str | None = None,
) -> dict[str, Any]:
    """v1 mispricing: large Δprob without commensurate BTC move, or regime conflict.

    Returns {flag, score, reasons} — pure logic, no I/O.
    """
    reasons: list[str] = []
    score = 0.0
    delta_pp = 0.0
    if prev_prob is not None:
        delta_pp = (float(pm_prob) - float(prev_prob)) * 100.0
        if abs(delta_pp) >= float(delta_pp_threshold):
            score += min(0.5, abs(delta_pp) / 40.0)
            reasons.append(f"pm_delta_pp={delta_pp:.1f}")
            if btc_ret is not None and abs(float(btc_ret)) < float(btc_ret_threshold):
                score += 0.3
                reasons.append(f"btc_quiet={btc_ret:.4f}")

    # extreme prob
    if pm_prob >= 0.85 or pm_prob <= 0.15:
        score += 0.15
        reasons.append(f"pm_extreme={pm_prob:.2f}")

    regime = (fusion_regime or "").upper()
    if regime in ("RISK_OFF", "CRASH") and pm_prob >= 0.7:
        score += 0.2
        reasons.append(f"regime_conflict={regime}")
    if regime == "RISK_ON" and pm_prob <= 0.3:
        score += 0.15
        reasons.append(f"regime_conflict={regime}")

    score = max(0.0, min(1.0, score))
    return {
        "flag": score >= 0.35,
        "score": round(score, 3),
        "delta_pp": round(delta_pp, 2),
        "reasons": reasons,
    }


def load_fixture_markets(path: str | None = None) -> list[PmMarket]:
    import json
    from pathlib import Path

    if path:
        p = Path(path)
    else:
        p = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "macro" / "polymarket_markets.json"
        if not p.is_file():
            p = Path("tests/fixtures/macro/polymarket_markets.json")
    if not p.is_file():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    out = []
    for row in data.get("markets") or []:
        out.append(
            PmMarket(
                market_id=str(row.get("market_id") or row.get("id") or ""),
                title=str(row.get("title") or ""),
                prob=float(row.get("prob") if row.get("prob") is not None else 0.5),
                prev_prob=(
                    float(row["prev_prob"]) if row.get("prev_prob") is not None else None
                ),
                metadata=dict(row.get("metadata") or {}),
            )
        )
    return [m for m in out if m.market_id]


def fetch_polymarket_live(market_ids: list[str]) -> list[PmMarket]:
    """Best-effort HTTP fetch — fail-open empty. Timeout short for Hermes cycle."""
    if not market_ids:
        return []
    try:
        import json
        from urllib.request import Request, urlopen

        # Gamma API sample endpoint pattern; fail soft if schema changes
        out: list[PmMarket] = []
        for mid in market_ids[:10]:
            url = f"https://gamma-api.polymarket.com/markets/{mid}"
            req = Request(url, headers={"User-Agent": "xagent-macro/1.0"})
            with urlopen(req, timeout=4) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            # try common fields
            prob = raw.get("lastTradePrice") or raw.get("outcomePrices")
            if isinstance(prob, str) and "," in prob:
                try:
                    prob = float(json.loads(prob)[0] if prob.startswith("[") else prob.split(",")[0])
                except Exception:
                    prob = 0.5
            try:
                p = float(prob)
            except Exception:
                p = 0.5
            if p > 1.0:
                p = p / 100.0
            out.append(
                PmMarket(
                    market_id=str(mid),
                    title=str(raw.get("question") or raw.get("title") or mid)[:200],
                    prob=max(0.0, min(1.0, p)),
                    metadata={"source": "gamma"},
                )
            )
        return out
    except Exception:
        return []
