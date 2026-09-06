# Hermes Recost — CostModel `2026-09-v1` vs. Legacy

**Stand:** 2026-09-05 · **Ticket:** #316 · **CostModel:** `2026-09-v1` · **Legacy:** `legacy`

Read-only Snapshot der Operator-Hermes-Memory. Jedes Experiment wurde auf dem **gleichen** Walk-Forward-Fenster wie `HermesAgent.run_cycle` (`created_at` − `14`d, `fold_days=3`, `step_days=3`) mit `hermes/validation.py` + CostModel-fähigem `hermes/backtester.py` (`backtest_mode=ta_only`) neu bewertet. Keine Live-Orders, keine Promotion, keine Schreibzugriffe auf den Input oder `hermes/memory/`.

- Input: `/private/tmp/claude-502/-Users-jholze-Documents-scripts-trading-bot-worktrees-staging-review-septemper/b92b3f7b-894d-460c-ad10-cf4007b691f4/scratchpad/hermes-memory-snapshot`
- Getaggte Kopie (`cost_model: legacy`): `/private/tmp/hermes-recost-tagged`
- Laufzeit: **172.8s**

## Gesamt

| Kennzahl | Wert |
| --- | ---: |
| Experimente bewertet | 465 |
| Filter übersprungen (Limit/Symbols) | 0 |
| Alt: rejected | 465 |
| Alt: promoted | 0 |
| Neu: promoted (`2026-09-v1`) | 0 |
| **rejected → promoted (Flips)** | 0 |
| Unverändert (gleiches Verdict) | 460 |
| Weiter rejected | 460 |
| unresolvable (keine OHLCV / keine Folds) | 5 |

## Flips rejected → promoted nach Variable

Keine Flips.

## Flips nach Symbol

Keine Flips.

## Flips nach Quelle (grok / heuristic)

Keine Flips.

## Die 10 größten realized_pnl-Deltas

Delta = `variant.realized_pnl` (CostModel `2026-09-v1`) − `variant.realized_pnl` (legacy, gespeichert im Experiment).

| id | symbol | variable | old variant pnl | new variant pnl | Δ pnl | old trades | new trades | verdict alt→neu |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| exp_230af54b | ARIA/USDT | take_profit_pct | 0.00 | 0.00 | +0.00 | 0 | 0 | rejected → rejected |
| exp_2c003591 | ARIA/USDT | rsi_buy_high | 0.00 | 0.00 | +0.00 | 0 | 0 | rejected → rejected |
| exp_d43768b1 | ETH/USDT | volume_multiplier | 0.00 | 0.00 | +0.00 | 0 | 0 | rejected → rejected |
| exp_4cab18c3 | TRX/USDT | buy_regime | 0.00 | 0.00 | +0.00 | 0 | 0 | rejected → rejected |
| exp_1a1f4c0d | GMRT/USDT | stop_loss_pct | 0.00 | 0.00 | +0.00 | 0 | 0 | rejected → rejected |
| exp_0d306c25 | TREE/USDT | rsi_buy_low | 0.00 | 0.00 | +0.00 | 0 | 0 | rejected → rejected |
| exp_5224c622 | ARIA/USDT | rsi_sell_30 | 0.00 | 0.00 | +0.00 | 0 | 0 | rejected → rejected |
| exp_9dd369e9 | ETH/USDT | reversal_rsi_cross_low | 0.00 | 0.00 | +0.00 | 0 | 0 | rejected → rejected |
| exp_e9d262f5 | ADA/USDT | reversal_volume_multiplier | 0.00 | 0.00 | +0.00 | 0 | 0 | rejected → rejected |
| exp_d4b3e833 | TRX/USDT | take_profit_pct | 0.00 | 0.00 | +0.00 | 0 | 0 | rejected → rejected |

## Was das für die Baseline bedeutet

Kein rejected→promoted-Flip. Würde man die Varianten anwenden, änderte sich **kein** Wert in `baseline.json` / `baseline.demo.json`. Hermes bleibt aus, bis dieser Bericht gelesen ist (#310).

### Befund: Walk-Forward hat nie gehandelt

Alle bewerteten Experimente haben `trades = 0` sowohl in den gespeicherten Legacy-Metriken als auch unter `2026-09-v1`. Die Fold-Fenster (`fold_days=3`, `backtest_days=14`) liefern auf 4h typisch 18 Bars; `Backtester.run` bricht bei `< 30` Bars ab, bevor Indikatoren oder Fills gerechnet werden. Das Kostenmodell kommt auf diesen Fenstern nicht zum Tragen — die Ablehnungen sind ein Geometrie-Problem der Walk-Forward-Folds, kein 3-%-Round-Trip.

### unresolvable

| Grund | Anzahl |
| --- | ---: |
| ohlcv unavailable | 5 |

| symbol | unresolvable |
| --- | ---: |
| CAT/USDT | 5 |

## Caveat: Sharpe und Win-Rate sind erstmals netto

`hermes/metrics.py` (`sharpe_from_trades`, Win-Rate über `pnl > 0`) rechnet über das `pnl`-Feld der SELL-Trades. Unter dem Legacy-Modell war `pnl = (price − entry) · qty` **brutto** (1,5 % Slippage traf nur `balance`, keine Fee). Unter `core/costs.py` ist `pnl = CostModel.realized_pnl(...)` **netto** (0,2 % Fee + Tier-Slippage stecken in `quote_net` und der Kostenbasis). Sharpe und Win-Rate der Neu-Bewertung sind deshalb nicht 1:1 mit den gespeicherten Legacy-Metriken vergleichbar — sie messen zum ersten Mal denselben Cash-Strom wie der Kontostand.

## Methode

- Baseline-Params: Profil `symbol|timeframe` aus `baseline*.json` des jeweiligen Ledgers, `variable = old_value`.
- Varianten-Params: dieselben Params, `variable = new_value`.
- Fenster: `[created_at − 14d, created_at]`, OHLCV über `historical_prices._fetch_ohlcv_range` (Gate, `enableRateLimit`, Prozess-Cache). Ein Fetch pro Symbol über die Vereinigung der Fenster.
- Verdict: `GoalEngine.evaluate_walk_forward` — **ohne** Live-Evidence und ohne Dual-Promote (beides ist Ledger, nicht Kostenmodell).
- Fehlt die Historie eines Symbols → `unresolvable`, kein Raten.
