# Hermes — Self-Improving Trading Agent

Stand: 15. Juni 2026

Diese Anleitung erklärt den **Hermes-Agenten** in einfacher Sprache: was er macht, wie er mit dem **Live-Bot** und dem **Volatile-Profil** zusammenspielt, und wie du alles in **Telegram nachvollziehst** — auch ohne Trading- oder Programmierkenntnisse.

---

## 1. Was ist Hermes?

Stell dir Hermes als einen **Assistenten vor, der deine Trading-Strategie ständig testet und verbessert** — ähnlich wie ein Wissenschaftler:

1. Er nimmt die **aktuelle Strategie** (z. B. „Kaufe ARIA bei RSI unter 30“).
2. Er ändert **genau einen Parameter** (z. B. RSI-Schwelle von 30 auf 28).
3. Er **simuliert** beide Varianten mit historischen Kursdaten.
4. Er entscheidet: **besser, gleich oder schlechter** — und merkt sich das Ergebnis.
5. Nur wenn die neue Variante **klar besser** ist, wird sie ins **Memory** übernommen (optional auch in `config.strategies[]`).

Hermes handelt **nicht selbstständig wild**. Er optimiert nur die Parameter deiner bestehenden RSI/Bollinger-Strategie. Bessere Werte landen in **`hermes/memory/baseline.json`** — und werden **sofort im Live-Bot genutzt**, auch wenn sie noch **nicht** in `config.strategies[]` stehen.

---

## 1b. Hermes vs. Live-Bot vs. Volatile-Profil — drei Rollen

| Rolle | Was ist das? | Analogie |
|-------|--------------|----------|
| **Live-Bot** | Kauft und verkauft alle 10 Minuten | Der Fahrer |
| **Hermes** | Testet Einstellungen im Hintergrund (~30 Min.) | Der Tüftler im Labor |
| **Volatile-Profil** | Extra-Ausstiegsregeln für hektische Coins | Sicherheitsgurt bei wilder Fahrt |

**Wichtig:** Hermes und der Live-Bot sind **nicht dasselbe Programm**, aber sie teilen sich das **Gedächtnis** (`baseline.json`).

### Welche Parameter gelten beim Handel?

Der Bot wählt in dieser Reihenfolge (siehe `strategies/registry.py`):

```
1. config.strategies[]     — du hast den Coin fest eingetragen (z. B. ARIA)
2. Hermes Memory         — gelernte Werte aus baseline.json (z. B. H, WLD)
3. + Volatile-Overlay      — nur bei offener Position + volatilem Coin
4. altcoin_social          — Trending-Coins ohne eigenes Profil
5. Standard-Defaults
```

**Promotion** (`sync_to_config: true` + erfolgreicher Test) schreibt Werte zusätzlich in `config.json` → `strategies[]`. Das ist **optional** für den Live-Betrieb — Memory allein reicht.

**Volatile-Profil** (`volatile_altcoin` in `config.json`) ist **unabhängig** von Hermes: Es legt BB-/Volumen-Verkaufsregeln **oben drauf**, wenn der Coin als volatile gilt. Profilname dann: `hermes_baseline+volatile`.

Aktuell: `volatile_altcoin.mode: shadow` — der Bot **zeigt** volatile Verkäufe in Telegram (`shadow->SELL_30`), führt sie aber **nicht aus**.

---

## 1c. Hermes Coin-Pool (Hybrid-Modus)

Hermes lernt nicht nur die Coins aus `hermes.symbols`. Im **Hybrid-Modus** (`symbols_mode: hybrid`) setzt sich der Pool so zusammen:

| Quelle | Beispiel | Bedeutung |
|--------|----------|-----------|
| **Pins** (`symbols_pin`) | ARIA/USDT | Immer dabei — dein Haupt-Coin |
| **Offene Positionen** | H/USDT, STG/USDT | Was du gerade hältst, wird mitoptimiert |
| **CMC Top-N** | aus Watchlist | Trending-Coins aus der Beobachtungsliste |

`hermes.symbols: ["ARIA", "H", …]` allein **pinnt** im Hybrid-Modus **nicht** — nur `symbols_pin` tut das. H landet im Pool, weil du eine Position hältst oder CMC ihn in die Watchlist hebt.

Maximal **8 Coins** gleichzeitig (`symbols_max: 8`). Rotation: `signal_activity` — Hermes bearbeitet zuerst Coins mit viel Signal-Aktivität.

