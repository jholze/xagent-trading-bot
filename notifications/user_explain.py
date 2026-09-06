"""Layperson German explanations for Telegram notifications."""

from __future__ import annotations

import re
from html import escape
from typing import Any

_RATIONALE_PARTS = {
    "TA→BUY": "Technische Analyse sieht eine Kaufchance (RSI und Volumen passen).",
    "TA→HOLD": "Technische Analyse gibt kein klares Kauf- oder Verkaufssignal — abwarten.",
    "TA→SELL": "Technische Analyse sieht Verkaufsdruck.",
    "TA→SELL_20": "RSI ist überkauft — wir verkaufen 20 % der Position, der Rest bleibt investiert.",
    "TA→SELL_30": "RSI ist stark überkauft — wir verkaufen weitere 30 % (gestaffelter Exit).",
    "TA→SELL_STOP_FULL": "Verlustgrenze erreicht — Position wird vollständig geschlossen zum Schutz.",
    "TA→SELL_STOP_PARTIAL": "Verlustgrenze erreicht — 50 % werden geschlossen, Rest bleibt unter Beobachtung.",
    "TA→SELL_TP": "Zielgewinn erreicht — ein Teil der Position wird mit Gewinn verkauft.",
    "TA→take_profit": "Festes Gewinnziel erreicht — Teilgewinn wird mitgenommen.",
    "X→price_target hit": "Der empfohlene Kursziel-Preis vom X-Signal wurde erreicht — Verkauf.",
    "X→stop_loss hit": "Die empfohlene Stop-Loss-Marke vom X-Signal wurde unterschritten — Verkauf.",
    "X+CMC consensus": "X (Twitter) und CMC-Signal (Markt/Community) zeigen in die gleiche Richtung.",
    "strong consensus": "Mehrere Quellen (Technik + Social) stimmen überein — stärkeres Signal.",
    "multi_source": "Mehrere Signalquellen liefern dasselbe Bild.",
    "DCA->accumulation": "Nachkauf (DCA) in der Akkumulationsphase — Position wird bei Dip vergrößert, Exit-Leiter bleibt unverändert.",
    "DCA→accumulation": "Nachkauf (DCA) in der Akkumulationsphase — Position wird bei Dip vergrößert, Exit-Leiter bleibt unverändert.",
    "Trail→ATR stop": "Trailing-Stop (ATR) ausgelöst — Position wird geschlossen, um Gewinn oder Schutz zu sichern.",
    "Trail→take profit": "Trailing-Take-Profit erreicht — Gewinn wird mitgenommen.",
    "Life→max profit": "Maximale Haltedauer im Gewinn erreicht — Position wird geschlossen.",
    "Time→profit exit": "Zeitbasiertes Gewinnziel erreicht — Verkauf.",
    "15m→vol entry": "15-Minuten-Volumen-Sensor sieht einen Einstieg.",
}

_RISK_MESSAGES = {
    "max open positions": "Maximale Anzahl offener Positionen erreicht — kein neuer Kauf möglich.",
    "daily trade limit": "Tageslimit für Käufe erreicht — Verkäufe zählen separat.",
    "max_daily_sells": "Tageslimit für Verkäufe erreicht.",
    "max_daily_dca_buys": "Tageslimit für DCA-Nachkäufe erreicht.",
    "max_daily_dca_usdt": "Tages-Volumenlimit für DCA-Nachkäufe erreicht.",
    "max position concentration": "Dieser Coin wäre zu groß im Portfolio — Kauf wurde begrenzt oder blockiert.",
    "trade cooldown": "Kürzlich schon gehandelt — kurze Pause gegen zu häufiges Hin und Her.",
    "no position to sell": "Keine offene Position zum Verkaufen.",
    "no amount to sell": "Verkaufsmenge ist null — nichts zu verkaufen.",
    "trading disabled": "Handel ist ausgeschaltet (Modus OFF). Nur Analyse, kein Trade.",
    "live_confirm": "Live-Handel noch nicht bestätigt — sende /live_confirm.",
    "trust score": "X-Account-Vertrauenswert zu niedrig für Live-Handel.",
    "invalid price": "Kein gültiger Preis — Trade abgebrochen.",
    "min trade": "Betrag unter dem Mindest-Trade — zu klein für die Börse.",
    "cash floor": "Cash-Floor erreicht — Mindest-Bargeld bleibt frei (keine Auto-Käufe).",
    "size_too_small": "Betrag nach Limits unter dem Mindest-Trade.",
}

