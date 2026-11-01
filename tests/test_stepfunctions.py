"""Step Functions executor against moto, plus status mapping and the API refresh path."""

import json
import uuid
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from rws.asl import TEMPLATE_ARNS
from rws.executors.base import RunRequest
from rws.executors.stepfunctions import StepFunctionsExecutor
from rws.flows import FlowDefinition
from rws.models import RunStatus
from tests.conftest import RUN_INPUT, AuthHeaders

ROLE = "arn:aws:iam::123456789012:role/rws-sfn"
ARNS = {k: f"arn:aws:lambda:us-east-1:123456789012:function:{k}" for k in TEMPLATE_ARNS}


@pytest.fixture
def sfn() -> Iterator[Any]:
    with mock_aws():
        yield boto3.client("stepfunctions", region_name="us-east-1")


def request(example_flow: dict[str, Any], sm_arn: str | None = None) -> RunRequest:
    return RunRequest(
        run_id=uuid.uuid4(),
        flow_id=uuid.uuid4(),
        definition=FlowDefinition.model_validate(example_flow["definition"]),
        input=RUN_INPUT,
        state_machine_arn=sm_arn,
    )


def test_start_creates_state_machine_and_execution(sfn: Any, example_flow: dict[str, Any]) -> None:
    executor = StepFunctionsExecutor(sfn, ROLE, ARNS)
    req = request(example_flow)
    result = executor.start(req)

    assert result.state_machine_arn
    assert result.state_machine_arn.endswith(f":stateMachine:rws-flow-{req.flow_id}")
    machine = sfn.describe_state_machine(stateMachineArn=result.state_machine_arn)
    assert json.loads(machine["definition"])["StartAt"] == "validate"

    execution = sfn.describe_execution(executionArn=result.execution_ref)
    assert execution["name"] == str(req.run_id)
    assert json.loads(execution["input"]) == {
        "run_id": str(req.run_id),
        "input": RUN_INPUT,
        "results": {},
    }
    status = executor.describe(result.execution_ref)
    assert status is not None
    assert status.status is RunStatus.RUNNING


def test_start_is_idempotent_per_run_id(sfn: Any, example_flow: dict[str, Any]) -> None:
    executor = StepFunctionsExecutor(sfn, ROLE, ARNS)
    req = request(example_flow)
    first = executor.start(req)
    # e.g. a client retry with the same Idempotency-Key after a crash
    again = executor.start(replace(req, state_machine_arn=first.state_machine_arn))
    assert again.execution_ref == first.execution_ref
    executions = sfn.list_executions(stateMachineArn=first.state_machine_arn)["executions"]
    assert len(executions) == 1


def test_start_when_execution_already_exists(sfn: Any, example_flow: dict[str, Any]) -> None:
    """A retry after the execution finished: AWS refuses a second one with that name."""
    sm_arn = "arn:aws:states:us-east-1:123456789012:stateMachine:rws-flow-x"
    req = request(example_flow, sm_arn=sm_arn)
    with Stubber(sfn) as stub:
        stub.add_client_error(
            "start_execution", service_error_code="ExecutionAlreadyExists", http_status_code=400
        )
        result = StepFunctionsExecutor(sfn, ROLE, ARNS).start(req)
        stub.assert_no_pending_responses()
    # The existing execution is referenced by its deterministic ARN (name = run id).
    assert result.execution_ref == (
        f"arn:aws:states:us-east-1:123456789012:execution:rws-flow-x:{req.run_id}"
    )
    assert result.state_machine_arn == sm_arn


def test_cancel_stops_execution(sfn: Any, example_flow: dict[str, Any]) -> None:
    executor = StepFunctionsExecutor(sfn, ROLE, ARNS)
    result = executor.start(request(example_flow))
    executor.cancel(result.execution_ref)
    status = executor.describe(result.execution_ref)
    assert status is not None
    assert status.status is RunStatus.CANCELLED


class FakeSFN:
    """Just enough of the client to return chosen DescribeExecution responses."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response

    def describe_execution(self, executionArn: str) -> dict[str, Any]:
        return self.response


@pytest.mark.parametrize(
    ("response", "expected", "output", "error"),
    [
        (
            {"status": "SUCCEEDED", "output": json.dumps({"results": {"a": {"ok": 1}}})},
            RunStatus.SUCCEEDED,
            {"a": {"ok": 1}},
            None,
        ),
        (
            {"status": "FAILED", "error": "DatasetValidationError", "cause": "empty"},
            RunStatus.FAILED,
            None,
            "DatasetValidationError: empty",
        ),
        ({"status": "TIMED_OUT"}, RunStatus.FAILED, None, "TIMED_OUT"),
        ({"status": "ABORTED"}, RunStatus.CANCELLED, None, None),
    ],
)
def test_describe_maps_statuses(
    response: dict[str, Any], expected: RunStatus, output: Any, error: str | None
) -> None:
    executor = StepFunctionsExecutor(FakeSFN(response), ROLE, ARNS)  # type: ignore[arg-type]
    status = executor.describe("arn")
    assert status is not None
    assert (status.status, status.output, status.error) == (expected, output, error)


def test_api_with_step_functions_executor(
    app: FastAPI, client: TestClient, auth: AuthHeaders, example_flow: dict[str, Any], sfn: Any
) -> None:
    """Start through the API, then let the (mocked) execution succeed and read it back."""
    app.state.executor = StepFunctionsExecutor(sfn, ROLE, ARNS)
    flow_id = client.post("/flows", json=example_flow, headers=auth()).json()["id"]

    run = client.post(f"/flows/{flow_id}/runs", json={"input": RUN_INPUT}, headers=auth()).json()
    assert run["status"] == "RUNNING"
    assert client.get(f"/runs/{run['id']}", headers=auth()).json()["status"] == "RUNNING"

    # The flow's state machine is created once and reused by the next run.
    second = client.post(f"/flows/{flow_id}/runs", json={"input": RUN_INPUT}, headers=auth())
    assert second.status_code == 202
    assert len(sfn.list_state_machines()["stateMachines"]) == 1

    app.state.executor = StepFunctionsExecutor(
        FakeSFN({"status": "SUCCEEDED", "output": json.dumps({"results": {"x": 1}})}),  # type: ignore[arg-type]
        ROLE,
        ARNS,
    )
    done = client.get(f"/runs/{run['id']}", headers=auth()).json()
    assert done["status"] == "SUCCEEDED"
    assert done["output"] == {"x": 1}


def test_describe_failure_returns_last_known_status(
    app: FastAPI, client: TestClient, auth: AuthHeaders, example_flow: dict[str, Any], sfn: Any
) -> None:
    app.state.executor = StepFunctionsExecutor(sfn, ROLE, ARNS)
    flow_id = client.post("/flows", json=example_flow, headers=auth()).json()["id"]
    run = client.post(f"/flows/{flow_id}/runs", json={"input": RUN_INPUT}, headers=auth()).json()

    class Broken:
        def describe_execution(self, executionArn: str) -> dict[str, Any]:
            raise ConnectionError("throttled")

        def stop_execution(self, executionArn: str, cause: str) -> None:
            raise ConnectionError("throttled")

    app.state.executor = StepFunctionsExecutor(Broken(), ROLE, ARNS)  # type: ignore[arg-type]
    assert client.get(f"/runs/{run['id']}", headers=auth()).json()["status"] == "RUNNING"
    # Cancel still succeeds: the DB is the source of truth, the stop is best effort.
    assert client.post(f"/runs/{run['id']}/cancel", headers=auth()).json()["status"] == "CANCELLED"
