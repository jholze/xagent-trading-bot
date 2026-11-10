"""T220-01 Rev 2 route contract. Fake clients only. Non-issued local draft.

Each test maps one acceptance line in
``specs/short-testnet-routing-v220/04-requirements.md``. The guard is not
wired into the live adapter.
"""

from __future__ import annotations

import json

import pytest

from execution import futures_testnet_route as route

PINNED = "https://api-testnet.gateapi.io/api/v4/futures/usdt/orders"
NOW = 1_700_000_000
AGE = 30


def _cfg(*, strict: bool | None = True, enabled: bool = True, tenants: list | None = None) -> dict:
    block: dict = {"enabled": enabled, "tenants": ["tenant-a"] if tenants is None else tenants}
    if strict is not None:
        block["strict_route"] = strict
    return {
        "shorts": {
            "allow_live": False,
            "climax_fade": {"enabled": True},
            "testnet_futures": block,
        }
    }


def _proof(**overrides) -> dict:
    row = {
        "margin_mode": "isolated",
        "position_mode": "one_way",
        "leverage": "2",
        "as_of": NOW,
    }
    row.update(overrides)
    return row


def _ok_proof() -> route.ModeProof:
    return route.prove_mode(_proof(), _proof(), observed_at=NOW, max_age_seconds=AGE)


class _Transport:
    def __init__(self, status: int = 200, location: str = ""):
        self.calls: list[tuple[str, str]] = []
        self.status = status
        self.location = location

    def request(self, method: str, url: str) -> dict:
        self.calls.append((method, url))
        body = {"status": self.status}
        if self.location:
            body["location"] = self.location
        return body


class _ModeClient:
    def __init__(self, *, synthetic: bool, readback: dict, set_ack: dict | None = None):
        self.synthetic = synthetic
        self._readback = readback
        self._set_ack = set_ack or readback
        self.set_calls: list[str] = []

    def read_mode(self) -> dict:
        return self._readback

    def set_isolated_one_way(self) -> dict:
        self.set_calls.append("set")
        return self._set_ack


def test_ac_t01_pinned_origin_accepted_before_transport():
    transport = _Transport()
    decision = route.authorize_route(
        _cfg(),
        tenant_id="tenant-a",
        mode="testnet",
        url=PINNED,
        mode_proof=_ok_proof(),
        live_env="GATE_API_KEY",
        testnet_env="GATE_TESTNET_KEY",
        transport=transport,
    )
    assert decision.allowed is True
    assert decision.signing_url == PINNED
    assert decision.credential_env == "GATE_TESTNET_KEY"
    assert transport.calls == [("POST", PINNED)]
    assert decision.followed_redirect is False


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://api-testnet.gateapi.io/api/v4/futures/usdt/orders", "scheme_rejected"),
        ("https://api-testnet.gateapi.io:8443/api/v4/futures/usdt/orders", "port_rejected"),
        ("https://api-testnet.gateapi.io.evil.example/api/v4/futures/usdt/orders", "host_rejected"),
        ("https://user:secret@api-testnet.gateapi.io/api/v4/futures/usdt/orders", "userinfo_rejected"),
        ("https://api-testnet.gateapi.io/api/v4/futures/usdt/../../spot/orders", "path_escape"),
        ("https://api-testnet.gateapi.io/api/v4/futures/usdt/%2e%2e/spot/orders", "path_escape"),
        ("https://api-testnet.gateapi.io/api/v4/futures/usdt/%2Fspot", "path_escape"),
        ("https://api.gateio.ws/api/v4/futures/usdt/orders", "mainnet_rejected"),
        ("https://fx-api.gateio.ws/api/v4/futures/usdt/orders", "mainnet_rejected"),
        ("https://fx-api-testnet.gateio.ws/api/v4/futures/usdt/orders", "legacy_host_rejected"),
    ],
)
def test_ac_t01_rejects_bad_origin_without_transport(url, reason):
    transport = _Transport()
    decision = route.authorize_route(
        _cfg(),
        tenant_id="tenant-a",
        mode="testnet",
        url=url,
        mode_proof=_ok_proof(),
        live_env="GATE_API_KEY",
        testnet_env="GATE_TESTNET_KEY",
        transport=transport,
    )
    assert decision.allowed is False
    assert decision.reason == reason
    assert decision.signing_url is None
    assert transport.calls == []
    assert "secret" not in repr(decision)


def test_ac_t01_userinfo_secret_is_not_in_the_decision():
    decision = route.check_futures_url(
        "https://live-key:live-secret@api-testnet.gateapi.io/api/v4/futures/usdt/orders"
    )
    blob = repr(decision)
    assert decision.reason == "userinfo_rejected"
    assert "live-key" not in blob
    assert "live-secret" not in blob


def test_ac_t01_cross_origin_redirect_is_not_followed():
    transport = _Transport(
        status=302,
        location="https://api.gateio.ws/api/v4/futures/usdt/orders",
    )
    decision = route.authorize_route(
        _cfg(),
        tenant_id="tenant-a",
        mode="testnet",
        url=PINNED,
        mode_proof=_ok_proof(),
        live_env="GATE_API_KEY",
        testnet_env="GATE_TESTNET_KEY",
        transport=transport,
    )
    assert decision.allowed is False
    assert decision.reason == "redirect_rejected"
    assert decision.followed_redirect is False
    assert transport.calls == [("POST", PINNED)]
    assert route.hosts_after_failure(route.PINNED_HOST) == ()


