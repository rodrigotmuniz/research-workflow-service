"""Runs through the API with the local executor and a mocked S3."""

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rws.executors.base import RunRequest, StartResult
from rws.executors.local import LocalExecutor
from rws.tasks.errors import TransientError
from tests.conftest import RUN_INPUT, AuthHeaders


@pytest.fixture
def flow_id(client: TestClient, auth: AuthHeaders, example_flow: dict[str, Any]) -> str:
    resp = client.post("/flows", json=example_flow, headers=auth())
    return str(resp.json()["id"])


def start(
    client: TestClient, auth: AuthHeaders, flow_id: str, key: str | None = None, **inp: Any
) -> Any:
    headers = {**auth(), **({"Idempotency-Key": key} if key else {})}
    return client.post(f"/flows/{flow_id}/runs", json={"input": inp or RUN_INPUT}, headers=headers)


def wait(app: FastAPI, run_id: str) -> None:
    app.state.executor.wait(uuid.UUID(run_id))


def test_run_succeeds_end_to_end(
    app: FastAPI, client: TestClient, auth: AuthHeaders, flow_id: str, s3: Any
) -> None:
    resp = start(client, auth, flow_id)
    assert resp.status_code == 202
    run_id = resp.json()["id"]
    assert resp.headers["Location"] == f"/runs/{run_id}"

    wait(app, run_id)
    run = client.get(f"/runs/{run_id}", headers=auth()).json()

    assert run["status"] == "SUCCEEDED"
    assert run["started_at"]
    assert run["finished_at"]
    assert run["output"]["validate"] == {"object_count": 3, "total_bytes": 18}
    assert run["output"]["archive"]["copied"] == 3
    assert run["output"]["notify"]["delivered"] is True
    archived = s3.list_objects_v2(Bucket="archive", Prefix="archive/ds/")["KeyCount"]
    assert archived == 4  # 3 files + checksum manifest


def test_failed_step_fails_the_run(
    app: FastAPI, client: TestClient, auth: AuthHeaders, flow_id: str, s3: Any
) -> None:
    run_id = start(client, auth, flow_id, **{**RUN_INPUT, "prefix": "empty/"}).json()["id"]
    wait(app, run_id)
    run = client.get(f"/runs/{run_id}", headers=auth()).json()

    assert run["status"] == "FAILED"
    assert run["error"].startswith("validate: DatasetValidationError")
    assert run["output"] is None


def test_missing_reference_fails_the_run(
    app: FastAPI, client: TestClient, auth: AuthHeaders, flow_id: str, s3: Any
) -> None:
    run_id = start(client, auth, flow_id, bucket="data").json()["id"]
    wait(app, run_id)
    run = client.get(f"/runs/{run_id}", headers=auth()).json()
    assert run["status"] == "FAILED"
    assert "$.input.prefix" in run["error"]


