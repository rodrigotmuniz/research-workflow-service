"""AWS Step Functions executor: one state machine per flow, one execution per run."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING

from rws.asl import compile_to_asl
from rws.executors.base import RemoteStatus, RunRequest, StartResult
from rws.models import RunStatus

if TYPE_CHECKING:
    from mypy_boto3_stepfunctions import SFNClient

log = logging.getLogger(__name__)

_STATUS_MAP = {
    "RUNNING": RunStatus.RUNNING,
    "PENDING_REDRIVE": RunStatus.RUNNING,
    "SUCCEEDED": RunStatus.SUCCEEDED,
    "FAILED": RunStatus.FAILED,
    "TIMED_OUT": RunStatus.FAILED,
    "ABORTED": RunStatus.CANCELLED,
}


class StepFunctionsExecutor:
    def __init__(
        self,
        client: SFNClient,
        role_arn: str,
        task_arns: Mapping[str, str],
        name_prefix: str = "rws-flow",
    ) -> None:
        self._sfn = client
        self._role_arn = role_arn
        self._task_arns = task_arns
        self._prefix = name_prefix

    def start(self, request: RunRequest) -> StartResult:
        sm_arn = request.state_machine_arn or self._ensure_state_machine(request)
        try:
            # The execution name is the run id, so a repeated start cannot create a
            # second execution: AWS returns the running one (same name + input) or
            # raises ExecutionAlreadyExists (e.g. it already finished).
            resp = self._sfn.start_execution(
                stateMachineArn=sm_arn,
                name=str(request.run_id),
                input=json.dumps(request.initial_document()),
            )
            ref = resp["executionArn"]
        except self._sfn.exceptions.ExecutionAlreadyExists:
            ref = f"{sm_arn.replace(':stateMachine:', ':execution:')}:{request.run_id}"
            log.info("execution already started", extra={"execution_arn": ref})
        return StartResult(execution_ref=ref, state_machine_arn=sm_arn)

    def _ensure_state_machine(self, request: RunRequest) -> str:
        """Create the flow's state machine. Flows are immutable, so this happens once;
        CreateStateMachine with an identical definition is itself idempotent."""
        definition = compile_to_asl(
            request.definition, self._task_arns, comment=f"flow {request.flow_id}"
        )
        resp = self._sfn.create_state_machine(
            name=f"{self._prefix}-{request.flow_id}",
            definition=json.dumps(definition),
            roleArn=self._role_arn,
            type="STANDARD",
            tags=[{"key": "rws:flow_id", "value": str(request.flow_id)}],
        )
        return resp["stateMachineArn"]

    def cancel(self, execution_ref: str) -> None:
        self._sfn.stop_execution(executionArn=execution_ref, cause="cancelled by user")

    def describe(self, execution_ref: str) -> RemoteStatus | None:
        resp = self._sfn.describe_execution(executionArn=execution_ref)
        status = _STATUS_MAP.get(resp["status"], RunStatus.RUNNING)
        output = None
        if status is RunStatus.SUCCEEDED and resp.get("output"):
            output = json.loads(resp["output"]).get("results")
        error = None
        if status is RunStatus.FAILED:
            error = f"{resp.get('error', resp['status'])}: {resp.get('cause', '')}".strip(": ")
        return RemoteStatus(status=status, output=output, error=error)
