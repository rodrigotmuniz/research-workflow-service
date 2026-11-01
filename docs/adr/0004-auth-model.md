# ADR 4: OAuth2 resource server with scopes and ownership

Status: accepted

## Context

Researchers authenticate with their institution's identity provider. The service must not
handle passwords, must work with any OpenID Connect provider, and must keep each user's
flows and runs private.

## Decision

- The API is an OAuth2 **resource server**. It accepts `Authorization: Bearer <JWT>` and
  verifies the RS256 signature with keys from the provider's JWKS endpoint (cached), plus
  `iss`, `aud`, `exp`, `iat` and `sub`. Only RS256 is accepted (no `none`, no HMAC).
- **Scopes per endpoint**: `flows:read` (read flows and runs), `flows:write` (create
  flows), `runs:start` (start and cancel runs). A missing scope is `403` with
  `WWW-Authenticate: Bearer error="insufficient_scope"`.
- **Ownership**: the token subject owns what it creates; every query filters by it, and
  other users' resources return `404` so ids cannot be probed.
- **Safe default**: `RWS_AUTH_MODE` defaults to `jwks`, which requires a JWKS URL and
  does not mount the dev routes. A container started without auth settings fails to
  start instead of serving an open token endpoint.
- **Local development**: only with an explicit `RWS_AUTH_MODE=dev` (docker compose,
  `make run`, tests) the API generates an RSA key pair in memory at startup, exposes
  `POST /dev/token` (hidden from the schema) and verifies its own tokens through the
  same code path. `RWS_ENV=production` (set in the ECS task definition) makes dev mode a
  startup error, so a copied local config cannot open production.

## Consequences

- Any OIDC provider works with three settings (issuer, audience, JWKS URL).
- Authorization is coarse (owner-only). Sharing flows with groups would add a membership
  table and role checks in the same dependency.
