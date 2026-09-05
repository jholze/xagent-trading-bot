# Umbau-Konzept — Kasse, Kassenbuch, Wetterdienst, Gegenrichtung, Kontext, Muster, Autonomie

**Stand:** 5. September 2026 · **Basis:** `staging` @ `1b40c69` (PR #297) · **Tracking:** Epic #309

Dieses Verzeichnis ist die Quelle der Wahrheit für den Umbau. Es ersetzt `ARCHITECTURE_PLAN.md` (Juni 2026, Redis-3+1-Split) — der liegt archiviert unter `archive/superseded/`.

## Leseordnung

| Dokument | Was drinsteht | Status |
|---|---|---|
| [`konzept-regime-strategie-v3.md`](konzept-regime-strategie-v3.md) | Jesse-Analyse, Order-Lebenszyklus, Kostenmodell-Design, Regime-Strategie, Shorts — **Abschnitte 1–11 werden von v4 referenziert** | aktiv |
| [`konzept-regime-strategie-v4.md`](konzept-regime-strategie-v4.md) | Abgleich gegen die Codebasis, Erweiterung auf 7 Phasen, Code-Kritik (Teil F) | **aktiv — Leitdokument** |
| [`phase1-kasse.md`](phase1-kasse.md) | Aufgabenliste Phase 1 mit C/G-Delegation und Abnahmekriterien | aktiv |
| [`audit-exceptions-phase1.md`](audit-exceptions-phase1.md) | 204 Exception-Stellen in geldrelevanten Dateien, heuristisch vorklassifiziert — Arbeitsgrundlage für Phase 1 §1a | aktiv |
| [`archive/konzept-regime-strategie-v2.md`](archive/konzept-regime-strategie-v2.md) | Vorversion | superseded |

v4 legt sich über v3, ersetzt es nicht. Wer den Umbau versteht, hat v4 gelesen und v3 als Nachschlagewerk daneben.

## Die sieben Phasen

| Phase | Kurzname | Ergebnis | Art |
|---|---|---|---|
| 0 | Fundament | Testsuite vollständig, CI, isolierte Test-DB — Voraussetzung für alles Folgende | Härtung |
| 1 | Kasse | Echte Orders, korrekte Antworten, korrekte Kosten, ein Codepfad für Paper und Live | Härtung |
| 2 | Kassenbuch | Vollständiges, steuertaugliches Ledger aus Börsendaten + Kontext-Achsen | Bauarbeit |
| 3 | Wetterdienst | Regime-Bias aus den zwei bestehenden Modulen, zusammengeführt | Umbau |
| 4 | Gegenrichtung | Proaktive Shorts auf Futures, Hebel ≤ 2x, Börsen-Stop | Umbau + Neubau |
| 5 | Kontext-Layer | Externe Datenquellen mit Adapter-Abstraktion, LLM-Veto | Erweiterung |
| 6 | Musterdatenbank | Kontext-Achsen auswerten, Regeln explizit programmieren | Forschung |
| 7 | Autonome Weiterentwicklung | Hermes einhegen: Bänder, Journal, Kostenmodell, Live-Veto | Einhegung |

Phase 0 stammt aus dem September-Review (Epic #309): Phase 1 verlangt „pytest grün“ als Definition of Done — das ist heute nicht erfüllt (6 rot, 13 Money-Path-Tests per `collect_ignore` ausgeschlossen, keine CI).

Nichts aus Phase 5–7 ändert die Reihenfolge von 0–4. Solange das Kostenmodell 15-fach daneben liegt, optimiert Hermes auf Rauschen.

## Zusammenspiel mit dem September-Review

Das Review (5 Fachlinien + Grok-Intent-Klärung, 3./4.9.) hat unabhängig dieselben Kernbefunde erhoben wie v4 Teil A/F — Kostenmodell, Fail-Open, fehlende Börsen-Recovery — und zusätzlich Lücken gefunden, die v4 nicht hatte: Slot-Eviction (verkauft und lehnt dann ab), `exposure_multiplier` nie gelesen, `peak_equity` nie geschrieben, Stale-Price-Cache ohne TTL, Zwei-Writer bei Redeploy. Diese sind als Tickets in Phase 0/1/2 eingeordnet. Wo v4 präziser war (Fail-Open-Trennung Kontext vs. Geld, Order-Lebenszyklus, Hermes-Verwerfungs-Bias durch 3 % Round-Trip), gilt v4.

## Arbeitsweise

Ein Branch pro Ticket, Grok implementiert, Claude reviewt — Details in [`CLAUDE.md`](../../CLAUDE.md). Geldrelevante Änderungen (`risk/`, `execution/`, `aria_bot.py`, Ledger) werden zeilenweise auditiert, nie direkt gemerged.
