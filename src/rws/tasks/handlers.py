"""The four task actions plus their AWS Lambda entry points.

Every action receives an event ``{"run_id", "step_id", "params"}`` and returns a
JSON-serialisable dict. Actions are written to be *idempotent*: Step Functions (and
our local executor) deliver at-least-once, so running a step twice must be harmless.
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from rws.tasks.errors import DatasetValidationError, TaskError, TransientError

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

log = logging.getLogger(__name__)

Event = dict[str, Any]
Action = Callable[[Event], dict[str, Any]]

MANIFEST_PREFIX = "manifest-"
_TRANSIENT_CODES = {
    "SlowDown",
    "Throttling",
    "RequestTimeout",
    "InternalError",
    "ServiceUnavailable",
}


def s3_client() -> S3Client:
    """S3 client with botocore's own retries. Honours ``AWS_ENDPOINT_URL`` (LocalStack)."""
    return boto3.client("s3", config=Config(retries={"mode": "standard", "max_attempts": 5}))


def _classify(exc: ClientError | BotoCoreError) -> Exception:
    """Map a boto error to a retryable or permanent task error."""
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        if code in _TRANSIENT_CODES or status >= 500:
            return TransientError(f"S3 {code}: {exc}")
        return TaskError(f"S3 {code}: {exc}")
    return TransientError(f"S3 connection error: {exc}")


def _list_objects(s3: S3Client, bucket: str, prefix: str) -> Iterator[dict[str, Any]]:
    """Yield every object under ``prefix`` (paginated), skipping our own manifests."""
    try:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                if not obj["Key"].rsplit("/", 1)[-1].startswith(MANIFEST_PREFIX):
                    yield dict(obj)
    except (ClientError, BotoCoreError) as exc:
        raise _classify(exc) from exc


def validate_dataset(event: Event) -> dict[str, Any]:
    """Check the dataset exists, has enough objects and only allowed file types."""
    p = event["params"]
    objects = list(_list_objects(s3_client(), p["bucket"], p["prefix"]))
    if len(objects) < p.get("min_objects", 1):
        raise DatasetValidationError(
            f"expected at least {p.get('min_objects', 1)} objects under "
            f"s3://{p['bucket']}/{p['prefix']}, found {len(objects)}"
        )
    allowed = p.get("allowed_extensions")
    if allowed:
        bad = [o["Key"] for o in objects if not o["Key"].endswith(tuple(allowed))]
        if bad:
            raise DatasetValidationError(f"{len(bad)} file(s) with disallowed extension: {bad[:5]}")
    return {"object_count": len(objects), "total_bytes": sum(o["Size"] for o in objects)}


def compute_checksums(event: Event) -> dict[str, Any]:
    """Hash every object (streamed) and write a sorted manifest next to the data.

    The manifest is deterministic, so re-running the step overwrites it with the
    same bytes: idempotent.
    """
    p = event["params"]
    algorithm = p.get("algorithm", "sha256")
    s3 = s3_client()
    checksums: dict[str, str] = {}
    try:
        for obj in _list_objects(s3, p["bucket"], p["prefix"]):
            digest = hashlib.new(algorithm)
            body = s3.get_object(Bucket=p["bucket"], Key=obj["Key"])["Body"]
            for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                digest.update(chunk)
            checksums[obj["Key"]] = digest.hexdigest()
        manifest_key = f"{p['prefix'].rstrip('/')}/{MANIFEST_PREFIX}{algorithm}.json"
        manifest = json.dumps({"algorithm": algorithm, "files": checksums}, sort_keys=True)
        s3.put_object(Bucket=p["bucket"], Key=manifest_key, Body=manifest.encode())
    except (ClientError, BotoCoreError) as exc:
        raise _classify(exc) from exc
    return {"algorithm": algorithm, "object_count": len(checksums), "manifest_key": manifest_key}


def copy_objects(event: Event) -> dict[str, Any]:
    """Copy every object to the destination prefix, skipping ones already copied.

    "Already copied" = same size and ETag at the destination, which makes a retry
    after a partial failure cheap and safe.
    """
    p = event["params"]
    s3 = s3_client()
    src_prefix, dest_prefix = p["source_prefix"], p["dest_prefix"].rstrip("/")
    copied = skipped = 0
    try:
        for obj in _list_objects(s3, p["source_bucket"], src_prefix):
            relative = obj["Key"][len(src_prefix) :].lstrip("/")
            dest_key = f"{dest_prefix}/{relative}" if dest_prefix else relative
            if _same_object(s3, p["dest_bucket"], dest_key, obj):
                skipped += 1
                continue
            # Managed copy: switches to multipart for large objects.
            s3.copy({"Bucket": p["source_bucket"], "Key": obj["Key"]}, p["dest_bucket"], dest_key)
            copied += 1
        # Our manifest is excluded from listings; carry it over explicitly if present.
        for manifest in _manifests(s3, p["source_bucket"], src_prefix):
            name = manifest.rsplit("/", 1)[-1]
            dest_key = f"{dest_prefix}/{name}" if dest_prefix else name
            s3.copy({"Bucket": p["source_bucket"], "Key": manifest}, p["dest_bucket"], dest_key)
    except (ClientError, BotoCoreError) as exc:
        raise _classify(exc) from exc
    return {"copied": copied, "skipped": skipped, "dest": f"s3://{p['dest_bucket']}/{dest_prefix}"}


def _manifests(s3: S3Client, bucket: str, prefix: str) -> list[str]:
    root = prefix.rstrip("/") + "/" + MANIFEST_PREFIX
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=root)
    return [o["Key"] for o in resp.get("Contents", [])]


def _same_object(s3: S3Client, bucket: str, key: str, src: dict[str, Any]) -> bool:
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise
    return bool(head["ContentLength"] == src["Size"] and head["ETag"] == src["ETag"])


def notify(event: Event) -> dict[str, Any]:
    """Send a completion message to a webhook (or just log it when none is set).

    The ``Idempotency-Key`` header lets the receiver drop duplicate deliveries.
    """
    p = event["params"]
    message = p["message"].replace("{run_id}", event["run_id"])
    url = p.get("webhook_url")
    if not url:
        log.info("notification", extra={"notification": message})
        return {"channel": "log", "delivered": True}
    if not url.startswith(("https://", "http://")):
        raise TaskError("webhook_url must be http(s)")
    body = json.dumps({"run_id": event["run_id"], "message": message}).encode()
    request = urllib.request.Request(  # noqa: S310 - scheme checked above
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": f"{event['run_id']}:{event['step_id']}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:  # noqa: S310
            status = resp.status
    except urllib.error.HTTPError as exc:
        if exc.code >= 500 or exc.code == 429:
            raise TransientError(f"webhook returned {exc.code}") from exc
        raise TaskError(f"webhook returned {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TransientError(f"webhook unreachable: {exc}") from exc
    return {"channel": "webhook", "delivered": True, "status": status}


ACTIONS: dict[str, Action] = {
    "validate_dataset": validate_dataset,
    "compute_checksums": compute_checksums,
    "copy_objects": copy_objects,
    "notify": notify,
}


# --- AWS Lambda entry points (one function per action = least-privilege IAM) ---------


def validate_dataset_handler(event: Event, context: object) -> dict[str, Any]:
    return validate_dataset(event)


def compute_checksums_handler(event: Event, context: object) -> dict[str, Any]:
    return compute_checksums(event)


def copy_objects_handler(event: Event, context: object) -> dict[str, Any]:
    return copy_objects(event)


def notify_handler(event: Event, context: object) -> dict[str, Any]:
    return notify(event)
