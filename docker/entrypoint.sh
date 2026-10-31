#!/usr/bin/env bash
# Apply database migrations, then hand over to the main process (exec = PID 1,
# so it receives SIGTERM from ECS/docker and shuts down gracefully).
set -euo pipefail

if [[ "${RWS_SKIP_MIGRATIONS:-0}" != "1" ]]; then
  alembic upgrade head
fi
exec "$@"
