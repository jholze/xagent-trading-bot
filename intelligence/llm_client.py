"""Pluggable OpenAI-compatible LLM client (epic #72 C7).

Backends:
  LLM_BACKEND=xai (default) → https://api.x.ai/v1 + XAI_API_KEY + grok-4.3
      via the Responses API (POST /v1/responses). Chat completions 404 for the
      SuperGrok sidecar token; pi-ai and the sidecar probe both use responses.
      with XAI_USE_SUBSCRIPTION=1 (+ XAI_AUTH_SIDECAR_URL / XAI_AUTH_TRIGGER_TOKEN)
      → {sidecar}/v1 + trigger token, one-shot fallback to XAI_API_KEY on
      sidecar 401/403/503/connection error (#397 Phase 2, see intelligence/xai_auth.py)
  LLM_BACKEND=openai_compat → LLM_BASE_URL + LLM_API_KEY + LLM_MODEL
      via chat completions (local OpenAI-compatible servers).

ask_grok* remain as thin aliases for compatibility.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from openai import OpenAI

from intelligence.xai_auth import (
    DEFAULT_XAI_MODEL,
    fallback_endpoint_after,
    resolve_xai_endpoint,
    XaiEndpoint,
)
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
    via_subscription: bool = False

    @property
    def cache_key(self) -> str:
        return f"{self.backend}|{self.base_url}|{self.model}"

    @property
    def endpoint(self) -> XaiEndpoint:
        return XaiEndpoint(base_url=self.base_url, api_key=self.api_key, via_subscription=self.via_subscription)


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

    # xai default — sidecar when XAI_USE_SUBSCRIPTION is on and configured, else api.x.ai
    endpoint = resolve_xai_endpoint()
    key = endpoint.api_key
    if not endpoint.via_subscription:
        key = key or os.environ.get("LLM_API_KEY") or ""
    model = os.environ.get("GROK_PARSE_MODEL") or os.environ.get("LLM_MODEL") or DEFAULT_XAI_MODEL
    return LlmSettings(
        backend="xai",
        base_url=endpoint.base_url,
        api_key=key,
        model=model,
        via_subscription=endpoint.via_subscription,
    )


def _direct_xai_settings(settings: LlmSettings, endpoint: XaiEndpoint) -> LlmSettings:
    """Same backend/model as ``settings`` but pointed at the metered endpoint."""
    return LlmSettings(
        backend=settings.backend,
        base_url=endpoint.base_url,
        api_key=endpoint.api_key or os.environ.get("LLM_API_KEY") or "",
        model=settings.model,
        via_subscription=False,
    )


def _endpoint_host(base_url: str) -> str:
    """Hostname only. Never the path, query, or userinfo (those can carry secrets)."""
    return urlparse(base_url or "").hostname or "unknown"


def _llm_failure_line(prefix: str, settings: LlmSettings, model: str, exc: BaseException) -> str:
    return f"{prefix}: model={model} host={_endpoint_host(settings.base_url)}: {exc}"


def _response_text(response: Any) -> str:
    """Assistant text from a Responses API result. Chat-shaped objects are not accepted."""
    direct = getattr(response, "output_text", None)
    if isinstance(direct, str) and direct.strip():
        return direct
    parts: list[str] = []
    output = getattr(response, "output", None)
    if not isinstance(output, list):
        return ""
    for item in output:
        if getattr(item, "type", None) not in (None, "message"):
            continue
        content = getattr(item, "content", None)
        if not isinstance(content, list):
            continue
        for part in content:
            text = getattr(part, "text", None)
            if isinstance(text, str) and text:
                parts.append(text)
    return "".join(parts)


def _as_chat(text: str) -> Any:
    """Shape a Responses payload like a chat completion so existing callers stay unchanged."""
    message = type("Msg", (), {"content": text})()
    choice = type("Choice", (), {"message": message})()
    return type("Chat", (), {"choices": [choice]})()


def _invoke(settings: LlmSettings, *, model: str, messages: list[dict[str, str]], temperature: float, timeout_sec: int):
    """One completion. xAI uses Responses; OpenAI-compatible servers stay on chat completions."""
    client = _get_client(settings, timeout_sec)
    if settings.backend == "xai":
        response = client.responses.create(model=model, input=messages, temperature=temperature)
        return _as_chat(_response_text(response))
    return client.chat.completions.create(model=model, messages=messages, temperature=temperature)


def _chat_completion(
    settings: LlmSettings,
    *,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    timeout_sec: int,
):
    """One completion; via the sidecar it is repeated once on XAI_API_KEY when the sidecar fails."""
    try:
        return _invoke(settings, model=model, messages=messages, temperature=temperature, timeout_sec=timeout_sec)
    except Exception as e:
        direct = fallback_endpoint_after(e, settings.endpoint, context="llm_client")
        if direct is None:
            raise
        return _invoke(
            _direct_xai_settings(settings, direct),
            model=model,
            messages=messages,
            temperature=temperature,
            timeout_sec=timeout_sec,
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
    base_url: str | None = None,
    api_key: str | None = None,
) -> str:
    """Free-text completion. Returns error string on failure (legacy grok_agent style)."""
    try:
        if base_url:
            settings = LlmSettings(
                backend="openai_compat",
                base_url=str(base_url).strip().rstrip("/"),
                api_key=(api_key or "").strip(),
                model=model or "",
            )
        else:
            settings = llm_settings()
        response = _chat_completion(
            settings,
            model=model or settings.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            timeout_sec=timeout_sec,
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
        settings: LlmSettings | None = None
        try:
            settings = llm_settings()
            response = _chat_completion(
                settings,
                model=model or settings.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                timeout_sec=timeout_sec,
            )
            content = response.choices[0].message.content or ""
            return parse_llm_json(content, required_keys=required_keys)
        except Exception as e:
            last_error = e
            used_model = model or (settings.model if settings is not None else "unset")
            if attempt < retries:
                wait = 2**attempt
                prefix = f"LLM retry {attempt + 1}/{retries} after {wait}s"
            else:
                prefix = f"LLM failed after {retries + 1} attempts"
            if settings is None:
                log(f"{prefix}: model={used_model} host=unknown: {e}", "WARNING")
            else:
                log(_llm_failure_line(prefix, settings, used_model, e), "WARNING")
            if attempt < retries:
                time.sleep(wait)
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
