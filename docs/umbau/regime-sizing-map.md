# Regime-Sizing-Map — wo Größe entsteht (#362)

**Stand:** 12. September 2026 · **Tracking:** #362, #376 · **#362 kein Code-Change** · **#376:** `md_boost == 1.0` unter RISK_OFF/WARMUP/CRASH · **Hängt ab von:** [`konzept-regime-strategie-v4.md`](konzept-regime-strategie-v4.md) Phase 3

Umbau v4 hat zwei Schichten vorgesehen ([`konzept-regime-strategie-v4.md:163-168`](konzept-regime-strategie-v4.md)): **Orakel = Marktrisiko** (`Darf jetzt überhaupt gekauft werden, und wie groß?`, Zyklus in Minuten), **Detektor = Richtungs-Bias** (`Ist dieser Coin in einem Trend, und in welche Richtung?`, Kerze 1h/4h). Die Codebasis hat heute ~6–8 Sizing-Berührungspunkte über **zwei inkompatible Regime-Vokabulare** (Oracle/Fusion: `RISK_ON` / `NEUTRAL` / `RISK_OFF` / `CRASH`; Detector: `RANGING` / `STRONG_UPTREND` / `STRONG_DOWNTREND` / `CHOPPY_HIGH_VOL` / `TRANSITION` / …) plus ein **drittes Makro-Vokabular** (Session-/Kalender-/Polymarket-Tags, nicht dieselben Zustandsnamen). Dieses Dokument ist die geschlossene Karte: welcher Faktor in welcher Reihenfolge in die Ordergröße eingeht, mit welchem Clamp, und was bei eigenem Fehler passiert. Es ändert nichts.

`strategies/indicator_regime.py` ist **keine** Sizing-Schicht — siehe Abschnitt 5; es steht nicht in der Tabelle.

---

## 1. Kompositionspunkt (der Anker)

Einzige Stelle, an der die Faktoren zur Ordergröße werden: `RiskManager._dynamic_size` in `risk/risk_manager.py`. Der normale Entry-Pfad (nicht `manual`, nicht DCA) rechnet in **dieser Reihenfolge**:

1. **Faktoren einzeln** (noch nicht multipliziert):
   - `trust_factor` `:1766-1772` — `1.0 + trust_delta * 0.1`; bei `source == "x"` und Trust unter `min_trust_for_live` zusätzlich `*= 0.85`. `trust_score is None` → 70.0.
   - `conf_factor` `:1774` — `0.8 + (conf / 100.0) * 0.4`. `confidence is None` → 50.0.
   - `atr_factor` `:1776-1778` — `ref_atr / max(atr_pct, 0.5)`, Clamp `min(1.5, max(0.5, …))`.
   - `dd_mult` `:1780-1782` — `drawdown_size_multiplier` (Default 0.5), sobald `_equity_drawdown_pct() >= drawdown_throttle_pct` (Default 10.0); sonst 1.0.
   - `global_mult` `:1784-1812` — aus Fusion `size_mult`, nur wenn `apply_size_mult` und `active`; Clamp `max(0.0, min(1.5, …))`. Unter `fail_closed_guards == "deny"` und `degraded`: `min(1.0, global_mult)`, Regime `UNKNOWN`.
   - `coin_bias` `:1836-1841` — `get_size_bias(symbol)` (Default vorher 1.0).
   - `calendar_mult` / `session_mult` / `pm_mult` `:1853-1858` — aus `get_risk_multipliers()`; Startwerte 1.0.

2. **Produkt** `:1865-1875`:
   ```
   total = trust_factor * conf_factor * atr_factor * dd_mult
         * global_mult * coin_bias
         * calendar_mult * session_mult * pm_mult
   ```
   `max_mult` = `aggression.max_position_multiplier` (Default 2.0), `min_mult` = `risk.min_size_multiplier` (Default 0.25). `md_boost` startet bei 1.0.

3. **`md_boost` danach, multiplikativ** `:1901-1902`: `if md_boost > 1.0 and global_mult > 0: total *= md_boost`. Quelle: `size_boost_for_regime()` (`risk/moderate_deploy.py:89`), keyed auf das Fusion-Regime. Gleichzeitig **Anhebung der Clamp-Obergrenze** via `effective_max_total_multiplier` `:1903-1905` (`max(base_max, moderate_deploy.max_total_multiplier)`). `size_boost_for_regime` liefert immer `≥ 1.0` — es ist ein Boost, kein De-Risking.