def test_transient_errors_are_retried(
    app: FastAPI, client: TestClient, auth: AuthHeaders, flow_id: str, s3: Any
) -> None:
    from rws.tasks import handlers

    calls = {"n": 0}
    real_copy = handlers.copy_objects

    def flaky_copy(event: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientError("S3 SlowDown")
        return real_copy(event)

    app.state.executor = LocalExecutor(
        app.state.session_factory,
        actions={**handlers.ACTIONS, "copy_objects": flaky_copy},
        sleep=lambda _: None,
    )
    run_id = start(client, auth, flow_id).json()["id"]
    wait(app, run_id)
    run = client.get(f"/runs/{run_id}", headers=auth()).json()

    assert run["status"] == "SUCCEEDED"
    assert calls["n"] == 3


def test_step_timeout_fails_the_run(
    app: FastAPI, client: TestClient, auth: AuthHeaders, example_flow: dict[str, Any], s3: Any
) -> None:
    import threading

    from rws.tasks import handlers

    release = threading.Event()

    def stuck_validate(event: dict[str, Any]) -> dict[str, Any]:
        release.wait(10)
        return {}

    app.state.executor = LocalExecutor(
        app.state.session_factory,
        actions={**handlers.ACTIONS, "validate_dataset": stuck_validate},
        sleep=lambda _: None,
    )
    example_flow["definition"]["steps"][0]["timeout_seconds"] = 1
    flow = client.post("/flows", json=example_flow, headers=auth()).json()
    run_id = start(client, auth, flow["id"]).json()["id"]
    wait(app, run_id)
    release.set()
    run = client.get(f"/runs/{run_id}", headers=auth()).json()

    assert run["status"] == "FAILED"
    assert run["error"] == "validate: StepTimeout: timed out after 1s"
    assert run["attempts"] == 1  # timeouts are not retried


def test_call_with_timeout_returns_and_propagates_errors() -> None:
    from rws.executors.local import call_with_timeout

    assert call_with_timeout(lambda: 42, 1) == 42

    def socket_timeout() -> None:
        raise TimeoutError("read timed out")

    # The function's own TimeoutError is not mistaken for a step timeout.
    with pytest.raises(TimeoutError, match="read timed out"):
        call_with_timeout(socket_timeout, 1)


def test_idempotency_key_returns_the_same_run(
    app: FastAPI, client: TestClient, auth: AuthHeaders, flow_id: str, s3: Any
) -> None:
    first = start(client, auth, flow_id, key="k-1")
    second = start(client, auth, flow_id, key="k-1")

    assert first.status_code == 202
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    wait(app, first.json()["id"])
    assert len(client.get("/runs", headers=auth()).json()["items"]) == 1


def test_idempotency_key_with_different_body_is_409(
    client: TestClient, auth: AuthHeaders, flow_id: str, s3: Any
) -> None:
    start(client, auth, flow_id, key="k-2")
    resp = start(client, auth, flow_id, key="k-2", **{**RUN_INPUT, "prefix": "other/"})
    assert resp.status_code == 409


def test_idempotency_keys_are_per_user(
    app: FastAPI, client: TestClient, auth: AuthHeaders, example_flow: dict[str, Any], s3: Any
) -> None:
    alice_flow = client.post("/flows", json=example_flow, headers=auth(subject="alice")).json()
    bob_flow = client.post("/flows", json=example_flow, headers=auth(subject="bob")).json()
    headers = {"Idempotency-Key": "same"}
    a = client.post(
        f"/flows/{alice_flow['id']}/runs",
        json={"input": RUN_INPUT},
        headers={**auth(subject="alice"), **headers},
    )
    b = client.post(
        f"/flows/{bob_flow['id']}/runs",
        json={"input": RUN_INPUT},
        headers={**auth(subject="bob"), **headers},
    )
    assert a.status_code == b.status_code == 202
    assert a.json()["id"] != b.json()["id"]


class FlakyStartExecutor:
    """Fails to start the first time, then works: simulates an executor outage."""

    def __init__(self, inner: LocalExecutor) -> None:
        self.inner = inner
        self.calls = 0

    def start(self, request: RunRequest) -> StartResult:
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("executor down")
        return self.inner.start(request)

    def cancel(self, execution_ref: str) -> None:
        self.inner.cancel(execution_ref)

    def describe(self, execution_ref: str) -> None:
        return None


def test_executor_outage_is_503_and_retry_with_same_key_launches(
    app: FastAPI, client: TestClient, auth: AuthHeaders, flow_id: str, s3: Any
) -> None:
    inner = app.state.executor
    app.state.executor = FlakyStartExecutor(inner)

    first = start(client, auth, flow_id, key="k-3")
    assert first.status_code == 503
    assert first.headers["Retry-After"] == "5"

    second = start(client, auth, flow_id, key="k-3")
    assert second.status_code == 200
    run_id = second.json()["id"]
    inner.wait(uuid.UUID(run_id))
    assert client.get(f"/runs/{run_id}", headers=auth()).json()["status"] == "SUCCEEDED"


def test_cancel_running_run(
    app: FastAPI, client: TestClient, auth: AuthHeaders, flow_id: str, s3: Any
) -> None:
    import threading

    from rws.tasks import handlers

    gate = threading.Event()

    def slow_validate(event: dict[str, Any]) -> dict[str, Any]:
        gate.wait(5)
        return handlers.validate_dataset(event)

    app.state.executor = LocalExecutor(
        app.state.session_factory, actions={**handlers.ACTIONS, "validate_dataset": slow_validate}
    )
    run_id = start(client, auth, flow_id).json()["id"]

    cancelled = client.post(f"/runs/{run_id}/cancel", headers=auth())
    gate.set()
    wait(app, run_id)

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    run = client.get(f"/runs/{run_id}", headers=auth()).json()
    # The in-flight step finished, but the worker could not overwrite CANCELLED.
    assert run["status"] == "CANCELLED"
    assert run["output"] is None


def test_cannot_cancel_finished_run(
    app: FastAPI, client: TestClient, auth: AuthHeaders, flow_id: str, s3: Any
) -> None:
    run_id = start(client, auth, flow_id).json()["id"]
    wait(app, run_id)
    resp = client.post(f"/runs/{run_id}/cancel", headers=auth())
    assert resp.status_code == 409


def test_list_runs_filters_and_ownership(
    app: FastAPI, client: TestClient, auth: AuthHeaders, flow_id: str, s3: Any
) -> None:
    ok = start(client, auth, flow_id).json()["id"]
    bad = start(client, auth, flow_id, **{**RUN_INPUT, "prefix": "none/"}).json()["id"]
    wait(app, ok)
    wait(app, bad)

    def ids(**params: Any) -> list[str]:
        resp = client.get("/runs", params=params, headers=auth())
        return [r["id"] for r in resp.json()["items"]]

    assert ids(status="SUCCEEDED") == [ok]
    assert ids(status="FAILED") == [bad]
    assert set(ids(flow_id=flow_id)) == {ok, bad}
    assert client.get("/runs", headers=auth(subject="bob")).json()["items"] == []
    assert client.get(f"/runs/{ok}", headers=auth(subject="bob")).status_code == 404
    assert client.post(f"/runs/{ok}/cancel", headers=auth(subject="bob")).status_code == 404


def test_cannot_start_run_on_someone_elses_flow(
    client: TestClient, auth: AuthHeaders, flow_id: str
) -> None:
    resp = client.post(f"/flows/{flow_id}/runs", json={"input": {}}, headers=auth(subject="bob"))
    assert resp.status_code == 404