---

## 2. Was ist neu? (Phase 2+)

| Feature | Kurz erklärt | Warum wichtig |
|---------|--------------|---------------|
| **Walk-Forward-Validierung** | Statt einen großen Zeitraum einmal zu testen, wird in **viele kleine Zeitfenster** (Folds) geteilt und jede Variante in jedem Fenster verglichen | Verhindert „Zufallstreffer“ — eine Einstellung muss in **mehreren** Perioden besser sein |
| **Grok-Härtung** | KI-Vorschläge laufen über einen stabilen Client mit **Wiederholungen** bei Fehlern | Weniger Abstürze; ohne API-Key fällt Hermes automatisch auf **einfache Heuristiken** zurück |
| **Skills (Gelerntes)** | Jeder Zyklus speichert eine **Lektion** („RSI hoch → schlechter“) mit Vertrauenswert | Spätere Experimente vermeiden bekannte Fehler |
| **Order-Provenance** | Trades, die mit Hermes-Parametern laufen, sind in der Order-Historie als **Hermes-Experiment** markiert | Du siehst später: *Welcher Trade kam von welcher Optimierung?* |

---

## 3. Ein Lernzyklus — Schritt für Schritt

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
│  Baseline   │ ──► │  Vorschlag   │ ──► │  Backtest beide │
│  (aktuell)  │     │  (1 Parameter)│     │  Varianten      │
└─────────────┘     └──────────────┘     └────────┬────────┘
                                                   │
                     ┌──────────────┐     ┌─────────▼────────┐
                     │  Skill +     │ ◄── │  Bewertung       │
                     │  Experiment  │     │  (promoted?)     │
                     │  speichern   │     └─────────┬────────┘
                     └──────────────┘               │
                                          ja ──────► Config + Telegram
```

**Ablauf in Worten:**

1. **Baseline laden** — aktuelle Parameter für Symbol/Zeitrahmen (z. B. `ARIA/USDT` auf `4h`).
2. **Vorschlag** — Grok (wenn `XAI_API_KEY` gesetzt) oder Heuristik wählt **einen** Parameter und einen neuen Wert.
3. **Backtest** — Walk-Forward über die letzten ~35 Tage, aufgeteilt in 7-Tage-Fenster mit 3-Tage-Verschiebung.
4. **Bewertung** — Variante muss u. a. in **≥ 60 % der Folds** besser sein und Mindest-Kennzahlen erfüllen.
5. **Übernahme** — bessere Parameter → `hermes/memory/baseline.json` (immer); optional zusätzlich → `config.strategies[]` bei Promotion.
6. **Lernen** — Experiment + Skill werden in `hermes/memory/` gespeichert.
7. **Live-Bot** — nutzt Memory **sofort** beim nächsten Zyklus (Kauf, Verkauf, Rebuy).

---

## 4. Walk-Forward — einfach erklärt

**Problem:** Ein Backtest über 35 Tage kann zufällig gut aussehen, obwohl die Strategie in Wirklichkeit instabil ist.

**Lösung:** Hermes teilt die 35 Tage in **überlappende Wochenfenster**:

```
|-- Fold 0: Tag 1–7 --|
      |-- Fold 1: Tag 4–10 --|
            |-- Fold 2: Tag 7–13 --|
                  ... usw.
