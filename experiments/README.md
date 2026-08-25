# Experiments

SSOT for **live / paper experiments** on staging. Not a dump of every plan.

- **Hier:** eine Datei pro Experiment, plus optionalem Unterordner für Reports.
- **Nicht hier:** allgemeine Architektur (`plans/`), GIS-Rohauswertungen (`auswertungen/`), Code-Reviews (`plans/reviews/`).

Ältere Ticket-Skizzen liegen noch unter `plans/ticket-experiment-*.md`. Neue Experimente nur noch hier.

## Namenskonvention

```
experiments/YYYY-MM-DD_<slug>.md
experiments/<slug>/            # optional: Backtests, Scorecards, Kill-Notes
```

Beispiel: `2026-08-13_correlated-tier-rotation-v0.md`

## Pflicht-Abschnitte in jeder Datei

1. **Steckbrief** — Status, Branch, Tenant, Kill, PR/Issue
2. **Hypothese** — eine prüfbare Behauptung
3. **Was an / was aus** — Flags, Default in `config.json`, Tenant-Overlay
4. **Isolation** — warum Henry/default unberührt bleiben
5. **Erfolg / Kill** — Zahlen + Datum, nicht Bauchgefühl
6. **Related tickets** — GitHub-Issues mit kurzer Rolle

## Regeln

- Default in `config.json` bleibt **aus**, bis das Experiment gewonnen hat.
- Paper / `xagent_test` zuerst. Kein Production-Tenant.
- Ein Experiment = ein Tenant-Overlay oder ein klarer Flag-Satz. Nicht RelVol, LOB und Rotation in eine Datei.
- Kill steht oben, nicht am Ende.
