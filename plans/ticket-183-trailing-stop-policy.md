# Plan: Trailing-Stop Policy Consistency (#183)

**Ticket:** [#183](https://github.com/jholze/xagent-trading-bot/issues/183)  
**Epic:** [#178](https://github.com/jholze/xagent-trading-bot/issues/178) (orthogonal — no WS work)  
**Branch (nach Approve):** `feat/trailing-stop-policy-183` from `origin/staging`  
**Mode:** Plan + Arena (grill) — **noch nicht implementieren** bis Freigabe  

---

## 1. Problem (präzise)

Volatile defaults in `config.json`:

```json
"trailing_stop": {
  "activation_gain_pct": 5,
  "min_trail_pct": 8,
  "atr_multiplier": 2.0,
  "max_trail_pct": 25
}
```

**Semantik heute** (`strategies/trailing_stop.py`):

1. Arm nur wenn **current gain** ≥ `activation_gain_pct` (5 %).
2. Trigger wenn Drop vom **recent_high** ≥ `trail_pct` (clamp ATR×mult in [min, max]).
3. **Kein** Bezug zu Entry beim Stop-Level → Stop kann **unter Entry** liegen.

| Peak-Gain | Stop @ 8 % Trail | vs Entry |
|-----------|------------------|----------|
| +5 % (gerade armed) | −3.4 % | **unter Wasser** |
| +8 % | −0.6 % | knapp unter |
| +10 % | +1.2 % | erst jetzt über Entry |
| +12 % | +3.0 % | ok |

**Invariant verletzt:** „Trailing-Stop schützt Gewinn“ — bei frischem Arm schützt er oft **nicht einmal Break-even**.

Gilt für **Cycle DE** und **WS Sidecar** (gleiche pure eval).

---

## 2. Ziel / Non-Goals

### Ziel
Trailing-Stop-Policy so, dass nach Activation der implizite Stop **nie unter Entry** liegt (optional: nie unter Entry×(1+ε)), ohne Runner unnötig abzuwürgen.

### Success (Staging, 24–72h)
- [ ] Unit-Tests: BE-Lock / Policy-Math grün
- [ ] Mind. 1 Config-Flag schaltbar (`breakeven_lock` o.ä.)
- [ ] Ledger: `exit_ws` / `trailing_stop` Sells mit Rationale logbar
- [ ] Kein Anstieg Double-Sells / Fire-Storm
- [ ] Optional: kleiner Offline-Backtest / Counterfactual auf Demo-Open-Lots

### Non-Goals (explizit)
- Kein WS-Infra, kein Sidecar-Refactor
- Kein multi-factor Exit-Score / Hermes / CMC im Tick
- Kein Umbau TTP (nur als **Vorbild** für dynamic trail, optional Phase 2)
- Kein Production-Rollout in diesem PR

---

## 3. Optionen (Arena-Vergleich)

| Option | Idee | Pro | Contra |
|--------|------|-----|--------|
| **A. Activation ↑** (z. B. 10) | Arm erst wenn Peak-Trail über Entry | 1 Zeile Config | Verzögert Schutz; +5…+9 % ungeschützt |
| **B. min_trail ↓** (z. B. 4) | Engerer Trail | Weniger Underwater | Mehr Whipsaw auf Vol-Coins |
| **C. BE-Lock** (empfohlen) | `stop = max(entry, peak×(1−trail%))` | Korrigiert Invariant; behält early arm | Etwas mehr BE-Exits auf Noise |
| **D. Peak-scale trail** (wie TTP) | Trail 3→12 % mit Peak | Bessere Runner | Mehr Config; komplexer |
| **E. Hybrid C+A** | BE-Lock + activation 5–8 | Robust | Zwei Knöpfe |

### Math BE-Lock (C)

```
trail_pct = clamp(atr * mult, min_trail, max_trail)
raw_stop  = recent_high * (1 - trail_pct/100)
stop      = max(entry, raw_stop)   # optional: entry * (1 + be_buffer_pct/100)
trigger   = price <= stop  (äquivalent: drop so groß, dass raw_stop unter price und …)
```

Implementierung sauberer als „price ≤ entry und armed“ allein:

- **Armed** bleibt: gain oder peak_gain ≥ activation (siehe Open Question).
- **Trigger:** drop_from_high ≥ trail_pct **UND** (ohne Lock) wie heute; **mit Lock:** zusätzlich nie verkaufen „nur weil unter Entry“ wenn noch nicht armed — und wenn armed, Stop-Floor = Entry.

Einfache, testbare Formulierung:

```
if not armed: return None
trail = compute_trail_pct(...)
stop_px = recent_high * (1 - trail/100)
if cfg.breakeven_lock:
    stop_px = max(entry, stop_px)  # + optional buffer
if price > stop_px: return None
→ SELL_FULL trailing_stop
```

Bei Peak +5 %, trail 8 %: raw=96.6 → **stop=entry=100** → Drop von 105 auf 100 (≈4.8 %) reicht zum Exit **bei BE**, nicht −3 %.

---

## 4. Empfohlene Richtung (nach Arena)

**Phase 1 (dieses Ticket):** **Option C — `breakeven_lock: true`** default für `volatile_altcoin`.

Begründung:
- Behebt die genannte Inconsistency **direkt**
- 1 Flag, 1 Pure-Function-Change, gleiche API für Cycle + WS
- Activation 5 % kann bleiben (früher Schutz **am Entry**, nicht darunter)
- Rollback: Flag false

**Phase 2 (optional, eigenes Ticket oder Folge-PR):** Peak-scale trail (D) analog TTP `resolve_trail_pct`, nur wenn Phase 1 gemessen und Whipsaw ok.

**Nicht Phase 1:** Nur Activation auf 10 (A) — verschiebt das Problem, löst die Invariant nicht elegant.

---

## 5. Arena / Grill

### Grill verdict
**Go with conditions** — Problem ist real und math-klar; Scope muss **Policy-only** bleiben und mit **einer** messbaren Änderung (BE-Lock) starten.

### What survives
- Pure eval in `trailing_stop.py` ist der richtige Hebel (Cycle + Sidecar erben).
- Staging-first, live demo: schnelle Iteration ok.
- Ticket grenzt WS-Infra korrekt aus.

### Hard challenges
1. **Activation misst current gain, nicht peak** → kurz dip unter 5 % disarmed TS, obwohl Peak 20 %. → Fix-Option: arm on `peak_gain ≥ activation` (einmal armed sticky via position flag?) — **Scope-Falle**; Phase 1 nur BE-Lock, Arm-Logik dokumentieren.
2. **BE-Lock + enger Noise** → viele BE-Exits bei ±1 % nach +5 % Spike. → `be_buffer_pct` (z. B. 0.5–1 %) oder min hold nach Arm (später).
3. **TTP vs TS Konflikt** → beide full sell; trail-exclusive im Cycle blockt TA, nicht TS. Doppel-Logik ok, Priorität DE: TS pri 6, TTP oft höher — prüfen und Tests.
4. **ATR floor 8 %** bleibt dominant → BE-Lock ändert Floor, nicht Whipsaw-Weite ab großen Peaks.
5. **„Besser multi-data Exit“** wird hier **nicht** geliefert — nur Trail-Stop-Semantik. Erwartung managen.
6. **Config owner bot vs env sidecar** — irrelevant für Policy, aber Staging muss nach Deploy weiter `EXIT_REALTIME_OWNER=sidecar` haben.

### Failure modes

| Risk | Symptom | Severity | Mitigation |
|------|---------|----------|------------|
| BE-Whipsaw | Viele `trailing_stop` Sells ~0 % PnL | Med | buffer; kill flag |
| Under-trading runners | Early BE kills then rebuy | Med | Rebuy-Cooldown; peak_gain arm later |
| No effect | Flag false / wrong profile | Low | Log rationale includes `be_lock` |
| Double fire | Cycle+WS same second | Low | already recently_exited + bot sole execute |
| Silent wrong formula | stop still underwater | High | unit tests table-driven |

### Assumptions to validate
- [ ] Volatile profile is primary user of activation=5 / min_trail=8
- [ ] Operators accept more BE exits vs underwater stops
- [ ] Sidecar uses same `evaluate_trailing_stop` after change (yes today)
- [ ] Demo ledger has enough +5…+12 % peaks to see effect in 48h

### 5 killer questions (beantworten vor Code-Merge)
1. Arm-Kriterium: **current gain** oder **peak gain**? (Empfehlung Phase 1: current gain behalten; sticky peak-arm = Phase 2)
2. `be_buffer_pct` default 0 oder 0.5?
3. Stable-Profile: BE-Lock auch an, wenn activation 15?
4. Soll Rationale `"BE-lock"` explizit loggen?
5. Success metric: weniger negative realized auf TS-exits, oder nur „no underwater stop price in eval“?

### Smallest next experiment
Implement BE-Lock + unit table + staging config `breakeven_lock: true` on volatile only; 48h watch `trailing_stop` exit PnL distribution.

### Stop / rollback
`breakeven_lock: false` in config **or** `trailing_stop.mode: shadow` — no code revert required if flag-gated.

---

## 6. Implementation Plan (Feature Branch)

### Branch
```bash
git fetch origin staging
git checkout -B feat/trailing-stop-policy-183 origin/staging
```

### PR-Stack (klein halten)

| PR | Inhalt |
|----|--------|
| **PR1** | `evaluate_trailing_stop` + `compute_stop_price` + tests + config flag volatile |
| **PR2** (optional) | Mini script / auswertung: counterfactual open lots BE-lock vs base |
| **PR3** (optional) | Peak-scale trail (D) — nur nach PR1 soak |

### Code-Touchpoints

| File | Change |
|------|--------|
| `strategies/trailing_stop.py` | `compute_stop_price()`, BE-lock in `evaluate_trailing_stop`, richer rationale |
| `config.json` → volatile `trailing_stop` | `"breakeven_lock": true`, optional `"be_buffer_pct": 0` |
| `tests/unit/test_trailing_stop.py` | Table: underwater without lock / locked to entry / above entry unchanged |
| `services/exit_realtime/shadow_eval.py` | nur falls es eigene Stop-Math hat (heute: ruft pure eval — **keine** Duplikat-Logik) |
| `plans/ticket-183-trailing-stop-policy.md` | Plan im Repo (Kopie dieser Spec) |

### Algorithm (Phase 1)

```python
def compute_stop_price(entry, recent_high, atr_pct, cfg) -> float:
    trail = compute_trail_pct(atr_pct, cfg)
    raw = recent_high * (1 - trail / 100)
    if cfg.get("breakeven_lock"):
        floor = entry * (1 + float(cfg.get("be_buffer_pct") or 0) / 100)
        return max(floor, raw)
    return raw

def evaluate_trailing_stop(...):
    # arm: gain_pct >= activation  (unchanged Phase 1)
    stop = compute_stop_price(entry, recent_high, atr_pct, cfg)
    if price > stop: return None
    # fire SELL_FULL, rationale includes trail%, stop, be_lock yes/no
```

### Tests (must)

1. Peak +5 %, trail 8 %, price 97 → **ohne** lock: sell; **mit** lock: sell only if price ≤ entry (97 still sell if stop=entry… wait: stop=max(100,96.6)=100, price 97 ≤ 100 → **still sell**. Good — exits at loss smaller than raw stop? Price 97 is under entry; with BE lock stop is 100, so price 97 triggers. That's **stricter** once armed: any dip to/below entry sells.  
2. Peak +5 %, price 101 → no sell (above stop 100).  
3. Peak +20 %, trail 8 %, stop 110.4 → price 111 no, 110 yes; lock doesn't change.  
4. Below activation → None even if deep drop.  
5. shadow_only still works.

**Clarify #1:** With BE-lock, after arm at +5 %, **any** return to entry triggers full exit. That is intentional for Phase 1 (protect capital). If too aggressive: use buffer +0.5 % or require drop_from_high ≥ min(trail, gain_from_entry) — document in PR.

Alternative softer BE-lock (optional flag `breakeven_lock_mode: floor_only`):

- Only raise stop floor; still require `drop_pct >= trail_pct` **in addition** so BE alone doesn't fire without trail distance.

**Arena picks softer default for less whipsaw:**

```
armed && drop_pct >= trail_pct && price <= max(entry, peak*(1-trail/100))
```

Wait: if drop_pct >= 8 and peak was +5, price is already ~96.6 < entry. So requiring drop>=trail **still** sells underwater without floor.

**Correct soft form:**

```
stop = peak * (1 - trail/100)
if breakeven_lock:
    stop = max(entry * (1+buf/100), stop)
trigger if price <= stop
```

No separate drop_pct check needed if stop encodes trail. Equivalent to drop from high ≥ trail when stop is raw; when floor binds, trigger is price ≤ entry (tighter).

### Config default (volatile)

```json
"trailing_stop": {
  "enabled": true,
  "mode": "live",
  "atr_multiplier": 2.0,
  "activation_gain_pct": 5,
  "min_trail_pct": 8,
  "max_trail_pct": 25,
  "breakeven_lock": true,
  "be_buffer_pct": 0
}
```

Stable profiles: only if they set the flag; no silent global change.

---

## 7. Validation on Staging

1. Merge PR1 → bot + sidecar pick up pure eval (same image / staging deploy).
2. Exit Radar: near_exit / would_exit for TS should show fewer „underwater“ theoretical stops.
3. Logs: `Trail->… be_lock=1 stop=…`
4. 24h: count `source=trailing_stop` sells; avg realized_pnl; compare to week prior if possible.
5. Kill: `breakeven_lock: false` or `mode: shadow`.

---

## 8. Sequence toward „multi-data exits“ (out of #183)

| Step | Ticket / Thema |
|------|----------------|
| 1 | **#183 BE-Lock** (this plan) |
| 2 | #181 peak persist + TS metrics |
| 3 | Exit-Score design (Cycle): weight trail + sensor + TA |
| 4 | WS remains hard-safety only |

#183 is **foundation**, not the full vision.

---

## 9. Effort

| Item | Estimate |
|------|----------|
| PR1 code + tests | ~2–4h |
| Staging deploy + smoke | ~1h |
| Optional counterfactual script | ~2h |
| Peak-scale Phase 2 | separate day |

---

## 10. Approve checklist

Before implementation:

- [ ] Confirm **BE-Lock (C)** as Phase 1 (not only activation=10)
- [ ] Confirm softer vs hard: price ≤ max(entry, raw_stop) after arm
- [ ] `be_buffer_pct` default **0** ok?
- [ ] Branch name `feat/trailing-stop-policy-183` ok?
- [ ] Write plan also to `plans/ticket-183-trailing-stop-policy.md` in the feature PR

---

## 11. Next action after exit plan mode

On user **approve**:

1. Create `feat/trailing-stop-policy-183` from `origin/staging`
2. Add plan file under `plans/`
3. Implement PR1 (BE-Lock + tests + config)
4. PR → staging, deploy, short validation comment on #183
