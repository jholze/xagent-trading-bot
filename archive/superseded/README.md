# Superseded documents

Konvention für abgelöste Planungs- und Konzeptdokumente.

1. **Datei behält ihren Namen** und wandert nach `archive/superseded/` (bzw. `docs/<thema>/archive/` für themengebundene Vorversionen). Der Name bleibt, damit `git log --follow`, alte Links und Grep weiter funktionieren — das Verzeichnis ist das Signal.
2. **Erste Zeile wird ein Banner:** `> ⚠️ **SUPERSEDED · <datum>** — ersetzt durch [<nachfolger>](<pfad>). <ein Satz warum>. Historisch, nicht mehr pflegen.`
3. **Referenzen umbiegen:** Links in README/DOCUMENTATION/plans auf den neuen Pfad zeigen lassen.
4. **Nicht löschen** vor Ablauf eines Release-Zyklus (wie `archive/root_orphans/`).

| Datei | abgelöst am | durch |
|---|---|---|
| `ARCHITECTURE_PLAN.md` | 2026-09-05 | `docs/umbau/README.md` |
