import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from data_manager import atomic_write_json
from logger import log

MEMORY_DIR = Path(__file__).resolve().parent

DEFAULT_PARAMS = {
    "rsi_buy_low": 28,
    "rsi_buy_high": 48,
    "volume_multiplier": 1.3,
    "rsi_sell_30": 70,
    "rsi_sell_20": 85,
    "stop_loss_pct": 12.0,
    "buy_regime": "both",
    "reversal_rsi_cross_low": 32,
    "reversal_rsi_cross_high": 38,
    "reversal_volume_multiplier": 1.3,
    "cmc_trust_score": 65.0,
    "cmc_min_confidence": 55.0,
}


def _demo_mode() -> bool:
    return os.environ.get("DEMO_MODE", "0") == "1"


def _suffix() -> str:
    return ".demo" if _demo_mode() else ""


def _path(name: str) -> Path:
    return MEMORY_DIR / f"{name}{_suffix()}.json"


def _load(path: Path, default: dict) -> dict:
    if not path.exists():
        return default.copy()
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"Hermes memory read failed ({path}): {e}", "WARNING")
        return default.copy()


def _save(path: Path, data: dict):
    atomic_write_json(str(path), data)


def profile_key(symbol: str, timeframe: str) -> str:
    return f"{symbol}|{timeframe}"


def _migrate_baseline_v1(data: dict) -> dict:
    if data.get("version", 1) >= 2:
        data.setdefault("profiles", {})
        data.setdefault("rotation_index", 0)
        return data
    if not data.get("params") and not data.get("symbol"):
        return {
            "version": 2,
            "profiles": {},
            "rotation_index": 0,
            "updated_at": datetime.now().isoformat(),
        }
    symbol = data.get("symbol", "ARIA/USDT")
    timeframe = data.get("timeframe", "4h")
    key = profile_key(symbol, timeframe)
    profiles = {
        key: {
            "symbol": symbol,
            "timeframe": timeframe,
            "params": data.get("params") or {},
            "metrics": data.get("metrics") or {},
            "updated_at": data.get("updated_at"),
        }
    }
    return {
        "version": 2,
        "profiles": profiles,
        "rotation_index": 0,
        "active_key": key,
        "updated_at": datetime.now().isoformat(),
    }


def load_baseline_store() -> dict:
    default = {"version": 2, "profiles": {}, "rotation_index": 0}
    data = _load(_path("baseline"), default)
    return _migrate_baseline_v1(data)


def save_baseline_store(data: dict):
    data["updated_at"] = datetime.now().isoformat()
    _save(_path("baseline"), data)


def load_profile(symbol: str, timeframe: str) -> dict:
    store = load_baseline_store()
    key = profile_key(symbol, timeframe)
    profile = store.get("profiles", {}).get(key)
    if profile:
        return profile
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "params": {},
        "metrics": {},
    }


def save_profile(symbol: str, timeframe: str, profile: dict):
    store = load_baseline_store()
    key = profile_key(symbol, timeframe)
    profile["symbol"] = symbol
    profile["timeframe"] = timeframe
    profile["updated_at"] = datetime.now().isoformat()
    store.setdefault("profiles", {})[key] = profile
    store["active_key"] = key
    save_baseline_store(store)


def load_baseline() -> dict:
    """Backward-compatible: return active or first profile as flat baseline dict."""
    store = load_baseline_store()
    profiles = store.get("profiles", {})
    if not profiles:
        return {
            "version": 2,
            "symbol": "ARIA/USDT",
            "timeframe": "4h",
            "params": {},
            "metrics": {},
        }
    key = store.get("active_key") or next(iter(profiles))
    profile = profiles[key]
    return {
        "version": 2,
        "symbol": profile.get("symbol"),
        "timeframe": profile.get("timeframe", "4h"),
        "params": profile.get("params", {}),
        "metrics": profile.get("metrics", {}),
        "updated_at": profile.get("updated_at"),
        "profile_key": key,
    }


def save_baseline(data: dict):
    symbol = data.get("symbol")
    timeframe = data.get("timeframe", "4h")
    if not symbol:
        return
    save_profile(symbol, timeframe, {
        "params": data.get("params", {}),
        "metrics": data.get("metrics", {}),
    })


def list_profiles() -> list[dict]:
    store = load_baseline_store()
    return list(store.get("profiles", {}).values())


def set_rotation_index(index: int):
    store = load_baseline_store()
    store["rotation_index"] = index
    save_baseline_store(store)


def get_rotation_index() -> int:
    return int(load_baseline_store().get("rotation_index", 0))


def load_experiments() -> dict:
    return _load(_path("experiments"), {"experiments": []})


def save_experiments(data: dict):
    _save(_path("experiments"), data)


def append_experiment(record: dict) -> dict:
    data = load_experiments()
    record.setdefault("id", f"exp_{uuid.uuid4().hex[:8]}")
    record.setdefault("created_at", datetime.now().isoformat())
    data.setdefault("experiments", []).append(record)
    save_experiments(data)
    return record


def load_skills() -> dict:
    return _load(_path("skills"), {"skills": []})


def save_skills(data: dict):
    _save(_path("skills"), data)


def _delta_direction(old_value, new_value) -> str:
    try:
        old_f, new_f = float(old_value), float(new_value)
    except (TypeError, ValueError):
        return "same"
    if new_f > old_f:
        return "up"
    if new_f < old_f:
        return "down"
    return "same"


def _skill_key(skill: dict) -> tuple:
    applies = skill.get("applies_to") or {}
    return (
        applies.get("symbol", ""),
        applies.get("timeframe", ""),
        skill.get("variable", ""),
        skill.get("regime", ""),
        skill.get("delta_direction", "same"),
        bool(skill.get("promoted")),
    )


