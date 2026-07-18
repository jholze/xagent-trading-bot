"""Backward-compatible Grok JSON API — delegates to pluggable llm_client (C7)."""

from __future__ import annotations

from intelligence.llm_client import (
    GrokError,
    LlmError,
    ask_grok_json,
    ask_llm_json,
    clean_grok_json,
    clean_llm_json,
    parse_grok_json,
    parse_llm_json,
)

__all__ = [
    "GrokError",
    "LlmError",
    "ask_grok_json",
    "ask_llm_json",
    "clean_grok_json",
    "clean_llm_json",
    "parse_grok_json",
    "parse_llm_json",
]