_RISK_CODES = {
    "phantom_symbol": "Symbol ist nicht handelbar (Phantom/Test-Coin) — kein Orderversand.",
    "one_way": "Gegenläufige Position blockiert — Long und Short gleichzeitig sind nicht erlaubt.",
    "side_check_error": "Seiten-Prüfung ist fehlgeschlagen — Trade vorsichtshalber blockiert.",
    "trade_cooldown": "Kürzlich schon gehandelt — kurze Pause gegen zu häufiges Hin und Her.",
    "position_locked": "Position ist gesperrt — kein automatischer Verkauf oder Nachkauf.",
    "position_lock_check_error": "Sperr-Prüfung ist fehlgeschlagen — Trade vorsichtshalber blockiert.",
    "no_amount": "Verkaufsmenge ist null — nichts zu verkaufen.",
    "partial_sell_guard": "Teilverkauf nicht erlaubt — Guard hat den Schnitt blockiert.",
    "max_daily_sells": "Tageslimit für Verkäufe erreicht.",
    "stablecoin_blocked": "Stablecoin-Käufe sind gesperrt.",
    "correlated_tier_selloff": "Korrelierte Coins verkaufen bereits — neuer Kauf in dieser Gruppe blockiert.",
    "universe_trade_cap": "Tageslimit für neue Coins aus dem Universum erreicht.",
    "gainer_chase_guard": "Gainer-Chase-Guard — Coin ist schon zu stark gelaufen, Kauf blockiert.",
    "market_block": "Markt-Filter blockiert neue Käufe (zu riskantes Umfeld).",
    "market_bias_degraded": "Markt-Bias ist unsicher/degradiert — keine neuen Auto-Käufe.",
    "coin_memory_soft_block": "Coin-Memory rät von diesem Trade ab (historisch schlechte Pfade).",
    "watchlist_quality": "Watchlist-Qualität zu niedrig — Coin darf nicht als neuer Kauf rein.",
    "venue_liquidity_block": "Zu wenig Liquidität an der Börse — Trade blockiert.",
    "macro_calendar_block": "Makro-Kalender (Event) — neue Käufe in diesem Fenster gesperrt.",
    "slot_eviction_no_price": "Slot-Freimachung nicht möglich — kein Preis für den Eviction-Kandidaten.",
    "max_open_positions": "Maximale Anzahl offener Positionen erreicht — kein neuer Kauf möglich.",
    "max_position_percent": "Dieser Coin wäre zu groß im Portfolio — Kauf wurde begrenzt oder blockiert.",
    "cash_floor": "Cash-Floor erreicht — Mindest-Bargeld bleibt frei (keine Auto-Käufe).",
    "size_too_small": "Betrag nach Limits unter dem Mindest-Trade.",
    "sensor_reentry_cooloff": "Nach dem 15m-Sensor-Trade gilt eine Pause — kein sofortiger Re-Entry.",
    "max_daily_dca_buys": "Tageslimit für DCA-Nachkäufe erreicht.",
    "max_daily_trades": "Tageslimit für Käufe erreicht — Verkäufe zählen separat.",
    "max_daily_dca_usdt": "Tages-Volumenlimit für DCA-Nachkäufe erreicht.",
    "daily_loss_limit": "Tagesverlust-Limit erreicht — Handel für heute gestoppt.",
    "shorts_live_blocked": "Live-Shorts sind nicht freigeschaltet.",
    "no_short": "Kein offener Short zum Cover.",
    "shorts_disabled": "Shorts sind in der Config ausgeschaltet.",
    "shorts_slots": "Short-Slots voll — kein weiterer Short.",
    "short_mcap": "Marktkapitalisierung zu klein für einen Short.",
    "bad_price": "Kein gültiger Preis — Trade abgebrochen.",
    "short_margin": "Nicht genug Margin für den Short.",
    "short_margin_pct": "Short würde den Margin-Anteil am Portfolio überschreiten.",
    "mode_blocked": "Handel ist in diesem Modus blockiert.",
    "entries_paused": "Neue Käufe sind pausiert — Stops, Trails und Exit-Leiter laufen weiter.",
    "exits_paused": "Normale Verkäufe sind pausiert — Notverkäufe (Stops) laufen weiter.",
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
        "notify_lc_digest": True,
        "notify_x_digest": True,
        "notify_social_hold_explanations": True,
        "notify_blocked_trades": True,
        "cmc_digest_min_confidence": 60,
        "lc_digest_min_confidence": 55,
        "x_digest_min_effective_confidence": 70,
    }
    raw = cfg.observability_config.get("telegram_explanations", {})
    return {**defaults, **raw}