4. **Clamp von `total`** `:1908-1912`: `global_mult <= 0` (CRASH / Fail-Closed-Bias / Warmup-Null) → `total = 0.0` (kein Floor). Sonst `total = max(min_mult, min(max_mult, total))` bei `:1912` — `max_mult` ist hier bereits die ggf. angehobene Decke.

5. **`exposure_multiplier` zuletzt** `:1915-1922`. Kommentar `:1914`: *„Allocator de-risking: never a boost, applied once after other multipliers.“* `None` / Parse-Fehler → 1.0; Clamp `max(0.0, min(1.0, exp_mult))` bei `:1920`; `total *= exp_mult` nur wenn `exp_mult < 1.0` (`:1921-1922`).

6. **Rückgabe** `:1956`: `base_usdt * total`, plus `factors`-Dict (Diagnose, ändert die Größe nicht mehr).

**Sekundäre Instanz desselben `md_boost`-Musters auf dem DCA-Pfad:** `:823-860`. Der DCA-Zweig setzt `sized = base_usdt` und überspringt das Produkt aus Schritt 2. Nur wenn `md_boost > 1.0` wird `sized *= md_boost` (`:853`); `trust`/`conf`/`atr`/`dd`/`global_mult`/`coin_bias`/`calendar`/`session`/`pm`/`exposure_multiplier` greifen hier nicht. `source == "manual"` (`:812-822`) überspringt die Komposition vollständig (alle Faktoren 1.0).

Damit ist die Antwort geschlossen: Größe ist `base_usdt * total`; `total` ist das Produkt der neun Faktoren, danach optional `md_boost` (nur nach oben, nur wenn `global_mult > 0`), dann Floor/Cap, dann `exposure_multiplier` (nur nach unten, Clamp `[0, 1]`, einmal).

---

## 2. Die Tabelle

Eine Zeile pro Modul bzw. Faktor im Produkt `:1865-1875` plus die zwei Nachzügler `md_boost` / `exposure_multiplier`.

