# Coin Fact Layer (D8 / #103) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CMC-AI coin facts (news + structure + structural drivers) for every open position and watchlist coin → `memory_market_events` (+ optional RAG) → `DcaContext` / `evaluate_dca_policy` with `fact_*` reason_codes — fail-open, no ledger writes, no Grok on the hot path.

**Architecture:** Pure schema + policy factors first (no network). Then a bounded Hermes/memory cycle job: resolve CMC slug → fetch/parse three CMC AI pages (or fixture in tests) → upsert `MarketEvent`s. `build_dca_context` loads recent symbol-scoped facts from memory; policy applies the v1 factor table. Watchlist ranking is phase C.

**Tech Stack:** Python 3, existing `intelligence.memory` (`MarketEvent`, `MemoryStore`, `run_memory_cycle`), `strategies.dca_policy` / `dca_context`, optional HTTP fetch (same style as `news_providers._http_get`), pytest.

**Spec (product):** `plans/coin-fact-layer-d8.md`  
**Ticket:** [#103](https://github.com/jholze/xagent-trading-bot/issues/103) · Parent epic #79  

**Out of scope:** #102 scheduled DCA (leave code as-is; `dca.scheduled.enabled=false` in `config.json`). No Railway deploy in this plan unless explicitly requested.

**#102 status (do not change unless asked):**
- Config already: `"scheduled": { "enabled": false, "mode": "shadow", ... }` under `volatile_altcoin.dca`
- Code may exist; default off → production path unchanged

---

## File map

| File | Responsibility |
|------|----------------|
| `intelligence/memory/coin_facts.py` | **New.** Config, taxonomy constants, pure classifiers, `CoinFact` dataclass, fixture-friendly parse API |
| `intelligence/memory/coin_facts_cmc.py` | **New.** Slug resolve, URL build, fetch HTML/JSON, parse three endpoints → list of fact dicts |
| `intelligence/memory/coin_facts_ingest.py` | **New.** Universe (positions ∪ watchlist), cycle orchestrator, persist via `MemoryStore.upsert_event` |
| `intelligence/memory/service.py` | Wire `sync_coin_facts` into `run_memory_cycle` (fail-open) |
| `strategies/dca_policy.py` | Extend `DcaContext` + `evaluate_dca_policy` with fact factors |
| `strategies/dca_context.py` | Load recent fact summary for symbol into context |
| `strategies/dca_ask_snapshot.py` | Optional one-line fact summary in LIVE_DCA_POLICY block |
| `config.json` | `memory.coin_facts` block (default **enabled true only in staging when soaked**; start **enabled false** or true with shadow-friendly policy) |
| `tests/unit/test_coin_facts_*.py` | Pure policy, parse fixtures, ingest idempotency, context load |
| `tests/fixtures/cmc_ai/` | HTML/markdown fixtures for ALLO (and one second coin) |

Reuse (do not reinvent):
- `intelligence.memory.models.MarketEvent`
- `intelligence.memory.event_ingest.make_event_id`, `ingest_news_item` patterns
- `intelligence.memory.store.MemoryStore.upsert_event`
- `strategies.dca_decision_event` pattern for optional RAG index (same `index_rag` style)

---

## Design locks (implement exactly)

### MarketEvent mapping

```text
event_id     = make_event_id(source, f"{slug}|{endpoint}|{event_type}|{as_of_day}|{hash(desc[:80])}")
event_type   = taxonomy (unlock, profit_taking_narrative, volume_breakout, …)
symbols      = ["ALLO/USDT"]   # always pair form
impact_score = -1.0 … +1.0
description  = short TLDR line
source       = cmc_ai_updates | cmc_ai_price | cmc_ai_prediction
url          = page URL
metadata     = { slug, endpoint, ttl_hours, levels?, outlook?, raw_tags? }
```

### Policy rules (v1)

| Signal | Effect | reason_code |
|--------|--------|-------------|
| hack / exploit / delist | skip | `fact_hard_negative` |
| unlock / supply_overhang (large or impact ≤ −0.5) | mult ≤ 0.5 or skip if impact ≤ −0.8 | `fact_unlock` |
| profit_taking_narrative | mult ≤ 0.7 | `fact_profit_taking` |
| flow_only_move | mult ≤ 0.8; no boosts from facts | `fact_flow_only` |
| structure_risk | mult ≤ 0.5 | `fact_structure_risk` |
| volume_breakout (not hard-neg) | mult × 1.1 (cap) | `fact_volume_breakout` |
| partnership / utility_adoption + oversold (loss_pct ≤ −5) | mult × 1.1 | `fact_catalyst` / `fact_utility` |
| social_spike only | ignore | `fact_noise_ignore` |
| numeric price target only | never stored as policy signal | — |
| no facts | mult unchanged | fail-open |

Skip always beats size. Fact mult multiplies existing mult; clamp with `max_policy_mult`.

### DcaContext fields (add)

```python
fact_hard_negative: bool = False
fact_unlock: bool = False
fact_profit_taking: bool = False
fact_flow_only: bool = False
fact_structure_risk: bool = False
fact_volume_breakout: bool = False
fact_catalyst: bool = False
fact_utility: bool = False
fact_noise_only: bool = False
fact_event_count: int = 0
fact_min_impact: float = 0.0   # most negative impact among fresh facts
fact_summary: str = ""        # short for audit /ask
```

### Config (start safe)

```json
"memory": {
  "coin_facts": {
    "enabled": false,
    "policy_apply": true,
    "lookback_hours": 72,
    "sources": {
      "cmc_ai": {
        "enabled": true,
        "scrape_fallback": true,
        "ttl_hours_updates": 48,
        "ttl_hours_price": 12,
        "ttl_hours_prediction": 72,
        "max_coins_per_cycle": 40,
        "interval_sec": 3600,
        "prediction_use_targets_for_policy": false
      }
    },
    "universe": ["open_positions", "watchlist"]
  }
}
```

Default **`enabled: false`** until first staging soak; flip true in staging only.

### #102

Do not enable. Do not remove. Ignore in this plan.

---

## Task breakdown

### Task 1: Pure taxonomy + classifiers (no I/O)

**Files:**
- Create: `intelligence/memory/coin_facts.py`
- Test: `tests/unit/test_coin_facts_classify.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_coin_facts_classify.py
from intelligence.memory.coin_facts import classify_latest_updates_bullet, classify_price_analysis_snippet

def test_profit_taking_bullet():
    r = classify_latest_updates_bullet(
        "ALLO cools ~10% after AI-token rotation pump; profit-taking noted"
    )
    assert r is not None
    assert r.event_type == "profit_taking_narrative"
    assert r.impact_score < 0

def test_unlock_bullet():
    r = classify_latest_updates_bullet("Large unlock / low float vesting overhang for ALLO")
    assert r.event_type in ("unlock", "supply_overhang")
    assert r.impact_score < 0

def test_hard_negative_hack():
    r = classify_latest_updates_bullet("Protocol hack drains bridge funds")
    assert r.event_type in ("hack", "sec_alert", "exploit")
    assert r.impact_score <= -0.8

def test_flow_only_from_analysis():
    r = classify_price_analysis_snippet(
        "No clear secondary driver; move appears flow-driven with volume spike"
    )
    assert r.event_type == "flow_only_move"

def test_ignore_numeric_price_target():
    r = classify_prediction_driver(
        "Price prediction: ALLO will hit $2.50 by 2026",
        section="bullish",
    )
    assert r is None or r.event_type == "ignore_target"
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError` or missing symbols)

