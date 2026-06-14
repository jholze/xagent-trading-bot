"""Central command usage hints for Telegram (DE layperson text + English command names)."""

USAGE = {
    "add": {
        "menu_description": "Coin zur Watchlist hinzufügen (Symbol ergänzen)",
        "hint": (
            "❌ <b>/add</b> — Coin zur Beobachtungsliste hinzufügen\n\n"
            "So geht's: <code>/add SYMBOL</code>\n"
            "Beispiel: <code>/add RAVE</code> oder <code>/add SOL</code>"
        ),
        "help_line": "<code>/add SYMBOL</code> — Coin beobachten (z.B. <code>/add RAVE</code>)",
    },
    "remove": {
        "menu_description": "Coin von der Watchlist entfernen (Nummer ergänzen)",
        "hint": (
            "❌ <b>/remove</b> — Coin von der Liste entfernen\n\n"
            "Zuerst <code>/list</code> senden, dann Nummer wählen.\n"
            "Beispiel: <code>/remove 2</code>"
        ),
        "help_line": "<code>/remove NUMMER</code> — Coin entfernen (zuerst <code>/list</code>, z.B. <code>/remove 2</code>)",
    },
    "list": {
        "menu_description": "Alle beobachteten Coins anzeigen",
        "help_line": "<code>/list</code> — Alle beobachteten Coins anzeigen",
    },
    "buy": {
        "menu_description": "Coin kaufen — Liste oder Betrag ergänzen",
        "hint": (
            "❌ <b>/buy</b> — Coin kaufen (Paper oder Live)\n\n"
            "Zuerst <code>/buy</code> ohne Parameter → nummerierte Coin-Liste (gleich wie <code>/list</code>).\n"
            "Dann: <code>/buy NUMMER USDT</code> — Risk Manager zeigt Vorschau, Bestätigung per Button.\n"
            "Beispiele:\n"
            "• <code>/buy 1 25</code> — Coin Nr. 1, 25 $\n"
            "• <code>/buy ARIA 200</code> — 200 $ in ARIA investieren"
        ),
        "help_line": "<code>/buy NUMMER USDT</code> — Kaufen (Nummer aus <code>/buy</code> oder <code>/list</code>, z.B. <code>/buy 1 25</code>)",
    },
    "sell": {
        "menu_description": "Position verkaufen — Nummer und Prozent ergänzen",
        "hint": (
            "❌ <b>/sell</b> — Anteil einer Position verkaufen\n\n"
            "Nummern sind in <code>/positions</code> und <code>/sell</code> gleich (nach Wert sortiert).\n"
            "Dann: <code>/sell NUMMER PROZENT</code> — Vorschau mit Bestätigen/Abbrechen.\n"
            "Beispiel: <code>/sell 1 30</code> — 30 % von Position 1 verkaufen"
        ),
        "help_line": "<code>/sell NUMMER PROZENT</code> — Verkaufen (Nummer aus <code>/positions</code> oder <code>/sell</code>, z.B. <code>/sell 1 30</code>)",
    },
    "positions": {
        "menu_description": "Portfolio, Kurse und PnL",
        "help_line": "<code>/positions</code> oder <code>/portfolio</code> — Portfolio mit Kurse, PnL und letzte Trades",
    },
    "orders": {
        "menu_description": "Auftragshistorie und Order-Details",
        "hint": (
            "❌ <b>/orders</b> — Auftragshistorie (Order-Ledger)\n\n"
            "<code>/orders</code> — Letzte Orders mit 24h-Statistik\n"
            "<code>/orders page 2</code> — Seite 2 (5 Orders pro Seite)\n"
            "<code>/orders 3</code> — Details zu Order Nr. 3"
        ),
        "help_line": "<code>/orders</code> — Auftragshistorie; <code>/orders NUMMER</code> für Details",
    },
    "risk": {
        "menu_description": "Risiko-Limits und Drawdown",
        "help_line": "<code>/risk</code> — Risiko-Limits, Drawdown und Positionsgröße anzeigen",
    },
    "mode": {
        "menu_description": "Handelsmodus anzeigen und wechseln",
        "hint": (
            "❌ Unbekannter Modus.\n\n"
            "Sende <code>/mode</code> für die aktuelle Einstellung und alle Optionen:\n"
            "• <code>/mode paper</code> — Virtuelles Geld (Standard)\n"
            "• <code>/mode live</code> — Echtes Geld auf Gate.io (braucht <code>/live_confirm</code>)\n"
            "• <code>/mode off</code> — Nur Analyse, kein Handel"
        ),
        "help_line": "<code>/mode</code> — Handelsmodus anzeigen; <code>/mode paper|live|off</code> zum Wechseln",
    },
    "live_confirm": {
        "menu_description": "Live-Handel auf Gate.io bestätigen",
        "help_line": "<code>/live_confirm</code> — Live-Handel auf Gate.io Mainnet bestätigen",
    },
    "live_cancel": {
        "menu_description": "Live-Handel abbrechen, zurück zu Paper",
        "help_line": "<code>/live_cancel</code> — Live-Handel abbrechen, zurück zu Paper",
    },
    "gate": {
        "menu_description": "Gate.io API-Status und Balance",
        "help_line": "<code>/gate</code> — Gate.io API-Status, Balance, Spot-Bestände",
    },
    "dryrun": {
        "menu_description": "Enhanced Dry Run Status",
        "help_line": "<code>/dryrun</code> — Enhanced Dry Run (Sim-Wallet, Trending-Coins)",
    },
    "maxpositions": {
        "menu_description": "Max. offene Positionen anzeigen/setzen",
        "hint": (
            "❌ <b>/maxpositions</b> — Limit für gleichzeitige Positionen\n\n"
            "Aktuellen Wert: <code>/maxpositions</code>\n"
            "Setzen: <code>/maxpositions ANZAHL</code>\n"
            "Beispiel: <code>/maxpositions 10</code>"
        ),
        "help_line": "<code>/maxpositions ANZAHL</code> — Max. offene Positionen setzen (z.B. <code>/maxpositions 10</code>)",
    },
    "sandbox": {
        "menu_description": "Strategie-Sandbox-Experimente",
        "help_line": "<code>/sandbox</code> — Strategie-Experimente anzeigen (automatisch aus X-Posts)",
    },
    "sandbox_results": {
        "menu_description": "Sandbox-Experiment-Details (ID ergänzen)",
        "hint": (
            "❌ <b>/sandbox_results</b> — Details zu einem Experiment\n\n"
            "Zuerst <code>/sandbox</code> für die ID-Liste.\n"
            "Beispiel: <code>/sandbox_results hyp_abc123</code>"
        ),
        "help_line": "<code>/sandbox_results ID</code> — Metriken eines Experiments (z.B. <code>/sandbox_results hyp_abc123</code>)",
    },
    "sandbox_promote": {
        "menu_description": "Erfolgreiche Sandbox-Strategie übernehmen",
        "hint": (
            "❌ <b>/sandbox_promote</b> — Erfolgreiche Strategie aktivieren\n\n"
            "Zuerst <code>/sandbox_results ID</code> prüfen.\n"
            "Beispiel: <code>/sandbox_promote hyp_abc123</code>"
        ),
        "help_line": "<code>/sandbox_promote ID</code> — Strategie übernehmen (z.B. <code>/sandbox_promote hyp_abc123</code>)",
    },
    "backtest": {
        "menu_description": "Strategie-Backtest-Status",
        "help_line": "<code>/backtest</code> — Adaptiver Strategie-Backtest (Scheduling)",
    },
    "backtest_lock": {
        "menu_description": "Backtest für Coin sperren (Symbol ergänzen)",
        "help_line": "<code>/backtest_lock SYMBOL</code> — Backtest-Zeitplan für Coin fixieren",
    },
    "backtest_results": {
        "menu_description": "Backtest-Ergebnis anzeigen (Symbol ergänzen)",
        "help_line": "<code>/backtest_results SYMBOL</code> — Letztes Backtest-Ergebnis",
    },
    "hermes": {
        "menu_description": "Hermes Lern-Agent Status",
        "hint": (
            "🧠 <b>Hermes Self-Improvement Agent</b>\n\n"
            "<code>/hermes</code> — Baseline + letzte Experimente\n"
            "<code>/hermes_last</code> — Letzter Zyklus in Klartext\n"
            "<code>/hermes_run</code> — Einen Lern-Zyklus starten"
        ),
        "help_line": "<code>/hermes</code> — Status | <code>/hermes_last</code> — letzte Entscheidung | <code>/hermes_run</code> — Zyklus",
    },
    "hermes_last": {
        "menu_description": "Letzte Hermes-Entscheidung in Klartext",
        "help_line": "<code>/hermes_last</code> — Letzter Hermes-Zyklus erklärt",
    },
    "hermes_run": {
        "menu_description": "Einen Hermes-Lernzyklus starten",
        "help_line": "<code>/hermes_run</code> — Hermes-Lernzyklus manuell ausführen",
    },
    "decisions": {
        "menu_description": "Letzte Bot-Entscheidungen",
        "hint": (
            "📜 <b>/decisions</b> — Was der Bot wann entschieden hat\n\n"
            "<code>/decisions</code> — Letzte 8 Entscheidungen\n"
            "<code>/why SYMBOL</code> — Erklärung für einen Coin (z.B. <code>/why H</code>)"
        ),
        "help_line": "<code>/decisions</code> — Entscheidungsprotokoll | <code>/why SYMBOL</code> — Warum für einen Coin",
    },
    "why": {
        "menu_description": "Warum? — Erklärung für einen Coin (Symbol ergänzen)",
        "help_line": "<code>/why SYMBOL</code> — Letzte Entscheidung für einen Coin erklären",
    },
    "cmc": {
        "menu_description": "CMC Community-Sentiment",
        "help_line": "<code>/cmc</code> — CoinMarketCap Community-Stimmung (Sentiment-Signale)",
    },
    "addx": {
        "menu_description": "X-Account zum Monitoring hinzufügen",
        "hint": (
            "❌ <b>/addx</b> — X/Twitter-Account zum Monitoring hinzufügen\n\n"
            "So geht's: <code>/addx ACCOUNT</code>\n"
            "Beispiel: <code>/addx CryptoCapo_</code>"
        ),
        "help_line": "<code>/addx ACCOUNT</code> — X-Account hinzufügen (z.B. <code>/addx CryptoCapo_</code>)",
    },
    "removex": {
        "menu_description": "X-Account entfernen (Account ergänzen)",
        "hint": (
            "❌ <b>/removex</b> — X-Account entfernen\n\n"
            "So geht's: <code>/removex ACCOUNT</code>\n"
            "Beispiel: <code>/removex CryptoCapo_</code>"
        ),
        "help_line": "<code>/removex ACCOUNT</code> — X-Account entfernen (z.B. <code>/removex CryptoCapo_</code>)",
    },
    "listx": {
        "menu_description": "Überwachte X-Accounts mit Trust-Score",
        "help_line": "<code>/listx</code> — Alle überwachten X-Accounts mit Trust-Score",
    },
    "xposts": {
        "menu_description": "Letzte analysierte X-Posts",
        "help_line": "<code>/xposts</code> — Letzte analysierte X-Posts und Empfehlungen",
    },
    "xsignals": {
        "menu_description": "Aktuelle starke X-Signale",
        "help_line": "<code>/xsignals</code> — Aktuelle starke X-Signale (BUY/SELL)",
    },
    "xaccuracy": {
        "menu_description": "X-Account Trefferquote (Leaderboard)",
        "help_line": "<code>/xaccuracy</code> — Trefferquote der X-Accounts (Leaderboard)",
    },
    "tracktest": {
        "menu_description": "Test-Tweet durch Analyzer schicken",
        "help_line": "<code>/tracktest</code> — Test-Tweet sofort durch den Analyzer schicken",
    },
    "testaccount": {
        "menu_description": "X-Account Backtest (Account ergänzen)",
        "hint": (
            "❌ <b>/testaccount</b> — X-Account auf Empfehlungs-Performance testen\n\n"
            "So geht's: <code>/testaccount ACCOUNT [TAGE]</code>\n"
            "Standard: 60 Tage, wenn kein Zeitraum angegeben ist.\n"
            "Beispiel: <code>/testaccount CryptoCapo_</code>\n"
            "Beispiel: <code>/testaccount @Pentosh1 30</code>"
        ),
        "help_line": "<code>/testaccount ACCOUNT [TAGE]</code> — Backtest der BUY/SELL-Empfehlungen (Standard: 60 Tage)",
    },
    "menu": {
        "menu_description": "Alle Bereiche — Kategorien mit Buttons",
        "help_line": "<code>/menu</code> — Bereiche wählen (Watchlist, Handel, Modus, …)",
    },
    "help": {
        "menu_description": "Alle Befehle mit Beispielen",
        "help_line": "<code>/help</code> — Diese Befehlsliste",
    },
    "unknown": {
        "hint": "❓ Unbekannter Befehl. Sende <code>/help</code> für die komplette Liste mit Beispielen.",
    },
}


