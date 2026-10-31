"""Request/response models: these define the published OpenAPI schema."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from rws.flows import FlowDefinition
from rws.models import RunStatus


class FlowCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str = Field("", max_length=2000)
    definition: FlowDefinition


class FlowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str
    definition: FlowDefinition
    created_at: datetime


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any] = Field(default_factory=dict, description="Available to steps as $.input")


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    flow_id: uuid.UUID
    status: RunStatus
    input: dict[str, Any]
    output: dict[str, Any] | None
    error: str | None
    current_step: str | None
    attempts: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class FlowPage(BaseModel):
    items: list[FlowOut]
    next_cursor: str | None


class RunPage(BaseModel):
    items: list[RunOut]
    next_cursor: str | None


class Health(BaseModel):
    status: str
    version: str


class DevTokenRequest(BaseModel):
    subject: str = "dev-user"
    scopes: list[str] = Field(default_factory=lambda: ["flows:read", "flows:write", "runs:start"])


class DevTokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"  # noqa: S105 - not a secret
    expires_in: int
