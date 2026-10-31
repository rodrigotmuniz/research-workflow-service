#!/usr/bin/env bash
# Regenerate openapi.json from the app, then the typed client models from it.
set -euo pipefail
cd "$(dirname "$0")/.."

uv run python -m rws.openapi > openapi.json
uv run datamodel-codegen \
  --input openapi.json --input-file-type openapi \
  --output src/rws/client/models.py \
  --output-model-type pydantic_v2.BaseModel \
  --target-python-version 3.12 \
  --use-standard-collections --use-union-operator --field-constraints \
  --disable-timestamp --formatters builtin
echo "openapi.json and src/rws/client/models.py are up to date"
