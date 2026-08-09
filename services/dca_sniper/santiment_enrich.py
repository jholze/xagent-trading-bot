"""Santiment integration for DCA sniper deep analysis.

Uses the existing Pro-grade stack fully:
  1) Global regime snapshot (sidecar → Redis/store) via get_santiment_policy
  2) Optional per-asset SanAPI metrics (social, DAA, exchange flows, MVRV)

Design:
  - Prefer cached global snapshot (always cheap).
  - Per-asset fetch is optional, rate-limited, fail-open, TTL-cached.
  - Never invent on-chain/social numbers.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from logger import log

# Common base → Santiment project slug (Pro covers 1000s; map the frequent bags)
_SLUG_MAP: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XRP": "xrp",
    "ADA": "cardano",
    "AVAX": "avalanche",
    "DOT": "polkadot",
    "LINK": "chainlink",
    "ATOM": "cosmos",
    "NEAR": "near-protocol",
    "AAVE": "aave",
    "UNI": "uniswap",
    "LTC": "litecoin",
    "BCH": "bitcoin-cash",
    "DOGE": "dogecoin",
    "SHIB": "shiba-inu",
    "PEPE": "pepe",
    "ARB": "arbitrum",
    "OP": "optimism",
    "SUI": "sui",
    "APT": "aptos",
    "SEI": "sei-network",
    "TIA": "celestia",
    "INJ": "injective-protocol",
    "FET": "fetch-ai",
    "RENDER": "render-token",
    "FIL": "filecoin",
    "HBAR": "hedera-hashgraph",
    "ALGO": "algorand",
    "VET": "vechain",
    "ICP": "internet-computer",
    "FTM": "fantom",
    "SAND": "the-sandbox",
    "MANA": "decentraland",
    "AXS": "axie-infinity",
    "CRV": "curve-dao-token",
    "MKR": "maker",
    "SNX": "synthetix-network-token",
    "GRT": "the-graph",
    "IMX": "immutable-x",
    "STX": "blockstack",
    "RUNE": "thorchain",
    "ZIG": "zigcoin",
    "SKR": "saakuru-protocol",  # best-effort; may fail → meta.failed
    "BLESS": "bless",  # best-effort
    "LAB": "lab",
    "H": "humanode",  # best-effort; fail-open if wrong
    "DELLG": "dell",  # unlikely — will fail soft
    "GRAM": "gram",
}


_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()
# Long TTL: Sanbase Pro is rate-limited (~5k calls/mo). Prefer global Redis only.
_DEFAULT_TTL = 21600.0  # 6h — asset API is optional/expensive


def resolve_santiment_slug(symbol: str) -> str | None:
    """Map trading symbol to Santiment project slug."""
    s = str(symbol or "").strip().upper().replace("-", "/")
    base = s.split("/")[0] if s else ""
    if not base:
        return None
    if base in _SLUG_MAP:
        return _SLUG_MAP[base]
    # env override: SANTIMENT_SLUG_MAP=BLESS:bless-token,FOO:bar
    raw = (os.environ.get("SANTIMENT_SLUG_MAP") or "").strip()
    if raw:
        for part in raw.split(","):
            if ":" not in part:
                continue
            k, v = part.split(":", 1)
            if k.strip().upper() == base:
                return v.strip().lower() or None
    # last resort: lowercase ticker (works for some projects)
    return base.lower()


def get_global_santiment(config_raw: dict | None = None) -> dict[str, Any]:
    """Policy-facing global regime (existing bot path)."""
    try:
        from services.santiment_policy import get_santiment_policy

        return dict(get_santiment_policy(config_raw) or {})
    except Exception as e:
        return {
            "active": False,
            "fresh": False,
            "rationale": f"santiment_policy_error:{type(e).__name__}",
            "size_mult": 1.0,
            "block_buys": False,
        }


def get_global_snapshot() -> dict[str, Any] | None:
    try:
        from services.santiment_store import get_latest_snapshot, snapshot_is_fresh

        snap = get_latest_snapshot(allow_redis=True)
        if not snap:
            return None
        return {
            **snap,
            "fresh": snapshot_is_fresh(snap),
        }
    except Exception:
        return None


def _cache_get(key: str) -> dict[str, Any] | None:
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if not hit:
            return None
        exp, val = hit
        if time.time() > exp:
            _CACHE.pop(key, None)
            return None
        return dict(val)


def _cache_set(key: str, val: dict[str, Any], ttl: float) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.time() + max(60.0, ttl), dict(val))


def fetch_asset_santiment(
    symbol: str,
    *,
    api_key: str | None = None,
    ttl_sec: float | None = None,
    force: bool = False,
    lean: bool = True,
    micro: bool = True,
) -> dict[str, Any]:
    """Per-asset SanAPI bundle (cached). Fail-open.

    Prefer **off** in sniper config — global regime via Redis is enough and free
    of extra API calls. When enabled: micro=1 metric (DAA), 6h TTL, no lag retry.
    """
    slug = resolve_santiment_slug(symbol)
    ttl = float(ttl_sec if ttl_sec is not None else _DEFAULT_TTL)
    mode = "micro" if micro else ("lean" if lean else "full")
    cache_key = f"asset:{mode}:{slug or symbol}"
    if not force:
        cached = _cache_get(cache_key)
        if cached is not None:
            cached["from_cache"] = True
            return cached

    key = (api_key or os.environ.get("SANTIMENT_API_KEY") or "").strip()
    if not key or not slug:
        out = {
            "available": False,
            "symbol": symbol,
            "slug": slug,
            "reason": "no_api_key" if not key else "no_slug",
            "features": {},
            "meta": {},
            "from_cache": False,
        }
        _cache_set(cache_key, out, min(ttl, 300))
        return out

    try:
        from services.santiment_sidecar.client import SantimentClient

        client = SantimentClient(key)
        raw = client.fetch_asset_signals(
            slug, lean=lean, micro=micro, try_research=False
        )
        features = dict(raw.get("features") or {})
        meta = dict(raw.get("meta") or {})
        live = [
            k
            for k in (meta.get("metrics_ok") or [])
            if not str(k).startswith("research_")
        ]
        out = {
            "available": bool(live or features),
            "symbol": symbol,
            "slug": slug,
            "reason": None if features else "no_metrics",
            "features": features,
            "meta": meta,
            "policy_fresh": bool(meta.get("fresh")),
            "from_cache": False,
            "api_calls": meta.get("api_calls_this_fetch"),
        }
        _cache_set(cache_key, out, ttl)
        return out
    except Exception as e:
        log(f"santiment asset fetch {symbol}/{slug}: {e}", "DEBUG")
        out = {
            "available": False,
            "symbol": symbol,
            "slug": slug,
            "reason": f"error:{type(e).__name__}",
            "features": {},
            "meta": {},
            "from_cache": False,
        }
        _cache_set(cache_key, out, 120)
        return out


def score_asset_signals(
    features: dict[str, float],
    *,
    allow_research: bool = True,
) -> dict[str, Any]:
    """Pure: map asset features → soft decision hints for recovery DCA.

    Live keys drive size_mult fully. research_* (lagged Pro metrics) only apply
    soft half-weight and never alone create hard caution for block paths.
    """
    f = features or {}
    hints: list[str] = []
    size_mult = 1.0
    social_hot = False
    onchain_weak = False
    exchange_distribution = False
    high_vol = False
    used_research = False

    def _level(key: str) -> tuple[float | None, bool]:
        """Return (value, is_research). Prefer live over research_*."""
        if key in f and f.get(key) is not None:
            try:
                return float(f[key]), False
            except (TypeError, ValueError):
                pass
        if allow_research:
            rk = f"research_{key}"
            if rk in f and f.get(rk) is not None:
                try:
                    return float(f[rk]), True
                except (TypeError, ValueError):
                    pass
        return None, False

    def _d(key: str) -> tuple[float | None, bool]:
        """Return (delta, is_research). Prefer live."""
        return _level(f"{key}_delta_1d")

    def _w(is_research: bool) -> float:
        return 0.5 if is_research else 1.0

    # Social spike without structure is often a trap into dumps
    sv, sv_r = _d("social_volume")
    if sv is not None and sv >= 0.35:
        social_hot = not sv_r  # only live social is hard caution
        hints.append("social_volume_spike" + ("_research" if sv_r else ""))
        size_mult *= 1.0 - (0.15 * _w(sv_r))
        used_research = used_research or sv_r
    if sv is not None and sv <= -0.25:
        hints.append("social_cooling" + ("_research" if sv_r else ""))
        size_mult *= 1.0 + (0.05 * _w(sv_r))
        used_research = used_research or sv_r

    daa, daa_r = _d("daa")
    if daa is not None and daa <= -0.15:
        onchain_weak = not daa_r
        hints.append("daa_declining" + ("_research" if daa_r else ""))
        size_mult *= 1.0 - (0.2 * _w(daa_r))
        used_research = used_research or daa_r
    if daa is not None and daa >= 0.15:
        hints.append("daa_rising" + ("_research" if daa_r else ""))
        size_mult *= 1.0 + (0.08 * _w(daa_r))
        used_research = used_research or daa_r

    # Volatility: free/realtime on Pro — cut size into chaos
    vol, vol_r = _level("vol_1d")
    if vol is not None:
        used_research = used_research or vol_r
        if vol >= 0.08:
            high_vol = True
            hints.append("vol_extreme" + ("_research" if vol_r else ""))
            size_mult *= 1.0 - (0.3 * _w(vol_r))
        elif vol >= 0.04:
            high_vol = True
            hints.append("vol_elevated" + ("_research" if vol_r else ""))
            size_mult *= 1.0 - (0.15 * _w(vol_r))

    # Exchange flows: inflow → distribution risk for DCA adds
    try:
        inf = None
        outf = None
        flow_research = False
        if "exchange_inflow" in f:
            inf = float(f["exchange_inflow"])
        elif allow_research and "research_exchange_inflow" in f:
            inf = float(f["research_exchange_inflow"])
            flow_research = True
        if "exchange_outflow" in f:
            outf = float(f["exchange_outflow"])
        elif allow_research and "research_exchange_outflow" in f:
            outf = float(f["research_exchange_outflow"])
            flow_research = True
        if inf is not None and outf is not None:
            w = _w(flow_research)
            if inf > 0 and inf > outf * 1.25:
                if not flow_research:
                    exchange_distribution = True
                hints.append(
                    "exchange_inflow_dominant" + ("_research" if flow_research else "")
                )
                size_mult *= 1.0 - (0.25 * w)
                used_research = used_research or flow_research
            elif outf > inf * 1.25:
                hints.append(
                    "exchange_outflow_dominant" + ("_research" if flow_research else "")
                )
                size_mult *= 1.0 + (0.05 * w)
                used_research = used_research or flow_research
    except (TypeError, ValueError):
        pass

    inf_d, inf_r = _d("exchange_inflow")
    if inf_d is not None and inf_d >= 0.3:
        if not inf_r:
            exchange_distribution = True
        if "exchange_inflow_spike" not in hints and "exchange_inflow_spike_research" not in hints:
            hints.append("exchange_inflow_spike" + ("_research" if inf_r else ""))
        size_mult *= 1.0 - (0.2 * _w(inf_r))
        used_research = used_research or inf_r

    # MVRV high → crowded valuation (soft)
    try:
        mvrv = None
        m_r = False
        if "mvrv" in f:
            mvrv = float(f["mvrv"])
        elif allow_research and "research_mvrv" in f:
            mvrv = float(f["research_mvrv"])
            m_r = True
        if mvrv is not None:
            if mvrv >= 2.5:
                hints.append("mvrv_elevated" + ("_research" if m_r else ""))
                size_mult *= 1.0 - (0.1 * _w(m_r))
                used_research = used_research or m_r
            elif 0 < mvrv <= 1.0:
                hints.append("mvrv_cheap" + ("_research" if m_r else ""))
                size_mult *= 1.0 + (0.05 * _w(m_r))
                used_research = used_research or m_r
    except (TypeError, ValueError):
        pass

    size_mult = max(0.5, min(1.25, size_mult))
    caution = social_hot or onchain_weak or exchange_distribution or high_vol
    return {
        "size_mult": round(size_mult, 4),
        "hints": hints,
        "social_hot": social_hot,
        "onchain_weak": onchain_weak,
        "exchange_distribution": exchange_distribution,
        "high_vol": high_vol,
        "used_research": used_research,
        "caution": caution,
    }


def build_santiment_enrichment(
    symbol: str,
    *,
    config_raw: dict | None = None,
    fetch_asset: bool = False,
    api_key: str | None = None,
    asset_ttl_sec: float | None = None,
    lean: bool = True,
    micro: bool = True,
) -> dict[str, Any]:
    """Santiment pack for one sniper candidate.

    Always includes global regime (Redis/store) — **zero** SanAPI calls.
    Per-asset SanAPI only if fetch_asset=True (default off; thrift).
    """
    global_pol = get_global_santiment(config_raw)
    snap = get_global_snapshot()
    asset: dict[str, Any] = {
        "available": False,
        "reason": "disabled",
        "features": {},
        "meta": {},
        "score": {},
    }
    if fetch_asset:
        asset = fetch_asset_santiment(
            symbol,
            api_key=api_key,
            ttl_sec=asset_ttl_sec,
            lean=lean,
            micro=micro,
        )
        asset["score"] = score_asset_signals(dict(asset.get("features") or {}))

    # Combined size mult: global * asset (soft). 0.0 is valid (CRASH).
    try:
        raw_g = global_pol.get("size_mult")
        g_mult = float(1.0 if raw_g is None else raw_g)
    except (TypeError, ValueError):
        g_mult = 1.0
    # apply_size_mult only when policy active (or fail-closed active)
    if not global_pol.get("active") or not global_pol.get("apply_size_mult", True):
        g_mult = 1.0
    a_mult = 1.0
    if asset.get("available") and isinstance(asset.get("score"), dict):
        try:
            raw_a = (asset.get("score") or {}).get("size_mult")
            a_mult = float(1.0 if raw_a is None else raw_a)
        except (TypeError, ValueError):
            a_mult = 1.0
    # Floor only when not hard-blocked; CRASH/block keeps true mult (incl. 0)
    product = g_mult * a_mult
    if product <= 0:
        combined = 0.0
    else:
        combined = max(0.35, min(1.5, product))

    block = bool(global_pol.get("block_buys"))
    regime = global_pol.get("regime")
    social_block = block or (
        str(regime or "").upper() in ("CRASH",)
        and bool(global_pol.get("fresh") or global_pol.get("active"))
    )

    return {
        "global": global_pol,
        "snapshot_fresh": bool(
            (snap or {}).get("fresh") if snap else global_pol.get("fresh")
        ),
        "snapshot_as_of": (snap or {}).get("as_of") if snap else global_pol.get("as_of"),
        "regime": regime,
        "global_size_mult": g_mult,
        "asset": asset,
        "asset_size_mult": a_mult,
        "combined_size_mult": round(combined, 4),
        "social_block": social_block,
        "social_caution": bool(
            (asset.get("score") or {}).get("caution")
            or str(regime or "").upper() in ("RISK_OFF", "CRASH")
        ),
        "rationale": str(global_pol.get("rationale") or ""),
        "scores": (snap or {}).get("scores") if snap else None,
        "features_global": (snap or {}).get("features") if snap else None,
        "policy_inputs": ((snap or {}).get("meta") or {}).get("policy_inputs")
        if snap
        else None,
    }


def apply_santiment_to_candidate(
    cand: dict[str, Any],
    pack: dict[str, Any],
) -> dict[str, Any]:
    """Merge Santiment pack into candidate for checklist/policy."""
    out = dict(cand)
    out["santiment"] = pack
    if pack.get("social_block"):
        out["social_block"] = True
        out["block_buys"] = True
    if pack.get("social_caution"):
        out["social_caution"] = True
    regime = pack.get("regime")
    if regime:
        out["santiment_regime"] = regime
    asset = pack.get("asset") if isinstance(pack.get("asset"), dict) else {}
    score = asset.get("score") if isinstance(asset.get("score"), dict) else {}
    if score.get("exchange_distribution"):
        # align with evidence/wallet caution
        out.setdefault("wallet", {})
        if isinstance(out["wallet"], dict):
            # don't claim available unless real wallet; mark flow signal
            out["santiment_exchange_distribution"] = True
    if score.get("social_hot"):
        out["social_noise"] = True
    # quality signal
    if pack.get("snapshot_fresh") or asset.get("available"):
        out["santiment_fresh"] = True
    return out


def apply_santiment_size(
    usdt: float,
    size_reason: str,
    pack: dict[str, Any],
    *,
    cfg: dict[str, Any] | None = None,
) -> tuple[float, str, list[str]]:
    """Apply combined Santiment mult; block on CRASH social_block if configured."""
    cfg = cfg or {}
    extra: list[str] = []
    usdt = float(usdt or 0)
    reason = str(size_reason or "")
    if usdt <= 0:
        return usdt, reason, extra

    if pack.get("social_block") and bool(cfg.get("deep_santiment_block_buys", True)):
        return 0.0, "santiment_block_buys", ["santiment_block_buys"]

    try:
        raw_m = pack.get("combined_size_mult")
        mult = float(1.0 if raw_m is None else raw_m)
    except (TypeError, ValueError):
        mult = 1.0
    if mult <= 0:
        return 0.0, "santiment_size_zero", extra + ["santiment_mult=0"]
    if abs(mult - 1.0) > 0.02:
        usdt = round(usdt * mult, 2)
        extra.append(f"santiment_mult={mult}")
        min_u = float(cfg.get("min_meaningful_usdt") or 200)
        if usdt < min_u:
            return 0.0, "santiment_size_too_small", extra

    # Asset exchange distribution: demote heavy
    asset = pack.get("asset") if isinstance(pack.get("asset"), dict) else {}
    score = asset.get("score") if isinstance(asset.get("score"), dict) else {}
    if score.get("exchange_distribution") and "HEAVY" in reason.upper():
        small = float(cfg.get("small_dca_usdt") or 500)
        min_u = float(cfg.get("min_meaningful_usdt") or 200)
        if small >= min_u:
            return round(min(usdt, small), 2), "DCA_SMALL_santiment_flow", extra
        return 0.0, "santiment_flow_block", extra + ["exchange_distribution"]

    return usdt, reason, extra
