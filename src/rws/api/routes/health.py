"""Liveness and readiness probes (used by ECS / load balancer health checks)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import text

from rws import __version__
from rws.api.deps import SessionDep
from rws.api.schemas import Health

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=Health)
def liveness() -> Health:
    """The process is up. Deliberately does not touch dependencies."""
    return Health(status="ok", version=__version__)


@router.get("/readyz", response_model=Health)
def readiness(session: SessionDep) -> Health:
    """Ready to serve traffic: the database answers."""
    try:
        session.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database unavailable") from exc
    return Health(status="ok", version=__version__)
