# ADR 2: Executor protocol and a local executor

Status: accepted

## Context

Developers and CI must run the whole system without an AWS account. LocalStack covers S3
well; Step Functions and Lambda emulation needs Docker-in-Docker and is slow and brittle
in CI.

## Decision

The API depends on a small `Executor` protocol (`start`, `cancel`, `describe`) in
`src/rws/executors/base.py`, with two implementations:

- `StepFunctionsExecutor`: creates the flow's state machine, starts/stops executions and
  maps execution status to run status (read on `GET /runs/{id}`).
- `LocalExecutor`: runs the same task handlers in a thread pool, with the same run
  document, `$.` reference semantics and retry policy, and writes status to the database.

Docker compose runs the API in local mode against PostgreSQL and LocalStack S3.

## Consequences

- Fast tests and a three-command local setup; the HTTP layer is tested once for both.
- The two executors could drift. Mitigations: shared handlers and retry code, the ASL
  compiler tested against a committed state machine, and the Step Functions executor
  tested against moto.
- The local executor is not durable (a restart loses in-flight runs). It is for
  development only, and the README says so.