def hint(key: str) -> str:
    return USAGE.get(key, {}).get("hint", USAGE["unknown"]["hint"])


def build_help_message() -> str:
    sections = [
        ("📋 <b>Watchlist</b> — Welche Coins der Bot beobachtet", ["list", "add", "remove"]),
        ("💰 <b>Handel</b> — Kaufen, verkaufen, Portfolio", ["buy", "sell", "positions", "orders", "risk"]),
        ("⚙️ <b>Modus & Sicherheit</b>", ["mode", "maxpositions", "live_confirm", "live_cancel", "gate", "dryrun"]),
        ("🐦 <b>X / Twitter</b> — Posts analysieren lassen", ["addx", "removex", "listx", "xsignals", "xposts", "xaccuracy", "testaccount", "tracktest"]),
        ("🔍 <b>Transparenz</b> — Was der Bot warum tut", ["decisions", "why", "hermes", "hermes_last", "hermes_run", "cmc"]),
        ("🧪 <b>Sandbox & Strategie-Tests</b>", ["sandbox", "sandbox_results", "sandbox_promote", "backtest", "backtest_lock", "backtest_results"]),
        ("❓ <b>Hilfe</b>", ["menu", "help"]),
    ]
    lines = [
        "<b>🛠️ Telegram-Befehle</b>",
        "",
        "Tipp: Menü-Button „Menü“ — Schnellbefehle + <code>/menu</code> für alle Bereiche.",
        "Bei unvollständigen Befehlen (z.B. nur <code>/buy</code>) antwortet der Bot mit einem Beispiel.",
        "",
    ]
    for title, keys in sections:
        lines.append(title)
        for key in keys:
            lines.append(USAGE[key]["help_line"])
        lines.append("")
    lines.append("Sende <code>/help</code> jederzeit für diese Liste.")
    return "\n".join(lines)