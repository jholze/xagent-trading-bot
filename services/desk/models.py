"""Desk HUD card shapes. Not snapshot assembly."""

from __future__ import annotations

from typing import TypedDict

STANCES = ("ARMED", "BLOCK", "SIZE↓", "IDLE", "MISS")

DEFAULT_CMC_MIN = 55.0
OVERSOLD_RSI = 40.0
RISK_OFF_REGIMES = frozenset({"RISK_OFF", "CRASH"})
MEMORY_BLOCK_FLAGS = frozenset({"do_not_buy", "hard_block", "block"})
LC_BUYISH = frozenset({"buy", "strong_buy", "accumulate", "buy_now"})


class TaCard(TypedDict):
    setup: str
    path: str
    blocker: str
    stance: str


class SocialCard(TypedDict):
    lead: str
    chorus: str
    ttl: str
    stance: str


class MemoryCard(TypedDict):
    bias: str
    flag: str | None
    lesson: str
    stance: str


class Hud(TypedDict):
    ta: TaCard
    social: SocialCard
    memory: MemoryCard
