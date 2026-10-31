#!/usr/bin/env bash
# End-to-end smoke test against a running stack (docker compose up -d).
# Creates the example flow, starts a run and waits until it SUCCEEDS.
set -euo pipefail

API="${RWS_API_URL:-http://localhost:8000}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

json_field() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }

echo "==> waiting for $API/readyz"
for _ in $(seq 1 60); do
  curl -fsS "$API/readyz" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "$API/readyz" >/dev/null

echo "==> getting a dev token"
TOKEN=$(curl -fsS -X POST "$API/dev/token" -H 'Content-Type: application/json' \
  -d '{"subject": "smoke-user"}' | json_field '["access_token"]')
AUTH=(-H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json')

echo "==> creating flow"
FLOW_ID=$(curl -fsS -X POST "$API/flows" "${AUTH[@]}" \
  --data @"$ROOT/examples/archive-dataset.flow.json" | json_field '["id"]')
echo "    flow $FLOW_ID"

echo "==> starting run"
KEY="smoke-$(date +%s)"
INPUT=$(python3 -c "import json; print(json.dumps({'input': json.load(open('$ROOT/examples/archive-dataset.input.json'))}))")
RUN_ID=$(curl -fsS -X POST "$API/flows/$FLOW_ID/runs" "${AUTH[@]}" \
  -H "Idempotency-Key: $KEY" -d "$INPUT" | json_field '["id"]')
echo "    run $RUN_ID"

echo "==> retrying the same request (idempotency): must return the same run"
SAME_ID=$(curl -fsS -X POST "$API/flows/$FLOW_ID/runs" "${AUTH[@]}" \
  -H "Idempotency-Key: $KEY" -d "$INPUT" | json_field '["id"]')
[[ "$SAME_ID" == "$RUN_ID" ]] || { echo "FAIL: got a second run $SAME_ID"; exit 1; }

echo "==> polling run status"
for _ in $(seq 1 60); do
  STATUS=$(curl -fsS "$API/runs/$RUN_ID" "${AUTH[@]}" | json_field '["status"]')
  echo "    $STATUS"
  case "$STATUS" in
    SUCCEEDED) break ;;
    FAILED|CANCELLED) curl -fsS "$API/runs/$RUN_ID" "${AUTH[@]}"; exit 1 ;;
  esac
  sleep 1
done
[[ "$STATUS" == "SUCCEEDED" ]] || { echo "FAIL: run did not finish in time"; exit 1; }

curl -fsS "$API/runs/$RUN_ID" "${AUTH[@]}" | python3 -m json.tool
echo "==> smoke test passed"
