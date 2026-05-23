import os
from datetime import datetime

LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "aria_log.txt")

# Stelle sicher, dass der logs-Ordner existiert
os.makedirs(LOG_DIR, exist_ok=True)


def log(message, level="INFO"):
    """Schreibt eine Nachricht in die Log-Datei + Terminal"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] [{level}] {message}"

    # In Datei schreiben
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(log_entry + "\n")
    except Exception as e:
        print(f"Fehler beim Schreiben ins Log: {e}")

    # Auch im Terminal ausgeben
    print(log_entry)


def log_signal(strategy_tf, signal, rsi, vol_mult, price, has_position):
    """Spezielle Log-Funktion für Trading-Signale"""
    status = "JA" if has_position else "NEIN"
    log(
        f"SIGNAL | {strategy_tf} | {signal} | RSI: {rsi:.1f} | Vol: {vol_mult:.2f}x | Preis: ${price:.4f} | Position: {status}"
    )
