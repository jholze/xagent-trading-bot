# Ticket: Block micro Grid / BB sells (TAO-style)

| | |
|--|--|
| **Typ** | Config / Risk policy |
| **Branch** | `fix/grid-min-gain-bb-stable` + follow-up `fix/stable-bb-sell-overlay` |
| **Priorität** | Mittel–Hoch (Fees + Lärm durch 5‑Min-Roundtrips) |
| **Scope** | Staging-first |
| **Status** | Done (config + stable overlay wiring) |

## Problem

Beispiel **TAO/USDT** (2026-08-09):

- Buy `source=grid` @ 208.88
- Sell ~5 min später `source=grid`, **`exit=bb_upper`**, gain **+0,1 %**, `SELL_FULL`
- Rationale: `BB->upper extension … RSI=76.0, gain=0.1%`

Zwei Lücken:

1. **Grid-Level-Harvests:** `grid.sell_policy.min_sell_gain_pct = 0.0` (nur ~0,15 % Buffer).
2. **Structure BB auf Stable/Grid:** `stable_altcoin` hat **kein** `bb_sell_min_gain_pct` → Default **0** in `market_structure.py`. Volatile hat bereits **12**.

Order-Channel kann `grid` sein, obwohl Exit `bb_upper` ist (Profil/Sources).

## Lösung

| Knob | Alt | Neu | Pfad |
|------|-----|-----|------|
| `grid.sell_policy.min_sell_gain_pct` | `0.0` | **`1.0`** | `apply_grid_sell_guards` |
| `stable_altcoin.bb_sell_min_gain_pct` | (unset→0) | **`2`** | `evaluate_market_structure_sells` |
| `mid_cap_defaults.bb_sell_min_gain_pct` | unset | **`2`** | same (kein Micro-BB) |
| `core/config.py` grid defaults | `0.0` | **`1.0`** | parity |

Effektive Grid-Floor mit `green_buffer_pct: 0.15`:

`entry × (1+1/100) × (1+0.15/100)` ≈ **+1,15 %** über Entry.

Volatile unverändert (`bb_sell_min_gain_pct: 12`).

## Kill / Rollback

```json
"grid.sell_policy.min_sell_gain_pct": 0.0
"stable_altcoin.bb_sell_min_gain_pct": 0   // oder Key entfernen
"mid_cap_defaults.bb_sell_min_gain_pct": 0
```

## Acceptance

- [x] Config + core defaults
- [x] Unit: grid guard blocks gain &lt; 1 %, allows ≥ ~1,2 %
- [x] Unit: stable-like bb_sell_min_gain_pct=2 blocks +0,1 % TAO case
- [x] PR → staging deploy
- [x] Ticket closed

## Follow-up (2026-08-09): stable overlay wiring

Config `stable_altcoin.bb_sell_min_gain_pct=2` alone was **not enough**:
`overlay_stable_sell` only merged `BASE_SELL_KEYS` and dropped all `bb_sell_*`.
Live DE still saw default `bb_sell_min_gain_pct=0` → WLD/ETH/BNB micro `bb_upper`
full closes after deploy of #231.

**Fix:** merge `STABLE_STRUCTURE_SELL_KEYS` (incl. `bb_sell_min_gain_pct`) in
`overlay_stable_sell` — same structure knobs volatile already got.

## Non-goals

- Grace after entry (Option B)
- Partial statt Full (Option C)
- Grid global aus
