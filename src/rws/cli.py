"""``rws`` command-line interface, a thin layer over :class:`rws.client.RwsClient`.

Configuration: ``RWS_API_URL`` (default http://localhost:8000) and ``RWS_TOKEN``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import BaseModel

from rws.client import ApiError, RwsClient
from rws.client.models import FlowCreate, RunStatus

app = typer.Typer(help="Research Workflow Service CLI", no_args_is_help=True)
flows_app = typer.Typer(help="Manage flows", no_args_is_help=True)
runs_app = typer.Typer(help="Start and track runs", no_args_is_help=True)
app.add_typer(flows_app, name="flows")
app.add_typer(runs_app, name="runs")

ApiUrl = Annotated[str, typer.Option(envvar="RWS_API_URL", help="API base URL")]
Token = Annotated[str | None, typer.Option(envvar="RWS_TOKEN", help="Bearer token")]
DEFAULT_URL = "http://localhost:8000"


def make_client(api_url: str, token: str | None) -> RwsClient:
    """Factory (patched in tests to talk to an in-process app)."""
    return RwsClient(api_url, token)


def _echo(model: BaseModel | dict[str, Any]) -> None:
    data = model.model_dump(mode="json") if isinstance(model, BaseModel) else model
    typer.echo(json.dumps(data, indent=2))


def _call(api_url: str, token: str | None, fn_name: str, *args: Any, **kwargs: Any) -> Any:
    client = make_client(api_url, token)
    try:
        return getattr(client, fn_name)(*args, **kwargs)
    except ApiError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        client.close()


@app.command()
def login(
    api_url: ApiUrl = DEFAULT_URL,
    subject: str = "dev-user",
    scopes: str = "flows:read flows:write runs:start",
) -> None:
    """Get a token from the local dev issuer. Usage: ``export RWS_TOKEN=$(rws login)``."""
    typer.echo(_call(api_url, None, "dev_token", subject, scopes.split()))


@flows_app.command("create")
def flows_create(file: Path, api_url: ApiUrl = DEFAULT_URL, token: Token = None) -> None:
    """Create a flow from a JSON file (``{"name", "description", "definition"}``)."""
    flow = FlowCreate.model_validate_json(file.read_text())
    _echo(_call(api_url, token, "create_flow", flow))


@flows_app.command("list")
def flows_list(api_url: ApiUrl = DEFAULT_URL, token: Token = None, limit: int = 20) -> None:
    _echo(_call(api_url, token, "list_flows", limit))


@flows_app.command("get")
def flows_get(flow_id: str, api_url: ApiUrl = DEFAULT_URL, token: Token = None) -> None:
    _echo(_call(api_url, token, "get_flow", flow_id))


@runs_app.command("start")
def runs_start(
    flow_id: str,
    input_json: Annotated[str, typer.Option("--input", help="Run input as JSON")] = "{}",
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    wait: Annotated[bool, typer.Option(help="Wait until the run finishes")] = False,
    timeout: float = 120,
    api_url: ApiUrl = DEFAULT_URL,
    token: Token = None,
) -> None:
    """Start a run. With ``--wait``, exit code 0 only if the run SUCCEEDED."""
    try:
        run_input = json.loads(input_json)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"--input is not valid JSON: {exc}") from exc
    run = _call(api_url, token, "start_run", flow_id, run_input, idempotency_key)
    if wait:
        run = _call(api_url, token, "wait_for_run", run.id, timeout)
    _echo(run)
    if wait and run.status != RunStatus.SUCCEEDED:
        raise typer.Exit(code=2)


@runs_app.command("get")
def runs_get(run_id: str, api_url: ApiUrl = DEFAULT_URL, token: Token = None) -> None:
    _echo(_call(api_url, token, "get_run", run_id))


@runs_app.command("list")
def runs_list(
    flow_id: str | None = None,
    status: RunStatus | None = None,
    limit: int = 20,
    api_url: ApiUrl = DEFAULT_URL,
    token: Token = None,
) -> None:
    _echo(_call(api_url, token, "list_runs", flow_id, status, limit))


@runs_app.command("cancel")
def runs_cancel(run_id: str, api_url: ApiUrl = DEFAULT_URL, token: Token = None) -> None:
    _echo(_call(api_url, token, "cancel_run", run_id))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
