"""Layperson German explanations for Telegram notifications."""

from __future__ import annotations

import re
from typing import Any

_RATIONALE_PARTS = {
    "TA→BUY": "Technische Analyse sieht eine Kaufchance (RSI und Volumen passen).",
    "TA→SELL_20": "RSI ist überkauft — wir verkaufen 20 % der Position, der Rest bleibt investiert.",
    "TA→SELL_30": "RSI ist stark überkauft — wir verkaufen weitere 30 % (gestaffelter Exit).",
    "TA→SELL_STOP_FULL": "Verlustgrenze erreicht — Position wird vollständig geschlossen zum Schutz.",
    "TA→SELL_STOP_PARTIAL": "Verlustgrenze erreicht — 50 % werden geschlossen, Rest bleibt unter Beobachtung.",
    "TA→SELL_TP": "Zielgewinn erreicht — ein Teil der Position wird mit Gewinn verkauft.",
    "TA→take_profit": "Festes Gewinnziel erreicht — Teilgewinn wird mitgenommen.",
    "X→price_target hit": "Der empfohlene Kursziel-Preis vom X-Signal wurde erreicht — Verkauf.",
    "X→stop_loss hit": "Die empfohlene Stop-Loss-Marke vom X-Signal wurde unterschritten — Verkauf.",
    "X+CMC consensus": "X (Twitter) und CMC-Community stimmen in die gleiche Richtung.",
    "strong consensus": "Mehrere Quellen (Technik + Social) stimmen überein — stärkeres Signal.",
    "multi_source": "Mehrere Signalquellen liefern dasselbe Bild.",
}

_RISK_MESSAGES = {
    "max open positions": "Maximale Anzahl offener Positionen erreicht — kein neuer Kauf möglich.",
    "daily trade limit": "Tageslimit für Trades erreicht — Bot pausiert bis morgen.",
    "max position concentration": "Dieser Coin wäre zu groß im Portfolio — Kauf wurde begrenzt oder blockiert.",
    "trade cooldown": "Kürzlich schon gehandelt — kurze Pause gegen zu häufiges Hin und Her.",
    "no position to sell": "Keine offene Position zum Verkaufen.",
    "no amount to sell": "Verkaufsmenge ist null — nichts zu verkaufen.",
    "trading disabled": "Handel ist ausgeschaltet (Modus OFF). Nur Analyse, kein Trade.",
    "live_confirm": "Live-Handel noch nicht bestätigt — sende /live_confirm.",
    "trust score": "X-Account-Vertrauenswert zu niedrig für Live-Handel.",
    "invalid price": "Kein gültiger Preis — Trade abgebrochen.",
    "min trade": "Betrag unter dem Mindest-Trade — zu klein für die Börse.",
}

_PARAM_LABELS = {
    "buy_regime": "Kauf-Stil",
    "rsi_buy_low": "RSI Kauf unten",
    "rsi_buy_high": "RSI Kauf oben",
    "volume_multiplier": "Mindest-Volumen",
    "rsi_sell_30": "RSI Verkauf Stufe 30%",
    "rsi_sell_20": "RSI Verkauf Stufe 20%",
    "take_profit_pct": "Gewinnziel %",
    "stop_loss_pct": "Verlustgrenze %",
    "cmc_trust_score": "CMC Vertrauen",
    "cmc_min_confidence": "CMC Mindest-Confidence",
    "reversal_rsi_cross_low": "Umkehr RSI unten",
    "reversal_rsi_cross_high": "Umkehr RSI oben",
    "reversal_volume_multiplier": "Umkehr Volumen",
}

_AMPLEGLOSS = {
    "Stark Bullish": "Sehr bullisch — Aufwärtstrend mit starkem Volumen.",
    "Bullish": "Bullisch — eher Aufwärtsdruck.",
    "Neutral": "Neutral — kein klares Signal.",
    "Bearish": "Bärisch — eher Abwärtsdruck.",
    "Stark Bearish": "Sehr bärisch — Abwärtstrend mit starkem Volumen.",
}