| Modul | Regime-Vokabular | Was es emittiert | Wo konsumiert | Zeitbasis | Verhalten bei eigenem Fehler |
|---|---|---|---|---|---|
| Oracle + Fusion (`services/market_oracle/regime.py`, `services/market_policy_fusion.py`) → `md_boost` | `RISK_ON` / `NEUTRAL` / `RISK_OFF` / `CRASH` (Fusion zusätzlich `WARMUP`; `size_boost_for_regime` keyed auf `:112-118` inkl. `WARMUP`) | State-Selection `:101-154` → `policy_for_state` `size_mult` / `block_new_entries` `:158-184`. Fusion mit Santiment `:1-21` (`min(size_mult)`, außer Oracle-`RISK_ON` darf Santiment-NEUTRAL/RISK_ON nicht nach unten ziehen). `size_boost_for_regime()` `:89` liest den **Regimenamen**, nicht `size_mult`, und liefert Boost `≥ 1.0` | `_dynamic_size` `:1901-1902` (`total *= md_boost`); Clamp-Decke `:1903-1905`; DCA-Pfad `:853` | Orakel-Poll `poll_interval_sec` Default 300 s (`services/market_oracle/config.py:53`). Hysterese getrennt: `StateHysteresis` `:192-221`, Default `min_bars_to_flip=2` (zwei aufeinanderfolgende Rohzustände vor Flip). Fusion liest den Orakel-Snapshot, kein eigenes Raster | `md_boost`: explizit `md_boost = 1.0` (`:1906-1907`) — fail-neutral. `size_boost_for_regime` selbst `except` → 1.0 (`moderate_deploy.py:136-137`). DCA: nacktes `except Exception: pass` (`:859-860`) — Boost entfällt, `sized` bleibt `base_usdt` |
| Detector + Allocator (`intelligence/regime_detector.py`, `intelligence/strategy_allocator.py`) → `exposure_multiplier` | `RANGING` / `STRONG_UPTREND` / `STRONG_DOWNTREND` / `CHOPPY_HIGH_VOL` / `TRANSITION` (`DEFAULT_REGIMES` `:28-61`; Tech-Score `:157-207`) | `RegimeResult.primary_regime`; Allocator mappt auf `exposure_mult` `:54-116` (`STRONG_DOWNTREND` → 0.40 bei `:95`, `CHOPPY_HIGH_VOL` → 0.55 bei `:101`, `TRANSITION` → 0.70 bei `:107`, plus `*= 0.85` bei `vol_tier == "volatile"` `:116`; defensives Sentiment `≤ defensive_thresh` → 0.30). Emit als `exposure_multiplier` `:68`, `:128` | verdrahtet in `strategies/decision_engine.py:43,46,119,1329` → `TradeOrder.exposure_multiplier` → `_dynamic_size` `:1915-1922` | Kerze des Coin-Timeframes (`detect` Default `"4h"` `:224`; DecisionEngine reicht `market.timeframe` + `ohlcv_df` bzw. `fetch_ohlcv(..., limit=300)` `:1321-1324`). Flip-Cooldown `cooldown_bars` Default 6 (`regime_detector.py:58`); Score-`hysteresis` Default 0.15 (`:57`) | Detector/Allocator-`except` loggt WARNING (`decision_engine.py:1350-1351`), `allocation` bleibt `None`. Am Kompositionspunkt: `raw_exp is None` → `exp_mult = 1.0` (`:1917`); Parse-Fehler ebenfalls 1.0 (`:1918-1919`) — fail-neutral, **kein** De-Risking |
| Makro Session / Kalender / Polymarket (`intelligence/macro/regime_rules.py`, `intelligence/macro/sync.py`) | drittes Vokabular: Session-/Event-Tags (`asia_open_low_vol_fakeout`, `macro_pre_*`, `london_ny_overlap`, sonst `"neutral"`) — **nicht** `RISK_*` und **nicht** Detector-Namen | `apply_regime_rules` `:10-56` → `session_mult` / `calendar_mult` (Defaults 1.0, können auf `fakeout_size_mult` 0.5 bzw. `size_mult_pre_high_impact` 0.5 fallen). `sync.py` merged Kalenderfenster und setzt `pm_mult` (Mispricing → `min(pm_mult, 0.85)`). Publiziert via `publish_macro_snapshot` (`sync.py:292,473`) | `_dynamic_size` `:1853-1858` `get_risk_multipliers()` → Produkt `:1872-1874` | Snapshot-TTL 120 s (`intelligence/macro/snapshot.py:14`). Publish im Hermes-Memory-Zyklus (`intelligence/memory/service.py:169-178`, Intervall `HERMES_INTERVAL_SEC` Default 1800 s, min. 120, `:386`). Session-Fenster UTC-Wanduhr (`session_clock.py:11-15`). `get_risk_multipliers` clampf jeden Mult auf `[0.0, 1.5]` (`snapshot.py:88-91`) | Docstring `regime_rules.py:19`: *„fail-open defaults“*. `get_risk_multipliers` wirft nie, leerer/staler Snapshot → alle drei 1.0 (`snapshot.py:62-82`). Am Kompositionspunkt: nacktes `except Exception: pass` (`:1862-1863`) — die drei De-Risking-Eingänge bleiben 1.0 (**fail-open**) |
| `coin_bias` (`intelligence.memory.cache.get_size_bias`) | n/a (Coin-Profil, kein Marktregime) | `float` Size-Bias aus dem Memory-Profil (`cache.py:53-67`; fehlt/disabled → 1.0 in der Cache-Funktion selbst) | `_dynamic_size` `:1836-1841` → Produkt `:1871` | pro Order, Lookup des Coin-Profils; keine Kerzen-Hysterese | `_guard_failed("coin_memory_size_bias", …)` `:1836-1841`. Liefert der Guard ein Deny (`fail_closed_guards == "deny"`): `coin_bias = self._most_restrictive_coin_size_bias()` (`:1741-1751`, `memory.gross_loss.size_bias_cap` oder 0.5) — **fail-closed**. Modus `"log"`: `coin_bias = 1.0` |
| `trust_factor` | n/a | `1.0 + ((trust - 70) / 10) * 0.1`; X-Quelle mit niedrigem Trust `*= 0.85` (`:1766-1772`) | Produkt `:1866` | pro Order / Signal (`trust_score`) | n/a (kein eigener Guard; `None` → 70.0) |
| `conf_factor` | n/a | `0.8 + (conf / 100.0) * 0.4` (`:1774`) | Produkt `:1867` | pro Order / Signal (`confidence`) | n/a (kein eigener Guard; `None` → 50.0) |
| `atr_factor` | n/a | inverse ATR-Normierung, Clamp `[0.5, 1.5]` (`:1776-1778`) | Produkt `:1868` | aktuelle Indikatoren der Kerze (`indicators["atr_pct"]`) | n/a (kein eigener Guard; fehlendes `atr_pct` → `atr_reference_pct` Default 3.0) |
| `dd_mult` | n/a | 1.0 unter der Schwelle, sonst `drawdown_size_multiplier` Default 0.5 (`:1780-1782`) | Produkt `:1869` | Equity-Drawdown im Sizing-Call (`_equity_drawdown_pct`) | n/a (kein eigener Guard um `dd_mult` selbst) |
| `global_mult` | dasselbe Oracle/Fusion-Vokabular wie die `md_boost`-Zeile | Fusion-`size_mult` (nach `min(...)`, Clamp `[0.0, 1.5]`) — **De-Risking und Cap**, kein Boost über 1.5 | `_dynamic_size` `:1784-1812` → Produkt `:1870`; zusätzlich Zero-Out von `total`, wenn `global_mult <= 0` (`:1909-1910`) | wie Oracle/Fusion-Zeile (Snapshot des Orakel-Zyklus) | `_guard_failed("global_market_bias", …)` `:1808-1812`. Deny → `global_mult = 0.0`, `global_regime = "UNKNOWN"` (Größe wird in Schritt 4 genullt). `"log"`: Exception schluckt, `global_mult` bleibt 1.0 |

