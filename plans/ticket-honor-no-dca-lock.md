# Ticket: Honor explicit `no_dca` on position locks (BLESS sniper)

| | |
|--|--|
| **Typ** | Bugfix / Risk safety |
| **Branch** | `fix/honor-no-dca-lock` |
| **Priorität** | Hoch |
| **Scope** | Staging-first |
| **Status** | Implemented on branch (pending PR) |

## Multi-agent finding (summary)

Three explore agents agreed:

1. **Sniper path** is bot_http → Risk → fill; no local skip of Risk.
2. **`signal=BUY_DCA`** already triggers Risk `dca_blocked`.
3. **Root cause (primary):** `lock_modes()` remaps exact  
   `{no_auto_sell, no_dca, no_evict}` → sell-only `{no_auto_sell, no_evict}`  
   → **strips `no_dca`**. BLESS ops lock used that triple → sniper DCA allowed by design.
4. Secondary: fail-open `except: pass` on lock checks; sniper source not listed in `_is_dca_buy` sources (signal still covers).

Incident: BLESS #781 `dca_sniper` $380 @ 0.0125 while Mongo showed lock modes including `no_dca`.

## Product rule

| Mode set | Intent |
|----------|--------|
| Telegram `/lock` default | sell-only: `no_auto_sell` + `no_evict` (DCA/sniper OK) |
| Modes **include** `no_dca` (any set) | **DCA + sniper blocked** — honor as written |
| Ops full hold | may use triple including `no_dca` — must work |

## Fix

1. Remove legacy triple remap in `lock_modes` — honor stored modes.
2. Fail-closed: sniper execute + Risk `dca_blocked` errors → deny.
3. `_is_dca_buy`: also treat `source in ("dca_sniper", …)`.
4. Unit tests: triple with `no_dca` **blocks** DCA; default telegram modes still allow DCA.

## Acceptance

- [x] `modes=[no_auto_sell,no_dca,no_evict]` → `dca_blocked` True
- [x] `DEFAULT_MODES` → `dca_blocked` False
- [x] Fail-closed lock checks (sniper + risk)
- [x] Unit tests green
- [ ] PR staging + ticket closed

## Non-goals

- Revert BLESS sniper fill (ops separate)
- Change sniper sizing / recovery_hold
