# Auswertungen

**Sprache:** Deutsch · [English](README.en.md)

Hier landen periodische Bot-Auswertungen (Nacht-Läufe, Wochenreviews, Incident-Analysen).

## Format: Markdown (`.md`)

**Empfehlung: `.md` statt `.txt`**

| Format | Vorteil | Nachteil |
|--------|---------|----------|
| **`.md`** | Tabellen, Überschriften, gut lesbar in GitHub/Cursor/Telegram-Copy | Minimal mehr Syntax |
| `.txt` | Maximale Einfachheit | Keine Struktur, Tabellen unübersichtlich |
| `.json` | Maschinenlesbar, automatisierbar | Schlecht zum Lesen |

Für manuelle Analysen wie Nacht-Reports ist **Markdown** der beste Kompromiss — weiterhin Plain Text, versionierbar in Git, in jedem Worktree nutzbar.

## Dateinamen

```
YYYY-MM-DD_<typ>.md
```

Beispiele:

- `2026-06-13_nacht.md` — Overnight-Auswertung
- `2026-06-14_tag.md` — Tages-Auswertung (automatisch)
- `2026-06-14_woche.md` — Wochenreview
- `2026-06-15_incident_portfolio.md` — Bug-/Incident-Analyse

## Tägliche Auswertung (automatisch)

```bash
python3 scripts/daily_auswertung.py
```

Optional:

```bash
python3 scripts/daily_auswertung.py --date 2026-06-14
python3 scripts/daily_auswertung.py --bot-dir /pfad/zum/trading_bot
```

Erzeugt `auswertungen/YYYY-MM-DD_tag.md` aus den JSON-Dateien im Bot-Verzeichnis.

**Cron** (täglich 23:55, mit Log in `logs/daily_auswertung_cron.log`):

```bash
# Einmalig installieren:
(crontab -l 2>/dev/null; echo '55 23 * * * /Users/jholze/Documents/scripts/trading_bot/scripts/cron_daily_auswertung.sh') | crontab -

# Manuell testen:
bash scripts/cron_daily_auswertung.sh
```

## Inhalt (Checkliste)

Jede Auswertung sollte mindestens enthalten:

1. **Zeitraum & Kontext** (Version, Modus, Config-Flags)
2. **Kurzfassung** (1 Absatz)
3. **Trades & Portfolio** (Zahlen, Tabelle)
4. **Bot-Aktivität** (Zyklen, Signale)
5. **Ursachen** (warum etwas passiert / nicht passiert ist)
6. **Auffälligkeiten / Bugs**
7. **Empfehlungen / nächste Schritte**
8. **Referenz-Dateien** (welche JSON/Logs relevant waren)

Vorlage: `_vorlage.md`

## Bot-Verhalten nachvollziehen

Wenn eine Auswertung erklärt, **warum** der Bot verkauft oder blockiert hat:

- [DOCUMENTATION.md §18](../DOCUMENTATION.md#18-changelog--entscheidungshilfe-version-20) — Entscheidungsbaum, typische Verwirrung
- `logs/decisions.jsonl` — pro Zyklus: `action`, `sources`, `exit_ladder_step`, `timeframe`, `strategy_profile`
- Telegram: `/why SYMBOL`, `/decisions`, `/ask`

## Datenquellen (typisch)

- `live_trade_history.json`
- `orders.live.json`
- `positions.live.json` (inkl. `exit_ladder_step`, `sold_percent`)
- `cmc_posts.json`
- `config.json`
- `logs/decisions.jsonl` — jede Bot-Entscheidung mit Rationale (auch in Telegram: `/decisions`)
- `hermes/memory/experiments.json` — Hermes-Lernzyklen (auch: `/hermes_last`)
- `hermes/memory/baseline.json` — gelernte Parameter pro Coin (Live-Fallback, auch ohne `strategies[]`)
- `config.json` → `volatile_altcoin` — Shadow/Live-Modus für hektische Altcoins
- `bot.log` (falls aktuell)

## Telegram vs. Tages-Report

| Kanal | Wann | Für wen |
|-------|------|---------|
| **Telegram** (live) | Alle 10 Min. Zyklus-Summary, bei Trades sofort | Schneller Überblick, **„Warum:“**-Erklärungen |
| **`/decisions`** | On-demand, letzte Einträge | „Was hat der Bot wann entschieden?“ |
| **`auswertungen/*_tag.md`** | Täglich 23:55 (Cron) | Tagesrückblick mit Tabellen, Hermes-Abschnitt |

Die drei ergänzen sich: Telegram für den Moment, `/decisions` für Details, Markdown-Report für die Archivierung.