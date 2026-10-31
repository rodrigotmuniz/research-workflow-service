"""OAuth2 bearer token validation (OpenID Connect JWTs) and scope checks.

Production: tokens are issued by an external OIDC provider; we verify the RS256
signature with the provider's public keys (JWKS), plus issuer, audience and expiry.

Local dev: :class:`DevIssuer` generates a throw-away RSA key pair *in memory* at
startup and can mint tokens. The verification path is identical; only the key
source differs. No key material is ever written to disk or committed.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

SCOPES = {
    "flows:read": "Read your flows and runs",
    "flows:write": "Create flows",
    "runs:start": "Start and cancel runs",
}


class AuthError(Exception):
    """The token is missing, malformed, expired or not signed by a trusted key."""


@dataclass(frozen=True)
class Principal:
    """The authenticated caller: the token subject and the scopes it was granted."""

    subject: str
    scopes: frozenset[str]

    def has_scopes(self, required: set[str]) -> bool:
        return required <= self.scopes


KeyResolver = Callable[[str], Any]
"""Given a raw token, return the public key to verify it with (looked up by ``kid``)."""


def jwks_url_resolver(url: str) -> KeyResolver:
    client = jwt.PyJWKClient(url, cache_keys=True, lifespan=300)
    return lambda token: client.get_signing_key_from_jwt(token).key


def static_jwks_resolver(jwks: dict[str, Any]) -> KeyResolver:
    keyset = jwt.PyJWKSet.from_dict(jwks)

    def resolve(token: str) -> Any:
        kid = jwt.get_unverified_header(token).get("kid")
        for key in keyset.keys:
            if key.key_id == kid:
                return key.key
        raise AuthError("unknown signing key")

    return resolve


class TokenVerifier:
    def __init__(self, issuer: str, audience: str, key_resolver: KeyResolver) -> None:
        self.issuer = issuer
        self.audience = audience
        self._resolve_key = key_resolver

    def verify(self, token: str) -> Principal:
        try:
            claims = jwt.decode(
                token,
                key=self._resolve_key(token),
                algorithms=["RS256"],  # never accept "none" or HMAC with a public key
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "sub"]},
            )
        except (jwt.PyJWTError, AuthError) as exc:
            raise AuthError(str(exc)) from exc
        return Principal(subject=claims["sub"], scopes=_parse_scopes(claims))


def _parse_scopes(claims: dict[str, Any]) -> frozenset[str]:
    """Scopes come as a space-separated ``scope`` string (RFC 9068) or an ``scp`` list."""
    if isinstance(claims.get("scope"), str):
        return frozenset(claims["scope"].split())
    if isinstance(claims.get("scp"), list):
        return frozenset(str(s) for s in claims["scp"])
    return frozenset()


class DevIssuer:
    """A minimal local token issuer. Never enable it outside local development."""

    def __init__(self, issuer: str, audience: str) -> None:
        self.issuer = issuer
        self.audience = audience
        self.kid = f"dev-{uuid.uuid4().hex[:8]}"
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwks(self) -> dict[str, Any]:
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(self._private_key.public_key(), as_dict=True)
        jwk.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        return {"keys": [jwk]}

    def mint(self, subject: str, scopes: list[str], ttl_seconds: int = 3600) -> str:
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "aud": self.audience,
            "sub": subject,
            "scope": " ".join(scopes),
            "iat": now,
            "exp": now + ttl_seconds,
        }
        return jwt.encode(claims, self._private_key, algorithm="RS256", headers={"kid": self.kid})

    def verifier(self) -> TokenVerifier:
        return TokenVerifier(self.issuer, self.audience, static_jwks_resolver(self.jwks()))