def explanations_config(config=None) -> dict:
    from core.config import get_bot_config

    cfg = config or get_bot_config()
    defaults = {
        "enabled": True,
        "verbosity": "verbose",
        "language": "de",
        "show_technical_codes": True,
        "notify_hermes_every_cycle": True,
        "notify_cmc_digest": True,
        "notify_x_digest": True,
        "notify_social_hold_explanations": True,
        "notify_blocked_trades": True,
        "cmc_digest_min_confidence": 60,
        "x_digest_min_effective_confidence": 70,
    }
    raw = cfg.observability_config.get("telegram_explanations", {})
    return {**defaults, **raw}


def explanations_enabled(config=None) -> bool:
    return bool(explanations_config(config).get("enabled", True))


def _match_rationale_part(part: str) -> str:
    part = part.strip()
    if part in _RATIONALE_PARTS:
        return _RATIONALE_PARTS[part]
    if part.startswith("X→") and "@" in part:
        m = re.match(r"X→(\w+)@([^(]+)\((\d+)%\)", part)
        if m:
            action, account, conf = m.groups()
            act_de = "Kauf" if action == "BUY" else "Verkauf" if action == "SELL" else action
            return (
                f"X-Account @{account} empfiehlt {act_de} "
                f"(Confidence {conf}%, Trust-Score fließt ein)."
            )
    if part.startswith("CMC→"):
        m = re.match(r"CMC→(\w+)\((\d+)%\)", part)
        if m:
            action, conf = m.groups()
            act_de = "Kauf" if action == "BUY" else "Verkauf" if action == "SELL" else action
            return f"CMC-Community tendiert zu {act_de} (Stimmung {conf}%)."
    if part.startswith("TA→"):
        return _RATIONALE_PARTS.get(part, f"Technische Analyse: {part[3:]}")
    return part


def explain_rationale(rationale: str) -> str:
    if not rationale:
        return "Keine zusätzliche Begründung hinterlegt."
    parts = [p.strip() for p in rationale.split("|") if p.strip()]
    lines = [_match_rationale_part(p) for p in parts]
    return " ".join(lines)


def explain_risk(message: str, code: str = "") -> str:
    if not message:
        return "Trade wurde vom Risiko-Manager blockiert."
    lower = message.lower()
    for key, de in _RISK_MESSAGES.items():
        if key in lower:
            return de
    if code == "trade_cooldown":
        return _RISK_MESSAGES["trade cooldown"]
    if code == "max_open_positions":
        return _RISK_MESSAGES["max open positions"]
    if code == "mode_blocked":
        if "off" in lower:
            return _RISK_MESSAGES["trading disabled"]
        if "live_confirm" in lower:
            return _RISK_MESSAGES["live_confirm"]
    return message


def explain_sell_tier(signal: str) -> str:
    sig = (signal or "").upper()
    if "STOP_FULL" in sig or sig.endswith("_FULL"):
        return "Not-Verkauf: Verlustgrenze — gesamte Position wird geschlossen."
    if "STOP_PARTIAL" in sig or "PARTIAL_50" in sig:
        return "Verlustgrenze — 50 % werden verkauft, Rest bleibt unter Beobachtung."
    if "TP" in sig or "TAKE_PROFIT" in sig:
        return "Gewinnziel erreicht — Teil der Position wird mit Gewinn verkauft."
    if "30" in sig:
        return "RSI überkauft (Stufe 2) — 30 % der Position werden verkauft."
    if "20" in sig:
        return "RSI überkauft (Stufe 1) — 20 % der Position werden verkauft."
    if "SELL" in sig:
        return "Verkaufssignal — Position wird (teilweise) reduziert."
    return ""


def explain_ampel(ampel_text: str) -> str:
    if not ampel_text:
        return ""
    for key, gloss in _AMPLEGLOSS.items():
        if key.lower() in (ampel_text or "").lower():
            return gloss
    return f"Marktampel: {ampel_text}."


