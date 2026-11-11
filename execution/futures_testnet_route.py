"""Default-off Gate futures-testnet route guard (T220-01, non-issued).

The guard is not wired into ``GateExecutionAdapter``. Package A still owns
``execution/gate_adapter.py`` and ``execution/recovery.py`` (#772, #800, #801
open). Missing ``shorts.testnet_futures.strict_route`` is off. Off denies this
path and does not fall back to mainnet or to the legacy testnet host.

Pinned origin, checked against Gate API v4.106.180 and ccxt 4.5.88
``urls['test']['private']['futures']``:

    https://api-testnet.gateapi.io/api/v4

``fx-api-testnet.gateio.ws`` is the legacy futures testnet name from the v1
changelog. It is not an automatic failover.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any
from urllib.parse import urlsplit

PINNED_HOST = "api-testnet.gateapi.io"
PINNED_ORIGIN = "https://api-testnet.gateapi.io"
PINNED_PREFIX = "/api/v4/futures/usdt"
PINNED_BASE = "https://api-testnet.gateapi.io/api/v4"
LEGACY_TESTNET_HOST = "fx-api-testnet.gateio.ws"
LIVE_HOSTS = ("api.gateio.ws", "fx-api.gateio.ws")

_PATH_SEGMENT = re.compile(r"[A-Za-z0-9._~-]+")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ACCEPTED_MARGIN = frozenset({"isolated"})
_ACCEPTED_POSITION = frozenset({"one_way", "one-way", "single"})
_DENIED_POSITION = frozenset({"dual", "hedge", "dual_plus", "double", "both"})
_REDACT_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "secret",
        "passphrase",
        "password",
        "sign",
        "signature",
        "authorization",
        "key",
    }
)


@dataclass(frozen=True)
class RouteDecision:
    allowed: bool
    reason: str
    signing_url: str | None = None
    credential_env: str | None = None
    transport_calls: int = 0
    followed_redirect: bool = False
    inhibits_existing_adapter: bool = False


@dataclass(frozen=True)
class ModeProof:
    allowed: bool
    reason: str
    margin_mode: str = ""
    position_mode: str = ""
    set_invoked: bool = False


@dataclass(frozen=True)
class Conversion:
    accepted: bool
    reason: str
    contracts: Decimal | None = None


def strict_route_enabled(raw: Any) -> bool:
    """Literal True only. Missing, false, 1, and \"true\" stay off."""
    block = _testnet_block(raw)
    if not isinstance(block, dict):
        return False
    return block.get("strict_route") is True


def hosts_after_failure(_failed_host: str) -> tuple[str, ...]:
    """No automatic failover to legacy testnet or to mainnet."""
    return ()


def check_futures_url(url: str) -> RouteDecision:
    """Accept only the pinned https origin and a normalized futures-USDT path."""
    if not isinstance(url, str) or not url.strip():
        return RouteDecision(False, "url_missing")
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
        port = parts.port
    except ValueError:
        return RouteDecision(False, "port_rejected")
    if parts.scheme != "https":
        return RouteDecision(False, "scheme_rejected")
    if parts.username or parts.password:
        return RouteDecision(False, "userinfo_rejected")
    if not isinstance(host, str) or host != PINNED_HOST:
        if isinstance(host, str) and (
            host == LEGACY_TESTNET_HOST or host.endswith("." + LEGACY_TESTNET_HOST)
        ):
            return RouteDecision(False, "legacy_host_rejected")
        if isinstance(host, str) and (host in LIVE_HOSTS or host.endswith(".gateio.ws")):
            return RouteDecision(False, "mainnet_rejected")
        return RouteDecision(False, "host_rejected")
    if port not in (None, 443):
        return RouteDecision(False, "port_rejected")
    if parts.fragment:
        return RouteDecision(False, "fragment_rejected")
    query = parts.query or ""
    if "://" in query or "@" in query or "\\" in query:
        return RouteDecision(False, "query_rejected")
    path = parts.path or ""
    lowered = path.lower()
    if ".." in path or "\\" in path or "//" in path:
        return RouteDecision(False, "path_escape")
    if "%2e" in lowered or "%2f" in lowered or "%5c" in lowered:
        return RouteDecision(False, "path_escape")
    normalized = posixpath.normpath(path)
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    segments = normalized.split("/")
    if segments[:5] != ["", "api", "v4", "futures", "usdt"]:
        return RouteDecision(False, "path_not_futures_usdt")
    for segment in segments[5:]:
        if not segment or _PATH_SEGMENT.fullmatch(segment) is None:
            return RouteDecision(False, "path_escape")
    signing = PINNED_ORIGIN + normalized
    if query:
        signing = signing + "?" + query
    return RouteDecision(True, "ok", signing_url=signing)


