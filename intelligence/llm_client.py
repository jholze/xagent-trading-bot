"""Pluggable OpenAI-compatible LLM client (epic #72 C7).

Backends:
  LLM_BACKEND=xai (default) → https://api.x.ai/v1 + XAI_API_KEY + grok-4
  LLM_BACKEND=openai_compat → LLM_BASE_URL + LLM_API_KEY + LLM_MODEL

ask_grok* remain as thin aliases for compatibility.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

from logger import log

load_dotenv()

_clients: dict[str, OpenAI] = {}


class LlmError(Exception):
    """Raised when LLM API or JSON parsing fails."""


# Backward-compatible name used across Hermes/tests
GrokError = LlmError


@dataclass(frozen=True)
class LlmSettings:
    backend: str
    base_url: str
    api_key: str
    model: str

    @property
    def cache_key(self) -> str:
        return f"{self.backend}|{self.base_url}|{self.model}"


def llm_settings() -> LlmSettings:
    backend = (os.environ.get("LLM_BACKEND") or "xai").strip().lower()
    if backend in ("openai", "openai_compat", "compat", "local"):
        backend = "openai_compat"
    else:
        backend = "xai"

    if backend == "openai_compat":
        base = (os.environ.get("LLM_BASE_URL") or "").strip().rstrip("/")
        if not base:
            raise LlmError("LLM_BACKEND=openai_compat requires LLM_BASE_URL")
        key = (
            os.environ.get("LLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("XAI_API_KEY")
            or ""
        )
        model = (
            os.environ.get("LLM_MODEL")
            or os.environ.get("GROK_PARSE_MODEL")
            or "gpt-4o-mini"
        )
        return LlmSettings(backend=backend, base_url=base, api_key=key, model=model)

    # xai default
    key = os.environ.get("XAI_API_KEY") or os.environ.get("LLM_API_KEY") or ""
    model = os.environ.get("GROK_PARSE_MODEL") or os.environ.get("LLM_MODEL") or "grok-4"
    return LlmSettings(
        backend="xai",
        base_url="https://api.x.ai/v1",
        api_key=key,
        model=model,
    )


def _get_client(settings: LlmSettings, timeout_sec: int) -> OpenAI:
    if not settings.api_key and settings.backend == "xai":
        raise LlmError("XAI_API_KEY not set")
    if not settings.api_key and settings.backend == "openai_compat":
        # Some local servers accept empty key
        key = settings.api_key or "local"
    else:
        key = settings.api_key
    ck = f"{settings.cache_key}|{timeout_sec}|{key[:8]}"
    if ck not in _clients:
        _clients[ck] = OpenAI(
            api_key=key or "local",
            base_url=settings.base_url,
            timeout=timeout_sec,
        )
    return _clients[ck]


def reset_llm_clients() -> None:
    """Test helper: drop cached clients."""
    _clients.clear()


def clean_llm_json(response: str) -> str:
    cleaned = (response or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def parse_llm_json(response: str, required_keys: list[str] | None = None) -> dict:
    if not response or response.startswith("API-Fehler"):
        raise LlmError(response or "Empty LLM response")
    try:
        data = json.loads(clean_llm_json(response))
    except json.JSONDecodeError as e:
        raise LlmError(f"Invalid JSON from LLM: {e}") from e
    if not isinstance(data, dict):
        raise LlmError(f"Expected JSON object, got {type(data).__name__}")
    if required_keys:
        missing = [k for k in required_keys if k not in data]
        if missing:
            raise LlmError(f"Missing required keys: {missing}")
    return data


def ask_llm(
    prompt: str,
    *,
    temperature: float = 0.7,
    model: str | None = None,
    timeout_sec: int = 60,
) -> str:
    """Free-text completion. Returns error string on failure (legacy grok_agent style)."""
    try:
        settings = llm_settings()
        client = _get_client(settings, timeout_sec)
        response = client.chat.completions.create(
            model=model or settings.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        return f"API-Fehler: {e}"


def ask_llm_json(
    prompt: str,
    *,
    model: str | None = None,
    retries: int = 2,
    timeout_sec: int = 30,
    required_keys: list[str] | None = None,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            settings = llm_settings()
            client = _get_client(settings, timeout_sec)
            response = client.chat.completions.create(
                model=model or settings.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
            )
            content = response.choices[0].message.content or ""
            return parse_llm_json(content, required_keys=required_keys)
        except Exception as e:
            last_error = e
            if attempt < retries:
                wait = 2**attempt
                log(f"LLM retry {attempt + 1}/{retries} after {wait}s: {e}", "WARNING")
                time.sleep(wait)
            else:
                log(f"LLM failed after {retries + 1} attempts: {e}", "WARNING")
    raise LlmError(str(last_error)) from last_error


# --- aliases used by existing call sites ---
def ask_grok(prompt: str, temperature: float = 0.7, model: str | None = None) -> str:
    return ask_llm(prompt, temperature=temperature, model=model)


def ask_grok_json(
    prompt: str,
    *,
    model: str | None = None,
    retries: int = 2,
    timeout_sec: int = 30,
    required_keys: list[str] | None = None,
) -> dict[str, Any]:
    return ask_llm_json(
        prompt,
        model=model,
        retries=retries,
        timeout_sec=timeout_sec,
        required_keys=required_keys,
    )


def clean_grok_json(response: str) -> str:
    return clean_llm_json(response)


def parse_grok_json(response: str, required_keys: list[str] | None = None) -> dict:
    return parse_llm_json(response, required_keys=required_keys)
