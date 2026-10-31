"""ORM models: a Flow (immutable definition) and its Runs."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from rws.db import Base, UTCDateTime, utcnow


class RunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATUSES


TERMINAL_STATUSES = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED})

# The run state machine: which status a run may move to from each status.
ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset({RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


class Flow(Base):
    """A named, immutable flow definition owned by one user.

    Flows are never edited in place: to change a flow, create a new one. That keeps
    every run reproducible from the definition it was started with.
    """

    __tablename__ = "flows"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    definition: Mapped[dict[str, Any]] = mapped_column(JSON)
    # Set lazily by the Step Functions executor the first time the flow runs.
    state_machine_arn: Mapped[str | None] = mapped_column(String(2048), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    runs: Mapped[list[Run]] = relationship(back_populates="flow")

    __table_args__ = (Index("ix_flows_owner_created", "owner_id", "created_at", "id"),)


class Run(Base):
    """One execution of a flow, with its persisted lifecycle status."""

    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    flow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("flows.id"), index=True)
    owner_id: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default=RunStatus.PENDING)
    input: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    current_step: Mapped[str | None] = mapped_column(String(64), default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Client-supplied key that makes "start run" safe to retry.
    idempotency_key: Mapped[str | None] = mapped_column(String(255), default=None)
    request_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    # Opaque handle from the executor (Step Functions execution ARN, or "local:<id>").
    execution_ref: Mapped[str | None] = mapped_column(String(2048), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)

    flow: Mapped[Flow] = relationship(back_populates="runs")

    __table_args__ = (
        UniqueConstraint("owner_id", "idempotency_key", name="uq_runs_owner_idempotency_key"),
        Index("ix_runs_owner_created", "owner_id", "created_at", "id"),
    )