```bash
python3 -m pytest tests/unit/test_coin_facts_classify.py -v
```

- [ ] **Step 3: Implement minimal `coin_facts.py`**

Include:
- `@dataclass CoinFactDraft` with fields: `event_type`, `impact_score`, `description`, `source`, `polarity_hint`
- Keyword/heuristic classifiers (regex OK for v1; no LLM)
- `EVENT_TYPES` frozenset from spec taxonomy
- `coin_facts_config(raw: dict) -> dict` merge defaults
- `coin_facts_enabled(raw) -> bool`

- [ ] **Step 4: Run tests — PASS**

- [ ] **Step 5: Commit**

```bash
git add intelligence/memory/coin_facts.py tests/unit/test_coin_facts_classify.py
git commit -m "feat(memory): coin-fact taxonomy and pure classifiers (#103 A)"
```

---

### Task 2: Policy fact factors (no network)

**Files:**
- Modify: `strategies/dca_policy.py`
- Test: `tests/unit/test_dca_policy.py` (extend) or `tests/unit/test_coin_facts_policy.py`

- [ ] **Step 1: Failing tests**

```python
from strategies.dca_policy import DcaContext, evaluate_dca_policy, dca_policy_config

def _cfg():
    return dca_policy_config({"policy": {"enabled": True, "shadow": False, "harvest_mode": "soft"}})

def test_hard_negative_skips():
    ctx = DcaContext(symbol="ALLO/USDT", cash_mode="STEADY", fact_hard_negative=True)
    r = evaluate_dca_policy(ctx, _cfg())
    assert r.skip is True
    assert "fact_hard_negative" in r.reason_codes

def test_no_facts_unchanged_vs_baseline():
    base = DcaContext(symbol="ALLO/USDT", cash_mode="STEADY", fusion_size_mult=1.0)
    with_facts = DcaContext(symbol="ALLO/USDT", cash_mode="STEADY", fusion_size_mult=1.0, fact_event_count=0)
    rb = evaluate_dca_policy(base, _cfg())
    rf = evaluate_dca_policy(with_facts, _cfg())
    assert rb.skip == rf.skip
    assert abs(rb.size_mult - rf.size_mult) < 1e-9

def test_profit_taking_reduces_mult():
    ctx = DcaContext(symbol="ALLO/USDT", cash_mode="STEADY", fact_profit_taking=True)
    r = evaluate_dca_policy(ctx, _cfg())
    assert r.skip is False
    assert r.size_mult <= 0.7 + 1e-9
    assert "fact_profit_taking" in r.reason_codes
```

