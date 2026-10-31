.PHONY: install lint test up smoke down client asl

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

client:         ## Regenerate openapi.json and the typed client
	./scripts/generate-client.sh

asl:            ## Recompile the example state machine
	uv run python -m rws.asl examples/archive-dataset.flow.json > infra/statemachines/archive-dataset.asl.json
