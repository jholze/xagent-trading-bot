# Grid Mode Roadmap (A → B → C)

Goal: real grid *behavior* by coin and market regime → more rotation, better handling in every regime.
Roll out only after local history backtests pass and explicit go-ahead.

## Phase A — Behavior (this PR track)

1. **Trading mode** from Regime + Allocator: `GRID` | `MOMENTUM` | `HYBRID` | `DEFENSIVE`.
2. **Grid plan** (levels + buy/sell *slices*), pure logic, no exchange limits required.
3. **GridStrategy** uses the plan (partial sells, sized buys, re-center).
4. **Local backtest** (`scripts/backtest_grid_plan.py`) on synthetic + optional OHLCV history.
5. Unit tests for plan + mode resolution.

## Phase B — Quality (uses existing volatile/stable split)

Your bot already classifies coins via `intelligence/volatility_classifier.py`
(`stable` | `volatile`) and `classify_coin` (`meme` | `mid_cap` | `large_cap`).
Phase B **reuses that** instead of inventing a second taxonomy.

1. **Spacing by tier** — wider grid for volatile/meme, tighter for stable/large_cap  
   (`spacing_atr_mult_for_coin`, config keys under `grid.*_spacing_atr_mult`).
2. **Mode bias by tier** — volatile + mixed weights → HYBRID (keep entry spikes);  
   stable + grid-dominant → pure GRID.
3. **Regime flip** — DEFENSIVE + open position → `SELL_PARTIAL_50` (reduce inventory).
4. **Entry-sensor in GRID** — buy becomes a *slice* (`dca_usdt`), not full size / BUY_STRONG.
5. **Persist plan per tenant in Mongo** — collection `grid_plans` via
   `storage/grid_plan_store.py` (legacy `config.grid_states` still mirrored for `/grid`).

## Phase C — Exchange limits (optional / local first)

1. **Fee-aware min spacing** — `enforce_fee_spacing` / `fee_aware_min_spacing`.
2. **Limit specs from plan** — `plan_to_limit_specs` (no Gate yet).
3. **Shadow book** — `GridLimitShadowBook` + `simulate_limit_grid_path` for local backtests
   (`scripts/backtest_grid_plan.py --limits`).
4. Live multi-limit Gate executor — **not wired**; remains Protocol only until rollout.

## Success metrics (local backtest)

- Ranging series: more round-trips than buy-hold, lower max drawdown than full momentum.
- Strong trend series: mode stays MOMENTUM / low grid churn.
- Defensive series: near-zero new buys.
