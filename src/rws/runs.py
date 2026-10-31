"""Run lifecycle: start (idempotent), cancel, status transitions and status refresh.

All status changes go through :func:`transition`, a compare-and-set UPDATE that only
succeeds if the run is currently in a status allowed to move to the target. Two
writers racing (e.g. a worker finishing while a user cancels) cannot both win.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rws.db import utcnow
from rws.executors.base import Executor, RunRequest
from rws.flows import FlowDefinition
from rws.models import ALLOWED_TRANSITIONS, Flow, Run, RunStatus

log = logging.getLogger(__name__)


class IdempotencyConflict(Exception):
    """The idempotency key was already used with a different request body."""


class InvalidTransition(Exception):
    """The run cannot move to the requested status (e.g. cancelling a finished run)."""


class ExecutorUnavailable(Exception):
    """The executor could not start the run; the client should retry with the same key."""


def transition(session: Session, run_id: uuid.UUID, to: RunStatus, **fields: Any) -> bool:
    """Move a run to ``to`` if the state machine allows it. Returns True if it moved."""
    sources = [s for s, targets in ALLOWED_TRANSITIONS.items() if to in targets]
    now = utcnow()
    values: dict[str, Any] = {"status": to, "updated_at": now, **fields}
    if to is RunStatus.RUNNING:
        values.setdefault("started_at", now)
    if to.is_terminal:
        values["finished_at"] = now
    result = cast(
        CursorResult[Any],
        session.execute(
            update(Run).where(Run.id == run_id, Run.status.in_(sources)).values(**values)
        ),
    )
    session.commit()
    moved = result.rowcount == 1
    if moved:
        log.info("run status changed", extra={"run_id": str(run_id), "status": str(to)})
    return moved


def set_fields(session: Session, run_id: uuid.UUID, **fields: Any) -> None:
    """Update non-status fields (progress info) of a run."""
    session.execute(update(Run).where(Run.id == run_id).values(updated_at=utcnow(), **fields))
    session.commit()


def request_hash(flow_id: uuid.UUID, run_input: dict[str, Any]) -> str:
    canonical = json.dumps({"flow_id": str(flow_id), "input": run_input}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def start_run(
    session: Session,
    executor: Executor,
    flow: Flow,
    owner_id: str,
    run_input: dict[str, Any],
    idempotency_key: str | None = None,
) -> tuple[Run, bool]:
    """Create and launch a run. Returns ``(run, created)``.

    With an idempotency key, repeating the same request returns the original run
    instead of starting a second one; reusing the key for a different request is a
    conflict.
    """
    digest = request_hash(flow.id, run_input)
    if idempotency_key:
        existing = _find_by_key(session, owner_id, idempotency_key)
        if existing:
            return _replay(session, executor, flow, existing, digest), False

    run = Run(
        flow_id=flow.id,
        owner_id=owner_id,
        input=run_input,
        idempotency_key=idempotency_key,
        request_hash=digest,
        status=RunStatus.PENDING,
    )
    session.add(run)
    try:
        session.commit()
    except IntegrityError:
        # A concurrent request with the same key won the race: replay it.
        session.rollback()
        existing = _find_by_key(session, owner_id, idempotency_key or "")
        if existing is None:  # pragma: no cover - the constraint guarantees it exists
            raise
        return _replay(session, executor, flow, existing, digest), False

    _launch(session, executor, flow, run)
    return run, True


def _find_by_key(session: Session, owner_id: str, key: str) -> Run | None:
    return session.scalar(select(Run).where(Run.owner_id == owner_id, Run.idempotency_key == key))


def _replay(session: Session, executor: Executor, flow: Flow, existing: Run, digest: str) -> Run:
    if existing.request_hash != digest or existing.flow_id != flow.id:
        raise IdempotencyConflict("idempotency key already used with a different request")
    if existing.status == RunStatus.PENDING:
        # The first attempt was stored but never launched (crash or executor outage).
        # Launching again is safe because executors are idempotent per run id.
        _launch(session, executor, flow, existing)
    return existing


def _launch(session: Session, executor: Executor, flow: Flow, run: Run) -> None:
    request = RunRequest(
        run_id=run.id,
        flow_id=flow.id,
        definition=FlowDefinition.model_validate(flow.definition),
        input=run.input,
        state_machine_arn=flow.state_machine_arn,
    )
    try:
        result = executor.start(request)
    except Exception as exc:
        log.exception("executor failed to start run", extra={"run_id": str(run.id)})
        raise ExecutorUnavailable(str(exc)) from exc

    if result.state_machine_arn and flow.state_machine_arn != result.state_machine_arn:
        flow.state_machine_arn = result.state_machine_arn
    run.execution_ref = result.execution_ref
    session.commit()
    transition(session, run.id, RunStatus.RUNNING)  # no-op if a worker got there first
    session.refresh(run)


def cancel_run(session: Session, executor: Executor, run: Run) -> Run:
    if not transition(session, run.id, RunStatus.CANCELLED):
        raise InvalidTransition(f"run is {run.status}; only PENDING or RUNNING can be cancelled")
    if run.execution_ref:
        try:
            executor.cancel(run.execution_ref)
        except Exception:
            # The DB already says CANCELLED; a late stop failure is logged, not surfaced.
            log.exception("executor cancel failed", extra={"run_id": str(run.id)})
    session.refresh(run)
    return run


def refresh_run(session: Session, executor: Executor, run: Run) -> Run:
    """Pull the latest status from the executor for runs it tracks remotely."""
    if RunStatus(run.status).is_terminal or not run.execution_ref:
        return run
    try:
        remote = executor.describe(run.execution_ref)
    except Exception:
        log.exception("could not describe execution", extra={"run_id": str(run.id)})
        return run
    if remote and remote.status != run.status:
        transition(session, run.id, remote.status, output=remote.output, error=remote.error)
        session.refresh(run)
    return run
