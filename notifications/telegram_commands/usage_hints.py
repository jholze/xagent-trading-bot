"""Central command usage hints for Telegram (DE layperson text + English command names)."""

USAGE = {
    "add": {
        "hint": (
            "❌ <b>/add</b> — Coin zur Beobachtungsliste hinzufügen\n\n"
            "So geht's: <code>/add SYMBOL</code>\n"
            "Beispiel: <code>/add RAVE</code> oder <code>/add SOL</code>"
        ),
        "help_line": "<code>/add SYMBOL</code> — Coin beobachten (z.B. <code>/add RAVE</code>)",
    },
    "remove": {
        "hint": (
            "❌ <b>/remove</b> — Coin von der Liste entfernen\n\n"
            "Zuerst <code>/list</code> senden, dann Nummer wählen.\n"
            "Beispiel: <code>/remove 2</code>"
        ),
        "help_line": "<code>/remove NUMMER</code> — Coin entfernen (zuerst <code>/list</code>, z.B. <code>/remove 2</code>)",
    },
    "list": {
        "help_line": "<code>/list</code> — Alle beobachteten Coins anzeigen",
    },
    "buy": {
        "hint": (
            "❌ <b>/buy</b> — Coin kaufen (Paper, Testnet oder Live)\n\n"
            "So geht's: <code>/buy SYMBOL USDT</code> oder <code>/buy NUMMER USDT</code>\n"
            "Beispiele:\n"
            "• <code>/buy ARIA 200</code> — 200 $ in ARIA investieren\n"
            "• <code>/buy 1 200</code> — Coin Nr. 1 aus <code>/list</code>, 200 $"
        ),
        "help_line": "<code>/buy SYMBOL USDT</code> — Kaufen (z.B. <code>/buy ARIA 200</code> oder <code>/buy 1 200</code>)",
    },
    "sell": {
        "hint": (
            "❌ <b>/sell</b> — Anteil einer Position verkaufen\n\n"
            "Zuerst <code>/sell</code> ohne Parameter → Liste der Positionen.\n"
            "Dann: <code>/sell NUMMER PROZENT</code>\n"
            "Beispiel: <code>/sell 1 30</code> — 30 % von Position 1 verkaufen"
        ),
        "help_line": "<code>/sell NUMMER PROZENT</code> — Verkaufen (zuerst <code>/sell</code> für Liste, z.B. <code>/sell 1 30</code>)",
    },
    "positions": {
        "help_line": "<code>/positions</code> — Portfolio, Kurse, Gewinn/Verlust und letzte Trades",
    },
    "risk": {
        "help_line": "<code>/risk</code> — Risiko-Limits, Drawdown und Positionsgröße anzeigen",
    },
    "mode": {
        "hint": (
            "❌ Unbekannter Modus.\n\n"
            "Sende <code>/mode</code> für die aktuelle Einstellung und alle Optionen:\n"
            "• <code>/mode paper</code> — Virtuelles Geld (Standard)\n"
            "• <code>/mode gate_testnet</code> — Gate.io Testnet\n"
            "• <code>/mode live</code> — Echtes Geld (braucht <code>/live_confirm</code>)\n"
            "• <code>/mode off</code> — Nur Analyse, kein Handel"
        ),
        "help_line": "<code>/mode</code> — Handelsmodus anzeigen; <code>/mode paper|gate_testnet|live|off</code> zum Wechseln",
    },
    "live_confirm": {
        "help_line": "<code>/live_confirm</code> — Live-Handel auf Gate.io Mainnet bestätigen",
    },
    "live_cancel": {
        "help_line": "<code>/live_cancel</code> — Live-Handel abbrechen, zurück zu Paper",
    },
    "gate": {
        "help_line": "<code>/gate</code> — Gate.io API-Status (Mainnet + Testnet)",
    },
    "sandbox": {
        "help_line": "<code>/sandbox</code> — Strategie-Experimente anzeigen (automatisch aus X-Posts)",
    },
    "sandbox_results": {
        "hint": (
            "❌ <b>/sandbox_results</b> — Details zu einem Experiment\n\n"
            "Zuerst <code>/sandbox</code> für die ID-Liste.\n"
            "Beispiel: <code>/sandbox_results hyp_abc123</code>"
        ),
        "help_line": "<code>/sandbox_results ID</code> — Metriken eines Experiments (z.B. <code>/sandbox_results hyp_abc123</code>)",
    },
    "sandbox_promote": {
        "hint": (
            "❌ <b>/sandbox_promote</b> — Erfolgreiche Strategie aktivieren\n\n"
            "Zuerst <code>/sandbox_results ID</code> prüfen.\n"
            "Beispiel: <code>/sandbox_promote hyp_abc123</code>"
        ),
        "help_line": "<code>/sandbox_promote ID</code> — Strategie übernehmen (z.B. <code>/sandbox_promote hyp_abc123</code>)",
    },
    "cmc": {
        "help_line": "<code>/cmc</code> — CoinMarketCap Community-Stimmung (Sentiment-Signale)",
    },
    "addx": {
        "hint": (
            "❌ <b>/addx</b> — X/Twitter-Account zum Monitoring hinzufügen\n\n"
            "So geht's: <code>/addx ACCOUNT</code>\n"
            "Beispiel: <code>/addx CryptoCapo_</code>"
        ),
        "help_line": "<code>/addx ACCOUNT</code> — X-Account hinzufügen (z.B. <code>/addx CryptoCapo_</code>)",
    },
    "removex": {
        "hint": (
            "❌ <b>/removex</b> — X-Account entfernen\n\n"
            "So geht's: <code>/removex ACCOUNT</code>\n"
            "Beispiel: <code>/removex CryptoCapo_</code>"
        ),
        "help_line": "<code>/removex ACCOUNT</code> — X-Account entfernen (z.B. <code>/removex CryptoCapo_</code>)",
    },
    "listx": {
        "help_line": "<code>/listx</code> — Alle überwachten X-Accounts mit Trust-Score",
    },
    "xposts": {
        "help_line": "<code>/xposts</code> — Letzte analysierte X-Posts und Empfehlungen",
    },
    "xsignals": {
        "help_line": "<code>/xsignals</code> — Aktuelle starke X-Signale (BUY/SELL)",
    },
    "xaccuracy": {
        "help_line": "<code>/xaccuracy</code> — Trefferquote der X-Accounts (Leaderboard)",
    },
    "tracktest": {
        "help_line": "<code>/tracktest</code> — Test-Tweet sofort durch den Analyzer schicken",
    },
    "help": {
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
        ("💰 <b>Handel</b> — Kaufen, verkaufen, Portfolio", ["buy", "sell", "positions", "risk"]),
        ("⚙️ <b>Modus & Sicherheit</b>", ["mode", "live_confirm", "live_cancel", "gate"]),
        ("🐦 <b>X / Twitter</b> — Posts analysieren lassen", ["addx", "removex", "listx", "xsignals", "xposts", "xaccuracy", "tracktest"]),
        ("🧪 <b>Sandbox & CMC</b>", ["sandbox", "sandbox_results", "sandbox_promote", "cmc"]),
        ("❓ <b>Hilfe</b>", ["help"]),
    ]
    lines = [
        "<b>🛠️ Telegram-Befehle</b>",
        "",
        "Tipp: Bei unvollständigen Befehlen (z.B. nur <code>/buy</code>) antwortet der Bot mit einem Beispiel.",
        "",
    ]
    for title, keys in sections:
        lines.append(title)
        for key in keys:
            lines.append(USAGE[key]["help_line"])
        lines.append("")
    lines.append("Sende <code>/help</code> jederzeit für diese Liste.")
    return "\n".join(lines)