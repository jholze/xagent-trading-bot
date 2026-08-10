# Ticket: Staging experiment grid-share / hold / MFE v1

| | |
|--|--|
| **Typ** | Config experiment (staging-first) |
| **Branch** | `experiment/grid-share-hold-v1` |
| **Priorität** | Hoch (Opportunity-Cost vs GIS leaders) |
| **Scope** | **default** (`config.json`) + **henry** (Mongo `tenant_configs`) |
| **Horizon** | 3–5 Tage messen, dann keep / rollback / tune |
| **Status** | In progress |

## Problem

Grid-Audit (default, staging Mongo, ~10d):

| Metric | Baseline |
|--------|----------|
| Grid-Buy-Share | ~60 % overall, **82–92 %** recent days |
| Median hold (pair) | ~**3.5 h** |
| Under 2 h | ~**42 %** |
| Post-exit MFE ≥3 % | **30/139** pairs (TAKE/TUT/H/BEAT/WLD…) |
| Dominant exits | `bb_upper` / `technical` + **full close** |

Kapital und Attention in Grid-Scalps statt Momentum/Gainer-Leadern.

## Hebel (Config only)

| # | Knob | Before | Experiment | Why |
|---|------|--------|------------|-----|
| 1 | `strategy_allocator.default_grid_weight` | 0.6 | **0.4** | RANGING+neutral: less pure GRID |
| 1b | `default_momentum_weight` | 0.4 | **0.6** | Symmetric |
| 2 | pure_grid “enger” | — | **via weights** | `resolve_trading_mode`: stable + g=0.4 → **HYBRID** (not pure GRID). Config `pure_grid_*` is not runtime-wired today. |
| 3 | `sell_policy.rotation.prefer_full_close` | true | **false** | Stop upgrading every profit path to SELL_FULL |
| 3b | `grid_profit_full_close` | true | **false** | Grid level harvests can leave tails |
| 3c | `profit_exit_full_close` | true | **true (keep)** | Non-grid profit full close still OK |
| 4 | BB / grid min gain | already 2 % / 1 % on default | **henry align** `grid.sell_policy.min_sell_gain_pct` 0→**1.0** | Henry body was stale |
| 5 | Gainer slots | `scan_prefer_gainer=true` | **unchanged** | Already on; re-check after share drops |

## Tenants

| Tenant | Config path | Action |
|--------|-------------|--------|
| **default** | Service `config.json` (no `tenant_configs` row) | Deploy this branch |
| **henry** | Mongo `tenant_configs.body` deep-merge | Patch same knobs after/at deploy |

## Success metrics (3–5d, both tenants)

Compare to baseline window (or last 7d pre-deploy):

1. **Grid-Buy-Share** (fills `source=grid` / all buys) → target **&lt; 55 %** days average  
2. **Median hold hours** (buy→sell pair) → target **&gt; 5 h**  
3. **MFE ≥3 % after exit** rate among closed pairs → **not worse**; ideally down (fewer early exits of runners)  
4. **GIS overlap** (qualitative): fewer obvious missed leaders while capital stuck in grid  
5. **PnL / fees**: no large fee blow-up; grid realized not collapse without offsetting momentum

### Kill / rollback

If after ≥3 full days:

- Grid share still &gt;75 % **and** median hold still &lt;3.5 h → escalate (code path / risk-off), or  
- Realized PnL clearly worse without better open MFE → **rollback knobs**

```json
"strategy_allocator.default_grid_weight": 0.6
"strategy_allocator.default_momentum_weight": 0.4
"sell_policy.rotation.prefer_full_close": true
"sell_policy.rotation.grid_profit_full_close": true
```

Henry: restore from backup doc or reverse patch.

## Non-goals

- Grid globally off  
- Sniper size changes  
- Santiment fusion changes  
- Production (main) deploy  

## Acceptance

- [x] Ticket + branch  
- [ ] `config.json` + `core/config.py` defaults  
- [ ] Unit: allocator ranging weights + sell_rotation still green  
- [ ] Henry Mongo patched (same knobs + min_sell_gain 1.0)  
- [ ] PR → staging deploy  
- [ ] Day-0 note: deploy timestamp + baseline snapshot  

## Ops notes

- Measure via Mongo `orders_v2` (`tenant_id` in {`default`,`henry`}), source/exit fields as in grid audit.  
- Prefer script re-run of audit query rather than ad-hoc Telegram anecdotes.
