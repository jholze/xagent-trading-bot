# 60-Tage-Backtest: Relativvolumen-Zündung — Anleitung

Drei Dateien, alle laufbereit. Ich konnte den Lauf hier **nicht** ausführen (kein Netz zur
Gate-API aus der Cloud-Session, kein Netz aus dem Device-Bridge-Bash). Die Logik ist gegen
echte Gate-Kerzen getestet, der Datenlauf muss bei dir passieren.

| Datei | Zweck |
|-------|-------|
| `scripts/backtest_volume_ignition_60d.py` | Der Backtest |
| `tests/test_volume_ignition_backtest.py` | 13 Fixture-Tests, laufen ohne Netz |
| `tests/fixtures/ignition_1h.json` | Echte Gate-1h-Kerzen (IMU/HEI/BMT/AKE) |

## Erst die Tests

```bash
python3.13 tests/test_volume_ignition_backtest.py
# oder: python3.13 -m pytest tests/test_volume_ignition_backtest.py -v
```

13 Tests, alle grün. Wenn die nicht durchlaufen, stimmt etwas mit der Umgebung nicht —
dann bitte gar nicht erst den Backtest starten.

## Dann der Lauf

```bash
# 1) Basislauf. Erster Durchlauf holt die Daten (ccxt/Gate, ~400 Coins × 60d 1h)
#    und legt sie unter auswertungen/cache/ignition/ ab. Rechne mit 20-40 min.
python3.13 scripts/backtest_volume_ignition_60d.py --days 60 --max-symbols 400

# 2) Sensitivitäts-Sweep. Läuft auf dem Cache, also schnell.
python3.13 scripts/backtest_volume_ignition_60d.py --days 60 --sweep

# 3) Die Kernthese isoliert: nur Coins UNTER dem Produktivfilter
python3.13 scripts/backtest_volume_ignition_60d.py --days 60 --max-baseline-vol24 500000
```

Output landet als JSON in `auswertungen/gis/volume_ignition_backtest_*.json`.
Schick mir das JSON, dann werte ich es aus.

## Was der Backtest anders macht als meine erste Analyse

Meine 4-Coin-Analyse war eine Mechanismus-Illustration mit rückblickend gewählten Gewinnern.
Der Backtest nimmt **alle** Coins und **alle** Signale. Konkret abgesichert:

| Bias | Gegenmaßnahme |
|------|---------------|
| Lookahead | Baseline nur aus Stunden **vor** dem Signal; Entry zum **Open der Folgestunde** (man kennt das Stundenvolumen erst, wenn die Stunde vorbei ist). Test `test_causality_no_lookahead` schneidet die Zukunft weg und prüft, dass dasselbe Signal entsteht. |
| Selektion | Kein Vorfiltern auf Gewinner. Cooldown je Coin, sonst wird jedes Signal genommen. |
| Survivorship | Universum = heute gelistete Paare; seither delistete fehlen. **Nicht behebbar**, wird im Report als Caveat ausgewiesen. Verzerrt nach oben. |
| Kosten | 0,2 % Round-Trip + 25 bps Slippage je Seite. |
| Füllbarkeit | Ticket ≤ 2 % des Zündungsstunden-Volumens. Bei IMU heißt das: **149 USDT statt 500**. Signale unter `--min-ticket` werden verworfen und **gezählt**. |
| Kapital | Chronologische Portfolio-Sim mit `max_open`-Slots. Signale bei vollem Portfolio werden verworfen und gezählt. Ein Signal-Mittelwert ohne Slot-Restriktion ist eine Fantasiezahl. |
| Regime | Alles zusätzlich je 15-Tage-Bucket. |
| Overfitting | Sweep über `mult` × `win` × Exit-Policy. |

## Der wichtigste Benchmark

Das Script rechnet **„gleiche Coins, zufälliger Einstiegszeitpunkt"** mit. Das trennt
*„das Signal timed richtig"* von *„diese Coins liefen sowieso"* — und ist die Zahl, an der
die ganze These hängt. Ausgabe:

```
>>> Timing-Edge (avg Signal - avg Zufall): +X.XX Prozentpunkte
```

Ist die nahe null, ist das Relativvolumen kein Timing-Signal, sondern nur ein
Coin-Auswahlfilter. Dann wäre die These in ihrer starken Form widerlegt.

## Was ich beim Testen gefunden habe

Zwei echte Fehler, die ohne Fixture-Test durchgerutscht wären:

1. **rvol-Explosion.** Schlafende Coins haben Stunden ganz ohne Umsatz, der Median läuft
   gegen null, jede Kleinorder sieht wie ein Ausbruch aus — IMU kam im ersten Lauf auf
   **15.094x**. Über 400 Coins hätte das die Ergebnisse mit Müll aus toten Paaren geflutet.
   Naheliegende Lösung (Zählfilter „mindestens X % der Baselinestunden mit Umsatz") ist
   **falsch**: IMU hatte 6 von 12 Nullstunden und wäre komplett verschwunden — der größte
   Gewinner des Fensters. Richtig ist eine Untergrenze für den Nenner (`--baseline-floor`)
   plus die absolute Hürde `--min-ign-qvol` gegen echte Leichen.

2. **Phantomfills am Stop.** Öffnete eine Stunde bereits unter dem Stop, wurde trotzdem
   zum Stoplevel gefüllt. Jetzt gap-aware zum Open.

Außerdem gingen die Skip-Zähler im Nulltrade-Fall verloren — also genau dann, wenn sie die
interessanteste Information sind.

## Erwartungsmanagement

Der bereits vorhandene 30-Tage-Backtest (`2026-08-05_gainer_entry_policy_30d.md`) zeigt für
naives „gestern-Top-kaufen" bei 24 h Haltedauer nur **−1,3 %** bzw. **+1,9 %** im Schnitt.
Wenn das Relativvolumen-Signal keinen deutlichen Abstand dazu und zum Zufalls-Timing schafft,
ist es die Umbauarbeit nicht wert.

Der wahrscheinlichste Ausgang nach dem, was ich in den Fixtures sehe: das Signal trifft,
aber die **realisierbare Ticketgröße** wird zum Flaschenhals. Bei IMU waren es 149 USDT bei
einem Wunsch von 500. Ein Signal mit +200 % auf 149 USDT ist +300 USDT — nett, aber es trägt
kein Portfolio. Die ehrliche Frage, die der Lauf beantworten muss, ist deshalb nicht
„funktioniert das Signal", sondern **„wie viel Kapital lässt sich damit überhaupt einsetzen"**.
Deswegen gibt der Report `median_realizable_ticket` und `skipped_too_illiquid` mit aus.

Und ein Nebenergebnis der Fixtures, das zum BLESS-Fall passt: `trail20` macht aus denselben
drei Trades **+16 %** statt **+176 %** bei `fix48`. Der Exit ist weiter der teuerste Teil.
