"""CMC Pro API → CoinFactDraft list (#105 D8b).

Structured quotes/content (not HTML cmc-ai pages). Fail-open, plan-aware.
No ledger writes.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from intelligence.memory.coin_facts import CoinFactDraft, normalize_symbol
from logger import log

BASE_URL = "https://pro-api.coinmarketcap.com/v1"
HttpGetJson = Callable[[str, dict[str, Any]], dict[str, Any] | None]


def cmc_pro_config(coin_facts_cfg: dict | None = None) -> dict[str, Any]:
    defaults = {
        "enabled": True,
        "quotes": True,
        "content": True,
        "trending_annotate": True,
        "ttl_hours_quotes": 6,
        "max_symbols_per_cycle": 40,
        "volume_breakout_chg_pct": 8.0,
        "volume_breakout_vol_chg_pct": 40.0,
        "dump_chg_pct": -12.0,
        "pump_chg_pct": 15.0,
        "rs_outperform_pct": 5.0,
        "rs_underperform_pct": -8.0,
    }
    raw = dict((coin_facts_cfg or {}).get("sources") or {}).get("cmc_pro") or {}
    if not isinstance(raw, dict):
        raw = {}
    return {**defaults, **raw}


def _base_symbol(pair: str) -> str:
    s = normalize_symbol(pair)
    return s.split("/")[0].upper() if s else ""


def _headers(api_key: str) -> dict[str, str]:
    return {"X-CMC_PRO_API_KEY": api_key, "Accept": "application/json"}


def default_http_get_json(path: str, params: dict[str, Any], *, api_key: str) -> dict | None:
    try:
        import requests
    except Exception:
        return None
    if not api_key:
        return None
    try:
        resp = requests.get(
            f"{BASE_URL}{path}",
            headers=_headers(api_key),
            params=params,
            timeout=20,
        )
        if resp.status_code != 200:
            log(f"cmc_pro {path} status={resp.status_code}", "DEBUG")
            return None
        return resp.json()
    except Exception as e:
        log(f"cmc_pro {path} failed: {e}", "DEBUG")
        return None


@dataclass(frozen=True)
class QuoteSnap:
    """Normalized 24h quote snapshot for one symbol."""

    symbol: str
    chg: float
    vol: float
    vol_chg: float
    price: float
    btc_chg: float | None = None

    @property
    def meta(self) -> dict[str, Any]:
        return {
            "kind": "coin_fact",
            "provider": "cmc_pro",
            "change_24h_pct": round(self.chg, 3),
            "volume_24h": round(self.vol, 2),
            "volume_change_24h_pct": round(self.vol_chg, 3),
            "price": self.price,
        }


def _num(usd: dict, key: str) -> float:
    try:
        return float(usd.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_quote_snap(
    symbol: str,
    quote: dict[str, Any],
    *,
    btc_chg_24h: float | None = None,
) -> QuoteSnap:
    q = quote.get("quote") or quote
    usd = (q.get("USD") or {}) if isinstance(q, dict) and "USD" in q else (q if isinstance(q, dict) else {})
    btc = None
    if btc_chg_24h is not None:
        try:
            btc = float(btc_chg_24h)
        except (TypeError, ValueError):
            btc = 0.0
    return QuoteSnap(
        symbol=normalize_symbol(symbol),
        chg=_num(usd, "percent_change_24h"),
        vol=_num(usd, "volume_24h"),
        vol_chg=_num(usd, "volume_change_24h"),
        price=_num(usd, "price"),
        btc_chg=btc,
    )


def _draft(
    snap: QuoteSnap,
    *,
    event_type: str,
    impact: float,
    description: str,
    polarity: str,
    signal: str,
    extra: dict | None = None,
) -> CoinFactDraft:
    meta = {**snap.meta, "signal": signal, **(extra or {})}
    return CoinFactDraft(
        event_type=event_type,
        impact_score=impact,
        description=description,
        source="cmc_pro_quotes",
        polarity_hint=polarity,
        metadata=meta,
    )


def _quote_signals(snap: QuoteSnap, pro: dict) -> Iterator[CoinFactDraft]:
    """Yield zero or more drafts from one quote snapshot (pure)."""
    vb_chg = float(pro.get("volume_breakout_chg_pct") or 8)
    vb_vol = float(pro.get("volume_breakout_vol_chg_pct") or 40)
    dump = float(pro.get("dump_chg_pct") or -12)
    pump = float(pro.get("pump_chg_pct") or 15)
    s, chg, vol, vol_chg = snap.symbol, snap.chg, snap.vol, snap.vol_chg

    if chg >= vb_chg and vol_chg >= vb_vol:
        yield _draft(
            snap,
            event_type="volume_breakout",
            impact=min(0.45, 0.15 + chg / 100.0),
            description=(
                f"CMC Pro: {s} +{chg:.1f}% 24h with volume change "
                f"{vol_chg:.0f}% (vol ${vol:,.0f})"
            ),
            polarity="+",
            signal="volume_breakout",
        )
    elif chg <= dump:
        mild = chg > dump - 8
        yield _draft(
            snap,
            event_type="profit_taking_narrative" if mild else "structure_risk",
            impact=max(-0.7, chg / 40.0),
            description=(
                f"CMC Pro: {s} {chg:.1f}% 24h drawdown "
                f"(vol ${vol:,.0f}, vol_chg {vol_chg:.0f}%)"
            ),
            polarity="-",
            signal="dump_24h",
        )
    elif chg >= pump and vol_chg < vb_vol:
        yield _draft(
            snap,
            event_type="flow_only_move",
            impact=-0.1,
            description=(
                f"CMC Pro: {s} +{chg:.1f}% 24h without strong volume "
                f"confirmation (vol_chg {vol_chg:.0f}%)"
            ),
            polarity="caution",
            signal="pump_low_vol",
        )

    if snap.btc_chg is None:
        return
    rel = chg - snap.btc_chg
    out_thr = float(pro.get("rs_outperform_pct") or 5)
    und_thr = float(pro.get("rs_underperform_pct") or -8)
    btc = snap.btc_chg
    extra = {"btc_chg_24h": btc, "rel_pp": round(rel, 2)}
    if rel >= out_thr:
        yield _draft(
            snap,
            event_type="relative_strength",
            impact=min(0.35, rel / 50.0),
            description=(
                f"CMC Pro: {s} outperforms BTC by {rel:.1f}pp "
                f"(coin {chg:.1f}% vs BTC {btc:.1f}%)"
            ),
            polarity="+",
            signal="rs_out",
            extra=extra,
        )
    elif rel <= und_thr:
        yield _draft(
            snap,
            event_type="structure_risk",
            impact=max(-0.45, rel / 40.0),
            description=(
                f"CMC Pro: {s} underperforms BTC by {abs(rel):.1f}pp "
                f"(coin {chg:.1f}% vs BTC {btc:.1f}%)"
            ),
            polarity="-",
            signal="rs_under",
            extra=extra,
        )


def quote_row_to_drafts(
    symbol: str,
    quote: dict[str, Any],
    *,
    btc_chg_24h: float | None = None,
    cfg: dict | None = None,
) -> list[CoinFactDraft]:
    """Pure mapping: one CMC quotes.latest row → zero or more drafts."""
    pro = {**cmc_pro_config(), **(cfg or {})}
    snap = parse_quote_snap(symbol, quote, btc_chg_24h=btc_chg_24h)
    return list(_quote_signals(snap, pro))


_CONTENT_NEG = re.compile(
    r"\b(hack|exploit|sec\s*charg|ban|delist|lawsuit|fraud|crash|rug)\b", re.I
)
_CONTENT_POS = re.compile(
    r"\b(partner|listing|mainnet|upgrade|etf|approv|integration)\b", re.I
)


def content_item_to_draft(
    item: dict[str, Any], universe_bases: set[str]
) -> list[tuple[str, CoinFactDraft]]:
    """Map one content/latest item to (symbol, draft) for matching universe coins."""
    title = str(item.get("title") or item.get("subtitle") or "").strip()
    if not title:
        return []
    body = str(item.get("subtitle") or item.get("source_name") or "")[:200]
    text = f"{title} {body}"

    bases: list[str] = []
    for a in item.get("assets") or item.get("currencies") or []:
        if isinstance(a, dict):
            sym = str(a.get("symbol") or a.get("slug") or "").upper()
            if sym:
                bases.append(sym.split("/")[0])
        elif isinstance(a, str):
            bases.append(a.upper().split("/")[0])

    if not bases:
        for m in re.findall(r"\b([A-Z]{2,10})\b", title.upper()):
            if m in universe_bases and m not in ("USD", "USDT", "THE", "AND", "FOR", "API"):
                bases.append(m)

    bases = [b for b in bases if b in universe_bases][:3]
    if not bases:
        return []

    if _CONTENT_NEG.search(text):
        et, imp, pol = (
            ("sec_alert", -0.55, "-")
            if re.search(r"hack|exploit", text, re.I)
            else ("structure_risk", -0.55, "-")
        )
    elif _CONTENT_POS.search(text):
        et, imp, pol = "partnership", 0.3, "+"
    else:
        et, imp, pol = "sector_rotation", 0.05, "mixed"

    url = str(item.get("url") or item.get("source_url") or "")
    return [
        (
            f"{base}/USDT",
            CoinFactDraft(
                event_type=et,
                impact_score=imp,
                description=f"CMC content: {title[:200]}",
                source="cmc_pro_content",
                polarity_hint=pol,
                metadata={"kind": "coin_fact", "provider": "cmc_pro", "url": url},
            ),
        )
        for base in bases
    ]


def fetch_quotes_for_symbols(
    symbols: list[str],
    *,
    api_key: str | None = None,
    http_get_json: HttpGetJson | None = None,
) -> tuple[dict[str, dict], float | None]:
    """Batch quotes/latest. Returns (base→raw_row, btc_chg_24h)."""
    key = (api_key or os.getenv("CMC_API_KEY") or "").strip()
    bases: list[str] = []
    seen: set[str] = set()
    for s in symbols or []:
        b = _base_symbol(s)
        if b and b not in seen:
            seen.add(b)
            bases.append(b)
    if "BTC" not in seen:
        bases.append("BTC")
    if not bases or not key:
        return {}, None

    def _get(path: str, params: dict) -> dict | None:
        if http_get_json:
            return http_get_json(path, params)
        return default_http_get_json(path, params, api_key=key)

    by_base: dict[str, dict] = {}
    btc_chg = None
    for i in range(0, len(bases), 80):
        chunk = bases[i : i + 80]
        data = _get(
            "/cryptocurrency/quotes/latest",
            {"symbol": ",".join(chunk), "convert": "USD"},
        )
        if not data:
            continue
        payload = (data.get("data") or {}) if isinstance(data, dict) else {}
        rows = (
            payload
            if isinstance(payload, list)
            else list(payload.values())
            if isinstance(payload, dict)
            else []
        )
        for row in rows:
            if isinstance(row, list):
                row = row[0] if row else {}
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "").upper()
            if not sym:
                continue
            by_base[sym] = row
            if sym == "BTC":
                usd = (row.get("quote") or {}).get("USD") or {}
                try:
                    btc_chg = float(usd.get("percent_change_24h") or 0)
                except (TypeError, ValueError):
                    btc_chg = None
    return by_base, btc_chg


def fetch_content_latest(
    *,
    api_key: str | None = None,
    limit: int = 20,
    http_get_json: HttpGetJson | None = None,
) -> list[dict]:
    key = (api_key or os.getenv("CMC_API_KEY") or "").strip()
    if not key:
        return []

    def _get(path: str, params: dict) -> dict | None:
        if http_get_json:
            return http_get_json(path, params)
        return default_http_get_json(path, params, api_key=key)

    data = _get("/content/latest", {"limit": int(limit)})
    if not data:
        return []
    payload = data.get("data")
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for k in ("news", "articles", "items"):
            if isinstance(payload.get(k), list):
                return [x for x in payload[k] if isinstance(x, dict)]
    return []


def collect_cmc_pro_drafts(
    symbols: list[str],
    *,
    config_raw: dict | None = None,
    api_key: str | None = None,
    http_get_json: HttpGetJson | None = None,
    quotes_payload: dict[str, dict] | None = None,
    btc_chg_24h: float | None = None,
    content_items: list[dict] | None = None,
    capabilities: dict | None = None,
) -> list[tuple[str, CoinFactDraft]]:
    """Return list of (symbol, draft). Inject payloads for unit tests."""
    from intelligence.memory.coin_facts import coin_facts_config

    cf = coin_facts_config(config_raw)
    pro = cmc_pro_config(cf)
    if not pro.get("enabled", True):
        return []

    key = (api_key or os.getenv("CMC_API_KEY") or "").strip()
    if quotes_payload is None and content_items is None and not key:
        return []

    if capabilities is None and quotes_payload is None:
        try:
            from data.cmc_capabilities import probe_capabilities

            capabilities = probe_capabilities(key)
        except Exception:
            capabilities = {"endpoints": {"quotes/latest": True}}

    eps = (capabilities or {}).get("endpoints") or {}
    out: list[tuple[str, CoinFactDraft]] = []

    if pro.get("quotes", True) and (quotes_payload is not None or eps.get("quotes/latest", True)):
        if quotes_payload is None:
            quotes_payload, btc_chg_24h = fetch_quotes_for_symbols(
                symbols, api_key=key, http_get_json=http_get_json
            )
        for sym in symbols:
            base = _base_symbol(sym)
            row = (quotes_payload or {}).get(base)
            if not row:
                continue
            for d in quote_row_to_drafts(
                normalize_symbol(sym), row, btc_chg_24h=btc_chg_24h, cfg=pro
            ):
                out.append((normalize_symbol(sym), d))

    if pro.get("content", True) and (
        content_items is not None or eps.get("content/latest", False)
    ):
        if content_items is None:
            content_items = fetch_content_latest(
                api_key=key, limit=20, http_get_json=http_get_json
            )
        universe_bases = {_base_symbol(s) for s in symbols}
        for item in content_items or []:
            out.extend(content_item_to_draft(item, universe_bases))

    return out