- [ ] **Step 2: Extend `DcaContext` + `evaluate_dca_policy`**

After existing factors (funding / profile / score), apply fact block:

```python
# 10) Coin facts (shared layer) — skip beats size
if not skip and ctx.fact_hard_negative:
    skip = True
    reasons.append("fact_hard_negative")
if not skip and ctx.fact_unlock:
    mult *= _f(cfg, "fact_unlock_mult", 0.5)
    reasons.append("fact_unlock")
    if mult < 0.35 or float(getattr(ctx, "fact_min_impact", 0) or 0) <= -0.8:
        skip = True
        reasons.append("fact_unlock_skip")
# ... profit_taking, flow_only, structure_risk, volume_breakout, catalyst, utility
# noise_only: reasons.append("fact_noise_ignore") only, no mult change
```

Add defaults to `dca_policy_config`:

```python
"fact_unlock_mult": 0.5,
"fact_profit_taking_mult": 0.7,
"fact_flow_only_mult": 0.8,
"fact_structure_risk_mult": 0.5,
"fact_volume_breakout_mult": 1.1,
"fact_catalyst_mult": 1.1,
"fact_utility_mult": 1.1,
```

- [ ] **Step 3: Tests PASS**

- [ ] **Step 4: Commit**

```bash
git commit -m "feat(dca): apply coin-fact flags in policy (#103 A)"
```

---

### Task 3: Load facts into `build_dca_context`

**Files:**
- Modify: `strategies/dca_context.py`
- Create helper in `intelligence/memory/coin_facts.py`: `summarize_facts_for_symbol(store, symbol, lookback_hours) -> dict flags`
- Test: `tests/unit/test_coin_facts_context.py`

- [ ] **Step 1: Failing test with fake store**

```python
class FakeStore:
    def __init__(self, events):
        self._events = events
    def list_events_for_symbol(self, symbol, *, since_iso=None, limit=50):
        return self._events

def test_context_sets_hard_negative(monkeypatch):
    from intelligence.memory.models import MarketEvent
    from strategies.dca_context import build_dca_context
    ev = MarketEvent(
        event_id="t1", timestamp="2026-07-18T12:00:00Z",
        event_type="hack", symbols=["ALLO/USDT"], impact_score=-0.9,
        description="hack", source="cmc_ai_updates",
    )
    # patch MemoryStore / list path used by summarize
    ...
    ctx = build_dca_context(symbol="ALLO/USDT", include_rag=False, config_raw={"memory": {"coin_facts": {"enabled": True}}})
    assert ctx.fact_hard_negative is True
    assert ctx.fact_event_count >= 1
```

- [ ] **Step 2: Implement `summarize_facts_for_symbol`**

Query path options (pick one; prefer existing store API):
- If `MemoryStore` already has symbol query → use it
- Else add `list_events(symbol=..., event_types=..., since=..., limit=)` fail-open empty

Map event_type → flags (hard_neg, unlock, profit_taking, …).

- [ ] **Step 3: Wire end of `build_dca_context`** (after profile/macro, fail-open)

```python
try:
    from intelligence.memory.coin_facts import apply_facts_to_context, coin_facts_enabled
    if coin_facts_enabled(raw):
        apply_facts_to_context(ctx, config_raw=raw)
except Exception:
    pass
```

- [ ] **Step 4: Tests PASS + commit**

```bash
git commit -m "feat(dca): load coin facts into DcaContext (#103 A)"
```

---

### Task 4: CMC AI parse from fixtures (no live network in CI)

