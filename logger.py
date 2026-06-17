import json
import os
from datetime import datetime

LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "aria_log.txt")
JSON_LOG_FILE = os.path.join(LOG_DIR, "aria_log.jsonl")
DECISIONS_LOG_FILE = os.path.join(LOG_DIR, "decisions.jsonl")

os.makedirs(LOG_DIR, exist_ok=True)


def _json_logs_enabled() -> bool:
    try:
        from data_manager import get_config
        return bool(get_config().get("observability", {}).get("json_logs", False))
    except Exception:
        return False


def log(message, level="INFO"):
    """Write a message to the log file and terminal."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] [{level}] {message}"

    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(log_entry + "\n")
    except Exception as e:
        print(f"Fehler beim Schreiben ins Log: {e}")

    try:
        print(log_entry)
    except BrokenPipeError:
        pass

    if _json_logs_enabled():
        log_json({"type": "log", "level": level, "message": message})


def log_json(event: dict, level: str = "INFO"):
    """Append a structured JSON event to logs/aria_log.jsonl."""
    if not _json_logs_enabled():
        return
    entry = {
        "timestamp": datetime.now().isoformat(),
        "level": level,
        **event,
    }
    try:
        with open(JSON_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"JSON log write failed: {e}")


def log_decision(entry: dict):
    """Append a decision audit record to logs/decisions.jsonl."""
    record = {"timestamp": datetime.now().isoformat(), **entry}
    try:
        with open(DECISIONS_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"Decision audit write failed: {e}", "ERROR")
        return
    if _json_logs_enabled():
        log_json({"type": "decision", **entry})


def log_signal(strategy_tf, signal, rsi, vol_mult, price, has_position):
    """Log trading signals."""
    status = "JA" if has_position else "NEIN"
    log(
        f"SIGNAL | {strategy_tf} | {signal} | RSI: {rsi:.1f} | Vol: {vol_mult:.2f}x | Preis: ${price:.4f} | Position: {status}"
    )