# ADR 3: At-least-once execution with idempotent tasks and idempotent run starts

Status: accepted

## Context

Exactly-once execution is not available end to end: Step Functions retries tasks, Lambda
can be invoked twice, clients retry on timeouts, and containers crash between writing to
the database and calling AWS.

## Decision

Accept at-least-once delivery and make every layer idempotent.

- **Run start.** Clients send an `Idempotency-Key` header. The key is unique per user
  (database constraint) and stored with a SHA-256 of the request. Same key and body:
  return the existing run (`200`). Same key, different body: `409`. Concurrent duplicates
  are resolved by the unique constraint.
- **Crash between insert and start.** The run stays `PENDING`. Retrying with the same key
  launches it again; this is safe because the execution name is the run id (Step
  Functions will not create a second execution) and the local executor ignores a run id
  it already started. Executor outages return `503` with `Retry-After`.
- **Tasks.** Checksums produce a deterministic manifest; copies skip destination objects
  with the same size and ETag; webhooks send an `Idempotency-Key` of `run_id:step_id`.
- **Status.** Transitions are compare-and-set updates, so late or duplicate updates
  cannot move a run out of a terminal status.

## Consequences

- Retries are always safe for clients and for the orchestrator.
- Runs started without a key can be duplicated by client retries; the API documents the
  header, and the CLI exposes `--idempotency-key`.
- A background reconciler for long-`PENDING` runs is still to do.
