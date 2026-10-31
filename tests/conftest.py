"""Shared fixtures: an in-memory app, dev tokens and a mocked S3 (moto)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws
from sqlalchemy.orm import Session, sessionmaker

from rws.api.app import create_app
from rws.auth import DevIssuer
from rws.config import Settings
from rws.db import Base
from rws.executors.local import LocalExecutor

ROOT = Path(__file__).resolve().parent.parent
ALL_SCOPES = ["flows:read", "flows:write", "runs:start"]


class AuthHeaders(Protocol):
    def __call__(self, scopes: list[str] | None = ..., subject: str = ...) -> dict[str, str]: ...


@pytest.fixture(autouse=True)
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake credentials so nothing can ever reach a real AWS account."""
    for key in ("AWS_ENDPOINT_URL", "AWS_PROFILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # A file (not :memory:) so the API and executor threads use separate connections.
    db_url = os.environ.get("TEST_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    return Settings(database_url=db_url, log_json=False, auth_mode="dev")


@pytest.fixture
def app(settings: Settings) -> Iterator[FastAPI]:
    app = create_app(settings)
    engine = app.state.engine
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    # Same executor as production local mode, but retries do not really sleep.
    executor = LocalExecutor(app.state.session_factory, sleep=lambda _: None)
    app.state.executor = executor
    yield app
    executor.shutdown()
    Base.metadata.drop_all(engine)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def session_factory(app: FastAPI) -> sessionmaker[Session]:
    factory: sessionmaker[Session] = app.state.session_factory
    return factory


@pytest.fixture
def auth(app: FastAPI) -> AuthHeaders:
    issuer: DevIssuer = app.state.dev_issuer

    def make(scopes: list[str] | None = None, subject: str = "alice") -> dict[str, str]:
        token = issuer.mint(subject, ALL_SCOPES if scopes is None else scopes)
        return {"Authorization": f"Bearer {token}"}

    return make


@pytest.fixture
def s3() -> Iterator[Any]:
    """Mocked S3 with a data bucket holding a small dataset and an empty archive."""
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="data")
        client.create_bucket(Bucket="archive")
        files = {"a.csv": b"x,y\n1,2\n", "b.csv": b"x,y\n3,4\n", "meta.json": b"{}"}
        for name, body in files.items():
            client.put_object(Bucket="data", Key=f"ds/{name}", Body=body)
        yield client


@pytest.fixture
def example_flow() -> dict[str, Any]:
    flow: dict[str, Any] = json.loads((ROOT / "examples/archive-dataset.flow.json").read_text())
    return flow


RUN_INPUT = {
    "bucket": "data",
    "prefix": "ds/",
    "archive_bucket": "archive",
    "archive_prefix": "archive/ds",
}
