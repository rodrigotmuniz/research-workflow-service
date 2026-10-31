"""Compile a :class:`FlowDefinition` into an Amazon States Language (ASL) document.

Each step becomes a ``Task`` state that invokes the step's Lambda. The run document
(``{"run_id", "input", "results"}``) flows through every state; each task result is
stored under ``$.results.<step_id>`` so later steps can reference it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rws.flows import FlowDefinition

# Errors worth retrying: our own TransientError plus Lambda/Step Functions hiccups.
# Anything else (e.g. DatasetValidationError) fails the run immediately.
RETRYABLE_ERRORS = [
    "TransientError",
    "Lambda.ServiceException",
    "Lambda.SdkClientException",
    "Lambda.TooManyRequestsException",
    "States.Timeout",
]


def to_asl_parameters(params: Mapping[str, Any]) -> dict[str, Any]:
    """Turn ``{"bucket": "$.input.bucket"}`` into ASL's ``{"bucket.$": "$.input.bucket"}``."""
    out: dict[str, Any] = {}
    for key, value in params.items():
        if isinstance(value, str) and value.startswith("$."):
            out[f"{key}.$"] = value
        elif isinstance(value, Mapping):
            out[key] = to_asl_parameters(value)
        else:
            out[key] = value
    return out


def compile_to_asl(
    definition: FlowDefinition, task_arns: Mapping[str, str], comment: str = ""
) -> dict[str, Any]:
    """Build the state machine. ``task_arns`` maps each action to its Lambda ARN."""
    missing = sorted({s.action for s in definition.steps} - set(task_arns))
    if missing:
        raise ValueError(f"no task ARN configured for: {', '.join(missing)}")

    states: dict[str, Any] = {}
    steps = definition.steps
    for index, step in enumerate(steps):
        state: dict[str, Any] = {
            "Type": "Task",
            "Resource": task_arns[step.action],
            "Parameters": {
                "run_id.$": "$.run_id",
                "step_id": step.id,
                "params": to_asl_parameters(step.params.model_dump(exclude_none=True)),
            },
            "ResultPath": f"$.results.{step.id}",
            "TimeoutSeconds": step.timeout_seconds,
        }
        if step.retry.max_attempts > 1:
            state["Retry"] = [
                {
                    "ErrorEquals": RETRYABLE_ERRORS,
                    "IntervalSeconds": step.retry.interval_seconds,
                    "MaxAttempts": step.retry.max_attempts - 1,  # ASL counts retries only
                    "BackoffRate": step.retry.backoff_rate,
                }
            ]
        if index + 1 < len(steps):
            state["Next"] = steps[index + 1].id
        else:
            state["End"] = True
        states[step.id] = state

    return {"Comment": comment, "StartAt": steps[0].id, "States": states}
