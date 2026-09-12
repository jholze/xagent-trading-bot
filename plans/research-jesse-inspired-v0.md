# Research Sidecar — Jesse-inspired (v0)

> **Branch:** `docs/research-jesse-inspired-v0`  
> **Status:** Ideas / plan only — no runtime dependency on Jesse  
> **Source:** Review of [jesse-ai/jesse](https://github.com/jesse-ai/jesse) (2026-08-13)  
> **Audience:** Staging experiments (default + henry), exit policy, RelVol, correlated-tier  

---

## 1. Why this exists

Jesse is a strong **research / strategy-lab** framework (backtest, optimize, Monte Carlo, rule significance, metrics, charts). It is **not** a replacement for xagent:

| xagent owns | Jesse is good at |
|---|---|
| Multi-tenant paper ledger, risk, locks | Clean research loop |
| Gainer / RelVol / exit-radar / DE | Significance + Monte Carlo |
| Henry tenant configs, Telegram | Parameter search (Optuna-style) |
| Live/paper execution path | Unified metrics + no-lookahead discipline |

**Decision:** steal research *patterns*, implement as a thin `research/` sidecar inside this repo. **Do not** run Jesse in the bot hot-path or dual-write to Jesse’s DB.

---

## 2. Goals

1. **Kill weak ideas early** — entry rules must beat a null/bootstrap baseline before staging.
2. **Harden policy changes** — exit trails, RelVol caps, correlated-tier: Monte Carlo + fixed metrics before merge.
3. **One scorecard language** — same metrics for default/henry, experiments, and backtests.
4. **Keep the bot boring** — research reads candles + ledger; bot stays the execution SSOT.

Non-goals:

- Replace TradingService / DecisionEngine / exit-radar.
- Adopt Jesse strategy DSL (`should_long` / `go_long`).
- Futures/shorts-first workflow.
- Full Jesse dashboard/MCP in production.

---

## 3. What to take from Jesse (prioritized)

### P0 — Rule significance testing

**Jesse idea:** Compare a real entry rule vs bootstrap random entries on the same candle history.

**xagent mapping:**

| Rule family | Event stream | Null model |
|---|---|---|
| RelVol ignition | `gainer_relvol` fires / ledger buys | Random 1h bars with same cooldown |
| Gainer heat/rank | board nominations that DE-approved | Random symbols in top-N by vol |
| Sensor 15m | `entry_sensor_15m` fills | Shuffle entry times, keep holding rules |

**Deliverable:**

```text
research/
  significance/
    bootstrap.py      # pure: rule hits vs random
    report.py         # JSON + markdown summary
scripts/research_rule_significance.py
```

**Kill criteria for a rule:**

- Edge p-value (or bootstrap rank) not better than chance at agreed threshold (e.g. top 5% of null).
- Or expectancy ≤ 0 after fees/slippage assumptions.

**Hooks to existing work:**

- RelVol experiment tickets / ignition backtest 60d.
- Gainer-catch v1.
- Any new entry source before `mode=trade`.

---

### P0 — Monte Carlo stress

**Jesse idea:** Trade-order shuffle + candle-based noise to separate skill from luck and catch overfit.

**xagent mapping:**

1. **Trade shuffle** — take closed trades from `henry:demo` / `default:demo` (or backtest trade list), reshuffle order, recompute equity curve N times.
2. **Path noise** — optional: jitter entry/exit prices by bps or resample bars within band.
3. **Policy compare** — same trade set under trail 5% vs 3.5% / full_close_at_12 (correlated-tier) → distribution of ΔPnL.

**Deliverable:**

```text
research/
  monte_carlo/
    shuffle_trades.py
    equity.py
    report.py
scripts/research_monte_carlo.py
```

**Inputs:** ledger fills (`orders` blob / `orders_v2`) or offline JSON from existing backtests.

**Outputs:** median / p5 / p95 equity, maxDD distribution, “% of sims worse than live path”.

**Hooks:**

- `scripts/backtest_exit_policy_10d.py`
- `scripts/validate_correlated_tier_rotation.py` (when landed)
- Manual operator sells vs trail-only counterfactual

---

### P1 — Unified metrics scorecard

**Jesse idea:** One metrics system (Sharpe, Sortino, Serenity, expectancy, win rate, …).

**xagent extras we need** (Jesse does not emphasize these; we do):

| Metric | Why |
|---|---|
| **Peak capture %** | `(exit - entry) / (peak - entry)` — “how much of the runner did we keep?” |
| **Giveback from peak %** | Peak → exit drawdown on winners |
| **Time-in-winner hours** | Stagnant bags with +8% idle 48h |
| **Slot turnover** | Full closes / day when book near cap |
| **US-session giveback** | Stock-token PnL around US open (correlated dump) |

**Deliverable:**

```text
research/
  metrics/
    trade_metrics.py   # from fill list
    book_metrics.py    # from positions snapshot + peaks
    scorecard.py       # markdown/JSON for auswertungen/
```

**Standard report sections:**

1. Realized PnL / expectancy / win rate  
2. Peak capture & giveback  
3. By source (`gainer_relvol`, `manual`, `trailing_take_profit`, `bb_upper`, …)  
4. By tier / symbol class (stock-token vs crypto)  
5. Book health (open slots, stagnant winners)

---

### P1 — Parameter search (Optuna-style)

**Jesse idea:** Optimize mode with fitness (smart Sharpe / Sortino / Serenity).

**xagent mapping:** Search over *policy knobs*, not full strategy rewrites:

```text
us_stock.trail_pct ∈ [2.5, 5.5]
us_stock.arm_gain_pct ∈ [6, 12]
us_stock.full_close_gain_pct ∈ [10, 20]
relvol.mult ∈ [6, 15]
relvol.max_pct_24h ∈ [25, 50]
stagnant_gain_pct ∈ [6, 12]
stagnant_idle_hours ∈ [12, 48]
```

**Fitness (proposal):**

```text
fitness = 0.4 * normalized_pnl
        + 0.3 * peak_capture
        - 0.2 * max_dd
        - 0.1 * overtrade_penalty
```

**Constraint:** never optimize on the same window used for final kill decision (train/holdout split by calendar).

**Deliverable:** `scripts/research_optuna_exit_policy.py` wrapping existing exit/relvol backtest runners.

---

### P2 — No-lookahead discipline (checklist + tests)

**Jesse idea:** Multi-TF/multi-symbol without peeking future bars.

**xagent checklist** (encode as unit tests where possible):

- [ ] Indicators for decision at `t` use only candles with `close_time ≤ t`.
- [ ] RelVol 1h slice uses completed hour, not partial current hour as “closed”.
- [ ] Exit trail peak updates only with prints known at evaluation time.
- [ ] Backtests that join CMC/social features lag-align by publish time.

**Deliverable:** `research/LOOKAHEAD.md` + 2–3 pure tests on bar indexing.

---

### P2 — Research notebook / CLI loop

**Jesse idea:** Research API + Jupyter.

**xagent minimal loop:**

```bash
# 1) significance on RelVol fires last 60d
python3 scripts/research_rule_significance.py --source gainer_relvol --days 60

# 2) MC on henry closed trades last 30d
python3 scripts/research_monte_carlo.py --tenant henry --days 30 --sims 1000

# 3) scorecard
python3 scripts/research_scorecard.py --tenant henry --from 2026-08-01
```

Optional later: one notebook that only calls these scripts (no second SSOT).

---

### P3 — Optional steals (only if gap proven)

| Jesse feature | Steal only if… |
|---|---|
| Specific indicators (Wavetrend, Hurst, …) | DE explicitly lacks a signal we need |
| Interactive backtest charts | Exit policy reviews waste too much time in JSON |
| MCP server for research scripts | Agents repeatedly re-invent CLI flags |
| Partial-fill order model | Ladder/partial bugs keep recurring |

**Do not** adopt: live engine, exchange layer, strategy DSL, built-in editor.

---

## 4. Architecture (sidecar)

```text
┌─────────────────────────────────────────────┐
│  xagent runtime (unchanged hot-path)        │
│  bot · gainer-signal · exit-radar · risk    │
└──────────────────┬──────────────────────────┘
                   │ read-only
                   ▼
┌─────────────────────────────────────────────┐
│  research/  (offline / cron / operator)     │
│  candles · ledger fills · position peaks    │
│  significance · monte_carlo · metrics       │
│  → auswertungen/research_*.json|.md         │
└─────────────────────────────────────────────┘
```

**Data sources (read-only):**

- Mongo `xagent_test`: `orders` (`henry:demo`, `default:demo`), `orders_v2`, `positions`
- Existing OHLCV helpers (`historical_prices`, Gate REST already used in scripts)
- Optional: `auswertungen/` JSON from prior backtests as frozen fixtures

**Fail-open:** research never blocks bot start; missing Mongo → clear error and exit 2.

---

## 5. Mapping to current experiments

| Current work | How research v0 helps |
|---|---|
| RelVol trade mode (staging) | Significance on fires; scorecard by `source=gainer_relvol` |
| Manual >10% Henry stock cash | Counterfactual: trail-only vs cash-at-10%; peak capture |
| `experiment/correlated-tier-rotation-v0` | MC + metrics before `enabled=true`; us_stock trail 3.5 vs 5 |
| Book full / max_open | Slot turnover + stagnant metrics |
| BLESS / long losers | Giveback + time underwater stats |

---

## 6. Phased delivery

### Phase A — Foundations (1–2 PR)

- [ ] `research/` package skeleton + README
- [ ] `metrics/scorecard.py` from ledger fills + optional positions
- [ ] CLI: `scripts/research_scorecard.py --tenant henry`
- [ ] Write sample to `auswertungen/research_scorecard_henry_<date>.json`

**Done when:** one command produces peak-capture + by-source PnL for henry last 14d.

### Phase B — Significance + Monte Carlo (1–2 PR)

- [ ] Bootstrap significance for RelVol-style event lists
- [ ] Trade-shuffle MC on closed trades
- [ ] Unit tests pure (no network)
- [ ] Document kill thresholds in this file (update after first real run)

**Done when:** RelVol 60d events get a significance report; Henry 30d trades get MC band.

### Phase C — Optuna exit policy (optional)

- [ ] Wire Optuna to exit-policy / correlated-tier parameter grid
- [ ] Train/holdout split by date
- [ ] Output recommended config patch (not auto-apply to Mongo)

**Done when:** recommended `us_stock.trail_pct` with holdout metrics beats baseline.

### Phase D — Polish

- [ ] Lookahead checklist tests
- [ ] Optional notebook
- [ ] Optional MCP wrapper (only if agents use it weekly)

---

## 7. Kill / success criteria for *this research program*

**Keep investing if:**

- ≥1 staging experiment killed *before* deploy by significance/MC that would have lost money, **or**
- ≥1 policy change improved peak-capture without collapsing total PnL (measured on holdout).

**Stop / shrink if:**

- Scorecards unused for 4+ weeks of experiments, **or**
- Maintenance cost exceeds value of gut-driven staging (honest call).

---

## 8. Explicit non-goals recap

- No Jesse pip dependency in bot image.
- No second execution ledger.
- No automatic config apply from optimize (always human + backup for Henry Mongo).
- No rewrite of DE/strategies into Jesse classes.

---

## 9. Suggested first concrete tickets

1. **`research-scorecard-v0`** — metrics + CLI + henry/default 14d sample  
2. **`research-monte-carlo-v0`** — shuffle closed trades, equity bands  
3. **`research-relvol-significance-v0`** — bootstrap on RelVol events from gainer-signal/ledger  
4. **(after correlated-tier lands)** MC compare trail 5 vs 3.5 on us_stock path  

---

## 10. References

- [jesse-ai/jesse](https://github.com/jesse-ai/jesse) — features list (backtest, optimize, Monte Carlo, rule significance, metrics, ML, MCP)
- Internal: RelVol consume fixes (#245–#248), volume ignition backtest, exit policy 10d, correlated-tier worktree
- Related plans: `plans/ticket-experiment-gainer-relvol-shadow-v0.md`, `plans/ticket-experiment-gainer-catch-v1.md`, `plans/BACKTEST-ANLEITUNG-volume-ignition.md`

---

## 11. Open questions

1. Holdout policy: calendar split (e.g. last 7d always holdout) or walk-forward?
2. Fees/slippage assumptions for paper (0 vs 10 bps)?
3. Should scorecard run as daily Railway cron (`xagent-gis-monitor`-style) or operator-only?
4. Peak data: trust `positions.recent_high` only, or rebuild peaks from OHLCV for closed trades?

---

*v0 — ideas only. Implementation starts with Phase A when approved.*
