"""Run endpoints: start (idempotent), get, list and cancel."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Response, status
from sqlalchemy import select

from rws import runs as run_service
from rws.api.deps import CanReadFlows, CanStartRuns, ExecutorDep, SessionDep
from rws.api.routes.flows import get_owned_flow
from rws.api.schemas import RunCreate, RunOut, RunPage
from rws.models import Run, RunStatus
from rws.pagination import paginate

router = APIRouter(tags=["runs"])


def get_owned_run(session: SessionDep, run_id: uuid.UUID, owner_id: str) -> Run:
    run = session.get(Run, run_id)
    if run is None or run.owner_id != owner_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return run


@router.post(
    "/flows/{flow_id}/runs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RunOut,
    responses={
        200: {"description": "Replay: a run with this Idempotency-Key already exists"},
        409: {"description": "Idempotency-Key reused with a different request"},
        503: {"description": "Executor unavailable; retry with the same Idempotency-Key"},
    },
)
def start_run(
    flow_id: uuid.UUID,
    body: RunCreate,
    response: Response,
    session: SessionDep,
    executor: ExecutorDep,
    principal: CanStartRuns,
    idempotency_key: Annotated[
        str | None,
        Header(max_length=255, description="Makes retries safe: same key = same run"),
    ] = None,
) -> Run:
    """Start a run asynchronously. Returns 202 at once; poll ``GET /runs/{id}``."""
    flow = get_owned_flow(session, flow_id, principal.subject)
    try:
        run, created = run_service.start_run(
            session, executor, flow, principal.subject, body.input, idempotency_key
        )
    except run_service.IdempotencyConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except run_service.ExecutorUnavailable as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "could not start run, retry with the same Idempotency-Key",
            headers={"Retry-After": "5"},
        ) from exc
    if not created:
        response.status_code = status.HTTP_200_OK
    response.headers["Location"] = f"/runs/{run.id}"
    return run


@router.get("/runs", response_model=RunPage)
def list_runs(
    session: SessionDep,
    principal: CanReadFlows,
    flow_id: uuid.UUID | None = None,
    status_filter: Annotated[RunStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> RunPage:
    stmt = select(Run).where(Run.owner_id == principal.subject)
    if flow_id:
        stmt = stmt.where(Run.flow_id == flow_id)
    if status_filter:
        stmt = stmt.where(Run.status == status_filter)
    rows, next_cursor = paginate(session, stmt, Run, limit, cursor)
    return RunPage(items=[RunOut.model_validate(r) for r in rows], next_cursor=next_cursor)


@router.get("/runs/{run_id}", response_model=RunOut)
def get_run(
    run_id: uuid.UUID, session: SessionDep, executor: ExecutorDep, principal: CanReadFlows
) -> Run:
    run = get_owned_run(session, run_id, principal.subject)
    return run_service.refresh_run(session, executor, run)


@router.post("/runs/{run_id}/cancel", response_model=RunOut)
def cancel_run(
    run_id: uuid.UUID, session: SessionDep, executor: ExecutorDep, principal: CanStartRuns
) -> Run:
    run = get_owned_run(session, run_id, principal.subject)
    try:
        return run_service.cancel_run(session, executor, run)
    except run_service.InvalidTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