def admit_tenant(raw: Any, tenant_id: str | None, mode: str | None) -> RouteDecision:
    """Testnet + literal flags + exactly one named tenant. Empty context denies."""
    if not strict_route_enabled(raw):
        return RouteDecision(False, "strict_route_off", inhibits_existing_adapter=False)
    if mode != "testnet":
        return RouteDecision(False, "mode_rejected")
    if tenant_id is None or not str(tenant_id).strip():
        return RouteDecision(False, "empty_tenant")
    block = _testnet_block(raw)
    if not isinstance(block, dict) or block.get("enabled") is not True:
        return RouteDecision(False, "testnet_futures_disabled")
    tenants = block.get("tenants")
    if not isinstance(tenants, list) or len(tenants) != 1:
        return RouteDecision(False, "not_one_named_tenant")
    named = tenants[0]
    if not isinstance(named, str) or not named.strip() or named != tenant_id:
        return RouteDecision(False, "tenant_mismatch")
    return RouteDecision(True, "ok")


def select_credentials(
    raw: Any,
    tenant_id: str | None,
    *,
    mode: str | None,
    live_env: str,
    testnet_env: str | None,
) -> RouteDecision:
    """Name a distinct testnet env, or deny. Never reads process secrets."""
    admission = admit_tenant(raw, tenant_id, mode)
    if not admission.allowed:
        return RouteDecision(False, admission.reason, credential_env=None)
    if not isinstance(testnet_env, str) or not testnet_env.strip():
        return RouteDecision(False, "testnet_credentials_unknown", credential_env=None)
    if testnet_env == live_env:
        return RouteDecision(False, "testnet_env_aliases_live", credential_env=None)
    return RouteDecision(True, "ok", credential_env=testnet_env)


def redact_mapping(payload: Any, extra_secrets: tuple[str, ...] = ()) -> dict[str, Any]:
    """Drop secret-bearing keys and replace known secret strings."""
    secrets = tuple(item for item in extra_secrets if isinstance(item, str) and item)
    if not isinstance(payload, dict):
        return {}
    return {str(key): _redact_value(value, secrets) for key, value in payload.items() if not _sensitive_key(key)}


def convert_base_qty(
    base_qty: Any,
    contract_size: Any,
    *,
    minimum: Any,
    step: Any,
    maximum: Any,
) -> Conversion:
    """Decimal contracts, rounded down, never above the exact size or the max."""
    base = _decimal(base_qty)
    size = _decimal(contract_size)
    floor = _decimal(minimum)
    quantum = _decimal(step)
    cap = _decimal(maximum)
    if base is None:
        return Conversion(False, "unknown_base")
    if size is None or size <= 0:
        return Conversion(False, "unknown_contract")
    if floor is None or floor <= 0:
        return Conversion(False, "unknown_minimum")
    if quantum is None or quantum <= 0:
        return Conversion(False, "unknown_step")
    if cap is None or cap <= 0:
        return Conversion(False, "unknown_maximum")
    if base <= 0:
        return Conversion(False, "non_positive")
    exact = base / size
    if not exact.is_finite() or exact <= 0:
        return Conversion(False, "non_positive")
    units = (exact / quantum).to_integral_value(rounding=ROUND_DOWN)
    stepped = units * quantum
    if stepped > exact or stepped > cap:
        return Conversion(False, "above_max")
    if stepped < floor:
        return Conversion(False, "below_min")
    return Conversion(True, "ok", contracts=stepped)


def prove_mode(
    set_ack: Any,
    readback: Any,
    *,
    observed_at: int,
    max_age_seconds: int,
) -> ModeProof:
    """Isolated one-way requires a set record and a matching fresh readback.

    A set response alone is not acceptance. Leverage 0 is cross. This function
    does not call an exchange.
    """
    if not isinstance(set_ack, dict):
        return ModeProof(False, "set_proof_missing")
    if not isinstance(readback, dict):
        return ModeProof(False, "readback_missing")
    set_view = _classify_mode(set_ack)
    read_view = _classify_mode(readback)
    if read_view.reason != "ok":
        return ModeProof(False, read_view.reason, read_view.margin_mode, read_view.position_mode)
    if set_view.reason != "ok":
        return ModeProof(False, set_view.reason, set_view.margin_mode, set_view.position_mode)
    if set_view.margin_mode != read_view.margin_mode or set_view.position_mode != read_view.position_mode:
        return ModeProof(False, "mode_mismatch", read_view.margin_mode, read_view.position_mode)
    for record in (set_ack, readback):
        fresh = _freshness(record, observed_at, max_age_seconds)
        if fresh is not None:
            return ModeProof(False, fresh, read_view.margin_mode, read_view.position_mode)
    return ModeProof(True, "ok", read_view.margin_mode, read_view.position_mode)


def collect_mode_proof(
    client: Any,
    *,
    set_ack: Any = None,
    allow_set: bool = False,
    observed_at: int,
    max_age_seconds: int,
) -> ModeProof:
    """Read a mode proof. Set is invoked only on an explicit synthetic client."""
    synthetic = getattr(client, "synthetic", False) is True
    invoked = False
    if allow_set and synthetic:
        setter = getattr(client, "set_isolated_one_way", None)
        if callable(setter):
            set_ack = setter()
            invoked = True
    reader = getattr(client, "read_mode", None)
    readback = reader() if callable(reader) else None
    proof = prove_mode(
        set_ack,
        readback,
        observed_at=observed_at,
        max_age_seconds=max_age_seconds,
    )
    return ModeProof(
        proof.allowed,
        proof.reason,
        proof.margin_mode,
        proof.position_mode,
        set_invoked=invoked,
    )


