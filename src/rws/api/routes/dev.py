"""Local-only token endpoint. Mounted only when RWS_AUTH_MODE=dev; hidden from OpenAPI."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from rws.api.schemas import DevTokenRequest, DevTokenResponse
from rws.auth import SCOPES, DevIssuer

router = APIRouter(prefix="/dev", include_in_schema=False)

TTL_SECONDS = 3600


@router.post("/token", response_model=DevTokenResponse)
def mint_token(body: DevTokenRequest, request: Request) -> DevTokenResponse:
    unknown = set(body.scopes) - set(SCOPES)
    if unknown:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown scopes: {sorted(unknown)}")
    issuer: DevIssuer = request.app.state.dev_issuer
    token = issuer.mint(body.subject, body.scopes, ttl_seconds=TTL_SECONDS)
    return DevTokenResponse(access_token=token, expires_in=TTL_SECONDS)


@router.get("/jwks.json")
def jwks(request: Request) -> dict[str, Any]:
    issuer: DevIssuer = request.app.state.dev_issuer
    return issuer.jwks()
