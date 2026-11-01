"""Health checks, run state machine rules, logging, settings, migrations and OpenAPI."""

import io
import json
import logging
import uuid
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from rws import __version__
from rws.config import Settings
from rws.db import Base, UTCDateTime
from rws.logging import JsonFormatter, configure_logging, request_id_var, run_id_var
from rws.models import Flow, Run, RunStatus
from rws.openapi import build_schema
from rws.pagination import InvalidCursor, decode_cursor
from rws.runs import transition
from tests.conftest import ROOT

# --- health ------------------------------------------------------------------------


def test_liveness(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.json() == {"status": "ok", "version": __version__}
    assert "X-Request-ID" in resp.headers


def test_readiness_ok_and_request_id_is_echoed(client: TestClient) -> None:
    resp = client.get("/readyz", headers={"X-Request-ID": "abc"})
    assert resp.status_code == 200
    assert resp.headers["X-Request-ID"] == "abc"


@pytest.mark.parametrize(
    "bad_id",
    ["x" * 129, "abc\ninjected log line", "<script>", "spaces are not ok", ""],
)
def test_unsafe_request_ids_are_replaced(client: TestClient, bad_id: str) -> None:
    resp = client.get("/healthz", headers={"X-Request-ID": bad_id})
    assert resp.headers["X-Request-ID"] != bad_id
    assert len(resp.headers["X-Request-ID"]) == 32  # a fresh uuid4 hex


def test_request_id_up_to_128_safe_chars_is_kept(client: TestClient) -> None:
    good = "trace-1.2:3_" + "a" * 116
    assert client.get("/healthz", headers={"X-Request-ID": good}).headers["X-Request-ID"] == good


def test_readiness_fails_without_database(app: FastAPI, client: TestClient) -> None:
    app.state.session_factory = sessionmaker(bind=create_engine("sqlite:////nonexistent/dir/x.db"))
    assert client.get("/readyz").status_code == 503


# --- run state machine ---------------------------------------------------------------


@pytest.fixture
def run_id(session_factory: sessionmaker[Session]) -> uuid.UUID:
    with session_factory() as session:
        flow = Flow(owner_id="a", name="f", definition={})
        session.add(flow)
        session.flush()
        run = Run(flow_id=flow.id, owner_id="a", input={})
        session.add(run)
        session.commit()
        return run.id


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ([RunStatus.RUNNING, RunStatus.SUCCEEDED], [True, True]),
        ([RunStatus.CANCELLED, RunStatus.RUNNING], [True, False]),
        ([RunStatus.RUNNING, RunStatus.FAILED, RunStatus.SUCCEEDED], [True, True, False]),
        ([RunStatus.SUCCEEDED], [False]),  # cannot skip RUNNING
        ([RunStatus.RUNNING, RunStatus.RUNNING], [True, False]),
    ],
)
def test_transitions_follow_the_state_machine(
    session_factory: sessionmaker[Session],
    run_id: uuid.UUID,
    path: list[RunStatus],
    allowed: list[bool],
) -> None:
    with session_factory() as session:
        assert [transition(session, run_id, s) for s in path] == allowed


def test_terminal_transition_sets_timestamps(
    session_factory: sessionmaker[Session], run_id: uuid.UUID
) -> None:
    with session_factory() as session:
        transition(session, run_id, RunStatus.RUNNING)
        transition(session, run_id, RunStatus.SUCCEEDED, output={"x": 1})
        run = session.get(Run, run_id)
        assert run is not None
        assert run.started_at is not None
        assert run.finished_at is not None
        assert run.finished_at >= run.started_at
        assert run.finished_at.tzinfo is not None


def test_naive_datetimes_are_rejected() -> None:
    from datetime import datetime

    with pytest.raises(ValueError, match="naive"):
        UTCDateTime().process_bind_param(datetime(2026, 1, 1), None)  # type: ignore[arg-type]


def test_bad_cursor() -> None:
    with pytest.raises(InvalidCursor):
        decode_cursor("bm90LWpzb24")


# --- logging & settings ----------------------------------------------------------------