```

- Jedes Fenster = **7 Tage** (`fold_days`)
- Verschiebung = **3 Tage** (`step_days`)
- Typisch entstehen **~10 Folds** bei 35 Tagen Lookback

**Promotion-Regeln (vereinfacht):**

| Kriterium | Standard-Wert | Bedeutung |
|-----------|---------------|-----------|
| Folds gewonnen | ≥ 60 % | Variante muss in mindestens 6 von 10 Folds besseren Sharpe haben |
| Aggregat-Sharpe | > Baseline | Im Schnitt über alle Folds besser |
| Erfolgskriterien | z. B. Sharpe ≥ 0.8, DD ≤ 15 %, WR ≥ 50 %, ≥ 5 Trades | Auch der Durchschnitt muss „gut genug“ sein |
| Drawdown pro Fold | max. +5 % vs. Baseline | Kein Fold darf die Strategie stark verschlechtern |

---

## 5. Grok, Heuristik und Skills

### Grok (optional)

Wenn `XAI_API_KEY` in der `.env` gesetzt ist:

- Grok schlägt den nächsten Parameter und Wert vor.
- Grok formuliert nach jedem Zyklus eine **Skill-Lektion**.
- Bei API-Fehlern: automatische **Wiederholung** (bis zu 3 Versuche), danach Fallback.

### Heuristik (immer verfügbar)

Ohne API-Key wählt Hermes zufällig einen noch nicht kürzlich getesteten Parameter und ändert ihn in kleinen Schritten (z. B. `rsi_sell_30`: 70 → 72).

### Skills

Gespeichert in `hermes/memory/skills.json`:

- **Muster:** z. B. „RSI sell 30 von 70 auf 72 verschlechterte Sharpe“
- **Confidence:** wie sicher Hermes ist (steigt bei wiederholter Bestätigung)
- **Verwendung:** Parameter mit schlechten Skills werden bei neuen Vorschlägen **vermieden**

---

## 6. Memory, Promotion und Order-Provenance

### Zwei Wege, wie Hermes-Werte live werden

| Weg | Wo gespeichert | Wann aktiv | Typisches Beispiel |
|-----|----------------|------------|-------------------|
| **Memory (Standard)** | `hermes/memory/baseline.json` | Sofort im Live-Bot | H/USDT ohne `strategies[]`-Eintrag |
| **Promotion** | zusätzlich `config.strategies[]` | Nach erfolgreichem Backtest + `sync_to_config` | ARIA — du willst Werte fest in der Config |

**Sell → Rebuy:** Memory bleibt erhalten. Du verlierst Hermes-Optimierung **nicht**, wenn du verkaufst und später wieder kaufst — solange du den Coin nicht manuell aus dem Memory löschst und kein expliziter `strategies[]`-Eintrag etwas anderes vorschreibt.

### Nachvollziehbarkeit in Orders

Bei Promotion enthält der Strategie-Eintrag `hermes_experiment_id` und `hermes_updated_at`. Orders können `source: hermes` tragen. In `/orders 3`:

```
Hermes  Experiment exp_65e4108a
```

Ohne Promotion siehst du in `/why H` oder `logs/decisions.jsonl` das Profil `hermes_baseline` bzw. `hermes_baseline+volatile`.

---

## 7. Einbindung in den Bot

### Automatisch (empfohlen)

In `config.json`:

```json
"hermes": {
  "enabled": true,
  "cycle_interval_sec": 1800
}
```

Wenn der Bot startet (`aria_bot.py`), läuft Hermes als **Hintergrund-Thread** alle 30 Minuten (1800 Sekunden) einen Zyklus.

### Manuell (CLI)

```bash
# Status anzeigen
python3 hermes_agent.py --status

# Einen Zyklus ausführen
python3 hermes_agent.py --once

# Sicher testen (eigene Demo-Dateien, keine Live-Daten)
python3 hermes_agent.py --once --demo