def explanations_enabled(config=None) -> bool:
    return bool(explanations_config(config).get("enabled", True))


_PCT_SUFFIX = re.compile(r"\(\d+%\)$")


def _normalize_rationale_key(part: str) -> str:
    """Map engine ASCII arrows / percent suffixes onto lookup keys.

    Emitters in decision_engine stay unchanged (existing tests freeze those
    strings). Lookup accepts ``TA->BUY``, ``CMC->SELL(70%)`` and
    ``multi-source consensus``.
    """
    key = part.strip().replace("->", "→")
    if key == "multi-source consensus":
        return "multi_source"
    return _PCT_SUFFIX.sub("", key).strip()


def _match_rationale_part(part: str) -> str:
    raw = part.strip()
    arrowed = raw.replace("->", "→")
    lookup = _normalize_rationale_key(raw)

    # Keep confidence figures from the original token (regex before dict).
    if arrowed.startswith("X→") and "@" in arrowed:
        m = re.match(r"X→(\w+)@([^(]+)\((\d+)%\)", arrowed)
        if m:
            action, account, conf = m.groups()
            act_de = "Kauf" if action == "BUY" else "Verkauf" if action == "SELL" else action
            return (
                f"X-Account @{account} empfiehlt {act_de} "
                f"(Confidence {conf}%, Trust-Score fließt ein)."
            )
    if arrowed.startswith("CMC→"):
        m = re.match(r"CMC→(\w+)\((\d+)%\)", arrowed)
        if m:
            action, conf = m.groups()
            act_de = "Kauf" if action == "BUY" else "Verkauf" if action == "SELL" else action
            return f"CMC-Signal tendiert zu {act_de} (Score {conf}%)."
    if arrowed.startswith("LC→"):
        m = re.match(r"LC→(\w+)\((\d+)%\)", arrowed)
        if m:
            action, conf = m.groups()
            act_de = "Kauf" if action == "BUY" else "Verkauf" if action == "SELL" else action
            return f"LunarCrush tendiert zu {act_de} (Score {conf}%)."

    if arrowed in _RATIONALE_PARTS:
        return _RATIONALE_PARTS[arrowed]
    if lookup in _RATIONALE_PARTS:
        return _RATIONALE_PARTS[lookup]
    if lookup == "multi_source":
        return _RATIONALE_PARTS["multi_source"]
    if arrowed.endswith(" consensus") and "+" in arrowed:
        return _RATIONALE_PARTS["multi_source"]
    if arrowed.startswith("shadow→"):
        action = arrowed.split("→", 1)[1] or "?"
        return (
            f"Shadow-Signal {action} — nur Beobachtung, kein Live-Trade."
        )
    if arrowed.startswith("TA→"):
        return _RATIONALE_PARTS.get(arrowed, f"Technische Analyse: {arrowed[3:]}")
    return raw


