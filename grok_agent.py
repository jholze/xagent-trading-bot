"""CLI / legacy Grok helpers — uses pluggable llm_client (epic #72 C7)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from intelligence.llm_client import ask_llm, ask_llm_json  # noqa: E402

MODEL = os.getenv("LLM_MODEL") or os.getenv("GROK_PARSE_MODEL") or "grok-4"
PARSE_MODEL = os.getenv("GROK_PARSE_MODEL", MODEL)


def ask_grok(prompt, temperature=0.7, model=None):
    return ask_llm(prompt, temperature=temperature, model=model or MODEL)


def ask_grok_json(prompt, model=None):
    try:
        import json

        data = ask_llm_json(prompt, model=model or PARSE_MODEL)
        return json.dumps(data)
    except Exception as e:
        return f"API-Fehler: {e}"


def read_file(filename):
    try:
        with open(filename, "r", encoding="utf-8") as f:
            content = f.read()
        print(f"\n=== INHALT VON {filename} ===\n")
        print(content)
        print(f"=== ENDE {filename} ===\n")
        return content
    except Exception as e:
        print(f"Fehler beim Lesen von {filename}: {e}")
        return None


def write_file(filename, content):
    try:
        path = Path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Datei '{filename}' geschrieben.")
        return True
    except Exception as e:
        print(f"Fehler beim Schreiben von {filename}: {e}")
        return False


if __name__ == "__main__":
    print("Grok agent ready. MODEL=", MODEL)