**Files:**
- Create: `intelligence/memory/coin_facts_cmc.py`
- Create: `tests/fixtures/cmc_ai/allora_latest_updates.html` (minimal HTML with TLDR-like bullets — capture from real page once, strip scripts)
- Create: similar stubs for price-analysis + price-prediction
- Test: `tests/unit/test_coin_facts_cmc_parse.py`

- [ ] **Step 1: Capture fixtures offline once** (agent or operator)

```bash
# optional local capture — not run in CI
# curl -A 'xagent-memory/1.0' -o tests/fixtures/cmc_ai/allora_latest_updates.html \
#   'https://coinmarketcap.com/cmc-ai/allora/latest-updates/'
```

If capture blocked, hand-write minimal HTML:

```html
<html><body>
  <h1>Latest updates</h1>
  <ul>
    <li>ALLO drops ~10% amid profit-taking after AI sector rotation</li>
    <li>Low float and vesting unlock overhang remains a risk</li>
  </ul>
</body></html>
```

- [ ] **Step 2: Tests**

```python
from pathlib import Path
from intelligence.memory.coin_facts_cmc import parse_latest_updates_html, parse_price_analysis_html, parse_price_prediction_html

FIX = Path("tests/fixtures/cmc_ai")

def test_parse_updates_yields_symbol_facts():
    html = (FIX / "allora_latest_updates.html").read_text()
    facts = parse_latest_updates_html(html, symbol="ALLO/USDT", slug="allora")
    assert facts
    types = {f.event_type for f in facts}
    assert "profit_taking_narrative" in types or "unlock" in types or "supply_overhang" in types

def test_prediction_ignores_numeric_targets():
    html = (FIX / "allora_price_prediction.html").read_text()
    facts = parse_price_prediction_html(html, symbol="ALLO/USDT", slug="allora")
    assert all(f.event_type != "price_target" for f in facts)
    # may include utility_adoption or supply_overhang from impact sections
```

- [ ] **Step 3: Implement parsers**

- Prefer CSS-light: strip tags → line/bullet split → `classify_*`
- `build_cmc_ai_urls(slug) -> dict[str, str]`
- `resolve_cmc_slug(symbol) -> str | None`: strip `/USDT`, lower; optional map file later; try CMC info cache if present in `data/`

- [ ] **Step 4: PASS + commit**

```bash
git commit -m "feat(memory): CMC AI HTML parse fixtures for coin facts (#103 B)"
```

---

### Task 5: Ingest + persist + cycle hook

**Files:**
- Create: `intelligence/memory/coin_facts_ingest.py`
- Modify: `intelligence/memory/service.py` (`run_memory_cycle`)
- Test: `tests/unit/test_coin_facts_ingest.py`

- [ ] **Step 1: Universe builder**

```python
def coin_fact_universe(config_raw: dict | None = None) -> list[str]:
    """Open positions ∪ active watchlist, capped, stable order (positions first)."""
    ...
```

Use `strategies.positions.list_active_positions` + `data_manager.load_effective_watchlist`. Cap with `max_coins_per_cycle`.

- [ ] **Step 2: Persist one draft**

```python
def persist_coin_fact(draft, *, store: MemoryStore, symbol: str, slug: str, url: str) -> str:
    # MarketEvent + upsert_event; optional embed_event; optional RAG like dca_decision_event
    ...
```

Idempotent `event_id` so re-runs do not spam.

- [ ] **Step 3: Cycle**

```python
def sync_coin_facts(store: MemoryStore | None = None, *, fetch_fn=None, config_raw=None) -> dict:
    """Returns {enabled, coins, events_written, errors}."""
    if not coin_facts_enabled(...):
        return {"enabled": False, "skipped": True}
    ...
```

`fetch_fn(url) -> str` injectable for tests (return fixture HTML).

- [ ] **Step 4: Wire service**

In `run_memory_cycle`, after news block:

```python
try:
    from intelligence.memory.coin_facts_ingest import sync_coin_facts
    out["coin_facts"] = sync_coin_facts(store)
except Exception as e:
    log(f"memory coin_facts cycle: {e}", "WARNING")
    out["coin_facts"] = {"error": str(e)[:200]}
```

- [ ] **Step 5: Test**

