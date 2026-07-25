"""AI2: LLM critic for WQE — structured JSON, clamp, fail-open."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

_VALID_STANCES = frozenset({"keep", "boost", "demote", "avoid_new"})


@dataclass
class AiCriticResult:
    stance: str = "keep"
    adjust: float = 0.0
    confidence: float = 0.0
    rationale: str = ""
    risk_tags: list[str] = field(default_factory=list)
    model: str = ""
    source: str = "ok"  # ok | skipped | error | disabled | no_evidence
    error: str = ""
    evidence_n: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def clamp_adjust(raw: Any, *, max_adjust: float = 0.2) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.0
    m = abs(float(max_adjust) if max_adjust else 0.2)
    return max(-m, min(m, v))


def clamp_confidence(raw: Any) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, v))


def normalize_stance(raw: Any) -> str:
    s = str(raw or "keep").strip().lower()
    if s not in _VALID_STANCES:
        return "keep"
    return s


def parse_critic_payload(
    data: dict[str, Any] | None,
    *,
    max_adjust: float = 0.2,
    evidence_n: int = 0,
    model: str = "",
) -> AiCriticResult:
    """Pure parse/clamp of critic JSON (unit-testable without LLM)."""
    if not isinstance(data, dict):
        return AiCriticResult(
            source="error",
            error="non_dict",
            evidence_n=evidence_n,
            model=model,
        )
    stance = normalize_stance(data.get("stance"))
    adjust = clamp_adjust(data.get("adjust", 0), max_adjust=max_adjust)
    conf = clamp_confidence(data.get("confidence", 0))
    rationale = str(data.get("rationale") or "")[:240]
    tags = data.get("risk_tags") or []
    if not isinstance(tags, list):
        tags = []
    tags = [str(t)[:48] for t in tags[:8]]
    return AiCriticResult(
        stance=stance,
        adjust=adjust,
        confidence=conf,
        rationale=rationale,
        risk_tags=tags,
        model=model,
        source="ok",
        evidence_n=evidence_n,
    )


def fuse_quality(
    quality_score: float,
    critic: AiCriticResult | None,
    *,
    max_adjust: float = 0.2,
) -> float:
    """quality_shadow_ai = clamp(quality + adjust * confidence, 0, 1)."""
    q = float(quality_score or 0.0)
    if critic is None or critic.source not in ("ok",):
        return max(0.0, min(1.0, q))
    adj = clamp_adjust(critic.adjust, max_adjust=max_adjust)
    conf = clamp_confidence(critic.confidence)
    return max(0.0, min(1.0, q + adj * conf))


def _build_prompt(
    *,
    symbol: str,
    wqe: dict[str, Any],
    memory: dict[str, Any],
    metrics: dict[str, Any],
    regime: dict[str, Any],
    evidence_block: str,
) -> str:
    payload = {
        "symbol": symbol,
        "wqe": wqe,
        "memory": memory,
        "metrics": metrics,
        "regime": regime,
        "evidence": evidence_block,
    }
    return (
        "You are a crypto watchlist quality critic for a Gate spot bot.\n"
        "Given metrics + memory evidence, return STRICT JSON only with keys:\n"
        '  stance: keep|boost|demote|avoid_new\n'
        "  adjust: number in [-0.2, 0.2] (score delta)\n"
        "  confidence: 0..1\n"
        "  rationale: max 240 chars\n"
        "  risk_tags: string array\n"
        "Rules: never invent venue numbers; if evidence empty use stance=keep adjust=0 low confidence;\n"
        "prefer demote/avoid_new when soft_block history + thin volume; never block sells.\n"
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False)[:4000]}\n"
        "JSON:"
    )


def run_ai_critic(
    *,
    symbol: str,
    quality_score: float,
    scores: dict[str, float] | None = None,
    tier_hint: str = "T2",
    flags: list[str] | None = None,
    memory: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    regime: dict[str, Any] | None = None,
    rag_pack: Any | None = None,
    config: dict | None = None,
    llm_json_fn: Callable[..., dict] | None = None,
) -> AiCriticResult:
    """Call LLM critic or skip. Fully fail-open."""
    try:
        from services.watchlist_quality.config import ai_config

        ai = ai_config(config)
    except Exception:
        ai = {"enabled": True, "max_adjust": 0.2}

    if ai.get("enabled") is False:
        return AiCriticResult(source="disabled")

    max_adjust = float(ai.get("max_adjust", 0.2) or 0.2)
    require_evidence = bool(ai.get("require_evidence", True))

    evidence_block = ""
    evidence_n = 0
    if rag_pack is not None:
        try:
            evidence_n = int(getattr(rag_pack, "items", None) and len(rag_pack.items) or 0)
            if hasattr(rag_pack, "evidence_block"):
                evidence_block = rag_pack.evidence_block(
                    max_chars=int(ai.get("max_evidence_chars", 1500) or 1500)
                )
            elif isinstance(rag_pack, dict):
                evidence_n = int(rag_pack.get("n") or len(rag_pack.get("items") or []))
                evidence_block = str(rag_pack.get("evidence_block") or "")
        except Exception:
            pass

    if require_evidence and evidence_n == 0 and not (evidence_block or "").strip():
        return AiCriticResult(
            stance="keep",
            adjust=0.0,
            confidence=0.1,
            rationale="no_evidence",
            source="no_evidence",
            evidence_n=0,
        )

    prompt = _build_prompt(
        symbol=symbol,
        wqe={
            "quality_score": quality_score,
            "scores": scores or {},
            "tier_hint": tier_hint,
            "flags": flags or [],
        },
        memory=memory or {},
        metrics=metrics or {},
        regime=regime or {},
        evidence_block=evidence_block or "(none)",
    )

    try:
        if llm_json_fn is None:
            from intelligence.llm_client import ask_grok_json

            llm_json_fn = ask_grok_json
        timeout = int(ai.get("timeout_sec", 12) or 12)
        data = llm_json_fn(
            prompt,
            timeout_sec=timeout,
            required_keys=["stance", "adjust", "confidence"],
        )
        model = str(ai.get("model") or "")
        return parse_critic_payload(
            data, max_adjust=max_adjust, evidence_n=evidence_n, model=model
        )
    except Exception as e:
        return AiCriticResult(
            source="error",
            error=f"{type(e).__name__}:{e}",
            evidence_n=evidence_n,
        )
