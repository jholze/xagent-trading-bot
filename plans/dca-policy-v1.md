# DCA Policy v1 (freeze) — GitHub #95 / D1

> **Status:** FREEZE 2026-07-19  
> **Epic:** [#79](https://github.com/jholze/xagent-trading-bot/issues/79)  
> **Tickets:** D1 #95 · D2 #96 · D3 #97  
> **Depends:** Adaptive cash #90 (shipped), RAG #72 (consume only)

## 1. Principles

1. **Policy-first** — pure functions; unit-testable; no I/O inside `evaluate_dca_policy`
2. **Hard gates first** — existing `should_dca` / scoring run **before** policy
3. **Skip beats size** — if `skip=True`, no candidate (unless shadow mode)
4. **Fail-open** — missing fusion/memory/RAG → `size_mult=1.0`, no skip from missing data
5. **No Grok** in hot path — LLM only Reflect / `/ask`
6. **No order writes** in policy/context modules
7. **Cash coupling** — respect `cash_mode` and cap by `spendable_dca` when known

## 2. Types

### `DcaContext` (inputs after hard gates passed)

| Field | Type | Source (D2) |
|-------|------|-------------|
| `symbol` | str | position / market |
| `cash_mode` | `DEPLOY`\|`STEADY`\|`HARVEST`\|`""` | cash_policy / fusion |
| `fusion_size_mult` | float | `get_global_market_bias` (default 1.0) |
| `block_buys` | bool | fusion |
| `drawdown_active` | bool | risk throttle |
| `spendable_dca` | float \| None | cash policy (None = unknown) |
| `calendar_high_impact` | bool | macro / fail-open false |
| `session_low_liquidity` | bool | macro / fail-open false |
| `score` | int | from `DCADecision` |
| `max_score` | int | from decision |
| `loss_pct` | float | unrealized % (negative = loss) |
| `size_bias` | float | CoinProfile (default 1.0) |
| `entry_bias` | str | CoinProfile (`neutral`/`soft_block`/`prefer`) |
| `extreme_funding` | bool | tech flags / fail-open false |
| `rag_hit_count` | int | optional retrieve (info only, not decide) |

### `DcaPolicyResult` (outputs)

| Field | Type | Meaning |
|-------|------|---------|
| `size_mult` | float | multiply base DCA usdt; clamped `[0, max_policy_mult]` |
| `skip` | bool | drop candidate if not shadow |
| `reason_codes` | list[str] | audit trail e.g. `harvest_skip`, `deploy_boost` |
| `policy_version` | str | `"1"` |

### Bridge to DecisionPacket (#82 later)

```text
AgentStance(
  agent="dca",
  stance="buy" if not skip else "skip",
  size_hint=size_mult,  # mult semantics documented here
  confidence=...,
  reasons=reason_codes,
  veto=skip,
  veto_code=reason_codes[0] if skip else null,
)
```

## 3. Factor table v1 (defaults)

Apply in order; **last skip wins**; mults **multiply** then clamp.

| # | Condition | Effect | reason_code |
|---|-----------|--------|-------------|
| 1 | `cash_mode==HARVEST` OR `block_buys` OR `fusion_size_mult < 0.7` | `skip=True` **or** mult `*=0.4` if `harvest_soft=true` | `harvest` / `block_buys` / `low_size_mult` |
| 2 | `cash_mode==DEPLOY` OR `fusion_size_mult >= 1.0` | mult `*=1.35` | `deploy_boost` |
| 3 | `cash_mode==STEADY` or empty | mult `*=1.0` | `steady` |
| 4 | `calendar_high_impact` | mult `*=0.5`; if mult&lt;0.35 → skip | `calendar` |
| 5 | `session_low_liquidity` | mult `*=0.7` | `session` |
| 6 | `drawdown_active` | mult `*=0.5` | `drawdown` |
| 7 | `extreme_funding` | skip | `funding` |
| 8 | `entry_bias==soft_block` | mult `*=0.6` (not hard skip — recovery) | `profile_soft_block` |
| 9 | `size_bias < 0.75` | mult `*= max(0.5, size_bias)` | `size_bias` |
| 10 | `score >= 0.8 * max_score` and not HARVEST | mult `*=1.25` | `score_boost` |
| 11 | missing fusion (size_mult defaulted) | no change | `fail_open_fusion` |

**Config defaults:**

```json
"dca": {
  "policy": {
    "enabled": true,
    "shadow": true,
    "policy_version": "1",
    "max_policy_mult": 2.0,
    "harvest_mode": "skip",
    "deploy_mult": 1.35,
    "harvest_mult": 0.4,
    "calendar_mult": 0.5,
    "session_mult": 0.7,
    "drawdown_mult": 0.5,
    "score_boost_mult": 1.25,
    "score_boost_ratio": 0.8,
    "soft_block_mult": 0.6
  }
}
```

- `harvest_mode`: `"skip"` | `"soft"` (soft = mult only)
- `shadow: true` → never drop candidate; append reasons to rationale; keep original usdt
- Cap usdt by `spendable_dca` when not null and not shadow-live: `usdt = min(usdt, spendable_dca)` after mult

## 4. Pipeline (D3)

```text
should_dca / scoring  →  if fail: None
build_dca_context     →  DcaContext (fail-open)
evaluate_dca_policy   →  DcaPolicyResult
if skip and not shadow → None
usdt = decision.usdt_amount * size_mult  (if not shadow)
usdt = min(usdt, spendable_dca) if spendable known and not shadow
rationale += policy reasons
→ DCACandidate
```

## 5. Non-goals v1

- Grok in evaluate
- Scheduled Monday DCA (D7)
- soft_block hard-kills DCA
- Writing orders from policy
- Bus publish (optional later)

## 6. Acceptance D1

- [x] This doc frozen and linked from #95 / epic-dca-agent.md
- [x] Types + factor table + config keys reviewable without code dive
- [x] Mapping to cash_mode and DecisionPacket noted

## 7. Implementation tickets

| D2 #96 | `build_dca_context` — done |
| D3 #97 | wire + pure `evaluate_dca_policy` + tests — done |
| D6 #101 | Observability — done |
| D4b #99 | LIVE_DCA_POLICY in /ask — done |
| D4 #98 | `persist_dca_decision_event` → `memory_market_events` + optional RAG — done |

### D4 persist

- On every policy audit (`emit_dca_policy_audit`): write `MarketEvent` `event_type=dca_decision`
- Config: `persist_events` (default true), `index_rag` (default true, fail-open)
- Includes shadow decisions so staging learns without live size changes


### D6 config

```json
"log_audit": true,
"telegram_audit": false,
"telegram_on_skip_only": true
```

Log line example:

```text
DCA policy ZBT/USDT: shadow mode=HARVEST fusion_sm=0.50 mult=1.0 skip=True reasons=[harvest_skip] usdt=500->500 spendable_dca=800 v1
```
