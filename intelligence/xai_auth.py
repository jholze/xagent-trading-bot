"""Where the bot's xAI calls go: metered ``XAI_API_KEY`` or the SuperGrok sidecar (#397 Phase 2).

One switch, default **off**::

    XAI_USE_SUBSCRIPTION=1          # route xAI calls through the auth sidecar
    XAI_AUTH_SIDECAR_URL=http://xagent-xai-auth.railway.internal:8080
    XAI_AUTH_TRIGGER_TOKEN=...      # shared secret, same value as on the sidecar

With the flag on *and* both variables set, ``resolve_xai_endpoint()`` returns
``{SIDECAR_URL}/v1`` + the trigger token as ``api_key``. The sidecar
(``services/xai_auth_sidecar``) checks that bearer, injects the kept-alive
OAuth access token and relays the call to ``https://api.x.ai/v1``. The access
token never reaches this process. A subscription bearer on that host is not on
the developer-API model catalog, so both chat completions and responses come
back HTTP 404 ``not-found`` with a string ``error`` field.

Flag off (unset / ``0`` / ``false``), or URL / token missing → today's
behaviour exactly: ``https://api.x.ai/v1`` + ``XAI_API_KEY``. Unsetting the
flag is the whole rollback.

Fallback: when a call *via the sidecar* fails with 401 / 403 / 404 / 503 or a
connection error (sidecar down, no credential on the volume, refresh failed,
or the sidecar returns 404), the caller logs
``xai subscription fallback to XAI_API_KEY`` plus the JSON body's ``error``
string (never the key) and repeats the call **once** against the metered
endpoint. No key there → fails as today. Timeouts are deliberately not a
fallback trigger (a slow xAI answer through the proxy would only be waited
for twice). Other relayed statuses (400, 429, 500, …) are not either.

Every module that talks to xAI (``intelligence/llm_client.py``,
``grok_x_search.py`` → ``x_data_provider.py``, WQE ``ai_critic`` via
``ask_grok_json``) resolves its endpoint here so there is exactly one copy of
this logic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from logger import log

SUBSCRIPTION_FLAG_VAR = "XAI_USE_SUBSCRIPTION"
SIDECAR_URL_VAR = "XAI_AUTH_SIDECAR_URL"
TRIGGER_TOKEN_VAR = "XAI_AUTH_TRIGGER_TOKEN"
API_KEY_VAR = "XAI_API_KEY"

XAI_DIRECT_BASE_URL = "https://api.x.ai/v1"

# Bare ``grok-4`` is not on the current xAI models catalog and is not in the
# May 15 2026 redirect list (those slugs alias to grok-4.3). A missing model id
# is HTTP 404 ``not-found`` from ``https://api.x.ai/v1``. grok-4.3 is the
# long-context successor and still accepts chat completions.
DEFAULT_XAI_MODEL = "grok-4.3"

# These ids 404 on api.x.ai (no redirect). The May 15 2026 slugs that still
# resolve (grok-3, grok-4-0709, grok-4-fast-*, grok-code-fast-1) are not listed.
RETIRED_XAI_MODEL_IDS = frozenset(
    {
        "grok-4",
        "grok-4-latest",
        "grok-2",
        "grok-2-latest",
        "grok-2-1212",
        "grok-2-vision-1212",
        "grok-beta",
        "grok-vision-beta",
        "grok-3-mini",
        "grok-3-mini-fast",
    }
)

FALLBACK_LOG_MARKER = "xai subscription fallback to XAI_API_KEY"
_FALLBACK_STATUSES = frozenset({401, 403, 404, 503})
_SECRET_VARS = (API_KEY_VAR, TRIGGER_TOKEN_VAR, "LLM_API_KEY", "OPENAI_API_KEY")
_TRUE = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class XaiEndpoint:
    """Where to send an OpenAI-compatible xAI call and with which bearer."""

    base_url: str
    api_key: str
    via_subscription: bool = False

    @property
    def label(self) -> str:
        return "sidecar" if self.via_subscription else "api.x.ai"


def flag_on(value: str | None, default: bool = False) -> bool:
    if value is None or not str(value).strip():
        return default
    return str(value).strip().lower() in _TRUE


def subscription_enabled() -> bool:
    """``XAI_USE_SUBSCRIPTION`` truthy. Default off."""
    return flag_on(os.environ.get(SUBSCRIPTION_FLAG_VAR), False)


def sidecar_base_url() -> str:
    """``{XAI_AUTH_SIDECAR_URL}/v1`` — trailing slash and an already-present ``/v1`` are tolerated."""
    raw = (os.environ.get(SIDECAR_URL_VAR) or "").strip().rstrip("/")
    if not raw:
        return ""
    if raw.endswith("/v1"):
        return raw
    return f"{raw}/v1"


def subscription_endpoint() -> XaiEndpoint | None:
    """Sidecar endpoint when the flag is on and URL + trigger token are set; else ``None``."""
    if not subscription_enabled():
        return None
    base = sidecar_base_url()
    token = (os.environ.get(TRIGGER_TOKEN_VAR) or "").strip()
    if not base or not token:
        log(
            f"{SUBSCRIPTION_FLAG_VAR} is on but {SIDECAR_URL_VAR} / {TRIGGER_TOKEN_VAR} "
            f"incomplete — using {API_KEY_VAR} + {XAI_DIRECT_BASE_URL}",
            "WARNING",
        )
        return None
    return XaiEndpoint(base_url=base, api_key=token, via_subscription=True)


def direct_endpoint() -> XaiEndpoint:
    """Metered ``XAI_API_KEY`` against ``https://api.x.ai/v1`` (``api_key`` may be empty)."""
    key = (os.environ.get(API_KEY_VAR) or "").strip()
    return XaiEndpoint(base_url=XAI_DIRECT_BASE_URL, api_key=key, via_subscription=False)


