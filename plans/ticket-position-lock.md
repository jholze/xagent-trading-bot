# Ticket: Position Lock (Auto-Sell / DCA / Eviction Hold)

| | |
|--|--|
| **Typ** | Feature / Risk safety |
| **Branch** | `feat/position-lock` |
| **Priorität** | Hoch (verhindert Re-Sell nach manuellem Ledger-Revert, z.B. BLESS) |
| **Scope** | Staging-first |

## Problem

Nach manuellem Undo eines Auto-Sells (Mongo ledger revert) feuert `exit_ws` / Trail erneut und schließt die Position wieder. Es fehlt ein **persistenter** Hold, den alle Exit-Pfade respektieren.

## Lösung

Pro offener Position optionales Feld `pos.lock`:

```json
{
  "enabled": true,
  "modes": ["no_auto_sell", "no_dca", "no_evict"],
  "reason": "telegram_lock",
  "locked_by": "telegram",
  "locked_at": "ISO",
  "until": null
}
```

### Modes

| Mode | Wirkung |
|------|---------|
| `no_auto_sell` | Blockiert exit_ws, trail, TA/cycle sells (nicht manual) |
| `no_dca` | Blockiert DCA add-ons |
| `no_evict` | Slot-Eviction überspringt Victim |
| `no_manual_sell` | Optional: auch `/sell` blocken (default aus) |

### Kill

`risk.position_locks.enabled=false`

## Enforcement-Punkte

1. **RiskManager SELL** → `code=position_locked` → ledger rejected / `/orders_blocked`
2. **exit_realtime.execute** early return (vor Order)
3. **exit_realtime.hub** skip trail eval
4. **DecisionEngine** sell/DCA → HOLD + source `position_locked`
5. **dca.should_dca** hard gate
6. **slot_eviction** skip locked victims
7. **positions** serialize/deserialize + `_CACHE_FIELDS` (überlebt order-rebuild)
8. **Telegram** `/lock` `/unlock` + 🔒 in `/positions`

## Ops

```
/lock BLESS permanent hold_after_revert
/unlock BLESS
/lock
```

## Acceptance

- [x] Auto-sell blockiert, manual sell erlaubt
- [x] DCA + eviction blockiert
- [x] Persistenz über redeploy (positions cache)
- [x] risk_rejects + rejected order code `position_locked`
- [x] Unit tests grün
- [ ] Staging: BLESS sell revert + lock + deploy
