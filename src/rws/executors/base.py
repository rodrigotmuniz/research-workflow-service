"""The executor interface the API depends on.

The API never talks to Step Functions or threads directly; it calls an ``Executor``.
That keeps the HTTP layer testable and lets the same code run locally (in-process)
or on AWS (Step Functions + Lambda).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from rws.flows import FlowDefinition
from rws.models import RunStatus


@dataclass(frozen=True)
class RunRequest:
    run_id: uuid.UUID
    flow_id: uuid.UUID
    definition: FlowDefinition
    input: dict[str, Any]
    state_machine_arn: str | None = None

    def initial_document(self) -> dict[str, Any]:
        """The JSON document that flows through the steps."""
        return {"run_id": str(self.run_id), "input": self.input, "results": {}}


@dataclass(frozen=True)
class StartResult:
    execution_ref: str
    state_machine_arn: str | None = None


@dataclass(frozen=True)
class RemoteStatus:
    status: RunStatus
    output: dict[str, Any] | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Executor(Protocol):
    def start(self, request: RunRequest) -> StartResult:
        """Start the run. Must be idempotent per ``run_id`` (safe to call twice)."""
        ...

    def cancel(self, execution_ref: str) -> None:
        """Ask the execution to stop. Best effort; the DB status is already CANCELLED."""
        ...

    def describe(self, execution_ref: str) -> RemoteStatus | None:
        """Current status from the execution backend, or ``None`` if the executor
        writes status to the database itself (local executor)."""
        ...
