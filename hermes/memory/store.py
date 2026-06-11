import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from data_manager import atomic_write_json
from logger import log

MEMORY_DIR = Path(__file__).resolve().parent


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


def load_baseline() -> dict:
    default = {
        "version": 1,
        "symbol": "ARIA/USDT",
        "timeframe": "4h",
        "params": {},
        "metrics": {},
        "updated_at": datetime.now().isoformat(),
    }
    return _load(_path("baseline"), default)


def save_baseline(data: dict):
    data["updated_at"] = datetime.now().isoformat()
    _save(_path("baseline"), data)


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


def append_skill(skill: dict) -> dict:
    data = load_skills()
    skill.setdefault("id", f"skill_{uuid.uuid4().hex[:8]}")
    skill.setdefault("created_at", datetime.now().isoformat())
    data.setdefault("skills", []).append(skill)
    save_skills(data)
    return skill


def recent_experiments(limit: int = 5) -> list:
    exps = load_experiments().get("experiments", [])
    return exps[-limit:]


def relevant_skills(
    symbol: str,
    timeframe: str,
    min_confidence: float = 0.0,
    limit: int = 10,
) -> list:
    skills = load_skills().get("skills", [])
    matched = []
    for skill in reversed(skills):
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
    return list(reversed(matched))


def refuted_variables(symbol: str, timeframe: str, limit: int = 20) -> set[str]:
    """Variables recently rejected — heuristic should avoid repeating."""
    refuted = set()
    for exp in reversed(load_experiments().get("experiments", [])):
        if exp.get("symbol") != symbol or exp.get("timeframe") != timeframe:
            continue
        if exp.get("verdict") == "rejected" and exp.get("variable"):
            refuted.add(exp["variable"])
        if len(refuted) >= limit:
            break
    return refuted


def init_baseline_from_config(config) -> dict:
    """Seed baseline.json from config.strategies if missing or empty."""
    baseline = load_baseline()
    if baseline.get("params"):
        return baseline

    hermes_cfg = config.hermes_config
    symbols = hermes_cfg.get("symbols", ["ARIA/USDT"])
    timeframes = hermes_cfg.get("timeframes", ["4h"])
    symbol = symbols[0]
    timeframe = timeframes[0]
    params = config.strategy_params(symbol, timeframe) or {}

    baseline.update({
        "symbol": symbol,
        "timeframe": timeframe,
        "params": {
            "rsi_buy_low": params.get("rsi_buy_low", 28),
            "rsi_buy_high": params.get("rsi_buy_high", 48),
            "volume_multiplier": params.get("volume_multiplier", 1.3),
            "rsi_sell_30": params.get("rsi_sell_30", 70),
            "rsi_sell_20": params.get("rsi_sell_20", 85),
            "stop_loss_pct": params.get("stop_loss_pct", config.stop_loss_pct),
        },
        "metrics": {},
    })
    save_baseline(baseline)
    log("Hermes baseline initialized from config", "INFO")
    return baseline