# Dauerhaft laufen lassen (ohne aria_bot)
python3 hermes_agent.py --interval 3600
```

### Telegram

| Befehl | Wirkung (für Einsteiger) |
|--------|--------------------------|
| `/hermes` | Technischer Status + **Klartext** zum letzten Zyklus |
| `/hermes_last` | Nur der letzte Lern-Zyklus in Alltagssprache |
| `/hermes_run` | Startet sofort einen Lernzyklus (du bekommst danach eine Meldung) |
| `/hermes_status` | Wie `/hermes` |
| `/why SYMBOL` | Letzte Trade-Entscheidung — inkl. Hermes-Experiment-ID am Coin |
| `/decisions` | Chronologisches Protokoll aller Bot-Entscheidungen |

### Automatische Hermes-Nachrichten in Telegram

Hermes meldet sich **von selbst** — du musst nicht ständig `/hermes` senden.

| Situation | Was du siehst (vereinfacht) |
|-----------|----------------------------|
| **Jeder Lern-Zyklus** (~30 Min.) | „Hermes-Test abgelehnt …“ oder Erklärung, warum nicht übernommen |
| **Promotion** (selten) | „Strategie übernommen“ — welcher Parameter geändert wurde und warum |
| **Live-Veto** | „Live-Schutz“ — Backtest war ok, echte Trades der letzten Tage sprechen dagegen |
| **Dual/Counterfactual** | Zusatzinfo mit „Was wäre passiert?“-PnL-Delta in USDT |

Jede Nachricht hat:
1. **Überschrift auf Deutsch** — was passiert ist
2. **Erklärung** — z. B. „Nur 1/4 Zeitfenster im Backtest waren besser“
3. **Technik-Zeile** — `rsi_sell_30 70->68 | verdict=rejected` (optional)

Steuerung in `config.json`:

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

Wenn `notify_hermes_every_cycle: false`, bekommst du nur Promotion und Live-Veto — keine Meldung bei abgelehnten Tests.

---

## 8. Konfiguration (`config.json` → `hermes`)

### Wichtigste Felder

| Feld | Beispiel | Erklärung |
|------|---------|-----------|
| `enabled` | `true` | Hermes im Bot aktivieren |
| `symbols` | `["ARIA/USDT"]` | Welche Coins optimiert werden |
| `timeframes` | `["4h"]` | Candle-Zeitrahmen |
| `tunable_params` | `rsi_buy_low`, … | Parameter, die Hermes ändern darf |
| `cycle_interval_sec` | `1800` | Pause zwischen Zyklen (Sekunden) |
| `sync_to_config` | `true` | Bessere Parameter automatisch in Bot-Strategie schreiben |
| `notify_on_promotion` | `true` | Telegram bei erfolgreicher Übernahme |

### Erfolgs- und Abbruchkriterien

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

### Walk-Forward

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

## 9. Speicherorte (Memory)

| Datei | Inhalt |
|-------|--------|
| `hermes/memory/baseline.json` | Aktuelle beste Parameter + Kennzahlen |
| `hermes/memory/experiments.json` | Historie aller Experimente |
| `hermes/memory/skills.json` | Gelernte Muster |

**Demo-Modus** (`--demo` oder `DEMO_MODE=1`): parallele Dateien `*.demo.json` — dein Live-Memory bleibt unberührt.

---

## 10. Konkrete Beispiele (30 Tage Praxis: H, ARIA, WLD)

Die folgenden Geschichten basieren auf echten Bot-Läufen (Mai–Juni 2026, Enhanced Dry Run). Sie zeigen, **wer welche Regeln bekommt** — ohne dass du Charts lesen musst.

### Beispiel 0 — H/USDT: Hermes Memory + Volatile, kein Config-Eintrag

**Situation:** Humanity (H) ist ein sehr volatiler Altcoin. Er steht **nicht** in `config.strategies[]`, aber Hermes hat ein Profil in `baseline.json` (z. B. `rsi_sell_30: 70`, `stop_loss_pct: 50`). Am 14.06. lief Hermes 30 Experimente für H — viele abgelehnt, **Memory trotzdem aktiv**.

**Zeitleiste (vereinfacht):**

| Wann | Ereignis | Was du verstehen sollst |
|------|----------|-------------------------|
| 13.06. | Kauf ~250 USDT @ ~0,28 $ | Einstieg — ab jetzt gelten Hermes-Parameter. |
| 13.06. | Kleiner Verkauf @ ~0,29 $ | Erste Gewinnmitnahme (+~2 USDT). |
| 14.06. 07:12 | Großer Auto-Verkauf @ ~0,48 $ | Pump erkannt → +~43 USDT realisiert. |
| 14.06. | Rebuy @ ~0,24 $ | Nach Dip wieder eingestiegen — **dieselben Hermes-Werte**. |
| 14.06. Abend | Zwei Teilverkäufe @ ~0,43–0,42 $ | Gestaffelte Exits (+~168 USDT). |
| 15.06. | Verkauf @ ~0,62 $, dann Rebuy @ ~0,35 $ | Weiterer Exit (+~186 USDT), Position neu aufgebaut. |

**Frage:** „Hat Hermes die Strategie nach dem Verkauf verloren?“  
**Antwort:** Nein. Memory in `baseline.json` gilt weiter — auch über Rebuys hinweg.

**Mit Shadow-Mode** (`volatile_altcoin.mode: shadow`) käme in Telegram zusätzlich z. B.:

```
Warum: Kurs am oberen Bollinger-Band — Verkauf wäre sinnvoll, läuft aber nur als Shadow.
shadow->SELL_30 | Profil: hermes_baseline+volatile
```

### Beispiel 0b — ARIA: Config schlägt Hermes

ARIA hat einen **festen** Eintrag in `config.strategies[]` (`rsi_sell_30: 72`, `take_profit_pct: 12`). Selbst wenn Hermes im Memory `70` vorschlägt, nutzt der Live-Bot **72** aus der Config.

| Merksatz | |
|----------|--|
| Du willst volle Kontrolle | Coin in `strategies[]` eintragen |
| Du willst, dass Hermes mitlernt | Coin **ohne** Eintrag halten (wie H) |

### Beispiel 0c — WLD: Wie H, aber weniger im Rampenlicht

WLD/USDT hat ebenfalls Memory in `baseline.json`, keinen `strategies[]`-Eintrag. Ablauf wie bei H: `hermes_baseline` ohne Position, `hermes_baseline+volatile` mit offener Position bei hohem ATR.

---

### Beispiel A — Erfolgreiche Promotion (in `config.strategies[]`)

**Ausgangslage:** Baseline `rsi_buy_low: 30`, Sharpe 0.95 über 10 Folds.

**Experiment:** Grok schlägt `rsi_buy_low: 28` vor.

**Ergebnis:**

- 7 von 10 Folds besser (70 % ≥ 60 %) ✓
- Aggregat-Sharpe 1.12 > 0.95 ✓
- Win Rate 54 %, Max Drawdown 11 %, 8 Trades ✓

**Was passiert:**

1. Telegram (Beispiel):
   ```
   🧠 Hermes — Strategie übernommen
   ✅ Hermes hat 'RSI Kauf unten' angepasst (30 -> 28) für ARIA/USDT.
   Der Bot hat die Einstellung im Backtest verbessert und übernimmt sie ins Live-Trading.
   Sharpe: 1.12 | WR: 54% | Folds: 7/10
   ```
2. `hermes/memory/baseline.json` wird aktualisiert (immer)
3. Bei `sync_to_config: true` auch `config.json` → Strategie für `ARIA/USDT` / `4h`
4. Nächster BUY auf ARIA nutzt `rsi_buy_low: 28`
5. Order-Detail zeigt später: `Hermes Experiment exp_a1b2c3d4`

---

### Beispiel B — Abgelehntes Experiment

**Experiment:** `rsi_sell_30: 70 → 72` (Heuristik, kein Grok-Key)

**Ergebnis:**

- 0 von 10 Folds besser
- Aggregat-Sharpe 0.0 (zu wenig Trades in den Folds)

**Ausgabe:**

```
Experiment exp_65e4108a: rsi_sell_30 70.0→72 → rejected
Won 0/10 folds (0% < 60%)
```

**Was passiert:**

- Baseline **bleibt** bei 70
- Skill wird gespeichert: „RSI sell 30 erhöhen → schlecht“
- Beim nächsten Zyklus wird `rsi_sell_30` eher **vermieden**

---

### Beispiel C — Telegram-Workflow

```
Du:  /hermes_last
Bot: 🧠 Hermes — letzter Zyklus
     🔬 Hermes-Test abgelehnt für H/USDT: RSI Verkauf Stufe 30% 70->68.
     Nur 1/4 Zeitfenster im Backtest waren besser — zu unsicher für eine Live-Änderung.
     rsi_sell_30 70->68 | verdict=rejected

