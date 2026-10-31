"""In-process executor: runs the steps in a thread pool, for local dev and tests.

It follows the same contract as the Step Functions state machine (same task
handlers, same run document, same retry policy), so a flow behaves the same way
locally and on AWS. Status is written straight to the database.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from rws.executors.base import RemoteStatus, RunRequest, StartResult
from rws.logging import run_id_var
from rws.models import RunStatus
from rws.runs import set_fields, transition
from rws.tasks.handlers import ACTIONS, Action
from rws.tasks.retry import call_with_retry

log = logging.getLogger(__name__)


def resolve_refs(value: Any, document: Mapping[str, Any]) -> Any:
    """Replace ``"$.a.b"`` strings with the value at that path in ``document``."""
    if isinstance(value, str) and value.startswith("$."):
        current: Any = document
        for part in value[2:].split("."):
            if not isinstance(current, Mapping) or part not in current:
                raise KeyError(f"reference {value!r} not found in run document")
            current = current[part]
        return current
    if isinstance(value, Mapping):
        return {k: resolve_refs(v, document) for k, v in value.items()}
    return value


class LocalExecutor:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        actions: Mapping[str, Action] = ACTIONS,
        sleep: Callable[[float], None] = time.sleep,
        max_workers: int = 4,
    ) -> None:
        self._sessions = session_factory
        self._actions = actions
        self._sleep = sleep
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="run")
        self._lock = threading.Lock()
        self._futures: dict[uuid.UUID, Future[None]] = {}
        self._cancel_events: dict[uuid.UUID, threading.Event] = {}

    def start(self, request: RunRequest) -> StartResult:
        ref = f"local:{request.run_id}"
        with self._lock:
            if request.run_id not in self._futures:  # idempotent per run_id
                cancel = threading.Event()
                self._cancel_events[request.run_id] = cancel
                self._futures[request.run_id] = self._pool.submit(self._execute, request, cancel)
        return StartResult(execution_ref=ref)

    def cancel(self, execution_ref: str) -> None:
        run_id = uuid.UUID(execution_ref.removeprefix("local:"))
        with self._lock:
            event = self._cancel_events.get(run_id)
        if event:
            event.set()

    def describe(self, execution_ref: str) -> RemoteStatus | None:
        return None  # the worker thread writes status to the DB directly

    def wait(self, run_id: uuid.UUID, timeout: float = 10) -> None:
        """Block until the run's thread finishes (used by tests and the CLI demo)."""
        with self._lock:
            future = self._futures.get(run_id)
        if future:
            future.result(timeout=timeout)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)

    # --- worker thread ---------------------------------------------------------------

    def _execute(self, request: RunRequest, cancel: threading.Event) -> None:
        token = run_id_var.set(str(request.run_id))
        try:
            self._run_steps(request, cancel)
        except Exception as exc:  # never let a worker die silently
            log.exception("run crashed")
            with self._sessions() as session:
                transition(session, request.run_id, RunStatus.FAILED, error=f"internal: {exc}")
        finally:
            run_id_var.reset(token)

    def _run_steps(self, request: RunRequest, cancel: threading.Event) -> None:
        run_id = request.run_id
        with self._sessions() as session:
            # The API may have marked it RUNNING already; either way we proceed.
            transition(session, run_id, RunStatus.RUNNING)
        document = request.initial_document()

        for step in request.definition.steps:
            if cancel.is_set():
                log.info("run cancelled before step", extra={"step": step.id})
                return
            with self._sessions() as session:
                set_fields(session, run_id, current_step=step.id, attempts=1)
            log.info("step started", extra={"step": step.id, "action": step.action})

            action = self._actions[step.action]
            try:
                params = resolve_refs(step.params.model_dump(exclude_none=True), document)
                event = {"run_id": str(run_id), "step_id": step.id, "params": params}
                result = call_with_retry(
                    partial(action, event),
                    step.retry,
                    sleep=self._sleep,
                    on_retry=partial(self._record_retry, run_id),
                )
            except Exception as exc:
                error = f"{step.id}: {type(exc).__name__}: {exc}"
                log.warning("step failed", extra={"step": step.id, "error": error})
                with self._sessions() as session:
                    transition(session, run_id, RunStatus.FAILED, error=error)
                return
            document["results"][step.id] = result
            log.info("step succeeded", extra={"step": step.id})

        with self._sessions() as session:
            transition(
                session, run_id, RunStatus.SUCCEEDED, output=document["results"], current_step=None
            )

    def _record_retry(self, run_id: uuid.UUID, attempt: int, _exc: Exception) -> None:
        with self._sessions() as session:
            set_fields(session, run_id, attempts=attempt + 1)
