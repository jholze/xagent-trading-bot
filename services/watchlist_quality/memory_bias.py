"""Memory → WQE adapter (Epic #124 · W1 / #125).

Pure read path via ``intelligence.memory.cache``. Fail-open. No ledger writes,
no Weaviate, no effective-watchlist side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class MemoryWqeInput:
    """WQE-facing memory bias for one symbol."""

    symbol: str
    entry_bias: str  # neutral | soft_block | prefer
    size_bias: float  # 0.5..1.2
    memory_score: float  # 0..1 component for quality_score
    hard_exclude_new_add: bool  # True → do not add as new T1/T2 from trending
    ttl_active: bool  # soft_block_until still in the future (if set)
    scope: str  # sensor_only | all_new | ""
    rationale: str
    source: str  # profile | default | disabled | error


_DEFAULT_NEUTRAL_SCORE = 0.5


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def _parse_iso_dt(raw: Any) -> datetime | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        u = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(u)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _watchlist_quality_cfg(config: dict | None) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    wq = config.get("watchlist_quality")
    return wq if isinstance(wq, dict) else {}


def _memory_cfg(config: dict | None) -> dict[str, Any]:
    wq = _watchlist_quality_cfg(config)
    mem = wq.get("memory")
    return mem if isinstance(mem, dict) else {}


def memory_wqe_enabled(config: dict | None = None) -> bool:
    """False when watchlist_quality.memory.enabled is false (kill-switch)."""
    mem = _memory_cfg(config)
    if mem.get("enabled") is False:
        return False
    return True


def _neutral(
    symbol: str,
    *,
    source: str,
    rationale: str = "",
    size_bias: float = 1.0,
) -> MemoryWqeInput:
    return MemoryWqeInput(
        symbol=symbol,
        entry_bias="neutral",
        size_bias=_clamp(size_bias, 0.5, 1.2),
        memory_score=_DEFAULT_NEUTRAL_SCORE,
        hard_exclude_new_add=False,
        ttl_active=False,
        scope="",
        rationale=rationale or source,
        source=source,
    )


def _apply_size_bias_to_score(score: float, size_bias: float, *, apply: bool) -> float:
    if not apply:
        return _clamp(score, 0.0, 1.0)
    sb = _clamp(size_bias, 0.5, 1.2)
    # Center size_bias at 1.0: 0.5 → 0.5x, 1.0 → 1x, 1.2 → 1.2x then clamp
    return _clamp(score * sb, 0.0, 1.0)


def get_memory_wqe_input(
    symbol: str,
    *,
    config: dict | None = None,
    ledger_scope: str | None = None,
    tenant_id: str = "default",
    now: datetime | None = None,
    profile: Any | None = None,
) -> MemoryWqeInput:
    """Map Trading Memory coin profile → WQE memory component (fail-open).

    Parameters
    ----------
    profile
        Optional pre-loaded CoinProfile (tests / batch). When None, loads via cache.
    now
        Injectable clock for TTL tests (UTC).
    """
    sym = (symbol or "").strip()
    if not sym:
        return _neutral("", source="default", rationale="empty_symbol")

    if not memory_wqe_enabled(config):
        return _neutral(sym, source="disabled", rationale="watchlist_quality.memory.enabled=false")

    mem_cfg = _memory_cfg(config)
    prefer_boost = float(mem_cfg.get("prefer_boost", 0.15) or 0.15)
    soft_penalty = float(mem_cfg.get("soft_penalty", 0.40) or 0.40)
    soft_penalty_sensor = float(mem_cfg.get("soft_penalty_sensor_only", 0.15) or 0.15)
    exclude_new = bool(mem_cfg.get("exclude_new_adds_on_soft_block", True))
    apply_size = bool(mem_cfg.get("apply_size_bias_to_score", True))

    wq = _watchlist_quality_cfg(config)
    # honor at WQE layer: if false, never hard_exclude (score still applies)
    honor = wq.get("honor_memory_soft_block")
    if honor is None:
        honor = True
    honor = bool(honor)

    try:
        from intelligence.memory.store import memory_enabled

        if not memory_enabled(config):
            return _neutral(sym, source="disabled", rationale="MEMORY_ENABLED off")
    except Exception:
        # If store import fails, still try profile path below
        pass

    try:
        if profile is None:
            from intelligence.memory.cache import get_coin_profile

            profile = get_coin_profile(
                sym,
                ledger_scope=ledger_scope,
                tenant_id=tenant_id,
                config=config,
            )
    except Exception as e:
        return _neutral(sym, source="error", rationale=f"profile_load:{type(e).__name__}")

    if profile is None:
        return _neutral(sym, source="default", rationale="no_profile")

    try:
        entry_bias = str(getattr(profile, "entry_bias", None) or "neutral").lower()
        if entry_bias not in ("neutral", "soft_block", "prefer"):
            entry_bias = "neutral"
        size_bias = _clamp(float(getattr(profile, "size_bias", 1.0) or 1.0), 0.5, 1.2)
        rationale = str(getattr(profile, "rationale", "") or "")
        feats = getattr(profile, "features", None) or {}
        if not isinstance(feats, dict):
            feats = {}
        scope = str(feats.get("soft_block_scope") or "").lower()
        until_raw = feats.get("soft_block_until")
        until_dt = _parse_iso_dt(until_raw)
        clock = now or datetime.now(timezone.utc)
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=timezone.utc)

        ttl_active = False
        soft_expired = False
        if until_dt is not None:
            if clock > until_dt:
                soft_expired = True
                ttl_active = False
            else:
                ttl_active = True

        # Expired soft_block → treat as neutral for WQE ranking
        if entry_bias == "soft_block" and soft_expired:
            score = _apply_size_bias_to_score(
                _DEFAULT_NEUTRAL_SCORE, size_bias, apply=apply_size
            )
            return MemoryWqeInput(
                symbol=sym,
                entry_bias="neutral",
                size_bias=size_bias,
                memory_score=score,
                hard_exclude_new_add=False,
                ttl_active=False,
                scope=scope,
                rationale=rationale or "soft_block_ttl_expired",
                source="profile",
            )

        if entry_bias == "prefer":
            score = _DEFAULT_NEUTRAL_SCORE + prefer_boost
            score = _apply_size_bias_to_score(score, size_bias, apply=apply_size)
            return MemoryWqeInput(
                symbol=sym,
                entry_bias="prefer",
                size_bias=size_bias,
                memory_score=_clamp(score, 0.0, 1.0),
                hard_exclude_new_add=False,
                ttl_active=ttl_active,
                scope=scope,
                rationale=rationale or "prefer",
                source="profile",
            )

        if entry_bias == "soft_block":
            # sensor_only: milder penalty; exclude only when exclude_new and not sensor_only
            # Plan: sensor_only → hard_exclude false for base keep; optional true for new adds
            #       all_new / legacy empty → hard_exclude true when honor+exclude_new
            if scope == "sensor_only":
                penalty = soft_penalty_sensor
                # Plan table: hard_exclude false for base; optional for new adds.
                # We set hard_exclude_new_add True only when exclude_new AND honor
                # but document that consumers should only apply it to *new* trending adds.
                # For sensor_only, plan says false for base-keep and optional for new adds —
                # default True for new-add flag when exclude_new so WQE can demote trending adds
                # while POS/base keep path ignores hard_exclude. Wait re-read plan:
                # "false für Base-Keep; true nur für *neue* CMC/Trending-Adds optional"
                # So hard_exclude_new_add can be True meaning "exclude if this is a new add".
                # For sensor_only it said false for base and true optional for new adds.
                # I'll set hard_exclude_new_add = honor and exclude_new for all soft_block
                # including sensor_only when exclude_new — consumers check is_new_add.
                # Actually table says sensor_only hard_exclude false; all_new true.
                hard_ex = False
            else:
                # all_new, empty legacy, or other
                penalty = soft_penalty
                hard_ex = bool(honor and exclude_new)

            score = _DEFAULT_NEUTRAL_SCORE - penalty
            score = _apply_size_bias_to_score(score, size_bias, apply=apply_size)
            return MemoryWqeInput(
                symbol=sym,
                entry_bias="soft_block",
                size_bias=size_bias,
                memory_score=_clamp(score, 0.0, 1.0),
                hard_exclude_new_add=hard_ex,
                ttl_active=ttl_active if until_dt is not None else True,
                scope=scope,
                rationale=rationale or "soft_block",
                source="profile",
            )

        # neutral or unknown
        score = _apply_size_bias_to_score(
            _DEFAULT_NEUTRAL_SCORE, size_bias, apply=apply_size
        )
        return MemoryWqeInput(
            symbol=sym,
            entry_bias="neutral",
            size_bias=size_bias,
            memory_score=score,
            hard_exclude_new_add=False,
            ttl_active=ttl_active,
            scope=scope,
            rationale=rationale or "neutral",
            source="profile",
        )
    except Exception as e:
        return _neutral(sym, source="error", rationale=f"map:{type(e).__name__}")
