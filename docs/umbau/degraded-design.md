# Fail-Open Tier 1b — „degraded" durch den Fusion-Pfad (#299, Phase 1 §4 / §1b Gruppe A+B)

**Stand:** 5. September 2026 · **Hängt ab von:** #299 Tier 1a (`risk.fail_closed_guards`, `_guard_failed`, `UNKNOWN`-Zweig in `moderate_deploy`)

## 1. Befund in einem Satz

Die Fusion (`services/market_policy_fusion.get_global_market_bias`) liefert bereits `active: False` / `fresh: False`, wenn keine Schicht aktiv ist — aber **(a)** die Verbraucher behandeln das als „neutral" (`size_mult 1.0`, kein Block) statt als „unbekannt", und **(b)** das Santiment-Sidecar (`sidecar/regime.py:180-188`) schreibt bei fehlenden Features `size_mult=1.0` mit gültigem `as_of`, sodass die Schicht **aktiv und frisch aussieht**, obwohl nichts gemessen wurde. Fehlende Daten sind von guten nicht unterscheidbar.

## 2. Änderung der Datenform (klein, additiv)

`get_global_market_bias()` bekommt zwei Felder — alle bestehenden bleiben:

```python
"degraded": bool,          # True wenn keine Schicht aktiv ODER eine aktive Schicht nicht (fresh and measured)
"layers": {                # Diagnose, nicht für Entscheidungen
    "santiment": {"active": bool, "fresh": bool, "measured": bool, "as_of": str|None},
    "oracle":    {"active": bool, "fresh": bool, "measured": bool, "as_of": str|None},
}
```

`measured` kommt von den Produzenten (Gruppe B):
- `services/santiment/sidecar/regime.py:180-188` — der „no Santiment features"-Zweig liefert `RegimeDecision(..., measured=False)`. `size_mult` dort bleibt 1.0 (Rückwärtskompatibilität), aber die Fusion sieht `measured=False` → `degraded=True`.
- `services/santiment/policy.py` / `services/market_oracle/policy.py` reichen `measured` aus dem Snapshot durch; fehlt das Feld (alter Snapshot) → `measured=True` (kein Fehlalarm für Bestandsdaten), **aber** `fresh=False` gilt weiterhin nach `snapshot_is_fresh`.
- `services/market_oracle/regime.py:42`, `client.py:168,223`, `intelligence/macro/*` (`snapshot.py:37`, `sync.py:133`, `regime_rules.py:18`, `calendar.py:183`, `polymarket.py:104`): überall, wo bei Fehler ein neutraler Default entsteht, `measured=False` setzen statt so zu tun, als sei gemessen worden.

## 3. Verbraucher — Verhalten bei `degraded=True`

Gilt unter `risk.fail_closed_guards: "deny"`; unter `"log"` bleibt das alte Verhalten plus **eine** WARNING pro Degraded-Episode (nicht pro Zyklus). `sensor_policy` bleibt in beiden Modi fail-open (Spec §4).

| Verbraucher | heute bei Ausfall | unter `deny` + `degraded` |
|---|---|---|
| `risk_manager._dynamic_size` :1366-1381 | `global_mult=1.0`, `regime=None` → `size_boost_default` 1.35 | `global_mult = min(1.0, …)`, **nie** Boost; `global_regime="UNKNOWN"` |
| `risk_manager` `market_block` :380 | kein Block | `block_buys=True` für **neue** Positionen (fail-closed für `block_buys_on_crash`); DCA und SELL unberührt |
| `_market_bias_for_cash` :1119, `_open_book_memory_counts` :1131, `_resolve_position_capacity` :1114/1145 | `regime=None` → CRASH-Adjust −12 entfällt | kein **positives** `regime_adj`; Kapazität wie NEUTRAL, nie besser |
| `strategies/dca_policy.py:226-252` | `size_mult 1.0` = Schwelle → `deploy_mult` 1.35 | `deploy_mult` nur wenn `measured and fresh`; sonst Basis-DCA |
| `strategies/oracle_climax.py:258-272` | liest Snapshot roh, kein `snapshot_is_fresh` | `snapshot_is_fresh(snap)` prüfen; stale/missing → `MODE_IDLE` (Trailing-Exits **nicht** blockiert) |
| `risk/moderate_deploy.size_boost_for_regime` | `None` → `size_boost_default` | `None`/`UNKNOWN` → 1.0 (Tier 1a liefert den Zweig) |
| `services/market_policy_fusion.py:222` (Sentiment-Injektion) | `sentiment` aus `NEUTRAL` | `sentiment=None` wenn degraded |

## 4. Sichtbarkeit

- `core/operator_notify.notify_operator(...)` **einmal** beim Übergang `degraded False→True` (und einmal bei Rückkehr), Cooldown 30 min.
- `/health/detail` (`aria_bot.py`) — **nicht anfassen** (Guardrail); stattdessen `services/market_context_observability.py` um `market_bias_degraded` + `layers` erweitern, das liest `/health/detail` bereits.
- **Nicht** auf `bus/heartbeats.py:53` oder `architecture_runtime.py:143` stützen — die melden bei totem Backend „alles gut".

## 5. Tests (`tests/unit/test_market_bias_degraded.py`)
1. Sidecar ohne Features → `measured=False` → Fusion `degraded=True`, `active` weiterhin True.
2. Beide Schichten inaktiv → `degraded=True`.
3. Beide frisch + gemessen → `degraded=False`, alle bestehenden Felder unverändert (Snapshot-Test gegen heutige Rückgabe).
4. `_dynamic_size` unter `deny` + degraded → Größe ≤ ohne Bias, `size_boost_for_regime` liefert 1.0 — der 35-%-Boost tritt **nicht** ein.
5. `market_block` unter `deny` + degraded → BUY neu abgelehnt (`code="market_bias_degraded"`), DCA und SELL genehmigt.
6. `dca_policy` degraded → kein `deploy_mult`.
7. `oracle_climax` mit stale Snapshot → `MODE_IDLE`; mit frischem `RISK_ON` → unverändert `MODE_GRIND`.
8. `notify_operator` genau einmal pro Übergang.
Unter `"log"`: Entscheidungen identisch zu heute, WARNING geloggt (1×).

## 6. Nicht hier
Gruppe C (Memory-/Facts-Gates), D (WQE), E (Einzel-Gates im Order-Pfad) — jeweils eigene Folge-Tasks aus `docs/audit/fail-open-phase1.md`; `gainer_signal/bot_http.py:352` und `eval_queue_runtime.py:75` (echte Bugs, eigene Tickets).