def resolve_xai_endpoint() -> XaiEndpoint:
    """Sidecar if configured (flag on + URL + token), otherwise the metered key."""
    return subscription_endpoint() or direct_endpoint()


def redact_secrets(text: str) -> str:
    """Replace known credential env values with ``[redacted]``. Empty stays empty."""
    if not text:
        return ""
    out = text
    for var in _SECRET_VARS:
        secret = (os.environ.get(var) or "").strip()
        if len(secret) >= 8 and secret in out:
            out = out.replace(secret, "[redacted]")
    return out


def xai_error_string(exc: BaseException) -> str:
    """The xAI JSON body's ``error`` string, redacted. Never the API key.

    xAI and the sidecar both send ``{"error": "<string>"}`` (model not-found,
    ``not found``, ``no_credential``). The OpenAI SDK only appends
    ``error.message`` to ``str(exc)``, so a string ``error`` otherwise
    disappears and the log is just ``Error code: 404``.
    """
    body = getattr(exc, "body", None)
    text = ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, str):
            text = err.strip()
        elif isinstance(err, dict):
            message = err.get("message")
            if isinstance(message, str):
                text = message.strip()
    return redact_secrets(text)[:500]


def is_subscription_fallback_error(exc: BaseException) -> bool:
    """True for sidecar 401/403/404/503 or a connection error (not a timeout)."""
    try:
        from openai import APIConnectionError, APIStatusError, APITimeoutError
    except Exception:  # pragma: no cover - openai is a hard dependency
        return False
    if isinstance(exc, APITimeoutError):
        return False
    if isinstance(exc, APIConnectionError):
        return True
    if isinstance(exc, APIStatusError):
        return int(getattr(exc, "status_code", 0) or 0) in _FALLBACK_STATUSES
    return False


def fallback_endpoint_after(exc: BaseException, endpoint: XaiEndpoint, *, context: str = "") -> XaiEndpoint | None:
    """Decide whether a failed call may be repeated once on the metered endpoint.

    Returns the direct endpoint (and logs the switch) when ``endpoint`` went via
    the sidecar and ``exc`` is a fallback-class error; ``None`` otherwise. The
    direct endpoint may carry an empty ``api_key`` — the caller fails as today.
    """
    if not endpoint.via_subscription or not is_subscription_fallback_error(exc):
        return None
    direct = direct_endpoint()
    status = getattr(exc, "status_code", None)
    reason = f"HTTP {status}" if status else type(exc).__name__
    where = f" [{context}]" if context else ""
    detail = xai_error_string(exc)
    extra = f": {detail}" if detail else ""
    tail = "" if direct.api_key else f" — {API_KEY_VAR} not set, call will fail"
    log(redact_secrets(f"{FALLBACK_LOG_MARKER}{where}: sidecar {reason}{extra}{tail}"), "WARNING")
    return direct
