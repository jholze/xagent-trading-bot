# #616 Walk-Forward Artefakt — gemessen

**Freeze-ID:** `FBR-v1.1-watchlist-616`
**WF_INCOMPLETE:** `false`
**shorts.allow_live:** false (not modified). Paper/research only.

## Data window

Target ledger 2026-07-01→2026-09-28 is not in the loaded fills. Canonical OOS windows with ≥1 exit: ['F4']. Folds adapted to the largest continuous real-fill window (equal UTC slices, expanding train prefix unused for parameters).

- Requested (--end inclusive): 2026-07-01T00:00:00+00:00 → 2026-09-28T00:00:00+00:00 UTC
- Used: 2026-09-20T00:00:00+00:00 → 2026-09-30T00:00:00+00:00 UTC
- Folds adapted: True
- Canonical OOS windows with ≥1 exit: ['F4']
- Long full exits in sample: 68
- Allowlisted for Arm A (`AUTO_SOURCES`): 6

| Fold | Train (UTC) | OOS (UTC) |
|------|-------------|-----------|
| F1 | 2026-09-20T00:00:00+00:00 → 2026-09-20T00:00:00+00:00 | 2026-09-20T00:00:00+00:00 → 2026-09-23T00:00:00+00:00 |
| F2 | 2026-09-20T00:00:00+00:00 → 2026-09-23T00:00:00+00:00 | 2026-09-23T00:00:00+00:00 → 2026-09-26T00:00:00+00:00 |
| F3 | 2026-09-20T00:00:00+00:00 → 2026-09-26T00:00:00+00:00 | 2026-09-26T00:00:00+00:00 → 2026-09-30T00:00:00+00:00 |

## 5a OOS

| Fold | Arm | n | PF | MaxDD | Winrate | Stop% | expired_wo_open | sum_pnl |
|------|-----|---|----|-------|---------|-------|-----------------|---------|
| F1 | A | 1 | 0.000 | 16.26 | 0.000 | 0.000 | — | -16.26 |
| F1 | B0 | 0 | n/a | 0.00 | n/a | n/a | 18 | 0.00 |
| F1 | B1 | 0 | n/a | 0.00 | n/a | n/a | 20 | 0.00 |
| F1 | C | 0 | n/a | 0.00 | n/a | n/a | — | 0.00 |
| F2 | A | 2 | 2.986 | 1.68 | 0.500 | 0.000 | — | 3.34 |
| F2 | B0 | 0 | n/a | 0.00 | n/a | n/a | 20 | 0.00 |
| F2 | B1 | 0 | n/a | 0.00 | n/a | n/a | 24 | 0.00 |
| F2 | C | 0 | n/a | 0.00 | n/a | n/a | — | 0.00 |
| F3 | A | 3 | inf | 0.00 | 1.000 | 0.000 | — | 27.37 |
| F3 | B0 | 0 | n/a | 0.00 | n/a | n/a | 7 | 0.00 |
| F3 | B1 | 0 | n/a | 0.00 | n/a | n/a | 10 | 0.00 |
| F3 | C | 0 | n/a | 0.00 | n/a | n/a | — | 0.00 |
| Agg OOS | A | 6 | 1.806 | 17.94 | 0.667 | 0.000 | — | 14.45 |
| Agg OOS | B0 | 0 | n/a | 0.00 | n/a | n/a | 45 | 0.00 |
| Agg OOS | B1 | 0 | n/a | 0.00 | n/a | n/a | 54 | 0.00 |
| Agg OOS | C | 0 | n/a | 0.00 | n/a | n/a | — | 0.00 |

PF / Winrate / Stop% are on net short PnL after 7.5 bp/side and 0.01%/8h funding.
MaxDD is the peak-to-trough of the fold's short equity (start 0), in USDT.
Agg OOS pools closed shorts whose **entry decision** (the long exit) falls in an OOS fold.

## Soft gates

