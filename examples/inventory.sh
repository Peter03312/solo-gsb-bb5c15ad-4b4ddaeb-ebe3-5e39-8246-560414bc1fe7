#!/usr/bin/env bash
# Inventory-mode example calls against a locally running service
# (docker compose up).  Demonstrates: batch registration, atomic
# reservation, idempotent retry, confirm/cancel lifecycle.
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
RID="inv-demo-001"

echo "== register a stock batch (idempotent by batch id) =="
curl -sS -X POST "$BASE/batches" \
  -H "Content-Type: application/json" \
  --data @examples/batch.json | python3 -m json.tool

echo "== re-register the same batch -> 200; different content -> 409 =="
curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$BASE/batches" \
  -H "Content-Type: application/json" --data @examples/batch.json
curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$BASE/batches" \
  -H "Content-Type: application/json" \
  -d '{"id": "B-stock-1000", "concentration": "999", "initial_nl": 2000000}'

echo "== create an inventory-mode plan (reserves stock draws atomically) =="
curl -sS -X POST "$BASE/plans" \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: $RID" \
  --data @examples/inventory_request.json | python3 -m json.tool

echo "== batch now holds the reservation =="
curl -sS "$BASE/batches/B-stock-1000" | python3 -m json.tool

echo "== identical retry -> 200, reservation NOT doubled =="
curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$BASE/plans" \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: $RID" \
  --data @examples/inventory_request.json

echo "== same X-Request-ID, different body -> 409 =="
BASE="$BASE" RID="$RID" python3 - <<'PY'
import json, os, urllib.request, urllib.error
body = json.load(open("examples/inventory_request.json"))
body["wells"][0]["final_concentration"] = "250"
req = urllib.request.Request(
    os.environ["BASE"] + "/plans",
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json",
             "X-Request-ID": os.environ["RID"]},
    method="POST",
)
try:
    urllib.request.urlopen(req)
except urllib.error.HTTPError as e:
    print("status:", e.code)
    print(e.read().decode())
PY

echo "== reservation state =="
curl -sS "$BASE/plans/$RID/reservation" | python3 -m json.tool

echo "== confirm execution: reserved -> used (repeatable, same state) =="
curl -sS -X POST "$BASE/plans/$RID/confirm" | python3 -m json.tool
curl -sS -o /dev/null -w "confirm again: %{http_code}\n" -X POST "$BASE/plans/$RID/confirm"

echo "== cancel after confirm -> 409 =="
curl -sS -X POST "$BASE/plans/$RID/cancel" | python3 -m json.tool

echo "== batch after confirm (used_nl counted) =="
curl -sS "$BASE/batches/B-stock-1000" | python3 -m json.tool

echo "== second plan, then cancel: hold released exactly once =="
curl -sS -X POST "$BASE/plans" \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: inv-demo-002" \
  -d '{"batch_ids": ["B-stock-1000"],
       "diluent": {"available_nl": 5000000},
       "wells": [{"id": "A1", "final_volume_nl": 1000, "final_concentration": "500"}],
       "containers": [],
       "pipette": {"min_nl": 500, "max_nl": 1000, "step_nl": 100},
       "default_well_capacity_nl": 2000}' | python3 -m json.tool
curl -sS -X POST "$BASE/plans/inv-demo-002/cancel" | python3 -m json.tool
curl -sS -o /dev/null -w "cancel again: %{http_code}\n" -X POST "$BASE/plans/inv-demo-002/cancel"
curl -sS "$BASE/batches/B-stock-1000" | python3 -m json.tool
