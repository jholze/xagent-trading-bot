# Long-buy membership (#631)

Revise the Ist long-buy membership stack. Do not remove it and do not open the universe.

Ersatz-Cap (exactly one): `universe.membership_revise.ersatz_cap_trade_max`

Flag: `universe.membership_revise.mode` = `off` | `shadow` | `enforce`

Shipped default is `shadow`. Shadow does not change who is trade-eligible. `enforce` applies staged `staged_trade_max_coins` / `staged_trade_rank_by` clamped to the Ersatz-Cap. Config save and `load_trade_universe` refuse `shadow` or `enforce` when that cap is missing, non-positive, or above 200.

`GAINER_RELVOL` / source `gainer_relvol` stays exempt from `universe_trade_cap`. This ticket does not change that exemption. `fire_enabled` and `allow_live` stay false. Venue liquidity and long mcap stay on the risk path.

## Ist layers and #628 codes

| Layer | Where | Knobs | #628 code |
|---|---|---|---|
| Universe split observe/trade | `services.universe.split`, `risk.risk_manager` | `split_enabled`, `trade_max_coins`, `trade_rank_by` | `universe_trade_cap` |
| Gainer expand inject | `services.gainer_universe.inject` | `expand_inject_max`, `trade_max_with_expand`, `mode=trade_expand` | none (still outside the trade set → `universe_trade_cap` at the split gate) |
| Chase guard | `services.gainer_universe.chase_guard`, `risk.risk_manager` | `chase_guard_enabled`, `chase_max_gain_from_prev_close_pct`, `chase_guard_sources` | `gainer_chase_guard` |
| WQE soft ranking | `services.watchlist_quality` soft/enforce | `watchlist_quality.mode`, `min_buy_score`, `ai.sort_by` | `watchlist_quality` |
| CMC via WQE | `services.watchlist_quality.universe` | `rank_cmc_candidates_by_wqe`, `cmc_only_buy_allowed` | none (WQE soft/enforce reject emits `watchlist_quality`) |

`watchlist_quality.mode=shadow` scores only and does not emit `watchlist_quality`.

## Revise path

Only the split trade-max / ranking knob is revised, and only when `mode=enforce`.

* Effective trade max = `min(staged_trade_max_coins or ist trade_max, ersatz_cap_trade_max)`.
* Enforce never passes a non-positive max into the selector (that selector treats `<= 0` as unlimited).
* After gainer expand and core-seed finalize, non-forced names are trimmed so discovery cannot exceed the cap. Open positions and base coins (when `trade_include_base`) keep the existing overflow rule: they stay even if they already fill the cap, and then no extra discovery names are added.

U4 (tape counts before vs after a flag-on window) is a post-merge soak. Do not invent miss rates.