```python
def test_sync_writes_events_from_fixtures(tmp_mongo_or_fake_store):
    def fetch(url: str) -> str:
        if "latest-updates" in url:
            return Path("tests/fixtures/cmc_ai/allora_latest_updates.html").read_text()
        ...
    out = sync_coin_facts(store, fetch_fn=fetch, config_raw={...enabled true, max_coins: 1...})
    assert out["events_written"] >= 1
    # second call same fixtures → 0 or same ids (idempotent)
```

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(memory): coin-fact CMC AI ingest cycle (#103 B)"
```

---

### Task 6: Config + ask snapshot + docs

**Files:**
- Modify: `config.json` — add `memory.coin_facts` with **enabled: false**
- Modify: `strategies/dca_ask_snapshot.py` — if `ctx.fact_summary`, append line under LIVE_DCA_POLICY
- Modify: `plans/coin-fact-layer-d8.md` — status → Implementation plan linked
- Test: extend `tests/unit/test_dca_ask_snapshot.py` lightly

- [ ] **Step 1: Config block** (see Design locks; enabled false)

- [ ] **Step 2: Ask snapshot**

```python
if getattr(ctx, "fact_summary", ""):
    lines.append(f"  facts: {ctx.fact_summary[:200]}")
```

- [ ] **Step 3: Commit**

```bash
git commit -m "chore(config): coin_facts defaults off; surface facts in /ask (#103)"
```

---

### Task 7: Watchlist consumer (phase C — optional after soak)

**Files:**
- Prefer: hook where watchlist rank / score is computed (search `watchlist` rank in `strategies/` / `services/`)
- Soft: boost/downrank using same `summarize_facts_for_symbol`
- Test: unit with fake facts → rank order change

Only start after Task 5 is green in staging once.

- [ ] **Step 1: Locate rank function; add pure `fact_rank_delta(flags) -> float`**
- [ ] **Step 2: Wire fail-open**
- [ ] **Step 3: Test + commit**

```bash
git commit -m "feat(watchlist): coin-fact rank soft signal (#103 C)"
```

---

### Task 8: Staging verification (no deploy unless asked)

- [ ] **Step 1:** Enable only on Railway **test** env: `memory.coin_facts.enabled=true` (or env override if you add one)
- [ ] **Step 2:** Run one Hermes memory cycle; confirm `out["coin_facts"]` and Mongo `memory_market_events` with `source` in `cmc_ai_*` for ≥1 open symbol
- [ ] **Step 3:** `/ask` or log audit shows `fact_*` when applicable
- [ ] **Step 4:** Document soak note in `plans/coin-fact-layer-d8.md` Acceptance checkboxes
- [ ] **Do not** enable production until soak ≥ few days

---

## Test commands (agent checklist)

```bash
# Phase A
python3 -m pytest tests/unit/test_coin_facts_classify.py tests/unit/test_coin_facts_policy.py tests/unit/test_coin_facts_context.py tests/unit/test_dca_policy.py -q

# Phase B
python3 -m pytest tests/unit/test_coin_facts_cmc_parse.py tests/unit/test_coin_facts_ingest.py -q

# Regression: DCA + memory
python3 -m pytest tests/unit/test_dca_ask_snapshot.py tests/unit/test_trading_memory.py -q --tb=line
```

Expected: all new tests green; existing DCA/memory not broken. #102 tests may still pass with scheduled off.

---

## PR / commit sequence

| PR | Tasks | Ship criterion |
|----|-------|----------------|
| **A** | 1–3 | Policy + context + unit tests, **no network** |
| **B** | 4–6 | Fixtures + ingest + cycle + config off by default |
| **C** | 7–8 | Watchlist + staging soak |

---

## Risks & mitigations

| Risk | Mitigation |
|------|------------|
| CMC AI HTML changes | Fixtures + fail-open; classifiers on text not fragile DOM depth |
| Rate limits | `max_coins_per_cycle`, TTL cache per slug+endpoint, interval_sec |
| Spam events | Strong event_id idempotency; cap events per coin per cycle (e.g. 8) |
| False hard skip | Require impact ≤ −0.8 + type in hard set; shadow policy first |
| Ledger damage | Only `memory_*` writes; assert in store |
| Confusion with #102 | Config scheduled.enabled=false; this plan never enables it |

---

## Spec coverage check

| Spec requirement | Task |
|------------------|------|
| Schema / taxonomy | 1 |
| Policy fact_* | 2 |
| build_dca_context flags | 3 |
| CMC AI 3 endpoints | 4–5 |
| All coins A∪B capped | 5 universe |
| Persist memory_market_events | 5 |
| Optional RAG | 5 (mirror dca_decision) |
| /ask surface | 6 |
| Watchlist | 7 |
| Staging soak | 8 |
| Fail-open / no Grok hot path | 2–5 design |
| Not #102 | explicit out |

---

## Execution handoff

Plan saved to `plans/coin-fact-layer-d8-implementation.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — one task per subagent, review between tasks  
2. **Inline Execution** — same session, batch with checkpoints  

Say which you want when ready to implement (start with **PR A / Tasks 1–3**).
