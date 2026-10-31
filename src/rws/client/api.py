"""A small typed HTTP client built on the models generated from the OpenAPI schema.

This is the *consumer* side of the published contract: if the API schema changes,
regenerating ``models.py`` (``scripts/generate-client.sh``) surfaces the breakage
here and in the CLI at type-check time.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx

from rws.client.models import FlowCreate, FlowOut, FlowPage, RunCreate, RunOut, RunPage, RunStatus

TERMINAL = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}


class ApiError(Exception):
    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class RwsClient:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._http = httpx.Client(
            base_url=base_url, headers=headers, transport=transport, timeout=timeout
        )

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = self._http.request(method, path, **kwargs)
        if resp.is_error:
            try:
                detail = resp.json().get("detail")
            except ValueError:
                detail = resp.text
            raise ApiError(resp.status_code, detail)
        return resp.json()

    # --- auth (local dev issuer only) --------------------------------------------------

    def dev_token(self, subject: str, scopes: list[str]) -> str:
        data = self._request("POST", "/dev/token", json={"subject": subject, "scopes": scopes})
        return str(data["access_token"])

    # --- flows -------------------------------------------------------------------------

    def create_flow(self, flow: FlowCreate) -> FlowOut:
        body = flow.model_dump(mode="json", exclude_none=True)
        return FlowOut.model_validate(self._request("POST", "/flows", json=body))

    def get_flow(self, flow_id: uuid.UUID | str) -> FlowOut:
        return FlowOut.model_validate(self._request("GET", f"/flows/{flow_id}"))

    def list_flows(self, limit: int = 20, cursor: str | None = None) -> FlowPage:
        params = {"limit": limit, **({"cursor": cursor} if cursor else {})}
        return FlowPage.model_validate(self._request("GET", "/flows", params=params))

    # --- runs --------------------------------------------------------------------------

    def start_run(
        self,
        flow_id: uuid.UUID | str,
        run_input: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> RunOut:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        body = RunCreate(input=run_input).model_dump(mode="json")
        data = self._request("POST", f"/flows/{flow_id}/runs", json=body, headers=headers)
        return RunOut.model_validate(data)

    def get_run(self, run_id: uuid.UUID | str) -> RunOut:
        return RunOut.model_validate(self._request("GET", f"/runs/{run_id}"))

    def list_runs(
        self,
        flow_id: uuid.UUID | str | None = None,
        status: RunStatus | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> RunPage:
        params: dict[str, Any] = {"limit": limit}
        if flow_id:
            params["flow_id"] = str(flow_id)
        if status:
            params["status"] = status.value
        if cursor:
            params["cursor"] = cursor
        return RunPage.model_validate(self._request("GET", "/runs", params=params))

    def cancel_run(self, run_id: uuid.UUID | str) -> RunOut:
        return RunOut.model_validate(self._request("POST", f"/runs/{run_id}/cancel"))

    def wait_for_run(
        self, run_id: uuid.UUID | str, timeout: float = 60, interval: float = 1
    ) -> RunOut:
        """Poll until the run reaches a terminal status."""
        deadline = time.monotonic() + timeout
        while True:
            run = self.get_run(run_id)
            if run.status in TERMINAL or time.monotonic() >= deadline:
                return run
            time.sleep(interval)