`md_boost` und `global_mult` sind **zwei Anwendungen desselben Oracle-Vokabulars** am selben Kompositionspunkt: `size_mult` dämpft (oder nullt), der Regimename boostet danach über die Clamp-Decke. Das Detector-Vokabular kommt nur als `exposure_multiplier` an, und nur nach dem Clamp.

---

## 3. Die eine Kompositionsregel und wer sie einhält

`services/market_policy_fusion.py:3-4`:

> Size applied ONCE in RiskManager — fusion takes min(size_mult)

Fusion hält den `min(size_mult)`-Teil intern ein (`:209-222`, Return-Clamp `:249`). Das ist die Fusion-Seite.

Der Vergleich `md_boost` gegen `exposure_multiplier` (min/Clamp-einmal) ist ein **Kategoriefehler**. `exposure_multiplier` ist ein per-Order-Allocator-De-Risk-Eingang (Detector-Vokabular; Kommentar `:1931` „never a boost, applied once after other multipliers“, Clamp `[0.0, 1.0]` bei `:1937`, Anwendung nur nach den anderen Faktoren `:1938-1939`). `md_boost` ist regime-abgeleitet (Oracle/Fusion-Vokabular). Die beiden teilen sich weder Vokabular noch Invariante.

Der eigentliche Befund sitzt **innerhalb** des Oracle-Vokabulars: `global_mult` und `md_boost` sind zwei unabhängig geschriebene Funktionen **desselben** Regime-Labels, gelesen aus demselben Bias-Dict, und werden in `_dynamic_size` multipliziert — `f(regime) * g(regime)`, ohne gemeinsame Invariante.

- `global_mult` (`:1801-1830` → Produkt `:1887`) ist Fusion-`size_mult`, eine reine Funktion des Oracle-Zustands in `services/market_oracle/regime.py::policy_for_state` (`:157-189`): RISK_OFF 0.35 / NEUTRAL 0.85 / RISK_ON 1.0 / CRASH 0.0. Clamp `[0.0, 1.5]` bei `:1811`.
- `md_boost` kommt aus `risk/moderate_deploy.py::size_boost_for_regime` (`:95-144`), keyed auf den Regimenamen (nicht auf `size_mult`), gelesen aus `bias["regime"]` (`:1912-1917`; DCA-Zweig `:862-864`).
- Komposition `:1918-1919`: `if md_boost > 1.0 and global_mult > 0: total *= md_boost`. Gleichzeitig Anhebung der Clamp-Obergrenze via `effective_max_total_multiplier` (`:1920-1922`). Dieselbe Boost-Logik existiert ein zweites Mal auf dem DCA-Pfad (`:836-873`), dort ohne das übrige Produkt — nur `md_boost` (`:865-866`).

`Size applied ONCE` ist damit nicht wörtlich wahr: dasselbe Oracle-Vokabular geht zweimal in die Größe ein.

**Vertrag nach #376.** Unter `RISK_OFF`, `WARMUP`, `CRASH` gilt `md_boost == 1.0` per Vertrag:

- `_DERISK_REGIMES` (`moderate_deploy.py:34`) überspringt den Cash-Rich-Extra (`cash_rich_extra_mult`) für alle drei De-Risking-Regimes (vorher nur CRASH — `size_boost_for_regime` `:134-140`).
- `moderate_deploy_config` clampft `size_boost_risk_off` / `size_boost_warmup` / `size_boost_crash` auf `<= 1.0` (`:70-72`); Config kann keinen De-Risk-Boost mehr einführen. `size_boost_for_regime` clampft zusätzlich jeden Boost `< 1.0` auf 1.0 (`:129-130`) — unter den drei Regimes bleibt 1.0.
- Live-Config (`config.json` `risk.moderate_deploy`): `size_boost_risk_off` und `size_boost_warmup` sind jetzt `1.0` (waren 1.25). `size_boost_crash` war und bleibt 1.0.

