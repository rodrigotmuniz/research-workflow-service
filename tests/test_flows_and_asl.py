import json
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from rws.asl import TEMPLATE_ARNS, compile_to_asl, main, to_asl_parameters
from rws.flows import MAX_STEP_TIMEOUT_SECONDS, FlowDefinition
from tests.conftest import ROOT


def test_example_flow_is_valid(example_flow: dict[str, Any]) -> None:
    definition = FlowDefinition.model_validate(example_flow["definition"])
    assert [s.action for s in definition.steps] == [
        "validate_dataset",
        "compute_checksums",
        "copy_objects",
        "notify",
    ]
    assert definition.steps[2].retry.max_attempts == 5


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ([], "at least 1 item"),
        ([{"id": "a", "action": "launch_rocket", "params": {}}], "does not match any"),
        ([{"id": "Bad-Id", "action": "notify", "params": {"message": "m"}}], "pattern"),
        ([{"id": "a", "action": "notify", "params": {"message": "m", "x": 1}}], "Extra inputs"),
        (
            [
                {"id": "a", "action": "notify", "params": {"message": "m"}},
                {"id": "a", "action": "notify", "params": {"message": "m"}},
            ],
            "duplicate step ids: a",
        ),
        (
            [
                {
                    "id": "a",
                    "action": "notify",
                    "params": {"message": "m"},
                    "retry": {"max_attempts": 0},
                }
            ],
            "greater than or equal to 1",
        ),
        (
            [{"id": "a", "action": "notify", "params": {"message": "m", "webhook_url": "ftp://x"}}],
            "must use https",
        ),
        (
            [
                {
                    "id": "a",
                    "action": "notify",
                    "params": {"message": "m", "webhook_url": "https://169.254.169.254/x"},
                }
            ],
            "non-public address",
        ),
    ],
)
def test_invalid_definitions_are_rejected(steps: list[Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        FlowDefinition.model_validate({"steps": steps})


def test_webhook_url_may_be_a_reference_checked_at_run_time() -> None:
    step = {"id": "n", "action": "notify", "params": {"message": "m", "webhook_url": "$.input.h"}}
    FlowDefinition.model_validate({"steps": [step]})


def test_to_asl_parameters_marks_references() -> None:
    params = {"bucket": "$.input.bucket", "n": 1, "nested": {"key": "$.results.a.k", "lit": "x"}}
    assert to_asl_parameters(params) == {
        "bucket.$": "$.input.bucket",
        "n": 1,
        "nested": {"key.$": "$.results.a.k", "lit": "x"},
    }


def test_compile_builds_a_linear_state_machine(example_flow: dict[str, Any]) -> None:
    definition = FlowDefinition.model_validate(example_flow["definition"])
    asl = compile_to_asl(definition, TEMPLATE_ARNS)

    assert asl["StartAt"] == "validate"
    states = asl["States"]
    assert [states[s].get("Next") for s in states] == ["checksums", "archive", "notify", None]
    assert states["notify"]["End"] is True
    archive = states["archive"]
    assert archive["ResultPath"] == "$.results.archive"
    assert archive["Parameters"]["params"]["dest_bucket.$"] == "$.input.archive_bucket"
    # 5 attempts in our model = 4 retries in ASL.
    assert archive["Retry"][0]["MaxAttempts"] == 4
    assert "TransientError" in archive["Retry"][0]["ErrorEquals"]
    # Same backoff shape as the local executor: capped, with full jitter.
    assert archive["Retry"][0]["MaxDelaySeconds"] == 60
    assert archive["Retry"][0]["JitterStrategy"] == "FULL"


def test_no_retry_block_when_single_attempt() -> None:
    definition = FlowDefinition.model_validate(
        {
            "steps": [
                {
                    "id": "n",
                    "action": "notify",
                    "params": {"message": "m"},
                    "retry": {"max_attempts": 1},
                }
            ]
        }
    )
    assert "Retry" not in compile_to_asl(definition, TEMPLATE_ARNS)["States"]["n"]


def test_timeouts_are_not_retried(example_flow: dict[str, Any]) -> None:
    """A timed-out Lambda may still be running; retrying would overlap with it."""
    definition = FlowDefinition.model_validate(example_flow["definition"])
    for state in compile_to_asl(definition, TEMPLATE_ARNS)["States"].values():
        errors = state["Retry"][0]["ErrorEquals"]
        assert "Lambda.ServiceException" in errors
        assert not {"States.Timeout", "Sandbox.Timedout", "Lambda.Unknown"} & set(errors)


def test_step_timeout_is_set_on_the_task_and_capped() -> None:
    step = {"id": "n", "action": "notify", "params": {"message": "m"}, "timeout_seconds": 42}
    asl = compile_to_asl(FlowDefinition.model_validate({"steps": [step]}), TEMPLATE_ARNS)
    assert asl["States"]["n"]["TimeoutSeconds"] == 42
    with pytest.raises(ValidationError, match="less than or equal to 900"):
        FlowDefinition.model_validate({"steps": [{**step, "timeout_seconds": 901}]})


def test_task_lambdas_can_run_as_long_as_the_longest_step() -> None:
    """The Lambda timeout must not be shorter than any step's TimeoutSeconds."""

    class Loader(yaml.SafeLoader):  # type: ignore[misc]
        pass

    Loader.add_multi_constructor("!", lambda loader, suffix, node: None)  # ignore !Ref etc.
    template = yaml.load((ROOT / "infra/template.yaml").read_text(), Loader=Loader)  # noqa: S506
    assert template["Globals"]["Function"]["Timeout"] >= MAX_STEP_TIMEOUT_SECONDS
    functions = [
        r for r in template["Resources"].values() if r["Type"] == "AWS::Serverless::Function"
    ]
    assert len(functions) == 4
    assert all("Timeout" not in f["Properties"] for f in functions)


def test_compile_requires_an_arn_per_action(example_flow: dict[str, Any]) -> None:
    definition = FlowDefinition.model_validate(example_flow["definition"])
    with pytest.raises(ValueError, match="copy_objects, notify"):
        compile_to_asl(definition, {"validate_dataset": "a", "compute_checksums": "b"})


def test_committed_asl_matches_the_compiler(capsys: pytest.CaptureFixture[str]) -> None:
    """infra/statemachines/*.asl.json must be regenerated when the compiler changes."""
    main([str(ROOT / "examples/archive-dataset.flow.json")])
    generated = json.loads(capsys.readouterr().out)
    committed = json.loads((ROOT / "infra/statemachines/archive-dataset.asl.json").read_text())
    assert generated == committed