def _social_detail_lines(social_ctx: dict | None) -> list[str]:
    if not social_ctx:
        return []
    lines = []
    x = social_ctx.get("x")
    if x:
        lines.append(
            f"X: @{x.get('account', '?')} → {x.get('action', '?')} "
            f"({x.get('confidence', 0)}%, Trust {x.get('trust_score', '?')})"
        )
    cmc = social_ctx.get("cmc")
    if cmc:
        lines.append(
            f"CMC: {cmc.get('action', '?')} ({cmc.get('confidence', 0)}%) — "
            f"Votes {cmc.get('votes_bullish', 0)}↑/{cmc.get('votes_bearish', 0)}↓"
        )
        if cmc.get("rationale"):
            lines.append(f"  \"{cmc['rationale'][:100]}\"")
    return lines


def explain_trade(
    analysis,
    trade_result=None,
    social_ctx: dict | None = None,
    signal: str = "",
) -> dict[str, Any]:
    """Build DE explanation fields for a trade notification."""
    action = getattr(analysis, "action", signal) or signal
    rationale = getattr(analysis, "rationale", "") or ""
    normalized = getattr(analysis, "normalized_action", action)
    sources = list(getattr(analysis, "sources", None) or [])

    why_parts = []
    if "BUY" in str(action):
        why_parts.append(explain_rationale(rationale))
    elif "SELL" in str(action):
        tier = explain_sell_tier(action)
        if tier:
            why_parts.append(tier)
        why_parts.append(explain_rationale(rationale))
    else:
        why_parts.append(explain_rationale(rationale))

    ampel = getattr(analysis, "ampel_text", "")
    ampel_gloss = explain_ampel(ampel)
    if ampel_gloss and "HOLD" in str(normalized):
        why_parts.append(ampel_gloss)

    why_de = " ".join(p for p in why_parts if p).strip() or "Bot hat eine Marktentscheidung getroffen."

    tech_line = rationale
    if getattr(analysis, "rsi", None):
        rsi = analysis.rsi
        if isinstance(rsi, (int, float)) and rsi > 0:
            tech_line = (tech_line + f" | RSI={rsi:.1f}").strip(" |")

    blocks = {}
    if trade_result and not trade_result.executed and trade_result.message:
        blocks["risk_de"] = explain_risk(trade_result.message)

    source_de = []
    if "technical" in sources:
        source_de.append("Technische Analyse")
    if "x" in sources:
        source_de.append("X/Twitter")
    if "cmc" in sources:
        source_de.append("CMC Community")
    if "take_profit" in sources:
        source_de.append("Gewinnziel")
    if "stop_loss" in sources:
        source_de.append("Stop-Loss")
    if "hermes" in sources or (social_ctx and social_ctx.get("hermes")):
        source_de.append("Hermes-Strategie")

    return {
        "why_de": why_de,
        "tech_line": tech_line,
        "source_de": ", ".join(source_de) or "Automatisch",
        "social_lines": _social_detail_lines(social_ctx),
        "blocks": blocks,
    }


def explain_hold_with_social(analysis, social_ctx: dict | None) -> str | None:
    """When TA says HOLD but social is strong — explain why no trade."""
    if not social_ctx:
        return None
    x = social_ctx.get("x")
    cmc = social_ctx.get("cmc")
    parts = []
    if x and x.get("action") in ("BUY", "SELL"):
        parts.append(f"X (@{x.get('account')}) sagt {x['action']}")
    if cmc and cmc.get("action") in ("BUY", "SELL"):
        parts.append(f"CMC sagt {cmc['action']} ({cmc.get('confidence', 0)}%)")
    if not parts:
        return None
    social_txt = " und ".join(parts)
    ta = explain_rationale(getattr(analysis, "rationale", "") or "")
    return (
        f"{social_txt}, aber die Technik gibt noch kein klares Signal — "
        f"daher kein Trade. ({ta[:120]})"
    )


