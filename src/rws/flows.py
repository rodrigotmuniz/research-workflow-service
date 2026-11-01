"""The flow definition language: an ordered list of typed steps.

A flow is validated once, when it is created, so a run never starts with a bad
definition. Each step names an ``action`` (one of the task handlers) and its
``params``. A string param starting with ``$.`` is a reference into the run
document, e.g. ``"$.input.bucket"`` or ``"$.results.checksums.manifest_key"``,
with the same meaning as a JSONPath in Amazon States Language.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rws.tasks.webhooks import check_url

StepId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetryPolicy(_Strict):
    """Retries for transient task errors: exponential backoff, capped, with full jitter."""

    max_attempts: int = Field(3, ge=1, le=10, description="Total attempts, including the first.")
    interval_seconds: int = Field(1, ge=1, le=300, description="Delay before the first retry.")
    backoff_rate: float = Field(2.0, ge=1.0, le=10.0, description="Delay multiplier per retry.")
    max_delay_seconds: int = Field(60, ge=1, le=3600, description="Longest delay between retries.")


class ValidateDatasetParams(_Strict):
    bucket: str
    prefix: str
    min_objects: int = Field(1, ge=0)
    allowed_extensions: list[str] | None = None


class ComputeChecksumsParams(_Strict):
    bucket: str
    prefix: str
    algorithm: Literal["sha256", "md5"] = "sha256"


class CopyObjectsParams(_Strict):
    source_bucket: str
    source_prefix: str
    dest_bucket: str
    dest_prefix: str


class NotifyParams(_Strict):
    message: str
    webhook_url: str | None = Field(
        None, description="https URL with a public host, or a $. reference", max_length=2048
    )

    @field_validator("webhook_url")
    @classmethod
    def _safe_webhook(cls, url: str | None) -> str | None:
        # References are resolved at run time; the task checks them before sending.
        if url is not None and not url.startswith("$."):
            check_url(url)
        return url


class _StepBase(_Strict):
    id: StepId
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    timeout_seconds: int = Field(300, ge=1, le=900)


class ValidateDatasetStep(_StepBase):
    action: Literal["validate_dataset"]
    params: ValidateDatasetParams


class ComputeChecksumsStep(_StepBase):
    action: Literal["compute_checksums"]
    params: ComputeChecksumsParams


class CopyObjectsStep(_StepBase):
    action: Literal["copy_objects"]
    params: CopyObjectsParams


class NotifyStep(_StepBase):
    action: Literal["notify"]
    params: NotifyParams


# Pydantic picks the right model by looking at the "action" field.
Step = Annotated[
    ValidateDatasetStep | ComputeChecksumsStep | CopyObjectsStep | NotifyStep,
    Field(discriminator="action"),
]


class FlowDefinition(_Strict):
    steps: list[Step] = Field(min_length=1, max_length=20)

    @field_validator("steps")
    @classmethod
    def _unique_ids(cls, steps: list[Step]) -> list[Step]:
        ids = [s.id for s in steps]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate step ids: {', '.join(duplicates)}")
        return steps
