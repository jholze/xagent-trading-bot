"""Telegram /ask bridge: queue questions for Cursor, deliver answers back."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from logger import log

_QUEUE_LOCK = threading.Lock()
_HEADLESS_DISPATCHED: set[str] = set()
_DEFAULT_QUEUE = Path("data/telegram_ask_queue.json")
_ROOT = Path(__file__).resolve().parent.parent
CURSOR_NOTIFY_MARKER = "@@CURSOR_ASK_NOTIFY@@"
CURSOR_ACTION_MARKER = "@@CURSOR_ASK_ACTION@@"


def _cfg() -> dict:
    try:
        from core.config import get_bot_config

        obs = get_bot_config().raw.get("observability", {})
        bridge_cfg = obs.get("ask_bridge")
        if bridge_cfg:
            return bridge_cfg
        # legacy: ask_bridge was nested under telegram_explanations
        return obs.get("telegram_explanations", {}).get("ask_bridge", {})
    except Exception:
        return {}


def _response_mode() -> str:
    """cursor_only = no Grok API; grok_fallback = Grok after timeout."""
    mode = str(_cfg().get("response_mode", "")).strip().lower()
    if mode in ("cursor_only", "grok_fallback"):
        return mode
    if not _cfg().get("grok_fallback_enabled", True) and not _cfg().get("auto_respond_enabled", True):
        return "cursor_only"
    return "grok_fallback"


def grok_fallback_enabled() -> bool:
    return _response_mode() == "grok_fallback" and bool(_cfg().get("grok_fallback_enabled", True))


def auto_respond_enabled() -> bool:
    return _response_mode() == "grok_fallback" and bool(_cfg().get("auto_respond_enabled", True))


def headless_dispatch_enabled() -> bool:
    return bool(_cfg().get("headless_dispatch_enabled", True))


def headless_dispatch_delay_sec() -> float:
    return float(_cfg().get("headless_dispatch_delay_sec", 12))


def queue_path() -> Path:
    raw = _cfg().get("queue_path", "data/telegram_ask_queue.json")
    return Path(raw)


def pending_notify_path() -> Path:
    raw = _cfg().get("pending_notify_path", "data/telegram_ask_pending.json")
    return Path(raw)


def notify_log_path() -> Path:
    raw = _cfg().get("notify_log_path", "data/telegram_ask_notify.jsonl")
    return Path(raw)


def agent_inbox_path() -> Path:
    raw = _cfg().get("agent_inbox_path", "data/telegram_ask_agent_inbox.json")
    return Path(raw)


def format_cursor_notify(payload: dict) -> str:
    """Single-line marker the Cursor watcher uses to wake the agent."""
    return f"{CURSOR_NOTIFY_MARKER}{json.dumps(payload, ensure_ascii=False)}{CURSOR_NOTIFY_MARKER}"


def format_agent_action(payload: dict) -> str:
    """Compact marker for terminal→chat coupling (pattern match)."""
    action = {
        "event": "answer_telegram_ask",
        "id": payload.get("id"),
        "question": (payload.get("question") or "")[:240],
        "response_mode": payload.get("response_mode", _response_mode()),
    }
    return f"{CURSOR_ACTION_MARKER}{json.dumps(action, ensure_ascii=False)}{CURSOR_ACTION_MARKER}"


def build_agent_task_brief(payload: dict) -> str:
    """Instructions for the Cursor/Grok agent (no external API)."""
    qid = payload.get("id", "?")
    question = payload.get("question", "")
    urls = _extract_urls(question)
    url_hint = f"\nURLs: {', '.join(urls)}" if urls else ""
    return (
        f"TELEGRAM /ASK — jetzt beantworten und an Telegram liefern.\n\n"
        f"ID: {qid}\n"
        f"Frage: {question}{url_hint}\n\n"
        f"1) Kontext: python3 scripts/ask_bridge_handle_pending.py\n"
        f"2) Analysiere mit Repo-Zugriff (decisions.jsonl, lc_signals, orders, config, URLs)\n"
        f"3) Pflicht: python3 scripts/ask_bridge_reply.py {qid} \"ANTWORT\" --by headless\n"
        f"   (Deutsch, Telegram-HTML, max 8 Sätze)\n"
        f"4) Kein grok_agent.py / keine xAI-API.\n"
    )


def write_agent_inbox(payload: dict) -> dict:
    """Persist the latest ask for hooks and agent dispatch."""
    inbox = {
        **payload,
        "task_brief": build_agent_task_brief(payload),
        "updated_at": _utc_iso(),
    }
    path = agent_inbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    from data_manager import atomic_write_json

    atomic_write_json(str(path), inbox)
    return inbox


def read_agent_inbox() -> dict | None:
    path = agent_inbox_path()
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("id"):
            return data
    except Exception:
        return None
    return None


def notify_cursor_agent(record: dict) -> dict:
    """Write pending notification for the Cursor-side watcher."""
    payload = {
        "event": "new_ask",
        "id": record.get("id"),
        "question": record.get("question"),
        "created_at": record.get("created_at"),
        "context": record.get("context") or {},
        "response_mode": _response_mode(),
        "reply_cmd": f"python3 scripts/ask_bridge_reply.py {record.get('id')} \"…\"",
        "notify_at": _utc_iso(),
    }
    path = pending_notify_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    from data_manager import atomic_write_json

    atomic_write_json(str(path), payload)
    log_path = notify_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    write_agent_inbox(payload)
    log(f"Ask bridge Cursor notify #{payload['id']}", "INFO")
    return payload


def get_latest_pending_notification() -> dict | None:
    path = pending_notify_path()
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("id"):
            return data
    except Exception:
        return None
    return None


def clear_pending_notification(question_id: str | None = None) -> None:
    path = pending_notify_path()
    if not path.is_file():
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        path.unlink(missing_ok=True)
        return
    if question_id and data.get("id") != question_id:
        return
    path.unlink(missing_ok=True)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: datetime | None = None) -> str:
    return (dt or _utc_now()).isoformat()


def _load_queue() -> dict:
    path = queue_path()
    if not path.is_file():
        return {"questions": []}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("questions"), list):
            return data
    except Exception as e:
        log(f"Ask bridge queue read failed: {e}", "WARNING")
    return {"questions": []}


def _save_queue(data: dict) -> None:
    path = queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    from data_manager import atomic_write_json

    atomic_write_json(str(path), data)


def _authorized_chat(chat_id: str | int | None) -> bool:
    allowed = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not allowed:
        return False
    return str(chat_id or "").strip() == str(allowed).strip()


def _rate_limit_ok(chat_id: str | int) -> tuple[bool, str]:
    cfg = _cfg()
    limit = int(cfg.get("rate_limit_per_hour", 20))
    if limit <= 0:
        return True, ""
    cutoff = _utc_now() - timedelta(hours=1)
    count = 0
    for item in _load_queue().get("questions", []):
        if str(item.get("chat_id", "")) != str(chat_id):
            continue
        try:
            ts = datetime.fromisoformat(str(item.get("created_at", "")).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if ts >= cutoff:
            count += 1
    if count >= limit:
        return False, f"Rate limit: max {limit} Fragen pro Stunde"
    return True, ""


def _extract_symbols(text: str) -> list[str]:
    tokens = re.findall(r"\b[A-Z]{2,10}\b", text.upper())
    skip = {
        "USDT", "USD", "BTC", "ETH", "WARUM", "WAS", "WIE", "WURDE", "GEKAUFT", "WIRD",
        "DER", "DIE", "DAS", "UND", "ASK", "IST", "SIND", "HAT", "HABEN", "NICHT",
        "GEHTS", "DIR", "MIR", "DEIN", "DEINE", "MEIN", "MEINE", "HEUTE", "GESTERN",
        "KANN", "KÖNNEN", "WILL", "BITTE", "DANKE", "HALLO", "GUT", "JA", "NEIN",
        "WENN", "DANN", "AUCH", "NUR", "NOCH", "SCHON", "SEHR", "VIEL", "WENIG",
        "MARKT", "MORGEN", "GERADE", "AKTUELL", "FRAGE", "ANTWORT",
        "CHECK", "MAL", "HTTPS", "HTTP", "DEX", "COM", "TOKEN", "SOLANA", "COINMARKETCAP",
    }
    return [t for t in tokens if t not in skip][:5]


def _extract_urls(text: str) -> list[str]:
    return re.findall(r"https?://[^\s<>\"{}|\\^`\[\]]+", text or "")[:3]


def _recent_decisions(limit: int = 8) -> list[dict]:
    entries = []
    try:
        from logger import DECISIONS_LOG_FILE

        if not os.path.isfile(DECISIONS_LOG_FILE):
            return entries
        with open(DECISIONS_LOG_FILE, encoding="utf-8") as f:
            lines = f.readlines()[-300:]
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            entries.append({
                "symbol": entry.get("symbol"),
                "action": entry.get("action"),
                "rationale": (entry.get("rationale") or "")[:160],
                "executed": entry.get("executed"),
                "at": (entry.get("timestamp") or "")[:16],
            })
            if len(entries) >= limit:
                break
    except Exception:
        pass
    return entries


def _recent_lc_signals(limit: int = 6) -> list[dict]:
    try:
        from data_manager import load_lc_signals

        out = []
        for sig in reversed(load_lc_signals().get("signals", [])):
            out.append({
                "coin": sig.get("coin"),
                "action": sig.get("action"),
                "confidence": sig.get("confidence"),
                "rationale": (sig.get("rationale") or "")[:120],
                "at": (sig.get("timestamp") or "")[:16],
            })
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


def _bot_status_snapshot() -> dict:
    try:
        from core.config import get_bot_config
        from risk.risk_manager import RiskManager

        cfg = get_bot_config()
        status = RiskManager(cfg).status_summary()
        status["trading_mode"] = cfg.trading_mode
        status["dry_run"] = bool(cfg.raw.get("dry_run", False))
        return status
    except Exception as e:
        return {"error": str(e)}


def build_context_snapshot(question: str) -> dict:
    """Rich local context for Cursor (no API). Grok fallback uses the same snapshot."""
    snapshot = {
        "question": question,
        "symbols_mentioned": _extract_symbols(question),
        "urls_mentioned": _extract_urls(question),
        "context_files": [
            "logs/decisions.jsonl",
            "data/lc_signals.json",
            "data/telegram_ask_queue.json",
            "logs/aria_log.txt",
        ],
    }
    try:
        from data_manager import is_demo_mode, load_trade_history, reconcile_demo_trade_history_on_startup

        hist = (
            reconcile_demo_trade_history_on_startup()
            if is_demo_mode()
            else load_trade_history()
        )
        snapshot["open_positions"] = int(hist.get("open_positions", 0) or 0)
        snapshot["virtual_balance_usdt"] = round(float(hist.get("virtual_balance", 0) or 0), 2)
        snapshot["realized_pnl"] = round(
            float(hist.get("realized_pnl", hist.get("total_pnl", 0)) or 0), 2
        )
    except Exception as e:
        snapshot["portfolio_error"] = str(e)

    try:
        from strategies.positions import bootstrap_positions, count_open_positions, list_active_positions

        if count_open_positions() == 0:
            bootstrap_positions()

        positions = []
        for pos in list_active_positions()[:12]:
            sym = pos.get("symbol", "")
            if "/" not in sym:
                sym = f"{sym}/USDT"
            entry = float(pos.get("average_entry", 0) or 0)
            amt = float(pos.get("amount", 0) or 0)
            positions.append({
                "symbol": sym,
                "notional_usdt": round(amt * entry, 2) if entry else 0,
                "sold_percent": round(float(pos.get("sold_percent", 0) or 0) * 100, 1),
            })
        snapshot["positions"] = positions
    except Exception as e:
        snapshot["positions_error"] = str(e)

    try:
        from data_manager import load_orders, resolve_ledger_scope
        from core.config import get_bot_config

        scope = resolve_ledger_scope(get_bot_config().trading_mode)
        recent = []
        for order in reversed(load_orders(scope).get("orders", [])):
            if order.get("status") != "filled":
                continue
            recent.append({
                "side": order.get("side"),
                "symbol": order.get("symbol"),
                "source": order.get("source"),
                "signal": order.get("signal"),
                "usdt": round(float((order.get("execution") or {}).get("usdt", 0) or 0), 2),
                "at": (order.get("timestamps") or {}).get("filled", "")[:16],
            })
            if len(recent) >= 6:
                break
        snapshot["recent_trades"] = recent
    except Exception as e:
        snapshot["trades_error"] = str(e)

    for sym_token in snapshot["symbols_mentioned"]:
        sym = f"{sym_token}/USDT"
        try:
            from logger import DECISIONS_LOG_FILE

            if os.path.isfile(DECISIONS_LOG_FILE):
                with open(DECISIONS_LOG_FILE, encoding="utf-8") as f:
                    for line in reversed(f.readlines()[-200:]):
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if (entry.get("symbol") or "").upper() == sym:
                            snapshot.setdefault("decisions", {})[sym] = {
                                "action": entry.get("action"),
                                "rationale": (entry.get("rationale") or "")[:200],
                                "executed": entry.get("executed"),
                                "at": (entry.get("timestamp") or "")[:16],
                            }
                            break
        except Exception:
            pass

    snapshot["bot_status"] = _bot_status_snapshot()
    snapshot["recent_decisions"] = _recent_decisions()
    snapshot["recent_lc_signals"] = _recent_lc_signals()

    return snapshot


def enqueue_question(chat_id: str | int, question: str) -> tuple[str | None, str]:
    """Add a question to the queue. Returns (id, error_message)."""
    cfg = _cfg()
    if not cfg.get("enabled", True):
        return None, "Ask-Bridge ist deaktiviert"

    if not _authorized_chat(chat_id):
        return None, "Nicht autorisiert"

    question = (question or "").strip()
    max_len = int(cfg.get("max_question_length", 500))
    if not question:
        return None, "Leere Frage"
    if len(question) > max_len:
        return None, f"Frage zu lang (max {max_len} Zeichen)"

    ok, reason = _rate_limit_ok(chat_id)
    if not ok:
        return None, reason

    qid = uuid.uuid4().hex[:10]
    record = {
        "id": qid,
        "chat_id": str(chat_id),
        "question": question,
        "status": "pending",
        "created_at": _utc_iso(),
        "answered_at": None,
        "delivered_at": None,
        "answer": None,
        "answered_by": None,
        "context": build_context_snapshot(question),
    }

    with _QUEUE_LOCK:
        data = _load_queue()
        data.setdefault("questions", []).append(record)
        _save_queue(data)

    notify_cursor_agent(record)
    log(f"Ask bridge enqueued #{qid}: {question[:80]}", "INFO")
    return qid, ""


def list_pending_questions() -> list[dict]:
    with _QUEUE_LOCK:
        data = _load_queue()
    return [q for q in data.get("questions", []) if q.get("status") == "pending"]


def get_question(question_id: str) -> dict | None:
    with _QUEUE_LOCK:
        data = _load_queue()
        for item in data.get("questions", []):
            if item.get("id") == question_id:
                return dict(item)
    return None


def auto_answer_if_pending(question_id: str, answered_by: str = "grok") -> tuple[bool, str]:
    """Answer a still-pending question via Grok (only when grok_fallback mode is enabled)."""
    if not auto_respond_enabled():
        return False, "Auto-respond deaktiviert (cursor_only)"
    if not grok_fallback_enabled():
        return False, "Grok-Fallback deaktiviert (cursor_only)"

    with _QUEUE_LOCK:
        data = _load_queue()
        found = None
        for item in data.get("questions", []):
            if item.get("id") == question_id and item.get("status") == "pending":
                found = item
                break
        if not found:
            return False, f"Frage #{question_id} nicht mehr pending"

    answer = _grok_fallback_answer(found.get("question", ""), found.get("context") or {})
    ok, err = submit_answer(question_id, answer, answered_by=answered_by)
    if ok:
        log(f"Ask bridge auto-responded #{question_id} via {answered_by}", "INFO")
    return ok, err


def submit_answer(question_id: str, answer: str, answered_by: str = "cursor") -> tuple[bool, str]:
    answer = (answer or "").strip()
    if not answer:
        return False, "Leere Antwort"

    with _QUEUE_LOCK:
        data = _load_queue()
        found = None
        for item in data.get("questions", []):
            if item.get("id") == question_id:
                found = item
                break
        if not found:
            return False, f"Frage #{question_id} nicht gefunden"
        if found.get("status") not in ("pending", "answered"):
            return False, f"Frage #{question_id} Status: {found.get('status')}"

        found["status"] = "answered"
        found["answer"] = answer[:4000]
        found["answered_by"] = answered_by
        found["answered_at"] = _utc_iso()
        _save_queue(data)

    clear_pending_notification(question_id)
    inbox = agent_inbox_path()
    if inbox.is_file():
        try:
            with open(inbox, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("id") == question_id:
                inbox.unlink(missing_ok=True)
        except Exception:
            pass
    log(f"Ask bridge answered #{question_id} via {answered_by}", "INFO")
    return True, ""


def _grok_fallback_answer(question: str, context: dict) -> str:
    try:
        from grok_agent import ask_grok

        prompt = (
            "Du bist der Trading-Bot-Assistent. Antworte kurz auf Deutsch (max 8 Sätze, Telegram-HTML erlaubt: <b>, <i>, <code>).\n\n"
            f"Frage: {question}\n\n"
            f"Kontext (JSON):\n{json.dumps(context, ensure_ascii=False, indent=2)}"
        )
        reply = ask_grok(prompt, temperature=0.3)
        if reply and not reply.startswith("API-Fehler"):
            return reply.strip()
        return f"⚠️ Grok-Fallback fehlgeschlagen: {reply}"
    except Exception as e:
        return f"⚠️ Grok-Fallback nicht verfügbar: {e}"


def _format_telegram_reply(item: dict) -> str:
    src = item.get("answered_by", "?")
    src_label = {
        "cursor": "Cursor",
        "grok": "API-Fallback",
        "headless": "Assistent (Auto)",
    }.get(src, src)
    q = item.get("question", "")
    a = item.get("answer", "")
    return (
        f"<b>💬 Antwort #{item.get('id')}</b> ({src_label})\n\n"
        f"<i>Frage:</i> {q}\n\n"
        f"{a}"
    )


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "")


def _send_telegram_chunks(text: str, chat_id: str | int | None = None) -> bool:
    from telegram_notifier import send_telegram_message

    for chunk in _split_telegram(text):
        if send_telegram_message(chunk, chat_id=chat_id):
            continue
        plain = _strip_html(chunk)
        if not send_telegram_message(plain, chat_id=chat_id, parse_mode=""):
            return False
    return True


def _question_age_sec(item: dict, now: datetime) -> float | None:
    try:
        created = datetime.fromisoformat(str(item.get("created_at", "")).replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return (now - created).total_seconds()
    except (ValueError, TypeError):
        return None


def _tick_headless_dispatch(now: datetime) -> None:
    if not headless_dispatch_enabled():
        return
    delay = headless_dispatch_delay_sec()
    with _QUEUE_LOCK:
        pending = [q for q in _load_queue().get("questions", []) if q.get("status") == "pending"]

    for item in pending:
        qid = str(item.get("id") or "")
        if not qid or qid in _HEADLESS_DISPATCHED:
            continue
        age = _question_age_sec(item, now)
        if age is None or age < delay:
            continue

        _HEADLESS_DISPATCHED.add(qid)
        from telegram_notifier import send_telegram_message

        send_telegram_message(
            f"🔄 <b>Bearbeite #{qid}</b> … (ca. 30–90s, lokal — keine xAI-API)",
            chat_id=item.get("chat_id"),
        )
        log(f"Ask bridge headless dispatch started #{qid}", "INFO")

        def _run(question_id: str = qid) -> None:
            log_path = _ROOT / "logs" / f"ask_headless_{question_id}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as out:
                subprocess.run(
                    [sys.executable, str(_ROOT / "scripts/ask_bridge_headless_dispatch.py"), question_id],
                    cwd=_ROOT,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    timeout=300,
                )

        threading.Thread(target=_run, daemon=True, name=f"ask-headless-{qid}").start()


def _split_telegram(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks


def _tick_once() -> None:
    cfg = _cfg()
    if not cfg.get("enabled", True):
        return

    timeout_sec = int(cfg.get("cursor_timeout_sec", 120))
    grok_enabled = grok_fallback_enabled()
    now = _utc_now()

    _tick_headless_dispatch(now)

    with _QUEUE_LOCK:
        data = _load_queue()
        changed = False
        for item in data.get("questions", []):
            status = item.get("status")
            if status == "pending" and grok_enabled:
                try:
                    created = datetime.fromisoformat(str(item.get("created_at", "")).replace("Z", "+00:00"))
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    continue
                age = (now - created).total_seconds()
                if age >= timeout_sec:
                    answer = _grok_fallback_answer(item.get("question", ""), item.get("context") or {})
                    item["status"] = "answered"
                    item["answer"] = answer[:4000]
                    item["answered_by"] = "grok"
                    item["answered_at"] = _utc_iso()
                    changed = True
                    log(f"Ask bridge Grok fallback for #{item.get('id')}", "INFO")

        if changed:
            _save_queue(data)

    to_deliver = []
    with _QUEUE_LOCK:
        data = _load_queue()
        for item in data.get("questions", []):
            if item.get("status") == "answered" and not item.get("delivered_at"):
                to_deliver.append(item)

    for item in to_deliver:
        qid = item.get("id")
        text = _format_telegram_reply(item)
        ok = _send_telegram_chunks(text, chat_id=item.get("chat_id"))
        if ok:
            with _QUEUE_LOCK:
                data = _load_queue()
                for stored in data.get("questions", []):
                    if stored.get("id") == qid:
                        stored["status"] = "delivered"
                        stored["delivered_at"] = _utc_iso()
                        break
                _save_queue(data)
            log(f"Ask bridge delivered #{qid} to Telegram", "INFO")
        else:
            log(f"Ask bridge delivery FAILED #{qid}", "WARNING")


def start_ask_bridge_poller(interval_sec: float | None = None):
    cfg = _cfg()
    if not cfg.get("enabled", True):
        return None
    interval = float(interval_sec or cfg.get("poll_interval_sec", 3))

    def _loop():
        while True:
            try:
                _tick_once()
            except Exception as e:
                log(f"Ask bridge poller error: {e}", "WARNING")
            time.sleep(interval)

    thread = threading.Thread(target=_loop, daemon=True, name="ask-bridge")
    thread.start()
    log(f"Ask bridge poller started (interval={interval}s)", "INFO")
    return thread