def explain_hermes_cycle(record: dict, proposal=None) -> str:
    verdict = record.get("verdict", "unknown")
    var = record.get("variable", "?")
    old_v = record.get("old_value", "?")
    new_v = record.get("new_value", "?")
    symbol = record.get("symbol", "?")
    reason = record.get("verdict_reason", "")
    folds_won = record.get("folds_won")
    folds_total = record.get("folds_total")
    cf = record.get("counterfactual_metrics") or {}
    live = record.get("live_metrics") or {}

    param_label = _PARAM_LABELS.get(var, var)

    if verdict == "promoted":
        headline = f"✅ Hermes hat '{param_label}' angepasst ({old_v} -> {new_v}) für {symbol}."
        detail = (
            "Der Bot hat die Einstellung im Backtest verbessert und übernimmt sie ins Live-Trading."
        )
    elif record.get("live_veto"):
        headline = f"🛡️ Hermes hat eine Änderung an {symbol} blockiert (Live-Schutz)."
        pnl = live.get("live_sell_pnl", 0)
        detail = (
            f"Vorschlag {param_label}: {old_v}->{new_v} wäre im Backtest ok, "
            f"aber echte Trades der letzten Tage ({pnl:+.1f} USDT Verkaufs-PnL) sprechen dagegen."
        )
    else:
        headline = f"🔬 Hermes-Test abgelehnt für {symbol}: {param_label} {old_v}->{new_v}."
        if folds_won is not None and folds_total:
            detail = (
                f"Nur {folds_won}/{folds_total} Zeitfenster im Backtest waren besser — "
                f"zu unsicher für eine Live-Änderung."
            )
        elif "not improved" in reason.lower():
            detail = "Die Variante war im Backtest nicht klar besser als die aktuelle Einstellung."
        elif "below success" in reason.lower():
            detail = "Metriken (z. B. Sharpe, Win-Rate) unterschreiten die Mindestkriterien."
        else:
            detail = reason or "Experiment hat die Validierung nicht bestanden."

    if cf.get("pnl_delta") is not None:
        detail += f" Counterfactual-PnL-Delta: {cf['pnl_delta']:+.2f} USDT."

    tech = f"{var} {old_v}->{new_v} | verdict={verdict}"
    if reason:
        tech += f" | {reason[:80]}"

    return f"{headline}\n{detail}\n<code>{tech}</code>"


def describe_param_change(key: str, value) -> str:
    label = _PARAM_LABELS.get(key, key)
    return f"{label}: {value}"


def explain_cmc_signal(signal) -> str:
    action = getattr(signal, "action", "?")
    coin = getattr(signal, "coin", "?")
    conf = getattr(signal, "confidence", 0)
    bull = getattr(signal, "votes_bullish", 0)
    bear = getattr(signal, "votes_bearish", 0)
    rat = getattr(signal, "rationale", "") or ""
    act_de = "Kauf" if action == "BUY" else "Verkauf" if action == "SELL" else "Abwarten"
    line = (
        f"<b>{coin}</b> — Community tendiert zu <b>{act_de}</b> ({conf}%). "
        f"Stimmen: {bull} bullish / {bear} bearish."
    )
    if rat:
        line += f"\n  {rat[:120]}"
    return line


def explain_x_signal(signal) -> str:
    account = getattr(signal, "account", "?")
    action = getattr(signal, "action", "?")
    coin = getattr(signal, "coin", "?")
    conf = getattr(signal, "confidence", 0)
    eff = getattr(signal, "effective_confidence", conf)
    trust = getattr(signal, "trust_score", "?")
    rat = getattr(signal, "rationale", "") or ""
    act_de = "Kauf" if action == "BUY" else "Verkauf" if action == "SELL" else action
    line = (
        f"<b>{coin}</b> — @{account} empfiehlt <b>{act_de}</b> "
        f"({conf}%, effektiv {eff:.0f}%, Trust {trust})."
    )
    if rat:
        line += f"\n  {rat[:120]}"
    return line


def format_decision_entry(entry: dict, show_technical: bool = True) -> str:
    sym = (entry.get("symbol") or "?").replace("/USDT", "")
    action = entry.get("action", "HOLD")
    ts = (entry.get("timestamp") or "")[:16].replace("T", " ")
    executed = entry.get("executed")
    status = "✅" if executed else "🚫" if entry.get("trade_message") else "👀"
    why = explain_rationale(entry.get("rationale", ""))
    line = f"{status} <b>{sym}</b> {action} — {why[:100]}"
    if show_technical and entry.get("rationale"):
        line += f"\n  <code>{entry['rationale']}</code>"
    if entry.get("trade_message") and not executed:
        line += f"\n  <i>{explain_risk(entry['trade_message'])}</i>"
    line += f"\n  <i>{ts}</i>"
    return line