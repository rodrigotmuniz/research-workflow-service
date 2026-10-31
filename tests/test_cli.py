"""The CLI and typed client, talking to the in-process app (consumer side of OpenAPI)."""

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from rws import cli
from rws.client import ApiError, RwsClient
from rws.client.models import RunStatus
from tests.conftest import ROOT, RUN_INPUT

runner = CliRunner()


@pytest.fixture
def wired(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def make(api_url: str, token: str | None) -> RwsClient:
        return RwsClient("http://testserver", token, transport=client._transport)

    monkeypatch.setattr(cli, "make_client", make)


def invoke(*args: str, token: str | None = None) -> Any:
    env = {"RWS_TOKEN": token} if token else {}
    return runner.invoke(cli.app, list(args), env=env)


def test_cli_full_flow(wired: None, app: FastAPI, s3: Any) -> None:
    token = invoke("login", "--subject", "cli-user").stdout.strip()

    created = invoke(
        "flows", "create", str(ROOT / "examples/archive-dataset.flow.json"), token=token
    )
    assert created.exit_code == 0, created.output
    flow_id = json.loads(created.stdout)["id"]

    assert json.loads(invoke("flows", "get", flow_id, token=token).stdout)["id"] == flow_id
    assert len(json.loads(invoke("flows", "list", token=token).stdout)["items"]) == 1

    started = invoke(
        "runs",
        "start",
        flow_id,
        "--input",
        json.dumps(RUN_INPUT),
        "--idempotency-key",
        "cli-1",
        "--wait",
        "--timeout",
        "10",
        token=token,
    )
    assert started.exit_code == 0, started.output
    run = json.loads(started.stdout)
    assert run["status"] == "SUCCEEDED"

    assert json.loads(invoke("runs", "get", run["id"], token=token).stdout)["status"] == "SUCCEEDED"
    listed = json.loads(
        invoke("runs", "list", "--status", "SUCCEEDED", "--flow-id", flow_id, token=token).stdout
    )
    assert [r["id"] for r in listed["items"]] == [run["id"]]

    cancelled = invoke("runs", "cancel", run["id"], token=token)
    assert cancelled.exit_code == 1
    assert "HTTP 409" in cancelled.stderr


def test_cli_wait_exits_2_when_run_fails(wired: None, s3: Any) -> None:
    token = invoke("login").stdout.strip()
    flow_id = json.loads(
        invoke(
            "flows", "create", str(ROOT / "examples/archive-dataset.flow.json"), token=token
        ).stdout
    )["id"]
    result = invoke(
        "runs",
        "start",
        flow_id,
        "--input",
        json.dumps({**RUN_INPUT, "prefix": "missing/"}),
        "--wait",
        "--timeout",
        "10",
        token=token,
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["status"] == "FAILED"


def test_cli_rejects_bad_json_input(wired: None) -> None:
    result = invoke("runs", "start", "some-flow", "--input", "{not json", token="t")
    assert result.exit_code == 2
    assert "not valid JSON" in result.output


def test_cli_reports_auth_errors(wired: None) -> None:
    result = invoke("flows", "list")
    assert result.exit_code == 1
    assert "HTTP 401" in result.stderr


def test_client_paginates_and_reports_errors(client: TestClient, app: FastAPI) -> None:
    api = RwsClient("http://testserver", transport=client._transport)
    token = api.dev_token("dana", ["flows:read", "runs:start"])
    authed = RwsClient("http://testserver", token, transport=client._transport)

    page = authed.list_runs(status=RunStatus.RUNNING, limit=5, cursor=None)
    assert page.items == []
    assert authed.list_flows(limit=1, cursor=None).next_cursor is None
    with pytest.raises(ApiError) as err:
        authed.get_run("00000000-0000-0000-0000-000000000000")
    assert err.value.status_code == 404
    assert err.value.detail == "run not found"
    with pytest.raises(ApiError) as err:
        authed.list_flows(cursor="paged")
    assert err.value.status_code == 400