Du:  /hermes
Bot: (technischer Status als <pre>-Block)
     🧠 In Klartext:
     (gleiche Erklärung wie /hermes_last)

Du:  /why H
Bot: ❓ Warum — H/USDT
     Letzte Entscheidung: SELL_30
     Warum: RSI überkauft — 30 % verkauft ...
     Hermes: Experiment exp_a1b2c3d4 (falls Parameter von Hermes stammen)
```

**Automatisch** (ohne Befehl, alle ~30 Min. bei `notify_hermes_every_cycle: true`):

```
🧠 Hermes — Lern-Zyklus
🔬 Hermes-Test abgelehnt für H/USDT: ...
```

---

### Beispiel D — Ohne vs. mit Grok

| Situation | Vorschlags-Quelle | Skill-Text |
|-----------|-------------------|------------|
| Kein `XAI_API_KEY` | `heuristic` — zufälliger Parameter ± kleiner Schritt | Template: „Changing rsi_sell_30 …“ |
| Mit `XAI_API_KEY` | `grok` — kontextbewusst (Baseline, Skills, Historie) | Grok formuliert Muster in natürlicher Sprache |

In beiden Fällen läuft der **gleiche strenge Backtest** — Grok beeinflusst nur die *Idee*, nicht die *Bewertung*.

---

## 11. Was Hermes **nicht** tut

- **Kein eigenes Trading:** Hermes platziert keine Orders direkt; er optimiert nur Strategie-Parameter.
- **Kein Ersatz für `strategies[]`:** Wenn du einen Coin fest in der Config hast, **gewinnt die Config** — Hermes-Memory wird dann ignoriert.
- **Kein Garant für Gewinn:** Backtests sind historisch — Zukunft kann anders sein.
- **Kein Multi-Parameter-Tuning:** Pro Zyklus nur **eine** Änderung (wissenschaftliche Methode).
- **Kein Volatile-Profil:** BB-/Volumen-Strukturregeln kommen aus `volatile_altcoin` — separates System, legt sich nur oben drauf.
- **Illiquide Coins:** Wenige Trades in 7-Tage-Folds → viele Ablehnungen (absichtlich konservativ).

---

## 12. Typische Probleme

| Symptom | Ursache | Lösung |
|---------|---------|--------|
| Immer `rejected`, 0 Trades | Zu wenig Signale auf dem Coin/Zeitrahmen | Normal bei volatilen Coins — **Memory aus letztem Stand** gilt trotzdem im Live-Bot |
| Live-Bot nutzt „falsche“ Werte | Coin in `strategies[]` eingetragen | Config-Eintrag entfernen oder anpassen — er hat Vorrang vor Memory |
| H nutzt nicht Hermes-Werte | Expliziter `strategies[]`-Eintrag für H | H-Eintrag aus Config entfernen; Memory in `baseline.json` prüfen |
| `Grok ... unavailable` | Kein API-Key | `XAI_API_KEY` in `.env` setzen — oder Heuristik nutzen |
| Keine Promotion trotz gutem Sharpe | Folds-Ratio unter 60 % | Strategie ist instabil über Zeitfenster — gewollt |
| Hermes läuft nicht | `hermes.enabled: false` | In `config.json` auf `true` setzen und Bot neu starten |

---

## 13. Live-Evidenz & Dual-Modus (v1.7+)

Hermes kann Walk-Forward-Ergebnisse mit dem **Dry-Run-Ledger** abgleichen und optional **Counterfactual-Replay** nutzen.

### Modi (`hermes.live_evidence.mode`)

| Modus | Verhalten |
|-------|-----------|
| `guardrail` | WF bleibt Primary; Live vetoed nur bei stark negativem `live_sell_pnl` |
| `dual` | Promotion via **WF** oder via **Live + Counterfactual** (Pfad B) |

### Pfad B (Dual) — Safety-Guards

Promotion ohne WF-Sieg nur wenn **alle** Bedingungen erfüllt:

- `live_trades >= 3`, `live_sell_trades >= 2`, `live_sell_pnl >= 0`
- Counterfactual **seeded** (Position aus `live_trade_history`, inkl. manueller BUY)
- `variant_sells >= min_counterfactual_sells` (Default 1)
- `counterfactual_pnl_delta > 0` und `>= min_live_pnl_delta_usdt` (Default 5)
- Variable in **Exit-Whitelist** (`take_profit_pct`, `rsi_sell_30/20`, CMC-Schwellen)
- `stop_loss_pct` nur via WF (`live_blocklist`)

### `take_profit_pct` lernen

Neu in `tunable_params`. Steuert `SELL_TP` (30 % Teilverkauf) in der TA-Strategie. Bounds: 5–30 %.

### Counterfactual manuell

```bash
python3 -m hermes.counterfactual --symbol H/USDT --from 2026-06-13T15:00:00 --to 2026-06-14T12:00:00
```

Experiment-Records enthalten `counterfactual_metrics` (Delta, seeded, seed_source).

---

## 14. Schnellreferenz

```bash
# Status
python3 hermes_agent.py --status

# Ein Zyklus (sicher)
python3 hermes_agent.py --once --demo

# Telegram
/hermes
/hermes_last
/hermes_run
/why SYMBOL
/decisions
```

**Dateien:** `hermes/` (Logik), `hermes/memory/` (Gedächtnis), `notifications/user_explain.py` (DE-Erklärungen), `config.json` → Blöcke `hermes` + `observability`.

**Showcase (alle Nachrichtentypen):** `python3 scripts/telegram_transparency_showcase.py`

---

*Weitere Bot-Dokumentation: [DOCUMENTATION.md](DOCUMENTATION.md) (Gesamtsystem, Transparenz-Glossar, alle Telegram-Befehle).*