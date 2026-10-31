"""Task handlers against a mocked S3 (moto) and a local webhook server."""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from rws.tasks import handlers
from rws.tasks.errors import DatasetValidationError, TaskError, TransientError


def event(**params: Any) -> dict[str, Any]:
    return {"run_id": "run-1", "step_id": "step", "params": params}


def test_validate_dataset_counts_objects(s3: Any) -> None:
    result = handlers.validate_dataset_handler(event(bucket="data", prefix="ds/"), None)
    assert result == {"object_count": 3, "total_bytes": 18}


def test_validate_dataset_rejects_too_few_objects(s3: Any) -> None:
    with pytest.raises(DatasetValidationError, match="at least 5"):
        handlers.validate_dataset(event(bucket="data", prefix="ds/", min_objects=5))


def test_validate_dataset_rejects_disallowed_extensions(s3: Any) -> None:
    with pytest.raises(DatasetValidationError, match="disallowed extension"):
        handlers.validate_dataset(event(bucket="data", prefix="ds/", allowed_extensions=[".csv"]))


def test_missing_bucket_is_a_permanent_error(s3: Any) -> None:
    with pytest.raises(TaskError, match="NoSuchBucket"):
        handlers.validate_dataset(event(bucket="nope", prefix="ds/"))


def test_compute_checksums_writes_a_deterministic_manifest(s3: Any) -> None:
    first = handlers.compute_checksums_handler(event(bucket="data", prefix="ds/"), None)
    body1 = s3.get_object(Bucket="data", Key=first["manifest_key"])["Body"].read()
    # Running again (at-least-once delivery) ignores the manifest and rewrites the same bytes.
    second = handlers.compute_checksums(event(bucket="data", prefix="ds/"))
    body2 = s3.get_object(Bucket="data", Key=second["manifest_key"])["Body"].read()

    assert (
        first
        == second
        == {
            "algorithm": "sha256",
            "object_count": 3,
            "manifest_key": "ds/manifest-sha256.json",
        }
    )
    assert body1 == body2
    manifest = json.loads(body1)
    assert manifest["files"]["ds/meta.json"] == (
        "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"  # sha256("{}")
    )


def test_copy_objects_is_idempotent(s3: Any) -> None:
    handlers.compute_checksums(event(bucket="data", prefix="ds/"))
    params = {
        "source_bucket": "data",
        "source_prefix": "ds/",
        "dest_bucket": "archive",
        "dest_prefix": "arc/",
    }
    first = handlers.copy_objects_handler(event(**params), None)
    second = handlers.copy_objects(event(**params))

    assert (first["copied"], first["skipped"]) == (3, 0)
    assert (second["copied"], second["skipped"]) == (0, 3)
    keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket="archive")["Contents"])
    assert keys == ["arc/a.csv", "arc/b.csv", "arc/manifest-sha256.json", "arc/meta.json"]


def test_copy_objects_to_bucket_root(s3: Any) -> None:
    result = handlers.copy_objects(
        event(source_bucket="data", source_prefix="ds", dest_bucket="archive", dest_prefix="")
    )
    assert result["copied"] == 3
    assert s3.head_object(Bucket="archive", Key="a.csv")["ContentLength"] == 8


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ClientError({"Error": {"Code": "SlowDown"}}, "GetObject"), TransientError),
        (
            ClientError(
                {"Error": {"Code": "X"}, "ResponseMetadata": {"HTTPStatusCode": 503}},  # type: ignore[typeddict-item]
                "GetObject",
            ),
            TransientError,
        ),
        (ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject"), TaskError),
        (EndpointConnectionError(endpoint_url="http://s3"), TransientError),
    ],
)
def test_boto_errors_are_classified(error: Exception, expected: type[Exception]) -> None:
    assert isinstance(handlers._classify(error), expected)  # type: ignore[arg-type]


def test_notify_without_webhook_logs() -> None:
    assert handlers.notify_handler(event(message="done {run_id}"), None) == {
        "channel": "log",
        "delivered": True,
    }


class _Webhook(BaseHTTPRequestHandler):
    status = 200
    received: list[tuple[dict[str, str], dict[str, Any]]] = []  # noqa: RUF012

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        _Webhook.received.append((dict(self.headers), json.loads(self.rfile.read(length))))
        self.send_response(_Webhook.status)
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def webhook() -> Iterator[str]:
    _Webhook.received.clear()
    server = HTTPServer(("127.0.0.1", 0), _Webhook)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/hook"
    server.shutdown()


def test_notify_posts_to_webhook_with_idempotency_key(webhook: str) -> None:
    _Webhook.status = 200
    result = handlers.notify(event(message="run {run_id} done", webhook_url=webhook))
    assert result == {"channel": "webhook", "delivered": True, "status": 200}
    headers, body = _Webhook.received[0]
    assert headers["Idempotency-Key"] == "run-1:step"
    assert body == {"run_id": "run-1", "message": "run run-1 done"}


@pytest.mark.parametrize(
    ("status", "expected"), [(503, TransientError), (429, TransientError), (400, TaskError)]
)
def test_notify_classifies_webhook_errors(
    webhook: str, status: int, expected: type[Exception]
) -> None:
    _Webhook.status = status
    with pytest.raises(expected):
        handlers.notify(event(message="m", webhook_url=webhook))


def test_notify_unreachable_webhook_is_transient() -> None:
    with pytest.raises(TransientError, match="unreachable"):
        handlers.notify(event(message="m", webhook_url="http://127.0.0.1:9/hook"))


def test_notify_rejects_non_http_schemes() -> None:
    with pytest.raises(TaskError, match="http"):
        handlers.notify(event(message="m", webhook_url="file:///etc/passwd"))
