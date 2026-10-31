"""FastAPI dependencies: DB session, executor and authentication/authorization."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, SecurityScopes
from sqlalchemy.orm import Session

from rws.auth import SCOPES, AuthError, Principal, TokenVerifier
from rws.db import session_scope
from rws.executors.base import Executor

bearer = HTTPBearer(auto_error=False, description="OAuth2 access token (JWT)")


def get_session(request: Request) -> Iterator[Session]:
    yield from session_scope(request.app.state.session_factory)


def get_executor(request: Request) -> Executor:
    executor: Executor = request.app.state.executor
    return executor


def get_principal(
    security_scopes: SecurityScopes,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Principal:
    """Authenticate the bearer token, then authorize the scopes the endpoint declares."""
    challenge = "Bearer"
    if security_scopes.scopes:
        challenge += f' scope="{security_scopes.scope_str}"'
    if credentials is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing bearer token",
            headers={"WWW-Authenticate": challenge},
        )
    verifier: TokenVerifier = request.app.state.verifier
    try:
        principal = verifier.verify(credentials.credentials)
    except AuthError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid token",
            headers={"WWW-Authenticate": f'{challenge}, error="invalid_token"'},
        ) from exc
    if not principal.has_scopes(set(security_scopes.scopes)):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"missing scope(s): {' '.join(sorted(set(security_scopes.scopes) - principal.scopes))}",
            headers={"WWW-Authenticate": f'{challenge}, error="insufficient_scope"'},
        )
    return principal


SessionDep = Annotated[Session, Depends(get_session)]
ExecutorDep = Annotated[Executor, Depends(get_executor)]

# One alias per permission; FastAPI passes the scopes to get_principal via SecurityScopes.
CanReadFlows = Annotated[Principal, Security(get_principal, scopes=["flows:read"])]
CanWriteFlows = Annotated[Principal, Security(get_principal, scopes=["flows:write"])]
CanStartRuns = Annotated[Principal, Security(get_principal, scopes=["runs:start"])]

assert {"flows:read", "flows:write", "runs:start"} <= set(SCOPES)
