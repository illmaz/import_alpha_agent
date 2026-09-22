#!/usr/bin/env bash
# Acceptance check: a report survives `docker compose restart fastapi`.
#
# The in-memory store of P1 Part 2 passed every unit test and would fail this.
# Usage:  ./scripts/verify_persistence.sh      (stack must already be up)
set -euo pipefail

API="${API:-http://localhost:8000}"

echo "==> creating a report"
REPORT_ID=$(curl -fsS -X POST "$API/v1/reports" \
  -H 'Content-Type: application/json' \
  -d '{"category":"home_organization","max_products":3}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["report_id"])')
echo "    report_id=$REPORT_ID"

echo "==> fetching it before the restart"
curl -fsS "$API/v1/reports/$REPORT_ID" > /dev/null
echo "    ok"

echo "==> docker compose restart fastapi"
docker compose restart fastapi >/dev/null

echo "==> waiting for the API to come back"
for _ in $(seq 1 30); do
  if curl -fsS "$API/health" >/dev/null 2>&1; then break; fi
  sleep 1
done

echo "==> fetching it after the restart"
if curl -fsS "$API/v1/reports/$REPORT_ID" \
  | python3 -c 'import json,sys
d = json.load(sys.stdin)
print("    survived:", d["report_id"], "status=" + d["report_status"], "opportunities=" + str(len(d["opportunities"])))'
then
  echo "PASS: report survived the restart"
else
  echo "FAIL: report was lost across the restart" >&2
  exit 1
fi
