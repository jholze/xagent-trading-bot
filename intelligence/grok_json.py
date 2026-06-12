"""Shared Grok JSON client with retries and explicit error handling."""

from __future__ import annotations

import json
import os
import re
import time

from dotenv import load_dotenv
from openai import OpenAI

from logger import log

load_dotenv()

MODEL = os.getenv("GROK_PARSE_MODEL", "grok-4")

_client: OpenAI | None = None


class GrokError(Exception):
    """Raised when Grok API or JSON parsing fails."""


def clean_grok_json(response: str) -> str:
    cleaned = (response or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def parse_grok_json(response: str, required_keys: list[str] | None = None) -> dict:
    if not response or response.startswith("API-Fehler"):
        raise GrokError(response or "Empty Grok response")
    try:
        data = json.loads(clean_grok_json(response))
    except json.JSONDecodeError as e:
        raise GrokError(f"Invalid JSON from Grok: {e}") from e
    if not isinstance(data, dict):
        raise GrokError(f"Expected JSON object, got {type(data).__name__}")
    if required_keys:
        missing = [k for k in required_keys if k not in data]
        if missing:
            raise GrokError(f"Missing required keys: {missing}")
    return data


def _get_client(timeout_sec: int) -> OpenAI:
    global _client
    api_key = os.getenv("XAI_API_KEY")
    if not api_key:
        raise GrokError("XAI_API_KEY not set")
    if _client is None:
        _client = OpenAI(api_key=api_key, base_url="https://api.x.ai/v1", timeout=timeout_sec)
    return _client


def ask_grok_json(
    prompt: str,
    *,
    model: str | None = None,
    retries: int = 2,
    timeout_sec: int = 30,
    required_keys: list[str] | None = None,
) -> dict:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            client = _get_client(timeout_sec)
            response = client.chat.completions.create(
                model=model or MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
            )
            content = response.choices[0].message.content or ""
            return parse_grok_json(content, required_keys=required_keys)
        except Exception as e:
            last_error = e
            if attempt < retries:
                wait = 2 ** attempt
                log(f"Grok retry {attempt + 1}/{retries} after {wait}s: {e}", "WARNING")
                time.sleep(wait)
            else:
                log(f"Grok failed after {retries + 1} attempts: {e}", "WARNING")
    raise GrokError(str(last_error)) from last_error