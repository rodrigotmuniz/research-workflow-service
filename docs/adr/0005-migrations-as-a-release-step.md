# ADR 5: Database migrations run as a one-off release step

Status: accepted

## Context

The container used to run `alembic upgrade head` in its entrypoint before starting the
API. The ECS service runs two tasks (and up to four during a rolling deploy), so every
deploy started several concurrent upgrades against the same database. Alembic does not
lock: two processes can both see revision N and both try to apply N+1.

## Decision

The API container no longer migrates. Migrations are a separate one-off step that runs
the same image with a different command, once per release, before the new API version
is rolled out:

- **Docker compose**: a `migrate` service runs `alembic upgrade head` and exits; `api`
  depends on it with `condition: service_completed_successfully`.
- **ECS**: run the API task definition once with a command override, wait for it to
  stop with exit code 0, then update the service:

  ```bash
  aws ecs run-task --cluster "$CLUSTER" --launch-type FARGATE \
    --task-definition "$TASK_DEF" --network-configuration "$NETWORK" \
    --overrides '{"containerOverrides":[{"name":"api","command":["alembic","upgrade","head"]}]}'
  ```

Migrations must stay backward compatible with the running version (expand, then
contract in a later release), because old tasks keep serving during the rollout.

## Consequences

- Exactly one process migrates, and a failed migration stops the release before any new
  API task starts.
- The deploy pipeline has one more step. An alternative was a PostgreSQL advisory lock
  around the upgrade in every container; it works, but keeps schema changes coupled to
  API start-up (slow starts, failing health checks during long migrations) and does not
  work with SQLite.