Folge: unter diesen Regimes liefert `effective_max_total_multiplier` die Basis-Decke (`aggression.max_position_multiplier` 2.0, `config.json:146`) — `boost <= 1.001` → `base_max` (`moderate_deploy.py:155-156`). Der DCA-Zweig (`risk_manager.py:836-873`, der nur `md_boost` anwendet) vergrößert DCA-Adds unter RISK_OFF daher nicht mehr (vorher: effektiv 1.5925 cash-rich — `1.0 + (1.25 - 1.0) * dca_boost_scale 0.9 = 1.225`, danach `* cash_rich_extra_mult 1.3`).

**NEUTRAL ist beabsichtigt.** Unter NEUTRAL ist der zusammengesetzte Regime-Kanal `0.85 (global_mult) × 2.5 (md_boost, cash-rich, gedeckelt durch max_boost 2.5) = 2.125`. Rechnung: Live `size_boost_neutral` 2.0 × `cash_rich_extra_mult` 1.3 = 2.6, Clamp `max_boost` 2.5 (`config.json:439,447,449`) → 2.5. Die effektive Decke steigt auf `max_total_multiplier` 2.6 gegenüber `aggression.max_position_multiplier` 2.0. `size_boost_risk_on: 2.1 > 2.0` ist absichtlich — `max_total_multiplier` 2.6 ist ein expliziter Live-Override (`config.json:438,444`). Das ist **INTENDED**, kein Defekt. Wer das ändern will, braucht ein eigenes Ticket.

**Unberührt durch #376.** `size_boost_default` 1.35 bei degradiertem/fehlendem Orakel unter `fail_closed_guards: "log"` (`config.json:427,443`) ist eingefrorenes, beabsichtigtes Tier-1b-Verhalten (#299, `tests/unit/test_market_bias_degraded.py::test_log_degraded_keeps_default_boost_and_warns_once`) und wird von #376 **nicht** geändert.

---

## 4. Beobachtungen zur Fehlerbehandlung

Nicht zu beheben in diesem Ticket. Eine Funktion (`_dynamic_size`), drei Disziplinen:

- `risk/risk_manager.py:1836-1841` (`coin_bias`) → `self._guard_failed(...)`, fällt im Deny-Modus auf den **restriktivsten** Bias zurück (`_most_restrictive_coin_size_bias`, Default 0.5) — fail-closed.
- `risk/risk_manager.py:1906-1907` (`md_boost`) → explizit `md_boost = 1.0` — fail-neutral, hier korrekt (ein ausgefallener Boost darf nicht als De-Risking oder als stiller 1.0-Erfolg der Gegenseite verwechselt werden; der Faktor ist als Boost definiert).
- `risk/risk_manager.py:1862-1863` (Makro `calendar_mult` / `session_mult` / `pm_mult`) → nacktes `except Exception: pass`, lässt alle drei De-Risking-Eingänge still bei 1.0. **Fail-open — die schlechteste der drei**, weil diese Faktoren Größe **reduzieren** sollen (Pre-FOMC, Asia-Fakeout, PM-Mispricing). Genau dann, wenn der Lookup scheitert, fällt die Dämpfung aus.

Zusätzlich, ohne Fix:

- `risk/risk_manager.py:859-860` — eigenes nacktes `except Exception: pass` um Import/Aufruf von `moderate_deploy` auf dem DCA-Pfad (Boost entfällt still).
- `intelligence/macro/regime_rules.py:19` — der Docstring wirbt fail-open Size-Defaults (`1.0`), gegen die projektweite Konvention fail-closed-near-money.

---

## 5. Was keine Sizing-Schicht ist

`strategies/indicator_regime.py` sieht namentlich nach einer Regime-Schicht aus und ist **keine**. Eigener Docstring `:1-12`: Single Overlay für Regime-RSI plus `trail_exclusive` RSI-Punch-Through — **Exit-Seite**, Layer 4 der Sell-Kette (RSI-Schwellen, Force `SELL_FULL` auf RSI-Sources). Kill: `sell_policy.indicator_regime.enabled=false`. Es emittiert keinen Size-Multiplikator, hängt nicht in `_dynamic_size`, und gehört **nicht** in die Tabelle oben.
