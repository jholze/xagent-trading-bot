# Gainer entry policy backtest (30d)

Generated: `2026-08-05T17:30:51.327980+00:00`

## Method (read limitations)

- Rank: **previous UTC day** return on liquid Gate USDT spot
- Entry: open of decision day; exits **+6h / +24h / +48h**; fee 0.2% RT
- Caps: max 3 open, max 6/day
- Universe n=80, min_vol=500000.0

**Limitations:** current liquid universe (survivorship); current vol proxy; no WS intraday; fixed horizon ≠ trail; no legacy balloon.

## 24h horizon comparison

| Policy | Trades | Win rate | Median PnL % | Avg PnL % | Sum PnL % | Median entry 24h% | Entries >40% | Entries >50% |
|--------|--------|----------|--------------|-----------|-----------|-------------------|--------------|--------------|
| fixed_v0 | 43 | 0.5116 | 0.0332 | -1.2887 | -55.412 | 19.7142 | 0.0 | 0.0 |
| coin_aware_v1 | 43 | 0.5349 | 0.1995 | 1.9136 | 82.2864 | 16.0465 | 0.0 | 0.0 |

## 6h / 48h

### fixed_v0
- 6h: `{"n": 43, "win_rate": 0.3953, "avg_pnl_pct": -0.5717, "median_pnl_pct": -1.1731, "sum_pnl_pct": -24.5837, "median_entry_pct_24h": 19.7142, "pct_entry_over_40": 0.0, "pct_entry_over_50": 0.0}`
- 48h: `{"n": 43, "win_rate": 0.4651, "avg_pnl_pct": 0.9615, "median_pnl_pct": -3.6168, "sum_pnl_pct": 41.344, "median_entry_pct_24h": 19.7142, "pct_entry_over_40": 0.0, "pct_entry_over_50": 0.0}`

### coin_aware_v1
- 6h: `{"n": 43, "win_rate": 0.4419, "avg_pnl_pct": 0.9454, "median_pnl_pct": -0.7102, "sum_pnl_pct": 40.6502, "median_entry_pct_24h": 16.0465, "pct_entry_over_40": 0.0, "pct_entry_over_50": 0.0}`
- 48h: `{"n": 43, "win_rate": 0.4651, "avg_pnl_pct": 6.4812, "median_pnl_pct": -0.8022, "sum_pnl_pct": 278.6906, "median_entry_pct_24h": 16.0465, "pct_entry_over_40": 0.0, "pct_entry_over_50": 0.0}`

## Validity read

- Prefer **coin_aware** if: lower median entry extension AND not much worse (or better) median/avg 24h PnL, and enough trades (n not ~0).
- Prefer **fixed_v0** if: coin_aware starves (n very low) or worse expectancy with no FOMO reduction.
- Neither is live truth without trail exits + dual stack.

Full JSON: `2026-08-05_gainer_entry_policy_30d.json`