def test_ac_t01_missing_flag_and_legacy_activation_do_not_enable():
    for raw in (
        {},
        {"shorts": {}},
        {"shorts": {"testnet_futures": {"enabled": True, "tenants": ["tenant-a"]}}},
        {"shorts": {"allow_live": True, "climax_fade": {"enabled": True}}},
        _cfg(strict=False),
        _cfg(strict=None),
    ):
        decision = route.admit_tenant(raw, "tenant-a", "testnet")
        assert decision.allowed is False
        assert decision.reason == "strict_route_off"
        assert decision.inhibits_existing_adapter is False
        assert route.strict_route_enabled(raw) is False


@pytest.mark.parametrize(
    ("tenant", "mode", "tenants", "enabled", "reason"),
    [
        (None, "testnet", ["tenant-a"], True, "empty_tenant"),
        ("", "testnet", ["tenant-a"], True, "empty_tenant"),
        ("   ", "testnet", ["tenant-a"], True, "empty_tenant"),
        ("tenant-a", "live", ["tenant-a"], True, "mode_rejected"),
        ("tenant-a", "real", ["tenant-a"], True, "mode_rejected"),
        ("tenant-a", "shadow", ["tenant-a"], True, "mode_rejected"),
        ("tenant-a", "testnet", [], True, "not_one_named_tenant"),
        ("tenant-a", "testnet", ["tenant-a", "tenant-b"], True, "not_one_named_tenant"),
        ("tenant-b", "testnet", ["tenant-a"], True, "tenant_mismatch"),
        ("tenant-a", "testnet", ["tenant-a"], False, "testnet_futures_disabled"),
    ],
)
def test_ac_t01_tenant_mode_matrix(tenant, mode, tenants, enabled, reason):
    decision = route.admit_tenant(
        _cfg(tenants=tenants, enabled=enabled),
        tenant,
        mode,
    )
    assert decision.allowed is False
    assert decision.reason == reason


def test_ac_t01_tenants_do_not_share_decisions():
    alpha = _cfg(tenants=["alpha"])
    beta = _cfg(tenants=["beta"])
    assert route.admit_tenant(alpha, "alpha", "testnet").allowed is True
    assert route.admit_tenant(beta, "alpha", "testnet").reason == "tenant_mismatch"
    assert route.admit_tenant(beta, "beta", "testnet").allowed is True
    assert route.admit_tenant(alpha, "beta", "testnet").reason == "tenant_mismatch"
    def _strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for key, item in value.items():
                yield from _strings(key)
                yield from _strings(item)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                yield from _strings(item)

    blob = "\n".join(_strings(vars(route)))
    assert "alpha" not in blob
    assert "beta" not in blob


def test_ac_t01_missing_testnet_credentials_do_not_use_live_env(monkeypatch):
    monkeypatch.setenv("GATE_API_KEY", "should-not-be-read")
    monkeypatch.setenv("GATE_API_SECRET", "should-not-be-read")
    decision = route.select_credentials(
        _cfg(),
        "tenant-a",
        mode="testnet",
        live_env="GATE_API_KEY",
        testnet_env=None,
    )
    assert decision.allowed is False
    assert decision.reason == "testnet_credentials_unknown"
    assert decision.credential_env is None
    assert "should-not-be-read" not in repr(decision)
    aliased = route.select_credentials(
        _cfg(),
        "tenant-a",
        mode="testnet",
        live_env="GATE_API_KEY",
        testnet_env="GATE_API_KEY",
    )
    assert aliased.reason == "testnet_env_aliases_live"
    assert aliased.credential_env is None


@pytest.mark.parametrize(
    "field",
    ["api_key", "apiKey", "secret", "passphrase", "sign", "signature", "authorization", "KEY", "SIGN"],
)
def test_ac_t01_redaction_matrix(field):
    payload = {field: "super-secret-value", "host": route.PINNED_HOST, "reason": "ok"}
    redacted = route.redact_mapping(payload, ("super-secret-value",))
    blob = json.dumps(redacted)
    assert field not in redacted
    assert "super-secret-value" not in blob
    assert redacted["host"] == route.PINNED_HOST


def test_ac_t01_contract_bounds_do_not_round_up():
    at_limit = route.convert_base_qty("10", "1", minimum="1", step="1", maximum="10")
    assert at_limit.accepted is True
    assert at_limit.contracts == route.Decimal("10")
    one_over = route.convert_base_qty("10.2", "1", minimum="1", step="1", maximum="10")
    assert one_over.accepted is True
    assert one_over.contracts == route.Decimal("10")
    assert one_over.contracts <= route.Decimal("10")
    coarse = route.convert_base_qty("5", "1", minimum="1", step="3", maximum="2")
    assert coarse.accepted is False
    assert coarse.reason == "above_max"
    assert coarse.contracts is None
    below = route.convert_base_qty("0.9", "1", minimum="1", step="1", maximum="10")
    assert below.reason == "below_min"
    assert below.contracts is None
    exact_min = route.convert_base_qty("1", "1", minimum="1", step="1", maximum="10")
    assert exact_min.accepted is True
    assert exact_min.contracts == route.Decimal("1")


