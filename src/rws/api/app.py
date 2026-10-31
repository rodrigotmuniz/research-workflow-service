"""Application factory: wires settings, database, auth and executor together.

Run with ``uvicorn rws.api.app:create_app --factory``.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import boto3
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker

from rws import __version__
from rws.api.routes import dev, flows, health, runs
from rws.auth import DevIssuer, TokenVerifier, jwks_url_resolver
from rws.config import Settings, get_settings
from rws.db import make_engine, make_session_factory
from rws.executors.base import Executor
from rws.executors.local import LocalExecutor
from rws.executors.stepfunctions import StepFunctionsExecutor
from rws.logging import configure_logging, request_id_var
from rws.pagination import InvalidCursor

log = logging.getLogger("rws.api")

DESCRIPTION = """
Define multi-step data automation flows (validate a dataset, compute checksums,
copy to an archive, notify), start runs asynchronously and track their status.

All endpoints except health checks need an OAuth2 bearer token (JWT) with the
scopes listed on each operation: `flows:read`, `flows:write`, `runs:start`.
"""


def build_executor(settings: Settings, session_factory: sessionmaker[Session]) -> Executor:
    if settings.executor == "stepfunctions":
        client = boto3.client("stepfunctions", region_name=settings.aws_region)
        assert settings.sfn_role_arn  # checked by Settings validation
        return StepFunctionsExecutor(
            client, settings.sfn_role_arn, settings.task_arns(), settings.state_machine_prefix
        )
    return LocalExecutor(session_factory)


def create_app(settings: Settings | None = None, *, executor: Executor | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        shutdown = getattr(app.state.executor, "shutdown", None)
        if shutdown:
            shutdown()
        engine.dispose()

    app = FastAPI(
        title="Research Workflow Service",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.executor = executor or build_executor(settings, session_factory)

    if settings.auth_mode == "dev":
        log.warning("AUTH MODE IS 'dev': the API issues its own tokens. Local use only.")
        issuer = DevIssuer(settings.auth_issuer, settings.auth_audience)
        app.state.dev_issuer = issuer
        app.state.verifier = issuer.verifier()
        app.include_router(dev.router)
    else:
        assert settings.auth_jwks_url  # checked by Settings validation
        app.state.verifier = TokenVerifier(
            settings.auth_issuer, settings.auth_audience, jwks_url_resolver(settings.auth_jwks_url)
        )

    app.include_router(health.router)
    app.include_router(flows.router)
    app.include_router(runs.router)

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = request_id
        log.info(
            "request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        return response

    @app.exception_handler(InvalidCursor)
    async def invalid_cursor(_: Request, exc: InvalidCursor) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    return app