```json
{
  "B0": {
    "folds": [
      {
        "fold": "F1",
        "state": "empty"
      },
      {
        "fold": "F2",
        "state": "empty"
      },
      {
        "fold": "F3",
        "state": "empty"
      }
    ],
    "not_all_folds_fail_literal": true,
    "any_traded_fold_nonnegative": false,
    "b_vs_a_pass": false,
    "b_vs_a_detail": {
      "sum_pnl_b": 0.0,
      "sum_pnl_a": 14.452425736349449,
      "sum_pnl_ge": false,
      "pf_b": null,
      "pf_a": 1.80571,
      "pf_ge": false,
      "n_b": 0,
      "n_required": 10
    },
    "b_vs_c": {
      "pass": false,
      "reason": "n<10; Go-P2 soft-gate not applicable and not passed"
    },
    "soft_gates_pass": false
  },
  "B1": {
    "folds": [
      {
        "fold": "F1",
        "state": "empty"
      },
      {
        "fold": "F2",
        "state": "empty"
      },
      {
        "fold": "F3",
        "state": "empty"
      }
    ],
    "not_all_folds_fail_literal": true,
    "any_traded_fold_nonnegative": false,
    "b_vs_a_pass": false,
    "b_vs_a_detail": {
      "sum_pnl_b": 0.0,
      "sum_pnl_a": 14.452425736349449,
      "sum_pnl_ge": false,
      "pf_b": null,
      "pf_a": 1.80571,
      "pf_ge": false,
      "n_b": 0,
      "n_required": 10
    },
    "b_vs_c": {
      "pass": false,
      "reason": "n<10; Go-P2 soft-gate not applicable and not passed"
    },
    "soft_gates_pass": false
  }
}
```

**Recommendation:** keep veto:lena

## Assumptions

- Event sample is filled SELL_FULL orders from the files passed to --orders, deduped by id. Partials are excluded.
- Demo-scope ledgers under data/ are not mixed in unless their path is passed explicitly. They have no exit_source, a different ledger_scope, and a gap after 2026-07-07.
- Tenant default MCP list_orders_public caps at 200 most recent fills (services/mcp/explain.py MAX_LIMIT). A 2160h query on 2026-09-29 still returned 2026-09-20→2026-09-29 only.
- No historical desk-regime tape (RISK_OFF / HARVEST) is in the repo or the order export. regime_at() is None on every bar. Arm B gates fail closed (G1_regime_missing). Regime was not inferred from price.
- No historical market-cap tape. mcap_of() is None. Arm B also fails G7_mcap_unknown. Arm A is not dropped for missing mcap; the exits are the operator's real fills. Universe mcap≥100M could not be applied.
- No pair-map deny list is applied (none found in repo). Lock flags are absent from the export; the lock gate is not tripped because no lock flag is present.
- SELL_FULL is treated as a flat book on that symbol for the counterfactual. Other-timeframe residual longs are not in the export.
- Arm A allowlist is strategies.short_policy.AUTO_SOURCES on this checkout: ['rsi_sell', 'exit_1h_rsi_rollover', 'oracle_climax_harvest', 'exit_volume_climax']. Size is auto_short_notional_usdt (0.35×sell, cap max_usdt_per_trade). Tier is volatile because orders carry no strategy_tier, so evaluate_short_cover time_cap is 4h.
- Arm A cover is evaluate_short_cover on 1h OHLC: high is tested first for liq/stop; then low updates recent_low; close is tested for trail, RSI, and time. Stop/liq fill at the threshold (or at the open if the bar gaps through). Trail/RSI/time fill at the bar close. This is not a tick replay.
- Arm B regime_allow for this run is the original #616 body ['RISK_OFF','HARVEST'], not the later RISK_OFF-only bump. The bump does not matter while regime is missing.
- Arm B cover (if a gate ever passes) is liq, then max_loss 80 USDT, then k×ATR (1.5 large / 2.0 mid), then time 24h/12h. Viktor daily cap 200 is implemented. No B trade opened on this run.
- Fees 7.5bp per side and funding 0.01% per 8h on entry notional are applied to short PnL. CostModel network fee fetch is disabled (fee_source=freeze_7_5bp).
- expired_wo_open counts watchlist entries whose 48h TTL elapses at or before the last closed 1h bar without an open. Entries whose TTL is after that bar are censored, not expired.
- max_entries=40 is the concurrent watching cap. A slot frees when that entry's TTL elapses. It is not a lifetime cap of 40.
- Arm C contributes no short. n=0 and sum_pnl=0 on every row.
- Train slices are not used to choose parameters. Freeze numbers are the constants in this script.

## Repro

```
python scripts/wf_watchlist_abc.py --start 2026-07-01 --end 2026-09-29 --freeze FBR-v1.1-watchlist-616 --arms A,B0,B1,C --orders artifacts/wf_watchlist_abc_20260929/exits_used.json --out artifacts/wf_watchlist_abc_20260929
```

