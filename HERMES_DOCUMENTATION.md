# Hermes — Self-Improving Trading Agent

**Language:** [Deutsch](HERMES_DOKUMENTATION.md) · English

Last updated: 23 June 2026 · Bot version 2.0

This guide explains the **Hermes agent** in plain language: what it works with the **live bot** and the **volatile profile** (exit ladder, trailing stop, 1h timeframe), and how to follow everything in **Telegram** — even without trading or programming experience.

> **Full system:** [DOCUMENTATION.en.md](DOCUMENTATION.en.md) §6.5–6.9 · **`/ask`** for plain-language questions · **`/decisions`** for every single decision

---

## 1. What is Hermes?

Think of Hermes as an **assistant that constantly tests and improves your trading strategy** — like a scientist:

1. It takes the **current strategy** (e.g. “buy ARIA when RSI is below 30”).
2. It changes **exactly one parameter** (e.g. RSI threshold from 30 to 28).
3. It **simulates** both variants on historical price data.
4. It decides: **better, same, or worse** — and remembers the result.
5. Only when the new variant is **clearly better**, it is saved to **memory** (optionally also to `config.strategies[]`).

Hermes does **not trade on its own**. It only optimizes parameters for your existing RSI/Bollinger strategy. Better values go to **`hermes/memory/baseline.json`** — and are **used immediately by the live bot**, even if they are **not** yet in `config.strategies[]`.

---

## 1b. Hermes vs. live bot vs. volatile profile — three roles

| Role | What is it? | Analogy |
|------|-------------|---------|
| **Live bot** | Buys and sells every 10 minutes | The driver |
| **Hermes** | Tests settings in the background (~30 min.) | The tinkerer in the lab |
| **Volatile profile** | Extra exit rules for hectic coins | Seatbelt on a wild ride |

**Important:** Hermes and the live bot are **not the same program**, but they share **memory** (`baseline.json`).

### Which parameters apply when trading?

The bot picks in this order (see `strategies/registry.py`):

```
1. config.strategies[]     — you pinned the coin in config (e.g. ARIA)
2. Hermes memory           — learned values from baseline.json (e.g. H, WLD)
3. + volatile overlay      — only with open position + volatile coin
4. altcoin_social          — trending CMC coins without their own profile
5. standard defaults
```

**Promotion** (`sync_to_config: true` + successful test) also writes values to `config.json` → `strategies[]`. That is **optional** for live operation — memory alone is enough.

**Volatile profile** (`volatile_altcoin` in `config.json`) is **independent** of Hermes: it adds BB/volume sell rules **on top** when the coin is classified volatile. Profile name then: `hermes_baseline+volatile`.

Currently: `volatile_altcoin.mode: live` — volatile sell rules are **executed** (not shadow-only).

---

## 1d. Volatile sells alongside Hermes (since bot 2.0)

Hermes optimizes **parameters** (RSI thresholds, stop-loss, …). These **three mechanisms** control **how much** is sold — independent of Hermes:

| Mechanism | What it does | Example |
|-----------|--------------|---------|
| **Exit ladder** | 4 tiers at 30/30/20/20 % of position **peak** | Tier 2 → sell 30 % of peak, `exit_ladder_step: 2` |
| **ATR trailing stop** | From +10 % gain: full close if price drops sharply from high | `Trail->ATR stop (drop 12% from high)` |
| **Structure sells** | Upper BB, volume exhaustion, volume dump | `market_structure` in `decisions.jsonl` |

**1h timeframe:** New volatile coins use **1h candles** (`volatile_altcoin.timeframe: "1h"`). Legacy positions keep their TF until closed.

**Rebuy cooldown (4 h):** After a sell, the risk manager blocks **automatic** buys for 4 hours — prevents sell→buy churn (see H/USDT June 22). Hermes params stay active; only **timing** of rebuy is gated.

