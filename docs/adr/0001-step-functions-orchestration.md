# ADR 1: AWS Step Functions for run orchestration

Status: accepted

## Context

A run is a sequence of steps that can take minutes to hours, touch large S3 datasets and
fail transiently. The service must not lose a run when an API container restarts, and
users need a clear status for each run.

## Decision

Compile each flow into an Amazon States Language state machine (`src/rws/asl.py`) and run
each step as a Lambda task. One state machine per flow, created the first time the flow
runs; one Standard execution per run, named after the run id.

- `Task` states with `ResultPath: $.results.<step_id>` keep the run input and every step
  result in one document, so later steps can reference earlier outputs.
- Retry policy per step maps to an ASL `Retry` block on `TransientError` and Lambda
  service errors, with `IntervalSeconds`, `BackoffRate`, `MaxDelaySeconds` and
  `"JitterStrategy": "FULL"` (the local executor computes the same capped, jittered
  delays); any other error fails the execution.
- Each Task state gets `TimeoutSeconds` from its step (`timeout_seconds`, at most 900).
  Every task Lambda has a 900-second timeout, Lambda's maximum, so the state's own
  timeout is the one that fires. Timeouts (`States.Timeout`, or `Sandbox.Timedout` /
  `Lambda.Unknown` from Lambda) are not retried: the step would likely time out again,
  and Step Functions does not stop the Lambda that is still running, so a retry would
  overlap with it. The run fails with the timeout as its error.
- Flows are immutable, so a state machine never needs updating.

## Consequences

- Durability, retries, timeouts and an audit trail come from a managed service.
- Execution names are unique per state machine, which makes "start" idempotent.
- Lock-in to AWS, and cost per state transition. For very high-volume, short steps an
  Express workflow or a queue-based worker pool would be cheaper.
- Alternatives considered: Temporal (portable, code-first, but another cluster to run),
  Airflow/Prefect/Dagster (better for scheduled batch DAGs than per-request runs), and a
  home-grown SQS worker (we would rebuild retries, timeouts and history ourselves).
