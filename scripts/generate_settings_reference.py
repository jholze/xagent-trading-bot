#!/usr/bin/env python3
"""Generate docs/reference/settings-features-options.generated.md from code + config.

Source of truth for *values* is the committed config.json (paper staging tree).
Effects/descriptions come from a curated map in this file and from Telegram
module docstrings. Run:

  python3 scripts/generate_settings_reference.py

CI fails if the generated file drifts (see .github/workflows/docs-settings-freshness.yml).
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "reference" / "settings-features-options.generated.md"
CONFIG = ROOT / "config.json"
TG_DIR = ROOT / "notifications" / "telegram_commands"

# Curated one-line effects for important keys (extend as needed). Missing keys
# still appear with value + path; agents must open the code location.
EFFECTS: dict[str, str] = {
    "update_interval": "Seconds between main analysis cycles.",
    "max_usdt_per_trade": "Hard cap USDT notional per entry (money path).",
    "max_daily_trades": "Max entries per UTC day.",
    "max_open_positions": "Max concurrent open lots.",
    "trade_cooldown_hours": "Min hours between trades on the same symbol.",
    "trading_mode": "paper | live | off (see Telegram /mode).",
    "live.dry_run": "When true, live mode does not place real exchange orders.",
    "live.dry_run_enhanced": "Sim-wallet + trending watchlist while dry_run.",
    "hermes.enabled": "Master switch for Hermes self-improvement cycles.",
    "regime_detector.enabled": "Opt-in technical+sentiment regime fusion.",
    "strategy_allocator.enabled": "Opt-in dynamic Grid vs Momentum allocation.",
    "grid.enabled": "Opt-in ATR grid strategy.",
    "strategy_backtest.auto_run": "Auto backtest/tune when strategy params change.",
    "observability.telegram_explanations.enabled": "German 'Warum:' explanations on Telegram.",
    "observability.telegram_explanations.verbosity": "verbose | short explanation style.",
    "multi_tenant": "Tenant routing / overlays (structure depends on deploy).",
    "rule_switches": "Per-tenant feature rule switches (see specs/tenant-rule-switches).",
}

FEATURE_SECTIONS = [
    ("hermes.enabled", "Hermes"),
    ("regime_detector.enabled", "Regime detector"),
    ("strategy_allocator.enabled", "Strategy allocator"),
    ("grid.enabled", "Grid"),
    ("strategy_backtest.auto_run", "Strategy backtest auto-run"),
    ("live.dry_run", "Live dry-run"),
    ("live.dry_run_enhanced", "Enhanced dry-run"),
    ("observability.telegram_explanations.enabled", "Telegram explanations"),
    ("watchlist_quality", "Watchlist Quality Engine (WQE) block"),
    ("dca_sniper", "DCA sniper block"),
    ("gainer_signal", "Gainer signal block"),
    ("shorts", "Shorts block"),
    ("sandbox", "Sandbox block"),
    ("mcp", "MCP block"),
    ("memory", "Memory/RAG block"),
]


def dig(cfg: dict, path: str):
    cur: object = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def fmt_val(v) -> str:
    if isinstance(v, (dict, list)):
        return f"`<{type(v).__name__} len={len(v)}>`"
    return f"`{json.dumps(v, ensure_ascii=False)}`"


def discover_telegram_commands() -> list[tuple[str, str, str]]:
    """Return (command, module, summary) from telegram_commands package."""
    found: dict[str, tuple[str, str]] = {}
    cmd_re = re.compile(r"""(?:^|\s)/([a-z][a-z0-9_]{1,40})\b""")
    for path in sorted(TG_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        # module docstring first line(s)
        summary = ""
        try:
            mod = ast.parse(text)
            doc = ast.get_docstring(mod) or ""
            summary = " ".join(doc.strip().splitlines()[:2])[:160]
        except SyntaxError:
            summary = ""
        # explicit /cmd in docstrings and string literals near file top
        for m in cmd_re.finditer(text[:4000]):
            cmd = m.group(1)
            if cmd in {"http", "https", "usdt"}:
                continue
            found.setdefault(cmd, (path.name, summary))
        # Command names listed in menu_commands tuples
        if path.name == "menu_commands.py":
            for m in re.finditer(r"['\"]([a-z][a-z0-9_]{2,})['\"]", text):
                cmd = m.group(1)
                if cmd in {
                    "modus", "handel", "transparenz", "watchlist", "system",
                    "pending", "chrome", "state", "invalid", "true", "false",
                }:
                    continue
                if len(cmd) >= 3:
                    found.setdefault(cmd, (path.name, summary or "Listed in Telegram menu map"))
    rows = [(cmd, mod, summ) for cmd, (mod, summ) in sorted(found.items())]
    return rows


def top_level_rows(cfg: dict) -> list[str]:
    lines = [
        "| Key | Value in committed `config.json` | Effect (curated / TBD) |",
        "|---|---|---|",
    ]
    for key in sorted(cfg.keys()):
        path = key
        val = cfg[key]
        effect = EFFECTS.get(path, "_Open code / DOCUMENTATION; no curated one-liner yet._")
        lines.append(f"| `{path}` | {fmt_val(val)} | {effect} |")
    return lines


def feature_rows(cfg: dict) -> list[str]:
    lines = [
        "| Feature / block | Path | Value | Notes |",
        "|---|---|---|---|",
    ]
    for path, label in FEATURE_SECTIONS:
        val = dig(cfg, path)
        effect = EFFECTS.get(path, "")
        lines.append(f"| {label} | `{path}` | {fmt_val(val)} | {effect} |")
    return lines


def telegram_rows(cmds: list[tuple[str, str, str]]) -> list[str]:
    lines = [
        "| Command | Module | Summary (from module docstring / menu) |",
        "|---|---|---|",
    ]
    for cmd, mod, summ in cmds:
        safe = summ.replace("|", "\\|") if summ else "_see module_"
        lines.append(f"| `/{cmd}` | `notifications/telegram_commands/{mod}` | {safe} |")
    return lines


def main() -> None:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    cmds = discover_telegram_commands()
    parts: list[str] = []
    parts.append("<!-- AUTO-GENERATED by scripts/generate_settings_reference.py — do not hand-edit. -->")
    parts.append("# Settings / features / options reference (generated)\n")
    parts.append(
        "Generated from the committed `config.json` and `notifications/telegram_commands/` "
        "on this branch. **Paper staging values** — not a promise about live `main`. "
        "Money-path keys still require Jens for live changes.\n"
    )
    parts.append("Regenerate: `python3 scripts/generate_settings_reference.py`\n")
    parts.append("## Feature toggles and major blocks\n")
    parts.extend(feature_rows(cfg))
    parts.append("\n## Top-level `config.json` keys\n")
    parts.extend(top_level_rows(cfg))
    parts.append("\n## Telegram commands (discovered)\n")
    parts.append(
        f"Discovered **{len(cmds)}** command tokens from `notifications/telegram_commands/`. "
        "Human narrative remains in [DOCUMENTATION.md §7](../../DOCUMENTATION.md). "
        "Operator-only commands stay gated in code.\n"
    )
    parts.extend(telegram_rows(cmds))
    parts.append(
        "\n## Nested keys worth opening in code\n"
        "- `architecture.*` — rebuy cooldowns, runtime knobs (`core/config.py`).\n"
        "- `risk.*` / `costs.*` — money path (Jens for live).\n"
        "- `volatile_altcoin.*` / `stable_altcoin.*` / `mid_cap_defaults.*` — profile overlays.\n"
        "- `rule_switches` — tenant feature switches.\n"
        "- `observability.*` — digests, explanations, telemetry.\n"
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(parts) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size} bytes, {len(cmds)} telegram cmds)")


if __name__ == "__main__":
    main()
