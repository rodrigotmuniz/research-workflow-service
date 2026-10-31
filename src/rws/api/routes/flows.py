"""Flow endpoints. Users only ever see their own flows (ownership = token subject)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from rws.api.deps import CanReadFlows, CanWriteFlows, SessionDep
from rws.api.schemas import FlowCreate, FlowOut, FlowPage
from rws.models import Flow
from rws.pagination import paginate

router = APIRouter(prefix="/flows", tags=["flows"])


def get_owned_flow(session: SessionDep, flow_id: uuid.UUID, owner_id: str) -> Flow:
    flow = session.get(Flow, flow_id)
    # 404 (not 403) for other users' flows: do not reveal that the id exists.
    if flow is None or flow.owner_id != owner_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "flow not found")
    return flow


@router.post("", status_code=status.HTTP_201_CREATED, response_model=FlowOut)
def create_flow(body: FlowCreate, session: SessionDep, principal: CanWriteFlows) -> Flow:
    flow = Flow(
        owner_id=principal.subject,
        name=body.name,
        description=body.description,
        definition=body.definition.model_dump(mode="json", exclude_none=True),
    )
    session.add(flow)
    session.commit()
    return flow


@router.get("", response_model=FlowPage)
def list_flows(
    session: SessionDep,
    principal: CanReadFlows,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> FlowPage:
    stmt = select(Flow).where(Flow.owner_id == principal.subject)
    rows, next_cursor = paginate(session, stmt, Flow, limit, cursor)
    return FlowPage(items=[FlowOut.model_validate(r) for r in rows], next_cursor=next_cursor)


@router.get("/{flow_id}", response_model=FlowOut)
def get_flow(flow_id: uuid.UUID, session: SessionDep, principal: CanReadFlows) -> Flow:
    return get_owned_flow(session, flow_id, principal.subject)
