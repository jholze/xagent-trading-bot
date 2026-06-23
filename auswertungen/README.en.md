# Reports (`auswertungen`)

**Language:** [Deutsch](README.md) · English

Periodic bot reports land here (overnight runs, weekly reviews, incident analyses).

## Format: Markdown (`.md`)

**Recommendation: `.md` instead of `.txt`**

| Format | Advantage | Disadvantage |
|--------|-----------|--------------|
| **`.md`** | Tables, headings, readable in GitHub/Cursor | Slightly more syntax |
| `.txt` | Maximum simplicity | No structure, tables are messy |
| `.json` | Machine-readable | Hard to read |

For manual analyses like overnight reports, **Markdown** is the best compromise — still plain text, versionable in Git.

## Filenames

```
YYYY-MM-DD_<type>.md
```

Examples:

- `2026-06-13_nacht.md` — overnight report
- `2026-06-14_tag.md` — daily report (automatic)
- `2026-06-14_woche.md` — weekly review
- `2026-06-15_incident_portfolio.md` — bug/incident analysis

## Daily report (automatic)

```bash
python3 scripts/daily_auswertung.py
```

Optional:

```bash
python3 scripts/daily_auswertung.py --date 2026-06-14
python3 scripts/daily_auswertung.py --bot-dir /path/to/trading_bot
```

Creates `auswertungen/YYYY-MM-DD_tag.md` from JSON files in the bot directory.

## Content checklist

Each report should include at minimum:

1. **Time range & context** (version, mode, config flags)
2. **Summary** (one paragraph)
3. **Trades & portfolio** (numbers, table)
4. **Bot activity** (cycles, signals)
5. **Causes** (why something happened or didn't)
6. **Notable issues / bugs**
7. **Recommendations / next steps**
8. **Reference files** (which JSON/logs mattered)

Template: `_vorlage.md`

## Understanding bot behavior

When a report should explain **why** the bot sold or blocked:

- [DOCUMENTATION.en.md §18](../DOCUMENTATION.en.md#18-changelog--decision-guide-version-20) — decision tree, common confusion
- `logs/decisions.jsonl` — per cycle: `action`, `sources`, `exit_ladder_step`, `timeframe`, `strategy_profile`
- Telegram: `/why SYMBOL`, `/decisions`, `/ask`

## Data sources (typical)

- `live_trade_history.json`
- `orders.live.json`
- `positions.live.json` (incl. `exit_ladder_step`, `sold_percent`)
- `cmc_posts.json`
- `config.json`
- `logs/decisions.jsonl` — every bot decision with rationale (`/decisions`)
- `hermes/memory/experiments.json` — Hermes learning cycles (`/hermes_last`)
- `hermes/memory/baseline.json` — learned per-coin params (live fallback without `strategies[]`)
- `config.json` → `volatile_altcoin` — shadow/live mode for hectic altcoins
- `bot.log` (if current)

## Telegram vs. daily report

| Channel | When | For whom |
|---------|------|----------|
| **Telegram** (live) | Every 10 min. cycle summary, on trades immediately | Quick overview, **"Why:"** explanations |
| **`/decisions`** | On demand, recent entries | “What did the bot decide when?” |
| **`auswertungen/*_tag.md`** | Daily 23:55 (cron) | Day recap with tables, Hermes section |

All three complement each other: Telegram for the moment, `/decisions` for detail, Markdown report for archiving.