# Telegram-Befehlsmenü (native Menu Button)

> Status: **umgesetzt** (archiviert 2026-06-15)  
> Ursprünglich: Grok Plan Mode `plan.md` dieser Session

## Ergebnis

Native Telegram-Befehlsliste (`setMyCommands` + `setChatMenuButton`) ist live:

- `notifications/telegram_commands/command_menu.py` — Registry + API-Registrierung
- DE/EN-Beschreibungen, 8 Hub-Befehle + Bereichs-Commands
- Registrierung in `aria_bot.py` und `scripts/start_with_ngrok.sh`
- Tests: `tests/unit/test_command_menu.py`

Relevante Commits auf `main`: `bcbbcb2` … `ecd338f` (feat telegram menu).

## Ursprünglicher Plan (Kontext)

Registrierung aller Bot-Befehle über die Telegram Bot API (`setMyCommands`), damit das native Menü neben der Eingabezeile erscheint.

### Zielverhalten

- Menü-Button links neben der Eingabezeile zeigt Befehle mit kurzer Beschreibung
- Tipp fügt `/befehl` in die Eingabezeile ein (Telegram-Standard)
- Parameter-Befehle (`/buy`, `/why`, …) zeigen weiterhin Hinweise, wenn nur der Stamm gesendet wird

### Umsetzungs-Checkliste

- [x] **menu-registry** — `command_menu.py` + `menu_description` in `usage_hints.py`
- [x] **register-api** — `setMyCommands` + `setChatMenuButton` in `register_bot_commands()`
- [x] **startup-wire** — Registrierung in `aria_bot.py` und Start-Skripten
- [x] **tests** — `tests/unit/test_command_menu.py`
- [x] **docs** — `DOCUMENTATION.md` §7 + `README.md`

### Bewusst nicht umgesetzt (optional später)

- Reply Keyboard (dauerhafte Tastatur)
- Hierarchisches Inline-Menü mit Kategorien (teilweise via `/menu` Hub)