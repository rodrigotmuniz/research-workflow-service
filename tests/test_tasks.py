"""Task handlers against a mocked S3 (moto) and a local webhook server."""

import json
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from rws.tasks import handlers, webhooks
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
    delay = 0.0
    received: list[tuple[dict[str, str], dict[str, Any]]] = []  # noqa: RUF012

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        _Webhook.received.append((dict(self.headers), json.loads(self.rfile.read(length))))
        time.sleep(_Webhook.delay)
        self.send_response(_Webhook.status)
        if 300 <= _Webhook.status < 400:
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def local_webhooks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let tests post to a server on 127.0.0.1 over http (both blocked by default)."""
    real = webhooks.is_public_ip
    monkeypatch.setenv("RWS_WEBHOOK_ALLOW_HTTP", "true")
    monkeypatch.setattr(webhooks, "is_public_ip", lambda a: a == "127.0.0.1" or real(a))


@pytest.fixture
def webhook(local_webhooks: None) -> Iterator[str]:
    _Webhook.received.clear()
    _Webhook.status, _Webhook.delay = 200, 0.0
    server = HTTPServer(("127.0.0.1", 0), _Webhook)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/hook"
    server.shutdown()


def fake_dns(monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    """Every host name resolves to ``address``."""

    def getaddrinfo(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def test_notify_posts_to_webhook_with_idempotency_key(webhook: str) -> None:
    result = handlers.notify(event(message="run {run_id} done", webhook_url=webhook + "?a=1"))
    assert result == {"channel": "webhook", "delivered": True, "status": 200}
    headers, body = _Webhook.received[0]
    assert headers["Idempotency-Key"] == "run-1:step"
    assert body == {"run_id": "run-1", "message": "run run-1 done"}


@pytest.mark.parametrize(
    ("status", "expected"),
    [(503, TransientError), (429, TransientError), (400, TaskError), (302, TaskError)],
)
def test_notify_classifies_webhook_errors(
    webhook: str, status: int, expected: type[Exception]
) -> None:
    _Webhook.status = status
    with pytest.raises(expected, match=str(status)):
        handlers.notify(event(message="m", webhook_url=webhook))
    assert len(_Webhook.received) == 1  # a redirect is reported, never followed


def test_notify_unreachable_webhook_is_transient(local_webhooks: None) -> None:
    with pytest.raises(TransientError, match="unreachable"):
        handlers.notify(event(message="m", webhook_url="http://127.0.0.1:9/hook"))


def test_notify_times_out_quickly(webhook: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webhooks, "TIMEOUT_SECONDS", 0.2)
    _Webhook.delay = 1.0
    with pytest.raises(TransientError, match="timed out"):
        handlers.notify(event(message="m", webhook_url=webhook))


def test_notify_rejects_non_http_schemes() -> None:
    with pytest.raises(TaskError, match="https"):
        handlers.notify(event(message="m", webhook_url="file:///etc/passwd"))


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # cloud instance metadata
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",  # noqa: S104
        "224.0.0.1",
        "240.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
    ],
)
def test_non_public_addresses_are_rejected(address: str) -> None:
    assert not webhooks.is_public_ip(address)


@pytest.mark.parametrize("address", ["8.8.8.8", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert webhooks.is_public_ip(address)


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://hooks.example.com/x", "must use https"),
        ("ftp://hooks.example.com/x", "must use https"),
        ("https:///no-host", "needs a host"),
        ("https://user:pw@hooks.example.com/", "no credentials"),
        ("https://localhost/x", "localhost"),
        ("https://api.localhost/x", "localhost"),
        ("https://127.0.0.1/x", "non-public"),
        ("https://169.254.169.254/latest/meta-data/", "non-public"),
        ("https://[::1]/x", "non-public"),
        ("https://[::ffff:10.0.0.1]/x", "non-public"),
    ],
)
def test_unsafe_urls_are_rejected(url: str, message: str) -> None:
    with pytest.raises(webhooks.UnsafeWebhookURL, match=message):
        webhooks.check_url(url)


def test_safe_url_passes_static_checks() -> None:
    assert webhooks.check_url("https://hooks.example.com/x").hostname == "hooks.example.com"


def test_host_resolving_to_a_private_address_is_rejected_at_send_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The URL looked fine when the flow was created, but DNS now points inside."""
    fake_dns(monkeypatch, "10.0.0.7")
    with pytest.raises(webhooks.UnsafeWebhookURL, match=r"10\.0\.0\.7"):
        handlers.notify(event(message="m", webhook_url="https://hooks.example.com/x"))


def test_unresolvable_host_is_a_task_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(TaskError, match="does not resolve"):
        handlers.notify(event(message="m", webhook_url="https://nope.example.com/x"))


def test_connection_goes_to_the_checked_ip(webhook: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The request uses the address we validated, with the original Host header."""
    port = webhook.split(":")[2].split("/")[0]
    fake_dns(monkeypatch, "127.0.0.1")
    handlers.notify(event(message="m", webhook_url=f"http://hooks.example.com:{port}/hook"))
    headers, _ = _Webhook.received[0]
    assert headers["Host"] == f"hooks.example.com:{port}"


def test_https_verifies_tls(webhook: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """https to a server without a valid certificate fails (and is retried as transient)."""
    port = webhook.split(":")[2].split("/")[0]
    fake_dns(monkeypatch, "127.0.0.1")
    with pytest.raises(TransientError, match="unreachable"):
        handlers.notify(event(message="m", webhook_url=f"https://hooks.example.com:{port}/"))