def explain_rationale(rationale: str) -> str:
    if not rationale:
        return "Keine zusätzliche Begründung hinterlegt."
    parts = [p.strip() for p in rationale.split("|") if p.strip()]
    lines = [_match_rationale_part(p) for p in parts]
    return " ".join(lines)


def explain_risk(message: str, code: str = "") -> str:
    code = (code or "").strip()
    if not message and not code:
        return "Trade wurde vom Risiko-Manager blockiert."
    lower = (message or "").lower()
    if code == "trade_cooldown":
        if message and "Stop-loss rebuy cooldown" in message:
            return "Nach Stop-Loss-Verkauf gilt eine längere Pause — kein automatischer Re-Entry (verhindert Churn)."
        if message and "Rebuy cooldown" in message:
            return "Kürzlich verkauft — Re-Kauf erst nach der konfigurierten Pause (verhindert sinnloses Hin-und-Her)."
        if message and "DCA interval" in message:
            return "DCA-Nachkauf zu früh — Mindestabstand zwischen Nachkäufen noch nicht erreicht."
        return _RISK_CODES.get("trade_cooldown") or _RISK_MESSAGES["trade cooldown"]
    if code == "mode_blocked":
        if "off" in lower:
            return _RISK_MESSAGES["trading disabled"]
        if "live_confirm" in lower:
            return _RISK_MESSAGES["live_confirm"]
        return _RISK_CODES["mode_blocked"]
    if code in _RISK_CODES:
        return _RISK_CODES[code]
    if message:
        for key, de in _RISK_MESSAGES.items():
            if key in lower:
                return de
    if not message:
        return "Trade wurde vom Risiko-Manager blockiert."
    return message


