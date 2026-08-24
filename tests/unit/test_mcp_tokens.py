from services.mcp_tokens import hash_token, actor_from_bearer, bootstrap_from_env


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
