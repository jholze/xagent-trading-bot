from services.mcp_tokens import (
    actor_from_bearer,
    actor_from_extra,
    bootstrap_from_env,
    hash_token,
    tokens_match,
)


def test_hash_is_sha256_hex():
    h = hash_token("secret")
    assert len(h) == 64 and h != "secret"


def test_owner_env_token():
    actors = bootstrap_from_env(owner_token="owner-secret", extras=[])
    a = actor_from_bearer("owner-secret", actors)
    assert a is not None and a.role == "owner" and "*" in a.tenants
    assert actor_from_bearer("wrong", actors) is None


def test_operator_extra():
    extras = [{
        "token": "henry-secret",
        "actor_id": "henry-op",
        "role": "operator",
        "tenants": ["henry"],
        "caps": ["read", "trade", "lock"],
    }]
    actors = bootstrap_from_env(owner_token="owner-secret", extras=extras)
    a = actor_from_bearer("henry-secret", actors)
    assert a.tenants == ("henry",)
    assert actor_from_bearer("owner-secret", actors).role == "owner"


def test_tokens_match_is_constant_time_and_rejects_wrong():
    assert tokens_match("secret", "secret") is True
    assert tokens_match("secret", "nope") is False
    assert tokens_match("", "secret") is False
    assert tokens_match("secret", "") is False


def test_extra_owner_role_is_rejected():
    assert actor_from_extra({
        "token": "evil",
        "role": "owner",
        "tenants": ["*"],
        "caps": ["read", "trade", "lock", "kill"],
    }) is None
    actors = bootstrap_from_env(owner_token="owner-secret", extras=[{
        "token": "evil",
        "role": "owner",
        "tenants": ["*"],
        "caps": ["kill"],
    }])
    assert actor_from_bearer("evil", actors) is None


def test_operator_must_have_exactly_one_tenant_no_star():
    assert actor_from_extra({
        "token": "t",
        "role": "operator",
        "tenants": ["henry", "default"],
        "caps": ["read", "trade", "lock"],
    }) is None
    assert actor_from_extra({
        "token": "t",
        "role": "operator",
        "tenants": ["*"],
        "caps": ["read", "trade", "lock"],
    }) is None
    a = actor_from_extra({
        "token": "t",
        "actor_id": "henry-op",
        "role": "operator",
        "tenants": ["henry"],
        "caps": ["read", "trade", "lock", "kill"],
    })
    assert a is not None
    assert a.tenants == ("henry",)
    assert "kill" not in a.caps
    assert "trade" in a.caps


def test_observer_cannot_keep_trade_cap():
    a = actor_from_extra({
        "token": "t",
        "role": "observer",
        "tenants": ["henry"],
        "caps": ["read", "trade", "lock"],
    })
    assert a is not None
    assert a.caps == ("read",)


def test_invalid_role_skipped():
    assert actor_from_extra({"token": "t", "role": "admin", "tenants": ["henry"]}) is None