def upsert_skill(skill: dict, old_value=None, new_value=None) -> dict:
    if old_value is not None and new_value is not None:
        skill["delta_direction"] = _delta_direction(old_value, new_value)

    data = load_skills()
    skills = data.setdefault("skills", [])
    key = _skill_key(skill)
    alpha = 0.3

    for existing in skills:
        if _skill_key(existing) != key:
            continue
        existing["evidence_count"] = int(existing.get("evidence_count", 1)) + 1
        existing["last_seen"] = datetime.now().isoformat()
        new_conf = float(skill.get("confidence", existing.get("confidence", 0.5)))
        old_conf = float(existing.get("confidence", new_conf))
        existing["confidence"] = round(old_conf * (1 - alpha) + new_conf * alpha, 2)
        if new_conf > old_conf and skill.get("pattern"):
            existing["pattern"] = skill["pattern"]
        save_skills(data)
        prune_skills()
        return existing

    skill.setdefault("id", f"skill_{uuid.uuid4().hex[:8]}")
    skill.setdefault("created_at", datetime.now().isoformat())
    skill.setdefault("last_seen", datetime.now().isoformat())
    skill.setdefault("evidence_count", 1)
    skills.append(skill)
    save_skills(data)
    prune_skills()
    return skill


def append_skill(skill: dict) -> dict:
    return upsert_skill(skill)


def prune_skills(max_per_variable: int = 5, min_confidence: float = 0.25) -> int:
    from core.config import get_bot_config

    cfg = get_bot_config().hermes_config.get("skills", {})
    max_per_variable = int(cfg.get("max_per_variable", max_per_variable))
    min_confidence = float(cfg.get("min_confidence", min_confidence))

    data = load_skills()
    skills = data.get("skills", [])
    if not skills:
        return 0

    kept = [s for s in skills if float(s.get("confidence", 0)) >= min_confidence]
    by_var: dict[str, list] = {}
    for s in kept:
        var = s.get("variable", "_")
        by_var.setdefault(var, []).append(s)

    pruned = []
    for var_skills in by_var.values():
        var_skills.sort(
            key=lambda s: (float(s.get("confidence", 0)), int(s.get("evidence_count", 0))),
            reverse=True,
        )
        pruned.extend(var_skills[:max_per_variable])

    pruned.sort(key=lambda s: s.get("last_seen", s.get("created_at", "")))
    removed = len(skills) - len(pruned)
    if removed > 0:
        data["skills"] = pruned
        save_skills(data)
    return removed


def recent_experiments(limit: int = 5) -> list:
    exps = load_experiments().get("experiments", [])
    return exps[-limit:]


def relevant_skills(symbol: str, timeframe: str, min_confidence: float = 0.0, limit: int = 10) -> list:
    skills = load_skills().get("skills", [])
    matched = []
    ranked = sorted(
        skills,
        key=lambda s: (float(s.get("confidence", 0)), int(s.get("evidence_count", 1))),
        reverse=True,
    )
    for skill in ranked:
        applies = skill.get("applies_to") or {}
        if applies.get("symbol") and applies["symbol"] != symbol:
            continue
        if applies.get("timeframe") and applies["timeframe"] != timeframe:
            continue
        if float(skill.get("confidence", 0)) < min_confidence:
            continue
        matched.append(skill)
        if len(matched) >= limit:
            break
    return matched


def refuted_variables(symbol: str, timeframe: str, limit: int = 20) -> set[str]:
    refuted = set()
    for exp in reversed(load_experiments().get("experiments", [])):
        if exp.get("symbol") != symbol or exp.get("timeframe") != timeframe:
            continue
        if exp.get("verdict") == "rejected" and exp.get("variable"):
            refuted.add(exp["variable"])
        if len(refuted) >= limit:
            break
    return refuted


def _default_params_for_symbol(config, symbol: str, timeframe: str) -> dict:
    params = config.strategy_params(symbol, timeframe) or {}
    merged = dict(DEFAULT_PARAMS)
    merged.update({
        "rsi_buy_low": params.get("rsi_buy_low", merged["rsi_buy_low"]),
        "rsi_buy_high": params.get("rsi_buy_high", merged["rsi_buy_high"]),
        "volume_multiplier": params.get("volume_multiplier", merged["volume_multiplier"]),
        "rsi_sell_30": params.get("rsi_sell_30", merged["rsi_sell_30"]),
        "rsi_sell_20": params.get("rsi_sell_20", merged["rsi_sell_20"]),
        "stop_loss_pct": params.get("stop_loss_pct", config.stop_loss_pct),
        "buy_regime": params.get("buy_regime", merged["buy_regime"]),
    })
    if config.raw.get("live", {}).get("dry_run_enhanced"):
        merged["cmc_min_confidence"] = float(
            config.raw.get("dry_run_defaults", {}).get("cmc_min_confidence", 55)
        )
    return merged


def init_baseline_from_config(config, symbol: str = None, timeframe: str = None) -> dict:
    hermes_cfg = config.hermes_config
    symbols = hermes_cfg.get("symbols", ["ARIA/USDT"])
    timeframes = hermes_cfg.get("timeframes", ["4h"])
    symbol = symbol or symbols[0]
    timeframe = timeframe or timeframes[0]

    profile = load_profile(symbol, timeframe)
    if profile.get("params"):
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "params": profile["params"],
            "metrics": profile.get("metrics", {}),
            "updated_at": profile.get("updated_at"),
        }

    params = _default_params_for_symbol(config, symbol, timeframe)
    baseline = {
        "symbol": symbol,
        "timeframe": timeframe,
        "params": params,
        "metrics": {},
    }
    save_baseline(baseline)
    log(f"Hermes baseline initialized for {symbol} {timeframe}", "INFO")
    return baseline