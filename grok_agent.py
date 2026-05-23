import os
from dotenv import load_dotenv
from openai import OpenAI
from pathlib import Path

load_dotenv()

client = OpenAI(
    api_key=os.getenv("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

MODEL = "grok-4"

def ask_grok(prompt):
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
        )
        return response.choices[0].message.content
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
    Path(filename).parent.mkdir(parents=True, exist_ok=True)
    with open(filename, "w", encoding="utf-8") as f:
        f.write(content.strip())
    print(f"✅ Gespeichert: {filename}")

if __name__ == "__main__":
    print("🤖 Grok Agent gestartet")
    print("Verfügbare Befehle:")
    print("   Lies main.py")
    print("   Lies alle .py Dateien")
    print("   write datei.py Deine Beschreibung")
    print("   exit\n")

    while True:
        try:
            user_input = input("👉 ").strip()

            if user_input.lower() in ["exit", "quit", "bye"]:
                print("Tschüss!")
                break

            if user_input.startswith("Lies "):
                file_part = user_input[5:].strip()
                if file_part in ["alle .py Dateien", "alle python", "alle"]:
                    for f in sorted(Path(".").glob("*.py")):
                        read_file(f.name)
                else:
                    read_file(file_part)
                continue

            if user_input.startswith("write "):
                parts = user_input.split(maxsplit=2)
                if len(parts) == 3:
                    filename = parts[1]
                    desc = parts[2]
                    print(f"Generiere {filename}...")
                    code = ask_grok(f"Schreibe vollständigen sauberen Python-Code für die Datei '{filename}':\n\n{desc}")
                    write_file(filename, code)
                continue

            print("\n" + ask_grok(user_input) + "\n")

        except KeyboardInterrupt:
            print("\nTschüss!")
            break
        except Exception as e:
            print(f"Fehler: {e}")
