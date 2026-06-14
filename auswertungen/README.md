# Auswertungen

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

**Cron-Beispiel** (täglich 23:55):

```
55 23 * * * cd /Users/jholze/Documents/scripts/trading_bot && python3 scripts/daily_auswertung.py
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

## Datenquellen (typisch)

- `live_trade_history.json`
- `orders.live.json`
- `positions.live.json`
- `cmc_posts.json`
- `config.json`
- `bot.log` (falls aktuell)