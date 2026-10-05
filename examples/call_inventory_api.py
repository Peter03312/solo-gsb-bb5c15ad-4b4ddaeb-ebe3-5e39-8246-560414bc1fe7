#!/usr/bin/env python3
"""Inventory-mode flow against the planner HTTP API (standard library only).

Usage:
    python examples/call_inventory_api.py [base_url]
Default base URL: http://localhost:8000
"""
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"

BATCH = {"id": "B-stock-1000", "concentration": "1000", "initial_nl": 2_000_000}

with open("examples/inventory_request.json", encoding="utf-8") as fh:
    PLAN = json.load(fh)

REQUEST_ID = "inv-demo-001"


def call(path, body=None, method="POST", request_id=None):
    url = BASE + path
    headers = {"Content-Type": "application/json"}
    if request_id is not None:
        headers["X-Request-ID"] = request_id
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


print("1) register batch:")
status, batch = call("/batches", BATCH)
print("   HTTP", status, batch)

print("2) create inventory plan (reserves the stock draws):")
status, plan = call("/plans", PLAN, request_id=REQUEST_ID)
print("   HTTP", status, "state:", plan.get("state"))
print("   reservations:", plan.get("reservations"))
for t in plan["plan"]["transfers"]:
    src, dst = t["source"], t["destination"]
    print(f"   {t['step']:2d}. {src['kind']:>12} -> {dst['kind']:<13} "
          f"{t['volume_nl']:>5} nl")

print("3) identical retry (200, no double reservation):")
status, _ = call("/plans", PLAN, request_id=REQUEST_ID)
print("   HTTP", status)

print("4) batch inventory now:")
status, batch = call("/batches/B-stock-1000", method="GET")
print("   HTTP", status, {k: batch[k] for k in
                           ("initial_nl", "reserved_nl", "used_nl",
                            "available_nl")})

print("5) confirm execution (reserved -> used):")
status, result = call(f"/plans/{REQUEST_ID}/confirm")
print("   HTTP", status, result["state"], result["detail"])
status, result = call(f"/plans/{REQUEST_ID}/confirm")
print("   repeat confirm -> HTTP", status, result["state"])

print("6) cancel after confirm -> 409:")
status, result = call(f"/plans/{REQUEST_ID}/cancel")
print("   HTTP", status, result["error"], "-", result["detail"])

print("7) second plan, then cancel (hold released exactly once):")
body2 = {
    "batch_ids": ["B-stock-1000"],
    "diluent": {"available_nl": 5_000_000},
    "wells": [{"id": "A1", "final_volume_nl": 1000,
               "final_concentration": "500"}],
    "containers": [],
    "pipette": {"min_nl": 500, "max_nl": 1000, "step_nl": 100},
    "default_well_capacity_nl": 2000,
}
status, plan2 = call("/plans", body2, request_id="inv-demo-002")
print("   create -> HTTP", status, "reserved:", plan2.get("reservations"))
status, result = call("/plans/inv-demo-002/cancel")
print("   cancel -> HTTP", status, result["state"])
status, result = call("/plans/inv-demo-002/cancel")
print("   repeat cancel -> HTTP", status, result["state"], "(released once)")

print("8) final batch inventory:")
status, batch = call("/batches/B-stock-1000", method="GET")
print("   HTTP", status, {k: batch[k] for k in
                           ("initial_nl", "reserved_nl", "used_nl",
                            "available_nl")})