def explain_sell_tier(signal: str, sources: list | None = None, ampel_text: str = "") -> str:
    sig = (signal or "").upper()
    src = [str(s).lower() for s in (sources or [])]
    ampel = (ampel_text or "").lower()
    is_grid = "grid" in src or "grid" in ampel or "grid sell" in ampel or "grid buy" in ampel

    if "STOP_FULL" in sig or sig.endswith("_FULL"):
        return "Not-Verkauf: Verlustgrenze — gesamte Position wird geschlossen."
    if "STOP_PARTIAL" in sig or "PARTIAL_50" in sig:
        if is_grid and "regime" in " ".join(src):
            return "Defensiv/Grid: 50 % der Position werden abgebaut (Regime-Flip)."
        return "Verlustgrenze — 50 % werden verkauft, Rest bleibt unter Beobachtung."
    if "TP" in sig or "TAKE_PROFIT" in sig:
        return "Gewinnziel erreicht — Teil der Position wird mit Gewinn verkauft."

    if is_grid:
        # Prefer concrete ampel (e.g. "Grid sell L1 @ …") over RSI templates
        if ampel_text and "grid" in ampel:
            return (
                f"Grid-Level getroffen — Teilverkauf am Grid ({ampel_text}). "
                "Bezogen auf Grid-Center/Levels, nicht zwingend auf Einstand."
            )
        if "30" in sig:
            return "Grid-Level: ca. 30 % der Position werden am Sell-Level verkauft."
        if "20" in sig:
            return "Grid-Level: ca. 20 % der Position werden am Sell-Level verkauft."
        if "10" in sig:
            return "Grid-Level: ca. 10 % der Position werden am Sell-Level verkauft."
        return "Grid-Verkauf — Position wird am Sell-Level (teilweise) reduziert."

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
        kind = cmc_signal_kind(cmc)
        bull = cmc.get("votes_bullish", 0)
        bear = cmc.get("votes_bearish", 0)
        lines.append(
            f"CMC [{cmc_source_label_de(kind)}]: {cmc.get('action', '?')} "
            f"({cmc.get('confidence', 0)}%) — {cmc_score_line_de(kind, bull, bear)}"
        )
        if cmc.get("rationale"):
            lines.append(f"  \"{cmc['rationale'][:100]}\"")
    lc = social_ctx.get("lc")
    if lc:
        lines.append(
            f"LC: {lc.get('action', '?')} ({lc.get('confidence', 0)}%) — "
            f"Galaxy {lc.get('galaxy_score', 0):.0f}, AltRank {lc.get('alt_rank', 0)}, "
            f"Sentiment {lc.get('sentiment', 0):.0f}%"
        )
        if lc.get("rationale"):
            lines.append(f"  \"{lc['rationale'][:100]}\"")
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

    ampel = getattr(analysis, "ampel_text", "") or ""
    why_parts = []
    if "BUY_DCA" in str(action) or "BUY_DCA" in str(normalized):
        why_parts.append(
            "Nachkauf (DCA) — bestehende Position wird bei Dip vergrößert, nicht neu eröffnet."
        )
        why_parts.append(explain_rationale(rationale))
    elif "BUY" in str(action):
        why_parts.append(explain_rationale(rationale))
    elif "SELL" in str(action):
        tier = explain_sell_tier(action, sources=sources, ampel_text=ampel)
        if tier:
            why_parts.append(tier)
        # Skip generic TA rationales that contradict grid (e.g. RSI templates)
        is_grid = "grid" in [s.lower() for s in sources] or "grid" in ampel.lower()
        if not is_grid:
            why_parts.append(explain_rationale(rationale))
    else:
        why_parts.append(explain_rationale(rationale))

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
        blocks["risk_de"] = explain_risk(
            trade_result.message,
            code=getattr(trade_result, "code", "") or "",
        )

    source_de = []
    if any(str(s).lower().startswith("grid") or str(s).lower() == "grid" for s in sources) or (
        "grid" in (ampel or "").lower()
    ):
        source_de.append("Grid")
    if any("mode_hybrid" in str(s).lower() for s in sources):
        source_de.append("Hybrid-Mode")
    if any("mode_grid" in str(s).lower() for s in sources):
        source_de.append("Grid-Mode")
    if "technical" in sources:
        source_de.append("Technische Analyse")
    if "x" in sources:
        source_de.append("X/Twitter")
    if "cmc" in sources:
        cmc_meta = (social_ctx or {}).get("cmc") or {}
        if cmc_meta:
            source_de.append(f"CMC {cmc_source_label_de(cmc_signal_kind(cmc_meta))}")
        else:
            source_de.append("CMC")
    if "lc" in sources:
        source_de.append("LunarCrush")
    if "take_profit" in sources:
        source_de.append("Gewinnziel")
    if "stop_loss" in sources:
        source_de.append("Stop-Loss")
    if "entry_sensor_15m" in sources:
        source_de.append("15m Entry-Sensor")
    if "hermes" in sources or (social_ctx and social_ctx.get("hermes")):
        source_de.append("Hermes-Strategie")

    return {
        "why_de": why_de,
        "tech_line": tech_line,
        "source_de": ", ".join(source_de) or "Automatisch",
        "social_lines": _social_detail_lines(social_ctx),
        "blocks": blocks,
    }


