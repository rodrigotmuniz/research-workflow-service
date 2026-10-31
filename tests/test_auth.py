import time
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient

from rws.auth import AuthError, DevIssuer, Principal, TokenVerifier, _parse_scopes
from tests.conftest import AuthHeaders


@pytest.fixture
def issuer() -> DevIssuer:
    return DevIssuer("https://issuer.test", "rws")


def test_valid_token_yields_principal(issuer: DevIssuer) -> None:
    principal = issuer.verifier().verify(issuer.mint("alice", ["flows:read"]))
    assert principal == Principal("alice", frozenset({"flows:read"}))
    assert principal.has_scopes({"flows:read"})
    assert not principal.has_scopes({"flows:read", "runs:start"})


def _forge(issuer: DevIssuer, **overrides: Any) -> str:
    now = int(time.time())
    claims = {"iss": issuer.issuer, "aud": issuer.audience, "sub": "a", "iat": now, "exp": now + 60}
    claims.update(overrides)
    return jwt.encode(claims, issuer._private_key, algorithm="RS256", headers={"kid": issuer.kid})


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": int(time.time()) - 10},  # expired
        {"aud": "someone-else"},  # token for another API
        {"iss": "https://evil.test"},  # untrusted issuer
    ],
)
def test_rejects_bad_claims(issuer: DevIssuer, overrides: dict[str, Any]) -> None:
    with pytest.raises(AuthError):
        issuer.verifier().verify(_forge(issuer, **overrides))


def test_rejects_token_signed_by_another_key(issuer: DevIssuer) -> None:
    other = DevIssuer(issuer.issuer, issuer.audience)
    with pytest.raises(AuthError, match="unknown signing key"):
        issuer.verifier().verify(other.mint("mallory", ["flows:read"]))


def test_rejects_unsigned_token(issuer: DevIssuer) -> None:
    token = jwt.encode({"sub": "x"}, key="", algorithm="none", headers={"kid": issuer.kid})
    with pytest.raises(AuthError):
        issuer.verifier().verify(token)


def test_rejects_garbage(issuer: DevIssuer) -> None:
    with pytest.raises(AuthError):
        issuer.verifier().verify("not-a-jwt")


def test_jwks_url_verifier(issuer: DevIssuer, monkeypatch: pytest.MonkeyPatch) -> None:
    """The production path: keys fetched from the provider's JWKS endpoint."""
    from rws import auth

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: issuer.jwks())
    verifier = TokenVerifier(
        issuer.issuer, issuer.audience, auth.jwks_url_resolver("https://x/jwks")
    )
    assert verifier.verify(issuer.mint("bob", [])).subject == "bob"


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({"scope": "a b"}, {"a", "b"}),
        ({"scp": ["a", "c"]}, {"a", "c"}),
        ({}, set()),
    ],
)
def test_scope_claim_formats(claims: dict[str, Any], expected: set[str]) -> None:
    assert _parse_scopes(claims) == expected


# --- through the API ------------------------------------------------------------------


def test_missing_token_is_401(client: TestClient) -> None:
    resp = client.get("/flows")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"].startswith("Bearer")


def test_invalid_token_is_401(client: TestClient) -> None:
    resp = client.get("/flows", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401
    assert 'error="invalid_token"' in resp.headers["WWW-Authenticate"]


def test_missing_scope_is_403(client: TestClient, auth: AuthHeaders) -> None:
    resp = client.post("/flows", json={}, headers=auth(["flows:read"]))
    assert resp.status_code == 403
    assert resp.json()["detail"] == "missing scope(s): flows:write"
    assert 'error="insufficient_scope"' in resp.headers["WWW-Authenticate"]


def test_dev_token_endpoint(client: TestClient) -> None:
    resp = client.post("/dev/token", json={"subject": "carol", "scopes": ["flows:read"]})
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    assert client.get("/flows", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.get("/dev/jwks.json").json()["keys"][0]["alg"] == "RS256"


def test_dev_token_rejects_unknown_scopes(client: TestClient) -> None:
    resp = client.post("/dev/token", json={"scopes": ["admin:all"]})
    assert resp.status_code == 400
