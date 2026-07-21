"""Soft hot-reload scopes for operator use (no trading-core code reload).

Scopes
------
ui      — telegram_messages + menu/help locales
config  — config.json cache + BotConfig refresh + fingerprint
lists   — re-read watchlist + X accounts (and refresh in-memory consumers)
cache   — in-memory prices + OHLCV RAM (+ best-effort Redis key bust)
all     — ui + config + lists + cache

Never reloads strategy/risk/order modules — restart/deploy for those.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from logger import log

BOT_ROOT = Path(__file__).resolve().parents[1]
_AUDIT_PATH = BOT_ROOT / "logs" / "reload_audit.jsonl"
_LOCK = threading.RLock()
_LAST: dict[str, Any] | None = None

SCOPES = ("ui", "config", "lists", "cache", "all")


@dataclass
class ScopeResult:
    scope: str
    ok: bool
    detail: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "ok": self.ok,
            "detail": self.detail,
            "meta": self.meta,
        }


@dataclass
class ReloadReport:
    scopes: list[str]
    results: list[ScopeResult]
    started_at: str
    elapsed_ms: float
    source: str = "api"
    actor: str = ""

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scopes": list(self.scopes),
            "ok": self.ok,
            "started_at": self.started_at,
            "elapsed_ms": self.elapsed_ms,
            "source": self.source,
            "actor": self.actor,
            "results": [r.as_dict() for r in self.results],
        }


def last_reload() -> dict[str, Any] | None:
    with _LOCK:
        return dict(_LAST) if _LAST else None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _append_audit(report: ReloadReport) -> None:
    """A6 — append-only audit trail for reload actions."""
    try:
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(report.as_dict(), ensure_ascii=False, default=str)
        with open(_AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        log(f"reload audit write failed: {e}", "WARNING")


def reload_ui() -> ScopeResult:
    """A1 — clear and reload telegram i18n + menu/help catalogs."""
    meta: dict[str, Any] = {}
    try:
        from notifications.telegram_i18n import reload_messages

        reload_messages()
        meta["messages"] = "ok"
    except Exception as e:
        return ScopeResult("ui", False, f"messages: {e}", meta)

    try:
        from notifications.telegram_commands.menu_i18n import reload_menu_data

        reload_menu_data()
        meta["menu"] = "ok"
    except Exception as e:
        return ScopeResult("ui", False, f"menu: {e}", meta)

    try:
        from notifications.telegram_commands.usage_hints import clear_usage_cache

        clear_usage_cache()
        meta["usage_hints"] = "ok"
    except Exception as e:
        meta["usage_hints"] = f"skip:{e}"

    return ScopeResult("ui", True, "telegram_messages + menu/help reloaded", meta)


def reload_config_scope() -> ScopeResult:
    """A2 — force config disk/mongo reload and refresh fingerprint."""
    meta: dict[str, Any] = {}
    try:
        from data_manager import reload_config
        from services.config_fingerprint import config_fingerprint

        cfg = reload_config()
        fp = config_fingerprint(cfg)
        meta["fingerprint"] = fp
        meta["trading_mode"] = cfg.get("trading_mode")
        meta["max_open_positions"] = cfg.get("max_open_positions")
        # Touch BotConfig path (always fresh via get_config after cache clear)
        try:
            from core.config import get_bot_config

            bc = get_bot_config()
            if hasattr(bc, "refresh"):
                bc.refresh()
            meta["bot_config"] = "refreshed"
        except Exception as e:
            meta["bot_config"] = f"skip:{e}"
        return ScopeResult(
            "config",
            True,
            f"config reloaded · fingerprint {fp} · greift ab nächstem Cycle",
            meta,
        )
    except Exception as e:
        return ScopeResult("config", False, str(e), meta)


def reload_lists() -> ScopeResult:
    """A3 — re-read watchlist + X accounts; refresh optional consumers."""
    meta: dict[str, Any] = {}
    try:
        from data_manager import load_effective_watchlist, load_watchlist, load_x_accounts

        base = load_watchlist() or []
        effective = load_effective_watchlist() or []
        accounts = load_x_accounts() or []
        meta["watchlist_base"] = len(base)
        meta["watchlist_effective"] = len(effective)
        meta["x_accounts"] = len(accounts)

        # Best-effort: refresh XAnalyzer singleton accounts if present
        try:
            import aria_bot

            analyzer = getattr(aria_bot, "x_analyzer", None) or getattr(aria_bot, "analyzer", None)
            if analyzer is not None and hasattr(analyzer, "_reload_accounts"):
                analyzer._reload_accounts()
                meta["x_analyzer"] = "reloaded"
            elif analyzer is not None and hasattr(analyzer, "accounts"):
                analyzer.accounts = accounts
                meta["x_analyzer"] = "accounts_set"
        except Exception as e:
            meta["x_analyzer"] = f"skip:{e}"

        return ScopeResult(
            "lists",
            True,
            (
                f"watchlist {len(effective)} coins (base {len(base)}) · "
                f"X-Accounts {len(accounts)}"
            ),
            meta,
        )
    except Exception as e:
        return ScopeResult("lists", False, str(e), meta)


def reload_cache() -> ScopeResult:
    """A4 — bust price + OHLCV caches (RAM; Redis best-effort)."""
    meta: dict[str, Any] = {}
    try:
        from price_fetcher import clear_price_cache

        n_local = clear_price_cache()
        meta["price_ram"] = n_local
    except Exception as e:
        meta["price_ram"] = f"err:{e}"

    try:
        from bus.price_cache import clear_redis_price_cache, price_cache_from_config

        cache = price_cache_from_config()
        deleted = clear_redis_price_cache(cache)
        meta["price_redis_deleted"] = deleted
    except Exception as e:
        meta["price_redis"] = f"skip:{e}"

    try:
        from bus.ohlcv_cache import ohlcv_cache_from_config

        oc = ohlcv_cache_from_config()
        ram_before = 0
        try:
            ram_before = int(oc.stats().get("ram_entries", 0) or 0)
        except Exception:
            pass
        oc.clear()
        meta["ohlcv_ram_cleared"] = ram_before
    except Exception as e:
        meta["ohlcv"] = f"skip:{e}"

    ok = not any(str(v).startswith("err:") for v in meta.values())
    detail_bits = []
    if "price_ram" in meta:
        detail_bits.append(f"price_ram={meta['price_ram']}")
    if "price_redis_deleted" in meta:
        detail_bits.append(f"redis_price={meta['price_redis_deleted']}")
    if "ohlcv_ram_cleared" in meta:
        detail_bits.append(f"ohlcv_ram={meta['ohlcv_ram_cleared']}")
    return ScopeResult(
        "cache",
        ok,
        "caches cleared · " + ", ".join(detail_bits) if detail_bits else "caches cleared",
        meta,
    )


_SCOPE_FNS: dict[str, Callable[[], ScopeResult]] = {
    "ui": reload_ui,
    "config": reload_config_scope,
    "lists": reload_lists,
    "cache": reload_cache,
}


def normalize_scopes(raw: str | list[str] | None) -> list[str]:
    if raw is None or raw == "" or raw == "all":
        return ["ui", "config", "lists", "cache"]
    if isinstance(raw, str):
        parts = [p.strip().lower() for p in raw.replace(",", " ").split() if p.strip()]
    else:
        parts = [str(p).strip().lower() for p in raw if str(p).strip()]
    if not parts or "all" in parts:
        return ["ui", "config", "lists", "cache"]
    out: list[str] = []
    for p in parts:
        if p in _SCOPE_FNS and p not in out:
            out.append(p)
    return out


def run_reload(
    scopes: str | list[str] | None = "all",
    *,
    source: str = "api",
    actor: str = "",
) -> ReloadReport:
    """Execute one or more reload scopes; always writes audit + last snapshot."""
    resolved = normalize_scopes(scopes)
    started = time.perf_counter()
    started_at = _utc_now_iso()
    results: list[ScopeResult] = []
    with _LOCK:
        for scope in resolved:
            fn = _SCOPE_FNS.get(scope)
            if not fn:
                results.append(ScopeResult(scope, False, f"unknown scope: {scope}"))
                continue
            try:
                results.append(fn())
            except Exception as e:
                log(f"reload scope {scope} failed: {e}", "ERROR")
                results.append(ScopeResult(scope, False, str(e)))
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
        report = ReloadReport(
            scopes=resolved,
            results=results,
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            source=source,
            actor=actor,
        )
        global _LAST
        _LAST = report.as_dict()
        _append_audit(report)
        log(
            f"reload [{source}] scopes={resolved} ok={report.ok} "
            f"{elapsed_ms}ms actor={actor or '-'}",
            "INFO",
        )
        return report


def format_reload_report_html(report: ReloadReport) -> str:
    """Telegram-friendly HTML summary."""
    icon = "✅" if report.ok else "⚠️"
    lines = [
        f"{icon} <b>Reload</b> · <code>{', '.join(report.scopes)}</code>",
        f"<i>{report.started_at}</i> · {report.elapsed_ms:.0f} ms",
    ]
    if report.actor:
        lines.append(f"Actor: <code>{report.actor}</code>")
    for r in report.results:
        mark = "✅" if r.ok else "❌"
        lines.append(f"{mark} <b>{r.scope}</b> — {r.detail}")
        if r.scope == "config" and r.meta.get("fingerprint"):
            lines.append(f"   └ fingerprint <code>{r.meta['fingerprint']}</code>")
        if r.scope == "lists":
            m = r.meta
            if m.get("watchlist_effective") is not None:
                lines.append(
                    f"   └ WL {m.get('watchlist_effective')} · "
                    f"X {m.get('x_accounts', '?')}"
                )
    lines.append("")
    lines.append("<i>Code/Risk/Orders: Restart oder Deploy nötig.</i>")
    return "\n".join(lines)


def format_reload_help_html() -> str:
    last = last_reload()
    lines = [
        "<b>🔄 Soft Reload</b>",
        "",
        "Lädt Daten/UI neu — <b>ohne</b> Trading-Core zu patchen.",
        "",
        "<code>/reload</code> — diese Hilfe + letzter Reload",
        "<code>/reload ui</code> — Telegram-Texte / Menü",
        "<code>/reload config</code> — config.json (nächster Cycle)",
        "<code>/reload lists</code> — Watchlist + X-Accounts",
        "<code>/reload cache</code> — Preis/OHLCV-Caches",
        "<code>/reload all</code> — alles oben",
        "",
    ]
    if last:
        ok = "✅" if last.get("ok") else "⚠️"
        lines.append(
            f"Letzter Reload: {ok} <code>{', '.join(last.get('scopes') or [])}</code> "
            f"@ {last.get('started_at', '?')}"
        )
    else:
        lines.append("Letzter Reload: <i>noch keiner in diesem Prozess</i>")
    return "\n".join(lines)