def explain_hold_with_social(
    analysis,
    social_ctx: dict | None,
    blockers: dict | None = None,
) -> str | None:
    """Explain HOLD when social looked actionable but no trade executed."""
    if not social_ctx:
        return None

    from core.config import get_bot_config

    sources = set(getattr(analysis, "sources", None) or [])
    blockers = blockers or {}
    cfg = get_bot_config()
    counted = []
    gated = []

    x = social_ctx.get("x")
    if x and x.get("action") in ("BUY", "SELL"):
        if "x" in sources:
            counted.append(f"X (@{x.get('account')}) → {x['action']}")
        elif x.get("action") == "BUY":
            eff = float(x.get("effective_confidence", x.get("confidence", 0)) or 0)
            gated.append(f"X BUY (eff. {eff:.0f}%) unter Schwelle")

    cmc = social_ctx.get("cmc")
    if cmc and cmc.get("action") in ("BUY", "SELL"):
        if "cmc" in sources:
            counted.append(f"CMC → {cmc['action']} ({cmc.get('confidence', 0)}%)")
        elif cmc.get("action") == "BUY":
            conf = float(cmc.get("confidence", 0) or 0)
            trust = float(cmc.get("trust_score", cfg.cmc_config.get("trust_score", 65)) or 65)
            eff = conf * trust / 100.0
            min_c = float(cfg.cmc_config.get("min_confidence", 60))
            gated.append(f"CMC BUY {conf:.0f}% (eff. {eff:.0f}%, Schwelle {min_c:.0f}%)")

    lc = social_ctx.get("lc")
    if lc and lc.get("action") in ("BUY", "SELL"):
        if "lc" in sources:
            counted.append(f"LunarCrush → {lc['action']} ({lc.get('confidence', 0)}%)")
        elif lc.get("action") == "BUY":
            conf = float(lc.get("confidence", 0) or 0)
            trust = float(lc.get("trust_score", cfg.lunarcrush_config.get("trust_score", 72)) or 72)
            eff = conf * trust / 100.0
            min_c = float(cfg.lunarcrush_config.get("min_confidence", 40))
            gated.append(f"LC BUY {conf:.0f}% (eff. {eff:.0f}%, Schwelle {min_c:.0f}%)")

    if not counted:
        return None

    open_pos = int(blockers.get("open_positions", 0) or 0)
    max_pos = int(blockers.get("max_open_positions", cfg.max_open_positions) or 0)
    if open_pos >= max_pos:
        return (
            f"{' + '.join(counted)} — aber Max. offene Positionen erreicht "
            f"({open_pos}/{max_pos}), daher kein Kauf."
        )

    if blockers.get("has_position"):
        return f"{' + '.join(counted)} — Position bereits offen, kein Nachkauf."

    shadow = getattr(analysis, "shadow_action", "") or ""
    if shadow and "BUY" in shadow:
        return (
            f"{' + '.join(counted)} — Kauf-Signal im Shadow-Modus "
            f"(volatile_altcoin), daher kein Live-Trade."
        )

    ta = explain_rationale(getattr(analysis, "rationale", "") or "") or "TA->HOLD"
    return (
        f"{' + '.join(counted)}, aber die Technik gibt noch kein klares Signal — "
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
    elif verdict == "pending":
        headline = (
            f"⏳ Hermes will '{param_label}' anpassen ({old_v} -> {new_v}) für {symbol}."
        )
        wp = record.get("win_probability")
        ntr = record.get("total_trades")
        if wp is not None and ntr is not None:
            from hermes.significance import format_win_probability

            detail = (
                f"{format_win_probability(float(wp), int(ntr))}, Hold-out ok. "
                f"Veto mit /hermes_veto {record.get('id', '')}."
            )
        else:
            detail = "Qualifiziert — Veto-Fenster läuft, noch nicht live."
    elif verdict == "vetoed":
        headline = f"🛑 Hermes-Promotion für {symbol} per Veto storniert."
        detail = f"'{param_label}' bleibt bei {old_v}."
    elif verdict == "rolled_back":
        headline = f"↩️ Hermes hat '{param_label}' für {symbol} zurückgesetzt."
        detail = "Snapshot vor der Promotion wiederhergestellt."
    elif verdict == "suppressed":
        headline = (
            f"👁️ Hermes-Test (observe) für {symbol}: {param_label} {old_v}->{new_v}."
        )
        detail = "Qualifiziert, aber observe-Modus schreibt keine Config."
    elif verdict == "inconclusive":
        headline = (
            f"⚪ Hermes-Test unentschieden für {symbol}: {param_label} {old_v}->{new_v}."
        )
        detail = (
            "Baseline und Variante hatten 0 Trades im Fenster — "
            "kein Urteil über die Strategie (#308)."
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

    from notifications.coin_links import format_links_line

    ticker = (symbol or "").replace("/USDT", "").split("/")[0]
    links = format_links_line(ticker) if ticker else ""
    links_part = f"\n{links}" if links else ""
    return (
        f"{escape(headline, quote=False)}\n"
        f"{escape(detail, quote=False)}{links_part}\n"
        f"<code>{escape(tech, quote=False)}</code>"
    )


def describe_param_change(key: str, value) -> str:
    label = _PARAM_LABELS.get(key, key)
    return f"{label}: {value}"


def explain_lc_signal(signal) -> str:
    from notifications.coin_links import format_links_line, format_ticker_html

    action = getattr(signal, "action", "?")
    coin = getattr(signal, "coin", "?")
    conf = getattr(signal, "confidence", 0)
    galaxy = getattr(signal, "galaxy_score", 0)
    alt_rank = getattr(signal, "alt_rank", 0)
    sentiment = getattr(signal, "sentiment", 0)
    rat = getattr(signal, "rationale", "") or ""
    act_de = "Kauf" if action == "BUY" else "Verkauf" if action == "SELL" else "Abwarten"
    coin_html = format_ticker_html(coin, symbol_suffix="")
    links = format_links_line(coin)
    links_part = f"\n{links}" if links else ""
    line = (
        f"<b>{coin_html}</b> — LunarCrush tendiert zu <b>{act_de}</b> ({conf}%). "
        f"Galaxy {galaxy:.0f}, AltRank {alt_rank}, Sentiment {sentiment:.0f}%.{links_part}"
    )
    if rat:
        line += f"\n  {rat[:120]}"
    return line


def _cmc_attr(obj, key: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def cmc_signal_kind(signal) -> str:
    """Classify CMC signal origin for honest operator labels.

    - community: real CMC community endpoint votes
    - market_trending: Startup/Builder trending/latest (not community chat)
    - listings_synthetic: listings mcap-band momentum with synthetic scores
    - quotes_synthetic: quotes/latest 24h% mapped to fake vote ratios
    """
    qf = bool(_cmc_attr(signal, "quotes_fallback", False))
    tier = str(_cmc_attr(signal, "signal_tier", "") or "").lower()
    author = str(
        _cmc_attr(signal, "account", None)
        or _cmc_attr(signal, "author", None)
        or ""
    )
    rat = str(_cmc_attr(signal, "rationale", "") or "")
    pid = str(_cmc_attr(signal, "post_id", "") or "")
    rat_l = rat.lower()

    if tier == "quote" or author == "CMC Market" or pid.startswith("cmc_quote_"):
        return "quotes_synthetic"
    if qf or pid.startswith("cmc_mkt_listings_") or "mcap-band" in rat_l or (
        "listings" in rat_l and "market trending" in rat_l
    ):
        return "listings_synthetic"
    if tier == "trending" or author == "CMC Market Trending" or "market trending" in rat_l:
        return "market_trending"
    if tier == "community" or author in ("CMC Community", "CMC Trending", "CMC Content"):
        return "community"
    if qf:
        return "quotes_synthetic"
    return "market_trending" if tier else "quotes_synthetic"


def cmc_source_label_de(kind: str) -> str:
    return {
        "community": "Community",
        "market_trending": "Markt-Trending",
        "listings_synthetic": "Listings (abgeleitet)",
        "quotes_synthetic": "Kursdaten (abgeleitet)",
    }.get(kind, "CMC")


def cmc_score_line_de(kind: str, bull: int | float, bear: int | float) -> str:
    """Never call synthetic scores 'Community-Votes'."""
    b, e = int(bull or 0), int(bear or 0)
    if kind == "community":
        return f"Community-Votes {b}↑/{e}↓"
    if kind == "market_trending":
        return f"Trend-Score {b}↑/{e}↓ (kein Community-Vote)"
    if kind == "listings_synthetic":
        return f"Listings-Score {b}↑/{e}↓ (aus Momentum, kein Community-Vote)"
    if kind == "quotes_synthetic":
        return f"Kurs-Score {b}↑/{e}↓ (aus 24h-%, kein Community-Vote)"
    return f"Score {b}↑/{e}↓ (kein Community-Vote)"


def _cmc_tier_label(signal) -> str:
    """Short bracket label for /cmc lists."""
    return cmc_source_label_de(cmc_signal_kind(signal))


def explain_cmc_signal(signal) -> str:
    from notifications.coin_links import format_links_line, format_ticker_html

    action = getattr(signal, "action", "?")
    coin = getattr(signal, "coin", "?")
    conf = getattr(signal, "confidence", 0)
    bull = getattr(signal, "votes_bullish", 0)
    bear = getattr(signal, "votes_bearish", 0)
    rat = getattr(signal, "rationale", "") or ""
    kind = cmc_signal_kind(signal)
    tier = cmc_source_label_de(kind)
    rank = int(getattr(signal, "trending_rank", 0) or 0)
    rank_part = f" #{rank}" if rank > 0 and kind == "market_trending" else ""
    if action == "BUY":
        act_de = "bullish" if kind in ("quotes_synthetic", "listings_synthetic") else "Kauf-Signal"
    elif action == "SELL":
        act_de = "bearish" if kind in ("quotes_synthetic", "listings_synthetic") else "Verkauf-Signal"
    else:
        act_de = "neutral"
    coin_html = format_ticker_html(coin, symbol_suffix="")
    links = format_links_line(coin)
    links_part = f"\n{links}" if links else ""
    line = (
        f"<b>[{tier}{rank_part}]</b> {coin_html} — <b>{act_de}</b> ({conf}%). "
        f"{cmc_score_line_de(kind, bull, bear)}.{links_part}"
    )
    if rat:
        line += f"\n  {rat[:120]}"
    return line


def explain_x_signal(signal) -> str:
    from notifications.coin_links import format_links_line, format_ticker_html

    account = getattr(signal, "account", "?")
    action = getattr(signal, "action", "?")
    coin = getattr(signal, "coin", "?")
    conf = getattr(signal, "confidence", 0)
    eff = getattr(signal, "effective_confidence", conf)
    trust = getattr(signal, "trust_score", "?")
    rat = getattr(signal, "rationale", "") or ""
    act_de = "Kauf" if action == "BUY" else "Verkauf" if action == "SELL" else action
    coin_html = format_ticker_html(coin, symbol_suffix="")
    links = format_links_line(coin)
    links_part = f"\n{links}" if links else ""
    line = (
        f"<b>{coin_html}</b> — @{account} empfiehlt <b>{act_de}</b> "
        f"({conf}%, effektiv {eff:.0f}%, Trust {trust}).{links_part}"
    )
    if rat:
        line += f"\n  {rat[:120]}"
    return line


def format_decision_entry(entry: dict, show_technical: bool = True) -> str:
    from notifications.coin_links import format_ticker_html

    sym = (entry.get("symbol") or "?").replace("/USDT", "")
    sym_html = format_ticker_html(sym, symbol_suffix="")
    action = entry.get("action", "HOLD")
    ts = (entry.get("timestamp") or "")[:16].replace("T", " ")
    executed = entry.get("executed")
    status = "✅" if executed else "🚫" if entry.get("trade_message") else "👀"
    why = explain_rationale(entry.get("rationale", ""))
    line = f"{status} <b>{sym_html}</b> {action} — {escape(why[:100], quote=False)}"
    if show_technical and entry.get("rationale"):
        line += f"\n  <code>{escape(str(entry['rationale']), quote=False)}</code>"
    if entry.get("trade_message") and not executed:
        risk_txt = explain_risk(
            entry["trade_message"],
            code=str(entry.get("code") or entry.get("risk_code") or ""),
        )
        line += f"\n  <i>{escape(risk_txt, quote=False)}</i>"
    line += f"\n  <i>{ts}</i>"
    return line