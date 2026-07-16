# Grid Mode Roadmap (A → B → C)

Goal: real grid *behavior* by coin and market regime → more rotation, better handling in every regime.
Roll out only after local history backtests pass and explicit go-ahead.

## Phase A — Behavior (this PR track)

1. **Trading mode** from Regime + Allocator: `GRID` | `MOMENTUM` | `HYBRID` | `DEFENSIVE`.
2. **Grid plan** (levels + buy/sell *slices*), pure logic, no exchange limits required.
3. **GridStrategy** uses the plan (partial sells, sized buys, re-center).
4. **Local backtest** (`scripts/backtest_grid_plan.py`) on synthetic + optional OHLCV history.
5. Unit tests for plan + mode resolution.

## Phase B — Quality

1. Coin-class spacing (volatile / stable / meme).
2. Regime flip: GRID → DEFENSIVE forces controlled exit / pause new grid buys.
3. Persist plan state per tenant in Mongo (not only `config.grid_states`).
4. Entry-sensor interaction: in GRID mode spike = stronger buy *slice*, not full momentum size.

## Phase C — Exchange limits (optional)

1. Optional limit orders per level when live + `use_limit_orders`.
2. Fill tracking / cancel-replace on re-center.
3. Fee-aware min distance between levels.

## Success metrics (local backtest)

- Ranging series: more round-trips than buy-hold, lower max drawdown than full momentum.
- Strong trend series: mode stays MOMENTUM / low grid churn.
- Defensive series: near-zero new buys.