@pytest.mark.parametrize(
    ("base", "size", "minimum", "step", "maximum", "reason"),
    [
        (None, "1", "1", "1", "10", "unknown_base"),
        ("1", None, "1", "1", "10", "unknown_contract"),
        ("1", "0", "1", "1", "10", "unknown_contract"),
        ("1", "1", None, "1", "10", "unknown_minimum"),
        ("1", "1", "1", None, "10", "unknown_step"),
        ("1", "1", "1", "1", None, "unknown_maximum"),
        ("nan", "1", "1", "1", "10", "unknown_base"),
        ("1", "Infinity", "1", "1", "10", "unknown_contract"),
    ],
)
def test_ac_t01_unknown_contract_inputs_deny(base, size, minimum, step, maximum, reason):
    result = route.convert_base_qty(base, size, minimum=minimum, step=step, maximum=maximum)
    assert result.accepted is False
    assert result.reason == reason
    assert result.contracts is None


def test_ac_t01_decimal_string_does_not_use_binary_float_spill():
    result = route.convert_base_qty("0.3", "0.1", minimum="1", step="1", maximum="3")
    assert result.accepted is True
    assert result.contracts == route.Decimal("3")
    spilled = route.convert_base_qty("0.3", "0.1", minimum="1", step="1", maximum="2")
    assert spilled.accepted is False
    assert spilled.reason == "above_max"
    assert spilled.contracts is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"margin_mode": "cross"},
        {"leverage": "0"},
        {"leverage": "0", "margin_mode": "isolated"},
        {"position_mode": "dual"},
        {"position_mode": "hedge"},
        {"position_mode": "dual_plus"},
        {"dual_mode": True},
        {"position_mode": ""},
        {"margin_mode": ""},
        {"leverage": None},
    ],
)
def test_ac_t01_bad_mode_proof_denies(overrides):
    readback = _proof(**overrides)
    proof = route.prove_mode(_proof(), readback, observed_at=NOW, max_age_seconds=AGE)
    assert proof.allowed is False
    assert proof.reason != "ok"


def test_ac_t01_set_response_without_readback_denies():
    proof = route.prove_mode(_proof(), None, observed_at=NOW, max_age_seconds=AGE)
    assert proof.reason == "readback_missing"
    assert proof.allowed is False


def test_ac_t01_readback_without_set_denies():
    proof = route.prove_mode(None, _proof(), observed_at=NOW, max_age_seconds=AGE)
    assert proof.reason == "set_proof_missing"


def test_ac_t01_mismatched_readback_denies_even_if_set_is_isolated():
    readback = _proof(margin_mode="cross", leverage="2")
    proof = route.prove_mode(_proof(), readback, observed_at=NOW, max_age_seconds=AGE)
    assert proof.allowed is False
    assert proof.reason == "margin_cross"


def test_ac_t01_stale_and_missing_timestamps_deny():
    stale = _proof(as_of=NOW - AGE - 1)
    assert route.prove_mode(_proof(), stale, observed_at=NOW, max_age_seconds=AGE).reason == "stale_or_unknown"
    missing = _proof()
    missing.pop("as_of")
    assert route.prove_mode(_proof(), missing, observed_at=NOW, max_age_seconds=AGE).reason == "stale_or_unknown"
    fresh = route.prove_mode(_proof(), _proof(), observed_at=NOW, max_age_seconds=AGE)
    assert fresh.allowed is True
    assert fresh.margin_mode == "isolated"
    assert fresh.position_mode == "one_way"


def test_ac_t01_real_client_is_not_switched():
    client = _ModeClient(synthetic=False, readback=_proof())
    proof = route.collect_mode_proof(
        client,
        set_ack=None,
        allow_set=True,
        observed_at=NOW,
        max_age_seconds=AGE,
    )
    assert client.set_calls == []
    assert proof.set_invoked is False
    assert proof.reason == "set_proof_missing"


def test_ac_t01_synthetic_set_still_needs_matching_readback():
    client = _ModeClient(
        synthetic=True,
        readback=_proof(margin_mode="cross", leverage="2"),
        set_ack=_proof(),
    )
    proof = route.collect_mode_proof(
        client,
        allow_set=True,
        observed_at=NOW,
        max_age_seconds=AGE,
    )
    assert client.set_calls == ["set"]
    assert proof.allowed is False
    assert proof.reason == "margin_cross"
    assert proof.set_invoked is True


def test_ac_t01_synthetic_set_and_readback_allow():
    client = _ModeClient(synthetic=True, readback=_proof(), set_ack=_proof())
    proof = route.collect_mode_proof(
        client,
        allow_set=True,
        observed_at=NOW,
        max_age_seconds=AGE,
    )
    assert proof.allowed is True
    assert proof.set_invoked is True
    assert client.set_calls == ["set"]