**Important:** Hermes does **not** tune exit-ladder tiers or trailing params — those come from `config.json` → `volatile_altcoin`. Hermes may optimize e.g. `rsi_sell_30`; the ladder then decides **how many coins** actually leave on an RSI sell signal.

Details with examples: [DOCUMENTATION.en.md §6.7–6.9](DOCUMENTATION.en.md#67-exit-ladder--staged-partial-sells-volatile)

---

## 1c. Hermes coin pool (hybrid mode)

Hermes does not only learn coins from `hermes.symbols`. In **hybrid mode** (`symbols_mode: hybrid`) the pool is built from:

| Source | Example | Meaning |
|--------|---------|---------|
| **Pins** (`symbols_pin`) | ARIA/USDT | Always included — your main coin |
| **Open positions** | H/USDT, STG/USDT | What you hold gets optimized too |
| **CMC top-N** | from watchlist | Trending coins from the watchlist |

`hermes.symbols: ["ARIA", "H", …]` alone does **not pin** coins in hybrid mode — only `symbols_pin` does. H enters the pool because you hold a position or CMC puts it on the watchlist.

Maximum **8 coins** at once (`symbols_max: 8`). Rotation: `signal_activity` — Hermes works on coins with the most signal activity first.

---

## 2. What's new? (Phase 2+)

| Feature | Short explanation | Why it matters |
|---------|-------------------|----------------|
| **Walk-forward validation** | Splits history into **many small time windows** (folds) instead of one big backtest | Avoids “lucky streaks” — a setting must be better in **multiple** periods |
| **Grok hardening** | AI suggestions use a stable client with **retries** on errors | Fewer crashes; without API key Hermes falls back to **simple heuristics** |
| **Skills (learned patterns)** | Each cycle stores a **lesson** (“high RSI → worse”) with a confidence score | Later experiments avoid known mistakes |
| **Order provenance** | Trades using Hermes params can be marked as **Hermes experiment** in order history | You can see *which trade came from which optimization* |

---

## 3. A learning cycle — step by step

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
│  Baseline   │ ──► │  Proposal    │ ──► │  Backtest both  │
│  (current)  │     │  (1 param)   │     │  variants       │
└─────────────┘     └──────────────┘     └────────┬────────┘
                                                   │
                     ┌──────────────┐     ┌─────────▼────────┐
                     │  Skill +     │ ◄── │  Evaluation      │
                     │  experiment  │     │  (promoted?)     │
                     │  saved       │     └─────────┬────────┘
                     └──────────────┘               │
                                          yes ──────► config + Telegram
```

**In words:**

1. **Load baseline** — current parameters for symbol/timeframe (e.g. `ARIA/USDT` on `4h`).
2. **Proposal** — Grok (if `XAI_API_KEY` is set) or heuristic picks **one** parameter and new value.
3. **Backtest** — walk-forward over ~35 days, split into 7-day windows with 3-day step.
4. **Evaluation** — variant must win in **≥ 60% of folds** and meet minimum metrics.
5. **Adoption** — better parameters → `hermes/memory/baseline.json` (always); optionally → `config.strategies[]` on promotion.
6. **Learning** — experiment + skill saved in `hermes/memory/`.
7. **Live bot** — uses memory **immediately** on the next cycle (buy, sell, rebuy).

---

## 4. Walk-forward — explained simply

**Problem:** A 35-day backtest can look good by chance even if the strategy is unstable.

**Solution:** Hermes splits 35 days into **overlapping week windows**:

```
|-- Fold 0: day 1–7 --|
      |-- Fold 1: day 4–10 --|
            |-- Fold 2: day 7–13 --|
                  ... etc.
```

- Each window = **7 days** (`fold_days`)
- Step = **3 days** (`step_days`)
- Typically **~10 folds** with 35-day lookback

**Promotion rules (simplified):**

| Criterion | Default | Meaning |
|-----------|---------|---------|
| Folds won | ≥ 60% | Variant must beat baseline Sharpe in at least 6 of 10 folds |
| Aggregate Sharpe | > baseline | Better on average across all folds |
| Success criteria | e.g. Sharpe ≥ 0.8, DD ≤ 15%, WR ≥ 50%, ≥ 5 trades | Average must be “good enough” |
| Drawdown per fold | max +5% vs. baseline | No fold may badly worsen the strategy |

---

## 5. Grok, heuristic, and skills

### Grok (optional)

If `XAI_API_KEY` is set in `.env`:

- Grok suggests the next parameter and value.
- Grok writes a **skill lesson** after each cycle.
- On API errors: automatic **retry** (up to 3 attempts), then fallback.

### Heuristic (always available)

Without API key Hermes randomly picks an untested parameter and nudges it in small steps (e.g. `rsi_sell_30`: 70 → 72).

### Skills

Stored in `hermes/memory/skills.json`:

- **Pattern:** e.g. “RSI sell 30 from 70 to 72 worsened Sharpe”
- **Confidence:** how sure Hermes is (rises with repeated confirmation)
- **Usage:** parameters with bad skills are **avoided** in new proposals

---

## 6. Memory, promotion, and order provenance

### Two ways Hermes values go live

| Path | Stored where | When active | Typical example |
|------|--------------|-------------|-----------------|
| **Memory (default)** | `hermes/memory/baseline.json` | Immediately in live bot | H/USDT without `strategies[]` entry |
| **Promotion** | also `config.strategies[]` | After successful backtest + `sync_to_config` | ARIA — you want values fixed in config |

**Sell → rebuy:** Memory is kept. You do **not** lose Hermes optimization when you sell and buy again — unless you delete memory manually or an explicit `strategies[]` entry overrides it.

### Traceability in orders

On promotion the strategy entry has `hermes_experiment_id` and `hermes_updated_at`. Orders may have `source: hermes`. In `/orders 3`:

```
Hermes  Experiment exp_65e4108a
```

Without promotion, check `/why H` or `logs/decisions.jsonl` for profile `hermes_baseline` or `hermes_baseline+volatile`.

---

## 7. Integration in the bot

### Automatic (recommended)

In `config.json`:

```json
"hermes": {
  "enabled": true,
  "cycle_interval_sec": 1800
}
```

When the bot starts (`aria_bot.py`), Hermes runs as a **background thread** every 30 minutes (1800 seconds).

### Manual (CLI)

```bash
python3 hermes_agent.py --status
python3 hermes_agent.py --once
python3 hermes_agent.py --once --demo
python3 hermes_agent.py --interval 3600
```

### Telegram

| Command | Effect (for beginners) |
|---------|------------------------|
| `/hermes` | Technical status + **plain text** for last cycle |
| `/hermes_last` | Last learning cycle only, in plain language |
| `/hermes_run` | Start one learning cycle now |
| `/hermes_status` | Same as `/hermes` |
| `/why SYMBOL` | Last trade decision — incl. Hermes experiment ID if set |
| `/decisions` | Chronological log of all bot decisions |
| `/ask` | Plain-language question — e.g. “Why did H sell yesterday?” |

### Automatic Hermes messages in Telegram

Hermes notifies **on its own** — you don't need to send `/hermes` constantly.

| Situation | What you see (simplified) |
|-----------|---------------------------|
| **Every learning cycle** (~30 min.) | “Hermes test rejected …” or why it wasn't adopted |
| **Promotion** (rare) | “Strategy adopted” — which parameter changed and why |
| **Live veto** | “Live guard” — backtest OK but recent real trades disagree |
| **Dual/counterfactual** | Extra “what if?” PnL delta in USDT |

Each message has:
1. **Headline** — what happened
2. **Explanation** — e.g. “Only 1/4 backtest windows were better”
3. **Technical line** — `rsi_sell_30 70->68 | verdict=rejected` (optional)

Control in `config.json`:

```json
"hermes": {
  "notify_on_promotion": true,
  "live_evidence": { "notify_on_live_veto": true }
},
"observability": {
  "telegram_explanations": {
    "notify_hermes_every_cycle": true
  }
}
```

If `notify_hermes_every_cycle: false`, you only get promotion and live-veto — not messages for rejected tests.

---

## 8. Configuration (`config.json` → `hermes`)

### Key fields

| Field | Example | Explanation |
|-------|---------|-------------|
| `enabled` | `true` | Enable Hermes in the bot |
| `symbols` | `["ARIA/USDT"]` | Coins to optimize |
| `timeframes` | `["4h"]` | Candle timeframe |
| `tunable_params` | `rsi_buy_low`, … | Parameters Hermes may change |
| `cycle_interval_sec` | `1800` | Pause between cycles (seconds) |
| `sync_to_config` | `true` | Write better params to bot strategy on promotion |
| `notify_on_promotion` | `true` | Telegram on successful adoption |

### Success and failure criteria

```json
"success_criteria": {
  "min_sharpe": 0.8,
  "max_drawdown_pct": 15,
  "min_win_rate": 50,
  "min_trades": 5
},
"failure_criteria": {
  "sharpe_delta_max": -0.2,
  "drawdown_delta_max": 5
}
```

### Walk-forward

```json
"validation": {
  "mode": "walk_forward",
  "backtest_days": 35,
  "fold_days": 7,
  "step_days": 3,
  "min_folds_won_ratio": 0.6,
  "min_trades_aggregate": 5
}
```

### Skills

```json
"skills": {
  "max_per_variable": 5,
  "min_confidence": 0.25
}
```

---

## 9. Storage (memory)

| File | Content |
|------|---------|
| `hermes/memory/baseline.json` | Current best parameters + metrics |
| `hermes/memory/experiments.json` | History of all experiments |
| `hermes/memory/skills.json` | Learned patterns |

**Demo mode** (`--demo` or `DEMO_MODE=1`): parallel `*.demo.json` files — your live memory stays untouched.

---

## 10. Concrete examples (~30 days practice: H, ARIA, WLD)

These stories come from real bot runs (May–June 2026, enhanced dry run). They show **who gets which rules** — without reading charts.

### Example 0 — H/USDT: Hermes memory + volatile, no config entry

**Situation:** Humanity (H) is a very volatile altcoin. It is **not** in `config.strategies[]`, but Hermes has a profile in `baseline.json` (e.g. `rsi_sell_30: 70`, `stop_loss_pct: 50`). On 14 Jun. Hermes ran 30 experiments for H — many rejected, **memory still active**.

**Timeline (simplified):**

| When | Event | What to understand |
|------|-------|-------------------|
| 13 Jun. | Buy ~250 USDT @ ~$0.28 | Entry — Hermes parameters apply from now on. |
| 13 Jun. | Small sell @ ~$0.29 | First profit take (+~$2). |
| 14 Jun. 07:12 | Large auto sell @ ~$0.48 | Pump detected → +~$43 realized. |
| 14 Jun. | Rebuy @ ~$0.24 | Bought dip again — **same Hermes values**. |
| 14 Jun. evening | Two partial sells @ ~$0.43–0.42 | Staggered exits (+~$168). |
| 15 Jun. | Sell @ ~$0.62, then rebuy @ ~$0.35 | Further exit (+~$186), position rebuilt. |
| 22 Jun. 12:55 | `SELL_STOP_FULL` (~$63 remainder) | Emergency stop — 88 % already sold. |
| 22 Jun. 13:02 | Auto-`BUY` 7 min later | **Churn** — no rebuy cooldown yet. **Since 2.0:** 4 h pause. |

**Question:** “Did Hermes lose the strategy after selling?”  
**Answer:** No. Memory in `baseline.json` still applies — across rebuys too.

**Question:** “Why BUY right after stop?”  
**Answer:** Technical BUY + CMC sentiment — without rebuy cooldown the risk manager allowed it. Today: `architecture.min_hours_after_sell_before_rebuy: 4`.

**With exit ladder (live, since 2.0)** Telegram might show:

```
🔴 SELL 30% EXECUTED — H/USDT
Why: Taking profit — exit ladder tier 2.
Technical: exit_ladder_step: 2 | Profile: hermes_baseline+volatile
```

**With trailing stop** (on a strong run):

```
🔴 SELL FULL — SIREN/USDT
Why: Price 12 % below local high — ATR trailing triggered.
Technical: Trail->ATR stop | trailing_stop
```

### Example 0b — ARIA: config beats Hermes

ARIA has a **fixed** entry in `config.strategies[]` (`rsi_sell_30: 72`, `take_profit_pct: 12`). Even if Hermes memory suggests `70`, the live bot uses **72** from config.

| Rule of thumb | |
|---------------|--|
| You want full control | Add coin to `strategies[]` |
| You want Hermes to learn along | Hold coin **without** entry (like H) |

### Example 0c — WLD: like H, less spotlight

WLD/USDT also has memory in `baseline.json`, no `strategies[]` entry. Same flow as H: `hermes_baseline` without position, `hermes_baseline+volatile` with open position and high ATR.

---

### Example A — Successful promotion (into `config.strategies[]`)

**Starting point:** Baseline `rsi_buy_low: 30`, Sharpe 0.95 over 10 folds.

**Experiment:** Grok proposes `rsi_buy_low: 28`.

**Result:**

- 7 of 10 folds better (70% ≥ 60%) ✓
- Aggregate Sharpe 1.12 > 0.95 ✓
- Win rate 54%, max drawdown 11%, 8 trades ✓

**What happens:**

1. Telegram (example):
   ```
   🧠 Hermes — strategy adopted
   ✅ Hermes adjusted 'RSI buy low' (30 -> 28) for ARIA/USDT.
   Backtest improved; setting is now used in live trading.
   Sharpe: 1.12 | WR: 54% | Folds: 7/10
   ```
2. `hermes/memory/baseline.json` updated (always)
3. If `sync_to_config: true`, also `config.json` → strategy for `ARIA/USDT` / `4h`
4. Next BUY on ARIA uses `rsi_buy_low: 28`
5. Order detail later shows: `Hermes Experiment exp_a1b2c3d4`

---

### Example B — Rejected experiment

**Experiment:** `rsi_sell_30: 70 → 72` (heuristic, no Grok key)

**Result:**

- 0 of 10 folds better
- Aggregate Sharpe 0.0 (too few trades in folds)

**Output:**

```
Experiment exp_65e4108a: rsi_sell_30 70.0→72 → rejected
Won 0/10 folds (0% < 60%)
```

**What happens:**

- Baseline **stays** at 70
- Skill saved: “raising RSI sell 30 → bad”
- Next cycle avoids `rsi_sell_30` changes

---

### Example C — Telegram workflow

```
You:  /hermes_last
Bot:  🧠 Hermes — last cycle
      🔬 Hermes test rejected for H/USDT: RSI sell tier 30% 70->68.
      Only 1/4 backtest windows were better — too uncertain for a live change.
      rsi_sell_30 70->68 | verdict=rejected

You:  /why H
Bot:  ❓ Why — H/USDT
      Last decision: SELL_30
      Why: RSI overbought — selling 30% ...
```

---

### Example D — Without vs. with Grok

| Situation | Proposal source | Skill text |
|-----------|-----------------|------------|
| No `XAI_API_KEY` | `heuristic` — random param ± small step | Template: “Changing rsi_sell_30 …” |
| With `XAI_API_KEY` | `grok` — context-aware | Grok writes pattern in natural language |

In both cases the **same strict backtest** runs — Grok only influences the *idea*, not the *verdict*.

---

## 11. What Hermes does **not** do

- **No direct trading:** Hermes does not place orders; it only optimizes strategy parameters.
- **No override of `strategies[]`:** If a coin is fixed in config, **config wins** — Hermes memory is ignored.
- **No profit guarantee:** Backtests are historical — the future may differ.
- **No multi-parameter tuning:** One change per cycle (scientific method).
- **Not the volatile profile:** BB/volume rules, exit ladder, and trailing stop come from `volatile_altcoin` — separate layer on top.
- **No exit-ladder tuning:** Tiers `[30,30,20,20]` are config, not Hermes experiments.
- **No rebuy-cooldown tuning:** 4 h after sell is `architecture.*`, not Hermes.
- **Illiquid coins:** Few trades in 7-day folds → many rejections (intentionally conservative).

---

## 12. Common issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| Always `rejected`, 0 trades | Too few signals on coin/timeframe | Normal for volatile coins — **last memory** still applies in live bot |
| Live bot uses “wrong” values | Coin in `strategies[]` | Remove or edit config entry — it overrides memory |
| H doesn't use Hermes values | Explicit `strategies[]` entry for H | Remove H from config; check `baseline.json` |
| Sells “too little” / mini remainder left | Old partial-cap logic or no ladder | Exit ladder on? `exit_ladder.enabled: true` in `volatile_altcoin` |
| Buy right after sell | Rebuy cooldown | Normal since 2.0 — `/risk` shows `min_hours_after_sell_before_rebuy`; manual `/buy` still works |
| Don't understand a decision | Too many sources | `/why SYMBOL` or `/ask Why …?` or `/decisions SYMBOL` |
| `Grok ... unavailable` | No API key | Set `XAI_API_KEY` in `.env` — or use heuristic |
| No promotion despite good Sharpe | Folds ratio under 60% | Strategy unstable across time — by design |
| Hermes not running | `hermes.enabled: false` | Set `true` in `config.json` and restart bot |

---

## 13. Live evidence & dual mode (v1.7+)

Hermes can compare walk-forward results with the **dry-run ledger** and optionally use **counterfactual replay**.

### Modes (`hermes.live_evidence.mode`)

| Mode | Behavior |
|------|----------|
| `guardrail` | WF stays primary; live vetoes only on strongly negative `live_sell_pnl` |
| `dual` | Promotion via **WF** or via **live + counterfactual** (path B) |

### Path B (dual) — safety guards

Promotion without WF win only if **all** conditions hold:

- `live_trades >= 3`, `live_sell_trades >= 2`, `live_sell_pnl >= 0`
- Counterfactual **seeded** (position from `live_trade_history`, incl. manual BUY)
- `variant_sells >= min_counterfactual_sells` (default 1)
- `counterfactual_pnl_delta > 0` and `>= min_live_pnl_delta_usdt` (default 5)
- Variable in **exit whitelist** (`take_profit_pct`, `rsi_sell_30/20`, CMC thresholds)
- `stop_loss_pct` only via WF (`live_blocklist`)

### Learning `take_profit_pct`

In `tunable_params`. Controls `SELL_TP` (30% partial sell) in TA strategy. Bounds: 5–30%.

### Counterfactual manually

```bash
python3 -m hermes.counterfactual --symbol H/USDT --from 2026-06-13T15:00:00 --to 2026-06-14T12:00:00
```

Experiment records include `counterfactual_metrics` (delta, seeded, seed_source).

---

## 14. Quick reference

```bash
python3 hermes_agent.py --status
python3 hermes_agent.py --once --demo

/hermes
/hermes_last
/hermes_run
/why SYMBOL
/decisions
/ask Why did H sell?
```

**Files:** `hermes/` (logic), `hermes/memory/` (memory), `strategies/exit_ladder.py`, `strategies/trailing_stop.py`, `notifications/user_explain.py` (explanations), `config.json` → `hermes`, `volatile_altcoin`, `architecture`, `observability`.

**Showcase:** `python3 scripts/telegram_transparency_showcase.py`

---

*More bot documentation: [DOCUMENTATION.en.md](DOCUMENTATION.en.md) (full system, transparency glossary, all Telegram commands).*