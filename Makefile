.PHONY: install lint test up smoke down run client asl

# Dev auth (the API mints its own tokens) is never the default: local targets opt in.
DEV_ENV = RWS_AUTH_MODE=dev RWS_DATABASE_URL=sqlite:///./rws.db RWS_LOG_JSON=false \
	AWS_ENDPOINT_URL=http://localhost:4566 AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test \
	AWS_DEFAULT_REGION=us-east-1

install:        ## Install dependencies (Python 3.12 via uv)
	uv sync

lint:           ## ruff + mypy + template lint
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run cfn-lint infra/template.yaml

test:           ## Unit + API tests with coverage
	uv run pytest --cov

up:             ## API + Postgres + LocalStack
	docker compose up -d --build --wait

smoke:          ## End-to-end: create flow, start run, wait for SUCCEEDED
	./scripts/smoke.sh

down:
	docker compose down -v

run:            ## API on the host (port 8001, SQLite, dev auth; S3 from `make up`)
	$(DEV_ENV) uv run alembic upgrade head
	$(DEV_ENV) uv run uvicorn rws.api.app:create_app --factory --reload --port 8001

client:         ## Regenerate openapi.json and the typed client
	./scripts/generate-client.sh

asl:            ## Recompile the example state machine
	uv run python -m rws.asl examples/archive-dataset.flow.json > infra/statemachines/archive-dataset.asl.json