def test_json_log_lines_carry_correlation_ids() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("test.json")
    logger.addHandler(handler)
    logger.propagate = False
    t1, t2 = request_id_var.set("req-1"), run_id_var.set("run-1")
    try:
        logger.warning("hello", extra={"step": "validate"})
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            logger.exception("failed")
    finally:
        request_id_var.reset(t1)
        run_id_var.reset(t2)
    first, second = (
        json.loads(line) for line in stream.getvalue().splitlines() if line.startswith("{")
    )
    assert first["msg"] == "hello"
    assert (first["request_id"], first["run_id"], first["step"]) == ("req-1", "run-1", "validate")
    assert "RuntimeError: boom" in second["exc"]


def test_configure_logging_plain_text() -> None:
    configure_logging("DEBUG", json_output=False)
    assert logging.getLogger().level == logging.DEBUG
    assert not isinstance(logging.getLogger().handlers[0].formatter, JsonFormatter)
    configure_logging("INFO", json_output=True)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "RWS_AUTH_JWKS_URL"),  # the default is the safe mode: an external OIDC provider
        ({"auth_mode": "jwks"}, "RWS_AUTH_JWKS_URL"),
        ({"auth_mode": "dev", "env": "production"}, "not allowed when RWS_ENV=production"),
        ({"auth_mode": "dev", "executor": "stepfunctions"}, "RWS_SFN_ROLE_ARN"),
    ],
)
def test_settings_validation(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any], message: str
) -> None:
    for name in ("RWS_AUTH_MODE", "RWS_AUTH_JWKS_URL", "RWS_ENV", "RWS_EXECUTOR"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError, match=message):
        Settings(**kwargs)


def test_dev_auth_mode_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RWS_AUTH_MODE", "dev")
    assert Settings().auth_mode == "dev"
    monkeypatch.setenv("RWS_ENV", "production")
    with pytest.raises(ValidationError, match="not allowed"):
        Settings()


def test_production_settings_build_the_aws_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    from rws.api.app import create_app
    from rws.executors.stepfunctions import StepFunctionsExecutor

    monkeypatch.setenv("RWS_TASK_ARN_NOTIFY", "arn:aws:lambda:us-east-1:1:function:n")
    settings = Settings(
        database_url="sqlite://",
        auth_mode="jwks",
        auth_jwks_url="https://idp.example/jwks",
        executor="stepfunctions",
        sfn_role_arn="arn:aws:iam::1:role/r",
    )
    assert settings.task_arns() == {"notify": "arn:aws:lambda:us-east-1:1:function:n"}
    app = create_app(settings)
    assert isinstance(app.state.executor, StepFunctionsExecutor)
    assert not hasattr(app.state, "dev_issuer")
    with TestClient(app) as client:
        assert client.post("/dev/token", json={}).status_code == 404


# --- migrations & published schema ------------------------------------------------------


def test_migrations_match_the_models(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'migrations.db'}"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.attributes["database_url"] = url
    command.upgrade(config, "head")

    engine = create_engine(url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []

    # Migration 0002: the database itself rejects an unknown run status.
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO flows VALUES ('f1', 'a', 'f', '', '{}', NULL, '2026-01-01')")
        )
        insert_run = (
            "INSERT INTO runs (id, flow_id, owner_id, status, input, attempts, created_at,"
            " updated_at) VALUES (:id, 'f1', 'a', :status, '{}', 0, '2026-01-01', '2026-01-01')"
        )
        conn.execute(text(insert_run), {"id": "r1", "status": "RUNNING"})
    with pytest.raises(IntegrityError, match="ck_runs_status"), engine.begin() as conn:
        conn.execute(text(insert_run), {"id": "r2", "status": "PAUSED"})
    engine.dispose()
    command.downgrade(config, "base")


def test_models_reject_unknown_status(
    session_factory: sessionmaker[Session], run_id: uuid.UUID
) -> None:
    with session_factory() as session, pytest.raises(IntegrityError):
        session.execute(update(Run).where(Run.id == run_id).values(status="PAUSED"))


def test_committed_openapi_is_up_to_date() -> None:
    """openapi.json is the published contract; regenerate it with scripts/generate-client.sh."""
    committed = json.loads((ROOT / "openapi.json").read_text())
    assert build_schema() == committed


def test_openapi_documents_idempotency_and_security() -> None:
    schema = build_schema()
    start = schema["paths"]["/flows/{flow_id}/runs"]["post"]
    assert any(p["name"] == "Idempotency-Key" for p in start["parameters"])
    assert {"200", "202", "409", "503"} <= set(start["responses"])
    assert "/dev/token" not in schema["paths"]