def authorize_route(
    raw: Any,
    *,
    tenant_id: str | None,
    mode: str | None,
    url: str,
    mode_proof: ModeProof | None,
    live_env: str,
    testnet_env: str | None,
    transport: Any = None,
    method: str = "POST",
) -> RouteDecision:
    """Check origin, tenant, and mode proof before any transport call."""
    admission = admit_tenant(raw, tenant_id, mode)
    if not admission.allowed:
        return admission
    checked = check_futures_url(url)
    if not checked.allowed:
        return checked
    if mode_proof is None or not mode_proof.allowed:
        reason = "mode_proof_missing" if mode_proof is None else mode_proof.reason
        return RouteDecision(False, reason)
    creds = select_credentials(
        raw,
        tenant_id,
        mode=mode,
        live_env=live_env,
        testnet_env=testnet_env,
    )
    if not creds.allowed:
        return creds
    if transport is None:
        return RouteDecision(
            True,
            "ok",
            signing_url=checked.signing_url,
            credential_env=creds.credential_env,
            transport_calls=0,
        )
    if getattr(transport, "follows_redirects", None) is not False:
        return RouteDecision(False, "redirects_not_disabled", transport_calls=0)
    response = transport.request(method, checked.signing_url)
    calls = 1
    if not isinstance(response, dict):
        return RouteDecision(False, "response_unreadable", transport_calls=calls)
    status = response.get("status")
    if isinstance(status, bool) or not isinstance(status, int):
        return RouteDecision(False, "response_unreadable", transport_calls=calls)
    if status in _REDIRECT_STATUSES:
        return RouteDecision(
            False,
            "redirect_rejected",
            signing_url=None,
            credential_env=None,
            transport_calls=calls,
            followed_redirect=False,
        )
    if status < 200 or status > 299:
        return RouteDecision(False, "response_rejected", transport_calls=calls)
    return RouteDecision(
        True,
        "ok",
        signing_url=checked.signing_url,
        credential_env=creds.credential_env,
        transport_calls=calls,
        followed_redirect=False,
    )


def _testnet_block(raw: Any) -> Any:
    if not isinstance(raw, dict):
        return None
    shorts = raw.get("shorts")
    if not isinstance(shorts, dict):
        return None
    return shorts.get("testnet_futures")


def _sensitive_key(key: Any) -> bool:
    return str(key).strip().lower() in _REDACT_KEYS


def _redact_value(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return redact_mapping(value, secrets)
    if isinstance(value, list):
        return [_redact_value(item, secrets) for item in value]
    if isinstance(value, str):
        redacted = value
        for secret in secrets:
            if secret and secret in redacted:
                redacted = redacted.replace(secret, "[redacted]")
        return redacted
    return value


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    if not number.is_finite():
        return None
    return number


@dataclass(frozen=True)
class _ModeView:
    reason: str
    margin_mode: str = ""
    position_mode: str = ""


def _classify_mode(record: dict) -> _ModeView:
    leverage = record.get("leverage")
    leverage_zero = False
    if leverage is not None and str(leverage).strip() != "":
        parsed = _decimal(leverage)
        if parsed is None or parsed < 0:
            return _ModeView("unknown_leverage")
        leverage_zero = parsed == 0
    margin = _text(record.get("margin_mode", record.get("marginMode")))
    if leverage_zero:
        margin = "cross"
    position = _text(record.get("position_mode", record.get("positionMode")))
    dual = record.get("dual_mode", record.get("dualMode"))
    if dual is True or _text(dual) in _DENIED_POSITION or _text(dual) in {"true", "1"}:
        position = "dual"
    if position in _DENIED_POSITION:
        return _ModeView("position_mode_denied", margin, position)
    if margin == "cross":
        return _ModeView("margin_cross", "cross", position)
    if margin not in _ACCEPTED_MARGIN:
        return _ModeView("unknown_margin", margin, position)
    if position not in _ACCEPTED_POSITION:
        return _ModeView("unknown_position", margin, position)
    if leverage is None or str(leverage).strip() == "":
        return _ModeView("unknown_leverage", margin, position)
    return _ModeView("ok", "isolated", "one_way")


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower().replace(" ", "_")


def _freshness(record: dict, observed_at: int, max_age_seconds: int) -> str | None:
    as_of = record.get("as_of")
    if isinstance(as_of, bool) or as_of is None:
        return "stale_or_unknown"
    try:
        stamped = int(as_of)
        observed = int(observed_at)
        limit = int(max_age_seconds)
    except (TypeError, ValueError):
        return "stale_or_unknown"
    if limit < 0 or stamped > observed or observed - stamped > limit:
        return "stale_or_unknown"
    return None
